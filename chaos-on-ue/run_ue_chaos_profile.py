#!/usr/bin/env python3
import csv
import os
import random
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# ============================================================
# Basic configuration
# ============================================================

NAMESPACE = "default"
LABEL_SELECTOR = "app=ue,role=ue"

CHAOS_SEED = int(os.environ.get("CHAOS_SEED", "12345"))
random.seed(CHAOS_SEED)

RUN_ID = os.environ.get("CHAOS_RUN_ID", time.strftime("chaos-%Y%m%d-%H%M%S"))
OUT = Path(f"ue_chaos_trace_{RUN_ID}.csv")

# Total time window in which events may start
EXPERIMENT_DURATION_S = 180

# Event process per UE
# Smaller value => more frequent events
MEAN_INTERARRIVAL_S = 35

# Event duration
MIN_EVENT_DURATION_S = 10
MAX_EVENT_DURATION_S = 45

# CPU chaos range
CPU_MIN = 15
CPU_MAX = 85

# Network delay range
LATENCY_MIN_MS = 5
LATENCY_MAX_MS = 90

# Network jitter range
JITTER_MIN_MS = 1
JITTER_MAX_MS = 20

# CPU workers used by Chaos Mesh StressChaos
CPU_WORKERS = 4


# ============================================================
# Shell helpers
# ============================================================

