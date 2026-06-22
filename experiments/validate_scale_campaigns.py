from pathlib import Path
from collections import defaultdict, Counter
import csv
import re

ROOT = Path("experiment-logs")
SEEDS = [777, 888, 999]

def latest_run(method, seed):
    runs = sorted(ROOT.glob(f"scale-{method}-{seed}-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None

def read_scale(run):
    f = run / "scale_events.csv"
    if not f.exists():
        return []
    rows = list(csv.DictReader(f.open()))
    return [int(r["replicas"]) for r in rows]

def read_rounds(run):
    f = run / "rounds.csv"
    if not f.exists():
        return []
    return list(csv.DictReader(f.open()))

def aggregate_split(run):
    agg = defaultdict(lambda: {"packets": 0, "bytes": 0, "samples": 0})

    for f in sorted(run.glob("stage_*/round_*/split_stats.txt")):
        txt = f.read_text(errors="ignore")
        blocks = txt.split("statistics {")
        for b in blocks[1:]:
            teid = re.search(r"teid:\s*(\d+)", b)
            k = re.search(r"k:\s*(\d+)", b)
            packets = re.search(r"packets:\s*(\d+)", b)
            bytes_ = re.search(r"bytes:\s*(\d+)", b)

            if teid and k and packets and bytes_:
                key = (int(teid.group(1)), int(k.group(1)))
                agg[key]["packets"] += int(packets.group(1))
                agg[key]["bytes"] += int(bytes_.group(1))
                agg[key]["samples"] += 1

    return agg

def summarize_run(run):
    rounds = read_rounds(run)
    k_counter = Counter()
    reason_counter = Counter()
    tx_by_k = defaultdict(list)

    for r in rounds:
        k = r.get("k", "")
        reason = r.get("reason", "")
        tx = r.get("last_tx_ms", "0")

        if k:
            k_counter[k] += 1
        if reason:
            reason_counter[reason] += 1

        try:
            txf = float(tx)
            if txf > 0 and k:
                tx_by_k[k].append(txf)
        except ValueError:
            pass

    agg = aggregate_split(run)
    total_packets = sum(v["packets"] for v in agg.values())
    total_bytes = sum(v["bytes"] for v in agg.values())

    return {
        "rounds": len(rounds),
        "k_counter": k_counter,
        "reason_counter": reason_counter,
        "tx_by_k": tx_by_k,
        "agg": agg,
        "total_packets": total_packets,
        "total_bytes": total_bytes,
        "done": (run / "split_stats_final.txt").exists(),
        "chaos_traces": len(list(run.glob("ue_chaos_trace_*.csv"))),
    }

def fmt_counter(c):
    if not c:
        return "-"
    return ", ".join(f"{k}:{v}" for k, v in sorted(c.items()))

def fmt_mean_tx(tx_by_k):
    out = []
    for k in sorted(tx_by_k, key=lambda x: int(x)):
        vals = tx_by_k[k]
        if vals:
            out.append(f"k={k}:{sum(vals)/len(vals):.2f}ms(n={len(vals)})")
    return ", ".join(out) if out else "-"

print("===== campaign validation =====")

for seed in SEEDS:
    static = latest_run("static-k3", seed)
    iod = latest_run("iod", seed)

    print()
    print(f"### seed={seed}")

    if not static:
        print("STATIC: missing")
        continue
    if not iod:
        print("IOD: missing")
        continue

    ss = summarize_run(static)
    si = summarize_run(iod)

    static_scale = read_scale(static)
    iod_scale = read_scale(iod)

    print(f"STATIC dir: {static}")
    print(f"IOD dir:    {iod}")

    print(f"finished: static={ss['done']} iod={si['done']}")
    print(f"chaos traces: static={ss['chaos_traces']} iod={si['chaos_traces']}")
    print(f"scale static: {static_scale}")
    print(f"scale iod:    {iod_scale}")
    print(f"same scale:   {static_scale == iod_scale}")

    print(f"decisions static: {fmt_counter(ss['k_counter'])}")
    print(f"decisions iod:    {fmt_counter(si['k_counter'])}")
    print(f"reasons iod:      {fmt_counter(si['reason_counter'])}")
    print(f"mean tx iod:      {fmt_mean_tx(si['tx_by_k'])}")

    print(f"UPF static: packets={ss['total_packets']} bytes={ss['total_bytes']}")
    print(f"UPF iod:    packets={si['total_packets']} bytes={si['total_bytes']}")

    if ss["total_bytes"] > 0:
        red_b = 100 * (ss["total_bytes"] - si["total_bytes"]) / ss["total_bytes"]
        red_p = 100 * (ss["total_packets"] - si["total_packets"]) / ss["total_packets"]
        print(f"reduction: packets={red_p:.2f}% bytes={red_b:.2f}%")

    print("IOD UPF by TEID,k:")
    for (teid, k), v in sorted(si["agg"].items()):
        print(f"  teid={teid:<3} k={k} samples={v['samples']:<3} packets={v['packets']:<8} bytes={v['bytes']}")

print()
print("===== checks expected =====")
print("1) finished must be True for static and iod")
print("2) same scale must be True")
print("3) static should have only k=3")
print("4) iod should have k=3 and possibly k=5")
print("5) iod bytes/packets should be lower if it used k=5")
