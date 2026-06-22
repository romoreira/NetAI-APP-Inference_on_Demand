from pathlib import Path
from collections import Counter, defaultdict
import csv
import math
import re
import statistics as st

ROOT = Path("experiment-logs")
MASTER = sorted(ROOT.glob("full-matrix-*"), key=lambda p: p.stat().st_mtime, reverse=True)[0]

METHODS = ["static-k1", "static-k2", "static-k3", "static-k4", "static-k5", "iod"]
TCRIT_95_DF9 = 2.262

def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0

def sd(xs):
    return st.stdev(xs) if len(xs) >= 2 else 0.0

def ci95(xs):
    if len(xs) < 2:
        return 0.0
    t = TCRIT_95_DF9 if len(xs) == 10 else 1.96
    return t * sd(xs) / math.sqrt(len(xs))

def latest_run_dir(method, seed):
    runs = sorted(ROOT.glob(f"scale-{method}-{seed}-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None

def run_dir_from_log(log_file, method, seed):
    p = Path(log_file)
    if p.exists():
        txt = p.read_text(errors="ignore")
        m = re.search(r"RESULTS_DIR=(\S+)", txt)
        if m:
            d = Path(m.group(1))
            if d.exists():
                return d
    return latest_run_dir(method, seed)

def read_scale_sequences():
    f = MASTER / "scale_sequences.csv"
    seq = {}
    if not f.exists():
        return seq
    for r in csv.DictReader(f.open()):
        seq[int(r["seed"])] = r["scale_values"]
    return seq

def read_rounds(run_dir):
    f = run_dir / "rounds.csv"
    if not f.exists():
        return []
    return list(csv.DictReader(f.open()))

def aggregate_split(run_dir):
    agg = defaultdict(lambda: {"packets": 0, "bytes": 0, "samples": 0})

    for f in sorted(run_dir.glob("stage_*/round_*/split_stats.txt")):
        txt = f.read_text(errors="ignore")
        for b in txt.split("statistics {")[1:]:
            teid = re.search(r"teid:\s*(\d+)", b)
            k = re.search(r"k:\s*(\d+)", b)
            packets = re.search(r"packets:\s*(\d+)", b)
            bytes_ = re.search(r"bytes:\s*(\d+)", b)

            if all([teid, k, packets, bytes_]):
                key = (int(teid.group(1)), int(k.group(1)))
                agg[key]["packets"] += int(packets.group(1))
                agg[key]["bytes"] += int(bytes_.group(1))
                agg[key]["samples"] += 1

    return agg

def summarize_run(seed, method, status, log_file, scale_values):
    run_dir = run_dir_from_log(log_file, method, seed)
    if run_dir is None:
        raise RuntimeError(f"Run dir not found for seed={seed} method={method}")

    rounds = read_rounds(run_dir)
    agg = aggregate_split(run_dir)

    k_count = Counter()
    reason_count = Counter()
    tx_vals = []
    head_vals = []
    tx_by_k = defaultdict(list)
    head_by_k = defaultdict(list)

    for r in rounds:
        k = str(r.get("k", "")).strip()
        reason = str(r.get("reason", "")).strip()
        if k:
            k_count[k] += 1
        if reason:
            reason_count[reason] += 1

        try:
            tx = float(r.get("last_tx_ms", "0"))
        except ValueError:
            tx = 0.0

        try:
            head = float(r.get("last_head_ms", "0"))
        except ValueError:
            head = 0.0

        if tx > 0:
            tx_vals.append(tx)
            if k:
                tx_by_k[k].append(tx)

        if head > 0:
            head_vals.append(head)
            if k:
                head_by_k[k].append(head)

    total_packets = sum(v["packets"] for v in agg.values())
    total_bytes = sum(v["bytes"] for v in agg.values())
    total_round_decisions = len(rounds)

    return {
        "seed": seed,
        "method": method,
        "status": int(status),
        "run_dir": str(run_dir),
        "scale_values": scale_values,
        "round_decisions": total_round_decisions,
        "k1_count": k_count.get("1", 0),
        "k2_count": k_count.get("2", 0),
        "k3_count": k_count.get("3", 0),
        "k4_count": k_count.get("4", 0),
        "k5_count": k_count.get("5", 0),
        "normal_count": reason_count.get("normal", 0),
        "tx_bad_count": reason_count.get("tx_bad", 0),
        "head_bad_count": reason_count.get("head_bad", 0),
        "mixed_app_bad_count": reason_count.get("mixed_app_bad", 0),
        "static_count": reason_count.get("static", 0),
        "mean_tx_ms": mean(tx_vals),
        "mean_head_ms": mean(head_vals),
        "mean_tx_k1_ms": mean(tx_by_k["1"]),
        "mean_tx_k2_ms": mean(tx_by_k["2"]),
        "mean_tx_k3_ms": mean(tx_by_k["3"]),
        "mean_tx_k4_ms": mean(tx_by_k["4"]),
        "mean_tx_k5_ms": mean(tx_by_k["5"]),
        "packets": total_packets,
        "bytes": total_bytes,
        "mb": total_bytes / 1_000_000,
        "packets_per_decision": total_packets / total_round_decisions if total_round_decisions else 0,
        "bytes_per_decision": total_bytes / total_round_decisions if total_round_decisions else 0,
        "split_entries": len(agg),
        "chaos_traces": len(list(run_dir.glob("ue_chaos_trace_*.csv"))),
    }

scale_seq = read_scale_sequences()

runs_csv = MASTER / "runs.csv"
runs = list(csv.DictReader(runs_csv.open()))

by_run = []

for r in runs:
    seed = int(r["seed"])
    method = r["method"]
    status = int(r["status"])
    log_file = r["log_file"]
    scale_values = scale_seq.get(seed, "")

    by_run.append(summarize_run(seed, method, status, log_file, scale_values))

out_by_run = ROOT / "full_matrix_by_run.csv"
fields = list(by_run[0].keys())

with out_by_run.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    w.writerows(by_run)

by_method = defaultdict(list)
for r in by_run:
    by_method[r["method"]].append(r)

method_rows = []
for method in METHODS:
    rows = by_method[method]
    bytes_ = [r["bytes"] for r in rows]
    packets = [r["packets"] for r in rows]
    mb = [r["mb"] for r in rows]
    bpd = [r["bytes_per_decision"] for r in rows]
    ppd = [r["packets_per_decision"] for r in rows]
    tx = [r["mean_tx_ms"] for r in rows if r["mean_tx_ms"] > 0]

    method_rows.append({
        "method": method,
        "n": len(rows),
        "mean_mb": mean(mb),
        "sd_mb": sd(mb),
        "ci95_mb": ci95(mb),
        "mean_bytes": mean(bytes_),
        "ci95_bytes": ci95(bytes_),
        "mean_packets": mean(packets),
        "ci95_packets": ci95(packets),
        "mean_bytes_per_decision": mean(bpd),
        "ci95_bytes_per_decision": ci95(bpd),
        "mean_packets_per_decision": mean(ppd),
        "ci95_packets_per_decision": ci95(ppd),
        "mean_tx_ms": mean(tx),
        "ci95_tx_ms": ci95(tx),
        "mean_k1_count": mean([r["k1_count"] for r in rows]),
        "mean_k2_count": mean([r["k2_count"] for r in rows]),
        "mean_k3_count": mean([r["k3_count"] for r in rows]),
        "mean_k4_count": mean([r["k4_count"] for r in rows]),
        "mean_k5_count": mean([r["k5_count"] for r in rows]),
        "mean_tx_bad_count": mean([r["tx_bad_count"] for r in rows]),
    })

out_method = ROOT / "full_matrix_method_summary.csv"
with out_method.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(method_rows[0].keys()))
    w.writeheader()
    w.writerows(method_rows)

by_seed_method = {(r["seed"], r["method"]): r for r in by_run}

vs_rows = []
for seed in sorted(set(r["seed"] for r in by_run)):
    iod = by_seed_method.get((seed, "iod"))
    if not iod:
        continue

    for baseline in ["static-k1", "static-k2", "static-k3", "static-k4", "static-k5"]:
        b = by_seed_method.get((seed, baseline))
        if not b:
            continue

        vs_rows.append({
            "seed": seed,
            "baseline": baseline,
            "iod_bytes": iod["bytes"],
            "baseline_bytes": b["bytes"],
            "delta_bytes_baseline_minus_iod": b["bytes"] - iod["bytes"],
            "traffic_reduction_pct_vs_baseline": 100 * (b["bytes"] - iod["bytes"]) / b["bytes"] if b["bytes"] else 0,
            "iod_packets": iod["packets"],
            "baseline_packets": b["packets"],
            "delta_packets_baseline_minus_iod": b["packets"] - iod["packets"],
            "packet_reduction_pct_vs_baseline": 100 * (b["packets"] - iod["packets"]) / b["packets"] if b["packets"] else 0,
            "iod_decisions": iod["round_decisions"],
            "baseline_decisions": b["round_decisions"],
            "iod_k5_count": iod["k5_count"],
            "iod_tx_bad_count": iod["tx_bad_count"],
        })

out_vs = ROOT / "full_matrix_vs_iod_by_seed.csv"
with out_vs.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(vs_rows[0].keys()))
    w.writeheader()
    w.writerows(vs_rows)

