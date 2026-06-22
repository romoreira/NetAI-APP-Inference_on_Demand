from pathlib import Path
import csv
import re
from collections import defaultdict

ROOT = Path("experiment-logs")
MASTER = sorted(ROOT.glob("full-matrix-*"), key=lambda p: p.stat().st_mtime, reverse=True)[0]

EXPECTED_SEEDS = [str(s) for s in range(4001, 4011)]
EXPECTED_METHODS = ["static-k1", "static-k2", "static-k3", "static-k4", "static-k5", "iod"]

runs_csv = MASTER / "runs.csv"
print(f"MASTER={MASTER}")

if not runs_csv.exists():
    print("ERROR: runs.csv not found")
    raise SystemExit(1)

rows = list(csv.DictReader(runs_csv.open()))
print(f"runs={len(rows)}/60")

bad_status = [r for r in rows if r["status"] != "0"]
print(f"bad_status={len(bad_status)}")

seen = {(r["seed"], r["method"]) for r in rows}
missing = []
for seed in EXPECTED_SEEDS:
    for method in EXPECTED_METHODS:
        if (seed, method) not in seen:
            missing.append((seed, method))

print(f"missing={len(missing)}")
for seed, method in missing[:20]:
    print(f"  MISSING seed={seed} method={method}")

if bad_status:
    print("BAD STATUS:")
    for r in bad_status:
        print(f"  seed={r['seed']} method={r['method']} status={r['status']} log={r['log_file']}")

def run_dir_from_log(log_file):
    p = Path(log_file)
    if p.exists():
        txt = p.read_text(errors="ignore")
        m = re.search(r"RESULTS_DIR=(\S+)", txt)
        if m:
            d = Path(m.group(1))
            if d.exists():
                return d
    return None

stats = []
errors = []

for r in rows:
    d = run_dir_from_log(r["log_file"])
    if d is None:
        errors.append((r["seed"], r["method"], "missing RESULTS_DIR"))
        continue

    split_files = list(d.glob("stage_*/round_*/split_stats.txt"))
    if not split_files:
        errors.append((r["seed"], r["method"], "no split_stats files"))
        continue

    txt_all = ""
    packets_files = 0
    permission_errors = 0

    for f in split_files:
        txt = f.read_text(errors="ignore")
        txt_all += txt
        if "packets:" in txt and "bytes:" in txt:
            packets_files += 1
        if "Permission denied" in txt or "publickey" in txt:
            permission_errors += 1

    stats.append({
        "seed": r["seed"],
        "method": r["method"],
        "dir": str(d),
        "split_files": len(split_files),
        "packet_files": packets_files,
        "permission_errors": permission_errors,
    })

print()
print("===== split_stats validation =====")
bad_split = 0
for s in stats:
    ok = s["packet_files"] > 0 and s["permission_errors"] == 0
    if not ok:
        bad_split += 1
        print(
            f"BAD seed={s['seed']} method={s['method']} "
            f"split_files={s['split_files']} packet_files={s['packet_files']} "
            f"permission_errors={s['permission_errors']} dir={s['dir']}"
        )

print(f"bad_split_runs={bad_split}")

print()
print("===== per-seed counts =====")
by_seed = defaultdict(list)
for r in rows:
    by_seed[r["seed"]].append(r["method"])

for seed in sorted(by_seed):
    methods = by_seed[seed]
    print(f"seed={seed} n={len(methods)} methods={','.join(methods)}")

print()
if len(rows) == 60 and not bad_status and not missing and bad_split == 0:
    print("OK: full matrix completed and UPF split_stats look valid.")
else:
    print("NOT OK: see issues above.")
