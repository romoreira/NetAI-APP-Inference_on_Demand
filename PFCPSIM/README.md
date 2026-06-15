# PFCPsim

This directory documents how to build and run the PFCP simulator used to install PFCP sessions in the UPF.

The PFCP simulator runs on the UE node using host networking. It connects to the UPF PFCP endpoint on N4 and creates the PDR, FAR, and QER rules used by BESS.

## Validated topology

### N4 / PFCP control plane

| Role | Interface | IP |
|---|---|---:|
| PFCPsim node | `enp7s0` | `192.168.80.2` |
| UPF PFCP endpoint | `enp9s0` | `192.168.80.1` |

### N3 information used by PFCP sessions

| Role | IP |
|---|---:|
| UE/GNB N3 endpoint | `192.168.70.100` |
| UPF N3 endpoint | `192.168.70.1` |

## Build the Docker image

From the PFCPsim source directory:

```bash
cd ~/pfcpsim_v2
sudo docker build -t pfcpsim:patched .
```

Check the image:

```bash
sudo docker images | grep pfcpsim
```

## Start PFCPsim and create 10 sessions

Create the helper script:

```bash
cat > ~/setup_pfcpsim_10.sh <<'EOF_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

PFCP_IFACE="enp7s0"
UE_N4_IP="192.168.80.2"
UPF_N4_IP="192.168.80.1"

UE_GNB_IP="192.168.70.100"
UPF_N3_IP="192.168.70.1"

COUNT=10
BASE_ID=1
UE_POOL="10.250.0.0/24"

MBR_LIST="1000000000,1000000000,1000000000,1000000000,1000000000,1000000000,1000000000,1000000000,1000000000,1000000000"

echo "==> Configuring PFCP interface"
sudo ip addr add "${UE_N4_IP}/24" dev "${PFCP_IFACE}" 2>/dev/null || true
sudo ip link set "${PFCP_IFACE}" up
ip -br addr show dev "${PFCP_IFACE}"

echo "==> Testing reachability to UPF PFCP endpoint"
ping -c 2 "${UPF_N4_IP}"

echo "==> Removing old pfcpsim container"
sudo docker rm -f pfcpsim 2>/dev/null || true

echo "==> Starting pfcpsim"
sudo docker run --rm -d --network host \
  --name pfcpsim \
  pfcpsim:patched \
  -p 12345 \
  --interface "${PFCP_IFACE}"

sleep 2

echo "==> Configuring service"
sudo docker exec pfcpsim pfcpctl -s localhost:12345 service configure \
  --n3-addr "${UPF_N3_IP}" \
  --remote-peer-addr "${UPF_N4_IP}"

echo "==> Associating with UPF"
sudo docker exec pfcpsim pfcpctl -s localhost:12345 service associate

echo "==> Creating ${COUNT} sessions"
sudo docker exec pfcpsim pfcpctl -s localhost:12345 session create \
  --count "${COUNT}" \
  --baseID "${BASE_ID}" \
  --ue-pool "${UE_POOL}" \
  --gnb-addr "${UE_GNB_IP}" \
  --app-filter "udp:any:any:allow:100" \
  --session-mbr-uplink   "${MBR_LIST}" \
  --session-mbr-downlink "${MBR_LIST}" \
  --app-mbr-uplink       "${MBR_LIST}" \
  --app-mbr-downlink     "${MBR_LIST}"

echo "==> Final logs"
sudo docker logs --tail 80 pfcpsim

echo
echo "OK: PFCP sessions requested."
echo "Expected uplink TEIDs: 1,11,21,31,41,51,61,71,81,91"
echo "Expected UE IPs: 10.250.0.1 to 10.250.0.10"
EOF_SCRIPT

chmod +x ~/setup_pfcpsim_10.sh
```

Run the script:

```bash
~/setup_pfcpsim_10.sh
```

## Expected UPF state

After the script succeeds, validate on the UPF node:

```bash
sudo docker exec bess /opt/bess/bessctl/bessctl show module pdrLookup | head -25
sudo docker exec bess /opt/bess/bessctl/bessctl show module farLookup | head -25
sudo docker exec bess /opt/bess/bessctl/bessctl show module appQERLookup | head -25
sudo docker exec bess /opt/bess/bessctl/bessctl show module sessionQERLookup | head -25

curl -s http://127.0.0.1:8080/metrics | grep 'pfcp_sessions'
```

Expected values:

```text
pdrLookup = 20 rules
farLookup = 20 rules
appQERLookup = 40 rules
sessionQERLookup = 20 rules
pfcp_sessions{node_id="192.168.80.2"} 10
```

## Important behavior

Reloading the BESS pipeline with `bessctl run file` clears BESS datapath rules. After every pipeline reload, recreate PFCP sessions by running:

```bash
~/setup_pfcpsim_10.sh
```

If `pfcp_sessions` becomes larger than 10 after repeated experiments, reset the UPF PFCP container and reload the pipeline on the UPF side before creating sessions again.

## Validated result

The validated PFCPsim setup created 10 active PFCP sessions. These sessions installed the rules required for synthetic UE traffic with TEIDs:

```text
1, 11, 21, 31, 41, 51, 61, 71, 81, 91
```