vs_summary_rows = []
by_baseline = defaultdict(list)
for r in vs_rows:
    by_baseline[r["baseline"]].append(r)

for baseline in ["static-k1", "static-k2", "static-k3", "static-k4", "static-k5"]:
    rows = by_baseline[baseline]
    red_b = [r["traffic_reduction_pct_vs_baseline"] for r in rows]
    red_p = [r["packet_reduction_pct_vs_baseline"] for r in rows]
    delta_mb = [r["delta_bytes_baseline_minus_iod"] / 1_000_000 for r in rows]

    vs_summary_rows.append({
        "baseline": baseline,
        "n": len(rows),
        "mean_traffic_reduction_pct": mean(red_b),
        "sd_traffic_reduction_pct": sd(red_b),
        "ci95_traffic_reduction_pct": ci95(red_b),
        "mean_packet_reduction_pct": mean(red_p),
        "ci95_packet_reduction_pct": ci95(red_p),
        "mean_delta_mb": mean(delta_mb),
        "ci95_delta_mb": ci95(delta_mb),
    })

out_vs_summary = ROOT / "full_matrix_vs_iod_summary.csv"
with out_vs_summary.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(vs_summary_rows[0].keys()))
    w.writeheader()
    w.writerows(vs_summary_rows)

print()
print(f"MASTER={MASTER}")
print(f"WROTE={out_by_run}")
print(f"WROTE={out_method}")
print(f"WROTE={out_vs}")
print(f"WROTE={out_vs_summary}")

print()
print("===== method summary =====")
for r in method_rows:
    print(
        f"{r['method']:<10} "
        f"n={r['n']:<2} "
        f"MB={r['mean_mb']:.2f}±{r['ci95_mb']:.2f} "
        f"packets={r['mean_packets']:.0f}±{r['ci95_packets']:.0f} "
        f"B/decision={r['mean_bytes_per_decision']:.0f}±{r['ci95_bytes_per_decision']:.0f} "
        f"mean_tx={r['mean_tx_ms']:.2f}ms"
    )

print()
print("===== IOD vs baselines =====")
for r in vs_summary_rows:
    print(
        f"IOD vs {r['baseline']:<9} "
        f"traffic_reduction={r['mean_traffic_reduction_pct']:.2f}%±{r['ci95_traffic_reduction_pct']:.2f} "
        f"delta={r['mean_delta_mb']:.2f}MB±{r['ci95_delta_mb']:.2f}"
    )
