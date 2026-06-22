# NetAI Inference on Demand Experiments

This document summarizes the experimental rationale, system scenario, execution protocol, directory structure, and validation logic for the `NetAI-APP-Inference_on_Demand` campaign. It is intended to be used as a compact memory file for a local AI assistant or for a researcher who needs to understand the experiment without reading the full terminal history.

The document deliberately focuses on the final experimental design and the stable execution procedure. It does not describe intermediate failed attempts except when a detail is necessary to explain the final protocol or to avoid invalid runs.

---

## 1. High-level goal

The experiment evaluates **Inference on Demand (IoD)** for split neural inference over a 5G-like user-plane path. The core idea is to adapt how much of the model is executed at the UE side before sending intermediate activations through the network toward the MEC side.

The split point is represented by `k`.

- A lower `k` means that less computation is performed at the UE side and more intermediate data is sent through the network.
- A higher `k` means that more computation is performed locally at the UE side and a smaller activation tensor is transmitted.
- The trade-off is between UE-side head inference time and network transmission cost.

The campaign compares fixed split configurations against an adaptive IoD strategy:

```text
static-k1
static-k2
static-k3
static-k4
static-k5
iod
```

The static baselines keep `k` fixed during the run. The `iod` method chooses `k` online according to observed runtime conditions, such as UE-side head inference delay and transmission delay. The logged decision reasons include states such as `normal` and `tx_bad`.

The experiment is not just a model benchmark. It is an end-to-end systems experiment involving:

- UE-side GPU inference and activation fragmentation;
- GTP-U encapsulation over an N3-like path;
- PFCP-installed sessions in a BESS-based UPF;
- UPF-side split-aware packet accounting;
- MEC-side reception;
- Kubernetes scaling;
- Chaos-based runtime disturbances;
- repeated full-matrix execution across seeds and methods.

---

## 2. Experimental scenario

The setup represents a split-inference service deployed across three logical sides:

```text
UE side  ->  UPF side  ->  MEC side
```

### 2.1 UE side

The UE side runs Kubernetes UE pods. Each UE pod executes a sender application that:

1. loads image frames from the mounted dataset directory;
2. runs the head part of the neural model up to split point `k`;
3. serializes the resulting activation tensor;
4. fragments the activation into packet-sized chunks;
5. encapsulates the traffic as GTP-U traffic;
6. sends the traffic through the UE N3 interface toward the UPF.

Relevant sender program:

```text
ue-side / container path: /app/ue_sender.py
```

Important `ue_sender.py` parameters used in the campaign:

```text
--iface net1
--data-dir /data
--quant fp16
--fps 1
--loop 5
--chunk 1300
--next-hop-mac 04:3f:72:fa:75:60
--upf-n3 192.168.70.1
--device cuda
```

The sender logs per-frame measurements such as:

```text
[UE] frame=<i> head_infer=<ms> tx=<ms> bytes=<n> frags=<n> sent=<n>
```

These logs are the main source for UE-side metrics:

- head inference latency;
- transmission time;
- bytes sent by the UE application;
- number of fragments generated;
- number of packets sent.

### 2.2 UPF side

The UPF side runs a BESS-based UPF with DPDK/Mellanox ports and a PFCP control component.

Relevant UPF properties:

```text
UPF SSH/control address: ubuntu@192.168.80.1
UPF N3 address:          192.168.70.1
UPF N3 MAC:              04:3f:72:fa:75:60
UPF N6/MEC-side address: 192.168.90.1
```

The UPF has two main containers:

```text
bess
pfcpiface
```

The UPF exports PFCP session count through Prometheus-style metrics on the UPF host:

```bash
curl -s localhost:8080/metrics | grep '^pfcp_sessions'
```

The expected healthy state before and during the campaign is:

```text
pfcp_sessions{node_id="192.168.80.2"} 10
```

### 2.3 MEC side

The MEC side is the receiver side of the split-inference traffic after UPF processing. The relevant MEC endpoint used by the UE traffic is:

```text
MEC IP:   192.168.90.2
MEC port: 5001
```

The campaign keeps the MEC side manual/stable during the final execution by using:

```text
MANUAL_MEC=1
```

---

## 3. PFCP and TEID mapping

PFCP sessions are created once before the campaign. They are not recreated before each seed and not recreated before each method. The campaign only checks PFCP state and collects statistics.

The session creation script is:

