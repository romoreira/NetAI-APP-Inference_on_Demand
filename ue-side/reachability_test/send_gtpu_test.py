#!/usr/bin/env python3
from scapy.all import Ether, IP, UDP, Raw, sendp
from struct import pack
import time

IFACE = "enp9s0"

# L2 N3
SRC_MAC = "06:ef:18:c6:68:d7"      # UE enp9s0
DST_MAC = "04:3f:72:fa:75:60"      # UPF enp7s0np0

# Outer GTP-U
GNB_IP = "192.168.70.100"
UPF_N3_IP = "192.168.70.1"
TEID = 1

# Inner packet
UE_IP = "10.250.0.1"
MEC_IP = "192.168.90.2"

inner = IP(src=UE_IP, dst=MEC_IP) / UDP(sport=40001, dport=5001) / Raw(b"hello-from-ue-teid-1")

# GTP-U header: flags=0x30, msgtype=0xff, length=inner_len, teid=TEID
gtpu = pack("!BBH", 0x30, 0xff, len(bytes(inner))) + pack("!I", TEID)

pkt = (
    Ether(src=SRC_MAC, dst=DST_MAC) /
    IP(src=GNB_IP, dst=UPF_N3_IP) /
    UDP(sport=2152, dport=2152) /
    Raw(gtpu + bytes(inner))
)

print(f"Sending 5 GTP-U packets on {IFACE}: TEID={TEID}, inner {UE_IP} -> {MEC_IP}:5001")

for i in range(5):
    sendp(pkt, iface=IFACE, verbose=False)
    print(f"sent {i+1}")
    time.sleep(0.2)
