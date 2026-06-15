"""
mec_receiver.py  —  servidor MEC (atrás da N6)
----------------------------------------------
Recebe pacotes IPv6/SRH/TLV/tensor (o BESS lê o TLV mas repassa intacto),
remonta os fragmentos, reconstrói o tensor de ativação e roda forward_tail
na GPU para completar a inferência (camadas k+1..N + cabeça de detecção).

Espelha exatamente o formato do ue_sender.py:
  IPv6(40B) / SRH(8B + 16B SID + TLV) / Raw(chunk de tensor)
  TLV value: [k][dtype][frag_off:2][frag_total:2][scale:4][ndim][dims:2*ndim]

Recebe via AF_PACKET raw socket na interface da N6 (ou onde o BESS entregar).
Agrupa fragmentos por (src_teid_proxy) — aqui usamos (frag_total, shape, scale)
+ contador de chegada; quando todos os frag de um frame chegam, reconstrói.

Requer: CAP_NET_RAW. Mesmo split_model.py e .pth do UE.
"""
import argparse
import socket
import struct
import time

import torch

from split_model import load_fire_resnet18, deserialize_activation

SPLIT_TLV_TYPE = 124
ETH_P_ALL = 0x0003


def parse_packet(buf):
    """
    Parseia um frame recebido. Retorna dict com k, dtype, frag_off,
    frag_total, scale, shape, e o chunk de payload — ou None se não for
    um pacote nosso (sem SRH/TLV esperado).

    O pacote que chega depois do decap do BESS pode vir com ou sem o
    cabeçalho Ethernet, e com ou sem o IPv4/UDP/GTP externos, dependendo
    de quanto o BESS remove. Aqui tratamos o caso geral: localizamos o
    IPv6 (versão 6) e parseamos a partir dele.
    """
    # tenta achar o início do IPv6: procuramos um byte cujo nibble alto == 6
    # nas posições típicas (0 = sem eth; 14 = com eth). Robustez mínima.
    for off in (14, 0):
        if off + 40 > len(buf):
            continue
        if (buf[off] >> 4) == 6:
            return _parse_from_ipv6(buf, off)
    return None


def _parse_from_ipv6(buf, off):
    # IPv6 header: 40 bytes. next header em buf[off+6]
    nh = buf[off + 6]
    payload_len = struct.unpack(">H", buf[off + 4:off + 6])[0]
    p = off + 40
    if nh != 43:                      # 43 = Routing header (SRH)
        return None
    # SRH: nh(1) hdrlen(1) type(1) segleft(1) lastentry(1) flags(1) tag(2)
    srh_nh = buf[p]
    hdr_ext_len = buf[p + 1]
    srh_type = buf[p + 2]
    if srh_type != 4:                 # 4 = Segment Routing
        return None
    srh_total = (hdr_ext_len + 1) * 8
    srh_body = buf[p + 8: p + srh_total]
    # body = SID(16B) + TLV
    sid = srh_body[:16]
    tlv = srh_body[16:]
    if len(tlv) < 2 or tlv[0] != SPLIT_TLV_TYPE:
        return None
    tlv_len = tlv[1]
    val = tlv[2:2 + tlv_len]
    k, dtype, frag_off, frag_total, scale, ndim = struct.unpack(">BBHHfB", val[:11])
    dims = [struct.unpack(">H", val[11 + 2 * i:13 + 2 * i])[0] for i in range(ndim)]
    # payload = tudo depois do SRH, dentro do payload_len do IPv6
    chunk = buf[p + srh_total: off + 40 + payload_len]
    return {
        "k": k, "dtype": dtype, "frag_off": frag_off, "frag_total": frag_total,
        "scale": scale, "shape": dims, "chunk": bytes(chunk),
    }


class FrameAssembler:
    """Agrupa fragmentos de um frame até completar, depois reconstrói."""
    def __init__(self):
        self.buffers = {}     # key -> {frag_off: chunk}
        self.meta = {}        # key -> (k, dtype, scale, shape, frag_total)

    def add(self, info):
        # chave do frame: (frag_total, tuple(shape), scale arredondado)
        key = (info["frag_total"], tuple(info["shape"]), round(info["scale"], 6))
        self.buffers.setdefault(key, {})[info["frag_off"]] = info["chunk"]
        self.meta[key] = (info["k"], info["dtype"], info["scale"],
                          info["shape"], info["frag_total"])
        if len(self.buffers[key]) == info["frag_total"]:
            return self._reconstruct(key)
        return None

    def _reconstruct(self, key):
        frags = self.buffers.pop(key)
        k, dtype, scale, shape, frag_total = self.meta.pop(key)
        payload = b"".join(frags[i] for i in range(frag_total))
        meta = {"shape": shape, "dtype": dtype, "scale": scale}
        tensor = deserialize_activation(payload, meta)
        return k, tensor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="eth0", help="interface onde o MEC recebe (N6)")
    ap.add_argument("--weights", default="/models/best.pth", help="caminho do best.pth (ResNet-18 fire)")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    import os
    if args.weights and os.path.isfile(args.weights):
        model, missing, unexpected = load_fire_resnet18(args.weights, map_location=args.device)
        model = model.eval().to(args.device)
        print(f"[MEC] best.pth carregado de {args.weights} "
              f"({len(missing)} missing, {len(unexpected)} unexpected)", flush=True)
    else:
        from split_model import SplitResNet18
        model = SplitResNet18().eval().to(args.device)
        print(f"[MEC] sem pesos em '{args.weights}': aleatórios (só valida transporte)", flush=True)

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind((args.iface, 0))
    asm = FrameAssembler()
    print(f"[MEC] escutando em {args.iface} device={args.device}", flush=True)

    n_frames = 0
    while True:
        buf = sock.recv(65535)
        info = parse_packet(buf)
        if info is None:
            continue
        done = asm.add(info)
        if done is not None:
            k, act = done
            t0 = time.perf_counter()
            with torch.no_grad():
                out = model.forward_tail(act.to(args.device), k)
            if args.device == "cuda":
                torch.cuda.synchronize()
            dt = (time.perf_counter() - t0) * 1000
            n_frames += 1
            print(f"[MEC] frame#{n_frames} k={k} act={tuple(act.shape)} "
                  f"-> det={tuple(out.shape)} tail_infer={dt:.1f}ms", flush=True)


if __name__ == "__main__":
    main()

