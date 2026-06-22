#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Usage
# ============================================================
# Static baseline:
#   MANUAL_MEC=1 CHAOS_SEED=777 ./run_iod_chaos_campaign.sh static-k3
#
# Inference on Demand:
#   MANUAL_MEC=1 CHAOS_SEED=777 ./run_iod_chaos_campaign.sh iod
#
# Optional static baselines:
#   MANUAL_MEC=1 CHAOS_SEED=777 ./run_iod_chaos_campaign.sh static-k1
#   MANUAL_MEC=1 CHAOS_SEED=777 ./run_iod_chaos_campaign.sh static-k5

METHOD="${1:-iod}"

# ============================================================
# Hosts and paths
# ============================================================

ROOT="$HOME/NetAI-APP-Inference_on_Demand"
UPF_SSH="${UPF_SSH:-ubuntu@192.168.80.1}"
MEC_SSH="${MEC_SSH:-ubuntu@192.168.90.2}"

TS="$(date +%Y%m%d_%H%M%S)"
CHAOS_SEED="${CHAOS_SEED:-12345}"
RUN_ID="chaos-${METHOD}-${CHAOS_SEED}-${TS}"
OUT="$ROOT/results/iod_campaign_${METHOD}_${CHAOS_SEED}_${TS}"

mkdir -p "$OUT"

# ============================================================
# Campaign configuration
# ============================================================

DURATION_S="${DURATION_S:-180}"
PERIOD_S="${PERIOD_S:-8}"

SENDER_FPS="${SENDER_FPS:-1}"
SENDER_LOOP="${SENDER_LOOP:-3}"

# Main session under test
TEID="${TEID:-1}"
UE_IP="${UE_IP:-10.250.0.1}"

# UE sender configuration
IFACE="${IFACE:-net1}"
DATA_DIR="${DATA_DIR:-/data}"
QUANT="${QUANT:-fp16}"
CHUNK="${CHUNK:-1300}"
NEXT_HOP_MAC="${NEXT_HOP_MAC:-04:3f:72:fa:75:60}"
N3_SRC="${N3_SRC:-192.168.70.100}"
UPF_N3="${UPF_N3:-192.168.70.1}"
MEC_IP="${MEC_IP:-192.168.90.2}"
MEC_PORT="${MEC_PORT:-5001}"
DEVICE="${DEVICE:-cuda}"

# MEC receiver control
# MANUAL_MEC=1 means: user starts mec_receiver.py manually on MEC.
MANUAL_MEC="${MANUAL_MEC:-1}"

# App-driven IOD thresholds.
# These use the previous ue_sender round.
HEAD_BAD_MS="${HEAD_BAD_MS:-80}"
TX_BAD_MS="${TX_BAD_MS:-35}"

# Optional CPU collection only for logs, not for decision.
CPU_SAMPLE_S="${CPU_SAMPLE_S:-0.5}"

# ============================================================
# Helpers
# ============================================================

log() {
  echo "[$(date +%H:%M:%S)] $*"
}

upf() {
  ssh -o StrictHostKeyChecking=no "$UPF_SSH" "$@"
}

get_pod() {
  kubectl get pods -l app=ue,role=ue -o jsonpath='{.items[0].metadata.name}'
}

split_clear() {
  upf "sudo docker exec bess /opt/bess/bessctl/bessctl command module splitController read_stats SplitControllerCommandReadStatsArg '{\"clear\": True}'" || true
}

split_read() {
  local file="$1"
  upf "sudo docker exec bess /opt/bess/bessctl/bessctl command module splitController read_stats SplitControllerCommandReadStatsArg '{\"clear\": False}'" \
    > "$file" 2>&1 || true
}

collect_pfcp_sessions() {
  local file="$1"
  upf "curl -s localhost:8080/metrics | grep pfcp_sessions || true" > "$file" 2>&1 || true
}

collect_upf_pipeline() {
  local file="$1"
  upf "sudo docker exec bess /opt/bess/bessctl/bessctl show module splitController; echo; sudo docker exec bess /opt/bess/bessctl/bessctl show pipeline | grep -Ei 'pdrLookup|gtpuDecap|splitController|appQERLookup|sessionQERLookup|farLookup|executeFAR' || true" \
    > "$file" 2>&1 || true
}

cpu_usage_pct() {
  local pod="$1"

  local u1 u2
  u1=$(kubectl exec "$pod" -- bash -lc "awk '/usage_usec/ {print \$2}' /sys/fs/cgroup/cpu.stat" 2>/dev/null || echo 0)
  sleep "$CPU_SAMPLE_S"
  u2=$(kubectl exec "$pod" -- bash -lc "awk '/usage_usec/ {print \$2}' /sys/fs/cgroup/cpu.stat" 2>/dev/null || echo 0)

  awk -v a="$u1" -v b="$u2" -v s="$CPU_SAMPLE_S" \
    'BEGIN { if (s <= 0) s=0.5; printf "%.1f", ((b-a)/(s*1000000))*100 }'
}

