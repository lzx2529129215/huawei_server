"""Prepare and verify the three real pressure HAPs at their daily baseline."""
from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FROZEN = ROOT / "results" / "daily_lead90_20261012_1640" / "candidate_runner"
if str(FROZEN) not in sys.path:
    sys.path.insert(0, str(FROZEN))
from memscenes.device import Device, quote
from memscenes.model import parse_stat

# Do not import by the generic ``native_plan`` name: the frozen runner is first
# on sys.path and has an unrelated 16-target module with that same filename.
BUNDLES = tuple(f"io.memscene.scene4.workload.a{i}" for i in (14, 15, 16))


def _uid_from_status(device: Device, pid: int) -> int:
    raw = device.shell(f"cat /proc/{pid}/status", check=False, timeout=10)
    match = re.search(r"(?m)^Uid:\s+(\d+)", raw)
    if not match:
        raise RuntimeError(f"cannot read real UID for target member pid={pid}")
    return int(match.group(1))


def _prepare_one(device: Device, bundle: str, case_id: str,
                 output: Path, profile: dict) -> dict:
    spec = device.app(bundle)
    uid = int(spec["uid"])
    device.force_stop(bundle)
    try:
        device.remove_owned_case_pages(bundle, case_id)
    except RuntimeError as exc:
        # A fresh unique case should have no old page file. Refuse if it does.
        if "not verified" in str(exc):
            raise
    load = profile["load"]
    daily_id = case_id + "_daily"
    params = {"case_id": case_id, "daily_mib": int(load["target_daily_mib"]),
        "peak_mib": int(load["target_peak_mib"]), "file_mib": int(load["target_file_mib"]),
        "hold_seconds": int(load["hold_seconds"]), "lifetime_seconds": 1200, "seed": 1,
        "bundle": bundle, "daily_case_id": daily_id, "await_peak": 1}
    launch = device.launch(bundle, params=params, timeout=60)
    deadline = time.monotonic() + 240
    raw = ""
    rows = []
    ready = None
    while time.monotonic() < deadline:
        raw, rows = device.journal(bundle, daily_id)
        ready = next((row for row in reversed(rows) if row.get("event") == "daily_ready"), None)
        failed = [row for row in rows if row.get("event") in ("allocation_failed", "file_sync_failed")]
        if failed:
            raise RuntimeError(f"daily preparation failed for {bundle}: {failed[-1]}")
        if ready and any(row.get("event") == "file_sync_complete" for row in rows):
            break
        time.sleep(0.5)
    if not ready:
        raise TimeoutError(f"daily_ready not observed for {bundle}")
    if not any(row.get("event") == "file_sync_complete" for row in rows):
        raise TimeoutError(f"file_sync_complete not observed for {bundle}")
    expected_daily = int(load["target_daily_mib"]) * 1024**2
    expected_file = int(load["target_file_mib"]) * 1024**2
    ready_bytes = int(ready.get("ready_bytes", 0))
    synced = max((int(row.get("file_synced_bytes", 0)) for row in rows), default=0)
    pid, row_uid, ticks = (int(ready.get(key, 0)) for key in ("pid", "uid", "start_ticks"))
    if ready_bytes != expected_daily or synced < expected_file or row_uid != uid or min(pid, ticks) <= 0:
        raise RuntimeError(f"daily readiness amounts/identity mismatch for {bundle}: {ready}")
    inspected = device.inspect_process_identity(pid, timeout=15)
    identity = inspected.get("identity") or {}
    if (int(identity.get("pid", -1)) != pid or int(identity.get("uid", -1)) != uid or
            str(identity.get("start_ticks", "")) != str(ticks)):
        raise RuntimeError(f"daily_ready PID/start/UID does not match live process for {bundle}")
    members = device.app_processes(bundle)
    member_rows = []
    for key, proc in members.items():
        member_pid = int(proc["pid"])
        member_uid = _uid_from_status(device, member_pid)
        member_rows.append({"pid": member_pid, "start_ticks": str(proc["start_ticks"]), "uid": member_uid})
    if not any(row["pid"] == pid and row["start_ticks"] == str(ticks) and row["uid"] == uid
               for row in member_rows):
        raise RuntimeError(f"daily_ready process is not in its expected memcg: {bundle}")
    raw_path = output / f"{bundle.rsplit('.', 1)[-1]}_daily_journal.jsonl"
    raw_path.write_text(raw, encoding="utf-8")
    return {"bundle": bundle, "uid": uid, "pid": pid, "start_ticks": ticks,
        "ready_bytes": ready_bytes, "file_synced_bytes": synced,
        "expected_daily_bytes": expected_daily, "expected_file_bytes": expected_file,
        "journal_path": str(raw_path.resolve()), "journal_events": len(rows),
        "memcg_members": member_rows, "launch_output": launch,
        "identity": identity, "ready_event": ready}


