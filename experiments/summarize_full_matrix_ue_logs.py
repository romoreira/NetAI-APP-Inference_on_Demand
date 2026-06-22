from pathlib import Path
from collections import defaultdict, Counter
import csv
import re
import math
import statistics as st

ROOT = Path("experiment-logs")
MASTER = sorted(ROOT.glob("full-matrix-*"), key=lambda p: p.stat().st_mtime, reverse=True)[0]

METHODS = ["static-k1", "static-k2", "static-k3", "static-k4", "static-k5", "iod"]
TCRIT_95_DF9 = 2.262

line_re = re.compile(
    r"frame=(?P<frame>\d+)\s+"
    r"head_infer=(?P<head>[\d.]+)ms\s+"
    r"tx=(?P<tx>[\d.]+)ms\s+"
    r"bytes=(?P<bytes>\d+)\s+"
    r"frags=(?P<frags>\d+)\s+"
    r"sent=(?P<sent>\d+)"
)

header_k_re = re.compile(r"\sk=(\d+)\s+")

def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0

def sd(xs):
    return st.stdev(xs) if len(xs) >= 2 else 0.0

def ci95(xs):
    if len(xs) < 2:
        return 0.0
    t = TCRIT_95_DF9 if len(xs) == 10 else 1.96
    return t * sd(xs) / math.sqrt(len(xs))

def find_run_dir(method, seed, log_file):
    txt = Path(log_file).read_text(errors="ignore") if Path(log_file).exists() else ""
    m = re.search(r"RESULTS_DIR=(\S+)", txt)
    if m and Path(m.group(1)).exists():
        return Path(m.group(1))

    runs = sorted(ROOT.glob(f"scale-{method}-{seed}-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None

def parse_ue_log(path):
    txt = path.read_text(errors="ignore")
    km = header_k_re.search(txt)
    k = int(km.group(1)) if km else None

    rows = []
    for line in txt.splitlines():
        m = line_re.search(line)
        if not m:
            continue
        rows.append({
            "k": k,
            "head_ms": float(m.group("head")),
            "tx_ms": float(m.group("tx")),
            "bytes": int(m.group("bytes")),
            "frags": int(m.group("frags")),
            "sent": int(m.group("sent")),
        })
    return rows

def summarize_run(seed, method, run_dir):
    frames = []
    for f in sorted(run_dir.glob("stage_*/round_*/ue_slot*_teid*.log")):
        frames.extend(parse_ue_log(f))

    k_count = Counter()
    tx_by_k = defaultdict(list)
    head_by_k = defaultdict(list)
    bytes_by_k = defaultdict(int)
    sent_by_k = defaultdict(int)
    frags_by_k = defaultdict(int)

    total_bytes = 0
    total_sent = 0
    total_frags = 0
    tx_vals = []
    head_vals = []

    for r in frames:
        k = r["k"]
        if k is not None:
            k_count[k] += 1
            tx_by_k[k].append(r["tx_ms"])
            head_by_k[k].append(r["head_ms"])
            bytes_by_k[k] += r["bytes"]
            sent_by_k[k] += r["sent"]
            frags_by_k[k] += r["frags"]

        total_bytes += r["bytes"]
        total_sent += r["sent"]
        total_frags += r["frags"]
        tx_vals.append(r["tx_ms"])
        head_vals.append(r["head_ms"])

    return {
        "seed": seed,
        "method": method,
        "run_dir": str(run_dir),
        "frames": len(frames),
        "ue_bytes": total_bytes,
        "ue_mb": total_bytes / 1_000_000,
        "ue_sent": total_sent,
        "ue_frags": total_frags,
        "mean_tx_ms": mean(tx_vals),
        "mean_head_ms": mean(head_vals),
        "k1_frames": k_count[1],
        "k2_frames": k_count[2],
        "k3_frames": k_count[3],
        "k4_frames": k_count[4],
        "k5_frames": k_count[5],
        "mean_tx_k1_ms": mean(tx_by_k[1]),
        "mean_tx_k2_ms": mean(tx_by_k[2]),
        "mean_tx_k3_ms": mean(tx_by_k[3]),
        "mean_tx_k4_ms": mean(tx_by_k[4]),
        "mean_tx_k5_ms": mean(tx_by_k[5]),
        "bytes_k1": bytes_by_k[1],
        "bytes_k2": bytes_by_k[2],
        "bytes_k3": bytes_by_k[3],
        "bytes_k4": bytes_by_k[4],
        "bytes_k5": bytes_by_k[5],
        "sent_k1": sent_by_k[1],
        "sent_k2": sent_by_k[2],
        "sent_k3": sent_by_k[3],
        "sent_k4": sent_by_k[4],
        "sent_k5": sent_by_k[5],
    }

runs = list(csv.DictReader((MASTER / "runs.csv").open()))
by_run = []

for r in runs:
    seed = int(r["seed"])
    method = r["method"]
    run_dir = find_run_dir(method, seed, r["log_file"])
    if run_dir is None:
        raise RuntimeError(f"missing run dir for {seed} {method}")
    by_run.append(summarize_run(seed, method, run_dir))

out = ROOT / "full_matrix_ue_by_run.csv"
with out.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(by_run[0].keys()))
    w.writeheader()
    w.writerows(by_run)

by_method = defaultdict(list)
for r in by_run:
    by_method[r["method"]].append(r)

method_rows = []
for method in METHODS:
    rows = by_method[method]
    mb = [r["ue_mb"] for r in rows]
    sent = [r["ue_sent"] for r in rows]
    tx = [r["mean_tx_ms"] for r in rows]
    head = [r["mean_head_ms"] for r in rows]

    method_rows.append({
        "method": method,
        "n": len(rows),
        "mean_ue_mb": mean(mb),
        "ci95_ue_mb": ci95(mb),
        "mean_sent": mean(sent),
        "ci95_sent": ci95(sent),
        "mean_tx_ms": mean(tx),
        "ci95_tx_ms": ci95(tx),
        "mean_head_ms": mean(head),
        "ci95_head_ms": ci95(head),
        "mean_k1_frames": mean([r["k1_frames"] for r in rows]),
        "mean_k2_frames": mean([r["k2_frames"] for r in rows]),
        "mean_k3_frames": mean([r["k3_frames"] for r in rows]),
        "mean_k4_frames": mean([r["k4_frames"] for r in rows]),
        "mean_k5_frames": mean([r["k5_frames"] for r in rows]),
    })

out2 = ROOT / "full_matrix_ue_method_summary.csv"
with out2.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(method_rows[0].keys()))
    w.writeheader()
    w.writerows(method_rows)