parse_last_sender_metrics() {
  local file="$1"

  if [ ! -f "$file" ]; then
    echo "0,0,0,0,0"
    return
  fi

  awk '
    match($0, /head_infer=([0-9.]+)ms/, h) {head=h[1]}
    match($0, /tx=([0-9.]+)ms/, t) {tx=t[1]}
    match($0, /frags=([0-9]+)/, f) {frags=f[1]}
    match($0, /sent=([0-9]+)/, s) {sent=s[1]}
    match($0, /bytes=([0-9]+)/, b) {bytes=b[1]}
    END {
      if (head == "") head=0
      if (tx == "") tx=0
      if (frags == "") frags=0
      if (sent == "") sent=0
      if (bytes == "") bytes=0
      print head "," tx "," frags "," sent "," bytes
    }
  ' "$file"
}

decide_k_from_app() {
  local method="$1"
  local last_head="$2"
  local last_tx="$3"

  case "$method" in
    static-k1) echo "1,static"; return ;;
    static-k2) echo "2,static"; return ;;
    static-k3) echo "3,static"; return ;;
    static-k4) echo "4,static"; return ;;
    static-k5) echo "5,static"; return ;;
  esac

  local tx_bad head_bad

  tx_bad=$(awk -v x="$last_tx" -v th="$TX_BAD_MS" \
    'BEGIN { print (x > th) ? 1 : 0 }')

  head_bad=$(awk -v x="$last_head" -v th="$HEAD_BAD_MS" \
    'BEGIN { print (x > th) ? 1 : 0 }')

  if [ "$tx_bad" = "1" ] && [ "$head_bad" = "1" ]; then
    echo "4,mixed_app_bad"
  elif [ "$tx_bad" = "1" ]; then
    echo "5,tx_bad"
  elif [ "$head_bad" = "1" ]; then
    echo "2,head_bad"
  else
    echo "3,normal"
  fi
}

run_sender() {
  local pod="$1"
  local k="$2"
  local file="$3"

  kubectl exec "$pod" -- bash -lc "
    cd /app
    python3 ue_sender.py \
      --iface ${IFACE} \
      --data-dir ${DATA_DIR} \
      --k ${k} \
      --quant ${QUANT} \
      --fps ${SENDER_FPS} \
      --loop ${SENDER_LOOP} \
      --chunk ${CHUNK} \
      --next-hop-mac ${NEXT_HOP_MAC} \
      --n3-src ${N3_SRC} \
      --upf-n3 ${UPF_N3} \
      --teid ${TEID} \
      --ue-ip ${UE_IP} \
      --mec-ip ${MEC_IP} \
      --mec-port ${MEC_PORT} \
      --device ${DEVICE}
  " > "$file" 2>&1 || true
}

start_mec() {
  if [ "$MANUAL_MEC" = "1" ]; then
    log "MANUAL_MEC=1, assuming MEC receiver is already running"
    MEC_PID=""
    return
  fi

  log "starting MEC receiver via SSH"
  ssh -o StrictHostKeyChecking=no "$MEC_SSH" "
    cd ~/NetAI-APP-Inference_on_Demand/mec-side
    timeout $((DURATION_S + 60))s sudo env PYTHONPATH=/home/ubuntu/.local/lib/python3.10/site-packages \
      python3 mec_receiver.py --iface enp7s0 --device cuda
  " > "$OUT/mec_receiver.log" 2>&1 &
  MEC_PID=$!
}

start_chaos() {
  log "starting chaos profile"
  (
    cd "$ROOT/chaos-on-ue"
    CHAOS_SEED="$CHAOS_SEED" CHAOS_RUN_ID="$RUN_ID" python3 run_ue_chaos_profile.py
  ) > "$OUT/chaos_runner.log" 2>&1 &
  CHAOS_PID=$!
}

copy_chaos_trace() {
  local trace="$ROOT/chaos-on-ue/ue_chaos_trace_${RUN_ID}.csv"

  if [ -f "$trace" ]; then
    cp "$trace" "$OUT/"
  fi
}

cleanup_chaos() {
  log "cleanup chaos objects"
  kubectl delete stresschaos --all -A --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl delete networkchaos --all -A --ignore-not-found=true >/dev/null 2>&1 || true
}

# ============================================================
# Main
# ============================================================

