#!/usr/bin/env python3
from scapy.all import Ether, IP, UDP, Raw, sendp
from struct import pack
import time

IFACE = "enp9s0"

# L2 N3: UE -> UPF access
SRC_MAC = "06:ef:18:c6:68:d7"      # UE enp9s0
DST_MAC = "04:3f:72:fa:75:60"      # UPF enp7s0np0

# Outer GTP-U
GNB_IP = "192.168.70.100"
UPF_N3_IP = "192.168.70.1"

# Inner destination
MEC_IP = "192.168.90.2"

# pfcpsim com baseID=1 e SessionStep=10
TEIDS = [1, 11, 21, 31, 41, 51, 61, 71, 81, 91]

PKTS_PER_UE = 5

def gtpu_header(teid: int, inner_bytes: bytes) -> bytes:
    # flags=0x30, msgtype=0xff G-PDU, length=inner_len, teid=TEID
    return pack("!BBH", 0x30, 0xff, len(inner_bytes)) + pack("!I", teid)

print(f"Sending {PKTS_PER_UE} packets for each of {len(TEIDS)} UEs on {IFACE}")

sent = 0

for idx, teid in enumerate(TEIDS, start=1):
    ue_ip = f"10.250.0.{idx}"
    sport = 40000 + idx
    dport = 5000 + idx

    payload = f"hello-from-ue-{idx}-teid-{teid}".encode()

    inner = (
        IP(src=ue_ip, dst=MEC_IP) /
        UDP(sport=sport, dport=dport) /
        Raw(payload)
    )

    gtpu = gtpu_header(teid, bytes(inner))

    pkt = (
        Ether(src=SRC_MAC, dst=DST_MAC) /
        IP(src=GNB_IP, dst=UPF_N3_IP) /
        UDP(sport=2152, dport=2152) /
        Raw(gtpu + bytes(inner))
    )

    for n in range(PKTS_PER_UE):
        sendp(pkt, iface=IFACE, verbose=False)
        sent += 1
        print(f"sent ue={idx:02d} ue_ip={ue_ip} teid={teid} udp={sport}->{dport} pkt={n+1}")
        time.sleep(0.05)

print(f"Done. Total packets sent: {sent}")