by_seed_method = {(r["seed"], r["method"]): r for r in by_run}

vs_rows = []
for seed in sorted(set(r["seed"] for r in by_run)):
    iod = by_seed_method[(seed, "iod")]
    for baseline in ["static-k1", "static-k2", "static-k3", "static-k4", "static-k5"]:
        b = by_seed_method[(seed, baseline)]
        vs_rows.append({
            "seed": seed,
            "baseline": baseline,
            "baseline_ue_mb": b["ue_mb"],
            "iod_ue_mb": iod["ue_mb"],
            "delta_mb": b["ue_mb"] - iod["ue_mb"],
            "reduction_pct": 100 * (b["ue_mb"] - iod["ue_mb"]) / b["ue_mb"] if b["ue_mb"] else 0,
            "baseline_sent": b["ue_sent"],
            "iod_sent": iod["ue_sent"],
            "sent_reduction_pct": 100 * (b["ue_sent"] - iod["ue_sent"]) / b["ue_sent"] if b["ue_sent"] else 0,
            "iod_k1_frames": iod["k1_frames"],
            "iod_k2_frames": iod["k2_frames"],
            "iod_k3_frames": iod["k3_frames"],
            "iod_k4_frames": iod["k4_frames"],
            "iod_k5_frames": iod["k5_frames"],
        })

out3 = ROOT / "full_matrix_ue_vs_iod_by_seed.csv"
with out3.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(vs_rows[0].keys()))
    w.writeheader()
    w.writerows(vs_rows)

by_baseline = defaultdict(list)
for r in vs_rows:
    by_baseline[r["baseline"]].append(r)

vs_summary = []
for baseline in ["static-k1", "static-k2", "static-k3", "static-k4", "static-k5"]:
    rows = by_baseline[baseline]
    red = [r["reduction_pct"] for r in rows]
    sent_red = [r["sent_reduction_pct"] for r in rows]
    delta = [r["delta_mb"] for r in rows]
    vs_summary.append({
        "baseline": baseline,
        "n": len(rows),
        "mean_reduction_pct": mean(red),
        "ci95_reduction_pct": ci95(red),
        "mean_sent_reduction_pct": mean(sent_red),
        "ci95_sent_reduction_pct": ci95(sent_red),
        "mean_delta_mb": mean(delta),
        "ci95_delta_mb": ci95(delta),
    })

out4 = ROOT / "full_matrix_ue_vs_iod_summary.csv"
with out4.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(vs_summary[0].keys()))
    w.writeheader()
    w.writerows(vs_summary)

print(f"MASTER={MASTER}")
print(f"WROTE={out}")
print(f"WROTE={out2}")
print(f"WROTE={out3}")
print(f"WROTE={out4}")

print()
print("===== UE-side method summary =====")
for r in method_rows:
    print(
        f"{r['method']:<10} "
        f"MB={r['mean_ue_mb']:.2f}±{r['ci95_ue_mb']:.2f} "
        f"sent={r['mean_sent']:.0f}±{r['ci95_sent']:.0f} "
        f"tx={r['mean_tx_ms']:.2f}±{r['ci95_tx_ms']:.2f}ms "
        f"head={r['mean_head_ms']:.2f}±{r['ci95_head_ms']:.2f}ms "
        f"kframes=[{r['mean_k1_frames']:.0f},{r['mean_k2_frames']:.0f},{r['mean_k3_frames']:.0f},{r['mean_k4_frames']:.0f},{r['mean_k5_frames']:.0f}]"
    )

print()
print("===== UE-side IOD vs baselines =====")
for r in vs_summary:
    print(
        f"IOD vs {r['baseline']:<9} "
        f"MB_reduction={r['mean_reduction_pct']:.2f}%±{r['ci95_reduction_pct']:.2f} "
        f"sent_reduction={r['mean_sent_reduction_pct']:.2f}%±{r['ci95_sent_reduction_pct']:.2f} "
        f"delta={r['mean_delta_mb']:.2f}MB±{r['ci95_delta_mb']:.2f}"
    )
