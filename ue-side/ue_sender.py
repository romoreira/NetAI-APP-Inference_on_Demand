#!/usr/bin/env python3
import argparse
import socket
import struct
import time
from pathlib import Path

import torch

from split_model import SimpleBackbone, serialize_activation

SPLIT_TLV_TYPE = 124


def get_mac(iface: str) -> bytes:
    with open(f"/sys/class/net/{iface}/address") as f:
        return bytes.fromhex(f.read().strip().replace(":", ""))


def mac_to_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


def ipv4_bytes(ip: str) -> bytes:
    return socket.inet_aton(ip)


def csum16(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack(f">{len(data)//2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


def ipv4_header(src: str, dst: str, proto: int, payload_len: int, ident: int = 0) -> bytes:
    total_len = 20 + payload_len
    hdr = struct.pack(
        ">BBHHHBBH4s4s",
        0x45,
        0,
        total_len,
        ident & 0xFFFF,
        0,
        64,
        proto,
        0,
        ipv4_bytes(src),
        ipv4_bytes(dst),
    )
    chk = csum16(hdr)
    return hdr[:10] + struct.pack(">H", chk) + hdr[12:]


def udp_header(src_port: int, dst_port: int, payload_len: int) -> bytes:
    udp_len = 8 + payload_len
    return struct.pack(">HHHH", src_port, dst_port, udp_len, 0)


def build_split_tlv(k, dtype_code, frag_off, frag_total, scale, shape) -> bytes:
    val = struct.pack(
        ">BBHHfB",
        k & 0xFF,
        dtype_code & 0xFF,
        frag_off & 0xFFFF,
        frag_total & 0xFFFF,
        float(scale),
        len(shape) & 0xFF,
    )

    for d in shape:
        val += struct.pack(">H", int(d) & 0xFFFF)

    if len(val) > 255:
        raise ValueError(f"TLV too large: {len(val)} bytes")

    return struct.pack(">BB", SPLIT_TLV_TYPE, len(val)) + val


def load_frame_paths(data_dir):
    if not data_dir:
        return []

    root = Path(data_dir)
    if not root.exists():
        return []

    exts = ("*.jpg", "*.jpeg", "*.png", "*.bmp")
    paths = []
    for ext in exts:
        paths.extend(root.glob(ext))

    return sorted(paths)


def frame_from_path(path, device):
    from PIL import Image
    import numpy as np

    img = Image.open(path).convert("RGB").resize((640, 640))
    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)
    x = torch.from_numpy(arr).unsqueeze(0).to(device)
    return x


def assemble_packet(
    src_mac: bytes,
    dst_mac: bytes,
    outer_src_ip: str,
    outer_dst_ip: str,
    teid: int,
    inner_src_ip: str,
    inner_dst_ip: str,
    inner_src_port: int,
    inner_dst_port: int,
    k: int,
    dtype_code: int,
    scale: float,
    shape,
    frag_off: int,
    frag_total: int,
    chunk: bytes,
) -> bytes:
    tlv = build_split_tlv(k, dtype_code, frag_off, frag_total, scale, shape)
    inner_udp_payload = tlv + chunk

    inner_udp = udp_header(inner_src_port, inner_dst_port, len(inner_udp_payload))
    inner_ip_payload = inner_udp + inner_udp_payload

    inner_ip = ipv4_header(
        inner_src_ip,
        inner_dst_ip,
        proto=17,
        payload_len=len(inner_ip_payload),
        ident=frag_off,
    )

    inner_packet = inner_ip + inner_ip_payload

    gtp = struct.pack(">BBHI", 0x30, 0xFF, len(inner_packet), teid)

    outer_udp_payload = gtp + inner_packet
    outer_udp = udp_header(2152, 2152, len(outer_udp_payload))
    outer_ip_payload = outer_udp + outer_udp_payload

    outer_ip = ipv4_header(
        outer_src_ip,
        outer_dst_ip,
        proto=17,
        payload_len=len(outer_ip_payload),
        ident=frag_off,
    )

    eth = dst_mac + src_mac + struct.pack(">H", 0x0800)

    return eth + outer_ip + outer_ip_payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="net1")
    ap.add_argument("--data-dir", default="/data")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--quant", default="fp16", choices=["fp32", "fp16", "int8"])
    ap.add_argument("--fps", type=float, default=5.0)
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--chunk", type=int, default=1300)
    ap.add_argument("--next-hop-mac", required=True)
    ap.add_argument("--n3-src", default="192.168.70.100")
    ap.add_argument("--upf-n3", default="192.168.70.1")
    ap.add_argument("--teid", type=lambda x: int(x, 0), default=1)
    ap.add_argument("--ue-ip", default="10.250.0.1")
    ap.add_argument("--mec-ip", default="192.168.90.2")
    ap.add_argument("--mec-port", type=int, default=5001)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    src_mac = get_mac(args.iface)
    dst_mac = mac_to_bytes(args.next_hop_mac)

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((args.iface, 0))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)

    model = SimpleBackbone().eval().to(args.device)

    frame_paths = load_frame_paths(args.data_dir)
    use_dataset = len(frame_paths) > 0

    print(
        f"[UE-IPV4] iface={args.iface} mac={src_mac.hex(':')} "
        f"teid={args.teid} inner={args.ue_ip}:40001->{args.mec_ip}:{args.mec_port} "
        f"k={args.k} quant={args.quant} device={args.device} "
        f"dataset={'%d frames' % len(frame_paths) if use_dataset else 'synthetic'}",
        flush=True,
    )

    period = 1.0 / args.fps if args.fps > 0 else 0.0

    for i in range(args.loop):
        t0 = time.time()

        if use_dataset:
            frame = frame_from_path(frame_paths[i % len(frame_paths)], args.device)
        else:
            frame = torch.randn(1, 3, 640, 640, device=args.device)

        with torch.no_grad():
            ti = time.time()
            act = model.forward_head(frame, args.k)
            if str(args.device).startswith("cuda"):
                torch.cuda.synchronize()
            infer_ms = (time.time() - ti) * 1000.0

        payload, meta = serialize_activation(act, args.quant)
        dtype_code = int(meta["dtype"])
        scale = float(meta["scale"])
        shape = meta["shape"]

        total = len(payload)
        n_frags = (total + args.chunk - 1) // args.chunk

        tx0 = time.time()
        sent = 0

        for f in range(n_frags):
            chunk = payload[f * args.chunk:(f + 1) * args.chunk]

            pkt = assemble_packet(
                src_mac=src_mac,
                dst_mac=dst_mac,
                outer_src_ip=args.n3_src,
                outer_dst_ip=args.upf_n3,
                teid=args.teid,
                inner_src_ip=args.ue_ip,
                inner_dst_ip=args.mec_ip,
                inner_src_port=40001,
                inner_dst_port=args.mec_port,
                k=args.k,
                dtype_code=dtype_code,
                scale=scale,
                shape=shape,
                frag_off=f,
                frag_total=n_frags,
                chunk=chunk,
            )

            sock.send(pkt)
            sent += 1

        tx_ms = (time.time() - tx0) * 1000.0

        print(
            f"[UE-IPV4] frame={i} head_infer={infer_ms:.1f}ms "
            f"tx={tx_ms:.1f}ms bytes={total} frags={n_frags} sent={sent}",
            flush=True,
        )

        elapsed = time.time() - t0
        if period > elapsed:
            time.sleep(period - elapsed)

    sock.close()


if __name__ == "__main__":
    main()