```text
ue-side/create_gtp_sessions/create.sh
```

The expected PFCP session set is:

```text
10 PFCP sessions
```

Expected uplink TEIDs:

```text
1, 11, 21, 31, 41, 51, 61, 71, 81, 91
```

Expected UE IPs:

```text
10.250.0.1  through  10.250.0.10
```

The per-slot mapping follows this pattern:

```text
slot 0 -> TEID 1  -> UE IP 10.250.0.1
slot 1 -> TEID 11 -> UE IP 10.250.0.2
slot 2 -> TEID 21 -> UE IP 10.250.0.3
...
slot 9 -> TEID 91 -> UE IP 10.250.0.10
```

Important rule:

```text
PFCP sessions must remain persistent during the matrix.
The campaign clears split statistics, not PFCP sessions.
```

---

## 4. UPF split-aware accounting

The BESS UPF includes a `splitController` module used to collect split-aware packet and byte counters. These counters are read per round and stored in `split_stats.txt` files.

A healthy split-stat entry contains non-zero packet and byte counters, for example:

```text
response: statistics {
  teid: 1
  k: 3
  packets: 1262
  bytes: 1719086
}
```

In scaled stages, multiple UE slots may be active, so a round-level `split_stats.txt` may contain multiple statistics blocks. A typical scaled run showed counters such as:

```text
packets: 12610
bytes: 17184330
```

The campaign clears split statistics before collecting a new round. Clearing split statistics does not remove PFCP sessions.

---

## 5. Important implementation details fixed into the final protocol

### 5.1 Stable SSH for detached execution

The full matrix is executed with `nohup`. Therefore, UPF SSH collection must not depend on an interactive terminal session or a transient SSH agent.

A dedicated key was created on the UE host:

```text
~/.ssh/upf_nopass
```

The public key was installed on the UPF host under:

```text
/home/ubuntu/.ssh/authorized_keys
```

A healthy non-interactive test is:

```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes -i ~/.ssh/upf_nopass \
  ubuntu@192.168.80.1 'echo SSH_KEY_OK'
```

Expected output:

```text
SSH_KEY_OK
```

The campaign must be launched with:

```text
UPF_SSH="ubuntu@192.168.80.1"
UPF_SSH_KEY="$HOME/.ssh/upf_nopass"
```

The per-run campaign script must use `UPF_SSH_KEY` in its UPF SSH helper. The intended helper behavior is:

```bash
ssh -o BatchMode=yes \
    -o ConnectTimeout=8 \
    -o StrictHostKeyChecking=accept-new \
    -o IdentitiesOnly=yes \
    -i "$UPF_SSH_KEY" \
    "$UPF_SSH" "$cmd"
```

This ensures that detached runs keep collecting UPF statistics even after the terminal client is closed.

### 5.2 UE pod N3 source address

The sender must use the UE pod `net1` IP address as the N3 source address. It must not assume that the bridge IP is the pod IP.

Final behavior:

```bash
POD_N3_SRC=$(ip -4 -o addr show dev ${IFACE} | awk '{split($4,a,"/"); print a[1]; exit}')
```

The campaign logs the runtime sender configuration, for example:

```text
[RUN_SENDER] iface=net1 pod_n3_src=<pod-net1-ip> upf_n3=192.168.70.1 next_hop_mac=04:3f:72:fa:75:60 teid=<teid> ue_ip=<ue-ip>
```

### 5.3 PFCP sessions are not recreated by the matrix

The final full-matrix script does not call `create_gtp_sessions` and does not call `pfcpsim`. It only runs the experimental matrix over seeds and methods.

The per-method script collects PFCP state through files such as:

```text
pfcp_sessions_start.txt
pfcp_sessions_after_scale.txt
pfcp_sessions_stage_end.txt
pfcp_sessions_end.txt
```

---

## 6. Experimental factors

### 6.1 Methods

The full method set is:

```text
static-k1 static-k2 static-k3 static-k4 static-k5 iod
```

Static methods:

```text
static-k1 -> always use k=1
static-k2 -> always use k=2
static-k3 -> always use k=3
static-k4 -> always use k=4
static-k5 -> always use k=5
```

Adaptive method:

```text
iod -> choose k online using observed runtime conditions
```

The IoD method records its decisions in `rounds.csv` with columns such as:

```text
stage,round,slot,k,reason
```

Example decision rows:

