"""
ue_sender.py  (raw socket nativo, sem overhead do Scapy no hot-path)
--------------------------------------------------------------------
UE (pod) com poder de computação:
  1. roda o split-CNN até o ponto de corte k (head)
  2. serializa a ativação intermediária (com quantização opcional)
  3. monta Ether/IP/UDP(2152)/GTP-U/IPv6/SRH(+TLV split)/Raw(tensor)
  4. injeta na N3 (net1) via AF_PACKET raw socket nativo do kernel

Scapy é usado SÓ uma vez, para montar o template do pacote e garantir o
formato correto do SRH/TLV. No loop quente, os bytes são montados à mão e
enviados via socket.send() nativo — muito mais rápido e sem ENOBUFS por
reconstrução. O cabeçalho é pré-computado; só o payload e os campos de
comprimento/fragmentação mudam por pacote.

Requer: CAP_NET_RAW + CAP_NET_ADMIN no pod, e net1 (n3-net via OVS).
"""
import argparse
import glob
import os
import socket
import struct
import time

import torch

from split_model import SimpleBackbone, serialize_activation

# Tipo de TLV experimental para o "split-level" (124-127 reservados p/ experimentação).
SPLIT_TLV_TYPE = 124


def get_mac(iface: str) -> bytes:
    with open(f"/sys/class/net/{iface}/address") as f:
        return bytes.fromhex(f.read().strip().replace(":", ""))


def mac_to_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


def ipv4_to_bytes(ip: str) -> bytes:
    return socket.inet_aton(ip)


def ipv6_to_bytes(ip: str) -> bytes:
    return socket.inet_pton(socket.AF_INET6, ip)