def prepare_targets(device: Device, case_id: str, output: Path, profile: dict) -> dict:
    if not re.fullmatch(r"s4_[0-9a-f]{12}_c0001", case_id):
        raise ValueError("target preparation case id is malformed")
    output.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    failures: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        future_map = {pool.submit(_prepare_one, device, bundle, case_id, output, profile): bundle
                      for bundle in BUNDLES}
        for future, bundle in future_map.items():
            try:
                results[bundle] = future.result()
            except Exception as exc:
                failures[bundle] = f"{type(exc).__name__}: {exc}"
    if failures or set(results) != set(BUNDLES):
        for bundle in BUNDLES:
            device.force_stop(bundle)
        raise RuntimeError("target daily preparation incomplete: " + json.dumps(failures or results))
    proof = {"schema": "browser_trial_three_target_daily_ready_v1", "case_id": case_id,
        "targets": results, "failure_count": 0,
        "device_clock_domain": "CLOCK_BOOTTIME", "validated": True}
    (output / "target_daily_ready.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n",
                                                       encoding="utf-8")
    return proof


def cleanup_targets(device: Device, proof: dict, case_id: str,
                    owned_case_ids: dict[str, list[str]] | None = None) -> dict:
    outcomes = {}
    for bundle, expected in proof.get("targets", {}).items():
        try:
            members = device.app_processes(bundle)
            cases = (owned_case_ids or {}).get(bundle, [case_id + "_daily", case_id])
            cases = sorted(set(cases) | {case_id + "_daily", case_id})
            journal_identities = set()
            journal_cases = {}
            for owned_case in cases:
                _, rows = device.journal(bundle, owned_case)
                accepted = [row for row in rows if row.get("event") in
                            ("startup_begin", "daily_ready", "peak_begin", "peak_reached", "startup_ready")]
                journal_cases[owned_case] = len(accepted)
                for row in accepted:
                    try:
                        row_pid, row_uid = int(row.get("pid", -1)), int(row.get("uid", -1))
                    except (TypeError, ValueError):
                        continue
                    if row_pid > 0 and row_uid == int(expected["uid"]):
                        journal_identities.add((row_pid, row_uid))
            live_members = {(int(row["pid"]), _uid_from_status(device, int(row["pid"])),
                             str(row["start_ticks"])) for row in members.values()}
            exact = any(pid == int(expected["pid"]) and ticks == str(expected["start_ticks"])
                        and uid == int(expected["uid"]) for pid, uid, ticks in live_members)
            owned_live = sorted({pid for pid, uid, _ in live_members if (pid, uid) in journal_identities})
            # A retry can have a new generation. Only force-stop this test HAP when
            # its live cgroup still contains a PID recorded by one of this run's
            # exact daily/peak case journals for the same package UID.
            if exact or owned_live:
                device.force_stop(bundle)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and device.app_processes(bundle):
                    time.sleep(0.5)
            remaining = device.app_processes(bundle)
            if not remaining:
                page = device.remove_owned_case_pages(bundle, case_id)
                for owned_case in cases:
                    if owned_case != case_id:
                        try:
                            page = device.remove_owned_case_pages(bundle, owned_case)
                        except Exception:
                            pass
                outcomes[bundle] = {"verified_absent": True, "page_cleanup": page,
                    "owned_case_ids": cases, "journal_event_counts": journal_cases,
                    "verified_live_owned_pids": owned_live,
                    "daily_generation_seen": exact}
            else:
                outcomes[bundle] = {"verified_absent": False, "remaining": list(remaining.values()),
                    "exact_expected_generation_seen": exact, "verified_live_owned_pids": owned_live,
                    "owned_case_ids": cases, "journal_event_counts": journal_cases,
                    "refused_to_force_stop_without_owned_case_identity": not (exact or owned_live)}
        except Exception as exc:
            outcomes[bundle] = {"verified_absent": False, "error": f"{type(exc).__name__}: {exc}"}
    return outcomes