```text
0,0,0,3,normal
0,1,1,5,tx_bad
```

Interpretation:

- `normal`: the method keeps the default/normal split behavior.
- `tx_bad`: the method increases local processing to reduce transmitted activation size.

### 6.2 Seeds

The official full campaign uses 10 seeds:

```text
4001 4002 4003 4004 4005 4006 4007 4008 4009 4010
```

Each seed produces one scale profile that is reused across all methods for that seed. This enables paired comparison across methods under the same disturbance/scale pattern.

### 6.3 Stages and scaling

Each run has 5 stages:

```text
STAGES=5
```

Each stage has a duration of 180 seconds:

```text
STAGE_DURATION_S=180
```

The campaign samples the number of active UE replicas from:

```text
SCALE_POOL="1 2 3 4 5 6 7 8 9 10"
```

Therefore, each run evaluates a method under a sequence of 5 scale levels.

### 6.4 Round period

Within each stage, rounds are executed every 15 seconds:

```text
PERIOD_S=15
```

For each active slot/replica in a round, the sender emits traffic using:

```text
SENDER_LOOP=5
SENDER_FPS=1
```

### 6.5 IoD thresholds

The final campaign uses:

```text
HEAD_BAD_MS=80
TX_BAD_MS=22
```

These thresholds define the runtime conditions that can cause the IoD method to change `k`.

---

## 7. Official full-matrix campaign

The official campaign is:

```text
6 methods x 10 seeds = 60 runs
```

The final command used to launch the official campaign is conceptually:

```bash
cd ~/NetAI-APP-Inference_on_Demand

nohup env \
UPF_SSH="ubuntu@192.168.80.1" \
UPF_SSH_KEY="$HOME/.ssh/upf_nopass" \
NEXT_HOP_MAC="04:3f:72:fa:75:60" \
MANUAL_MEC=1 \
SEEDS="4001 4002 4003 4004 4005 4006 4007 4008 4009 4010" \
METHODS="static-k1 static-k2 static-k3 static-k4 static-k5 iod" \
STAGES=5 \
STAGE_DURATION_S=180 \
PERIOD_S=15 \
SENDER_LOOP=5 \
SENDER_FPS=1 \
SCALE_POOL="1 2 3 4 5 6 7 8 9 10" \
HEAD_BAD_MS=80 \
TX_BAD_MS=22 \
./experiments/run_full_scale_campaign_matrix.sh \
> experiment-logs/full_matrix_upf_nohup.log 2>&1 &

echo $! > experiment-logs/full_matrix_upf_nohup.pid
```

The official campaign directory observed during execution was:

```text
experiment-logs/full-matrix-20260621_012121
```

At an intermediate checkpoint, the campaign had reached:

```text
57 / 60 completed runs
```

and the master process was still alive under `nohup`.

Earlier during the same official campaign, a validation checkpoint found:

```text
1631 split_stats files with non-zero packets and bytes
0 observed split_stats files with missing or zero packets/bytes among inspected files
```

---

## 8. Directory structure

All campaign logs are under:

```text
~/NetAI-APP-Inference_on_Demand/experiment-logs/
```

### 8.1 Master matrix directory

The master campaign creates a directory like:

```text
experiment-logs/full-matrix-<timestamp>/
```

Example:

```text
experiment-logs/full-matrix-20260621_012121/
```

Important files:

```text
runs.csv
run_<method>_<seed>_<timestamp>.log
```

`runs.csv` records completed runs:

```text
seed,method,status,start_ts,end_ts,log_file
```

Expected final size:

```text
61 lines total = 1 header + 60 completed runs
```

### 8.2 Per-run directory

Each method/seed run creates a directory like:

```text
experiment-logs/scale-<method>-<seed>-<timestamp>/
```

Examples:

```text
experiment-logs/scale-static-k1-4001-20260621_012121/
experiment-logs/scale-static-k3-9201-20260621_011531/
experiment-logs/scale-iod-4001-20260621_<timestamp>/
```

Important per-run files:

```text
metadata.txt
rounds.csv
scale_events.csv
pod_inventory.csv
pod_events.csv
pfcp_sessions_start.txt
pfcp_sessions_end.txt
upf_pipeline_start.txt
upf_pipeline_end.txt
split_stats_final.txt
chaos_stage_<stage>.log
ue_chaos_trace_<...>.csv
```

### 8.3 Per-stage directory

Each run contains stage directories:

```text
stage_00_replicas_<n>/
stage_01_replicas_<n>/
stage_02_replicas_<n>/
stage_03_replicas_<n>/
stage_04_replicas_<n>/
```

Important per-stage files:

```text
pfcp_sessions_after_scale.txt
pfcp_sessions_stage_end.txt
split_clear_before_stage.txt
split_stats_stage_final.txt
```

### 8.4 Per-round directory

Each stage contains round directories:

```text
round_000/
round_001/
round_002/
...
```

Important per-round files:

```text
split_clear.txt
split_stats.txt
ue_slot<slot>_teid<teid>.log
```

`split_stats.txt` is the main UPF-side evidence for that round.

`ue_slot<slot>_teid<teid>.log` is the main UE-side evidence for that slot in that round.

---

## 9. What each output file means

### 9.1 `runs.csv`

Master-level ledger of completed runs.

A completed campaign should contain 60 rows plus the header.

Use it to know which method/seed combinations completed and where their run logs are located.

### 9.2 `run_<method>_<seed>_<timestamp>.log`

Master-level log for a specific method/seed run.

It records:

- start and end markers;
- per-stage progress;
- final `RESULTS_DIR` location;
- cleanup operations;
- whether the run completed successfully.

A useful way to find the real per-run output directory is:

```bash
grep -m1 '^RESULTS_DIR=' <run_log>
```

### 9.3 `rounds.csv`

Per-run round ledger.

For IoD, it is also the decision trace:

```text
stage,round,slot,k,reason
```

This file tells which split point was used for each slot in each round and why.

### 9.4 `split_stats.txt`

Per-round UPF split-aware accounting.

This is the key file for verifying that the UPF saw and counted the traffic. A valid file must include non-zero values for:

```text
packets:
bytes:
```

A valid file may contain multiple statistics entries because multiple UE slots and split points may be active.

### 9.5 `split_clear.txt`

Output of the split-stat clearing command before a round. It is used to avoid mixing counters from previous rounds.

### 9.6 `ue_slot<slot>_teid<teid>.log`

UE sender log for one slot and TEID.

It includes per-frame inference and transmission measurements:

```text
head_infer=<ms>
tx=<ms>
bytes=<n>
frags=<n>
sent=<n>
```

### 9.7 `pfcp_sessions_*.txt`

PFCP session sanity snapshots.

Healthy expected value:

```text
pfcp_sessions{node_id="192.168.80.2"} 10
```

### 9.8 `upf_pipeline_*.txt`

BESS pipeline snapshots. These are useful to verify that the UPF pipeline and modules were present at start and end of the run.

### 9.9 `pod_inventory.csv` and `pod_events.csv`

Kubernetes state logs. These are used to relate traffic and failures to UE pod availability and scaling events.

### 9.10 `scale_events.csv`

Records scaling actions and stage-level replica counts.

### 9.11 `chaos_stage_<stage>.log`

Chaos execution log for a stage. It documents the runtime disturbance injected during that stage.

---

## 10. Validation commands

### 10.1 Check whether the full matrix is still running

```bash
cd ~/NetAI-APP-Inference_on_Demand

ps -fp "$(cat experiment-logs/full_matrix_upf_nohup.pid)" || echo "processo terminou"
```

### 10.2 Count completed runs

```bash
cd ~/NetAI-APP-Inference_on_Demand

MASTER=$(ls -td experiment-logs/full-matrix-* | head -1)
tail -n +2 "$MASTER/runs.csv" | wc -l
```

Expected final value:

```text
60
```

### 10.3 Check final `runs.csv` size

```bash
cd ~/NetAI-APP-Inference_on_Demand

MASTER=$(ls -td experiment-logs/full-matrix-* | head -1)
wc -l "$MASTER/runs.csv"
```

Expected final value:

```text
61
```

### 10.4 Check for SSH permission failures

```bash
cd ~/NetAI-APP-Inference_on_Demand

grep -R "Permission denied" experiment-logs | head || echo "OK: sem Permission denied"
```

Healthy output:

```text
OK: sem Permission denied
```

### 10.5 Check for missing or zero UPF packet/byte counters

