#!/usr/bin/env python3
"""
make_iod_figures.py  (v3 - 6 figures: 3 policy-independent + 3 policy-descriptive)
----------------------------------------------------------------------------------
Runs ON THE UE MACHINE against ~/NetAI-APP-Inference_on_Demand/experiment-logs/.
Writes ./iod_figures/ : 6 vector PDFs (one plot/file, NO title, axis font 16,
numeric labels on figures) + the aggregated CSV behind each.

POLICY-INDEPENDENT (hold no matter how the controller picks k):
  fig3_compute_vs_bytes_frontier.pdf   operating points (head-infer x bytes)
  fig5_paired_delta_per_seed.pdf       per-seed robustness (IoD - static)
  fig6_ue_upf_path_closure.pdf         system closure (UE bytes vs UPF bytes)
POLICY-DESCRIPTIVE (describe the OBSERVED IoD behaviour; neutral wording, no
  claim about design intent - caption decides 'by design' vs 'emergent'):
  fig1_k_trajectory_vs_scale.pdf       observed mean-k vs replicas (+/- std)
  fig2_adaptation_rate_vs_scale.pdf    observed fraction of tx_bad rounds vs replicas
  fig7_iod_regime_share.pdf            observed P(k) per replicas as stacked area
                                       (only k values actually used appear)

Parser facts (from real logs):
  rounds.csv: per-slot UE metrics; round_000 warm-up rows (sent==0) dropped for UE
    metrics, KEPT for k/reason counts (k is valid there).
  split_stats.txt: PER-TEID blocks; round total = SUM over blocks.
  stage dir 'stage_NN_replicas_R' -> R replicas.
"""

import os, re, csv, glob, sys, math
from collections import defaultdict
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

ROOT = os.path.expanduser("~/NetAI-APP-Inference_on_Demand")
LOGS = os.path.join(ROOT, "experiment-logs")
OUT  = os.path.abspath("./iod_figures")
os.makedirs(OUT, exist_ok=True)

METHODS = ["static-k1", "static-k2", "static-k3", "static-k4", "static-k5", "iod"]
STATIC_K = {f"static-k{i}": i for i in range(1, 6)}

plt.rcParams.update({
    "pdf.fonttype": 42, "ps.fonttype": 42,
    "font.family": "serif", "font.size": 14,
    "axes.labelsize": 16, "xtick.labelsize": 16, "ytick.labelsize": 16,
    "legend.fontsize": 12,
    "axes.grid": True, "grid.alpha": 0.30, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.dpi": 120,
})
CMAP = {"static-k1": "#4878CF", "static-k2": "#6ACC65", "static-k3": "#D5BB67",
        "static-k4": "#B47CC7", "static-k5": "#8C8C8C", "iod": "#C44E52"}
KCOL = {1: "#4878CF", 2: "#6ACC65", 3: "#D5BB67", 4: "#B47CC7", 5: "#C44E52"}
FIGSIZE = (5.6, 3.8)
STAGE_RE = re.compile(r"stage_(\d+)_replicas_(\d+)")

def latest_master():
    c = sorted(glob.glob(os.path.join(LOGS, "full-matrix-*")), key=os.path.getmtime, reverse=True)
    if not c: sys.exit("No full-matrix-* dir under experiment-logs/")
    return c[0]

def read_runs(master):
    with open(os.path.join(master, "runs.csv")) as f:
        return list(csv.DictReader(f))

def resolve_run_dir(method, seed, log_file):
    if log_file and os.path.isfile(log_file):
        try:
            with open(log_file, errors="ignore") as f:
                for line in f:
                    if line.startswith("RESULTS_DIR="):
                        d = line.strip().split("=", 1)[1]
                        if os.path.isdir(d): return d
        except Exception:
            pass
    g = sorted(glob.glob(os.path.join(LOGS, f"scale-{method}-{seed}-*")), key=os.path.getmtime, reverse=True)
    return g[0] if g else None