def ip_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack(f">{len(data)//2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


def build_split_tlv(k, dtype_code, frag_off, frag_total, scale, shape) -> bytes:
    """
    TLV do SRH com split-level + metadados do tensor + fragmentação.
    value layout:
      [0] k  [1] dtype  [2:4] frag_off  [4:6] frag_total
      [6:10] scale(f32)  [10] ndim  [11:..] shape dims (u16 cada)
    Retorna o TLV completo (type, len, value) pronto pra concatenar no SRH.
    """
    val = struct.pack(">BBHHfB", k & 0xFF, dtype_code & 0xFF,
                      frag_off & 0xFFFF, frag_total & 0xFFFF,
                      float(scale), len(shape) & 0xFF)
    for d in shape:
        val += struct.pack(">H", d & 0xFFFF)
    # padding p/ múltiplo de 8 (alinhamento do SRH)
    pad = (-(len(val) + 2)) % 8
    val += b"\x00" * pad
    return struct.pack(">BB", SPLIT_TLV_TYPE, len(val)) + val


def build_srh(sid_bytes: bytes, tlv: bytes) -> bytes:
    """
    Segment Routing Header (RFC 8754) com 1 segmento + TLV.
    next_header=4 (IPv4? não) — aqui o SRH encapsula nada acima (payload cru),
    usamos next_header=59 (No Next Header) e o tensor vai como payload do IPv6.
    Hdr Ext Len em unidades de 8 bytes, sem contar os primeiros 8.
    """
    routing_type = 4          # Segment Routing
    segments_left = 0
    last_entry = 0
    flags = 0
    tag = 0
    # corpo: [segments(16B cada)] + [TLVs]
    body = sid_bytes + tlv
    total_len = 8 + len(body)             # 8 = bytes fixos do SRH
    hdr_ext_len = (total_len // 8) - 1
    nh = 59                               # No Next Header (payload cru segue)
    srh = struct.pack(">BBBBBBH", nh, hdr_ext_len, routing_type,
                      segments_left, last_entry, flags, tag)
    return srh + body


def build_headers(args, src_mac, dst_mac, k, dtype_code, scale, shape):
    """
    Pré-computa tudo que NÃO muda por fragmento (Ether/IP/UDP/GTP/IPv6 base).
    O SRH/TLV muda só nos campos frag_off (recalculado por pacote), então
    montamos o pacote final em assemble_packet().
    """
    eth = dst_mac + src_mac + struct.pack(">H", 0x0800)   # EtherType IPv4
    return eth


def assemble_packet(eth, args, sid6, src6, k, dtype_code, scale, shape,
                    frag_off, frag_total, payload):
    """Monta o pacote completo em bytes para um fragmento."""
    tlv = build_split_tlv(k, dtype_code, frag_off, frag_total, scale, shape)
    srh = build_srh(sid6, tlv)
    inner = srh + payload                      # depois do IPv6 vem SRH + tensor

    # IPv6 header (40B): paylen = len(srh+payload), next header = 43 (Routing)
    ver_tc_fl = (6 << 28)
    ipv6 = struct.pack(">IHBB", ver_tc_fl, len(inner), 43, 64)  # nh=43 routing, hop=64
    ipv6 += src6 + sid6

    gtp_payload = ipv6 + inner
    # GTP-U header (8B): flags=0x30 (v1, PT=1), msg=0xFF (G-PDU), len, TEID
    gtp = struct.pack(">BBHI", 0x30, 0xFF, len(gtp_payload), args.teid)

    udp_payload = gtp + gtp_payload
    udp_len = 8 + len(udp_payload)
    udp = struct.pack(">HHHH", 2152, 2152, udp_len, 0)   # checksum 0 (opcional IPv4)

    ip_payload = udp + udp_payload
    ip_total = 20 + len(ip_payload)
    ip_hdr = struct.pack(">BBHHHBBH", 0x45, 0, ip_total, 0, 0, 64, 17, 0)
    ip_hdr += ipv4_to_bytes(args.n3_src) + ipv4_to_bytes(args.upf_n3)
    csum = ip_checksum(ip_hdr)
    ip_hdr = ip_hdr[:10] + struct.pack(">H", csum) + ip_hdr[12:]

    return eth + ip_hdr + ip_payload


def load_frame_paths(data_dir):
    """Lista imagens do dataset (jpg/png). Retorna [] se vazio/inexistente."""
    if not data_dir or not os.path.isdir(data_dir):
        return []
    paths = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.PNG"):
        paths.extend(glob.glob(os.path.join(data_dir, "**", ext), recursive=True))
    return sorted(paths)


def frame_from_path(path, device):
    """Carrega imagem -> tensor (1,3,640,640) normalizado. Usa PIL se disponível."""
    from PIL import Image
    import numpy as np
    img = Image.open(path).convert("RGB").resize((640, 640))
    arr = np.asarray(img, dtype="float32").transpose(2, 0, 1) / 255.0
    return torch.from_numpy(arr).unsqueeze(0).to(device)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="net1")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--quant", default="fp16", choices=["fp32", "fp16", "int8"])
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--chunk", type=int, default=1300,
                    help="bytes de payload por fragmento (1300 cabe em MTU 1500 c/ folga; validado fim-a-fim)")
    ap.add_argument("--data-dir", default="/data",
                    help="diretório do dataset (imagens). Se vazio, usa frame sintético")
    ap.add_argument("--auto-ordinal", action="store_true")
    ap.add_argument("--base-teid", type=int, default=10)
    ap.add_argument("--next-hop-mac", required=True)
    ap.add_argument("--n3-src", default="192.168.70.10")
    ap.add_argument("--upf-n3", default="192.168.70.1")
    ap.add_argument("--teid", type=lambda x: int(x, 0), default=10)
    ap.add_argument("--ipv6-src", default="2001:db8::10")
    ap.add_argument("--srv6-sid", default="fc00:0:0:1::1")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    if args.auto_ordinal:
        host = socket.gethostname()
        try:
            ordn = int(host.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            ordn = 0
        args.teid = args.base_teid + ordn
        last = 10 + ordn
        args.n3_src = f"192.168.70.{last}"
        args.ipv6_src = f"2001:db8::{last}"
        print(f"[UE] auto-ordinal ue-{ordn}: teid={args.teid} n3_src={args.n3_src}", flush=True)

    src_mac = get_mac(args.iface)
    dst_mac = mac_to_bytes(args.next_hop_mac)
    sid6 = ipv6_to_bytes(args.srv6_sid)
    src6 = ipv6_to_bytes(args.ipv6_src)
    eth = build_headers(args, src_mac, dst_mac, None, None, None, None)

    # AF_PACKET raw socket nativo, ligado à interface (envio L2 cru, rápido)
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((args.iface, 0))
    # aumenta o buffer TX para reduzir ENOBUFS sob rajada
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
    except OSError:
        pass

    model = SimpleBackbone().eval().to(args.device)
    dtype_map = {"fp32": 0, "fp16": 1, "int8": 2}

    # dataset: lista frames; se vazio, cai pra frame sintético
    frame_paths = load_frame_paths(args.data_dir)
    use_dataset = len(frame_paths) > 0
    print(f"[UE] iface={args.iface} mac={src_mac.hex(':')} k={args.k} "
          f"quant={args.quant} device={args.device} sid={args.srv6_sid} "
          f"dataset={'%d frames' % len(frame_paths) if use_dataset else 'sintético'}",
          flush=True)

    period = 1.0 / args.fps if args.fps > 0 else 0
    i = 0
    while args.loop == 0 or i < args.loop:
        t0 = time.perf_counter()
        if use_dataset:
            frame = frame_from_path(frame_paths[i % len(frame_paths)], args.device)
        else:
            frame = torch.randn(1, 3, 640, 640, device=args.device)
        with torch.no_grad():
            act = model.forward_head(frame, args.k)
        infer_ms = (time.perf_counter() - t0) * 1000

        payload, meta = serialize_activation(act.cpu(), args.quant)
        total = len(payload)
        n_frags = (total + args.chunk - 1) // args.chunk
        dtype_code = meta["dtype"]
        scale = meta["scale"]
        shape = meta["shape"]

        sent = 0
        for f in range(n_frags):
            chunk = payload[f * args.chunk:(f + 1) * args.chunk]
            pkt = assemble_packet(eth, args, sid6, src6, args.k, dtype_code,
                                  scale, shape, f, n_frags, chunk)
            retries = 0
            while True:
                try:
                    sock.send(pkt)
                    sent += 1
                    break
                except OSError as e:
                    if e.errno == 105:           # ENOBUFS
                        retries += 1
                        if retries > 5000:
                            raise RuntimeError(f"frag {f} ENOBUFS persistente")
                        time.sleep(0.0002)
                        continue
                    raise
        tx_ms = (time.perf_counter() - t0) * 1000 - infer_ms
        print(f"[UE] frame={i} head_infer={infer_ms:.1f}ms tx={tx_ms:.1f}ms "
              f"bytes={total} frags={n_frags} sent={sent}", flush=True)
        i += 1
        dt = period - (time.perf_counter() - t0)
        if dt > 0:
            time.sleep(dt)
    sock.close()


if __name__ == "__main__":
    main()

