#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 10 seeds oficiais
SEEDS="${SEEDS:-1001 1002 1003 1004 1005 1006 1007 1008 1009 1010}"

# Métodos oficiais
METHODS="${METHODS:-static-k1 static-k2 static-k3 static-k4 static-k5 iod}"

# Configuração da campanha
STAGES="${STAGES:-5}"
STAGE_DURATION_S="${STAGE_DURATION_S:-180}"
PERIOD_S="${PERIOD_S:-15}"
SENDER_LOOP="${SENDER_LOOP:-5}"
SENDER_FPS="${SENDER_FPS:-1}"

# Valores possíveis de escala
SCALE_POOL="${SCALE_POOL:-1 2 3 4 5 6 7 8 9 10}"

# Thresholds do IOD
HEAD_BAD_MS="${HEAD_BAD_MS:-80}"
TX_BAD_MS="${TX_BAD_MS:-22}"

MASTER_TS="$(date +%Y%m%d_%H%M%S)"
MASTER_LOG_DIR="$ROOT/experiment-logs/full-matrix-${MASTER_TS}"

mkdir -p "$MASTER_LOG_DIR"

echo "master_ts=$MASTER_TS" > "$MASTER_LOG_DIR/metadata.txt"
echo "seeds=$SEEDS" >> "$MASTER_LOG_DIR/metadata.txt"
echo "methods=$METHODS" >> "$MASTER_LOG_DIR/metadata.txt"
echo "stages=$STAGES" >> "$MASTER_LOG_DIR/metadata.txt"
echo "stage_duration_s=$STAGE_DURATION_S" >> "$MASTER_LOG_DIR/metadata.txt"
echo "period_s=$PERIOD_S" >> "$MASTER_LOG_DIR/metadata.txt"
echo "sender_loop=$SENDER_LOOP" >> "$MASTER_LOG_DIR/metadata.txt"
echo "sender_fps=$SENDER_FPS" >> "$MASTER_LOG_DIR/metadata.txt"
echo "scale_pool=$SCALE_POOL" >> "$MASTER_LOG_DIR/metadata.txt"
echo "head_bad_ms=$HEAD_BAD_MS" >> "$MASTER_LOG_DIR/metadata.txt"
echo "tx_bad_ms=$TX_BAD_MS" >> "$MASTER_LOG_DIR/metadata.txt"

echo "seed,scale_values" > "$MASTER_LOG_DIR/scale_sequences.csv"
echo "seed,method,status,start_ts,end_ts,log_file" > "$MASTER_LOG_DIR/runs.csv"

make_scale_sequence() {
  local seed="$1"

  python3 - "$seed" "$STAGES" "$SCALE_POOL" <<'PY'
import random
import sys

seed = int(sys.argv[1])
stages = int(sys.argv[2])
pool = list(map(int, sys.argv[3].split()))

random.seed(seed)
seq = [random.choice(pool) for _ in range(stages)]

# garantir que pelo menos um estágio use múltiplos UEs
if max(seq) == 1 and len(pool) > 1:
    seq[-1] = max(pool)

print(" ".join(map(str, seq)))
PY
}

cleanup_before_run() {
  kubectl delete stresschaos --all -A --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl delete networkchaos --all -A --ignore-not-found=true >/dev/null 2>&1 || true
}

for SEED in $SEEDS; do
  SCALE_VALUES="$(make_scale_sequence "$SEED")"
  echo "$SEED,\"$SCALE_VALUES\"" >> "$MASTER_LOG_DIR/scale_sequences.csv"

  echo "============================================================"
  echo "SEED=$SEED"
  echo "SCALE_VALUES=$SCALE_VALUES"
  echo "============================================================"

  for METHOD in $METHODS; do
    RUN_START="$(date +%Y%m%d_%H%M%S)"
    RUN_LOG="$MASTER_LOG_DIR/run_${METHOD}_${SEED}_${RUN_START}.log"

    echo
    echo "===== START method=$METHOD seed=$SEED scale=[$SCALE_VALUES] ====="

    cleanup_before_run

    set +e

    MANUAL_MEC=1 \
    CHAOS_SEED="$SEED" \
    STAGES="$STAGES" \
    STAGE_DURATION_S="$STAGE_DURATION_S" \
    PERIOD_S="$PERIOD_S" \
    SENDER_LOOP="$SENDER_LOOP" \
    SENDER_FPS="$SENDER_FPS" \
    SCALE_VALUES="$SCALE_VALUES" \
    HEAD_BAD_MS="$HEAD_BAD_MS" \
    TX_BAD_MS="$TX_BAD_MS" \
    ./experiments/run_scale_iod_chaos_campaign.sh "$METHOD" \
      > "$RUN_LOG" 2>&1

    STATUS=$?

    set -e

    RUN_END="$(date +%Y%m%d_%H%M%S)"

    echo "$SEED,$METHOD,$STATUS,$RUN_START,$RUN_END,$RUN_LOG" >> "$MASTER_LOG_DIR/runs.csv"

    if [ "$STATUS" -ne 0 ]; then
      echo "ERROR: method=$METHOD seed=$SEED failed. See $RUN_LOG"
      exit "$STATUS"
    fi

    echo "===== DONE method=$METHOD seed=$SEED ====="
  done
done

cleanup_before_run

echo
echo "FULL_MATRIX_DONE"
echo "MASTER_LOG_DIR=$MASTER_LOG_DIR"
