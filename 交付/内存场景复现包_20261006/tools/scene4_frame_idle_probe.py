#!/usr/bin/env python3
"""Run the integrated Scene4 frame collector briefly without app launches/reclaim."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from memscenes.device import Device, quote
from memscenes.model import parse_stat
from memscenes.runner import (Session, _scene4_aa_process_inventory,
    _scene4_clock_probe, _scene4_wms_window_inventory, _scene4_wms_window_snapshot,
    parse_low_memory_missions, save)


EXPECTED_EXE = "/data/local/tmp/memshift_validation_20261007/full_chain/memshiftd"
EXPECTED_ARGS = ("/data/local/tmp/memshift_validation_20261007/full_chain/memshiftd "
                 "--config /data/local/tmp/memshift_validation_20261007/full_chain/resident.ini "
                 "--backend harmony-v1-ratio --apply")
EXPECTED_HASHES = {
    EXPECTED_EXE: "cae17800a71ae5e7953050d0fffc8e2588c364c2d3f3257f49eb2abce67d25ab",
    "/data/local/tmp/memshift_validation_20261007/full_chain/resident.ini":
        "7cbc072be80325cacb6149092507a87aaf893d92f920b4f93ab4a340e1655cb5",
    "/data/local/tmp/memshift_validation_20261007/full_chain/resident_control.conf":
        "5062cae48b51ed17573558e265b80bed7905001e6cb2024aee3b03290becb80d",
}
OWNED = [f"io.memscene.scene4.workload.a{i}" for i in range(1, 9)]


def _json_safe_state(state: dict) -> dict:
    """Serialize worker metadata without dropping its path or timing fields."""
    output = {}
    for key, value in state.items():
        if key in ("worker", "stop", "rows", "reviews"):
            continue
        output[key] = str(value) if isinstance(value, Path) else value
    return output


def verify_readonly_preflight(device: Device) -> dict:
    boot_raw = device.shell("cat /proc/sys/kernel/random/boot_id", check=False).strip()
    uptime_raw = device.shell("cat /proc/uptime", check=False).strip()
    meminfo_raw = device.shell("grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo", check=False)
    pids_raw = device.shell("pidof memshiftd", check=False).strip().split()
    result = {"boot_id": boot_raw, "uptime_raw": uptime_raw, "meminfo_raw": meminfo_raw,
              "daemon_pids_raw": pids_raw, "daemon": None, "hashes": {}, "apps": []}
    if not re.fullmatch(r"[0-9a-fA-F-]{32,40}", boot_raw) or len(pids_raw) != 1 or not pids_raw[0].isdigit():
        result["errors"] = ["boot_or_single_daemon_unknown"]
        return result
    pid = int(pids_raw[0])
    raw = device.shell("readlink /proc/" + str(pid) + "/exe; tr '\\0' ' ' < /proc/" +
        str(pid) + "/cmdline; echo; cat /proc/" + str(pid) + "/stat; cat /proc/" +
        str(pid) + "/status", check=False)
    lines = raw.splitlines()
    try:
        exe = lines[0].strip()
        args = lines[1].strip()
        stat = parse_stat(lines[2])
        uid_match = re.search(r"(?m)^Uid:\s*(\d+)", raw)
        uid = int(uid_match.group(1)) if uid_match else None
    except (IndexError, ValueError, TypeError):
        result["errors"] = ["daemon_identity_readback_invalid"]
        result["daemon_raw"] = raw
        return result
    result["daemon"] = {"pid": pid, "start_ticks": stat["start_ticks"],
        "exe": exe, "cmdline": args, "uid": uid}
    if exe != EXPECTED_EXE or args.rstrip() != EXPECTED_ARGS or stat["pid"] != pid or uid != 0:
        result["errors"] = ["original_daemon_identity_or_args_mismatch"]
        return result
    hash_raw = device.shell("sha256sum " + " ".join(map(quote, EXPECTED_HASHES)), check=False)
    result["hash_raw"] = hash_raw
    for line in hash_raw.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+(.+)$", line.strip())
        if match:
            result["hashes"][match.group(2)] = match.group(1).lower()
    if result["hashes"] != EXPECTED_HASHES:
        result["errors"] = ["original_service_hash_mismatch_or_incomplete"]
        return result
    control = device.shell("cat " + quote(
        "/data/local/tmp/memshift_validation_20261007/full_chain/resident_control.conf"),
        check=False)
    result["control_raw"] = control
    if not re.search(r"(?m)^paused=false\s*$", control):
        result["errors"] = ["original_control_not_paused_false_or_unknown"]
        return result
    for bundle in OWNED:
        app = device.app(bundle)
        processes = device.app_processes(bundle)
        result["apps"].append({"bundle": bundle, "uid": app["uid"],
            "cgroup": app["cgroup"], "process_count": len(processes),
            "processes": list(processes.values())})
    if any(row["process_count"] for row in result["apps"]):
        result["errors"] = ["owned_scene4_process_residue"]
        return result
    result["errors"] = []
    result["verified"] = True
    return result


def run_probe(args: argparse.Namespace) -> Path:
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    device = Device(args.hdc, args.device)
    preflight = verify_readonly_preflight(device)
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    save(out / "preflight.json", preflight)
    if not preflight.get("verified"):
        raise RuntimeError("read-only preflight refused probe: " + ",".join(preflight.get("errors", [])))

    session = Session(device, config, 4, "readonly_frame_probe", "candidate")
    session.output = out / "capture"
    session.output.mkdir()
    session.kernel_dmesg_path = session.output / "kernel_dmesg.raw.log"
    session.kernel_dmesg_reader["path"] = str(session.kernel_dmesg_path)
    save(session.output / "config.json", config)
    session.config_path = str(config_path)
    session.result["profile"] = "read_only_integrated_collector_probe"
    session.result["scene4_scope"] = "idle_screen_capture_only_no_launch_no_reclaim"
    session.result["preflight_path"] = str(out / "preflight.json")
    session.result["preflight_verified"] = True
    clock_begin = _scene4_clock_probe(session)
    case_id = "idle_frame_probe"
    mission_raw = device.shell("aa dump -l", timeout=10, check=False)
    wms_raw = device.shell("hidumper -s WindowManagerService -a '-a'", timeout=8, check=False)
    aa_raw = device.shell("aa dump -r", timeout=8, check=False)
    wms = _scene4_wms_window_inventory(wms_raw)
    proc = _scene4_aa_process_inventory(aa_raw)
    taskmanager_pids = {row.get("pid") for row in proc.get("rows", [])
        if row.get("bundle") == "com.huawei.hmos.taskmanager" and row.get("pid") is not None}
    visible_window_ids = []
    detail_evidence = []
    for row in wms.get("rows", []):
        pid, win_id = row.get("pid"), row.get("win_id")
        if pid not in taskmanager_pids or win_id is None:
            continue
        detail = device.shell("hidumper -s WindowManagerService -a " + quote(f"-w {win_id}"),
                              timeout=5, check=False)
        proof = _scene4_wms_window_snapshot(wms_raw, detail, pid, win_id)
        detail_evidence.append({"pid": pid, "win_id": win_id, "raw": detail, "proof": proof})
        if proof.get("known") and proof.get("visible") is True:
            visible_window_ids.append(int(win_id))
    case_begin = float(clock_begin["boottime_s"])
    session.popup_baselines[case_id] = {"mission_ids": sorted(
        row["mission_id"] for row in parse_low_memory_missions(mission_raw)),
        "missions": parse_low_memory_missions(mission_raw),
        "window_ids": sorted(set(visible_window_ids)),
        "window_inventory_complete": bool(wms.get("complete") and proc.get("complete")),
        "window_inventory_raw": wms_raw, "case_begin_boottime_seconds": case_begin,
        "case_begin_wall_ms": int(clock_begin["realtime_s"] * 1000)}
    baseline = {"case_id": case_id, "clock": clock_begin, "missions_raw": mission_raw,
        "wms_raw": wms_raw, "aa_raw": aa_raw, "wms_inventory": wms,
        "aa_inventory": proc, "window_details": detail_evidence,
        "visible_taskmanager_window_ids": visible_window_ids}
    save(session.output / "baseline.json", baseline)

    reader_started = False
    monitor = None
    frame_state = None
    try:
        session.start_kernel_dmesg_reader()
        reader_started = True
        monitor = session.start_scene4_reader_health_monitor(case_id, session.result)
        if not monitor["verified_alive_at_begin"] or monitor["failed"].is_set():
            raise RuntimeError("owned kernel dmesg reader was not alive at probe begin")
        frame_state = session.start_scene4_frame_capture(case_id, case_begin, args.duration)
        time.sleep(args.duration)
        end_sample = _scene4_clock_probe(session)
        session.stop_scene4_frame_capture(case_id)
        collection_end = _scene4_clock_probe(session)["boottime_s"]
        proof = session.finish_scene4_frame_capture(case_id, end_sample["boottime_s"], collection_end)
        reader_health = session.stop_scene4_reader_health_monitor(case_id)
        monitor = None
        session.result["kernel_reader_health"] = reader_health
        session.result["frame_capture_state"] = _json_safe_state(frame_state)
        session.result["frame_proof"] = proof
        session.result["frame_manifest_path"] = proof.get("manifest_path")
        session.result["frame_review_path"] = proof.get("review_path")
        session.result["clock_begin"] = clock_begin
        session.result["clock_end"] = end_sample
        session.result["clock_collection_end_boottime_s"] = collection_end
        session.result["no_ability_requests"] = True
        session.result["no_reclaim_actions"] = True
        session.result["frame_count"] = len(proof.get("frames", []))
        session.result["all_frames_pending_manual_review"] = all(
            row.get("review_classification") == "pending_review"
            for row in proof.get("reviews", []))
        session.result["cleanup_remote_temp"] = proof.get("remote_temp_cleanup")
        session.stop_kernel_dmesg_reader()
        reader_started = False
        cleanup_raw = device.shell("rmdir " + quote(session.remote) +
            " 2>&1; if test -d " + quote(session.remote) +
            "; then echo __REMOTE_DIR_PRESENT__; else echo __REMOTE_DIR_ABSENT__; fi",
            timeout=8, check=False)
        (out / "remote_temp_cleanup.txt").write_text(cleanup_raw, encoding="utf-8")
        session.result["kernel_dmesg_reader"] = session.kernel_dmesg_reader
        session.result["kernel_dmesg_reader_raw_sha256"] = (
            __import__("hashlib").sha256(session.kernel_dmesg_path.read_bytes()).hexdigest()
            if session.kernel_dmesg_path.is_file() else None)
        session.result["remote_temp_cleanup_raw"] = cleanup_raw
        save(session.output / "readonly_probe_result.json", session.result)
        return session.output / "readonly_probe_result.json"
    finally:
        if frame_state:
            session.stop_scene4_frame_capture(case_id)
            worker = frame_state.get("worker")
            if worker:
                worker.join(timeout=40)
        if monitor is not None:
            session.stop_scene4_reader_health_monitor(case_id)
        if reader_started:
            session.stop_kernel_dmesg_reader()
        remote_png = session.remote + f"/scene4_{case_id}_frame.png"
        cleanup_raw = device.shell("rm -f " + quote(remote_png) + "; rmdir " + quote(session.remote) +
            " 2>&1; if test -d " + quote(session.remote) +
            "; then echo __REMOTE_DIR_PRESENT__; else echo __REMOTE_DIR_ABSENT__; fi",
            timeout=8, check=False)
        (out / "remote_temp_cleanup.txt").write_text(cleanup_raw, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hdc", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration", type=float, default=12.0)
    args = parser.parse_args(argv)
    if not 12.0 <= args.duration <= 20.0:
        parser.error("duration must be 12–20 seconds")
    try:
        result = run_probe(args)
    except Exception as exc:
        print(f"read-only frame probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