def run(cmd: str, check=True) -> str:
    p = subprocess.run(
        cmd,
        shell=True,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return p.stdout.strip()


def cleanup_previous_chaos():
    print("[CLEANUP] deleting previous StressChaos and NetworkChaos objects")

    run(
        "kubectl delete stresschaos --all -A --ignore-not-found=true",
        check=False,
    )

    run(
        "kubectl delete networkchaos --all -A --ignore-not-found=true",
        check=False,
    )

    print("[CLEANUP] done")


# ============================================================
# Pod discovery
# ============================================================

def get_ue_pods():
    cmd = (
        f"kubectl get pods -n {NAMESPACE} "
        f"-l {LABEL_SELECTOR} "
        "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}'"
    )

    pods = run(cmd).splitlines()

    if not pods:
        raise RuntimeError(f"No UE pods found with selector: {LABEL_SELECTOR}")

    return pods


# ============================================================
# Random model
# ============================================================

def beta_int(low, high, alpha=2.0, beta=2.0):
    """
    Bounded random value in [low, high].
    Beta(2,2) concentrates values around the middle, while still allowing extremes.
    """
    x = random.betavariate(alpha, beta)
    return int(round(low + x * (high - low)))


def next_events_for_pod(pod):
    """
    Generate asynchronous events for one UE pod.

    Events may overlap within the same pod.
    Different pods may also have concurrent events.
    A pod may receive no event during the experiment window.
    """
    events = []
    offset = random.expovariate(1.0 / MEAN_INTERARRIVAL_S)
    event_id = 0

    while offset < EXPERIMENT_DURATION_S:
        duration = random.randint(MIN_EVENT_DURATION_S, MAX_EVENT_DURATION_S)

        cpu_load = beta_int(CPU_MIN, CPU_MAX)
        latency_ms = beta_int(LATENCY_MIN_MS, LATENCY_MAX_MS)
        jitter_ms = beta_int(JITTER_MIN_MS, JITTER_MAX_MS)

        events.append({
            "run_id": RUN_ID,
            "pod": pod,
            "event_id": event_id,
            "planned_begin_offset_s": round(offset, 3),
            "duration_s": duration,
            "cpu_load": cpu_load,
            "latency_ms": latency_ms,
            "jitter_ms": jitter_ms,
            "actual_begin_ts": "",
            "actual_end_ts": "",
            "no_chaos": 0,
            "status": "planned",
            "error": "",
        })

        # Important:
        # We do NOT add duration here.
        # This allows overlapping events in the same pod.
        offset += random.expovariate(1.0 / MEAN_INTERARRIVAL_S)
        event_id += 1

    return events


# ============================================================
# Chaos Mesh YAML
# ============================================================

def safe_name(e):
    name = f"{RUN_ID}-{e['pod']}-{e['event_id']}"
    name = name.lower().replace("_", "-").replace(".", "-")
    return name[:180]


def chaos_yaml(e):
    name = safe_name(e)
    pod = e["pod"]

    return f"""
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  namespace: {NAMESPACE}
  name: cpu-{name}
spec:
  mode: one
  selector:
    namespaces:
      - {NAMESPACE}
    pods:
      {NAMESPACE}:
        - {pod}
  stressors:
    cpu:
      workers: {CPU_WORKERS}
      load: {e["cpu_load"]}
  duration: '{e["duration_s"]}s'
---
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  namespace: {NAMESPACE}
  name: delay-{name}
spec:
  action: delay
  mode: one
  selector:
    namespaces:
      - {NAMESPACE}
    pods:
      {NAMESPACE}:
        - {pod}
  delay:
    latency: '{e["latency_ms"]}ms'
    jitter: '{e["jitter_ms"]}ms'
    correlation: '50'
  duration: '{e["duration_s"]}s'
"""


# ============================================================
# Event execution
# ============================================================

def apply_event(e, t0):
    yaml_path = None

    try:
        wait_s = e["planned_begin_offset_s"] - (time.time() - t0)

        if wait_s > 0:
            time.sleep(wait_s)

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(chaos_yaml(e))
            yaml_path = f.name

        begin = time.time()
        e["actual_begin_ts"] = f"{begin:.6f}"
        e["status"] = "running"

        print(
            f"[BEGIN] pod={e['pod']} event={e['event_id']} "
            f"cpu={e['cpu_load']} latency={e['latency_ms']}ms "
            f"jitter={e['jitter_ms']}ms duration={e['duration_s']}s"
        )

        run(f"kubectl apply -f {yaml_path}", check=True)

        time.sleep(e["duration_s"])

        run(f"kubectl delete -f {yaml_path} --ignore-not-found=true", check=False)

        end = time.time()
        e["actual_end_ts"] = f"{end:.6f}"
        e["status"] = "completed"

        print(f"[END] pod={e['pod']} event={e['event_id']}")

    except Exception as ex:
        e["status"] = "failed"
        e["error"] = str(ex).replace("\n", " ")[:300]
        print(f"[ERROR] pod={e['pod']} event={e['event_id']} error={e['error']}")

        if yaml_path:
            run(f"kubectl delete -f {yaml_path} --ignore-not-found=true", check=False)

    finally:
        if yaml_path:
            Path(yaml_path).unlink(missing_ok=True)


# ============================================================
# Main
# ============================================================

def main():
    cleanup_previous_chaos()

    pods = get_ue_pods()

    print(f"Run ID: {RUN_ID}")
    print(f"UE pods: {pods}")

    events = []

    for pod in pods:
        pod_events = next_events_for_pod(pod)

        if not pod_events:
            events.append({
                "run_id": RUN_ID,
                "pod": pod,
                "event_id": -1,
                "planned_begin_offset_s": "",
                "duration_s": 0,
                "cpu_load": 0,
                "latency_ms": 0,
                "jitter_ms": 0,
                "actual_begin_ts": "",
                "actual_end_ts": "",
                "no_chaos": 1,
                "status": "no_chaos",
                "error": "",
            })
        else:
            events.extend(pod_events)

    events.sort(
        key=lambda x: float(x["planned_begin_offset_s"])
        if x["planned_begin_offset_s"] != ""
        else float("inf")
    )

    active_events = [e for e in events if e["event_id"] != -1]

    print(f"Generated events: {len(active_events)}")
    print(f"Pods with no chaos: {sum(1 for e in events if e['event_id'] == -1)}")

    t0 = time.time()
    threads = []

    try:
        for e in active_events:
            th = threading.Thread(target=apply_event, args=(e, t0))
            th.start()
            threads.append(th)

        for th in threads:
            th.join()

    except KeyboardInterrupt:
        print("[INTERRUPT] received Ctrl+C, cleaning up chaos")
        cleanup_previous_chaos()
        raise

    fields = [
        "run_id",
        "pod",
        "event_id",
        "planned_begin_offset_s",
        "duration_s",
        "cpu_load",
        "latency_ms",
        "jitter_ms",
        "actual_begin_ts",
        "actual_end_ts",
        "no_chaos",
        "status",
        "error",
    ]

    with OUT.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(events)

    print(f"OK: trace saved to {OUT}")

    cleanup_previous_chaos()


if __name__ == "__main__":
    main()