def parse_split_total(path):
    if not os.path.isfile(path): return None
    tot_p = tot_b = 0; seen = False; cur_p = None
    with open(path, errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line.startswith("packets:"):
                cur_p = int(line.split(":")[1])
            elif line.startswith("bytes:") and cur_p is not None:
                tot_p += cur_p; tot_b += int(line.split(":")[1]); seen = True; cur_p = None
    return (tot_p, tot_b) if seen else None

def parse_rounds(path, method):
    if not os.path.isfile(path): return
    with open(path, errors="ignore") as f:
        for r in csv.DictReader(f):
            try:
                yield {
                    "stage": int(r["stage"]), "round": int(r["round"]),
                    "slot": int(r["slot"]), "teid": int(r["teid"]),
                    "head_ms": float(r.get("last_head_ms", "nan") or "nan"),
                    "tx_ms": float(r.get("last_tx_ms", "nan") or "nan"),
                    "sent": int(float(r.get("last_sent", 0) or 0)),
                    "bytes": int(float(r.get("last_bytes", 0) or 0)),
                    "k": int(r["k"]),
                    "reason": (r.get("reason") or "").strip(),
                    "split_stats_file": r.get("split_stats_file", "").strip(),
                    "method": method,
                }
            except (KeyError, ValueError):
                continue

def is_measured(r):
    return r["sent"] > 0 and r["bytes"] > 0 and not math.isnan(r["head_ms"])

def load_all():
    master = latest_master(); print(f"[load] master = {master}")
    rows, split_seen, missing = [], {}, []
    for run in read_runs(master):
        method, seed = run["method"], run["seed"]
        if method not in METHODS: continue
        rd = resolve_run_dir(method, seed, run.get("log_file", ""))
        if not rd: missing.append((method, seed)); continue
        for row in parse_rounds(os.path.join(rd, "rounds.csv"), method):
            sdirs = glob.glob(os.path.join(rd, f"stage_{row['stage']:02d}_replicas_*"))
            reps = None
            if sdirs:
                m = STAGE_RE.search(os.path.basename(sdirs[0]))
                if m: reps = int(m.group(2))
            row.update(seed=seed, replicas=reps, run_dir=rd)
            rows.append(row)
            key = (rd, row["stage"], row["round"])
            if key not in split_seen and row["split_stats_file"]:
                t = parse_split_total(row["split_stats_file"])
                if t: split_seen[key] = t
    print(f"[load] rows={len(rows)}  round-totals={len(split_seen)}  missing={len(missing)}")
    return rows, split_seen

def save(fig, name):
    fig.subplots_adjust(left=0.205, right=0.96, bottom=0.165, top=0.93)
    p = os.path.join(OUT, name); fig.savefig(p); plt.close(fig); print(f"[fig] {p}")

def dump_csv(name, header, rows):
    with open(os.path.join(OUT, name), "w", newline="") as f:
        w = csv.writer(f); w.writerow(header); [w.writerow(r) for r in rows]
    print(f"[csv] {os.path.join(OUT, name)}")

# ----------------------------------------------------- policy-independent figures
def figA_pareto(rows):
    pts = defaultdict(lambda: defaultdict(lambda: ([], [])))
    for r in rows:
        if is_measured(r):
            h, b = pts[r["method"]][r["seed"]]; h.append(r["head_ms"]); b.append(r["bytes"])
    out = []; fig, ax = plt.subplots(figsize=FIGSIZE)
    for m in METHODS:
        xs, ys = [], []
        for seed, (h, b) in pts[m].items():
            if not h: continue
            mh, mb = float(np.mean(h)), float(np.mean(b)) / 1024.0
            xs.append(mh); ys.append(mb); out.append([m, seed, f"{mh:.3f}", f"{mb:.2f}"])
        if not xs: continue
        ax.scatter(xs, ys, s=42, color=CMAP[m], alpha=0.70, edgecolor="white",
                   linewidth=0.5, zorder=3, marker=("*" if m == "iod" else "o"),
                   label=("IoD" if m == "iod" else m.replace("static-k", "k=")))
        ax.scatter([np.mean(xs)], [np.mean(ys)], s=170 if m == "iod" else 95,
                   color=CMAP[m], marker=("*" if m == "iod" else "o"),
                   edgecolor="black", linewidth=1.1, zorder=4)
    ic = pts["iod"]
    if ic:
        ax_ = np.mean([np.mean(v[0]) for v in ic.values()])
        ay_ = np.mean([np.mean(v[1]) for v in ic.values()]) / 1024.0
        ax.annotate(f"({ax_:.1f} ms, {ay_:.0f} KB)", (ax_, ay_),
                    textcoords="offset points", xytext=(10, 8), fontsize=12, color=CMAP["iod"])
    dump_csv("fig3_pareto_points.csv", ["method", "seed", "head_ms", "ue_bytes_KB"], out)
    ax.set_xlabel("UE head inference  [ms]")
    ax.set_ylabel("UE bytes / frame  [KB]")
    ax.legend(loc="upper right", frameon=False, ncol=2, handletextpad=0.3, columnspacing=0.8)
    save(fig, "fig3_compute_vs_bytes_frontier.pdf")

def figB_paired(rows):
    acc = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if is_measured(r): acc[r["seed"]][r["method"]].append(r["bytes"])
    smb = {s: {m: np.mean(v) / 1024.0 for m, v in d.items()} for s, d in acc.items()}
    statics = [m for m in METHODS if m != "iod"]
    data, out = {m: [] for m in statics}, []
    for s, mb in smb.items():
        if "iod" not in mb: continue
        for m in statics:
            if m in mb:
                d = mb["iod"] - mb[m]; data[m].append(d); out.append([s, m, f"{d:.2f}"])
    dump_csv("fig5_paired_delta.csv", ["seed", "static_method", "iod_minus_static_KB"], out)
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for i, m in enumerate(statics):
        v = data[m]
        if not v: continue
        j = (np.random.RandomState(i).rand(len(v)) - 0.5) * 0.26
        ax.scatter(np.full(len(v), i) + j, v, s=30, color=CMAP[m], alpha=0.6,
                   edgecolor="white", linewidth=0.4, zorder=3)
        mu = float(np.mean(v))
        ax.plot([i - 0.3, i + 0.3], [mu, mu], color="black", lw=2.0, zorder=4)
        ax.annotate(f"{mu:+.0f}", (i, mu), textcoords="offset points",
                    xytext=(0, 11 if mu >= 0 else -16), ha="center", fontsize=13, fontweight="bold")
    ax.axhline(0, color="grey", lw=1.0, ls="--")
    ax.set_xticks(range(len(statics)))
    ax.set_xticklabels([m.replace("static-", "") for m in statics])
    ax.set_xlabel("Static baseline (paired by seed)")
    ax.set_ylabel("IoD - static  [KB]")
    save(fig, "fig5_paired_delta_per_seed.pdf")

def figC_upf_load(rows, split):
    """Total UPF-counted bytes per round vs active replicas, per method.
    Shows how each policy loads the core network as demand scales. The honest
    systems message: IoD keeps core load bounded between the heavy (k1/k2) and
    light (k5) envelopes WITHOUT fixing k a priori. Absolute load (not per-replica)
    so the figure answers 'how much can the UPF take under scale'."""
    # round-key -> (method, replicas)
    meta = {}
    for r in rows:
        meta[(r["run_dir"], r["stage"], r["round"])] = (r["method"], r["replicas"])
    by = defaultdict(lambda: defaultdict(list))  # method -> replicas -> [MB]
    for key, (pk, byb) in split.items():
        if key not in meta: continue
        method, reps = meta[key]
        if reps: by[method][reps].append(byb / 1e6)
    out = []
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for m in METHODS:
        reps = sorted(by[m])
        if not reps: continue
        means = [float(np.mean(by[m][x])) for x in reps]
        for x, v in zip(reps, means): out.append([m, x, f"{v:.3f}"])
        ax.plot(reps, means, color=CMAP[m],
                lw=(2.6 if m == "iod" else 1.4),
                marker=("*" if m == "iod" else "o"),
                ms=(10 if m == "iod" else 5), alpha=0.95,
                label=("IoD" if m == "iod" else m.replace("static-k", "k=")),
                zorder=(6 if m == "iod" else 2))
    dump_csv("fig6_upf_load_vs_scale.csv", ["method", "replicas", "upf_MB_per_round"], out)
    # annotate IoD endpoint (load at max scale = the feasibility number)
    if by["iod"]:
        xr = sorted(by["iod"]); xm = xr[-1]; ym = float(np.mean(by["iod"][xm]))
        ax.annotate(f"{ym:.0f} MB", (xm, ym), textcoords="offset points",
                    xytext=(-6, 8), ha="right", fontsize=12, color=CMAP["iod"],
                    fontweight="bold")
    ax.set_xlabel("Active UE replicas")
    ax.set_ylabel("UPF load  [MB/round]")
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(loc="upper left", frameon=False, ncol=2,
              handletextpad=0.3, columnspacing=0.8)
    save(fig, "fig6_upf_load_vs_scale.pdf")

def figD_ktraj(rows):
    iod = [r for r in rows if r["method"] == "iod" and r["replicas"] is not None]
    by = defaultdict(list)
    for r in iod: by[r["replicas"]].append(r["k"])
    reps = sorted(by)
    mean = [np.mean(by[x]) for x in reps]; std = [np.std(by[x]) for x in reps]
    used_k = sorted({k for v in by.values() for k in v})
    dump_csv("fig1_k_vs_replicas.csv", ["replicas", "mean_k", "std_k", "n"],
             [[x, f"{m:.4f}", f"{s:.4f}", len(by[x])] for x, m, s in zip(reps, mean, std)])
    fig, ax = plt.subplots(figsize=FIGSIZE)
    # only reference lines for k values the controller actually used
    for m, k in STATIC_K.items():
        if k in used_k:
            ax.axhline(k, color=CMAP[m], lw=1.0, ls=":", alpha=0.6)
    ax.errorbar(reps, mean, yerr=std, color=CMAP["iod"], lw=2.0, marker="o", ms=6,
                capsize=3, label="IoD (mean +/- std over seeds)")
    for x, m in zip(reps, mean):
        ax.annotate(f"{m:.2f}", (x, m), textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=12, color=CMAP["iod"])
    ax.set_xlabel("Active UE replicas")
    ax.set_ylabel("Observed split point  k")
    ax.set_ylim(min(used_k) - 0.5, max(used_k) + 0.5)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(loc="best", frameon=False)
    save(fig, "fig1_k_trajectory_vs_scale.pdf")

def figE_adapt(rows):
    iod = [r for r in rows if r["method"] == "iod" and r["replicas"] is not None]
    by = defaultdict(lambda: defaultdict(int))
    for r in iod: by[r["replicas"]][r["reason"] or "normal"] += 1
    reps = sorted(by)
    frac = []; tots = []
    for x in reps:
        d = by[x]; tot = sum(d.values()); tots.append(tot)
        frac.append(d.get("tx_bad", 0) / tot if tot else 0.0)
    dump_csv("fig2_reason_vs_replicas.csv", ["replicas", "frac_tx_bad", "n"],
             [[x, f"{f:.4f}", n] for x, f, n in zip(reps, frac, tots)])
    fig, ax = plt.subplots(figsize=FIGSIZE)
    ax.plot(reps, frac, color=CMAP["iod"], lw=2.0, marker="s", ms=6)
    for x, f in zip(reps, frac):
        ax.annotate(f"{f*100:.0f}%", (x, f), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=12)
    ax.set_xlabel("Active UE replicas")
    ax.set_ylabel("Rounds in protection regime")
    ax.set_ylim(0, max(0.05, max(frac) * 1.25))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v*100:.0f}%"))
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    save(fig, "fig2_adaptation_rate_vs_scale.pdf")