```bash
cd ~/NetAI-APP-Inference_on_Demand

echo "===== split_stats with zero or missing packets/bytes ====="

find experiment-logs/scale-* -path "*/round_*/split_stats.txt" -type f | while read -r f; do
  p=$(awk '/packets:/ {print $2; exit}' "$f")
  b=$(awk '/bytes:/ {print $2; exit}' "$f")

  if [ -z "$p" ] || [ "$p" = "0" ] || [ -z "$b" ] || [ "$b" = "0" ]; then
    echo "BAD packets=${p:-MISSING} bytes=${b:-MISSING} file=$f"
  fi
done | head -50
```

Healthy behavior:

```text
Only the title is printed.
No BAD lines appear.
```

### 10.6 Count valid UPF split-stat files

```bash
cd ~/NetAI-APP-Inference_on_Demand

find experiment-logs/scale-* -path "*/round_*/split_stats.txt" -type f | while read -r f; do
  p=$(awk '/packets:/ {print $2; exit}' "$f")
  b=$(awk '/bytes:/ {print $2; exit}' "$f")
  [ -n "$p" ] && [ "$p" != "0" ] && [ -n "$b" ] && [ "$b" != "0" ] && echo "$f"
done | wc -l
```

This number should grow while the campaign is running.

### 10.7 Validate only completed runs using the real `RESULTS_DIR`

This avoids false positives caused by reconstructing output directory names from timestamps.

```bash
cd ~/NetAI-APP-Inference_on_Demand

MASTER=$(ls -td experiment-logs/full-matrix-* | head -1)
OK=0
BAD=0

while IFS=, read -r seed method status start end logfile; do
  OUT=""

  if [ -f "$logfile" ]; then
    OUT=$(grep -m1 '^RESULTS_DIR=' "$logfile" | cut -d= -f2-)
  fi

  if [ -z "$OUT" ] || [ ! -d "$OUT" ]; then
    OUT=$(ls -td experiment-logs/scale-"$method"-"$seed"-* 2>/dev/null | head -1)
  fi

  if [ -z "$OUT" ] || [ ! -d "$OUT" ]; then
    echo "BAD missing_dir seed=$seed method=$method logfile=$logfile"
    BAD=$((BAD+1))
    continue
  fi

  total=$(find "$OUT" -path "*/round_*/split_stats.txt" -type f | wc -l)
  missing_packets=$(find "$OUT" -path "*/round_*/split_stats.txt" -type f ! -exec grep -q "packets:" {} \; -print | wc -l)
  missing_bytes=$(find "$OUT" -path "*/round_*/split_stats.txt" -type f ! -exec grep -q "bytes:" {} \; -print | wc -l)
  ssherr=$(grep -R "Permission denied" "$OUT" 2>/dev/null | wc -l)

  if [ "$total" -gt 0 ] && [ "$missing_packets" -eq 0 ] && [ "$missing_bytes" -eq 0 ] && [ "$ssherr" -eq 0 ]; then
    echo "OK  seed=$seed method=$method split_stats=$total"
    OK=$((OK+1))
  else
    echo "BAD seed=$seed method=$method split_stats=$total missing_packets=$missing_packets missing_bytes=$missing_bytes permission_denied=$ssherr OUT=$OUT"
    BAD=$((BAD+1))
  fi
done < <(tail -n +2 "$MASTER/runs.csv")

echo "OK=$OK BAD=$BAD"
```

Healthy final output should be:

```text
OK=60 BAD=0
```

---

## 11. Expected analysis workflow after the campaign

After the campaign reaches 60 completed runs, the analysis should proceed as follows.

### 11.1 Confirm campaign completeness

Check that:

```text
runs.csv has 61 lines
60 completed runs are present
all six methods appear for each of the ten seeds
```

### 11.2 Confirm UPF validity

Check that:

```text
no split_stats.txt has missing packets
no split_stats.txt has packets: 0
no split_stats.txt has missing bytes
no split_stats.txt has bytes: 0
no Permission denied appears in experiment-logs
PFCP sessions stayed at 10 in the collected PFCP snapshots
```

### 11.3 Parse UE metrics

From `ue_slot<slot>_teid<teid>.log`, extract:

```text
head_infer_ms
tx_ms
bytes
frags
sent
```

Aggregate by:

```text
method
seed
stage
round
slot
k
reason
replicas
```

### 11.4 Parse UPF metrics

From `split_stats.txt`, extract:

```text
teid
k
packets
bytes
```

Aggregate by:

```text
method
seed
stage
round
teid
k
```

### 11.5 Join UE and UPF evidence

Use TEID and round/stage context to relate:

