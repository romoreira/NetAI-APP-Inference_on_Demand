#!/usr/bin/env bash
set -euo pipefail

UPF_SSH="ubuntu@192.168.80.1"

POD=$(kubectl get pods -l app=ue,role=ue -o jsonpath='{.items[0].metadata.name}')

echo "===== PFCP sessions no UPF ====="
ssh "$UPF_SSH" 'curl -s localhost:8080/metrics | grep pfcp_sessions || true'

echo
echo "===== zerar split stats ====="
ssh "$UPF_SSH" \
  'sudo docker exec bess /opt/bess/bessctl/bessctl command module splitController read_stats SplitControllerCommandReadStatsArg "{\"clear\": True}"' || true

for i in $(seq 0 9); do
  TEID=$((1 + 10*i))
  UE_IP="10.250.0.$((i + 1))"

  echo
  echo "===== teste sessão $((i + 1)): TEID=$TEID UE_IP=$UE_IP ====="

  kubectl exec "$POD" -- bash -lc "
    cd /app
    python3 ue_sender.py \
      --iface net1 \
      --data-dir /data \
      --k 4 \
      --quant fp16 \
      --fps 1 \
      --loop 1 \
      --chunk 1300 \
      --next-hop-mac 04:3f:72:fa:75:60 \
      --n3-src 192.168.70.100 \
      --upf-n3 192.168.70.1 \
      --teid $TEID \
      --ue-ip $UE_IP \
      --mec-ip 192.168.90.2 \
      --mec-port 5001 \
      --device cuda
  " | tee "validate_teid_${TEID}.log"

done

echo
echo "===== split stats final ====="
ssh "$UPF_SSH" \
  'sudo docker exec bess /opt/bess/bessctl/bessctl command module splitController read_stats SplitControllerCommandReadStatsArg "{\"clear\": False}"'