def figF_regime_share(rows):
    """Stacked area of P(k|replicas). Only k values actually used get a band -
    no dead/empty rows, so the figure never advertises unused k."""
    iod = [r for r in rows if r["method"] == "iod" and r["replicas"] is not None]
    by = defaultdict(list)
    for r in iod: by[r["replicas"]].append(r["k"])
    reps = sorted(by)
    used_k = sorted({k for v in by.values() for k in v})
    P = {k: [] for k in used_k}
    out = []
    for x in reps:
        v = by[x]; tot = len(v) or 1
        for k in used_k:
            p = v.count(k) / tot; P[k].append(p); out.append([x, k, f"{p:.4f}"])
    dump_csv("fig7_regime_share.csv", ["replicas", "k", "prob"], out)
    fig, ax = plt.subplots(figsize=FIGSIZE)
    ax.stackplot(reps, *[P[k] for k in used_k],
                 colors=[KCOL.get(k, "#777") for k in used_k],
                 labels=[f"k={k}" for k in used_k], alpha=0.85)
    # numeric label: share of the highest-k (protection) band at each replica
    hi = used_k[-1]
    for x, p in zip(reps, P[hi]):
        if p > 0.02:
            ax.annotate(f"{p*100:.0f}%", (x, 1.0 - p / 2), ha="center", va="center",
                        fontsize=11, color="white", fontweight="bold")
    ax.set_xlabel("Active UE replicas")
    ax.set_ylabel("Share of rounds")
    ax.set_ylim(0, 1); ax.set_xlim(min(reps), max(reps))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v*100:.0f}%"))
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(loc="lower left", frameon=False, ncol=len(used_k))
    save(fig, "fig7_iod_regime_share.pdf")

def main():
    rows, split = load_all()
    if not rows: sys.exit("No rows parsed.")
    figA_pareto(rows)
    figB_paired(rows)
    figC_upf_load(rows, split)
    figD_ktraj(rows)
    figE_adapt(rows)
    figF_regime_share(rows)
    print(f"\nDone. 6 PDFs + 6 CSVs in: {OUT}")

if __name__ == "__main__":
    main()
