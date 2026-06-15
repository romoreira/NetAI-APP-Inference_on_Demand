# UE Side

This directory contains the UE-side traffic generation scripts used to validate the inference-on-demand datapath through the UPF.

The UE side is responsible for generating synthetic GTP-U traffic toward the UPF N3 interface. PFCP session creation is documented separately in the `pfcpsim` README.

## Validated topology

### N3 data plane

| Role | Interface | IP | MAC |
|---|---|---:|---:|
| UE N3 sender | `enp9s0` | `192.168.70.100` | `06:ef:18:c6:68:d7` |
| UPF N3 ingress | `enp7s0np0` | `192.168.70.1` | `04:3f:72:fa:75:60` |

### N6 target

| Role | Interface | IP | MAC |
|---|---|---:|---:|
| MEC N6 ingress | `enp7s0` | `192.168.90.2` | `3e:89:68:ca:1a:c8` |

## Requirements

Install Scapy:

```bash
sudo apt update
sudo apt install -y python3-pip
pip3 install scapy
```

If Scapy is installed under the `ubuntu` user site-packages directory, run the scripts with:

```bash
USER_SITE=$(python3 -c 'import site; print(site.getusersitepackages())')
sudo env PYTHONPATH="$USER_SITE" python3 <script.py>
```

Do not install `scapy.all` as a package. `scapy.all` is a Python module inside the `scapy` package.

## Send one GTP-U test flow

Create the single-session GTP-U test script:

```bash
cat > ~/send_gtpu_test.py <<'PY'
#!/usr/bin/env python3
from scapy.all import Ether, IP, UDP, Raw, sendp
from struct import pack
import time

IFACE = "enp9s0"

SRC_MAC = "06:ef:18:c6:68:d7"
DST_MAC = "04:3f:72:fa:75:60"

GNB_IP = "192.168.70.100"
UPF_N3_IP = "192.168.70.1"
TEID = 1

UE_IP = "10.250.0.1"
MEC_IP = "192.168.90.2"

inner = IP(src=UE_IP, dst=MEC_IP) / UDP(sport=40001, dport=5001) / Raw(b"hello-from-ue-teid-1")
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
PY

chmod +x ~/send_gtpu_test.py
```

Run it:

```bash
USER_SITE=$(python3 -c 'import site; print(site.getusersitepackages())')
sudo env PYTHONPATH="$USER_SITE" python3 ~/send_gtpu_test.py
```

Expected MEC-side output:

```text
04:3f:72:fa:75:61 > 3e:89:68:ca:1a:c8, ethertype IPv4
10.250.0.1.40001 > 192.168.90.2.5001: UDP
```

## Send traffic for 10 UE sessions

This script sends five packets per UE session. It assumes the PFCP simulator created 10 sessions with base ID 1 and step 10, producing TEIDs:

```text
1, 11, 21, 31, 41, 51, 61, 71, 81, 91
```

Create the script:

```bash
cat > ~/send_gtpu_10ues.py <<'PY'
#!/usr/bin/env python3
from scapy.all import Ether, IP, UDP, Raw, sendp
from struct import pack
import time

IFACE = "enp9s0"

SRC_MAC = "06:ef:18:c6:68:d7"
DST_MAC = "04:3f:72:fa:75:60"

GNB_IP = "192.168.70.100"
UPF_N3_IP = "192.168.70.1"
MEC_IP = "192.168.90.2"

TEIDS = [1, 11, 21, 31, 41, 51, 61, 71, 81, 91]
PKTS_PER_UE = 5

def gtpu_header(teid: int, inner_bytes: bytes) -> bytes:
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
PY

chmod +x ~/send_gtpu_10ues.py
```

Run it:

```bash
USER_SITE=$(python3 -c 'import site; print(site.getusersitepackages())')
sudo env PYTHONPATH="$USER_SITE" python3 ~/send_gtpu_10ues.py
```

Expected MEC-side flows:

```text
10.250.0.1  -> 192.168.90.2:5001
10.250.0.2  -> 192.168.90.2:5002
...
10.250.0.10 -> 192.168.90.2:5010
```

## Validated result

The validated UE-side scripts generated GTP-U traffic that reached the MEC after UPF processing. The UPF counters confirmed:

```text
splitController: 105 packets
enp8s0np0Routes gate 0: 105 packets
n6MecDstEther: 105 packets
enp8s0np0Fast TX: 105 packets
drops: 0
```

