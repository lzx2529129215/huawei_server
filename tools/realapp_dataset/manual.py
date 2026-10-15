"""Supervised real-app trial with user-owned tmpfs files; never manages their lifetime."""
import argparse
import json
import time
from pathlib import Path

from .core import load, save, memory, rows, segment_states
from .device_ops import Target, Session
from .pipeline import (preflight, build_sampler, start_sampler, service_logs,
                       service_interventions, wait_stage, retrieve, event_summary, additional_events)
from .checkpoints import Checkpoints


def run(dev, suite, out, binary):
    if out.exists():
        raise FileExistsError(out)
    out.mkdir(parents=True)
    pf = preflight(dev, suite, out / "preflight")
    # Known large unlink incident prohibits managed fills, not read-only reuse.
    if pf["blockers"]:
        save(out / "result.json", {"status": "blocked", "reasons": pf["blockers"]})
        return
    session = Session(dev, out, "apply", manage_fill=False)
    checkpoints = Checkpoints(session)
    result = {"profile": "supervised_realapp_manual_occupancy", "phase": "apply",
              "valid_for_training": False, "completed": False,
              "reason": "exploratory sequence not frozen or paired",
              "occupancy_owner": "user", "reference_gib": 8}
    external = "stat -c '%n %s %b %i' /dev/shm/tmp/fill*"
    try:
        save(out / "external_occupancy_before.json", {"stat": dev.checked(external), "meminfo": dev.read("/proc/meminfo")})
        session.prepare()
        start_sampler(session, binary, pf["apps"])
        checkpoints.start()
        service_logs(session, "before")
        print("baseline started (60 seconds)", flush=True)
        wait_stage(session, 60, "baseline")
        # Read the complete baseline, not only its endpoint.
        samples = [json.loads(s) for s in dev.read(session.remote + "/system.jsonl").splitlines() if s.strip()]
        baseline = [s for s in samples if s.get("stage") == "baseline"]
        if not baseline or baseline[-1]["time"] - baseline[0]["time"] < 58:
            raise RuntimeError("incomplete baseline")
        if any(memory(s["meminfo"]).get("MemAvailable", 0) < .8 * 1024**3 for s in baseline):
            raise RuntimeError("baseline already below reference threshold")
        result["actions_begin"] = dev.uptime()
        dev.text(session.remote + "/stage", "actions")
        save(out / "ready.json", {"remote": session.remote, "actions_begin": result["actions_begin"]})
        print("ready for supervised real application actions", flush=True)
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            if (out / "finish.request").exists():
                break
            session.ensure_active()
            time.sleep(3)
        result["actions_end"] = dev.uptime()
        wait_stage(session, 60, "post")
        result["completed"] = True
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if session.started:
            try:
                service_logs(session, "after")
                save(out / "external_occupancy_after.json", {"stat": dev.checked(external), "meminfo": dev.read("/proc/meminfo")})
            except Exception as exc:
                result["final_snapshot_error"] = str(exc)
        result["restore"] = session.restore()
        result["checkpoints_stopped"] = checkpoints.finish()
        if session.started:
            time.sleep(1)
            retrieve(session)
            try:
                events = additional_events(session, event_summary(out))
                result["confirmed_oom"] = sum(e["category"] == "oom_kill" for e in events["events"])
                result["oom_coverage_complete"] = events["oom_label_coverage_complete"]
                save(out / "gate_segments.json", segment_states(rows(out / "system.jsonl")))
                service_interventions(out, "apply")
            except Exception as exc:
                result["evidence_error"] = str(exc)
        save(out / "result.json", result)
        print(result, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args()
    d = Target(r"D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe", "3QC0124C03000514")
    run(d, load(Path("config/realapp_dataset/suite.json")), a.output, build_sampler(Path("build/realapp_dataset/sampler")))
