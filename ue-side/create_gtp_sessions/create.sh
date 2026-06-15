#!/usr/bin/env bash
set -euo pipefail

# =========================
# PFCP / N4
# =========================
PFCP_IFACE="enp7s0"
UE_N4_IP="192.168.80.2"
UPF_N4_IP="192.168.80.1"

# =========================
# N3 / GTP-U
# UE/n3br -> UPF/enp7s0np0
# =========================
UE_GNB_IP="192.168.70.100"
UPF_N3_IP="192.168.70.1"

# =========================
# Sessões
# TEIDs uplink esperados:
# 1, 11, 21, 31, 41, 51, 61, 71, 81, 91
# UE IPs esperados:
# 10.250.0.1 até 10.250.0.10
# =========================
COUNT=10
BASE_ID=1
UE_POOL="10.250.0.0/24"

# Sem gargalo artificial: MBR alto para todas as sessões
MBR_LIST="1000000000,1000000000,1000000000,1000000000,1000000000,1000000000,1000000000,1000000000,1000000000,1000000000"

echo "==> Garantindo IP de controle PFCP no UE"
sudo ip addr add "${UE_N4_IP}/24" dev "${PFCP_IFACE}" 2>/dev/null || true
sudo ip link set "${PFCP_IFACE}" up
ip -br addr show dev "${PFCP_IFACE}"

echo "==> Testando alcance até UPF N4 ${UPF_N4_IP}"
ping -c 2 "${UPF_N4_IP}"

echo "==> Limpando pfcpsim antigo"
sudo docker rm -f pfcpsim 2>/dev/null || true

echo "==> Subindo pfcpsim"
sudo docker run --rm -d --network host \
  --name pfcpsim \
  pfcpsim:patched \
  -p 12345 \
  --interface "${PFCP_IFACE}"

sleep 2

echo "==> Configurando serviço PFCP"
sudo docker exec pfcpsim pfcpctl -s localhost:12345 service configure \
  --n3-addr "${UPF_N3_IP}" \
  --remote-peer-addr "${UPF_N4_IP}"

echo "==> Associando com UPF"
sudo docker exec pfcpsim pfcpctl -s localhost:12345 service associate

echo "==> Criando ${COUNT} sessões PFCP"
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

echo "==> Logs finais do pfcpsim"
sudo docker logs --tail 80 pfcpsim

echo
echo "OK: sessões solicitadas."
echo "TEIDs uplink esperados: 1,11,21,31,41,51,61,71,81,91"
echo "UE IPs esperados: 10.250.0.1 até 10.250.0.10"
