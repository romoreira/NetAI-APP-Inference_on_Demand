#!/usr/bin/env bash
set -euo pipefail

OUT="${1:-/tmp/upf_snapshot.txt}"

{
  echo "### timestamp"
  date -Is

  echo
  echo "### docker ps"
  sudo docker ps

  echo
  echo "### pfcp sessions"
  curl -s http://127.0.0.1:8080/metrics | grep pfcp_sessions || true

  echo
  echo "### UPF packet and byte counters"
  curl -s http://127.0.0.1:8080/metrics | grep -E 'upf_packets_count|upf_bytes_count|upf_dropped_count' || true

  echo
  echo "### BESS ports"
  sudo docker exec bess /opt/bess/bessctl/bessctl show port || true

  echo
  echo "### pdrLookup"
  sudo docker exec bess /opt/bess/bessctl/bessctl show module pdrLookup || true

  echo
  echo "### gtpuDecap"
  sudo docker exec bess /opt/bess/bessctl/bessctl show module gtpuDecap || true

  echo
  echo "### farLookup"
  sudo docker exec bess /opt/bess/bessctl/bessctl show module farLookup || true

  echo
  echo "### enp8s0np0Routes"
  sudo docker exec bess /opt/bess/bessctl/bessctl show module enp8s0np0Routes || true

  echo
  echo "### enp8s0np0Fast"
  sudo docker exec bess /opt/bess/bessctl/bessctl show port | grep -A8 enp8s0np0Fast || true

  echo
  echo "### enp7s0np0Fast"
  sudo docker exec bess /opt/bess/bessctl/bessctl show port | grep -A8 enp7s0np0Fast || true

} > "$OUT"

echo "$OUT"