```text
UE sender logs
IoD decisions in rounds.csv
UPF split-aware counters
replica count in stage directory name
chaos stage information
```

### 11.6 Recommended comparisons

The main comparisons are:

```text
iod vs static-k1
iod vs static-k2
iod vs static-k3
iod vs static-k4
iod vs static-k5
```

Use the same seed as a paired experimental unit. Since all methods under a seed share the same scale profile, paired comparisons are more appropriate than treating all method runs as independent.

Recommended metrics:

```text
UE transmitted bytes
UE tx time
UE head inference time
UPF counted bytes
UPF counted packets
IoD k distribution
IoD reason distribution
per-stage behavior under scale
```

### 11.7 Interpretation guideline

The purpose of IoD is not necessarily to minimize a single metric in every situation. It is to adapt split depth according to runtime conditions.

Expected qualitative behavior:

- `static-k1` and `static-k2` tend to send larger activations and stress the network path more.
- `static-k4` and `static-k5` tend to send smaller activations but require more UE-side computation.
- `static-k3` is a middle fixed baseline.
- `iod` should move between split choices as runtime conditions change.

Therefore, IoD should be interpreted as an adaptive trade-off mechanism, not simply as the method that must always beat the most aggressive fixed split on bytes or the lightest fixed split on compute.

---

## 12. Reproducibility checklist

Before launching a new official campaign:

```text
1. Confirm UPF BESS and pfcpiface containers are running.
2. Create PFCP sessions once using ue-side/create_gtp_sessions/create.sh.
3. Confirm pfcp_sessions = 10.
4. Confirm SSH key-based UPF access works with BatchMode.
5. Confirm NEXT_HOP_MAC is the UPF N3 MAC.
6. Confirm the UE sender computes pod_n3_src from net1.
7. Run a short nohup probe with one method and one stage.
8. Confirm the probe has no Permission denied.
9. Confirm the probe has non-zero packets and bytes in split_stats.txt.
10. Archive or clear old experiment-logs before the official run.
11. Launch the full matrix with UPF_SSH_KEY defined.
12. Validate the first official run before closing the terminal.
13. After completion, validate all completed runs with the zero/missing packet check.
```

---

## 13. Minimal pre-flight commands

### 13.1 Confirm PFCP sessions

```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes -i ~/.ssh/upf_nopass \
  ubuntu@192.168.80.1 \
  'curl -s localhost:8080/metrics | grep "^pfcp_sessions"'
```

Expected:

```text
pfcp_sessions{node_id="192.168.80.2"} 10
```

### 13.2 Confirm SSH key

```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes -i ~/.ssh/upf_nopass \
  ubuntu@192.168.80.1 'echo SSH_KEY_OK'
```

Expected:

```text
SSH_KEY_OK
```

### 13.3 Confirm no zero/missing UPF stats during execution

```bash
cd ~/NetAI-APP-Inference_on_Demand

find experiment-logs/scale-* -path "*/round_*/split_stats.txt" -type f | while read -r f; do
  p=$(awk '/packets:/ {print $2; exit}' "$f")
  b=$(awk '/bytes:/ {print $2; exit}' "$f")

  if [ -z "$p" ] || [ "$p" = "0" ] || [ -z "$b" ] || [ "$b" = "0" ]; then
    echo "BAD packets=${p:-MISSING} bytes=${b:-MISSING} file=$f"
  fi
done | head -50
```

Healthy behavior:

```text
No BAD lines.
```

---

## 14. Final memory summary

The final experimental story is:

```text
A split-inference application was evaluated over a 5G-like UE-UPF-MEC path.
UE pods generated GTP-U traffic containing intermediate neural activations.
A BESS-based UPF processed PFCP-installed sessions and collected split-aware counters.
The experiment compared static split points k=1..5 against an adaptive IoD policy.
Each method was tested under the same per-seed scale/chaos profile.
The official matrix used 6 methods, 10 seeds, 5 stages per run, and 180 seconds per stage.
The final detached campaign used a stable SSH key to collect UPF statistics reliably.
The key artifacts are runs.csv, rounds.csv, UE sender logs, PFCP snapshots, and split_stats.txt.
A valid run requires non-zero packets and bytes in every round-level split_stats.txt file.
The official run directory was experiment-logs/full-matrix-20260621_012121.
At the last observed checkpoint, the official campaign had 57/60 completed runs and was still running.
```