POD="$(get_pod)"
MEC_PID=""
CHAOS_PID=""

{
  echo "method=$METHOD"
  echo "run_id=$RUN_ID"
  echo "chaos_seed=$CHAOS_SEED"
  echo "pod=$POD"
  echo "teid=$TEID"
  echo "ue_ip=$UE_IP"
  echo "duration_s=$DURATION_S"
  echo "period_s=$PERIOD_S"
  echo "sender_fps=$SENDER_FPS"
  echo "sender_loop=$SENDER_LOOP"
  echo "manual_mec=$MANUAL_MEC"
  echo "head_bad_ms=$HEAD_BAD_MS"
  echo "tx_bad_ms=$TX_BAD_MS"
} > "$OUT/metadata.txt"

log "output: $OUT"
log "pod: $POD"
log "method: $METHOD"

trap 'cleanup_chaos' EXIT

log "checking PFCP sessions"
collect_pfcp_sessions "$OUT/pfcp_sessions_start.txt"

log "collecting UPF pipeline"
collect_upf_pipeline "$OUT/upf_pipeline_start.txt"

start_mec
sleep 3

start_chaos
sleep 2

echo "round,ts,elapsed_s,pod,method,teid,ue_ip,cpu_pct,last_head_ms,last_tx_ms,last_frags,last_sent,last_bytes,k,reason,split_stats_file,ue_log_file" \
  > "$OUT/rounds.csv"

START=$(date +%s)
ROUND=0

LAST_UE_LOG=""
LAST_HEAD=0
LAST_TX=0
LAST_FRAGS=0
LAST_SENT=0
LAST_BYTES=0

while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))

  if [ "$ELAPSED" -ge "$DURATION_S" ]; then
    break
  fi

  ROUND_DIR="$OUT/round_$(printf '%03d' "$ROUND")"
  mkdir -p "$ROUND_DIR"

  if [ -n "$LAST_UE_LOG" ]; then
    LAST_METRICS=$(parse_last_sender_metrics "$LAST_UE_LOG")
    LAST_HEAD=$(echo "$LAST_METRICS" | cut -d',' -f1)
    LAST_TX=$(echo "$LAST_METRICS" | cut -d',' -f2)
    LAST_FRAGS=$(echo "$LAST_METRICS" | cut -d',' -f3)
    LAST_SENT=$(echo "$LAST_METRICS" | cut -d',' -f4)
    LAST_BYTES=$(echo "$LAST_METRICS" | cut -d',' -f5)
  fi

  CPU=$(cpu_usage_pct "$POD")

  DECISION=$(decide_k_from_app "$METHOD" "$LAST_HEAD" "$LAST_TX")
  K=$(echo "$DECISION" | cut -d',' -f1)
  REASON=$(echo "$DECISION" | cut -d',' -f2)

  log "round=$ROUND elapsed=${ELAPSED}s cpu=$CPU last_head=$LAST_HEAD last_tx=$LAST_TX -> k=$K reason=$REASON"

  split_clear > "$ROUND_DIR/split_clear.txt" 2>&1 || true

  UE_LOG="$ROUND_DIR/ue_sender.log"
  run_sender "$POD" "$K" "$UE_LOG"

  SPLIT_FILE="$ROUND_DIR/split_stats.txt"
  split_read "$SPLIT_FILE"

  echo "$ROUND,$(date +%s),$ELAPSED,$POD,$METHOD,$TEID,$UE_IP,$CPU,$LAST_HEAD,$LAST_TX,$LAST_FRAGS,$LAST_SENT,$LAST_BYTES,$K,$REASON,$SPLIT_FILE,$UE_LOG" \
    >> "$OUT/rounds.csv"

  LAST_UE_LOG="$UE_LOG"

  ROUND=$((ROUND + 1))
  sleep "$PERIOD_S"
done

log "waiting chaos"
if [ -n "$CHAOS_PID" ]; then
  wait "$CHAOS_PID" || true
fi

log "waiting MEC"
if [ -n "$MEC_PID" ]; then
  wait "$MEC_PID" || true
fi

log "collecting final UPF state"
collect_pfcp_sessions "$OUT/pfcp_sessions_end.txt"
collect_upf_pipeline "$OUT/upf_pipeline_end.txt"

upf "sudo docker exec bess /opt/bess/bessctl/bessctl command module splitController read_stats SplitControllerCommandReadStatsArg '{\"clear\": False}'" \
  > "$OUT/split_stats_final.txt" 2>&1 || true

copy_chaos_trace

log "done"
echo "RESULTS_DIR=$OUT"
find "$OUT" -maxdepth 2 -type f | sort
