from __future__ import annotations
import concurrent.futures
import csv
import hashlib
import json
import math
import os
import re
import secrets
import statistics
import subprocess
import threading
import queue
import time
import shutil
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .device import Device, quote, ui_nodes, focused_bundles
from .kernel_capture import DeviceDmesgReader
from .scene4_roles import memory_roles
from .model import (ORDER, GIB, EventBook, canonical_hash, key_values,
                    fault_delta, disk_bytes, eswap_bytes, json_lines, combination_checks,
                    release_precedes_reuse, daily_promotion_targets, attempt_outcome,
                    attempts_complete, proc_identity_mismatch, parse_stat)

ROOT = Path(__file__).resolve().parents[1]
NATIVE_LOW_MEMORY_OWNERS = {"com.huawei.hmos.taskmanager"}
SCENE4_DONOR_DYNAMIC_SUFFIXES = [
    "aa_pid", "aa_uid", "aa_state", "aa_observed_boottime_s",
    "window_visible", "window_focused", "window_observed_boottime_s",
    "proc_verified", "proc_start_ticks", "proc_observed_boottime_s",
    "lifecycle_clear", "lifecycle_observed_boottime_s", "background_evidence_source"]
SCENE4_DONOR_STATIC_SUFFIXES = [
    "bundle", "uid", "pid", "start_ticks", "cold_ready", "window_ready",
    "cold_ready_boottime_s", "background_since_s", "last_heartbeat_s"]

SCENE4_V3_DONOR_SUFFIXES = [
    "bundle", "uid", "pid", "start_ticks", "daily_ready", "window_ready",
    "daily_ready_boottime_s", "background_since_s", "aa_pid", "aa_uid",
    "aa_state", "aa_observed_boottime_s", "window_visible", "window_focused",
    "window_observed_boottime_s", "proc_verified", "proc_start_ticks",
    "proc_observed_boottime_s", "lifecycle_clear", "lifecycle_observed_boottime_s",
    "background_evidence_source", "writeback_file_path", "writeback_file_dev",
    "writeback_file_ino", "writeback_file_size"]
SCENE4_V2_DONOR_SUFFIXES = SCENE4_V3_DONOR_SUFFIXES[:21]


def _scene4_append_jsonl(path: Path, row: dict) -> None:
    """Durably append one independently timestamped Scene4 evidence row."""
    with Path(path).open("a", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()


def _scene4_frame_remote_path(remote_root: str, session_id: str, case_id: str,
                              slot_index: int) -> str:
    if isinstance(slot_index, bool) or not isinstance(slot_index, int) or slot_index < 0:
        raise ValueError("Scene4 frame slot must be a nonnegative integer")
    safe_session = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id))
    safe_case = re.sub(r"[^A-Za-z0-9_-]", "_", str(case_id))
    return f"{remote_root.rstrip('/')}/scene4_{safe_session}_{safe_case}_frame_{slot_index:06d}.png"


def _scene4_observation_boottime_interval(clock_sample: dict, clock_host_start_ns: int,
                                          clock_host_end_ns: int, observation_host_start_ns: int,
                                          observation_host_end_ns: int) -> dict:
    """Conservatively map a host-observed proc read bracket into BOOTTIME."""
    values = (clock_sample.get("boottime_s"), clock_host_start_ns, clock_host_end_ns,
              observation_host_start_ns, observation_host_end_ns)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or
           not math.isfinite(float(value)) for value in values):
        return {"valid": False, "boottime_start_s": None, "boottime_end_s": None}
    if clock_host_end_ns < clock_host_start_ns or observation_host_end_ns < observation_host_start_ns:
        return {"valid": False, "boottime_start_s": None, "boottime_end_s": None}
    boot = float(clock_sample["boottime_s"])
    offset_low = boot - float(clock_host_end_ns) / 1e9
    offset_high = boot - float(clock_host_start_ns) / 1e9
    start = float(observation_host_start_ns) / 1e9 + offset_low
    end = float(observation_host_end_ns) / 1e9 + offset_high
    return {"valid": math.isfinite(start) and math.isfinite(end) and start <= end,
            "boottime_start_s": start, "boottime_end_s": end,
            "clock_offset_lower_s": offset_low, "clock_offset_upper_s": offset_high,
            "host_start_ns": observation_host_start_ns, "host_end_ns": observation_host_end_ns}


def _scene4_schedule_header() -> list[str]:
    fields = ["event_id", "case_id", "launch_boottime_s", "planned_needed_bytes"]
    for prefix in ("donor_a", "donor_b"):
        fields.extend(f"{prefix}_{name}" for name in
                      SCENE4_DONOR_STATIC_SUFFIXES + SCENE4_DONOR_DYNAMIC_SUFFIXES)
    return fields


def _scene4_schedule_v3_header() -> list[str]:
    fields = ["schema_version", "event_id", "case_id", "launch_boottime_s",
              "planned_needed_bytes", "donor_count"]
    for index in range(1, 9):
        fields.extend(f"donor_{index}_{name}" for name in SCENE4_V3_DONOR_SUFFIXES)
    return fields


def _scene4_schedule_v2_header() -> list[str]:
    fields = ["schema_version", "event_id", "case_id", "launch_boottime_s",
              "planned_needed_bytes", "donor_count"]
    for index in range(1, 9):
        fields.extend(f"donor_{index}_{name}" for name in SCENE4_V2_DONOR_SUFFIXES)
    return fields


def _scene4_schedule_v2_row(event_id: str, case_id: str, launch_boottime_s: float,
                            planned_needed_bytes: int, donors: list[dict]) -> list[str]:
    """Build the 1–8 donor daily-ready OS-snapshot schema used by force_shrink_all."""
    if not event_id or not case_id or not math.isfinite(float(launch_boottime_s)) or launch_boottime_s <= 0:
        raise ValueError("schema2 event, case, and positive finite BOOTTIME deadline are required")
    if isinstance(planned_needed_bytes, bool) or int(planned_needed_bytes) <= 0:
        raise ValueError("schema2 planned_needed_bytes must be positive")
    if not 1 <= len(donors) <= 8:
        raise ValueError("schema2 requires one through eight verified donor records")
    row = ["2", str(event_id), str(case_id), format(float(launch_boottime_s), ".17g"),
           str(int(planned_needed_bytes)), str(len(donors))]
    for donor in donors:
        missing = [name for name in SCENE4_V2_DONOR_SUFFIXES if name not in donor]
        if missing:
            raise ValueError(f"schema2 donor is missing required fields: {','.join(missing)}")
        if any(type(donor[key]) is not bool for key in
               ("daily_ready", "window_ready", "window_visible", "window_focused",
                "proc_verified", "lifecycle_clear")):
            raise ValueError("schema2 readiness and observation flags must be explicit booleans")
        if donor["aa_state"] != "BACKGROUND" or donor["background_evidence_source"] != "os_snapshot":
            raise ValueError("schema2 donor must have fresh background OS-snapshot evidence")
        if str(donor["pid"]) != str(donor["aa_pid"]) or str(donor["uid"]) != str(donor["aa_uid"]) or \
                str(donor["start_ticks"]) != str(donor["proc_start_ticks"]):
            raise ValueError("schema2 dynamic identity does not match frozen PID/UID/start_ticks")
        if donor["daily_ready"] is not True or donor["window_ready"] is not True or \
                donor["window_visible"] is not False or donor["window_focused"] is not False or \
                donor["proc_verified"] is not True or donor["lifecycle_clear"] is not True:
            raise ValueError("schema2 donor readiness, visibility, or identity proof is incomplete")
        for key in ("uid", "pid", "start_ticks", "aa_pid", "aa_uid", "proc_start_ticks"):
            if not str(donor[key]).isdecimal() or int(donor[key]) <= 0:
                raise ValueError(f"schema2 donor field {key} must be a positive decimal integer")
        for key in ("daily_ready_boottime_s", "background_since_s", "aa_observed_boottime_s",
                    "window_observed_boottime_s", "proc_observed_boottime_s",
                    "lifecycle_observed_boottime_s"):
            value = float(donor[key])
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"schema2 donor field {key} must be positive finite BOOTTIME")
        if float(donor["background_since_s"]) < float(donor["daily_ready_boottime_s"]):
            raise ValueError("schema2 background proof predates daily-ready")
        row.extend("true" if donor[name] is True else "false" if donor[name] is False
                   else str(donor[name]) for name in SCENE4_V2_DONOR_SUFFIXES)
    expected_width = 6 + len(donors) * len(SCENE4_V2_DONOR_SUFFIXES)
    if len(row) != expected_width:
        raise AssertionError("schema2 schedule row/donor width mismatch")
    return row


def _scene4_schedule_v3_row(event_id: str, case_id: str, launch_boottime_s: float,
                            planned_needed_bytes: int, donors: list[dict]) -> list[str]:
    """Build the explicit 1–8 donor OS-snapshot/writeback schema without legacy fields."""
    if not event_id or not case_id or not math.isfinite(float(launch_boottime_s)) or launch_boottime_s <= 0:
        raise ValueError("schema3 event, case, and positive finite BOOTTIME deadline are required")
    if isinstance(planned_needed_bytes, bool) or int(planned_needed_bytes) <= 0:
        raise ValueError("schema3 planned_needed_bytes must be positive")
    if not 1 <= len(donors) <= 8:
        raise ValueError("schema3 requires one through eight verified donor records")
    row = ["3", str(event_id), str(case_id), format(float(launch_boottime_s), ".17g"),
           str(int(planned_needed_bytes)), str(len(donors))]
    for donor in donors:
        missing = [name for name in SCENE4_V3_DONOR_SUFFIXES if name not in donor]
        if missing:
            raise ValueError(f"schema3 donor is missing required fields: {','.join(missing)}")
        if any(type(donor[key]) is not bool for key in
               ("daily_ready", "window_ready", "window_visible", "window_focused",
                "proc_verified", "lifecycle_clear")):
            raise ValueError("schema3 readiness and observation flags must be explicit booleans")
        if donor["aa_state"] not in ("BACKGROUND", "FOREGROUND") or \
                donor["background_evidence_source"] != "os_snapshot":
            raise ValueError("schema3 donor state/source is invalid")
        if str(donor["pid"]) != str(donor["aa_pid"]) or str(donor["uid"]) != str(donor["aa_uid"]) or \
                str(donor["start_ticks"]) != str(donor["proc_start_ticks"]):
            raise ValueError("schema3 dynamic identity does not match frozen PID/UID/start_ticks")
        if donor["writeback_file_path"] != f"memscene_{case_id}.pages":
            raise ValueError("schema3 file path must be the owned case backing file basename")
        for key in ("uid", "pid", "start_ticks", "aa_pid", "aa_uid", "proc_start_ticks",
                    "writeback_file_dev", "writeback_file_ino", "writeback_file_size"):
            if not str(donor[key]).isdecimal() or int(donor[key]) <= 0:
                raise ValueError(f"schema3 donor field {key} must be a positive decimal integer")
        for key in ("daily_ready_boottime_s", "background_since_s", "aa_observed_boottime_s",
                    "window_observed_boottime_s", "proc_observed_boottime_s",
                    "lifecycle_observed_boottime_s"):
            value = float(donor[key])
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"schema3 donor field {key} must be positive finite BOOTTIME")
        row.extend("true" if donor[name] is True else "false" if donor[name] is False
                   else str(donor[name]) for name in SCENE4_V3_DONOR_SUFFIXES)
    expected_width = 6 + len(donors) * len(SCENE4_V3_DONOR_SUFFIXES)
    if len(row) != expected_width:
        raise AssertionError("schema3 schedule row/donor width mismatch")
    return row


def _scene4_legacy_os_snapshot(session: "Session", case_id: str,
                               daily_records: dict[str, list[dict]],
                               bundles: list[str]) -> dict:
    """Read one batched AA/WMS/proc snapshot for the legacy eight-app schedule3 profile."""
    d = session.device
    ready = {}
    for bundle in bundles:
        row = next((item for item in reversed(daily_records.get(bundle, []))
                    if item.get("event") == "daily_ready"), None)
        if row is None:
            continue
        try:
            pid, uid, ticks = int(row["pid"]), int(row["uid"]), str(row["start_ticks"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid > 0 and uid > 0 and ticks.isdecimal() and int(ticks) > 0:
            ready[bundle] = {"bundle": bundle, "uid": uid, "pid": pid,
                "start_ticks": ticks, "daily_ready_boottime_s": _scene4_event_time(row)}
    pre_marker, aa_marker, wms_marker, end_marker = (
        "__S3_BOOT_PRE__", "__S3_AA__", "__S3_WMS__", "__S3_BOOT_POST__")
    script = (f"echo {pre_marker}; cat /proc/uptime; echo {aa_marker}; aa dump -r; "
              f"echo {wms_marker}; hidumper -s WindowManagerService -a '-a'; ")
    for index, donor in enumerate(ready.values()):
        pid, bundle, uid = donor["pid"], donor["bundle"], donor["uid"]
        leaf = f"/dev/memcg/100/{bundle}_{uid}"
        page_file = f"/proc/{pid}/root/data/storage/el2/base/haps/entry/files/memscene_{case_id}.pages"
        script += (f"echo __S3_PROC_{index}_BEGIN__; cat /proc/{pid}/stat 2>&1; "
            f"echo __S3_STATUS_{index}__; cat /proc/{pid}/status 2>&1; "
            f"echo __S3_STAT2_{index}__; cat /proc/{pid}/stat 2>&1; "
            f"echo __S3_CMD_{index}__; tr '\\000' ' ' < /proc/{pid}/cmdline 2>/dev/null; echo; "
            f"echo __S3_CGROUP_{index}__; cat /proc/{pid}/cgroup 2>&1; "
            f"echo __S3_MEMBERS_{index}__; cat {quote(leaf + '/cgroup.procs')} 2>&1; "
            f"echo __S3_FILE_{index}__; stat -c '%d|%i|%s' {quote(page_file)} 2>&1; "
            f"echo __S3_PROC_{index}_END__; ")
    script += f"echo {end_marker}; cat /proc/uptime"
    raw = d.shell(script, timeout=12, check=False)
    def section(begin: str, end: str) -> str:
        match = re.search(rf"(?ms)^{re.escape(begin)}\s*\n(.*?)(?=^{re.escape(end)}\s*$)", raw)
        return match.group(1).strip() if match else ""
    try:
        boot_pre = float(section(pre_marker, aa_marker).split()[0])
        aa_raw = section(aa_marker, wms_marker)
        remainder = raw.split(wms_marker + "\n", 1)[1]
        wms_raw = remainder.split("__S3_PROC_0_BEGIN__", 1)[0].strip()
        boot_post = float(raw.rsplit(end_marker + "\n", 1)[1].split()[0])
    except (ValueError, IndexError):
        return {"complete": False, "reason": "os_snapshot_clock_or_markers_missing", "raw": raw}
    table = _scene4_wms_window_inventory(wms_raw)
    proc_rows = {}
    for index, donor in enumerate(ready.values()):
        pid, bundle, uid = donor["pid"], donor["bundle"], donor["uid"]
        block = section(f"__S3_PROC_{index}_BEGIN__", f"__S3_PROC_{index}_END__")
        labels = [f"__S3_STATUS_{index}__", f"__S3_STAT2_{index}__",
                  f"__S3_CMD_{index}__", f"__S3_CGROUP_{index}__",
                  f"__S3_MEMBERS_{index}__", f"__S3_FILE_{index}__"]
        label_pattern = "|".join(map(re.escape, labels))
        def psec(label: str) -> str:
            match = re.search(rf"(?ms)^{re.escape(label)}\s*\n(.*?)(?=^(?:{label_pattern})\s*$|\Z)", block)
            return match.group(1).strip() if match else ""
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        stat1 = next((line for line in lines if re.match(rf"^{pid} \(", line)), "")
        stat2 = psec(f"__S3_STAT2_{index}__")
        status = psec(f"__S3_STATUS_{index}__")
        cmdline = psec(f"__S3_CMD_{index}__")
        cgroup = psec(f"__S3_CGROUP_{index}__")
        members = psec(f"__S3_MEMBERS_{index}__").split()
        file_fields = psec(f"__S3_FILE_{index}__").split("|")
        try:
            before, after = parse_stat(stat1), parse_stat(stat2)
            uid_line = re.search(r"(?m)^Uid:\s+(\d+)", status)
            actual_uid = int(uid_line.group(1)) if uid_line else None
            identity_ok = (before.get("pid") == after.get("pid") == pid and
                str(before.get("start_ticks")) == str(after.get("start_ticks")) == donor["start_ticks"] and
                actual_uid == uid and pid in [int(x) for x in members if x.isdigit()] and
                f"{bundle}_{uid}" in cgroup and bundle in cmdline)
            if len(file_fields) != 3 or not all(value.isdecimal() and int(value) > 0 for value in file_fields):
                raise ValueError("backing_file_stat_missing_or_invalid")
            aa_state = _scene4_aa_running_record_state(aa_raw, bundle, pid, uid)
            matching_windows = [row for row in table.get("rows", []) if row["pid"] == pid]
            proc_rows[bundle] = {**donor, "identity_verified": identity_ok,
                "aa_state": aa_state, "window_rows": matching_windows,
                "file_dev": int(file_fields[0]), "file_ino": int(file_fields[1]),
                "file_size": int(file_fields[2]), "proc_observed_boottime_s": boot_post,
                "aa_observed_boottime_s": boot_pre, "cgroup_raw": cgroup,
                "process_cmdline": cmdline}
        except (ValueError, TypeError) as exc:
            proc_rows[bundle] = {**donor, "identity_verified": False,
                "error": str(exc), "aa_state": None, "window_rows": [],
                "proc_observed_boottime_s": boot_post, "aa_observed_boottime_s": boot_pre}
    all_windows = [row for value in proc_rows.values() for row in value.get("window_rows", [])]
    details = _scene4_wms_details_batch(session, wms_raw, all_windows) if all_windows else []
    detail_by_pid: dict[int, list[dict]] = {}
    for item in details:
        detail_by_pid.setdefault(int(item.get("pid") or 0), []).append(item)
    for bundle, value in proc_rows.items():
        pid = int(value["pid"])
        owned_details = detail_by_pid.get(pid, [])
        known = bool(value.get("window_rows")) and len(owned_details) == len(value.get("window_rows", [])) and all(
            row.get("parsed", {}).get("known") is True and row.get("bracket_valid") is True
            for row in owned_details)
        value["window_details"] = owned_details
        value["window_ready"] = known
        value["window_visible"] = any(row.get("parsed", {}).get("visible") is True for row in owned_details) if known else None
        value["window_focused"] = any(row.get("parsed", {}).get("focused") is True for row in owned_details) if known else None
        value["window_observed_boottime_s"] = max((row.get("boottime_post_s") or 0 for row in owned_details), default=0) or None
        value["lifecycle_clear"] = bool(value.get("identity_verified") and value.get("aa_state") in ("BACKGROUND", "FOREGROUND") and known)
        value["lifecycle_observed_boottime_s"] = value.get("window_observed_boottime_s")
    return {"complete": bool(proc_rows) and all(row.get("identity_verified") for row in proc_rows.values()),
        "boot_pre_s": boot_pre, "boot_post_s": boot_post, "aa_raw": aa_raw,
        "wms_table": table, "wms_raw": wms_raw, "donors": proc_rows,
        "proc_raw": raw, "details_raw": [row.get("raw_batch") for row in details]}


def _scene4_schedule_publish_decision(stop_requested: bool, now_boottime_s: float,
                                      deadline_boottime_s: float,
                                      publish_reserve_seconds: float = 0.75) -> dict:
    """Deny publication within a measured safety margin of fixed T."""
    try:
        now_value, deadline_value = float(now_boottime_s), float(deadline_boottime_s)
        reserve_value = float(publish_reserve_seconds)
    except (TypeError, ValueError):
        return {"allowed": False, "reason": "clock_unknown"}
    if stop_requested:
        return {"allowed": False, "reason": "stop_requested"}
    if not math.isfinite(now_value) or not math.isfinite(deadline_value):
        return {"allowed": False, "reason": "clock_unknown"}
    if not math.isfinite(reserve_value) or reserve_value < 0:
        return {"allowed": False, "reason": "publish_reserve_invalid"}
    remaining = deadline_value - now_value
    if remaining <= 0:
        return {"allowed": False, "reason": "fixed_deadline_reached"}
    if remaining < reserve_value:
        return {"allowed": False, "reason": "publication_reserve_window_reached",
            "remaining_seconds": remaining, "publish_reserve_seconds": reserve_value}
    return {"allowed": True, "reason": None,
        "remaining_seconds": remaining, "publish_reserve_seconds": reserve_value}


def _scene4_profile_cutoff_status(snapshot_inflight: bool, writer_alive: bool,
                                  publication_rows: list[dict]) -> dict:
    """Only a publication that actually completed after T creates a schedule gap."""
    late = [row for row in publication_rows if row.get("crossed_deadline") is True]
    return {"snapshot_inflight_at_stop": bool(snapshot_inflight),
        "writer_alive_when_checked": bool(writer_alive),
        "late_publications": late,
        "measurement_gap": bool(late)}


def _scene4_legacy_v3_window(session: "Session", case_id: str,
                             bundles: list[str], daily_records: dict[str, list[dict]],
                             window_seconds: float, peak_mib: int, daily_mib: int, *,
                             window_start_host_monotonic: float | None = None,
                             window_start_boottime_s: float | None = None) -> dict:
    """Register one fixed legacy eight-app event and refresh only OS evidence until peak T."""
    cfg = session.config["scene4"]
    backend = cfg.get("oracle_backend")
    if cfg.get("execution_profile") != "legacy_peak_transition" or \
            cfg.get("oracle_profile") != "peak_transition_os_snapshot" or \
            backend not in {"proc_file_writeback", "force_shrink_all", "force_swapout"}:
        raise ValueError("legacy peak-transition route requires an explicit supported OS-snapshot backend")
    schema_version = 3 if backend == "proc_file_writeback" else 2
    if backend == "force_shrink_all" and cfg.get("oracle_ratio_semantics") != "cumulative_anon_watermark":
        raise ValueError("schema2 force_shrink_all route requires cumulative_anon_watermark semantics")
    if backend == "force_swapout" and cfg.get("oracle_ratio_semantics") != "resident_incremental":
        raise ValueError("force_swapout is a whole-pool command and cannot use cumulative ratio semantics")
    schedule_remote = str(cfg.get("oracle_schedule_remote_path", "")).strip()
    ledger_remote = str(cfg.get("oracle_ledger_remote_path", "")).strip()
    if not schedule_remote or not ledger_remote:
        raise ValueError("legacy peak-transition route requires bound schedule and ledger paths")
    host_window_started = (time.monotonic() if window_start_host_monotonic is None
                           else float(window_start_host_monotonic))
    d = session.device
    window_start_boottime = (float(d.shell("cat /proc/uptime").split()[0])
        if window_start_boottime_s is None else float(window_start_boottime_s))
    fixed_deadline = window_start_boottime + float(window_seconds)
    host_deadline = host_window_started + float(window_seconds)
    control = _scene4_control_readback(session, schedule_remote)
    if not control.get("verified"):
        raise RuntimeError("legacy peak-transition service control readback did not match requested A/B phase")
    roles = memory_roles(cfg, bundles, peak_mib, int(cfg.get("daily_file_mib", 0)))
    candidate_bundles = roles["reclaim_candidates"]
    exact_needed = roles["incremental_bytes"]
    needed = int(cfg.get("planned_needed_bytes", exact_needed))
    if needed != exact_needed or needed <= 0:
        raise ValueError("legacy profile planned bytes must equal registered peak-target incremental demand")
    event_id = f"{case_id}_peak_transition"
    suffix = f"v{schema_version}"
    local_path = session.output / f"oracle_schedule_{suffix}.tsv"
    observation_path = session.output / f"oracle_schedule_{suffix}_observations.jsonl"
    remote_parent = schedule_remote.rsplit("/", 1)[0] if "/" in schedule_remote else "."
    d.shell("mkdir -p " + quote(remote_parent), check=False)
    prior = d.shell("cat " + quote(schedule_remote), check=False)
    if prior.strip():
        prior_local = session.output / f"oracle_schedule_{suffix}_previous.tsv"
        prior_local.write_text(prior.rstrip("\n") + "\n", encoding="utf-8")
        d.call("file", "send", str(prior_local), schedule_remote + ".previous", timeout=45)
    ledger_state = d.shell("if test -e " + quote(ledger_remote) + "; then echo present; else echo absent; fi",
                           timeout=8, check=False).strip()
    if ledger_state == "present":
        existing_ledger = d.shell("cat " + quote(ledger_remote), timeout=10, check=False)
        parsed_ledger = list(csv.reader(existing_ledger.splitlines()))
        if not parsed_ledger or parsed_ledger[0][:4] != ["action_id", "event_id", "case_id", "ledger_status"]:
            raise RuntimeError("legacy profile bound ledger exists but has an unknown/unreadable header")
        if any(len(row) > 1 and row[1] == event_id for row in parsed_ledger[1:]):
            raise RuntimeError("legacy profile refuses to reuse an event_id already present in the ledger")
    elif ledger_state != "absent":
        raise RuntimeError("legacy profile bound ledger existence check was not authoritative")
    header = _scene4_schedule_v3_header() if schema_version == 3 else _scene4_schedule_v2_header()
    stop = threading.Event()
    writer_state: dict = {"registered": False, "rows_written": 0, "rounds": 0,
                          "candidate_bundles": [], "errors": [], "snapshot_intervals": [],
                          "schedule_publish_intervals": [], "snapshot_inflight": False,
                          "publish_inflight": False, "publish_after_stop_rejected": 0,
                          "window_start_boottime_s": window_start_boottime,
        "deadline_boottime_s": fixed_deadline, "schema_version": schema_version,
        "oracle_backend": backend}
    writer_lock = threading.Lock()
    frozen_donors: list[dict] | None = None
    observed_background_since: dict[str, float] = {}
    cadence = max(1.0, min(5.0, float(cfg.get("oracle_profile_action_interval_ms", 1000)) / 1000.0))
    def writer() -> None:
        nonlocal frozen_donors
        next_tick = time.monotonic()
        while not stop.is_set():
            snapshot_host_start_ns = None
            try:
                snapshot_host_start_ns = time.monotonic_ns()
                with writer_lock:
                    writer_state["snapshot_inflight"] = True
                obs = _scene4_legacy_os_snapshot(session, case_id, daily_records, candidate_bundles)
                snapshot_host_end_ns = time.monotonic_ns()
                snapshot_interval = {"round": writer_state.get("rounds", 0) + 1,
                    "host_start_monotonic_ns": snapshot_host_start_ns,
                    "host_end_monotonic_ns": snapshot_host_end_ns,
                    "duration_seconds": (snapshot_host_end_ns - snapshot_host_start_ns) / 1e9,
                    "boottime_start_s": obs.get("boot_pre_s"),
                    "boottime_end_s": obs.get("boot_post_s")}
                with writer_lock:
                    writer_state["snapshot_inflight"] = False
                    writer_state["snapshot_intervals"].append(snapshot_interval)
                    writer_state["rounds"] += 1
                    round_number = writer_state["rounds"]
                evidence = {"round": round_number, "event_id": event_id,
                    "complete": obs.get("complete"), "boot_pre_s": obs.get("boot_pre_s"),
                    "boot_post_s": obs.get("boot_post_s"), "wms_table_complete": obs.get("wms_table", {}).get("complete"),
                    "snapshot_interval": snapshot_interval,
                    "aa_raw": obs.get("aa_raw"), "wms_raw": obs.get("wms_raw"),
                    "proc_raw": obs.get("proc_raw"), "details_raw": obs.get("details_raw"),
                    "donors": obs.get("donors", {}), "error": obs.get("reason")}
                _scene4_append_jsonl(observation_path, evidence)
                if not obs.get("donors"):
                    raise RuntimeError(obs.get("reason", "no daily-ready OS donor identities"))
                by_bundle = obs["donors"]
                if frozen_donors is None:
                    selected = []
                    for bundle in candidate_bundles:
                        item = by_bundle.get(bundle)
                        if not item or not item.get("identity_verified") or \
                                item.get("aa_state") != "BACKGROUND" or \
                                item.get("window_ready") is not True or \
                                item.get("window_visible") is not False or \
                                item.get("window_focused") is not False or \
                                item.get("lifecycle_clear") is not True:
                            continue
                        ready_at = item.get("daily_ready_boottime_s")
                        background_since = max(float(item["aa_observed_boottime_s"]),
                                                float(item["window_observed_boottime_s"]))
                        if ready_at is None or not math.isfinite(float(ready_at)) or background_since < float(ready_at):
                            continue
                        observed_background_since[bundle] = background_since
                        selected.append({"bundle": bundle, "uid": item["uid"], "pid": item["pid"],
                            "start_ticks": item["start_ticks"], "daily_ready": True,
                            "window_ready": True, "daily_ready_boottime_s": float(ready_at),
                            "background_since_s": background_since,
                            "aa_pid": item["pid"], "aa_uid": item["uid"],
                            "aa_state": item["aa_state"],
                            "aa_observed_boottime_s": float(item["aa_observed_boottime_s"]),
                            "window_visible": False, "window_focused": False,
                            "window_observed_boottime_s": float(item["window_observed_boottime_s"]),
                            "proc_verified": True, "proc_start_ticks": item["start_ticks"],
                            "proc_observed_boottime_s": float(item["proc_observed_boottime_s"]),
                            "lifecycle_clear": True,
                            "lifecycle_observed_boottime_s": float(item["lifecycle_observed_boottime_s"]),
                            "background_evidence_source": "os_snapshot"})
                        if schema_version == 3:
                            selected[-1].update({
                                "writeback_file_path": f"memscene_{case_id}.pages",
                                "writeback_file_dev": item["file_dev"],
                                "writeback_file_ino": item["file_ino"],
                                "writeback_file_size": item["file_size"]})
                    if selected:
                        frozen_donors = selected
                    with writer_lock:
                        writer_state["candidate_bundles"] = [item["bundle"] for item in selected]
                if not frozen_donors:
                    raise RuntimeError("no self-owned donor passed fresh AA/WMS/proc/lifecycle identity checks")
                refreshed = []
                for static in frozen_donors:
                    item = by_bundle.get(static["bundle"])
                    if not item or not item.get("identity_verified") or \
                            item.get("start_ticks") != static["start_ticks"] or \
                            (schema_version == 3 and (
                                item.get("file_dev") != static["writeback_file_dev"] or
                                item.get("file_ino") != static["writeback_file_ino"] or
                                item.get("file_size") != static["writeback_file_size"])) or \
                            item.get("window_ready") is not True or \
                            item.get("window_visible") not in (True, False) or \
                            item.get("window_focused") not in (True, False):
                        raise RuntimeError("frozen donor identity/file/window observation became unknown")
                    current = dict(static)
                    current.update({"aa_pid": item["pid"], "aa_uid": item["uid"],
                        "aa_state": item.get("aa_state") or "UNKNOWN",
                        "aa_observed_boottime_s": float(item["aa_observed_boottime_s"]),
                        "window_visible": bool(item["window_visible"]),
                        "window_focused": bool(item["window_focused"]),
                        "window_observed_boottime_s": float(item["window_observed_boottime_s"]),
                        "proc_verified": bool(item["identity_verified"]),
                        "proc_start_ticks": item["start_ticks"],
                        "proc_observed_boottime_s": float(item["proc_observed_boottime_s"]),
                        "lifecycle_clear": bool(item.get("lifecycle_clear")),
                        "lifecycle_observed_boottime_s": float(item["lifecycle_observed_boottime_s"])})
                    refreshed.append(current)
                now = float(d.shell("cat /proc/uptime").split()[0])
                deadline = fixed_deadline
                decision = _scene4_schedule_publish_decision(stop.is_set(), now, deadline)
                if not decision["allowed"]:
                    if stop.is_set():
                        with writer_lock:
                            writer_state["publish_after_stop_rejected"] += 1
                    _scene4_append_jsonl(observation_path, {"round": round_number,
                        "event_id": event_id, "schedule_written": False,
                        "publish_gate_phase": "after_snapshot_before_row",
                        "publish_gate_decision": decision,
                        "deadline_boottime_s": deadline})
                    return
                row = (_scene4_schedule_v3_row(event_id, case_id, deadline, needed, refreshed)
                       if schema_version == 3 else
                       _scene4_schedule_v2_row(event_id, case_id, deadline, needed, refreshed))
                payload = "|".join(header) + "\n" + "|".join(row) + "\n"
                publish_host_start_ns = time.monotonic_ns()
                publish_boot_start = float(d.shell("cat /proc/uptime").split()[0])
                decision = _scene4_schedule_publish_decision(
                    stop.is_set(), publish_boot_start, fixed_deadline)
                if not decision["allowed"]:
                    with writer_lock:
                        writer_state["publish_after_stop_rejected"] += 1
                    _scene4_append_jsonl(observation_path, {"round": round_number,
                        "event_id": event_id, "schedule_written": False,
                        "publish_gate_phase": "immediately_before_atomic_write",
                        "publish_gate_decision": decision,
                        "deadline_boottime_s": fixed_deadline})
                    return
                with writer_lock:
                    writer_state["publish_inflight"] = True
                publish_error = None
                try:
                    _scene4_atomic_schedule_write(session, local_path, schedule_remote, payload)
                except Exception as exc:
                    publish_error = str(exc)
                    raise
                finally:
                    try:
                        publish_boot_end = float(d.shell("cat /proc/uptime").split()[0])
                    except Exception:
                        publish_boot_end = None
                    publish_host_end_ns = time.monotonic_ns()
                    publish_record = {"round": round_number,
                        "host_start_monotonic_ns": publish_host_start_ns,
                        "host_end_monotonic_ns": publish_host_end_ns,
                        "duration_seconds": (publish_host_end_ns-publish_host_start_ns)/1e9,
                        "boottime_start_s": publish_boot_start, "boottime_end_s": publish_boot_end,
                        "deadline_boottime_s": fixed_deadline,
                        "crossed_deadline": bool(publish_boot_end is not None and publish_boot_end > fixed_deadline),
                        "stop_was_set_at_completion": stop.is_set(), "error": publish_error}
                    with writer_lock:
                        writer_state["publish_inflight"] = False
                        writer_state["schedule_publish_intervals"].append(publish_record)
                with writer_lock:
                    writer_state["registered"] = True
                    writer_state["rows_written"] += 1
                _scene4_append_jsonl(observation_path, {"round": round_number,
                    "event_id": event_id, "schedule_written": True,
                    "deadline_boottime_s": deadline,
                    "schedule_publish_interval": publish_record,
                    "donor_bundles": [item["bundle"] for item in refreshed]})
            except Exception as exc:
                with writer_lock:
                    if snapshot_host_start_ns is not None and writer_state.get("snapshot_inflight"):
                        snapshot_host_end_ns = time.monotonic_ns()
                        failed_interval = {"round": writer_state.get("rounds", 0) + 1,
                            "host_start_monotonic_ns": snapshot_host_start_ns,
                            "host_end_monotonic_ns": snapshot_host_end_ns,
                            "duration_seconds": (snapshot_host_end_ns-snapshot_host_start_ns)/1e9,
                            "boottime_start_s": None, "boottime_end_s": None,
                            "error": str(exc)}
                        writer_state["snapshot_intervals"].append(failed_interval)
                        writer_state["snapshot_inflight"] = False
                    writer_state["errors"].append({"time_monotonic": time.monotonic(), "error": str(exc)})
                _scene4_append_jsonl(observation_path, {"event_id": event_id,
                    "schedule_written": False, "error": str(exc)})
            next_tick += cadence
            stop.wait(max(0.0, next_tick - time.monotonic()))
    thread = threading.Thread(target=writer, name=f"scene4-schema{schema_version}-{case_id}", daemon=True)
    thread.start()
    return {"event_id": event_id, "thread": thread, "stop": stop,
        "writer_state": writer_state, "writer_lock": writer_lock,
        "deadline_boottime_s": fixed_deadline,
        "host_deadline_monotonic": host_deadline,
        "window_start_boottime_s": window_start_boottime,
        "schedule_remote": schedule_remote,
        "local_schedule_path": str(local_path), "observations_path": str(observation_path),
        "control_readback": control, "planned_needed_bytes": needed,
        "publish_reserve_seconds": 0.75, "schema_version": schema_version,
        "oracle_backend": backend}


def _scene4_case_plan(single_case_calibration: bool = False,
                      single_case_kind: str = "popup") -> list[tuple[str, int, str]]:
    fixed = [("popup", 0, "popup"), ("popup", 1, "popup"),
             ("startup", 0, "startup"), ("startup", 1, "startup")]
    if not single_case_calibration:
        return fixed
    if single_case_kind not in {"popup", "startup"}:
        raise ValueError("Scene4 single-case kind must be popup or startup")
    return [next(case for case in fixed if case[0] == single_case_kind)]


def _scene4_probe_mode(cfg: dict, phase: str) -> dict:
    baseline_probe = bool(cfg.get("oracle_single_case_calibration_probe", False))
    functional_probe = bool(cfg.get("oracle_functional_calibration_probe", False))
    donor_proof_only = bool(cfg.get("oracle_donor_proof_only_probe", False))
    if baseline_probe and functional_probe:
        raise ValueError("baseline and functional scene4 probes are mutually exclusive")
    if baseline_probe and phase != "baseline":
        raise ValueError("single-case scene4 calibration probe is restricted to baseline A")
    if functional_probe and phase != "optimized":
        raise ValueError("functional scene4 calibration probe requires optimized active-oracle mode")
    if donor_proof_only and (not baseline_probe or phase != "baseline"):
        raise ValueError("donor-proof-only probe requires the non-counting baseline calibration profile")
    return {"single_case_probe": baseline_probe or functional_probe,
            "baseline_probe": baseline_probe, "functional_probe": functional_probe,
            "donor_proof_only": donor_proof_only}

def parse_low_memory_missions(raw: str, include_process_identity: bool = False) -> list[dict]:
    missions = []
    blocks = re.split(r"(?=^\s*Mission ID #)", raw, flags=re.M)
    for block in blocks:
        if "LowMemoryAbility" not in block or "com.huawei.hmos.taskmanager" not in block:
            continue
        mission = re.search(r"Mission ID #(\d+)", block)
        bundle = re.search(r"bundle name \[([^\]]+)\]", block)
        ability = re.search(r"main name \[([^\]]+)\]", block)
        state = re.search(r"state #(\w+)", block)
        start = re.search(r"start time \[(\d+)\]", block)
        if mission and bundle and ability and state and start:
            mono_start = int(start.group(1)) / 1000.0
            item = {"mission_id": mission.group(1), "bundle": bundle.group(1),
                "ability": ability.group(1), "state": state.group(1),
                "start_uptime_seconds": mono_start}
            if include_process_identity:
                pid = re.search(r"(?im)^\s*pid\s*[#:=]\s*(\d+)\b", block)
                item.update(pid=int(pid.group(1)) if pid else None,
                    start_monotonic_seconds=mono_start, start_clock_domain="MONOTONIC")
            missions.append(item)
    return missions

def native_low_memory_popup_candidate(nodes: list[dict], missions: list[dict],
                                      baseline_mission_ids: set[str], case_begin_uptime: float | None,
                                      capture_uptime: float | None,
                                      case_end_uptime: float | None = None) -> dict | None:
    if case_begin_uptime is None or capture_uptime is None or capture_uptime < case_begin_uptime:
        return None
    if case_end_uptime is not None and capture_uptime > case_end_uptime:
        return None
    low_memory_nodes = []
    for node in nodes:
        if node.get("__ownerBundleName") not in NATIVE_LOW_MEMORY_OWNERS:
            continue
        text = " ".join(str(node.get(field, "")) for field in ("text", "content", "description"))
        if any(term in text.lower() for term in ("系统内存不足", "内存不足", "低内存", "low memory", "not enough memory")):
            low_memory_nodes.append(node)
    if not low_memory_nodes:
        return None
    mission = next((row for row in missions if row.get("bundle") in NATIVE_LOW_MEMORY_OWNERS and
                    row.get("ability") == "LowMemoryAbility" and row.get("state") == "FOREGROUND" and
                    row.get("mission_id") not in baseline_mission_ids and
                    case_begin_uptime <= row.get("start_uptime_seconds", float("inf")) <= capture_uptime), None)
    if mission is None:
        return None
    window_ids = sorted({str(row.get("hostWindowId")) for row in low_memory_nodes if row.get("hostWindowId")})
    if not window_ids:
        return None
    return {"owner": mission["bundle"], "ability": mission["ability"],
            "mission_id": mission["mission_id"], "mission_state": mission["state"],
            "mission_start_uptime_seconds": mission["start_uptime_seconds"],
            "window_ids": window_ids, "capture_uptime_seconds": capture_uptime}

def test_popup_is_unoccluded(bundle: str, case_id: str, owned_nodes: list[dict],
                             focused_bundles_now: set[str],
                             system_modal_visible: bool = False) -> bool:
    """Require the test app itself to own the focused window as well as visible prompt text."""
    if system_modal_visible or bundle not in focused_bundles_now:
        return False
    return any(case_id in " ".join(str(row.get(field, "")) for field in
                                   ("text", "content", "description")) and
               bundle in " ".join(str(row.get(field, "")) for field in
                                  ("text", "content", "description"))
               for row in owned_nodes)

# This is an application helper called by the runner, not a pytest test.
test_popup_is_unoccluded.__test__ = False

def visible_native_low_memory_modal(nodes: list[dict], patterns: list[str]) -> bool:
    return any(row.get("__ownerBundleName") in NATIVE_LOW_MEMORY_OWNERS and
               str(row.get("visible", "true")).lower() == "true" and
               any(pattern.lower() in " ".join(str(row.get(field, "")) for field in
                                                ("text", "content", "description")).lower()
                   for pattern in patterns)
               for row in nodes)


def _scene4_test_dialog_bundle(bundle: str) -> bool:
    return bundle in {f"io.memscene.scene4.workload.a{index}" for index in range(3, 9)}


def _scene4_journal_popup_signals(attempts: dict[str, list[dict]],
        readback_proofs: dict[str, dict], bundle_case_ids: dict[str, str],
        begin_boottime_s: float, end_boottime_s: float) -> dict:
    """Report HAP callback/request records separately from any visible popup count."""
    signals = []
    evidence_gaps = []
    for bundle, rows in attempts.items():
        expected_case = bundle_case_ids.get(bundle)
        if not expected_case:
            continue
        for index, row in enumerate(rows):
            event = row.get("event")
            if event not in ("memory_notification", "popup_requested") or row.get("case_id") != expected_case:
                continue
            timestamp = _scene4_event_time(row)
            if timestamp is None:
                evidence_gaps.append({"bundle": bundle, "row_index": index,
                    "event": event, "reason": "signal_row_missing_device_boottime"})
                continue
            if not begin_boottime_s <= timestamp <= end_boottime_s:
                continue
            signals.append({"bundle": bundle, "event": event, "case_id": expected_case,
                "journal_row_index": index, "timestamp_boottime_s": timestamp,
                "pid": row.get("pid"), "level": row.get("level"), "phase": row.get("phase"),
                "source": "HAP journal callback/request; not proof of visible popup"})
    journals_complete = all(readback_proofs.get(bundle, {}).get("complete") is True
                            for bundle in bundle_case_ids)
    complete = journals_complete and not evidence_gaps
    return {"complete_journal_coverage": complete,
        "all_journals_complete": journals_complete,
        "scope": "callbacks and requests only; never added to native visible popup primary count",
        "memory_notification_callback_count_observed": sum(x["event"] == "memory_notification" for x in signals),
        "popup_requested_count_observed": sum(x["event"] == "popup_requested" for x in signals),
        "signals": signals, "evidence_gaps": evidence_gaps,
        "unknown_if_any_journal_incomplete": not complete}


def _scene4_screenshot_needed(scene4_fast_layout: bool, candidate: dict | None,
                              found: list[dict]) -> tuple[bool, int, int]:
    if not scene4_fast_layout:
        return bool(found or candidate), len(found), 0
    visible = [row for row in found if str(row.get("visible", "")).lower() == "true"]
    unknown = [row for row in found
               if str(row.get("visible", "")).lower() not in ("true", "false")]
    # Hidden text alone cannot trigger screenshots. Unknown visibility still
    # gets a diagnostic capture and cannot be counted as visible.
    return bool(candidate or visible or unknown), len(visible), len(unknown)


def _scene4_test_popup_journal_needed(bundle: str, scene4_fast_layout: bool,
                                      owned_text_candidates: list[dict],
                                      screenshot_in_case: bool) -> bool:
    if bundle.startswith("io.memscene.workload."):
        return True
    return bool(scene4_fast_layout and _scene4_test_dialog_bundle(bundle) and
                owned_text_candidates and screenshot_in_case)


SCENE4_FRAME_PERIOD_SECONDS = 2.0
SCENE4_FRAME_MAX_GAP_SECONDS = 5.0
SCENE4_FRAME_EXPECTED_PNG_BYTES = 5_472_434  # measured idle 3120x2080 frame; used only for disk preflight
SCENE4_FRAME_MAX_IDENTITY_AGE_SECONDS = 2.5
SCENE4_FRAME_REVIEW_STATES = {
    "confirmed_native_low_memory", "confirmed_not_native_low_memory", "unknown"}


def _scene4_frame_review_binding_matches(frame: dict, review: dict) -> bool:
    """Bind a human visual classification to one immutable captured image and clock interval."""
    if (not str(frame.get("frame_id", "")).strip() or
            str(review.get("frame_id", "")) != str(frame.get("frame_id", "")) or
            str(review.get("case_id", "")) != str(frame.get("case_id", ""))):
        return False
    frame_sha = str(frame.get("frame_sha256", ""))
    if not re.fullmatch(r"[0-9a-fA-F]{64}", frame_sha) or \
            str(review.get("frame_sha256", "")).lower() != frame_sha.lower():
        return False
    for review_field, frame_field in (("capture_boottime_pre_s", "capture_boottime_pre_s"),
                                      ("capture_boottime_post_s", "capture_boottime_post_s")):
        try:
            review_time, frame_time = float(review.get(review_field)), float(frame.get(frame_field))
            if (not math.isfinite(review_time) or not math.isfinite(frame_time) or
                    abs(review_time-frame_time) > 1e-6):
                return False
        except (TypeError, ValueError):
            return False
    return bool(frame.get("capture_status") == "captured" and frame.get("transfer_status") == "received" and
                frame.get("frame_sha256") and frame.get("capture_clock_valid"))


def _scene4_reviewed_frame_incident(frame: dict, review: dict) -> dict | None:
    """Accept a positive only when visual review and same-frame native identity all agree."""
    if not _scene4_frame_review_binding_matches(frame, review):
        return None
    if review.get("review_classification") != "confirmed_native_low_memory":
        return None
    if review.get("visual_modal_confirmed") is not True:
        return None
    candidates = frame.get("native_identity_candidates", [])
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        try:
            identity_matches = (
                candidate.get("complete") is True and candidate.get("bundle") == "com.huawei.hmos.taskmanager" and
                candidate.get("ability") == "LowMemoryAbility" and candidate.get("visible") is True and
                candidate.get("new_visible_episode") is True and candidate.get("baseline_visible") is not True and
                int(candidate.get("pid", -1)) == int(review.get("pid", -2)) and
                int(candidate.get("uid", -1)) == int(review.get("uid", -2)) and
                str(candidate.get("start_ticks", "")) == str(review.get("start_ticks", "")) and
                int(candidate.get("win_id", -1)) == int(review.get("win_id", -2)) and
                str(candidate.get("mission_id", "")) == str(review.get("mission_id", "")) and
                int(candidate.get("uid", -1)) > 0 and int(candidate.get("pid", -1)) > 0 and
                str(candidate.get("start_ticks", "")) not in ("", "0"))
        except (TypeError, ValueError):
            identity_matches = False
        if identity_matches:
            frame_start = float(frame["capture_boottime_pre_s"])
            frame_end = float(frame["capture_boottime_post_s"])
            identity_start = candidate.get("identity_boottime_pre_s")
            identity_end = candidate.get("identity_boottime_post_s")
            mission_start = candidate.get("mission_start_boottime_s")
            visible_episode_start = candidate.get("visible_episode_start_boottime_s")
            if (isinstance(identity_start, (int, float)) and isinstance(identity_end, (int, float)) and
                    not isinstance(identity_start, bool) and not isinstance(identity_end, bool) and
                    math.isfinite(float(identity_start)) and math.isfinite(float(identity_end)) and
                    isinstance(mission_start, (int, float)) and not isinstance(mission_start, bool) and
                    math.isfinite(float(mission_start)) and
                    isinstance(visible_episode_start, (int, float)) and not isinstance(visible_episode_start, bool) and
                    math.isfinite(float(visible_episode_start)) and
                    0 <= identity_end - identity_start <= SCENE4_FRAME_MAX_IDENTITY_AGE_SECONDS and
                    0 <= identity_end - frame_end <= SCENE4_FRAME_MAX_IDENTITY_AGE_SECONDS and
                    identity_start >= frame_start):
                return {"frame_id": frame["frame_id"], "case_id": frame["case_id"],
                    "incident_id": (f"{frame['case_id']}:popup:native:{candidate['mission_id']}:"
                                    f"{candidate['win_id']}"),
                    "mission_id": candidate["mission_id"], "win_id": candidate["win_id"],
                    "visible_episode_start_boottime_s": candidate["visible_episode_start_boottime_s"],
                    "pid": candidate["pid"], "uid": candidate["uid"],
                    "start_ticks": candidate["start_ticks"],
                    "bundle": candidate["bundle"], "ability": candidate["ability"],
                    "visible": True, "focused": candidate.get("focused"),
                    "frame_sha256": frame["frame_sha256"],
                    "capture_boottime_pre_s": frame_start,
                    "capture_boottime_post_s": frame_end,
                    "identity_boottime_pre_s": identity_start,
                    "identity_boottime_post_s": identity_end}
    return None


def _scene4_frame_manifest_proof(frames: list[dict], reviews: list[dict],
                                 case_begin_boottime_s: float | None,
                                 case_end_boottime_s: float | None,
                                 max_gap_seconds: float = SCENE4_FRAME_MAX_GAP_SECONDS,
                                 case_id: str | None = None,
                                 collection_end_boottime_s: float | None = None,
                                 readonly_observations: list[dict] | None = None) -> dict:
    """Require every scheduled frame and boundary to have an exact, reviewed visual record."""
    errors: list[str] = []
    if (not isinstance(case_begin_boottime_s, (int, float)) or isinstance(case_begin_boottime_s, bool) or
            not isinstance(case_end_boottime_s, (int, float)) or isinstance(case_end_boottime_s, bool) or
            not math.isfinite(float(case_begin_boottime_s)) or
            not math.isfinite(float(case_end_boottime_s))):
        errors.append("frame_window_boundary_missing")
        begin = end = None
    else:
        begin, end = float(case_begin_boottime_s), float(case_end_boottime_s)
        if end < begin:
            errors.append("frame_window_boundary_order_invalid")
    collection_end = end
    if collection_end_boottime_s is not None:
        if (not isinstance(collection_end_boottime_s, (int, float)) or
                isinstance(collection_end_boottime_s, bool) or
                not math.isfinite(float(collection_end_boottime_s))):
            errors.append("frame_collection_end_boundary_invalid")
        else:
            collection_end = float(collection_end_boottime_s)
            if end is not None and collection_end < end:
                errors.append("frame_collection_end_precedes_metric_end")
    reviews_by_id: dict[str, list[dict]] = {}
    for row in reviews:
        reviews_by_id.setdefault(str(row.get("frame_id", "")), []).append(row)
    # Do not sort away sequence errors: the durable manifest is itself evidence.
    slot_values = [row.get("slot_index") for row in frames]
    frame_ids = [str(row.get("frame_id", "")) for row in frames]
    slots_valid = not any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                           for value in slot_values)
    if not slots_valid:
        errors.append("frame_slot_invalid")
    if slots_valid and len(set(slot_values)) != len(slot_values):
        errors.append("frame_slot_duplicate")
    if len(set(frame_ids)) != len(frame_ids) or any(not value for value in frame_ids):
        errors.append("frame_id_duplicate_or_missing")
    if slots_valid and slot_values != sorted(slot_values):
        errors.append("frame_sequence_out_of_order")
    valid_slots = list(slot_values) if slots_valid else []
    if valid_slots and (valid_slots[0] != 0 or valid_slots != list(range(valid_slots[-1] + 1))):
        errors.append("frame_sequence_has_missing_slot")
    expected_case_id = str(case_id) if case_id is not None else next(
        (str(row.get("case_id")) for row in frames if row.get("case_id") is not None), "")
    if any(str(row.get("case_id", "")) != expected_case_id for row in frames):
        errors.append("frame_case_id_mismatch")
    for row in frames:
        timestamp_values = [row.get(key) for key in (
            "slot_expected_boottime_s", "capture_boottime_pre_s", "capture_boottime_post_s",
            "capture_host_start_ns", "capture_host_end_ns", "transfer_host_start_ns",
            "transfer_host_end_ns")]
        for candidate in row.get("native_identity_candidates", []) if isinstance(row.get("native_identity_candidates", []), list) else []:
            timestamp_values.extend(candidate.get(key) for key in (
                "identity_boottime_pre_s", "identity_boottime_post_s", "mission_start_boottime_s",
                "visible_episode_start_boottime_s"))
        if any(value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or
                                      not math.isfinite(float(value))) for value in timestamp_values):
            errors.append(f"frame_timestamp_nonfinite:{row.get('frame_id')}")
    ordered = list(frames)
    review_classification_by_frame_id: dict[str, str] = {}
    in_window, supplemental, classified, events = [], [], [], []
    for frame in ordered:
        pre, post = frame.get("capture_boottime_pre_s"), frame.get("capture_boottime_post_s")
        if begin is None or end is None:
            continue
        slot_time = frame.get("slot_expected_boottime_s")
        valid_bracket = (isinstance(pre, (int, float)) and isinstance(post, (int, float)) and
                         math.isfinite(float(pre)) and math.isfinite(float(post)) and post >= pre)
        if valid_bracket and post < begin:
            continue
        if valid_bracket and pre > collection_end:
            continue
        slot_in_main = isinstance(slot_time, (int, float)) and math.isfinite(float(slot_time)) and begin <= slot_time <= end
        if valid_bracket and begin <= pre <= end and post - pre > max_gap_seconds:
            errors.append(f"frame_capture_duration_exceeded:{frame.get('frame_id')}")
        if valid_bracket and pre < begin <= post:
            frame["supplemental_status"] = "cross_case_begin_boundary_unknown"
            frame["counted_in_metric_window"] = False
            supplemental.append(frame)
            continue
        if valid_bracket and pre <= end < post:
            frame["supplemental_status"] = "cross_metric_end_boundary_unknown"
            frame["counted_in_metric_window"] = False
            supplemental.append(frame)
            continue
        if valid_bracket and end < pre <= collection_end:
            frame["supplemental_status"] = "post_metric_window_supplemental_unknown"
            frame["counted_in_metric_window"] = False
            supplemental.append(frame)
            continue
        if valid_bracket and post > collection_end:
            frame["supplemental_status"] = "cross_collection_end_boundary_unknown"
            frame["counted_in_metric_window"] = False
            supplemental.append(frame)
            continue
        if valid_bracket:
            main_sample = begin <= pre and post <= end
        else:
            main_sample = slot_in_main
        if not main_sample:
            continue
        frame["counted_in_metric_window"] = True
        in_window.append(frame)
        if str(frame.get("case_id", "")) != expected_case_id:
            errors.append(f"frame_case_id_mismatch:{frame.get('frame_id')}")
        if not str(frame.get("frame_id", "")).strip():
            errors.append("frame_id_missing")
        sha = str(frame.get("frame_sha256", ""))
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            errors.append(f"frame_sha256_invalid:{frame.get('frame_id')}")
        time_values = [pre, post, frame.get("slot_expected_boottime_s"),
                       frame.get("capture_host_start_ns"), frame.get("capture_host_end_ns"),
                       frame.get("transfer_host_start_ns"), frame.get("transfer_host_end_ns")]
        for candidate in frame.get("native_identity_candidates", []) if isinstance(frame.get("native_identity_candidates", []), list) else []:
            time_values.extend(candidate.get(field) for field in (
                "identity_boottime_pre_s", "identity_boottime_post_s", "mission_start_boottime_s",
                "visible_episode_start_boottime_s"))
        if any(value is not None and (not isinstance(value, (int, float)) or not math.isfinite(float(value)))
               for value in time_values):
            errors.append(f"frame_timestamp_nonfinite:{frame.get('frame_id')}")
        # A screenCap bracket that touches either boundary has ambiguous visual
        # attribution and is retained as unknown rather than assigned to a case.
        if begin is not None and end is not None and isinstance(pre, (int, float)) and isinstance(post, (int, float)) and \
                (pre < begin or post > end):
            frame["cross_boundary"] = True
            errors.append(f"frame_cross_boundary:{frame.get('frame_id')}")
        if frame.get("capture_status") != "captured" or frame.get("transfer_status") != "received" or \
                not frame.get("capture_clock_valid") or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            errors.append(f"frame_capture_or_transfer_gap:{frame.get('frame_id')}")
            continue
        matching = reviews_by_id.get(str(frame.get("frame_id", "")), [])
        if len(matching) != 1:
            errors.append(f"frame_review_missing_or_duplicate:{frame.get('frame_id')}")
            continue
        review = matching[0]
        if str(review.get("case_id", "")) != str(frame.get("case_id", "")) or not _scene4_frame_review_binding_matches(frame, review):
            errors.append(f"frame_review_binding_mismatch:{frame.get('frame_id')}")
            continue
        classification = review.get("review_classification")
        if classification not in SCENE4_FRAME_REVIEW_STATES:
            errors.append(f"frame_review_pending_or_invalid:{frame.get('frame_id')}")
            continue
        review_classification_by_frame_id[str(frame.get("frame_id", ""))] = classification
        if not str(review.get("reviewer", "")).strip() or not str(review.get("reviewed_at", "")).strip():
            errors.append(f"frame_review_audit_identity_missing:{frame.get('frame_id')}")
            continue
        if classification == "unknown":
            errors.append(f"frame_review_unknown:{frame.get('frame_id')}")
            continue
        if classification == "confirmed_not_native_low_memory" and (
                review.get("full_screen_reviewed") is not True or
                review.get("visual_modal_confirmed") is not False):
            errors.append(f"frame_negative_without_full_screen_review:{frame.get('frame_id')}")
            continue
        if classification == "confirmed_native_low_memory":
            event = _scene4_reviewed_frame_incident(frame, review)
            if event is None:
                errors.append(f"frame_positive_identity_or_visual_proof_missing:{frame.get('frame_id')}")
                continue
            events.append(event)
        classified.append(frame)
    intervals = []
    for prior, current in zip(in_window, in_window[1:]):
        p_start, p_end = prior.get("capture_boottime_pre_s"), prior.get("capture_boottime_post_s")
        c_start, c_end = current.get("capture_boottime_pre_s"), current.get("capture_boottime_post_s")
        if all(isinstance(value, (int, float)) and math.isfinite(float(value))
               for value in (p_start, p_end, c_start, c_end)):
            # Worst case places prior capture at its earliest possible time and
            # the next capture at its latest possible time; never subtract an
            # unobserved capture duration from the coverage gap.
            gap = float(c_end) - float(p_start)
            intervals.append(gap)
            if float(p_end) < float(p_start) or float(c_end) < float(c_start) or \
                    float(p_end) - float(p_start) > max_gap_seconds or \
                    float(c_end) - float(c_start) > max_gap_seconds or gap < 0 or gap > max_gap_seconds:
                errors.append(f"frame_capture_gap_exceeded:{prior.get('frame_id')}:{current.get('frame_id')}")
        elif current.get("slot_expected_boottime_s") is not None:
            errors.append(f"frame_clock_gap:{current.get('frame_id')}")
    if begin is not None and end is not None:
        valid_intervals = [(float(row["capture_boottime_pre_s"]), float(row["capture_boottime_post_s"]))
            for row in in_window if isinstance(row.get("capture_boottime_pre_s"), (int, float)) and
            isinstance(row.get("capture_boottime_post_s"), (int, float)) and
            row.get("capture_status") == "captured" and row.get("transfer_status") == "received"]
        if not valid_intervals:
            errors.append("frame_window_has_no_completed_capture")
        else:
            if valid_intervals[0][1] - begin > max_gap_seconds:
                errors.append("frame_begin_coverage_gap_exceeded")
            if end - valid_intervals[-1][0] > max_gap_seconds:
                errors.append("frame_end_coverage_gap_exceeded")
        observed_gaps = list(intervals)
        if valid_intervals:
            observed_gaps.extend((valid_intervals[0][1] - begin, end - valid_intervals[-1][0]))
        maximum_observed_gap = max(observed_gaps, default=None)
    else:
        maximum_observed_gap = None
    slot_gaps = [row for row in in_window if row.get("capture_status") in
                 ("missed_slot", "capture_timeout", "capture_failed", "transfer_failed", "storage_full")]
    for row in slot_gaps:
        errors.append(f"frame_expected_slot_missing:{row.get('frame_id')}")
    # Consecutive positive samples of the same exact window/process generation
    # form one incident. A different/reused identity starts another incident.
    deduped_events: list[dict] = []
    last_key = None
    last_slot = None
    episode = 0
    frame_by_id = {str(frame.get("frame_id", "")): frame for frame in in_window}
    ordered_events = sorted(events, key=lambda row: (
        not isinstance(frame_by_id.get(str(row.get("frame_id", "")), {}).get("slot_index"), int),
        frame_by_id.get(str(row.get("frame_id", "")), {}).get("slot_index")
            if isinstance(frame_by_id.get(str(row.get("frame_id", "")), {}).get("slot_index"), int)
            else -1))
    for event in ordered_events:
        frame = frame_by_id.get(str(event.get("frame_id", "")), {})
        key = (event["case_id"], event["pid"], event["uid"], event["start_ticks"],
               event.get("mission_id"), event.get("mission_start_boottime_s"), event["win_id"])
        slot = frame.get("slot_index")
        if not isinstance(slot, int) or isinstance(slot, bool):
            continue
        if key != last_key or last_slot is None:
            starts_new_episode = True
        elif slot == last_slot + 1:
            starts_new_episode = False
        elif slot > last_slot + 1:
            between = [row for row in in_window if isinstance(row.get("slot_index"), int) and
                       last_slot < row["slot_index"] < slot]
            starts_new_episode = (len(between) == slot - last_slot - 1 and bool(between) and
                all(row.get("capture_status") == "captured" and
                    row.get("transfer_status") == "received" and
                    review_classification_by_frame_id.get(str(row.get("frame_id"))) ==
                        "confirmed_not_native_low_memory" for row in between))
            # Unknown, missing, pending, or failed frames cannot establish a
            # disappearance. Do not inflate confirmed event lower bounds.
        else:
            starts_new_episode = False
        if starts_new_episode:
            episode += 1
            deduped_events.append({**event, "episode": episode,
                "incident_id": f"{event['case_id']}:popup:episode:{episode}"})
        last_key, last_slot = key, slot
    observer_proof = _scene4_readonly_observation_proof(readonly_observations or [], begin, end,
        expected_case_id, max_gap_seconds)
    if observer_proof.get("complete"):
        # The independent WMS/AA/mission stream establishes window coverage.
        # Blank screenshots need no individual human review; however every
        # native window episode must still have a SHA-bound visual positive.
        visual_frame_ids = {str(event.get("frame_id", "")) for event in events}
        observer_replaced_errors = (
            "frame_capture_gap_exceeded:", "frame_expected_slot_missing:",
            "frame_begin_coverage_gap_exceeded", "frame_end_coverage_gap_exceeded",
            "frame_window_has_no_completed_capture", "frame_capture_or_transfer_gap:",
            "frame_review_missing_or_duplicate:", "frame_review_pending_or_invalid:",
            "frame_review_unknown:", "frame_negative_without_full_screen_review:",
            "frame_clock_gap:")
        retained_errors = []
        for error in errors:
            replaced = any(error == prefix or error.startswith(prefix) for prefix in observer_replaced_errors)
            frame_prefix = next((prefix for prefix in ("frame_sha256_invalid:",
                "frame_timestamp_nonfinite:", "frame_capture_duration_exceeded:")
                if error.startswith(prefix)), None)
            frame_ref = error[len(frame_prefix):] if frame_prefix else ""
            screenshot_only = frame_prefix is not None
            if screenshot_only and frame_ref and frame_ref not in visual_frame_ids:
                replaced = True
            if not replaced:
                retained_errors.append(error)
        errors = retained_errors
        reviewed_events: list[dict] = []
        for episode_index, observed_episode in enumerate(observer_proof.get("visible_native_episodes", []), 1):
            key = tuple(observed_episode.get("identity_key", ()))
            start_s, end_s = observed_episode.get("first_boottime_s"), observed_episode.get("last_boottime_s")
            corroborating = [event for event in events if _scene4_native_identity_key(event) == key and
                isinstance(event.get("capture_boottime_pre_s"), (int, float)) and
                isinstance(event.get("capture_boottime_post_s"), (int, float)) and
                isinstance(start_s, (int, float)) and isinstance(end_s, (int, float)) and
                event["capture_boottime_post_s"] >= start_s and event["capture_boottime_pre_s"] <= end_s]
            if not corroborating:
                errors.append("observer_episode_missing_in-episode_visual_confirmation:" +
                    ":".join(str(value) for value in key))
                continue
            # One reviewed full-screen frame confirms the content of this
            # continuous, exact process/window generation episode. Repeated
            # positive frames do not inflate its count.
            selected = sorted(corroborating, key=lambda event: event["capture_boottime_pre_s"])[0]
            reviewed_events.append({**selected, "episode": episode_index,
                "incident_id": f"{expected_case_id}:popup:episode:{episode_index}",
                "observer_first_boottime_s": start_s, "observer_last_boottime_s": end_s})
        for event in events:
            key = _scene4_native_identity_key(event)
            if not any(tuple(episode.get("identity_key", ())) == key and
                       isinstance(episode.get("first_boottime_s"), (int, float)) and
                       isinstance(episode.get("last_boottime_s"), (int, float)) and
                       event.get("capture_boottime_post_s", -1) >= episode["first_boottime_s"] and
                       event.get("capture_boottime_pre_s", float("inf")) <= episode["last_boottime_s"]
                       for episode in observer_proof.get("visible_native_episodes", [])):
                errors.append(f"visual_native_event_not_in_readonly_window_stream:{event.get('frame_id')}")
        deduped_events = reviewed_events
    return {"complete": not errors and bool(in_window) and
            (len(classified) == len(in_window) or observer_proof.get("complete") is True),
        "case_begin_boottime_s": begin, "case_end_boottime_s": end,
        "collection_end_boottime_s": collection_end,
        "target_period_seconds": SCENE4_FRAME_PERIOD_SECONDS,
        "maximum_allowed_capture_gap_seconds": max_gap_seconds,
        "maximum_observed_gap_seconds": maximum_observed_gap,
        "expected_slot_count": len(in_window), "captured_frame_count": sum(
            row.get("capture_status") == "captured" for row in in_window),
        "reviewed_frame_count": len(classified), "unknown_or_unreviewed_frame_count": len(in_window)-len(classified),
        "supplemental_frame_ids": [row.get("frame_id") for row in supplemental],
        "supplemental_cross_boundary_count": sum(
            str(row.get("supplemental_status", "")).startswith("cross_") for row in supplemental),
        "capture_gap_seconds": intervals, "frame_ids": [row.get("frame_id") for row in in_window],
        "native_popup_events": deduped_events, "readonly_observation_proof": observer_proof,
        "errors": errors}


def _scene4_native_identity_key(row: dict) -> tuple[str, str, str, str, str, str]:
    return (str(row.get("case_id", "")), str(row.get("mission_id", "")),
            str(row.get("win_id", "")), str(row.get("pid", "")),
            str(row.get("uid", "")), str(row.get("start_ticks", "")))


def _scene4_readonly_observation_proof(observations: list[dict], begin: float | None,
                                       end: float | None, case_id: str,
                                       max_gap_seconds: float) -> dict:
    """Validate the independent, per-command-complete native-window stream."""
    errors: list[str] = []
    if begin is None or end is None or end < begin:
        return {"complete": False, "errors": ["observer_window_boundary_invalid"],
                "visible_native_episodes": [], "observation_count": len(observations)}
    rows = sorted((row for row in observations if str(row.get("case_id", "")) == case_id),
                  key=lambda row: (row.get("boottime_pre_s")
                      if isinstance(row.get("boottime_pre_s"), (int, float)) else float("inf"),
                      row.get("observation_index", -1)))
    in_window = []
    required_commands = ("wms_pre", "aa_pre", "missions_pre", "wms_post", "aa_post", "missions_post")
    for row in rows:
        pre, post = row.get("boottime_pre_s"), row.get("boottime_post_s")
        if not (isinstance(pre, (int, float)) and isinstance(post, (int, float)) and
                math.isfinite(float(pre)) and math.isfinite(float(post)) and post >= pre):
            errors.append(f"observer_clock_invalid:{row.get('observation_index')}")
            continue
        if post < begin or pre > end:
            continue
        if pre < begin <= post or pre <= end < post:
            # Keep boundary-spanning samples as supplemental evidence only.
            continue
        if pre < begin or post > end:
            continue
        statuses = row.get("command_status")
        if not isinstance(statuses, dict):
            errors.append(f"observer_command_status_missing:{row.get('observation_index')}")
            continue
        for name in required_commands:
            item = statuses.get(name)
            if not isinstance(item, dict) or item.get("exit_code") != 0 or item.get("complete") is not True:
                errors.append(f"observer_command_incomplete:{row.get('observation_index')}:{name}")
        if row.get("error"):
            errors.append(f"observer_command_error:{row.get('observation_index')}")
        in_window.append(row)
    intervals = []
    for prior, current in zip(in_window, in_window[1:]):
        if float(current["boottime_pre_s"]) < float(prior["boottime_post_s"]):
            errors.append(f"observer_interval_overlap:{prior.get('observation_index')}:{current.get('observation_index')}")
        gap = float(current["boottime_post_s"]) - float(prior["boottime_pre_s"])
        intervals.append(gap)
        if gap < 0 or gap > max_gap_seconds:
            errors.append(f"observer_gap_exceeded:{prior.get('observation_index')}:{current.get('observation_index')}")
    if not in_window:
        errors.append("observer_window_has_no_samples")
        max_observed = None
    else:
        first_gap = float(in_window[0]["boottime_post_s"]) - float(begin)
        last_gap = float(end) - float(in_window[-1]["boottime_pre_s"])
        if first_gap < 0 or first_gap > max_gap_seconds:
            errors.append("observer_begin_coverage_gap_exceeded")
        if last_gap < 0 or last_gap > max_gap_seconds:
            errors.append("observer_end_coverage_gap_exceeded")
        max_observed = max([first_gap, last_gap, *intervals], default=None)
    # A positive episode requires an identity-complete visible candidate on
    # each sample. Only a complete no-candidate observation can establish a
    # disappearance; unknown samples never split an episode.
    episodes: list[dict] = []
    active: dict[tuple, dict] = {}
    for row in in_window:
        candidates = row.get("native_identity_candidates")
        identity_status = row.get("identity_status")
        complete_negative = (identity_status == "no_aa_low_memory_candidate" and
            isinstance(row.get("command_status"), dict) and
            all(row["command_status"].get(name, {}).get("exit_code") == 0 and
                row["command_status"].get(name, {}).get("complete") is True and
                row["command_status"].get(name, {}).get("inventory_complete") is True
                for name in required_commands))
        current_keys = set()
        if identity_status in ("identity_candidate_observed", "identity_candidate_observed_partial") and not isinstance(candidates, list):
            errors.append(f"observer_candidate_list_missing:{row.get('observation_index')}")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    errors.append(f"observer_candidate_malformed:{row.get('observation_index')}")
                    continue
                if candidate.get("complete") is not True or candidate.get("visible") is not True:
                    errors.append(f"observer_candidate_incomplete:{row.get('observation_index')}")
                    continue
                key = _scene4_native_identity_key({**candidate, "case_id": case_id})
                if any(not value for value in key) or key[1] in ("None", ""):
                    errors.append(f"observer_candidate_identity_incomplete:{row.get('observation_index')}")
                    continue
                current_keys.add(key)
                episode = active.get(key)
                if episode is None:
                    episode = {"identity_key": key, "first_observation_index": row.get("observation_index"),
                        "first_boottime_s": row.get("boottime_pre_s"),
                        "last_observation_index": row.get("observation_index"),
                        "last_boottime_s": row.get("boottime_post_s"), "observation_count": 0}
                    episodes.append(episode)
                    active[key] = episode
                episode["last_observation_index"] = row.get("observation_index")
                episode["last_boottime_s"] = row.get("boottime_post_s")
                episode["observation_count"] += 1
        if complete_negative:
            active.clear()
        elif identity_status == "identity_candidate_observed":
            if not current_keys:
                errors.append(f"observer_candidate_identity_missing:{row.get('observation_index')}")
            active = {key: episode for key, episode in active.items() if key in current_keys}
        else:
            errors.append(f"observer_identity_unknown:{row.get('observation_index')}:{identity_status}")
    return {"complete": not errors and bool(in_window), "errors": errors,
        "observation_count": len(rows), "in_window_observation_count": len(in_window),
        "supplemental_cross_boundary_count": sum(
            isinstance(row.get("boottime_pre_s"), (int, float)) and
            isinstance(row.get("boottime_post_s"), (int, float)) and
            (row["boottime_pre_s"] < begin <= row["boottime_post_s"] or
             row["boottime_pre_s"] <= end < row["boottime_post_s"]) for row in rows),
        "max_observed_gap_seconds": max_observed, "visible_native_episodes": episodes,
        "window_begin_boottime_s": begin, "window_end_boottime_s": end}


def _scene4_revalidate_frame_manifest_files(manifest_path: str | Path,
                                             review_path: str | Path,
                                             case_begin_boottime_s: float,
                                             case_end_boottime_s: float,
                                             case_id: str,
                                             collection_end_boottime_s: float | None = None,
                                             readonly_observations_path: str | Path | None = None,
                                             readonly_observations_sha256: str | None = None) -> dict:
    """Re-run the Scene4 proof after a human edits the SHA-bound review JSONL."""
    def read_rows(path: str | Path) -> list[dict]:
        rows = []
        with Path(path).open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"{path}:{line_number}: expected JSON object")
                rows.append(row)
        return rows
    try:
        frames, reviews = read_rows(manifest_path), read_rows(review_path)
    except Exception as exc:
        return {"complete": False, "errors": [f"frame_review_file_read_error:{exc}"],
                "manifest_path": str(manifest_path), "review_path": str(review_path)}
    observations = []
    if readonly_observations_path is not None:
        try:
            actual_sha = hashlib.sha256(Path(readonly_observations_path).read_bytes()).hexdigest()
            if not readonly_observations_sha256 or actual_sha.lower() != readonly_observations_sha256.lower():
                return {"complete": False, "errors": ["readonly_observations_sha256_missing_or_mismatch"],
                    "manifest_path": str(manifest_path), "review_path": str(review_path),
                    "readonly_observations_path": str(readonly_observations_path),
                    "readonly_observations_sha256_expected": readonly_observations_sha256,
                    "readonly_observations_sha256_actual": actual_sha}
            observations = read_rows(readonly_observations_path)
        except Exception as exc:
            return {"complete": False, "errors": [f"readonly_observations_read_error:{exc}"],
                "manifest_path": str(manifest_path), "review_path": str(review_path),
                "readonly_observations_path": str(readonly_observations_path)}
    proof = _scene4_frame_manifest_proof(frames, reviews, case_begin_boottime_s,
        case_end_boottime_s, case_id=case_id,
        collection_end_boottime_s=collection_end_boottime_s,
        readonly_observations=observations)
    proof.update(manifest_path=str(manifest_path), review_path=str(review_path),
        review_states={state: sum(row.get("review_classification") == state for row in reviews)
                       for state in (*SCENE4_FRAME_REVIEW_STATES, "pending_review")})
    return proof


def reconcile_scene4_frame_reviews(result_path: str | Path) -> Path:
    """Recompute a saved Scene4 result after SHA-bound frame reviews are completed.

    The original result, frame manifest, review file, and images are immutable.
    A separate sibling result records every input hash and recomputes counts and
    completeness from the saved non-UI gates plus the current reviewed manifest.
    """
    source = Path(result_path).resolve()
    original_bytes = source.read_bytes()
    original = json.loads(original_bytes.decode("utf-8"))
    scene4 = original.get("scene4_cold_start")
    if not isinstance(scene4, dict) or not isinstance(scene4.get("cases"), list):
        raise ValueError("result does not contain a Scene4 case collection")
    reviewed = deepcopy(original)
    updated_scene4 = reviewed["scene4_cold_start"]
    input_files = []
    for case in updated_scene4["cases"]:
        ui = case.get("ui_evidence", {})
        prior_proof = ui.get("frame_manifest", {})
        manifest_path = prior_proof.get("manifest_path")
        review_path = prior_proof.get("review_path") or ui.get("review_manifest_path")
        if not manifest_path or not review_path:
            proof = {"complete": False, "errors": ["frame_review_manifest_path_missing"],
                     "native_popup_events": []}
        else:
            manifest_path = Path(manifest_path)
            review_path = Path(review_path)
            if not manifest_path.is_absolute():
                manifest_path = source.parent / manifest_path
            if not review_path.is_absolute():
                review_path = source.parent / review_path
            begin = prior_proof.get("case_begin_boottime_s")
            end = prior_proof.get("case_end_boottime_s")
            collection_end = prior_proof.get("collection_end_boottime_s")
            observer_path = prior_proof.get("readonly_observations_path")
            if observer_path:
                observer_path = Path(observer_path)
                if not observer_path.is_absolute():
                    observer_path = source.parent / observer_path
            proof = _scene4_revalidate_frame_manifest_files(manifest_path, review_path,
                begin, end, str(case.get("case_id", "")), collection_end, observer_path,
                prior_proof.get("readonly_observations_sha256"))
            try:
                manifest_rows = [json.loads(line) for line in manifest_path.read_text(
                    encoding="utf-8").splitlines() if line.strip()]
            except Exception as exc:
                manifest_rows = []
                proof["errors"] = list(proof.get("errors", [])) + [
                    f"frame_manifest_image_scan_failed:{exc}"]
                proof["complete"] = False
            try:
                review_rows = [json.loads(line) for line in review_path.read_text(
                    encoding="utf-8").splitlines() if line.strip()]
            except Exception:
                review_rows = []
            visual_frame_ids = {str(row.get("frame_id", "")) for row in review_rows
                if row.get("review_classification") in
                    ("confirmed_native_low_memory", "confirmed_not_native_low_memory")}
            image_errors = []
            for frame in manifest_rows:
                if frame.get("capture_status") != "captured":
                    continue
                image_path = Path(frame.get("local_path") or "")
                if not image_path.is_absolute():
                    image_path = source.parent / image_path
                try:
                    actual_sha = hashlib.sha256(image_path.read_bytes()).hexdigest() if image_path.is_file() else None
                except OSError:
                    actual_sha = None
                input_files.append({"case_id": case.get("case_id"), "kind": "frame_image",
                    "frame_id": frame.get("frame_id"), "path": str(image_path),
                    "sha256": actual_sha})
                if (actual_sha is None or actual_sha.lower() != str(frame.get("frame_sha256", "")).lower()) and \
                        str(frame.get("frame_id", "")) in visual_frame_ids:
                    image_errors.append(f"frame_image_missing_or_sha_mismatch:{frame.get('frame_id')}")
            if image_errors:
                proof["errors"] = list(proof.get("errors", [])) + image_errors
                proof["image_integrity_complete"] = False
                proof["complete"] = False
                proof["native_popup_events"] = []
            else:
                proof["image_integrity_complete"] = True
            for label, path in (("manifest", manifest_path), ("reviews", review_path),
                                ("readonly_observations", observer_path)):
                if path is None:
                    continue
                try:
                    file_digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                except OSError:
                    file_digest = None
                input_files.append({"case_id": case.get("case_id"), "kind": label,
                    "path": str(path), "sha256": file_digest})
        ui["frame_manifest"] = proof
        ui["complete"] = bool(proof.get("complete"))
        ui["pending_review_count"] = proof.get("review_states", {}).get("pending_review", 0)
        case["ui_evidence_complete"] = ui["complete"]
        case["native_popup_events"] = [{**event, "category": "low_memory_popup_native",
            "incident": event.get("incident_id"), "source_id": event.get("frame_id"),
            "case_id": case.get("case_id"), "visual_reviewed": True,
            "timestamp_boottime_s": event.get("capture_boottime_pre_s")}
            for event in proof.get("native_popup_events", [])]
        counts = case.setdefault("counts", {})
        counts["native_low_memory_popup_confirmed_lower_bound"] = len(case["native_popup_events"])
        counts["native_low_memory_popup"] = len(case["native_popup_events"]) if ui["complete"] else None
        incidents = _scene4_unique_incident_ids(case.get("case_id", ""),
            case.get("target_classifications", []),
            case.get("kernel_oom", {}).get("victims", []) +
                case.get("kernel_oom", {}).get("confirmed_unattributed_victims", []),
            case["native_popup_events"])
        case["unique_incident_ids"] = sorted(incidents)
        counts["unique_incidents"] = len(incidents) if ui["complete"] else None
        known_primary = (sum(row.get("classification") == "startup_failure"
                             for row in case.get("target_classifications", [])) +
            len(case.get("kernel_oom", {}).get("victims", [])) +
            len(case.get("kernel_oom", {}).get("confirmed_unattributed_victims", [])) +
            len(case["native_popup_events"]))
        counts["primary_count_confirmed_lower_bound"] = known_primary
        counts["primary_count"] = known_primary if ui["complete"] else None
        counts["native_popup_count_observed"] = bool(ui["complete"])
        non_ui = case.get("measurement_gates_without_ui", {})
        case["measurement_complete"] = bool(non_ui.get("complete") is True and ui["complete"])
        case["primary_counts_observed"] = case["measurement_complete"]
        case["confirmed_count_lower_bounds"] = {
            "startup_failure": counts.get("startup_failure", 0),
            "confirmed_oom_kill": counts.get("confirmed_oom_kill", 0),
            "native_low_memory_popup": len(case["native_popup_events"]),
            "primary_count": known_primary,
            "unique_incidents": len(incidents)}
        if not case["measurement_complete"]:
            case["primary_counts_incomplete_reason"] = {
                "ui_evidence_complete": ui["complete"],
                "non_ui_measurement_gates_complete": non_ui.get("complete") is True}
    run_gates = updated_scene4.get("measurement_gates_without_ui", {})
    updated_scene4["measurement_complete"] = bool(run_gates.get("complete") is True and
        len(updated_scene4["cases"]) == 4 and
        all(case.get("measurement_complete") is True for case in updated_scene4["cases"]))
    keys = set().union(*(case.get("counts", {}).keys() for case in updated_scene4["cases"]))
    aggregate = {}
    for key in keys:
        values = [case.get("counts", {}).get(key) for case in updated_scene4["cases"]]
        aggregate[key] = sum(values) if all(isinstance(value, (int, float)) and
            not isinstance(value, bool) for value in values) else None
    aggregate["native_low_memory_popup_confirmed_lower_bound"] = sum(
        len(case.get("native_popup_events", [])) for case in updated_scene4["cases"])
    aggregate["primary_count_confirmed_lower_bound"] = sum(
        case.get("confirmed_count_lower_bounds", {}).get("primary_count", 0)
        for case in updated_scene4["cases"])
    all_incidents = {incident for case in updated_scene4["cases"]
                     for incident in case.get("unique_incident_ids", [])}
    aggregate["unique_incidents"] = len(all_incidents) if updated_scene4["measurement_complete"] else None
    updated_scene4["confirmed_count_lower_bounds"] = {
        key: (sum(case.get("confirmed_count_lower_bounds", {}).get(key, 0)
                  for case in updated_scene4["cases"])
              if key in ("startup_failure", "confirmed_oom_kill", "native_low_memory_popup", "primary_count")
              else (len(all_incidents) if key == "unique_incidents" else aggregate.get(key)))
        for key in ("startup_failure", "confirmed_oom_kill", "native_low_memory_popup",
                    "primary_count", "unique_incidents")}
    updated_scene4["cumulative_counts_observed"] = bool(updated_scene4["measurement_complete"])
    updated_scene4["cumulative_counts"] = aggregate if updated_scene4["measurement_complete"] else {
        key: None for key in aggregate}
    pair_groups: dict[str, list[dict]] = {}
    for case in updated_scene4["cases"]:
        pair_groups.setdefault(str(case.get("kind", "unknown")), []).append(case)
    updated_scene4["paired_case_review_summary"] = [{
        "case_kind": kind,
        "case_ids": [case.get("case_id") for case in group],
        "pair_complete": len(group) == 2 and all(
            case.get("measurement_complete") is True for case in group),
        "native_popup_counts": [case.get("counts", {}).get("native_low_memory_popup")
                                for case in group],
        "primary_counts": [case.get("counts", {}).get("primary_count") for case in group],
        "native_popup_sum": (sum(case["counts"]["native_low_memory_popup"] for case in group)
            if len(group) == 2 and all(isinstance(case.get("counts", {}).get(
                "native_low_memory_popup"), int) for case in group) else None),
        "native_popup_confirmed_lower_bound": sum(
            len(case.get("native_popup_events", [])) for case in group),
        "primary_count_confirmed_lower_bound": sum(
            case.get("confirmed_count_lower_bounds", {}).get("primary_count", 0)
            for case in group),
        "primary_count_sum": (sum(case["counts"]["primary_count"] for case in group)
            if len(group) == 2 and all(isinstance(case.get("counts", {}).get(
                "primary_count"), int) for case in group) else None)}
        for kind, group in pair_groups.items()]
    reviewed["measurement_evidence_complete"] = updated_scene4["measurement_complete"]
    observed_counts = updated_scene4["measurement_complete"]
    reviewed["counts"] = {**reviewed.get("counts", {}),
        "startup_failure": aggregate.get("startup_failure") if observed_counts else None,
        "oom_kill": aggregate.get("confirmed_oom_kill") if observed_counts else None,
        "low_memory_popup": aggregate.get("native_low_memory_popup") if observed_counts else None,
        "test_popup_separate": aggregate.get("test_popup_separate") if observed_counts else None,
        "donor_oom_side_effects": aggregate.get("donor_oom_side_effects") if observed_counts else None,
        "target_oom_side_effects": aggregate.get("target_oom_side_effects") if observed_counts else None,
        "unknown_timeout_or_gap": aggregate.get("unknown_timeout_or_gap") if observed_counts else None,
        "other_oom_side_effects": aggregate.get("other_oom_side_effects") if observed_counts else None,
        "total_events": aggregate.get("primary_count") if observed_counts else None,
        "unique_incidents": aggregate.get("unique_incidents") if observed_counts else None,
        "primary_count_observed": observed_counts,
        "measurement_complete": observed_counts}
    reviewed["scene4_target"] = {**reviewed.get("scene4_target", {}),
        "primary_count": aggregate.get("primary_count") if observed_counts else None,
        "primary_count_observed": observed_counts,
        "native_low_memory_popup": aggregate.get("native_low_memory_popup") if observed_counts else None,
        "unique_incidents": aggregate.get("unique_incidents") if observed_counts else None,
        "measurement_complete": updated_scene4["measurement_complete"]}
    reviewed["review_reconciliation"] = {"calculator_version": "scene4-review-reconcile-v2",
        "calculator_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_result_path": str(source),
        "source_result_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "manifest_and_review_inputs": input_files,
        "recomputed_at_utc": datetime.now(timezone.utc).isoformat(),
        "original_result_overwritten": False}
    output = source.with_name(source.stem + "_reviewed_" + str(time.time_ns()) + ".json")
    while output.exists():
        output = source.with_name(source.stem + "_reviewed_" + str(time.time_ns()) + ".json")
    atomic_save(output, reviewed)
    return output


def _scene4_marker_payload(raw: str, start_marker: str, end_marker: str) -> str:
    start = (raw or "").find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    while start < len(raw) and raw[start] in "\r\n \t":
        start += 1
    if not end_marker:
        return raw[start:].strip()
    end = raw.find(end_marker, start)
    return raw[start:end].strip() if end >= 0 else ""


def _scene4_frame_clock(raw: str) -> dict:
    values = {key: float(value) for key, value in re.findall(
        r"(boottime_s|monotonic_s|realtime_s|proc_uptime_s)=([0-9]+(?:\.[0-9]+)?)", raw or "")}
    return values


def _scene4_frame_capture_result(raw: str, marker: str) -> dict:
    """Parse the bounded screenCap command's real markers; CLOCK_POST is EOF."""
    raw = raw or ""
    pre_raw = _scene4_marker_payload(raw, marker + "CLOCK_PRE__", marker + "EXIT__")
    post_raw = _scene4_marker_payload(raw, marker + "CLOCK_POST__", "")
    pre_clock, post_clock = _scene4_frame_clock(pre_raw), _scene4_frame_clock(post_raw)
    pre_boot, post_boot = pre_clock.get("boottime_s"), post_clock.get("boottime_s")
    exit_match = re.search(re.escape(marker + "EXIT__") + r"\s*(-?\d+)", raw)
    exit_code = int(exit_match.group(1)) if exit_match else None
    valid = bool(exit_code == 0 and isinstance(pre_boot, (int, float)) and
        isinstance(post_boot, (int, float)) and math.isfinite(pre_boot) and
        math.isfinite(post_boot) and post_boot >= pre_boot)
    return {"pre_clock": pre_clock, "post_clock": post_clock,
        "pre_boottime_s": pre_boot, "post_boottime_s": post_boot,
        "exit_code": exit_code, "valid": valid}


def _scene4_advance_frame_slot(slot: int) -> int:
    """Advance once after each completed capture/transfer attempt."""
    return slot + 1


def _scene4_frame_identity_snapshot(session: "Session", frame: dict,
                                    wms_before_raw: str, wms_after_raw: str,
                                    aa_before_raw: str, aa_after_raw: str,
                                    mission_before_raw: str, mission_after_raw: str) -> dict:
    """Resolve any TaskManager LowMemoryAbility to fresh proc and visible WMS identity."""
    # Legacy Scene4 can start frame capture without the oracle clock-domain
    # initializer. Verify the configured helper hash once, then validate the
    # actual capture's own BOOTTIME/MONOTONIC bracket. Do not require unrelated
    # trace_clock setup for this identity-only path or delay the fixed launch.
    scene4_cfg = session.config.get("scene4", {})
    if not getattr(session, "scene4_frame_clock_hash_verified", False):
        helper_path = str(scene4_cfg.get("oracle_clock_probe_remote_path", "")).strip()
        expected_hash = str(scene4_cfg.get("oracle_clock_probe_sha256", "")).strip().lower()
        if not helper_path or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            return {"status": "unknown_clock_helper_unverified", "candidates": [],
                    "errors": ["clock_helper_path_or_hash_missing"]}
        try:
            hash_raw = session.device.shell("sha256sum " + quote(helper_path), timeout=10, check=False)
        except Exception as exc:
            return {"status": "unknown_clock_helper_unverified", "candidates": [],
                    "errors": [f"clock_helper_hash_probe_failed:{exc}"]}
        match = re.search(r"(?m)^([0-9a-fA-F]{64})\s+" + re.escape(helper_path) + r"\s*$", hash_raw)
        actual_hash = match.group(1).lower() if match else None
        if actual_hash != expected_hash:
            return {"status": "unknown_clock_helper_unverified", "candidates": [],
                    "errors": [f"clock_helper_hash_mismatch:expected={expected_hash},actual={actual_hash}"]}
        session.scene4_frame_clock_hash_verified = True
        session.result.setdefault("scene4_frame_clock_helper", {
            "remote_path": helper_path, "expected_sha256": expected_hash,
            "readback_sha256": actual_hash, "verified": True})
    frame_marker = f"__S4_CAP_{int(frame.get('slot_index', 0)):06d}_"
    capture_validation = _scene4_frame_capture_result(frame.get("capture_raw", ""), frame_marker)
    if not capture_validation.get("valid"):
        return {"status": "unknown_frame_clock_bracket", "candidates": [],
                "errors": ["capture_clock_bracket_or_exit_invalid"]}
    pre_processes = _scene4_aa_process_inventory(aa_before_raw)
    post_processes = _scene4_aa_process_inventory(aa_after_raw)
    before_rows = _scene4_wms_window_inventory(wms_before_raw)
    after_rows = _scene4_wms_window_inventory(wms_after_raw)
    taskmanager = "com.huawei.hmos.taskmanager"
    pre_system = [row for row in pre_processes.get("rows", []) if row.get("bundle") == taskmanager]
    post_system = [row for row in post_processes.get("rows", []) if row.get("bundle") == taskmanager]
    missions_pre = [row for row in parse_low_memory_missions(mission_before_raw, include_process_identity=True)
                    if row.get("bundle") == taskmanager and row.get("ability") == "LowMemoryAbility"]
    missions_post = [row for row in parse_low_memory_missions(mission_after_raw, include_process_identity=True)
                     if row.get("bundle") == taskmanager and row.get("ability") == "LowMemoryAbility"]
    identity = {"status": "no_aa_low_memory_candidate", "candidates": [],
        "errors": [], "wms_table_pre_complete": before_rows.get("complete"),
        "wms_table_post_complete": after_rows.get("complete"),
        "aa_pre_malformed": pre_processes.get("malformed_record_count"),
        "aa_post_malformed": post_processes.get("malformed_record_count"),
        "low_memory_missions_pre": missions_pre, "low_memory_missions_post": missions_post}
    if not missions_post:
        return identity
    unique_post_processes = {row.get("pid"): row for row in post_system}
    unique_pre_processes = {row.get("pid"): row for row in pre_system}
    if len(unique_post_processes) != 1 or len(unique_pre_processes) != 1 or \
            set(unique_post_processes) != set(unique_pre_processes):
        identity.update(status="unknown_taskmanager_process_identity_ambiguous",
                        errors=["taskmanager_pid_or_generation_changed_during_frame"])
        return identity
    system_pid = next(iter(unique_post_processes))
    post_aa = unique_post_processes[system_pid]
    pre_aa = unique_pre_processes[system_pid]
    if int(pre_aa.get("uid", -1)) != int(post_aa.get("uid", -2)):
        identity.update(status="unknown_taskmanager_uid_changed", errors=["aa_uid_changed"])
        return identity
    pre_wins = [row for row in before_rows.get("rows", []) if row.get("pid") == system_pid]
    post_wins = [row for row in after_rows.get("rows", []) if row.get("pid") == system_pid]
    by_pre = {row.get("win_id"): row for row in pre_wins}
    by_post = {row.get("win_id"): row for row in post_wins}
    common_ids = sorted(set(by_pre) & set(by_post))
    if not common_ids:
        identity.update(status="unknown_taskmanager_window_missing_or_changed",
                        errors=["no_stable_taskmanager_window_across_frame"])
        return identity
    baseline = session.popup_baselines.get(frame["case_id"], {})
    baseline_mission_ids = set(str(value) for value in baseline.get("mission_ids", []))
    baseline_window_ids = set(int(value) for value in baseline.get("window_ids", [])
                              if str(value).isdigit())
    new_missions = [row for row in missions_post
                    if str(row.get("mission_id")) not in baseline_mission_ids]
    new_window_ids = [win_id for win_id in common_ids if int(win_id) not in baseline_window_ids]
    if len(new_missions) == 1:
        mission = new_missions[0]
        mission_is_new = True
    elif not new_missions and len(missions_post) == 1 and len(new_window_ids) == 1 and \
            baseline.get("window_inventory_complete") is True:
        # An old Mission ID may be reused after its modal was gone at the
        # baseline. A newly visible, independently verified WinId forms a new episode.
        mission = missions_post[0]
        mission_is_new = False
    else:
        identity.update(status="unknown_low_memory_mission_not_unique_new_mission",
            errors=["no_unique_new_mission_or_new_visible_window_episode"],
            low_memory_mission_candidate_count=len(new_missions),
            new_window_ids=new_window_ids, baseline_window_inventory_complete=baseline.get("window_inventory_complete"))
        return identity
    clock_path = str(scene4_cfg.get("oracle_clock_probe_remote_path", "")).strip()
    if not clock_path:
        identity.update(status="unknown_identity_clock_path_missing", errors=["clock_path_missing"])
        return identity
    chunks = [f"echo __S4_FRAME_IDENTITY_PRE__; {quote(clock_path)}"]
    pid = int(system_pid)
    for label, suffix in (("STAT_BEFORE", "stat"), ("STATUS", "status"), ("CMDLINE", "cmdline"), ("STAT_AFTER", "stat")):
        chunks.append(f"echo __S4_FRAME_P{pid}_{label}__; " +
            (f"cat /proc/{pid}/{suffix} 2>&1" if suffix != "cmdline" else
             f"tr '\\000' ' ' < /proc/{pid}/cmdline 2>&1; echo"))
    for win_id in common_ids:
        chunks.append(f"echo __S4_FRAME_W{win_id}__; hidumper -s WindowManagerService -a '-w {win_id}'")
    chunks.append(f"echo __S4_FRAME_IDENTITY_POST__; {quote(clock_path)}")
    host_start_ns = time.monotonic_ns()
    raw = session.device.shell("; ".join(chunks), timeout=10, check=False)
    host_end_ns = time.monotonic_ns()
    (session.output / "ui" / f"scene4_frame_{frame['case_id']}_{frame['slot_index']:06d}_identity_raw.txt").write_text(
        raw, encoding="utf-8", newline="")
    pre_raw = _scene4_marker_payload(raw, "__S4_FRAME_IDENTITY_PRE__", f"__S4_FRAME_P{pid}_STAT_BEFORE__")
    post_raw = _scene4_marker_payload(raw, "__S4_FRAME_IDENTITY_POST__", "")
    # The post marker is last; _scene4_marker_payload's no-end path handles that.
    pre_clock, post_clock = _scene4_frame_clock(pre_raw), _scene4_frame_clock(post_raw)
    try:
        def part(label: str, next_label: str) -> str:
            return _scene4_marker_payload(raw, f"__S4_FRAME_P{pid}_{label}__",
                                          f"__S4_FRAME_P{pid}_{next_label}__")
        before_stat_raw = part("STAT_BEFORE", "STATUS")
        status_raw = part("STATUS", "CMDLINE")
        cmdline_raw = part("CMDLINE", "STAT_AFTER")
        after_stat_raw = (_scene4_marker_payload(raw, f"__S4_FRAME_P{pid}_STAT_AFTER__",
            f"__S4_FRAME_W{common_ids[0]}__") if common_ids else "")
        before_stat, after_stat = parse_stat(before_stat_raw), parse_stat(after_stat_raw)
        uid_match = re.search(r"(?m)^Uid:\s*(\d+)", status_raw)
        proc_uid = int(uid_match.group(1)) if uid_match else None
        cmdline = cmdline_raw.strip()
        package_match = bool(re.search(r"(?<![A-Za-z0-9_.])" + re.escape(taskmanager) +
            r"(?![A-Za-z0-9_.])", cmdline))
        proc_verified = bool(before_stat["pid"] == after_stat["pid"] == pid and
            before_stat["start_ticks"] == after_stat["start_ticks"] and
            proc_uid == int(post_aa.get("uid", -2)) and package_match and before_stat["start_ticks"] > 0)
        start_ticks = str(before_stat["start_ticks"])
    except (ValueError, TypeError, IndexError, KeyError):
        proc_uid, proc_verified, start_ticks, package_match = None, False, "", False
    identity_pre_s, identity_post_s = pre_clock.get("boottime_s"), post_clock.get("boottime_s")
    identity["identity_command_timing"] = {"host_start_monotonic_ns": host_start_ns,
        "host_end_monotonic_ns": host_end_ns, "host_duration_seconds": (host_end_ns-host_start_ns)/1e9,
        "identity_boottime_pre_s": identity_pre_s, "identity_boottime_post_s": identity_post_s,
        "clock_bracket_valid": bool(identity_pre_s is not None and identity_post_s is not None and
                                     identity_post_s >= identity_pre_s),
        "capture_to_identity_age_seconds": identity_post_s - frame.get("capture_boottime_post_s", identity_post_s)
            if identity_post_s is not None and isinstance(frame.get("capture_boottime_post_s"), (int, float)) else None}
    capture_clock_raw = _scene4_marker_payload(frame.get("capture_raw", ""),
        frame_marker + "CLOCK_PRE__", frame_marker + "EXIT__")
    offsets = [value.get("boottime_s") - value.get("monotonic_s") for value in
               (_scene4_frame_clock(capture_clock_raw),)
               if isinstance(value.get("boottime_s"), (int, float)) and
               isinstance(value.get("monotonic_s"), (int, float))]
    offset = offsets[0] if offsets else None
    mission_start_boot = (mission.get("start_monotonic_seconds") + offset
        if offset is not None and isinstance(mission.get("start_monotonic_seconds"), (int, float)) else None)
    begin_boot = session.popup_baselines.get(frame["case_id"], {}).get("case_begin_boottime_seconds")
    old_mission_reappeared = not mission_is_new
    same_mission_pre_post = (old_mission_reappeared and
        any(str(row.get("mission_id")) == str(mission.get("mission_id")) for row in missions_pre) and
        any(str(row.get("mission_id")) == str(mission.get("mission_id")) for row in missions_post))
    new_window_seen_before_capture = old_mission_reappeared and any(
        int(row.get("win_id", -1)) in new_window_ids for row in pre_wins)
    if (mission_start_boot is None or not isinstance(begin_boot, (int, float)) or
            (not old_mission_reappeared and not float(begin_boot) <= mission_start_boot <=
             float(frame.get("capture_boottime_post_s", -1)) + 0.5) or
            (old_mission_reappeared and not (same_mission_pre_post and
                new_window_seen_before_capture and baseline.get("window_inventory_complete") is True))):
        identity.update(status="unknown_mission_clock_or_case_binding", errors=["mission_outside_case_frame_window"])
        return identity
    visible_episode_start = (float(frame.get("capture_boottime_pre_s"))
        if old_mission_reappeared else mission_start_boot)
    details_by_win = {}
    for win_id in common_ids:
        detail_raw = _scene4_marker_payload(raw, f"__S4_FRAME_W{win_id}__",
            f"__S4_FRAME_W{common_ids[common_ids.index(win_id)+1]}__" if common_ids.index(win_id)+1 < len(common_ids)
            else "__S4_FRAME_IDENTITY_POST__")
        details_by_win[win_id] = _scene4_wms_window_snapshot(wms_after_raw, detail_raw, pid, win_id)
    candidates = []
    for win_id, parsed in details_by_win.items():
        row_pre, row_post = by_pre[win_id], by_post[win_id]
        window_stable = (row_pre.get("pid") == row_post.get("pid") == pid and
                         row_pre.get("window_name") == row_post.get("window_name"))
        prior_episode_starts = []
        worker_state = session.scene4_frame_workers.get(str(frame.get("case_id", "")), {})
        for prior_frame in worker_state.get("rows", []):
            if prior_frame.get("slot_index", -1) >= frame.get("slot_index", 0):
                continue
            for prior_candidate in prior_frame.get("native_identity_candidates", []):
                if (prior_candidate.get("visible") is True and
                        str(prior_candidate.get("pid")) == str(pid) and
                        str(prior_candidate.get("uid")) == str(proc_uid) and
                        str(prior_candidate.get("start_ticks")) == str(start_ticks) and
                        str(prior_candidate.get("mission_id")) == str(mission.get("mission_id")) and
                        int(prior_candidate.get("win_id", -1)) == int(win_id) and
                        isinstance(prior_candidate.get("visible_episode_start_boottime_s"), (int, float))):
                    prior_episode_starts.append(float(prior_candidate["visible_episode_start_boottime_s"]))
        episode_start = (mission_start_boot if mission_is_new else
            min(prior_episode_starts) if prior_episode_starts else
            float(frame.get("capture_boottime_pre_s")))
        episode_in_frame = (isinstance(episode_start, (int, float)) and math.isfinite(float(episode_start)) and
            float(begin_boot) <= float(episode_start) <= float(frame.get("capture_boottime_post_s", -1)))
        timing_ok = (identity_pre_s is not None and identity_post_s is not None and
            identity_post_s >= identity_pre_s and
            0 <= identity_post_s - float(frame.get("capture_boottime_post_s", identity_post_s)) <=
                SCENE4_FRAME_MAX_IDENTITY_AGE_SECONDS and
            identity_post_s - identity_pre_s <= SCENE4_FRAME_MAX_IDENTITY_AGE_SECONDS)
        complete = bool(window_stable and parsed.get("known") and parsed.get("visible") is True and
            parsed.get("pid") == pid and parsed.get("win_id") == win_id and
            proc_verified and timing_ok and episode_in_frame and mission.get("bundle") == taskmanager and
            mission.get("ability") == "LowMemoryAbility")
        candidates.append({"complete": complete, "bundle": taskmanager,
            "ability": "LowMemoryAbility", "mission_id": mission.get("mission_id"),
            "mission_start_boottime_s": mission_start_boot,
            "visible_episode_start_boottime_s": episode_start,
            "pid": pid, "uid": proc_uid,
            "start_ticks": start_ticks, "win_id": win_id,
            "visible": parsed.get("visible"), "focused": parsed.get("focused"),
            "baseline_visible": int(win_id) in baseline_window_ids,
            "new_visible_episode": bool(mission_is_new or int(win_id) not in baseline_window_ids),
            "focus_is_informational_only": True, "window_name": parsed.get("window_name"),
            "wms_pid": parsed.get("pid"), "wms_win_id": parsed.get("win_id"),
            "wms_pre_row": row_pre, "wms_post_row": row_post,
            "proc_verified": proc_verified, "proc_package_match": package_match,
            "process_uid": proc_uid, "aa_uid": post_aa.get("uid"),
            "aa_state": post_aa.get("state"),
            "window_stable_across_capture": window_stable,
            "identity_boottime_pre_s": identity_pre_s,
            "identity_boottime_post_s": identity_post_s,
            "identity_fresh_at_capture": timing_ok})
    identity.update(status="identity_candidate_observed" if candidates else "unknown_candidate_missing",
        candidates=candidates, system_process={"pid": pid, "uid": post_aa.get("uid"),
            "state": post_aa.get("state"), "proc_uid": proc_uid, "start_ticks": start_ticks,
            "proc_verified": proc_verified, "proc_package_match": package_match},
        low_memory_mission=mission)
    return identity

def native_low_memory_close_target(nodes: list[dict], patterns: list[str]) -> dict | None:
    """Find one verified TaskManager warning's header close glyph, never appCloseBtn."""
    roots = [row for row in nodes
             if row.get("__ownerBundleName") in NATIVE_LOW_MEMORY_OWNERS and
             row.get("type") == "root" and
             row.get("abilityName") in (None, "", "LowMemoryAbility") and
             str(row.get("visible", "false")).lower() == "true" and
             row.get("hostWindowId")]
    warning_windows = {str(row.get("hostWindowId")) for row in nodes
                       if row.get("__ownerBundleName") in NATIVE_LOW_MEMORY_OWNERS and
                       str(row.get("visible", "false")).lower() == "true" and
                       any(pattern.lower() in " ".join(str(row.get(field, "")) for field in
                                                       ("text", "content", "description")).lower()
                           for pattern in patterns)}
    roots = [row for row in roots if str(row.get("hostWindowId")) in warning_windows]
    if len(roots) != 1:
        return None
    root = roots[0]
    root_bounds = [int(value) for value in re.findall(r"\d+", str(root.get("bounds", "")))]
    if len(root_bounds) != 4 or root_bounds[2] <= root_bounds[0] or root_bounds[3] <= root_bounds[1]:
        return None
    x1, y1, x2, y2 = root_bounds
    candidates = []
    for row in nodes:
        if (row.get("__ownerBundleName") not in NATIVE_LOW_MEMORY_OWNERS or
                str(row.get("hostWindowId")) != str(root.get("hostWindowId")) or
                row.get("type") != "SymbolGlyph" or
                str(row.get("visible", "false")).lower() != "true" or
                str(row.get("enabled", "false")).lower() != "true" or
                str(row.get("clickable", "false")).lower() != "true" or
                row.get("id") == "appCloseBtn" or row.get("key") == "appCloseBtn"):
            continue
        bounds = [int(value) for value in re.findall(r"\d+", str(row.get("bounds", "")))]
        if len(bounds) != 4:
            continue
        cx, cy = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
        relative_x = (cx - x1) / (x2 - x1)
        relative_y = (cy - y1) / (y2 - y1)
        if 0.88 <= relative_x <= 0.99 and 0.0 <= relative_y <= 0.14:
            candidates.append({"x": cx, "y": cy, "bounds": row.get("bounds"),
                               "hostWindowId": str(root.get("hostWindowId")),
                               "root_bounds": root.get("bounds"),
                               "accessibilityId": row.get("accessibilityId")})
    return candidates[0] if len(candidates) == 1 else None

def popup_attribution_in_window(allow_case_attribution: bool, capture_wall_ms: int | None,
                                begin_wall_ms: int | None, end_wall_ms: int | None = None) -> bool:
    if not allow_case_attribution or capture_wall_ms is None:
        return False
    if begin_wall_ms is not None and capture_wall_ms < begin_wall_ms:
        return False
    if end_wall_ms is not None and capture_wall_ms > end_wall_ms:
        return False
    return True

def daily_stage_complete(bundles: list[str], daily_records: dict[str, list[dict]]) -> bool:
    """Report the warm-up barrier itself, independently from later peak outcomes."""
    return bool(bundles) and all(
        any(row.get("event") == "daily_ready" for row in daily_records.get(bundle, []))
        for bundle in bundles)

def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def atomic_save(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    save(temporary, value)
    temporary.replace(path)

def process_window_diagnostics(samples: list[dict], bundles: list[str],
                              initial: dict, ending: dict) -> dict:
    """Report per-identity counters only since their first sampled observation."""
    ordered = sorted(samples, key=lambda row: row.get("device_uptime_seconds", 0))
    result = {}
    for bundle in bundles:
        by_identity = {}
        for sample in ordered:
            app = sample.get("apps", {}).get(bundle, {})
            for process in app.get("processes", {}).values():
                identity = f"{process.get('pid')}:{process.get('start_ticks')}"
                row = by_identity.setdefault(identity, {"pid": process.get("pid"),
                    "start_ticks": str(process.get("start_ticks")),
                    "first_uptime": sample.get("device_uptime_seconds"),
                    "last_uptime": sample.get("device_uptime_seconds"),
                    "first_minor": process.get("minor"), "last_minor": process.get("minor"),
                    "first_major": process.get("major"), "last_major": process.get("major"),
                    "observations": 0})
                row["last_uptime"] = sample.get("device_uptime_seconds")
                row["last_minor"], row["last_major"] = process.get("minor"), process.get("major")
                row["observations"] += 1
        identities = []
        minor_total = major_total = 0
        for row in by_identity.values():
            minor = row["last_minor"] - row["first_minor"] if None not in (row["last_minor"], row["first_minor"]) else None
            major = row["last_major"] - row["first_major"] if None not in (row["last_major"], row["first_major"]) else None
            row["minor_delta_since_first_observation"] = minor
            row["major_delta_since_first_observation"] = major
            row["counter_regressed"] = bool((minor is not None and minor < 0) or
                                             (major is not None and major < 0))
            if minor is not None and minor >= 0: minor_total += minor
            if major is not None and major >= 0: major_total += major
            identities.append(row)
        baseline = initial.get("apps", {}).get(bundle, {}).get("processes", {})
        final = ending.get("apps", {}).get(bundle, {}).get("processes", {})
        result[bundle] = {"coverage": "partial_lower_bound_since_first_sample",
            "complete": False,
            "reason": "real apps lack a pre-launch baseline; short-lived children may fall between 1s samples",
            "initial_processes": list(baseline.values()), "ending_processes": list(final.values()),
            "processes": identities, "minor_lower_bound": minor_total,
            "major_lower_bound": major_total, "total_lower_bound": minor_total + major_total}
    return result

def _parse_stat_file_identity(raw: str) -> dict:
    """Parse the read-only toybox stat response used to bind a proc VMA to its backing file."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in reversed(lines):
        match = re.match(r"^(\d+)\s+(\d+)\s+(.+)$", line)
        if match:
            return {"device_decimal": int(match.group(1)), "inode": int(match.group(2)),
                    "path": match.group(3), "raw": raw}
    return {"device_decimal": None, "inode": None, "path": None, "raw": raw}


def _linux_device_major_minor(device_decimal: int) -> tuple[int, int]:
    """Decode Linux st_dev using the standard 12:20 major/minor encoding."""
    major = ((device_decimal >> 8) & 0xFFF) | ((device_decimal >> 32) & ~0xFFF)
    minor = (device_decimal & 0xFF) | ((device_decimal >> 12) & ~0xFF)
    return major, minor


def process_mapping_snapshot(device: Device, processes: dict[str, dict],
                             owned_file_path: str | None = None,
                             expected_file_bytes: int | None = None,
                             evidence_dir: Path | None = None,
                             evidence_stem: str = "proc") -> dict:
    """Capture proc stat and smaps_rollup evidence for each live target identity."""
    fields = ("Rss", "Pss", "Pss_Anon", "Pss_File", "RssAnon", "RssFile",
              "Private_Clean", "Private_Dirty", "Swap", "SwapPss")
    result = {}
    for identity, process in processes.items():
        pid = int(process["pid"])
        owned_vmas = []
        stat_file = {"device_decimal": None, "inode": None, "path": None, "raw": ""}
        raw_evidence = ""
        if owned_file_path:
            stat_raw = device.shell(f"stat -c '%d %i %n' {quote(owned_file_path)} 2>&1", check=False)
            stat_file = _parse_stat_file_identity(stat_raw)
            combined = device.shell(
                f"echo __MEMSCENE_STAT_FILE__; {stat_raw.strip()}; "
                f"echo __MEMSCENE_ROLLUP__; cat /proc/{pid}/smaps_rollup; "
                f"echo __MEMSCENE_MAPS__; cat /proc/{pid}/maps; "
                f"echo __MEMSCENE_FULL_SMAPS__; cat /proc/{pid}/smaps", check=False)
            raw_evidence = combined
            rollup_raw, separator, after_rollup = combined.partition("__MEMSCENE_ROLLUP__\n")
            _, maps_separator, after_maps = after_rollup.partition("__MEMSCENE_MAPS__\n")
            maps_raw, smaps_separator, full_raw = after_maps.partition("__MEMSCENE_FULL_SMAPS__\n")
            if not maps_separator:
                maps_raw, full_raw = "", ""
            if separator:
                rollup_raw = after_rollup.partition("__MEMSCENE_MAPS__\n")[0]
                owned_vmas = parse_owned_file_vmas(
                    full_raw, owned_file_path, stat_file.get("inode"),
                    stat_file.get("device_decimal"), maps_raw)
            else:
                rollup_raw = ""
        else:
            rollup_raw = device.shell(f"cat /proc/{pid}/smaps_rollup", check=False)
        values = key_values(rollup_raw)
        raw_path = None
        raw_digest = None
        if owned_file_path and raw_evidence and evidence_dir is not None:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            safe_stem = re.sub(r"[^A-Za-z0-9_.-]", "_", evidence_stem)
            raw_file = evidence_dir / f"{safe_stem}_{pid}_{process.get('start_ticks')}.txt"
            raw_file.write_bytes(raw_evidence.encode("utf-8"))
            raw_path = str(raw_file.relative_to(ROOT))
            raw_digest = hashlib.sha256(raw_file.read_bytes()).hexdigest()
        vma_size = sum(row["metrics_bytes"].get("Size") or 0 for row in owned_vmas)
        vma_fields_complete = bool(owned_vmas) and all(
            all(row["field_coverage"].get(field, False) for field in
                ("Size", "Rss", "Pss", "Private_Clean", "Shared_Clean"))
            for row in owned_vmas)
        stat_inode_match = bool(stat_file.get("inode") is not None and owned_vmas and
                                all(row.get("inode") == stat_file["inode"] for row in owned_vmas))
        stat_dev_match = None
        if stat_file.get("device_decimal") is not None and owned_vmas:
            expected_major_minor = _linux_device_major_minor(stat_file["device_decimal"])
            stat_dev_match = all(row.get("device_major_minor") == expected_major_minor for row in owned_vmas)
        result[identity] = {"pid": pid, "start_ticks": str(process.get("start_ticks")),
            "vsize_bytes": process.get("vsize"), "rss_pages": process.get("rss_pages"),
            "smaps_rollup_available": "Rss" in values,
            "smaps_rollup_bytes": {key: values.get(key) for key in fields},
            "smaps_rollup_field_coverage": {key: key in values for key in fields},
            "owned_file_expected_path": owned_file_path,
            "owned_file_stat": {key: value for key, value in stat_file.items() if key != "raw"},
            "owned_file_stat_raw": stat_file.get("raw"),
            "owned_file_stat_inode_match": stat_inode_match,
            "owned_file_stat_device_match": stat_dev_match,
            "owned_file_vmas": owned_vmas if owned_file_path else None,
            "owned_file_vma_count": len(owned_vmas),
            "owned_file_vma_size_bytes": vma_size if owned_vmas else None,
            "owned_file_expected_size_bytes": expected_file_bytes,
            "owned_file_vma_fields_complete": vma_fields_complete,
            "owned_file_vma_evidence_complete": bool(
                vma_fields_complete and stat_inode_match and stat_dev_match is True and
                expected_file_bytes is not None and vma_size == expected_file_bytes),
            "raw_proc_evidence_path": raw_path,
            "raw_proc_evidence_sha256": raw_digest}
    return result

def parse_owned_file_vmas(raw: str, expected_path: str, expected_inode: int | None = None,
                          expected_device_decimal: int | None = None,
                          maps_raw: str = "") -> list[dict]:
    """Extract VMA residency for the unique case filename and bind it to expected stat."""
    rows: list[dict] = []
    expected_name = Path(expected_path).name
    expected_suffix = "/haps/entry/files/" + expected_name
    map_identity: dict[str, tuple[int | None, tuple[int, int] | None]] = {}
    for line in maps_raw.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not re.match(r"^[0-9a-fA-F]+-[0-9a-fA-F]+$", parts[0]):
            continue
        try:
            inode = int(parts[4])
            major, minor = (int(value, 16) for value in parts[3].split(":", 1))
        except (ValueError, IndexError):
            continue
        mapped_path = parts[5].removesuffix(" (deleted)")
        if mapped_path.endswith(expected_suffix):
            map_identity[mapped_path] = (inode, (major, minor))
    current_header = None
    current_lines: list[str] = []

    def flush() -> None:
        if not current_header:
            return
        parts = current_header.split(None, 5)
        if len(parts) < 6:
            return
        mapped_path = parts[5].removesuffix(" (deleted)")
        if not mapped_path.endswith(expected_suffix):
            return
        try:
            inode = int(parts[4])
            major, minor = (int(value, 16) for value in parts[3].split(":", 1))
        except (ValueError, IndexError):
            return
        if expected_inode is None or expected_device_decimal is None:
            return
        if inode != expected_inode or map_identity.get(mapped_path) != (inode, (major, minor)):
            return
        expected_major_minor = _linux_device_major_minor(expected_device_decimal)
        if (major, minor) != expected_major_minor:
            return
        values = key_values("\n".join(current_lines))
        fields = ("Size", "Rss", "Pss", "Shared_Clean", "Shared_Dirty",
                  "Private_Clean", "Private_Dirty", "Referenced", "Swap", "SwapPss")
        rows.append({"header": current_header,
                     "mapped_path": mapped_path, "inode": inode,
                     "device_major_minor": (major, minor),
                     "metrics_bytes": {field: values.get(field) for field in fields},
                     "field_coverage": {field: field in values for field in fields}})

    header_re = re.compile(r"^[0-9a-fA-F]+-[0-9a-fA-F]+\s+")
    for line in raw.splitlines():
        if header_re.match(line):
            flush()
            current_header = line.strip()
            current_lines = [line]
        elif current_header is not None:
            current_lines.append(line)
    flush()
    return rows

def scene4_reclaim_snapshot(session: Session, case_id: str, bundles: list[str], phase: str) -> dict:
    """Capture per-process residency and global reclaim counters without forcing reclaim."""
    device = session.device
    uptime, wall_ms = device.clock_pair()
    meminfo_raw = device.shell("cat /proc/meminfo", check=False)
    vmstat_raw = device.shell("cat /proc/vmstat", check=False)
    meminfo = key_values(meminfo_raw)
    vmstat = key_values(vmstat_raw)
    selected_meminfo = ("MemTotal", "MemAvailable", "Cached", "Inactive(file)",
                        "Dirty", "Writeback", "Active(file)")
    selected_vmstat = ("pgsteal_kswapd", "pgsteal_direct", "pgscan_kswapd",
                       "pgscan_direct", "pgfault", "pgmajfault")
    app_rows: dict[str, Any] = {}
    for bundle in bundles:
        app_sample_start = time.monotonic()
        try:
            processes = device.app_processes(bundle)
            expected = device.app(bundle)["files"] + f"/memscene_{case_id}.pages"
            expected_bytes = int(session.config["scene4"]["file_mib"]) * 1024**2
            safe_case = re.sub(r"[^A-Za-z0-9_.-]", "_", case_id)
            safe_phase = re.sub(r"[^A-Za-z0-9_.-]", "_", phase)
            maps = process_mapping_snapshot(
                device, processes, expected, expected_bytes,
                (Path(session.output) / "scene4_proc_raw" / safe_case / safe_phase)
                if getattr(session, "output", None) else None,
                f"{safe_case}_{safe_phase}_{bundle}")
            for item in maps.values():
                vmas = item.get("owned_file_vmas") or []
                item["owned_file_vma_count"] = len(vmas)
                item["owned_file_resident_bytes"] = (
                    sum(row["metrics_bytes"]["Rss"] for row in vmas)
                    if vmas and all(row["metrics_bytes"].get("Rss") is not None for row in vmas) else None)
                item["owned_file_pss_bytes"] = (
                    sum(row["metrics_bytes"]["Pss"] for row in vmas)
                    if vmas and all(row["metrics_bytes"].get("Pss") is not None for row in vmas) else None)
                item["residency_scope"] = "exact_case_file_vma_plus_process_wide_smaps_rollup"
            app_rows[bundle] = {"processes": maps, "process_count": len(maps)}
        except Exception as exc:
            app_rows[bundle] = {"processes": {}, "process_count": None, "error": str(exc)}
        app_rows[bundle]["host_sample_started_monotonic"] = app_sample_start
        app_rows[bundle]["host_sample_ended_monotonic"] = time.monotonic()
    return {
        "case_id": case_id, "phase": phase,
        "device_uptime_seconds": uptime, "device_wall_ms": wall_ms,
        "host_snapshot_started_monotonic": min((row["host_sample_started_monotonic"] for row in app_rows.values()), default=None),
        "host_snapshot_ended_monotonic": max((row["host_sample_ended_monotonic"] for row in app_rows.values()), default=None),
        "apps": app_rows,
        "system_meminfo": {key: meminfo.get(key) for key in selected_meminfo},
        "system_vmstat": {key: vmstat.get(key) for key in selected_vmstat},
        "field_coverage": {
            "meminfo": {key: key in meminfo for key in selected_meminfo},
            "vmstat": {key: key in vmstat for key in selected_vmstat},
            "smaps_rollup_all_apps": all(
                bool(row.get("processes")) and
                all(item.get("smaps_rollup_available") for item in row.get("processes", {}).values())
                for row in app_rows.values() if row.get("error") is None),
            "smaps_rollup_fields_all_processes": {
                key: all(item.get("smaps_rollup_field_coverage", {}).get(key, False)
                         for row in app_rows.values() if row.get("error") is None
                         for item in row.get("processes", {}).values())
                for key in ("Rss", "Pss", "Pss_Anon", "Pss_File", "RssAnon", "RssFile",
                            "Private_Clean", "Private_Dirty", "Swap", "SwapPss")},
            "owned_file_vma_present_all_processes": all(
                bool(row.get("processes")) and all(item.get("owned_file_vma_count", 0) > 0
                    for item in row.get("processes", {}).values())
                for row in app_rows.values() if row.get("error") is None),
            "owned_file_vma_rss_pss_covered_all_processes": all(
                item.get("owned_file_vma_evidence_complete", False)
                for row in app_rows.values() if row.get("error") is None
                for item in row.get("processes", {}).values()),
            "owned_file_vma_size_matches_all_processes": all(
                item.get("owned_file_vma_size_bytes") == item.get("owned_file_expected_size_bytes")
                for row in app_rows.values() if row.get("error") is None
                for item in row.get("processes", {}).values()),
            "owned_file_stat_inode_matches_all_processes": all(
                item.get("owned_file_stat_inode_match") is True
                for row in app_rows.values() if row.get("error") is None
                for item in row.get("processes", {}).values()),
            "owned_file_stat_device_matches_all_processes": all(
                item.get("owned_file_stat_device_match") is True
                for row in app_rows.values() if row.get("error") is None
                for item in row.get("processes", {}).values()),
            "all_app_process_samples": all(row.get("error") is None and bool(row.get("processes"))
                                            for row in app_rows.values()),
        },
    }

def qualification_gate(result: dict, config: dict, expected_cases: list[dict]) -> list[str]:
    """Return reasons a measured qualification baseline cannot seed a freeze."""
    reasons = []
    mem_total = result.get("environment", {}).get("meminfo", {}).get("MemTotal")
    configured_gib = int(config.get("dram_gib", 0))
    if (configured_gib not in (16, 32) or mem_total is None or
            not int(0.9 * configured_gib * GIB) <= int(mem_total) <= configured_gib * GIB):
        reasons.append("qualification_requires_memtotal_matching_16_or_32gb_config")
    if result.get("scene") != 2 or result.get("phase") != "baseline" or result.get("profile") != "qualification":
        reasons.append("qualification_requires_scene2_baseline_profile")
    if not result.get("execution_complete") or not result.get("measurement_complete"):
        reasons.append("measurement_incomplete")
    if result.get("cases_hash") != cases_hash(expected_cases):
        reasons.append("case_set_mismatch")
    rows = result.get("case_results", [])
    if (len(rows) != len(expected_cases) or not rows or
            any(row.get("daily_stage_complete") is not True or row.get("fatal_error") for row in rows)):
        reasons.append("case_attempt_evidence_incomplete")
    counts = result.get("counts", {})
    if any(counts.get(key, 0) <= 0 for key in ("startup_failure", "oom_kill", "low_memory_popup")):
        reasons.append("three_trigger_categories_not_all_observed")
    if counts.get("total_events", 0) < config["scene2"]["baseline_min_events"]:
        reasons.append("baseline_below_minimum_category_events")
    return reasons

def target_child_counter_gaps(fork_rows: list[str], samples: list[dict], baseline: dict,
                              ending: dict, target_bundles: list[str]) -> list[dict]:
    """Find target-family forks in the measured window without endpoint counters.

    A process seen in an intermediate sample is insufficient: fault_delta only
    has a final value to add when the same PID:start_ticks identity is present
    at the ending snapshot.
    """
    baseline_processes = {}
    ending_by_pid = {}
    for bundle in target_bundles:
        for identity, process in baseline.get("apps", {}).get(bundle, {}).get("processes", {}).items():
            baseline_processes[identity] = process
        for identity, process in ending.get("apps", {}).get(bundle, {}).get("processes", {}).items():
            ending_by_pid.setdefault(int(process["pid"]), []).append(
                {"identity": identity, **process, "bundle": bundle})
    family_pids = {int(process["pid"]) for process in baseline_processes.values()}
    intermediate_by_pid: dict[int, list[str]] = {}
    for sample in samples:
        for bundle in target_bundles:
            for identity, process in sample.get("apps", {}).get(bundle, {}).get("processes", {}).items():
                intermediate_by_pid.setdefault(int(process["pid"]), []).append(identity)
    children = []
    forked_pids = set()
    for line in fork_rows:
        parent = re.search(r"\bparent_pid=(\d+)", line)
        child = re.search(r"\bchild_pid=(\d+)", line)
        if not (parent and child):
            continue
        parent_pid, child_pid = int(parent.group(1)), int(child.group(1))
        if parent_pid not in family_pids:
            continue
        family_pids.add(child_pid)
        # Multiple births with one reused PID cannot be fully distinguished by
        # sched_process_fork alone; all but an endpoint-matching last birth gap.
        endpoint = ending_by_pid.get(child_pid, [])
        resolved = next((row for row in endpoint if row["identity"] not in baseline_processes), None)
        repeated_pid = child_pid in forked_pids
        forked_pids.add(child_pid)
        if resolved is None or repeated_pid:
            children.append({"parent_pid": parent_pid, "child_pid": child_pid,
                "intermediate_identities": sorted(set(intermediate_by_pid.get(child_pid, []))),
                "ending_identities": [row["identity"] for row in endpoint],
                "reason": "no_matching_endpoint_pid_start_ticks" if resolved is None else "pid_reused_after_multiple_forks",
                "raw": line})
    return children

def validate_frozen_scope(frozen: dict, config: dict, profile: str) -> None:
    if profile == "acceptance" and (
            frozen.get("qualification_scope") != "formal_16gb" or
            frozen.get("dram_gib") != 16 or config.get("dram_gib") != 16):
        raise RuntimeError("正式验收仅接受实测16GiB设备生成的formal_16gb冻结配置")

def effective_settings(config: dict, profile: str) -> dict:
    config = deepcopy(config)
    if profile == "smoke":
        config["scene1"].update(background_mib_per_app=128, pressure_mib=256,
                                allocation_timeout_seconds=90, lifetime_seconds=360)
        config["scene2"].update(daily_mib=64, peak_mib=128, hold_seconds=3,
                                ready_timeout_seconds=30, lifetime_seconds=90, case_cycles=1,
                                calibration_peak_mib_levels=[128])
    return config

def scene4_execution_engine(config: dict) -> str:
    """Resolve Scene4 workload route only from an explicit profile selector."""
    cfg = config.get("scene4", {})
    legacy = cfg.get("execution_profile") == "legacy_peak_transition"
    if legacy and cfg.get("oracle_cold_start_runner"):
        raise ValueError("legacy peak-transition profile cannot use the six-cold-start oracle runner")
    return "scene2" if legacy else "scene4"

def scene3_calibration_coverage(config: dict, current_suite_hash: str) -> dict:
    """Describe reusable capacity evidence without claiming it calibrates this suite."""
    reference = config.get("scene3_calibration_reference", {})
    source = str(reference.get("path", ""))
    result = {"source": source, "used_as_acceptance_gate": False,
              "same_suite_hash": False, "coverage_valid": False, "by_peak_mib": {}}
    if not source:
        result["reason"] = "calibration_reference_not_configured"
        return result
    path = (ROOT / source).resolve()
    try:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        calibration = json.loads(raw.decode("utf-8-sig"))
        result.update({"source_sha256": digest,
                       "source_sha256_matches": digest.lower() == str(reference.get("sha256", "")).lower(),
                       "source_suite_hash": calibration.get("suite_hash"),
                       "current_suite_hash": current_suite_hash,
                       "same_suite_hash": calibration.get("suite_hash") == current_suite_hash,
                       "profile": calibration.get("profile"),
                       "source_checks_valid": bool(calibration.get("checks", {}).get("valid"))})
        expected_bundles = set(config.get("test_apps", []))
        for peak in reference.get("peak_mib", []):
            rows = [row for row in calibration.get("measurements", [])
                    if int(row.get("peak_mib", -1)) == int(peak)]
            valid_bundles = {row.get("bundle") for row in rows
                             if row.get("measured") and row.get("terminal_complete") and
                             row.get("daily_stage_observed") and row.get("peak_observed") and
                             row.get("ready_observed") and
                             int(row.get("journal_touched_peak_bytes", 0)) >= int(peak) * 1024**2}
            result["by_peak_mib"][str(peak)] = {
                "expected_bundles": sorted(expected_bundles),
                "verified_bundles": sorted(valid_bundles),
                "complete": valid_bundles == expected_bundles}
        result["coverage_valid"] = bool(
            result.get("source_sha256_matches") and result.get("source_checks_valid") and
            calibration.get("profile") == "qualification" and
            result["by_peak_mib"] and all(row["complete"] for row in result["by_peak_mib"].values()))
        if not result["coverage_valid"]:
            result["reason"] = "source_hash_profile_or_measured_bundle_coverage_mismatch"
    except Exception as exc:
        result.update({"reason": "calibration_reference_unavailable", "error": str(exc)})
    return result

def suite_spec(config: dict, scene: int, profile: str) -> dict:
    # Version labels deliberately do not enter the workload hash.
    if scene == 3:
        # The short diagnostic uses the same tested scene2 engine, but its fixed
        # two-case definition and the engine settings both participate in its hash.
        settings = {"scene2_engine": config["scene2"], "scene3_cases": config["scene3"]}
    elif scene == 4:
        settings = {"scene2_engine": config["scene2"], "scene4_cases": config["scene4"]}
    else:
        settings = config[f"scene{scene}"]
    return {"schema_version": config["schema_version"], "scene": scene, "profile": profile,
            "dram_gib": config["dram_gib"], "test_apps": config["test_apps"],
            "real_apps": config["real_apps"], "order": list(ORDER) if scene == 1 else None,
            "settings": settings, "popup_text_patterns": config["popup_text_patterns"]}

class Session:
    def __init__(self, device: Device, config: dict, scene: int, phase: str, profile: str):
        self.device, self.config, self.scene, self.phase, self.profile = device, config, scene, phase, profile
        self.id = f"s{scene}_{secrets.token_hex(6)}"
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
        self.output = ROOT / "results" / f"scene{scene}_{phase}_{stamp}_{self.id}"
        self.output.mkdir(parents=True)
        self.remote = "/data/local/tmp/memscenes_" + self.id
        self.device.shell("mkdir -p " + quote(self.remote))
        self.stop_event = threading.Event()
        self.sampler: threading.Thread | None = None
        self.samples: list[dict] = []
        self.markers: list[dict] = []
        self.trace_started = False
        self.trace_segment_name: str | None = None
        self.trace_segments: dict[str, dict] = {}
        self.pressure_pid: int | None = None
        self.oom_trace_previous: str | None = None
        self.oom_trace_instance: str | None = None
        self.oom_trace_instance_name: str | None = None
        self.oom_trace_instance_created = False
        self.lifecycle_trace_instance: str | None = None
        self.lifecycle_trace_instance_name: str | None = None
        self.lifecycle_trace_instance_created = False
        self.lifecycle_trace_global_previous: dict[str, str] = {}
        self.lifecycle_trace_active = False
        self.lifecycle_target_bundles: list[str] = []
        self.lifecycle_baseline: dict | None = None
        self.kernel_dmesg_process: subprocess.Popen | None = None
        self.kernel_dmesg_stream = None
        self.kernel_dmesg_stderr = None
        self.kernel_dmesg_path = self.output / "kernel_dmesg.raw.log"
        self.kernel_dmesg_reader: dict = {"started": False, "path": str(self.kernel_dmesg_path)}
        self.scene4_reader_monitors: dict[str, dict] = {}
        self.owned_page_files: set[tuple[str, str]] = set()
        self.book = EventBook()
        self.ui_observations: dict[str, list[dict]] = {}
        self.popup_baselines: dict[str, dict] = {}
        self.scene4_frame_workers: dict[str, dict] = {}
        self.scene4_journal_readback: dict[tuple[str, str], dict] = {}
        self.result = {"schema_version": 1, "session": self.id, "scene": scene, "phase": phase,
                       "profile": profile, "version_label": config["version_label"],
                       "status": "incomplete", "invalid_reasons": [],
                       "suite": suite_spec(config, scene, profile)}
        self.result["suite_hash"] = canonical_hash({key: value for key, value in self.result["suite"].items()
                                                    if key != "profile"})
        if scene == 3:
            self.result["scene3_scope"] = "short_diagnostic_prevalidation"
            self.result["scene3_calibration_coverage"] = scene3_calibration_coverage(
                config, self.result["suite_hash"])
        elif scene == 4:
            legacy_route = config.get("scene4", {}).get("execution_profile") == "legacy_peak_transition"
            self.result["scene4_scope"] = ("legacy_peak_transition_reproduction" if legacy_route else
                                            "cold_reclaim_sensitivity_prevalidation")
            self.result["scene4_candidate_only"] = not legacy_route
            if legacy_route:
                self.result["scene4_execution_route"] = {
                    "profile": "legacy_peak_transition",
                    "engine": "scene2_case",
                    "reclaim_enabled": False,
                    "hap_manifest": config.get("scene4", {}).get("hap_manifest"),
                }
        save(self.output / "config.json", config)

    def mark(self, name: str, *, device_clock: tuple[float, int] | None = None, **extra) -> dict:
        if device_clock is None:
            uptime = float(self.device.shell("cat /proc/uptime").split()[0])
            item = {"name": name, "device_uptime_seconds": uptime,
                    "device_clock_source": "proc_uptime_legacy", **extra}
        else:
            uptime, wall_ms = device_clock
            item = {"name": name, "device_uptime_seconds": float(uptime),
                    "device_boottime_seconds": float(uptime), "device_wall_ms": int(wall_ms),
                    "device_clock_source": "CLOCK_BOOTTIME", **extra}
        self.markers.append(item)
        safe = re.sub(r"[^a-zA-Z0-9_/-]", "_", name)
        if self.trace_started and self.trace_segment_name:
            self.trace_segments[self.trace_segment_name]["expected_markers"].append(name)
        self.device.shell("echo " + quote("B|0|memscenes/" + self.id + "_" + safe) +
                          " > /sys/kernel/debug/tracing/trace_marker; "
                          "echo E > /sys/kernel/debug/tracing/trace_marker", check=False)
        print(f"[{self.id}] {name}", flush=True)
        atomic_save(self.output / "progress.json", {"markers": self.markers, "status": "running"})
        return item

    def start_scene4_frame_capture(self, case_id: str, case_begin_boottime_s: float,
                                   expected_duration_s: float) -> dict:
        """Start a non-blocking, single-flight 2s screenCap worker for Scene4 only."""
        if self.scene != 4:
            raise RuntimeError("frame capture is Scene4-only")
        if case_id in self.scene4_frame_workers:
            raise RuntimeError(f"Scene4 frame worker already exists for {case_id}")
        capture_backend = self.config.get("scene4", {}).get("frame_capture_backend", "uitest_png")
        if capture_backend not in ("uitest_png", "snapshot_display_jpeg"):
            raise ValueError("unsupported Scene4 frame capture backend")
        capture_suffix = ".jpeg" if capture_backend == "snapshot_display_jpeg" else ".png"
        ui_dir = self.output / "ui"
        frame_dir = ui_dir / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = ui_dir / f"{case_id}_frame_manifest.jsonl"
        review_path = ui_dir / f"{case_id}_frame_reviews.jsonl"
        identity_updates_path = ui_dir / f"{case_id}_frame_identity_updates.jsonl"
        manifest_path.write_text("", encoding="utf-8")
        review_path.write_text("", encoding="utf-8")
        identity_updates_path.write_text("", encoding="utf-8")
        estimated_count = max(1, int(math.ceil(max(0.0, expected_duration_s) /
                                               SCENE4_FRAME_PERIOD_SECONDS)) + 1)
        estimated_bytes = estimated_count * SCENE4_FRAME_EXPECTED_PNG_BYTES
        free_bytes = shutil.disk_usage(frame_dir).free
        state = {"case_id": case_id, "stop": threading.Event(), "rows": [], "reviews": [],
            "manifest_path": str(manifest_path), "review_path": str(review_path),
            "identity_updates_path": str(identity_updates_path),
            "frame_dir": frame_dir, "expected_count_estimate": estimated_count,
            "estimated_bytes": estimated_bytes, "free_bytes_at_start": free_bytes,
            "preflight_complete": free_bytes >= estimated_bytes + 256 * 1024**2,
            "worker": None, "started_host_monotonic_ns": time.monotonic_ns(),
            "case_begin_boottime_s": float(case_begin_boottime_s),
            "identity_queue": queue.Queue(maxsize=2), "identity_stop": object()}
        self.scene4_frame_workers[case_id] = state
        if not state["preflight_complete"]:
            row = {"case_id": case_id, "frame_id": f"{self.id}:{case_id}:000000",
                "slot_index": 0, "slot_expected_boottime_s": float(case_begin_boottime_s),
                "capture_status": "storage_full", "transfer_status": "not_attempted",
                "capture_clock_valid": False, "frame_sha256": None,
                "error": "insufficient_free_disk_for_estimated_frame_set"}
            state["rows"].append(row)
            with manifest_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")
            review = {"case_id": case_id, "frame_id": row["frame_id"], "frame_sha256": None,
                "review_classification": "unknown", "reason": "insufficient_disk_preflight"}
            state["reviews"].append(review)
            review_path.write_text(json.dumps(review, sort_keys=True) + "\n", encoding="utf-8")
            state["error"] = "disk_preflight_failed"
            return state

        def append_row(row: dict, review: dict | None = None) -> None:
            state["rows"].append(row)
            with manifest_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            if review is not None:
                state["reviews"].append(review)
                with review_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(review, sort_keys=True, allow_nan=False) + "\n")

        def identity_worker() -> None:
            identity_queue = state["identity_queue"]
            while True:
                job = identity_queue.get()
                try:
                    if job is state["identity_stop"]:
                        return
                    row, capture_raw = job
                    slot = int(row["slot_index"])
                    marker = f"__S4_META_{slot:06d}_"
                    clock_path = str(self.config.get("scene4", {}).get("oracle_clock_probe_remote_path", "")).strip()
                    command = ("echo " + quote(marker + "CLOCK_PRE__") + "; " + quote(clock_path) + "; "
                        "echo " + quote(marker + "WMS_PRE__") + "; hidumper -s WindowManagerService -a '-a'; "
                        "echo " + quote(marker + "AA_PRE__") + "; aa dump -r; "
                        "echo " + quote(marker + "MISSIONS_PRE__") + "; aa dump -l; "
                        "echo " + quote(marker + "WMS_POST__") + "; hidumper -s WindowManagerService -a '-a'; "
                        "echo " + quote(marker + "AA_POST__") + "; aa dump -r; "
                        "echo " + quote(marker + "MISSIONS_POST__") + "; aa dump -l; "
                        "echo " + quote(marker + "CLOCK_POST__") + "; " + quote(clock_path))
                    host_start_ns = time.monotonic_ns()
                    try:
                        raw = self.device.shell(command, timeout=8, check=False)
                        error = None
                    except Exception as exc:
                        raw, error = "", str(exc)
                    host_end_ns = time.monotonic_ns()
                    raw_path = self.output / "ui" / f"{case_id}_frame_{slot:06d}_identity_metadata_raw.txt"
                    raw_path.write_text(raw, encoding="utf-8", newline="")
                    section = lambda left, right: _scene4_marker_payload(raw,
                        marker + left + "__", marker + right + "__")
                    if error:
                        identity = {"status": "unknown_identity_probe_error", "candidates": [], "errors": [error]}
                    else:
                        try:
                            identity = _scene4_frame_identity_snapshot(self, row,
                                section("WMS_PRE", "AA_PRE"), section("WMS_POST", "AA_POST"),
                                section("AA_PRE", "MISSIONS_PRE"), section("AA_POST", "MISSIONS_POST"),
                                section("MISSIONS_PRE", "WMS_POST"), section("MISSIONS_POST", "CLOCK_POST"))
                        except Exception as exc:
                            identity = {"status": "unknown_identity_probe_error", "candidates": [], "errors": [str(exc)]}
                    meta_pre = _scene4_frame_clock(section("CLOCK_PRE", "WMS_PRE")).get("boottime_s")
                    meta_post_raw = _scene4_marker_payload(raw, marker + "CLOCK_POST__", "")
                    meta_post = _scene4_frame_clock(meta_post_raw).get("boottime_s")
                    identity["metadata_capture_timing"] = {"host_start_monotonic_ns": host_start_ns,
                        "host_end_monotonic_ns": host_end_ns, "host_duration_seconds": (host_end_ns-host_start_ns)/1e9,
                        "metadata_boottime_pre_s": meta_pre, "metadata_boottime_post_s": meta_post,
                        "metadata_error": error}
                    identity_path = self.output / "ui" / f"{case_id}_frame_{slot:06d}_identity.json"
                    identity_path.write_text(json.dumps(identity, sort_keys=True, allow_nan=False, indent=2), encoding="utf-8")
                    row["identity_status"] = identity.get("status")
                    row["native_identity_candidates"] = identity.get("candidates", [])
                    row["identity_path"] = str(identity_path)
                    row["identity_raw_path"] = str(raw_path)
                    detailed_identity_raw = self.output / "ui" / f"scene4_frame_{case_id}_{slot:06d}_identity_raw.txt"
                    detailed_identity_raw_path = str(detailed_identity_raw) if detailed_identity_raw.is_file() else None
                    update = {"case_id": case_id, "frame_id": row["frame_id"], "slot_index": slot,
                        "identity_status": row["identity_status"], "identity_path": row["identity_path"],
                        "identity_metadata_raw_path": row["identity_raw_path"],
                        "identity_raw_path": detailed_identity_raw_path,
                        "metadata_boottime_pre_s": meta_pre,
                        "metadata_boottime_post_s": meta_post,
                        "native_identity_candidates": row.get("native_identity_candidates", [])}
                    with identity_updates_path.open("a", encoding="utf-8", newline="") as stream:
                        stream.write(json.dumps(update, sort_keys=True, allow_nan=False) + "\n")
                        stream.flush()
                finally:
                    identity_queue.task_done()

        def readonly_observation_worker() -> None:
            """Collect an independent WMS/AA/mission stream for the full case."""
            obs_path = self.output / "ui" / f"{case_id}_readonly_observations.jsonl"
            obs_path.parent.mkdir(parents=True, exist_ok=True)
            obs_path.write_text("", encoding="utf-8")
            state["readonly_observations_path"] = str(obs_path)
            clock_path = str(self.config.get("scene4", {}).get("oracle_clock_probe_remote_path", "")).strip()
            interval_ns = int(SCENE4_FRAME_PERIOD_SECONDS * 1e9)
            next_due_ns = time.monotonic_ns()
            index = 0
            while not state["stop"].is_set():
                now_ns = time.monotonic_ns()
                if now_ns < next_due_ns and state["stop"].wait((next_due_ns-now_ns)/1e9):
                    break
                index += 1
                marker = f"__S4_OBS_{index:06d}_"
                command = ("echo " + quote(marker + "CLOCK_PRE__") + "; " + quote(clock_path) + "; "
                    "echo " + quote(marker + "WMS_PRE__") + "; hidumper -s WindowManagerService -a '-a'; "
                    "echo " + quote(marker + "WMS_PRE_RC__") + "$?; "
                    "echo " + quote(marker + "AA_PRE__") + "; aa dump -r; echo " + quote(marker + "AA_PRE_RC__") + "$?; "
                    "echo " + quote(marker + "MISSIONS_PRE__") + "; aa dump -l; echo " + quote(marker + "MISSIONS_PRE_RC__") + "$?; "
                    "echo " + quote(marker + "WMS_POST__") + "; hidumper -s WindowManagerService -a '-a'; "
                    "echo " + quote(marker + "WMS_POST_RC__") + "$?; "
                    "echo " + quote(marker + "AA_POST__") + "; aa dump -r; echo " + quote(marker + "AA_POST_RC__") + "$?; "
                    "echo " + quote(marker + "MISSIONS_POST__") + "; aa dump -l; echo " + quote(marker + "MISSIONS_POST_RC__") + "$?; "
                    "echo " + quote(marker + "CLOCK_POST__") + "; " + quote(clock_path))
                host_start_ns = time.monotonic_ns()
                try:
                    raw = self.device.shell(command, timeout=8, check=False)
                    error = None
                except Exception as exc:
                    raw, error = "", str(exc)
                host_end_ns = time.monotonic_ns()
                raw_path = self.output / "ui" / f"{case_id}_readonly_observation_{index:06d}_raw.txt"
                raw_path.write_text(raw, encoding="utf-8", newline="")
                clock_pre_raw = _scene4_marker_payload(raw, marker + "CLOCK_PRE__", "WMS_PRE__")
                clock_post_raw = _scene4_marker_payload(raw, marker + "CLOCK_POST__", "")
                pre = _scene4_frame_clock(clock_pre_raw)
                post = _scene4_frame_clock(clock_post_raw)
                def section(label: str, following: str) -> str:
                    return _scene4_marker_payload(raw, marker + label + "__", marker + following + "__")
                def rc(label: str) -> int | None:
                    match = re.search(re.escape(marker + label + "_RC__") + r"\s*(\d+)", raw)
                    return int(match.group(1)) if match else None
                def has_marker(label: str) -> bool:
                    return marker + label + "__" in raw
                wms_pre = section("WMS_PRE", "WMS_PRE_RC")
                aa_pre = section("AA_PRE", "AA_PRE_RC")
                missions_pre = section("MISSIONS_PRE", "MISSIONS_PRE_RC")
                wms_post = section("WMS_POST", "WMS_POST_RC")
                aa_post = section("AA_POST", "AA_POST_RC")
                missions_post = section("MISSIONS_POST", "MISSIONS_POST_RC")
                wms_pre_inventory = _scene4_wms_window_inventory(wms_pre)
                wms_post_inventory = _scene4_wms_window_inventory(wms_post)
                aa_pre_inventory = _scene4_aa_process_inventory(aa_pre)
                aa_post_inventory = _scene4_aa_process_inventory(aa_post)
                mission_target_blocks = lambda value: [block for block in re.split(
                    r"(?=^\s*Mission ID #)", value or "", flags=re.M)
                    if "LowMemoryAbility" in block and "com.huawei.hmos.taskmanager" in block]
                mission_pre_count = len(parse_low_memory_missions(missions_pre, include_process_identity=True))
                mission_post_count = len(parse_low_memory_missions(missions_post, include_process_identity=True))
                command_status = {
                    "wms_pre": {"exit_code": rc("WMS_PRE"), "complete": has_marker("WMS_PRE") and bool(wms_pre.strip()), "inventory_complete": wms_pre_inventory.get("complete") is True},
                    "aa_pre": {"exit_code": rc("AA_PRE"), "complete": has_marker("AA_PRE") and _scene4_observer_aa_inventory_complete(aa_pre), "inventory_complete": _scene4_observer_aa_inventory_complete(aa_pre)},
                    "missions_pre": {"exit_code": rc("MISSIONS_PRE"), "complete": has_marker("MISSIONS_PRE") and _scene4_observer_mission_inventory_complete(missions_pre), "inventory_complete": _scene4_observer_mission_inventory_complete(missions_pre)},
                    "wms_post": {"exit_code": rc("WMS_POST"), "complete": has_marker("WMS_POST") and bool(wms_post.strip()), "inventory_complete": wms_post_inventory.get("complete") is True},
                    "aa_post": {"exit_code": rc("AA_POST"), "complete": has_marker("AA_POST") and _scene4_observer_aa_inventory_complete(aa_post), "inventory_complete": _scene4_observer_aa_inventory_complete(aa_post)},
                    "missions_post": {"exit_code": rc("MISSIONS_POST"), "complete": has_marker("MISSIONS_POST") and _scene4_observer_mission_inventory_complete(missions_post), "inventory_complete": _scene4_observer_mission_inventory_complete(missions_post)}}
                # Resolve OS identity independently of a missing screenshot. This
                # is metadata evidence only; it cannot create visual confirmation.
                synthetic_slot = 100000 + index
                capture_marker = f"__S4_CAP_{synthetic_slot:06d}_"
                capture_raw = (capture_marker + "CLOCK_PRE__\n" + clock_pre_raw + "\n" +
                    capture_marker + "EXIT__0\n" + capture_marker + "CLOCK_POST__\n" + clock_post_raw)
                identity_frame = {"case_id": case_id, "slot_index": synthetic_slot,
                    "capture_boottime_pre_s": pre.get("boottime_s"),
                    "capture_boottime_post_s": post.get("boottime_s"),
                    "capture_raw": capture_raw}
                try:
                    if all(item["exit_code"] == 0 and item["complete"] for item in command_status.values()):
                        identity = _scene4_frame_identity_snapshot(self, identity_frame,
                            wms_pre, wms_post, aa_pre, aa_post, missions_pre, missions_post)
                    else:
                        identity = {"status": "unknown_observation_command_incomplete", "candidates": [],
                            "errors": [name for name, item in command_status.items()
                                if item["exit_code"] != 0 or not item["complete"]]}
                except Exception as exc:
                    identity = {"status": "unknown_identity_probe_error", "candidates": [],
                                "errors": [str(exc)]}
                record = {"case_id": case_id, "observation_index": index,
                    "host_start_monotonic_ns": host_start_ns, "host_end_monotonic_ns": host_end_ns,
                    "host_duration_seconds": (host_end_ns-host_start_ns)/1e9,
                    "boottime_pre_s": pre.get("boottime_s"), "boottime_post_s": post.get("boottime_s"),
                    "raw_path": str(raw_path), "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                    "error": error, "visual_frame": None, "command_status": command_status,
                    "identity_status": identity.get("status"),
                    "native_identity_candidates": identity.get("candidates", []),
                    "identity": identity,
                    "wms_pre_raw": wms_pre, "aa_pre_raw": aa_pre,
                    "missions_pre_raw": missions_pre, "wms_post_raw": wms_post,
                    "aa_post_raw": aa_post, "missions_post_raw": missions_post}
                with obs_path.open("a", encoding="utf-8", newline="") as stream:
                    stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
                    stream.flush()
                state["readonly_observations_path"] = str(obs_path)
                state["readonly_observation_count"] = index
                next_due_ns = time.monotonic_ns() + interval_ns

        def worker() -> None:
            slot = 0
            anchor_host_ns = None
            self.device.shell("mkdir -p " + quote(self.remote), check=False)
            clock_path = str(self.config.get("scene4", {}).get("oracle_clock_probe_remote_path", "")).strip()
            while not state["stop"].is_set():
                now_ns = time.monotonic_ns()
                due_ns = now_ns if anchor_host_ns is None else anchor_host_ns + slot * int(SCENE4_FRAME_PERIOD_SECONDS * 1e9)
                if now_ns < due_ns and state["stop"].wait((due_ns-now_ns)/1e9):
                    break
                actual_ns = time.monotonic_ns()
                if anchor_host_ns is None:
                    anchor_host_ns = actual_ns
                # If a prior capture/transfer overran slots, materialize every
                # missed slot. Never issue burst captures to hide a gap.
                next_slot = int((actual_ns - anchor_host_ns) // int(SCENE4_FRAME_PERIOD_SECONDS * 1e9))
                if next_slot > slot:
                    for missing_slot in range(slot, next_slot):
                        missing = {"case_id": case_id,
                            "frame_id": f"{self.id}:{case_id}:{missing_slot:06d}",
                            "slot_index": missing_slot,
                            "slot_expected_boottime_s": float(state.get("first_capture_boottime_s") or
                                case_begin_boottime_s) + missing_slot * SCENE4_FRAME_PERIOD_SECONDS,
                            "capture_status": "missed_slot", "transfer_status": "not_attempted",
                            "capture_clock_valid": False, "frame_sha256": None}
                        append_row(missing, {"case_id": case_id, "frame_id": missing["frame_id"],
                            "frame_sha256": None, "review_classification": "unknown",
                            "reason": "capture_slot_missed"})
                    slot = next_slot
                frame_id = f"{self.id}:{case_id}:{slot:06d}"
                remote_png = _scene4_frame_remote_path(self.remote, self.id, case_id, slot)
                if capture_backend == "snapshot_display_jpeg":
                    remote_png = remote_png[:-4] + capture_suffix
                    capture_command = "snapshot_display -f " + quote(remote_png) + " -w 1560 -h 1040"
                else:
                    capture_command = "uitest screenCap -p " + quote(remote_png)
                host_capture_start = time.monotonic_ns()
                marker = f"__S4_CAP_{slot:06d}_"
                command = ("echo " + quote(marker + "CLOCK_PRE__") + "; " + quote(clock_path) + "; "
                    + capture_command + "; echo " + quote(marker + "EXIT__") + "$?; "
                    "echo " + quote(marker + "CLOCK_POST__") + "; " + quote(clock_path))
                capture_error = None
                try:
                    capture_raw = self.device.shell(command, timeout=10, check=False)
                except Exception as exc:
                    capture_raw, capture_error = "", str(exc)
                host_capture_end = time.monotonic_ns()
                capture_result = _scene4_frame_capture_result(capture_raw, marker)
                pre_clock, post_clock = capture_result["pre_clock"], capture_result["post_clock"]
                pre_boot, post_boot = capture_result["pre_boottime_s"], capture_result["post_boottime_s"]
                exit_code = capture_result["exit_code"]
                captured = bool(not capture_error and capture_result["valid"])
                if captured and state.get("first_capture_boottime_s") is None:
                    state["first_capture_boottime_s"] = pre_boot
                row = {"case_id": case_id, "frame_id": frame_id, "slot_index": slot,
                    "slot_expected_boottime_s": float(state.get("first_capture_boottime_s") or
                        case_begin_boottime_s) + slot * SCENE4_FRAME_PERIOD_SECONDS,
                    "scheduled_host_monotonic_ns": due_ns,
                    "capture_host_start_ns": host_capture_start, "capture_host_end_ns": host_capture_end,
                    "capture_boottime_pre_s": pre_boot, "capture_boottime_post_s": post_boot,
                    "capture_clock_valid": captured, "capture_exit_code": exit_code,
                    "capture_status": "captured" if captured else "capture_failed",
                    "capture_backend": capture_backend,
                    "requested_resolution": [1560, 1040] if capture_backend == "snapshot_display_jpeg" else None,
                    "capture_error": capture_error, "capture_raw_path": None,
                    "capture_raw": capture_raw, "transfer_status": "not_attempted",
                    "frame_sha256": None, "frame_bytes": None, "local_path": None,
                    "transfer_host_start_ns": None, "transfer_host_end_ns": None,
                    "transfer_elapsed_seconds": None, "native_identity_candidates": [],
                    "identity_status": "not_checked", "review_classification": "pending_review"}
                raw_path = self.output / "ui" / f"{case_id}_frame_{slot:06d}_capture_raw.txt"
                raw_path.write_text(capture_raw, encoding="utf-8", newline="")
                row["capture_raw_path"] = str(raw_path)
                if captured:
                    temp_path = state["frame_dir"] / f".{slot:06d}.part"
                    transfer_start = time.monotonic_ns()
                    try:
                        self.device.call("file", "recv", remote_png, str(temp_path), timeout=12, check=False)
                        if not temp_path.is_file():
                            raise RuntimeError("screenCap file receive did not create local file")
                        blob = temp_path.read_bytes()
                        valid_signature = (blob.startswith(b"\xff\xd8\xff") and blob.endswith(b"\xff\xd9")) if capture_suffix == ".jpeg" else blob.startswith(b"\x89PNG\r\n\x1a\n")
                        if not valid_signature:
                            raise RuntimeError("received frame lacks expected image signature")
                        digest = hashlib.sha256(blob).hexdigest()
                        final_path = state["frame_dir"] / f"{digest}{capture_suffix}"
                        if final_path.exists():
                            temp_path.unlink()
                        else:
                            temp_path.replace(final_path)
                        transfer_end = time.monotonic_ns()
                        row.update(transfer_status="received", frame_sha256=digest,
                            frame_bytes=len(blob), local_path=str(final_path),
                            transfer_host_start_ns=transfer_start, transfer_host_end_ns=transfer_end,
                            transfer_elapsed_seconds=(transfer_end-transfer_start)/1e9)
                    except Exception as exc:
                        transfer_end = time.monotonic_ns()
                        try:
                            temp_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        row.update(transfer_status="transfer_failed", transfer_error=str(exc),
                            transfer_host_start_ns=transfer_start, transfer_host_end_ns=transfer_end,
                            transfer_elapsed_seconds=(transfer_end-transfer_start)/1e9)
                    if row["transfer_status"] == "received":
                        try:
                            state["identity_queue"].put_nowait((row, capture_raw))
                        except queue.Full:
                            row["identity_status"] = "unknown_identity_metadata_queue_full"
                            state.setdefault("identity_gaps", []).append(frame_id)
                review = {"case_id": case_id, "frame_id": frame_id,
                    "frame_sha256": row.get("frame_sha256"),
                    "capture_boottime_pre_s": pre_boot, "capture_boottime_post_s": post_boot,
                    "review_classification": "pending_review" if captured and row.get("transfer_status") == "received" else "unknown",
                    "visual_modal_confirmed": None,
                    "full_screen_reviewed": False, "reviewer": None, "reviewed_at": None,
                    "candidate_identity_count": len(row.get("native_identity_candidates", []))}
                append_row(row, review)
                # Consume exactly one sequence number for every completed
                # capture/transfer attempt, before any continue branch.
                slot = _scene4_advance_frame_slot(slot)
                if not captured:
                    state.setdefault("capture_gaps", []).append({"frame_id": frame_id,
                        "error": row.get("capture_error") or "screen_capture_failed"})
                    # A returned non-zero capture is terminal for this slot but
                    # does not hide later observations. A command timeout is
                    # different: remote completion is unknown, so never issue
                    # overlapping captures.
                    if capture_error:
                        state["error"] = "screen_capture_completion_unknown_after_timeout"
                        # The independent observation stream was started with
                        # the frame worker and continues after this timeout.
                        break
                    continue
                if row.get("transfer_status") != "received":
                    state.setdefault("capture_gaps", []).append({"frame_id": frame_id,
                        "error": row.get("transfer_error") or "frame_transfer_failed"})
                    continue
                if state["stop"].is_set():
                    break
            state["finished_host_monotonic_ns"] = time.monotonic_ns()

        identity_thread = threading.Thread(target=identity_worker,
            name=f"scene4-frame-identity-{case_id}", daemon=True)
        state["identity_worker"] = identity_thread
        identity_thread.start()
        observation = threading.Thread(target=readonly_observation_worker,
            name=f"scene4-readonly-observation-{case_id}", daemon=True)
        state["readonly_observation_worker"] = observation
        observation.start()
        thread = threading.Thread(target=worker, name=f"scene4-frame-{case_id}", daemon=True)
        state["worker"] = thread
        thread.start()
        return state

    def stop_scene4_frame_capture(self, case_id: str) -> None:
        state = self.scene4_frame_workers.get(case_id)
        if state:
            state["stop"].set()

    def finish_scene4_frame_capture(self, case_id: str, case_end_boottime_s: float,
                                    collection_end_boottime_s: float | None = None) -> dict:
        state = self.scene4_frame_workers.get(case_id)
        if not state:
            return {"complete": False, "errors": ["frame_worker_missing"], "frames": [], "reviews": []}
        state["stop"].set()
        thread = state.get("worker")
        if thread:
            thread.join(timeout=40)
        if thread and thread.is_alive():
            state["error"] = "frame_worker_join_timeout"
        state["identity_queue"].put(state["identity_stop"])
        identity_thread = state.get("identity_worker")
        if identity_thread:
            identity_thread.join(timeout=60)
            if identity_thread.is_alive():
                state["error"] = "frame_identity_worker_join_timeout"
        observation_thread = state.get("readonly_observation_worker")
        if observation_thread:
            observation_thread.join(timeout=12)
            if observation_thread.is_alive():
                state.setdefault("secondary_errors", []).append("readonly_observation_worker_join_timeout")
        cleanup = {"attempted": False, "verified_absent": False}
        if thread and not thread.is_alive():
            suffix = ".jpeg" if self.config.get("scene4", {}).get("frame_capture_backend") == "snapshot_display_jpeg" else ".png"
            remote_glob = self.remote + f"/scene4_{self.id}_{case_id}_frame_*{suffix}"
            cleanup["attempted"] = True
            cleanup["raw"] = self.device.shell("rm -f " + remote_glob +
                "; if ls " + remote_glob + " >/dev/null 2>&1; then echo present; else echo absent; fi",
                timeout=8, check=False)
            cleanup["verified_absent"] = cleanup["raw"].strip().endswith("absent")
        observation_rows = []
        observation_path = state.get("readonly_observations_path")
        observation_sha256 = None
        if observation_path:
            try:
                observation_bytes = Path(observation_path).read_bytes()
                observation_sha256 = hashlib.sha256(observation_bytes).hexdigest()
                observation_rows = [json.loads(line) for line in observation_bytes.decode(
                    "utf-8").splitlines() if line.strip()]
            except (OSError, ValueError) as exc:
                state.setdefault("secondary_errors", []).append(f"readonly_observations_read_failed:{exc}")
        proof = _scene4_frame_manifest_proof(state["rows"], state["reviews"],
            state.get("case_begin_boottime_s"), case_end_boottime_s, case_id=case_id,
            collection_end_boottime_s=collection_end_boottime_s,
            readonly_observations=observation_rows)
        proof.update({"frames": state["rows"], "reviews": state["reviews"],
            "manifest_path": state["manifest_path"], "review_path": state["review_path"],
            "free_bytes_at_start": state["free_bytes_at_start"],
            "estimated_bytes": state["estimated_bytes"], "preflight_complete": state["preflight_complete"],
            "identity_updates_path": state.get("identity_updates_path"),
            "capture_gaps": state.get("capture_gaps", []),
            "identity_gaps": state.get("identity_gaps", []),
            "readonly_observations_path": state.get("readonly_observations_path"),
            "readonly_observations_sha256": observation_sha256,
            "readonly_observation_count": state.get("readonly_observation_count", 0),
            "readonly_observations": observation_rows,
            "worker_error": state.get("error"), "worker_finished": bool(thread and not thread.is_alive()),
            "remote_temp_cleanup": cleanup})
        return proof

    def trace_event_enable_snapshot(self) -> dict:
        base = "/sys/kernel/debug/tracing/events"
        candidates = (
            "sched/sched_switch", "sched/sched_wakeup", "sched/sched_wakeup_new",
            "sched/sched_blocked_reason", "kmem/rss_stat", "mm/rss_stat")
        script = "for e in " + " ".join(quote(item) for item in candidates) + "; do " \
                 "p=" + quote(base) + "/$e/enable; " \
                 "if test -r \"$p\"; then v=$(cat \"$p\" 2>/dev/null); echo \"$e=$v\"; fi; done"
        raw = self.device.shell(script, check=False)
        states = {}
        for line in raw.splitlines():
            key, sep, value = line.partition("=")
            if sep and value.strip() in ("0", "1"):
                states[key.strip()] = int(value.strip())
        return {"raw": raw, "states": states, "candidate_events": list(candidates)}

    def trace_control_snapshot(self) -> dict:
        values = {}
        for name, path in (("tracing_on", "/sys/kernel/debug/tracing/tracing_on"),
                           ("buffer_size_kb", "/sys/kernel/debug/tracing/buffer_size_kb")):
            values[name] = self.device.shell("cat " + quote(path), check=False).strip()
        return values

    def restore_trace_control_snapshot(self, before: dict) -> dict:
        after = self.trace_control_snapshot()
        writes = []
        paths = {"tracing_on": "/sys/kernel/debug/tracing/tracing_on",
                 "buffer_size_kb": "/sys/kernel/debug/tracing/buffer_size_kb"}
        for name, path in paths.items():
            original = str(before.get(name, "")).strip()
            current = str(after.get(name, "")).strip()
            if original and original.isdigit() and current != original:
                output = self.device.shell("echo " + original + " > " + quote(path) +
                                           "; cat " + quote(path), check=False)
                writes.append({"setting": name, "restored_to": original,
                               "readback": output.strip()})
        final = self.trace_control_snapshot()
        restored = all(not str(before.get(name, "")).strip() or
                       str(final.get(name, "")).strip() == str(before.get(name, "")).strip()
                       for name in paths)
        return {"before": before, "after_hitrace_finish": after,
                "restore_writes": writes, "final_readback": final, "restored": restored}

    def restore_trace_event_enable_snapshot(self, before: dict) -> dict:
        original = before.get("states", {})
        after = self.trace_event_enable_snapshot()
        changed = {key: value for key, value in original.items()
                   if key in after["states"] and after["states"][key] != value}
        writes = []
        base = "/sys/kernel/debug/tracing/events"
        for event, value in changed.items():
            path = base + "/" + event + "/enable"
            output = self.device.shell("echo " + str(value) + " > " + quote(path) +
                                       "; cat " + quote(path), check=False)
            writes.append({"event": event, "restored_to": value, "readback": output.strip()})
        final = self.trace_event_enable_snapshot()
        restored = all(final["states"].get(key) == value for key, value in original.items()
                       if key in final["states"])
        return {"before": before, "after_hitrace_finish": after,
                "restore_writes": writes, "final_readback": final, "restored": restored}

    def start_trace(self, trace_name: str = "trace.ftrace", buffer_kb: int = 65536,
                    categories: tuple[str, ...] = ("app", "ability", "sched", "memory", "memreclaim", "window")) -> None:
        control_before = self.trace_control_snapshot()
        buffer_before = control_before.get("buffer_size_kb", "")
        event_enable_before = self.trace_event_enable_snapshot()
        output = self.device.shell("hitrace --trace_begin -b " + str(buffer_kb) + " " + " ".join(categories))
        stem = Path(trace_name).stem
        (self.output / f"trace_start_{stem}.log").write_text(output, encoding="utf-8")
        buffer_after = self.trace_control_snapshot().get("buffer_size_kb", "")
        event_enable_during = self.trace_event_enable_snapshot()
        self.trace_segment_name = trace_name
        self.trace_segments[trace_name] = {"trace_name": trace_name,
            "requested_buffer_kb": buffer_kb, "categories": list(categories),
            "tracefs_buffer_before_kb": buffer_before,
            "tracefs_buffer_during_kb": buffer_after,
            "trace_control_before": control_before,
            "trace_event_enable_before": event_enable_before,
            "trace_event_enable_during": event_enable_during,
            "overwrite_policy": "see matching trace_start_<segment>.log device-reported setting",
            "overrun_statistics": "not exposed by device output", "expected_markers": []}
        self.result["hitrace_capture_segments"] = self.trace_segments
        self.result["hitrace_overrun_statistics"] = "not exposed by device output"
        if re.search(r"\b(fail|failed|error)\b", output, re.I):
            self.result["invalid_reasons"].append("trace_start_failed")
        else:
            self.trace_started = True

    def finish_trace(self) -> None:
        if not self.trace_started or not self.trace_segment_name:
            return
        trace_name = self.trace_segment_name
        state = self.trace_segments[trace_name]
        remote_trace = self.remote + "/" + trace_name
        trace_path = self.output / trace_name
        try:
            text = self.device.shell("hitrace --trace_finish -o " + quote(remote_trace), timeout=60)
            (self.output / f"trace_finish_{Path(trace_name).stem}.log").write_text(text, encoding="utf-8")
            self.device.receive(remote_trace, trace_path)
            state["raw_file_bytes"] = trace_path.stat().st_size
            state["trace_control_restore"] = self.restore_trace_control_snapshot(
                state.get("trace_control_before", {}))
            state["tracefs_buffer_after_finish_kb"] = state["trace_control_restore"].get(
                "final_readback", {}).get("buffer_size_kb", "")
            if not state["trace_control_restore"].get("restored"):
                self.result["measurement_evidence_complete"] = False
                self.result.setdefault("evidence_gaps", []).append({
                    "source": "hitrace_control_restore", "trace": trace_name,
                    "reason": "pre-trace tracing_on or buffer size was not restored"})
            state["trace_event_enable_restore"] = self.restore_trace_event_enable_snapshot(
                state.get("trace_event_enable_before", {}))
            if not state["trace_event_enable_restore"].get("restored"):
                self.result["measurement_evidence_complete"] = False
                self.result.setdefault("evidence_gaps", []).append({
                    "source": "hitrace_event_enable_restore", "trace": trace_name,
                    "reason": "pre-trace event enable state was not restored"})
            trace = trace_path.read_text(encoding="utf-8", errors="replace")
            missing = [self.id + "_" + re.sub(r"[^a-zA-Z0-9_/-]", "_", name)
                       for name in state["expected_markers"]
                       if self.id + "_" + re.sub(r"[^a-zA-Z0-9_/-]", "_", name) not in trace]
            state["marker_coverage"] = {"expected": len(state["expected_markers"]),
                                        "present": len(state["expected_markers"]) - len(missing),
                                        "missing": missing}
            expected_begin = next((name for name in state["expected_markers"]
                                   if name.startswith("measurement_begin_")), None)
            expected_end = next((name for name in state["expected_markers"]
                                 if name.startswith("measurement_end_")), None)
            device_markers = {row.get("name"): row.get("device_uptime_seconds")
                              for row in self.markers}
            trace_marker_times = {}
            for line in trace.splitlines():
                match = re.search(r"\s(\d+\.\d+):\s+tracing_mark_write:\s+[BE]\|0\|memscenes/([^\s]+)", line)
                if match:
                    name = match.group(2)
                    if name == self.id + "_" + (expected_begin or ""):
                        trace_marker_times["begin"] = float(match.group(1))
                    if name == self.id + "_" + (expected_end or ""):
                        trace_marker_times["end"] = float(match.group(1))
            clock_values = (device_markers.get(expected_begin), device_markers.get(expected_end),
                            trace_marker_times.get("begin"), trace_marker_times.get("end"))
            if expected_begin and expected_end and all(value is not None for value in clock_values):
                device_begin, device_end, trace_begin, trace_end = clock_values
                drift = (trace_end - trace_begin) - (device_end - device_begin)
                state["clock_mapping"] = {"valid": abs(drift) <= 1.0,
                    "device_begin_uptime_seconds": device_begin,
                    "device_end_uptime_seconds": device_end,
                    "trace_begin_seconds": trace_begin, "trace_end_seconds": trace_end,
                    "device_uptime_minus_trace_offset_seconds": device_begin - trace_begin,
                    "duration_drift_seconds": drift}
                if abs(drift) > 1.0:
                    self.result["measurement_evidence_complete"] = False
                    self.result.setdefault("evidence_gaps", []).append({
                        "source": "hitrace_clock_mapping", "trace": trace_name,
                        "reason": "measurement begin/end clock drift exceeds tolerance",
                        "drift_seconds": drift})
            else:
                state["clock_mapping"] = {"valid": False, "reason": "measurement markers unavailable in both clocks"}
                if expected_begin or expected_end:
                    self.result["measurement_evidence_complete"] = False
                    self.result.setdefault("evidence_gaps", []).append({
                        "source": "hitrace_clock_mapping", "trace": trace_name,
                        "reason": "measurement markers unavailable in both clocks"})
            if missing:
                self.result.setdefault("evidence_gaps", []).append({
                    "source": "hitrace", "trace": trace_name,
                    "reason": "one or more segment phase markers missing", "missing_markers": missing})
                self.result.setdefault("trace_segment_coverage", {})[trace_name] = "partial"
                self.result["measurement_evidence_complete"] = False
            else:
                self.result.setdefault("trace_segment_coverage", {})[trace_name] = "complete"
            if self.result.get("process_lifecycle_trace", {}).get("coverage") == "awaiting_finalized_trace":
                self._parse_process_lifecycle_raw(trace, trace_path)
        except Exception as exc:
            self.result.setdefault("evidence_collection_errors", []).append({
                "source": "hitrace", "trace": trace_name, "error": str(exc)})
            self.result["invalid_reasons"].append("trace_finish:" + str(exc))
            self.result["measurement_evidence_complete"] = False
        else:
            self.trace_started = False
            self.trace_segment_name = None

    def start_process_lifecycle_trace(self, target_bundles: list[str] | None = None,
                                      baseline: dict | None = None) -> None:
        """Enable sched fork/exit for a measured window after hitrace has started."""
        if not self.trace_started:
            self.result["process_lifecycle_trace"] = {"coverage": "unavailable",
                "reason": "hitrace did not start"}
            self.result["measurement_evidence_complete"] = False
            return
        base = "/sys/kernel/debug/tracing"
        name = "memscenes_lifecycle_" + self.id
        instance = base + "/instances/" + name
        self.device.shell("mkdir " + quote(instance), check=False)
        if self.device.shell("test -d " + quote(instance) + " && echo exists", check=False).strip():
            self.lifecycle_trace_instance = instance
            self.lifecycle_trace_instance_name = name
            self.lifecycle_trace_instance_created = True
        root = self.lifecycle_trace_instance or base
        enabled = {}
        for event in ("sched_process_fork", "sched_process_exit"):
            path = root + f"/events/sched/{event}/enable"
            previous = self.device.shell("cat " + quote(path), check=False).strip()
            if previous not in ("0", "1"):
                enabled[event] = {"available": False, "readback": previous}
                continue
            if self.lifecycle_trace_instance is None:
                self.lifecycle_trace_global_previous[path] = previous
            self.device.shell("echo 1 > " + quote(path), check=False)
            readback = self.device.shell("cat " + quote(path), check=False).strip()
            enabled[event] = {"available": True, "previous": previous,
                              "enabled_readback": readback, "enabled": readback == "1"}
        if self.lifecycle_trace_instance:
            instance_previous_tracing_on = self.device.shell(
                "cat " + quote(root + "/tracing_on"), check=False).strip()
        else:
            instance_previous_tracing_on = None
        # Hitrace already owns the global trace clock. Only turn tracing on in
        # our private instance; toggling the global switch could disrupt it.
        if self.lifecycle_trace_instance:
            self.device.shell("echo 1 > " + quote(root + "/tracing_on"), check=False)
        running = self.device.shell("cat " + quote(root + "/tracing_on"), check=False).strip()
        self.lifecycle_trace_active = running == "1" and all(
            enabled.get(event, {}).get("enabled") for event in ("sched_process_fork", "sched_process_exit"))
        self.lifecycle_target_bundles = list(target_bundles or [])
        self.lifecycle_baseline = baseline
        try:
            begin_uptime = float(self.device.shell("cat /proc/uptime").split()[0])
        except Exception:
            begin_uptime = None
        self.result["process_lifecycle_trace"] = {"mode": "isolated_instance" if self.lifecycle_trace_instance else "global_events_restored",
            "instance": self.lifecycle_trace_instance_name, "events": enabled,
            "instance_previous_tracing_on": instance_previous_tracing_on,
            "tracing_on_readback": running, "available": self.lifecycle_trace_active,
            "scope": "reuse_baseline_through_reuse_end" if baseline is not None else "unscoped_diagnostic_window",
            "begin_uptime_seconds": begin_uptime,
            "coverage": "pending_collection"}
        if not self.lifecycle_trace_active:
            self.result["measurement_evidence_complete"] = False

    def stop_process_lifecycle_trace(self, ending: dict | None = None) -> None:
        state = self.result.setdefault("process_lifecycle_trace", {})
        was_active = self.lifecycle_trace_active
        if not was_active:
            state.setdefault("coverage", "unavailable")
        root = self.lifecycle_trace_instance or "/sys/kernel/debug/tracing"
        try:
            if was_active:
                try:
                    state["end_uptime_seconds"] = float(self.device.shell("cat /proc/uptime").split()[0])
                except Exception as exc:
                    state["end_uptime_seconds"] = None
                    state["end_clock_error"] = str(exc)
                if self.lifecycle_trace_instance:
                    self.device.shell("echo 0 > " + quote(root + "/tracing_on"), check=False)
                # Hitrace owns the global trace buffer on this device. Do not
                # read /tracing/trace while it is active: stdout filtering can
                # block behind a large, continuously-written buffer. The
                # finalized host-side hitrace file is parsed in cleanup().
                self.lifecycle_trace_ending = ending
                state.update({"capture_source": "finalized_hitrace_trace_file",
                              "coverage": "awaiting_finalized_trace"})
        except Exception as exc:
            state.update({"coverage": "collection_failed", "collection_error": str(exc)})
            self.result["measurement_evidence_complete"] = False
            self.result.setdefault("evidence_gaps", []).append({
                "source": "process_lifecycle_trace", "reason": "enabled lifecycle window could not be collected"})
        finally:
            if self.lifecycle_trace_instance:
                for event in ("sched_process_fork", "sched_process_exit"):
                    previous = state.get("events", {}).get(event, {}).get("previous", "0")
                    path = root + f"/events/sched/{event}/enable"
                    self.device.shell("echo " + str(previous) + " > " + quote(path), check=False)
                    readback = self.device.shell("cat " + quote(path), check=False).strip()
                    state.setdefault("restored_instance_enable", {})[event] = readback
                    if readback != str(previous):
                        self.result["invalid_reasons"].append("process_lifecycle_trace_restore_failed")
            else:
                restore = {}
                for path, previous in self.lifecycle_trace_global_previous.items():
                    self.device.shell("echo " + previous + " > " + quote(path), check=False)
                    restore[path.rsplit("/", 2)[-2]] = self.device.shell("cat " + quote(path), check=False).strip()
                state["restored_global_enable"] = restore
                if any(restore.get(event) != previous for path, previous in self.lifecycle_trace_global_previous.items()
                       for event in (path.rsplit("/", 2)[-2],)):
                    self.result["invalid_reasons"].append("process_lifecycle_trace_restore_failed")
            if self.lifecycle_trace_instance:
                self.device.shell("rmdir " + quote(root), check=False)
                exists = self.device.shell("test -d " + quote(root) + " && echo exists", check=False).strip()
                state["instance_removed"] = not bool(exists)
                if exists:
                    self.result["invalid_reasons"].append("process_lifecycle_trace_instance_cleanup_failed")
            state["capture_collected"] = was_active and state.get("coverage") == "enabled_window_collected"
            self.lifecycle_trace_active = False

    def start_kernel_dmesg_reader(self) -> None:
        """Keep an owned dmesg -W reader alive for this session's case markers."""
        backend = self.config.get("scene4", {}).get("kernel_capture_backend", "hdc_stream") if self.scene == 4 else "hdc_stream"
        if backend == "device_local":
            if self.config["scene4"].get("execution_profile") != "legacy_peak_transition":
                raise ValueError("device-local capture currently requires legacy Scene4 profile")
            segment = len(self.kernel_dmesg_reader.setdefault("segments", []))
            process = DeviceDmesgReader(self.device, self.remote, segment)
            self.kernel_dmesg_process = process
            row = {"index": segment, "started": process.poll() is None,
                   "device_pid": process.pid, "start_ticks": process.start_ticks,
                   "backend": backend, "remote_command": "nohup dmesg -W",
                   "remote_raw": process.raw_path, "remote_stderr": process.stderr_path,
                   "stderr": str(self.output / f"kernel_dmesg_reader_{segment}.stderr.log")}
            self.kernel_dmesg_reader["segments"].append(row)
            self.kernel_dmesg_reader.update(row)
            if not row["started"]:
                raise RuntimeError("device-local kernel reader exited before case start")
            return
        if backend != "hdc_stream":
            raise ValueError("unsupported kernel capture backend")
        self.kernel_dmesg_stream = self.kernel_dmesg_path.open("ab", buffering=0)
        segment = len(self.kernel_dmesg_reader.setdefault("segments", []))
        stderr_path = self.output / f"kernel_dmesg_reader_{segment}.stderr.log"
        self.kernel_dmesg_stderr = stderr_path.open("wb", buffering=0)
        try:
            self.kernel_dmesg_process = subprocess.Popen(
                self.device.base + ["shell", "dmesg -W"], stdout=self.kernel_dmesg_stream,
                stderr=self.kernel_dmesg_stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            self.kernel_dmesg_stream.close()
            self.kernel_dmesg_stderr.close()
            self.kernel_dmesg_stream = self.kernel_dmesg_stderr = None
            raise
        time.sleep(0.5)
        alive = self.kernel_dmesg_process.poll() is None
        segment_row = {"index": segment, "started": alive, "host_pid": self.kernel_dmesg_process.pid,
                       "remote_command": "dmesg -W", "stderr": str(stderr_path),
                       "return_code_at_start": self.kernel_dmesg_process.poll()}
        self.kernel_dmesg_reader["segments"].append(segment_row)
        self.kernel_dmesg_reader.update({"started": alive, "host_pid": self.kernel_dmesg_process.pid,
                                         "remote_command": "dmesg -W", "stderr": str(stderr_path)})
        if not alive:
            self.result["measurement_evidence_complete"] = False
            raise RuntimeError("owned dmesg -W reader exited before case start")

    def ensure_kernel_dmesg_reader(self, case_id: str) -> bool:
        """Restart only between cases; a mid-case reader gap remains a hard evidence gap."""
        process = self.kernel_dmesg_process
        if process is not None and process.poll() is None:
            self.kernel_dmesg_reader.setdefault("health_checks", []).append({
                "case_id": case_id, "alive_before_begin": True, "host_pid": process.pid})
            return True
        exit_code = process.poll() if process is not None else None
        self.kernel_dmesg_reader.setdefault("health_checks", []).append({
            "case_id": case_id, "alive_before_begin": False, "previous_return_code": exit_code,
            "restarted_between_cases": True})
        if self.kernel_dmesg_stream and not self.kernel_dmesg_stream.closed:
            self.kernel_dmesg_stream.close()
        if self.kernel_dmesg_stderr and not self.kernel_dmesg_stderr.closed:
            self.kernel_dmesg_stderr.close()
        try:
            self.start_kernel_dmesg_reader()
        except Exception as exc:
            self.kernel_dmesg_reader["health_checks"][-1].update({"restart_error": str(exc)})
            self.result["measurement_evidence_complete"] = False
            return False
        self.kernel_dmesg_reader["health_checks"][-1].update({
            "restart_succeeded": True, "new_host_pid": self.kernel_dmesg_process.pid})
        return True

    def start_scene4_reader_health_monitor(self, case_id: str, run_result: dict) -> dict:
        """Poll the owned dmesg reader during one Scene4 case; never restart mid-case."""
        process = self.kernel_dmesg_process
        if process is None:
            raise RuntimeError("Scene4 dmesg reader handle is missing at case begin")
        alive_at_begin = process.poll() is None
        stop = threading.Event()
        failed = threading.Event()
        state = {"case_id": case_id, "host_pid": process.pid, "started_host_monotonic_ns": time.monotonic_ns(),
                 "verified_alive_at_begin": alive_at_begin, "failed": failed, "stop": stop, "thread": None,
                 "failure_record": None}

        def watch() -> None:
            while not stop.wait(0.25):
                return_code = process.poll()
                if return_code is None:
                    continue
                failed.set()
                ended_ns = time.monotonic_ns()
                stderr_path = Path(self.kernel_dmesg_reader.get("stderr", ""))
                stderr_bytes = stderr_path.read_bytes() if stderr_path.is_file() else b""
                try:
                    self.kernel_dmesg_stream.flush()
                    os.fsync(self.kernel_dmesg_stream.fileno())
                except Exception:
                    pass
                raw_bytes = self.kernel_dmesg_path.read_bytes() if self.kernel_dmesg_path.is_file() else b""
                device_probe = {"attempted": True, "online": False, "boot_id": None}
                try:
                    probe = subprocess.run(self.device.base + ["shell",
                        "cat /proc/sys/kernel/random/boot_id"], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, timeout=8, check=False)
                    probe_text = probe.stdout.decode("utf-8", errors="replace").strip()
                    device_probe.update(return_code=probe.returncode, output=probe_text,
                        online=probe.returncode == 0 and bool(probe_text),
                        boot_id=probe_text if probe.returncode == 0 and re.fullmatch(
                            r"[0-9a-fA-F-]{32,40}", probe_text) else None)
                except Exception as exc:
                    device_probe["error"] = str(exc)
                record = {"schema": "scene4-reader-death-v1", "case_id": case_id,
                    "reader_host_pid": process.pid, "return_code": return_code,
                    "detected_host_monotonic_ns": ended_ns,
                    "stderr_path": str(stderr_path), "stderr_bytes": len(stderr_bytes),
                    "stderr_text": stderr_bytes.decode("utf-8", errors="replace"),
                    "dmesg_raw_path": str(self.kernel_dmesg_path),
                    "dmesg_raw_bytes": len(raw_bytes),
                    "dmesg_raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    "dmesg_raw_tail_hex": raw_bytes[-256:].hex(),
                    "hdc_device_probe": device_probe,
                    "case_valid": False, "counts": None,
                    "midcase_restart_attempted": False}
                state["failure_record"] = record
                run_result.setdefault("reader_health_failures", []).append(record)
                run_result["measurement_complete"] = False
                run_result["invalid_reasons"] = list(run_result.get("invalid_reasons", []))
                run_result["invalid_reasons"].append(f"scene4_kernel_reader_died_midcase:{case_id}")
                checkpoint_path = self.output / f"scene4_reader_death_{case_id}.json"
                atomic_save(checkpoint_path, record)
                record["death_checkpoint_path"] = str(checkpoint_path)
                state["failure_record"] = record
                self.kernel_dmesg_reader.setdefault("health_checks", []).append(record)
                self.result["scene4_kernel_reader_health"] = self.kernel_dmesg_reader
                atomic_save(checkpoint_path, record)
                return

        thread = threading.Thread(target=watch, name=f"scene4-reader-health-{case_id}", daemon=True)
        state["thread"] = thread
        self.scene4_reader_monitors[case_id] = state
        thread.start()
        if not alive_at_begin:
            # A death between BEGIN marker and monitor startup is still a
            # case-boundary health failure; allow one poll period to persist it.
            thread.join(timeout=0.75)
        return state

    def stop_scene4_reader_health_monitor(self, case_id: str) -> dict:
        state = self.scene4_reader_monitors.get(case_id)
        if not state:
            return {"case_id": case_id, "stopped": True, "monitor_missing": True}
        state["stop"].set()
        thread = state.get("thread")
        if thread:
            thread.join(timeout=10)
        return {"case_id": case_id, "host_pid": state.get("host_pid"),
            "verified_alive_at_begin": state.get("verified_alive_at_begin"),
            "monitor_stopped": bool(thread and not thread.is_alive()),
            "reader_alive_at_end": bool(self.kernel_dmesg_process and
                self.kernel_dmesg_process.poll() is None),
            "failure": state.get("failure_record")}

    def mark_kernel_dmesg_boundary(self, case_id: str, phase: str,
                                   uptime: float, wall_ms: int) -> dict:
        if phase not in ("BEGIN", "END"):
            raise ValueError("kernel dmesg boundary must be BEGIN or END")
        if not self.kernel_dmesg_process or self.kernel_dmesg_process.poll() is not None:
            row = {"case_id": case_id, "phase": phase, "written": False,
                   "reason": "reader_not_alive", "reader_return_code": (
                       self.kernel_dmesg_process.poll() if self.kernel_dmesg_process else None)}
        else:
            payload = (f"MEMSCENES_CASE_{phase} session={self.id} case_id={case_id} "
                       f"uptime_ms={int(uptime * 1000)} wall_ms={int(wall_ms)}")
            try:
                output = self.device.shell("echo " + quote(payload) + " > /dev/kmsg", timeout=3, check=False)
                row = {"case_id": case_id, "phase": phase, "written": True,
                       "uptime_seconds": uptime, "wall_ms": wall_ms,
                       "payload": payload, "command_output": output[:300]}
            except Exception as exc:
                row = {"case_id": case_id, "phase": phase, "written": False,
                       "uptime_seconds": uptime, "wall_ms": wall_ms, "error": str(exc)}
        self.result.setdefault("kernel_dmesg_markers", []).append(row)
        if not row["written"]:
            self.result["measurement_evidence_complete"] = False
        return row

    def mark_scene4_kernel_dmesg_boundary(self, case_id: str, phase: str) -> dict:
        """Write a same-process CLOCK_BOOTTIME marker and verify exact write readback."""
        if phase not in ("BEGIN", "END"):
            raise ValueError("kernel dmesg boundary must be BEGIN or END")
        # This also verifies the installed helper hash before it is used.
        _scene4_clock_probe(self)
        path = str(self.config["scene4"]["oracle_clock_probe_remote_path"])
        started = time.monotonic()
        raw = self.device.shell(" ".join(map(quote, [path, "--mark", self.id, case_id, phase])),
                                timeout=10, check=False)
        completed = time.monotonic()
        values = _scene4_parse_marker_helper_output(raw)
        try:
            payload = values["marker_payload"]
            write_len = int(values["marker_write_len"])
            write_errno = int(values["marker_write_errno"])
            boottime_ns = int(values["marker_boottime_ns"])
            realtime_ms = int(values["marker_realtime_ms"])
            post_boottime_ns = int(values["marker_post_boottime_ns"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"scene4 same-process marker output incomplete: {raw!r}") from exc
        expected_prefix = f"MEMSCENES_CASE_{phase} session={self.id} case_id={case_id} "
        valid = (write_errno == 0 and write_len == len(payload.encode("utf-8")) and
                 payload.startswith(expected_prefix) and "uptime_ms=" in payload and
                 "boottime_ns=" in payload and post_boottime_ns >= boottime_ns)
        row = {"case_id": case_id, "phase": phase, "written": valid,
            "session_id": self.id,
            "marker_boottime_ns": boottime_ns, "boottime_seconds": boottime_ns / 1e9,
            "realtime_ms": realtime_ms, "write_len": write_len, "write_errno": write_errno,
            "post_boottime_ns": post_boottime_ns, "marker_payload": payload,
            "host_started_monotonic": started, "host_completed_monotonic": completed,
            "host_duration_seconds": completed - started, "helper_raw": raw,
            "reason": None if valid else "write_length_errno_or_payload_validation_failed"}
        self.result.setdefault("kernel_dmesg_markers", []).append(row)
        if not valid:
            self.result["measurement_evidence_complete"] = False
            raise RuntimeError(f"scene4 same-process dmesg marker write validation failed: {row}")
        return row

    def collect_kernel_dmesg_case(self, case_id: str, observed: dict[int, list[dict]],
                                  targets: list[str], attempts: dict[str, list[dict]],
                                  start_uptime: float, end_uptime: float,
                                  anchor_wall_ms: int, anchor_uptime: float, *,
                                  scene4_cold_only: bool = False,
                                  scene4_peak_transition: bool = False,
                                  daily_attempt_rows: dict[str, list[dict]] | None = None,
                                  daily_journal_readback_complete_bundles: set[str] | None = None,
                                  journal_readback_complete_bundles: set[str] | None = None,
                                  journal_case_ids: dict[str, str] | None = None,
                                  startup_failure_bundles: set[str] | None = None) -> dict:
        path = self.kernel_dmesg_path
        end_token = f"MEMSCENES_CASE_END session={self.id} case_id={case_id} "
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if isinstance(self.kernel_dmesg_process, DeviceDmesgReader):
                self.kernel_dmesg_process.collect(path, Path(self.kernel_dmesg_reader["stderr"]))
            if path.exists() and end_token in path.read_text(encoding="utf-8", errors="replace"):
                break
            if not self.kernel_dmesg_process or self.kernel_dmesg_process.poll() is not None:
                break
            time.sleep(0.1)
        raw = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        attempt_path = self.output / "kernel_dmesg" / f"{case_id}.json"
        attempt_path.parent.mkdir(parents=True, exist_ok=True)
        targets_uid = {bundle: self.device.app(bundle)["uid"] for bundle in targets}
        info = self.book.kernel_dmesg_oom_victims(
            raw, case_id, observed, targets_uid, attempts, start_uptime, end_uptime,
            anchor_wall_ms, anchor_uptime, str(path.relative_to(self.output)),
            scene4_cold_only=scene4_cold_only,
            scene4_peak_transition=scene4_peak_transition,
            daily_attempt_rows=daily_attempt_rows,
            daily_journal_readback_complete_bundles=daily_journal_readback_complete_bundles,
            journal_readback_complete_bundles=journal_readback_complete_bundles,
            journal_case_ids=journal_case_ids,
            startup_failure_bundles=startup_failure_bundles)
        info.update({"raw_path": str(path.relative_to(self.output)),
                     "raw_size_bytes": path.stat().st_size if path.exists() else 0,
                     "reader_alive_at_collection": bool(self.kernel_dmesg_process and
                         self.kernel_dmesg_process.poll() is None)})
        save(attempt_path, info)
        self.result.setdefault("kernel_dmesg_observations", []).append(
            {"case_id": case_id, **info, "attempt_path": str(attempt_path.relative_to(self.output))})
        if not info.get("coverage_complete"):
            self.result["measurement_evidence_complete"] = False
            self.result.setdefault("case_evidence_gaps", []).append({
                "case_id": case_id, "reason": "kernel_dmesg_marker_or_clock_coverage_incomplete",
                "details": info.get("reason")})
        return info

    def stop_kernel_dmesg_reader(self) -> None:
        process = self.kernel_dmesg_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if isinstance(process, DeviceDmesgReader):
            process.collect(self.kernel_dmesg_path, Path(self.kernel_dmesg_reader["stderr"]))
        if self.kernel_dmesg_stream and not self.kernel_dmesg_stream.closed:
            self.kernel_dmesg_stream.close()
        if self.kernel_dmesg_stderr and not self.kernel_dmesg_stderr.closed:
            self.kernel_dmesg_stderr.close()
        self.kernel_dmesg_reader.update({"ended": True,
            "return_code": process.returncode if process else None,
            "raw_size_bytes": self.kernel_dmesg_path.stat().st_size if self.kernel_dmesg_path.exists() else 0,
            "reader_stopped": process is None or process.poll() is not None})
        self.result["kernel_dmesg_reader"] = self.kernel_dmesg_reader
        if not self.kernel_dmesg_reader["reader_stopped"]:
            self.result["measurement_evidence_complete"] = False

    def enable_oom_trace(self) -> None:
        instance_name = "memscenes_" + self.id
        instance = "/sys/kernel/debug/tracing/instances/" + instance_name
        self.device.shell("mkdir " + quote(instance), check=False)
        if self.device.shell("test -d " + quote(instance) + " && echo exists", check=False).strip():
            self.oom_trace_instance_created = True
            self.oom_trace_instance_name = instance_name
        instance_event = instance + "/events/oom/mark_victim/enable"
        instance_enabled = self.device.shell("echo 1 > " + quote(instance_event) + "; cat " + quote(instance_event), check=False).strip()
        instance_running = self.device.shell("echo 1 > " + quote(instance + "/tracing_on") + "; cat " + quote(instance + "/tracing_on"), check=False).strip()
        if instance_enabled == "1" and instance_running == "1":
            self.oom_trace_instance = instance
            self.device.shell("echo 2048 > " + quote(instance + "/buffer_size_kb"), check=False)
            fmt = self.device.shell("cat " + quote(instance + "/events/oom/mark_victim/format"), check=False)
            (self.output / "oom_mark_victim.format").write_text(fmt, encoding="utf-8")
            self.result["oom_trace"] = {"event": "oom:mark_victim", "mode": "isolated_tracefs_instance",
                                        "available": True, "instance": instance_name,
                                        "enable_readback": instance_enabled,
                                        "tracing_on": instance_running, "buffer_size_kb": "2048"}
            return
        path = "/sys/kernel/debug/tracing/events/oom/mark_victim/enable"
        previous = self.device.shell("cat " + quote(path), check=False).strip()
        if previous in ("0", "1"):
            self.oom_trace_previous = previous
            self.device.shell("echo 1 > " + quote(path), check=False)
            enabled = self.device.shell("cat " + quote(path), check=False).strip()
            fmt = self.device.shell("cat /sys/kernel/debug/tracing/events/oom/mark_victim/format", check=False)
            (self.output / "oom_mark_victim.format").write_text(fmt, encoding="utf-8")
            self.result["oom_trace"] = {"event": "oom:mark_victim", "mode": "global_event",
                                        "available": "name: mark_victim" in fmt,
                                        "previous_enable": previous, "enable_readback": enabled,
                                        "format_evidence": "oom_mark_victim.format"}
        else:
            self.result["oom_trace"] = {"event": "oom:mark_victim", "available": False,
                                        "readback": previous}

    def read_kernel_trace(self, case_id: str, observed: dict[int, dict],
                          start_uptime: float, end_uptime: float) -> None:
        trace_root = self.oom_trace_instance or "/sys/kernel/debug/tracing"
        raw = self.device.shell("cat " + quote(trace_root + "/trace"), timeout=15, check=False)
        path = self.output / "kernel_trace" / f"{case_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        self.book.kernel_oom_victims(raw, case_id, observed, start_uptime, end_uptime,
                                     str(path.relative_to(self.output)))
        stats = self.device.shell("cat " + quote(trace_root + "/per_cpu/cpu0/stats"), check=False)
        (self.output / "kernel_trace_stats.txt").write_text(stats, encoding="utf-8")
        written = re.search(r"entries-written:\s*(\d+)", stats)
        buffered = re.search(r"entries-in-buffer:\s*(\d+)", stats)
        truncated = (int(written[1]) > int(buffered[1])) if written and buffered else None
        self.result.setdefault("oom_trace_observations", []).append({"case_id": case_id,
            "entries_written": int(written[1]) if written else None,
            "entries_in_buffer": int(buffered[1]) if buffered else None,
            "truncated": truncated, "coverage_complete": truncated is False,
            "trace_stats_raw": stats[:500], "victims_detected": sum(1 for row in self.book.events
                if row["case_id"] == case_id and row["category"] == "oom_kill")})
        self.result["oom_trace_observations"][-1]["auxiliary_trace_complete"] = truncated is False

    def snapshot(self, label: str, bundles: list[str]) -> dict:
        d = self.device
        snapshot_start = float(d.shell("cat /proc/uptime").split()[0])
        sample = {"label": label, "device_uptime_seconds": snapshot_start,
                  "snapshot_started_uptime_seconds": snapshot_start,
                  "meminfo": key_values(d.shell("cat /proc/meminfo")),
                  "vmstat": key_values(d.shell("cat /proc/vmstat")),
                  "disk_write_bytes": disk_bytes(d.shell("cat /sys/block/zram0/hyperhold_io_info_file", check=False)),
                  "apps": {}}
        for bundle in bundles:
            processes = d.app_processes(bundle)
            app_spec = d.app(bundle)
            cgroup = app_spec["cgroup"]
            raw = d.shell(
                "echo __MEMORY_CURRENT__; cat " + quote(cgroup + "/memory.current") +
                "; echo __MEMORY_USAGE__; cat " + quote(cgroup + "/memory.usage_in_bytes") +
                "; echo __MEMORY_STAT__; cat " + quote(cgroup + "/memory.stat") +
                "; echo __CGROUP_PROCS__; cat " + quote(cgroup + "/cgroup.procs"),
                timeout=15, check=False)
            def section(name: str) -> str:
                match = re.search(r"__" + re.escape(name) + r"__\s*\n(.*?)(?=\n__[A-Z_]+__\s*\n|\Z)", raw, re.S)
                return match.group(1).strip() if match else ""
            current_raw = section("MEMORY_CURRENT")
            usage_raw = section("MEMORY_USAGE")
            stat_raw = section("MEMORY_STAT")
            procs_raw = section("CGROUP_PROCS")
            def first_bytes(value: str) -> int | None:
                match = re.fullmatch(r"\s*(\d+)\s*", value)
                return int(match[1]) if match else None
            current_value = first_bytes(current_raw)
            current_source = "memory.current" if current_value is not None else None
            if current_value is None:
                current_value = first_bytes(usage_raw)
                if current_value is not None:
                    current_source = "memory.usage_in_bytes"
            cgroup_pids = sorted({int(line.strip()) for line in procs_raw.splitlines()
                                  if line.strip().isdigit()})
            app_sample = {"processes": processes, "memory_stat": key_values(stat_raw),
                          "eswap_bytes": eswap_bytes(stat_raw),
                          "cgroup_memory": {"uid": app_spec["uid"], "path": cgroup,
                              "current_bytes": current_value, "source": current_source,
                              "unavailable_reason": (None if current_value is not None else
                                  (current_raw or usage_raw or "both cgroup current files unavailable")),
                              "memory_current_raw": current_raw, "memory_usage_in_bytes_raw": usage_raw,
                              "cgroup_procs": cgroup_pids, "cgroup_procs_raw": procs_raw,
                              "process_identity_keys": sorted(processes)}}
            sample["apps"][bundle] = app_sample
            for identity, process in processes.items():
                for kind in ("minor", "major"):
                    marker = f"C|{process['pid']}|memscenes/{bundle}/{identity}/{kind}|{process[kind]}"
                    d.shell("echo " + quote(marker) + " > /sys/kernel/debug/tracing/trace_marker", check=False)
        sample["snapshot_finished_uptime_seconds"] = float(d.shell("cat /proc/uptime").split()[0])
        sample["snapshot_duration_seconds"] = (sample["snapshot_finished_uptime_seconds"] -
                                                sample["snapshot_started_uptime_seconds"])
        self.samples.append(sample)
        with (self.output / "samples.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
        return sample

    def start_sampler(self, bundles: list[str]) -> None:
        def loop():
            while not self.stop_event.wait(1):
                try:
                    self.snapshot("periodic", bundles)
                except Exception as exc:
                    self.result.setdefault("sampling_errors", []).append({
                        "source": "periodic_snapshot", "error": str(exc),
                        "classification": "unknown; process lifecycle/counter sample missing"})
                    self.result["measurement_evidence_complete"] = False
        self.sampler = threading.Thread(target=loop, daemon=True)
        self.sampler.start()

    def stop_sampler(self) -> None:
        self.stop_event.set()
        if self.sampler:
            self.sampler.join(timeout=90)
            if self.sampler.is_alive():
                self.result["invalid_reasons"].append("sampler_did_not_stop")

    def journal(self, bundle: str, case_id: str) -> list[dict]:
        raw, rows = self.device.journal(bundle, case_id)
        path = self.output / "journals" / f"{case_id}_{bundle}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        self.book.native_failures(rows, bundle, str(path.relative_to(self.output)), peak_only=True)
        return rows

    def wait_ready(self, bundle: str, case_id: str, timeout: float) -> list[dict]:
        deadline = time.monotonic() + timeout
        last = []
        while time.monotonic() < deadline:
            last = self.journal(bundle, case_id)
            if any(row.get("event") == "startup_ready" for row in last):
                return last
            if any(row.get("event") == "allocation_failed" for row in last):
                raise RuntimeError(f"{bundle} allocation_failed")
            if last and not self.device.app_processes(bundle):
                raise RuntimeError(f"{bundle} exited before readiness")
            time.sleep(0.5)
        raise TimeoutError(f"{bundle} startup readiness timeout")

    def launch_test(self, bundle: str, case_id: str, daily: int, peak: int, hold: int,
                    lifetime: int, file_mib: int = 0, seed: int = 1,
                    await_peak: bool = False, no_periodic_msync: bool = False) -> None:
        self.device.app(bundle)
        params = {"case_id": case_id, "daily_mib": daily,
                           "peak_mib": peak, "file_mib": file_mib, "hold_seconds": hold,
                           "lifetime_seconds": lifetime, "seed": seed, "bundle": bundle,
                           "daily_case_id": case_id + "_daily", "await_peak": int(await_peak)}
        if no_periodic_msync:
            params["no_periodic_msync"] = 1
        self.device.launch(bundle, params=params)

    def ui_evidence(self, case_id: str, targets: list[str], observation_phase: str = "unspecified",
                    allow_case_attribution: bool = True, scene4_fast_layout: bool = False) -> set[str]:
        capture_start = time.monotonic()
        light_inventory = None
        observation = {"case_id": case_id, "phase": observation_phase,
                       "host_monotonic": capture_start,
                       "capture_started_host_monotonic": capture_start,
                       "capture_completed_host_monotonic": None, "success": False,
                       "case_attribution_allowed": allow_case_attribution}
        try:
            if scene4_fast_layout:
                # Poll lightweight WMS/AA inventories first, then read only
                # TaskManager and the focused owned test window when present.
                snapshot_tag = f"{case_id}_sample_{time.monotonic_ns()}"
                layout_started = time.monotonic()
                light_inventory = _scene4_light_ui_inventory(self, snapshot_tag, targets)
                tree = light_inventory["tree"]
                layout_relpath = (light_inventory["targeted_layout_paths"][0]
                    if light_inventory["targeted_layout_paths"] else None)
                observation["layout_capture_duration_seconds"] = time.monotonic() - layout_started
                observation["layout_capture_mode"] = "WMS+AA inventory with targeted dumpLayout -m false -b bundle"
                observation["scene4_light_inventory"] = {key: value for key, value in light_inventory.items()
                    if key not in ("tree", "mission_raw", "wms_raw", "aa_raw")}
                observation["window_snapshot_captured"] = light_inventory["window_snapshot_captured"]
                observation["mission_snapshot_captured"] = light_inventory["mission_snapshot_captured"]
                observation["targeted_layout_paths"] = light_inventory["targeted_layout_paths"]
                path = self.output / layout_relpath if layout_relpath else None
                if layout_relpath is None:
                    inventory_path = self.output / "ui" / f"{snapshot_tag}_inventory.json"
                    save(inventory_path, observation["scene4_light_inventory"])
                    path = inventory_path
            else:
                tree = self.device.dump_ui(self.remote + "/layout.json", timeout=45)
                path = None
        except Exception as exc:
            observation["error"] = str(exc)
            observation["capture_completed_host_monotonic"] = time.monotonic()
            observation["capture_duration_seconds"] = observation["capture_completed_host_monotonic"] - capture_start
            self.ui_observations.setdefault(case_id, []).append(observation)
            self.result.setdefault("ui_evidence_errors", []).append({"case_id": case_id,
                "phase": observation_phase, "error": str(exc)})
            return set()
        if path is None:
            path = self.output / "ui" / f"{case_id}_{len(list((self.output / 'ui').glob('*.json'))) if (self.output / 'ui').exists() else 0}.json"
            save(path, tree)
        observation["layout"] = str(path.relative_to(self.output))
        observation["layout_captured"] = (bool(light_inventory and
            light_inventory["targeted_layout_captured"]) if scene4_fast_layout else True)
        capture_clock_sample = light_inventory.get("capture_clock") if light_inventory else None
        if scene4_fast_layout and capture_clock_sample:
            capture_uptime = capture_clock_sample.get("boottime_s")
            capture_wall_ms = int(capture_clock_sample["realtime_s"] * 1000) if capture_clock_sample.get("realtime_s") is not None else None
            observation.update(capture_boottime_seconds=capture_uptime,
                capture_monotonic_seconds=capture_clock_sample.get("monotonic_s"),
                capture_clock_mapping=capture_clock_sample, capture_wall_ms=capture_wall_ms)
        else:
            try:
                capture_uptime, capture_wall_ms = self.device.clock_pair()
                observation.update(capture_uptime_seconds=capture_uptime, capture_wall_ms=capture_wall_ms)
            except Exception as exc:
                observation["clock_error"] = str(exc)
                capture_uptime = capture_wall_ms = None
        mission_raw = light_inventory["mission_raw"] if light_inventory else ""
        if not scene4_fast_layout:
            try:
                mission_started = time.monotonic()
                mission_raw = self.device.shell("aa dump -l", timeout=10, check=False)
                observation["mission_capture_duration_seconds"] = time.monotonic() - mission_started
            except Exception as exc:
                mission_raw = ""
                observation["mission_error"] = str(exc)
                self.result.setdefault("ui_evidence_errors", []).append({"case_id": case_id,
                    "phase": observation_phase, "error": str(exc), "source": "mission_list"})
        mission_path = self.output / "ui" / f"{case_id}_{len(list((self.output / 'ui').glob('*.txt')))}_missions.txt"
        mission_path.write_text(mission_raw, encoding="utf-8")
        observation["mission_list"] = str(mission_path.relative_to(self.output))
        missions = parse_low_memory_missions(mission_raw, include_process_identity=scene4_fast_layout)
        baseline = self.popup_baselines.get(case_id, {})
        case_window_open = popup_attribution_in_window(
            allow_case_attribution, capture_wall_ms,
            baseline.get("case_begin_wall_ms"), baseline.get("case_end_ms"))
        candidate = None
        parse_started = time.monotonic()
        layout_nodes = _scene4_ui_nodes(tree) if scene4_fast_layout else list(ui_nodes(tree))
        native_visibility_proof = {"complete": True, "candidate_nodes_seen": False,
                                   "reason": "not_scene4"}
        if case_window_open:
            if scene4_fast_layout:
                candidate, native_visibility_proof = _scene4_native_popup_candidate(
                    self, layout_nodes, missions, set(baseline.get("mission_ids", [])),
                    baseline.get("case_begin_boottime_seconds"), capture_clock_sample,
                    baseline.get("case_end_boottime_seconds"), self.config["popup_text_patterns"])
            else:
                candidate = native_low_memory_popup_candidate(layout_nodes, missions,
                    set(baseline.get("mission_ids", [])),
                    baseline.get("case_begin_uptime_seconds"), capture_uptime)
        elif scene4_fast_layout:
            native_visibility_proof = {"complete": False, "candidate_nodes_seen": False,
                                       "reason": "capture_outside_case_wall_window"}
        if scene4_fast_layout and light_inventory is not None:
            native_visibility_proof["light_inventory"] = {
                "complete": light_inventory.get("complete"),
                "reason": light_inventory.get("reason"),
                "raw_paths": light_inventory.get("raw_paths"),
                "unknown_window_rows": light_inventory.get("unknown_window_rows"),
                "unknown_owner_rows": light_inventory.get("unknown_owner_rows"),
                "detail_errors": light_inventory.get("detail_errors"),
                "layout_errors": light_inventory.get("layout_errors"),
                "window_set_stable": light_inventory.get("window_set_stable"),
                "content_fresh_le_2_5s": light_inventory.get("content_fresh_le_2_5s")}
            native_visibility_proof["complete"] = bool(
                native_visibility_proof.get("complete") and light_inventory.get("complete"))
            if not light_inventory.get("complete") and not native_visibility_proof.get("reason"):
                native_visibility_proof["reason"] = light_inventory.get("reason")
        observation["native_candidate"] = candidate
        observation["native_candidate_nodes_seen"] = native_visibility_proof.get("candidate_nodes_seen", False)
        observation["native_window_visibility_complete"] = native_visibility_proof.get("complete", False)
        observation["native_window_visibility_proof"] = native_visibility_proof
        found = []
        for row in layout_nodes:
            text = " ".join(str(row.get(field, "")) for field in ("text", "content", "description"))
            if any(pattern.lower() in text.lower() for pattern in self.config["popup_text_patterns"]):
                found.append(row)
        focused = _scene4_focused_bundles(tree) if scene4_fast_layout else focused_bundles(tree)
        if scene4_fast_layout:
            observation["ui_candidate_parse_duration_seconds"] = time.monotonic() - parse_started
        screenshot_needed, found_visible_count, found_visibility_unknown_count = \
            _scene4_screenshot_needed(scene4_fast_layout, candidate, found)
        if scene4_fast_layout and light_inventory and light_inventory.get("unknown_visible_content_candidate"):
            # Preserve a visual artifact for visible content whose owner is
            # outside the trusted target/native set; it cannot be silently skipped.
            screenshot_needed = True
            observation["unknown_visible_content_screenshot_reason"] = True
        screenshot = path.with_suffix(".png")
        observation["screenshot_needed"] = screenshot_needed
        if scene4_fast_layout:
            observation["popup_text_nodes_seen"] = len(found)
            observation["popup_text_nodes_explicitly_visible"] = found_visible_count
            observation["popup_text_nodes_visibility_unknown"] = found_visibility_unknown_count
        if screenshot_needed:
            try:
                screenshot_started = time.monotonic()
                self.device.screenshot(self.remote + "/screen.png", screenshot)
                observation["screenshot_transfer_duration_seconds"] = time.monotonic() - screenshot_started
                observation["screenshot"] = str(screenshot.relative_to(self.output))
                observation["screenshot_captured"] = True
                if scene4_fast_layout:
                    screenshot_clock_started = time.monotonic()
                    screenshot_clock = _scene4_clock_probe(self)
                    observation["screenshot_clock_probe_duration_seconds"] = time.monotonic() - screenshot_clock_started
                    screenshot_uptime = screenshot_clock["boottime_s"]
                    screenshot_wall_ms = int(screenshot_clock["realtime_s"] * 1000)
                    observation["screenshot_complete_boottime_seconds"] = screenshot_uptime
                    observation["screenshot_complete_monotonic_seconds"] = screenshot_clock["monotonic_s"]
                    observation["screenshot_clock_mapping"] = screenshot_clock
                    observation["screenshot_complete_wall_ms"] = screenshot_wall_ms
                else:
                    screenshot_uptime, screenshot_wall_ms = self.device.clock_pair()
                    observation["screenshot_complete_uptime_seconds"] = screenshot_uptime
                    observation["screenshot_complete_wall_ms"] = screenshot_wall_ms
            except Exception as exc:
                observation["screenshot_error"] = str(exc)
                self.result.setdefault("ui_evidence_errors", []).append({"case_id": case_id,
                    "phase": observation_phase, "error": str(exc), "source": "screenshot"})
                screenshot_wall_ms = None
        else:
            observation["screenshot_captured"] = False
            screenshot_wall_ms = None
        screenshot_ok = not screenshot_needed or observation.get("screenshot_captured", False)
        screenshot_in_case = popup_attribution_in_window(
            allow_case_attribution and screenshot_ok, screenshot_wall_ms,
            baseline.get("case_begin_wall_ms"), baseline.get("case_end_ms"))
        observation["success"] = True
        observation["capture_clock_valid"] = capture_uptime is not None and capture_wall_ms is not None and (
            not scene4_fast_layout or bool(capture_clock_sample and
                self.result.get("scene4_clock_domain", {}).get("stable") is True and
                capture_clock_sample.get("trace_clock_selected") == "boot"))
        if case_window_open and screenshot_in_case and candidate and screenshot_ok:
            mission_id = candidate["mission_id"]
            window_ids = candidate["window_ids"]
            self.book.add("low_memory_popup_native", case_id, candidate["owner"], None,
                          int(capture_wall_ms or self.device.now_ms()), "system_ui_and_mission",
                          "visible native low-memory warning; new in-case TaskManager mission; "
                          f"ability={candidate['ability']} mission={mission_id} windows={window_ids} "
                          f"started_uptime={candidate['mission_start_uptime_seconds']} "
                          f"captured_uptime={candidate['capture_uptime_seconds']}",
                          str(screenshot.relative_to(self.output)),
                          f"{case_id}:popup:native:{mission_id}:{','.join(window_ids)}")
        for bundle in targets:
            owned = [row for row in found if row.get("__ownerBundleName") == bundle]
            if _scene4_test_popup_journal_needed(bundle, scene4_fast_layout, owned,
                                                 screenshot_in_case):
                journal_started = time.monotonic()
                rows = self.journal(bundle, case_id)
                if bundle.startswith("io.memscene.workload."):
                    rows += self.journal(bundle, case_id + "_daily")
                if scene4_fast_layout:
                    observation.setdefault("test_dialog_journal_reads", []).append({
                        "bundle": bundle, "duration_seconds": time.monotonic() - journal_started,
                        "owned_candidates": len(owned)})
            else:
                rows = []
            notifications = [row for row in rows if row.get("event") == "memory_notification" and
                             row.get("level") in (1, 2, 5, 6) and
                             row.get("case_id") in (case_id, case_id + "_daily") and
                             row.get("time_ms", 0) >= baseline.get("case_begin_wall_ms", 0) and
                             row.get("time_ms", 0) <= (capture_wall_ms or 0)]
            notifications.sort(key=lambda row: row.get("time_ms", 0))
            requested_rows = [row for row in rows if row.get("event") == "popup_requested" and
                              row.get("case_id") == case_id and
                              row.get("time_ms", 0) >= baseline.get("case_begin_wall_ms", 0) and
                              row.get("time_ms", 0) <= (capture_wall_ms or 0)]
            requested = bool(requested_rows)
            owned_visible = test_popup_is_unoccluded(
                bundle, case_id, owned, focused,
                visible_native_low_memory_modal(layout_nodes, self.config["popup_text_patterns"]))
            if (bundle.startswith("io.memscene.workload.") or _scene4_test_dialog_bundle(bundle)) and \
                    case_window_open and screenshot_in_case:
                if not notifications or not requested or not owned_visible or not screenshot_ok:
                    continue
                callback = notifications[-1]
                if callback.get("time_ms", 0) < baseline.get("case_begin_wall_ms", 0) or \
                        callback.get("time_ms", 0) > screenshot_wall_ms:
                    continue
                self.book.add("low_memory_popup_test", case_id, bundle, notifications[-1]["pid"],
                              int(screenshot_wall_ms), "ui_and_system_callback",
                              f"callback level={notifications[-1]['level']} case={notifications[-1]['case_id']} phase={notifications[-1]['phase']} callback_ms={notifications[-1]['time_ms']}; popup visible with request",
                              str(screenshot.relative_to(self.output)), f"{case_id}:popup:{bundle}")
        observation["focused_bundles"] = sorted(focused or [])
        observation["found_popup_nodes"] = len(found)
        observation["window_snapshot_captured"] = True
        observation["mission_snapshot_captured"] = True
        observation["capture_completed_host_monotonic"] = time.monotonic()
        observation["capture_duration_seconds"] = observation["capture_completed_host_monotonic"] - capture_start
        self.ui_observations.setdefault(case_id, []).append(observation)
        return focused

    def scene4_ui_evidence(self, case_id: str, targets: list[str],
                           observation_phase: str = "hold_window") -> set[str]:
        """Capture a real Scene4 window snapshot at the scheduled sample cadence."""
        return self.ui_evidence(case_id, targets, observation_phase=observation_phase,
                                scene4_fast_layout=True)

    def restore_native_low_memory_dialog(self, case_id: str) -> dict:
        """Close only the uniquely identified visible TaskManager warning X."""
        state = {"case_id": case_id, "status": "checking", "verified_absent": False}
        ui_dir = self.output / "ui"
        ui_dir.mkdir(parents=True, exist_ok=True)
        try:
            before = self.device.dump_ui(self.remote + "/restore_before.json", timeout=30)
            before_path = ui_dir / f"{case_id}_restore_before.json"
            save(before_path, before)
            before_nodes = list(ui_nodes(before))
            state["before_layout"] = str(before_path.relative_to(self.output))
            mission_before = self.device.shell("aa dump -l", timeout=10, check=False)
            before_missions_path = ui_dir / f"{case_id}_restore_before_missions.txt"
            before_missions_path.write_text(mission_before, encoding="utf-8")
            state["before_missions"] = str(before_missions_path.relative_to(self.output))
            state["visible_before"] = visible_native_low_memory_modal(
                before_nodes, self.config["popup_text_patterns"])
            state["missions_before"] = parse_low_memory_missions(mission_before)
            if not state["visible_before"]:
                state["status"] = "no_visible_native_warning"
                state["verified_absent"] = not bool(state["missions_before"])
            else:
                active_missions = [row for row in state["missions_before"]
                                   if row.get("bundle") in NATIVE_LOW_MEMORY_OWNERS and
                                   row.get("ability") == "LowMemoryAbility" and
                                   row.get("state") == "FOREGROUND"]
                target = (native_low_memory_close_target(before_nodes,
                    self.config["popup_text_patterns"]) if len(active_missions) == 1 else None)
                state["active_native_missions"] = active_missions
                if target is None:
                    state["status"] = "incomplete_ambiguous_close_target"
                else:
                    state["close_target"] = target
                    state["click_output"] = self.device.shell(
                        f"uitest uiInput click {target['x']} {target['y']}", timeout=10, check=False)
                    time.sleep(0.5)
                    after = self.device.dump_ui(self.remote + "/restore_after.json", timeout=30)
                    after_path = ui_dir / f"{case_id}_restore_after.json"
                    save(after_path, after)
                    after_nodes = list(ui_nodes(after))
                    state["after_layout"] = str(after_path.relative_to(self.output))
                    mission_after = self.device.shell("aa dump -l", timeout=10, check=False)
                    after_missions_path = ui_dir / f"{case_id}_restore_after_missions.txt"
                    after_missions_path.write_text(mission_after, encoding="utf-8")
                    state["after_missions"] = str(after_missions_path.relative_to(self.output))
                    state["visible_after"] = visible_native_low_memory_modal(
                        after_nodes, self.config["popup_text_patterns"])
                    state["missions_after"] = parse_low_memory_missions(mission_after)
                    state["verified_absent"] = (not state["visible_after"] and
                                                 not state["missions_after"])
                    state["status"] = "closed_and_verified" if state["verified_absent"] else "close_unverified"
            self.result.setdefault("native_popup_restoration", []).append(state)
            return state
        except Exception as exc:
            state.update(status="restore_error", error=str(exc))
            self.result.setdefault("native_popup_restoration", []).append(state)
            return state

    def collect_system_events(self, case_id: str, targets: list[str], start_ms: int, end_ms: int,
                              observed: dict[int, list[dict]], start_uptime: float,
                              end_uptime: float, anchor_wall_ms: int,
                              anchor_uptime: float) -> None:
        raw = self.device.shell(f"hisysevent -l -s {start_ms} -e {end_ms} -m 100000", timeout=20, check=False)
        file = self.output / "system_events" / f"{case_id}.jsonl"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(raw, encoding="utf-8")
        mapping = {bundle: self.device.app(bundle)["uid"] for bundle in targets}
        rows = json_lines(raw)
        self.book.system_kills(rows, case_id, mapping, start_ms, end_ms,
                               str(file.relative_to(self.output)), observed,
                               start_uptime, end_uptime, anchor_wall_ms, anchor_uptime)
        if len(rows) >= 100000:
            self.result["invalid_reasons"].append("system_event_query_truncated")

    def safe_now_ms(self, case_id: str, stage: str) -> int | None:
        try:
            return self.device.now_ms()
        except Exception as exc:
            self.result.setdefault("sampling_errors", []).append({
                "case_id": case_id, "stage": stage, "error": str(exc),
                "classification": "unknown; device wall-clock sample unavailable"})
            self.result["measurement_evidence_complete"] = False
            return None

    def stop_pressure(self) -> None:
        if self.pressure_pid is None:
            return
        pid = self.pressure_pid
        expected = self.remote + "/pressure"
        cmdline = self.device.shell(f"cat /proc/{pid}/cmdline", check=False)
        if expected in cmdline:
            self.device.shell(f"kill -TERM {pid}", check=False)
            for _ in range(100):
                state = self.device.shell(f"cat /proc/{pid}/stat", check=False)
                if not re.match(r"^\d+ \(.+\) [^Z]", state.strip()):
                    break
                time.sleep(0.1)
            else:
                self.device.shell(f"kill -KILL {pid}", check=False)
                self.result["invalid_reasons"].append("pressure_required_forced_stop")
        else:
            self.result["invalid_reasons"].append("pressure_process_lost")
        self.pressure_pid = None
        self.mark("pressure_released", pid=pid)
        self.device.receive(self.remote + "/pressure.log", self.output / "pressure.log")

    def cleanup(self) -> None:
        self.device.command_timeout = 15
        self.stop_sampler()
        if self.scene == 1:
            try:
                self.stop_process_lifecycle_trace()
            except Exception as exc:
                self.result.setdefault("evidence_collection_errors", []).append({
                    "source": "process_lifecycle_trace", "error": str(exc)})
                self.result["measurement_evidence_complete"] = False
        try:
            self.stop_kernel_dmesg_reader()
        except Exception as exc:
            self.result.setdefault("evidence_collection_errors", []).append({
                "source": "kernel_dmesg_reader", "error": str(exc)})
            self.result["measurement_evidence_complete"] = False
        try:
            self.stop_pressure()
        except Exception as exc:
            self.result["invalid_reasons"].append("pressure_cleanup:" + str(exc))
        owned_cleanup = {}
        for bundle in self.config["test_apps"]:
            # Cleanup is restricted to our generated workload packages.
            if re.fullmatch(r"io\.memscene\.(?:workload|scene4\.workload)\.a\d+", bundle):
                try:
                    self.device.force_stop(bundle)
                    remaining = self.device.app_processes(bundle)
                    owned_cleanup[bundle] = {"force_stop_sent": True,
                                              "remaining_processes": list(remaining.values()),
                                              "empty": not bool(remaining)}
                    if remaining:
                        self.result["invalid_reasons"].append("owned_app_cleanup_incomplete:" + bundle)
                except Exception as exc:
                    owned_cleanup[bundle] = {"force_stop_sent": True, "empty": None, "error": str(exc)}
                    self.result["invalid_reasons"].append("app_cleanup:" + str(exc))
        self.result["owned_app_cleanup"] = owned_cleanup
        owned_page_cleanup = []
        for bundle, case_id in self.owned_page_files:
            try:
                verification = self.device.remove_owned_case_pages(bundle, case_id)
                owned_page_cleanup.append({"bundle": bundle, "case_id": case_id,
                                           "verified_absent": bool(verification.get("absent"))})
            except Exception as exc:
                owned_page_cleanup.append({"bundle": bundle, "case_id": case_id,
                                           "verified_absent": False, "error": str(exc)})
                self.result["invalid_reasons"].append("page_file_cleanup:" + str(exc))
        self.result["owned_page_cleanup"] = owned_page_cleanup
        if self.trace_started:
            self.finish_trace()
        if self.oom_trace_previous is not None:
            path = "/sys/kernel/debug/tracing/events/oom/mark_victim/enable"
            self.device.shell("echo " + self.oom_trace_previous + " > " + quote(path), check=False)
            self.result["oom_trace"]["restored_enable"] = self.device.shell("cat " + quote(path), check=False).strip()
            if self.result["oom_trace"]["restored_enable"] != self.oom_trace_previous:
                self.result["invalid_reasons"].append("oom_trace_restore_failed")
        if self.oom_trace_instance_created:
            instance = "/sys/kernel/debug/tracing/instances/" + self.oom_trace_instance_name
            self.device.shell("echo 0 > " + quote(instance + "/tracing_on"), check=False)
            self.device.shell("echo 0 > " + quote(instance + "/events/oom/mark_victim/enable"), check=False)
            self.device.shell("rmdir " + quote(instance), check=False)
            exists = self.device.shell("test -d " + quote("/sys/kernel/debug/tracing/instances/" + self.oom_trace_instance_name) + " && echo exists", check=False).strip()
            self.result["oom_trace"]["instance_removed"] = not bool(exists)
            if exists:
                self.result["invalid_reasons"].append("oom_trace_instance_cleanup_failed")
        self.device.command_timeout = 45

    def _parse_process_lifecycle_raw(self, raw: str, source_path: Path) -> None:
        state = self.result.get("process_lifecycle_trace")
        if not state or state.get("coverage") != "awaiting_finalized_trace":
            return
        timed_rows = []
        trace_events = []
        boundary_rows = {}
        for line in raw.splitlines():
            timestamp_match = re.search(r"\s(\d+\.\d+):\s+([^:]+):\s*(.*)$", line)
            if not timestamp_match:
                continue
            timestamp = float(timestamp_match.group(1))
            event, payload = timestamp_match.group(2), timestamp_match.group(3)
            trace_events.append((timestamp, line))
            marker = re.search(r"(?:^|\s)([BE])\|0\|memscenes/([A-Za-z0-9_/-]+)", payload)
            if event == "tracing_mark_write" and marker:
                boundary_rows.setdefault(marker.group(2), []).append(timestamp)
        markers = {row.get("name"): row.get("device_uptime_seconds") for row in self.markers}
        trace_begin = next(iter(boundary_rows.get(self.id + "_reuse_begin", [])), None)
        trace_end = next(iter(boundary_rows.get(self.id + "_reuse_end", [])), None)
        uptime_begin, uptime_end = markers.get("reuse_begin"), markers.get("reuse_end")
        drift_seconds = None
        if None not in (trace_begin, trace_end, uptime_begin, uptime_end) and trace_end > trace_begin:
            drift_seconds = (trace_end - trace_begin) - (uptime_end - uptime_begin)
        mapped_bounds = (trace_begin, trace_end) if drift_seconds is not None else None
        for line in raw.splitlines():
            match = re.search(r"\s(\d+\.\d+): sched_process_(fork|exit):", line)
            if match:
                timestamp = float(match.group(1))
                if mapped_bounds is not None and mapped_bounds[0] <= timestamp <= mapped_bounds[1]:
                    timed_rows.append((timestamp, match.group(2), line))
        fork_rows = [line for _, event, line in timed_rows if event == "fork"]
        exit_rows = [line for _, event, line in timed_rows if event == "exit"]
        path = self.output / "process_lifecycle_events.txt"
        path.write_text("\n".join(line for _, _, line in timed_rows), encoding="utf-8")
        first_ts = min((ts for ts, _ in trace_events), default=None)
        last_ts = max((ts for ts, _ in trace_events), default=None)
        # Both boundary markers must survive in the trace ring. Zero fork/exit
        # rows remains valid, but absent boundaries do not prove full coverage.
        span_ok = (mapped_bounds is not None and first_ts is not None and
                   first_ts <= trace_begin and last_ts is not None and last_ts >= trace_end and
                   abs(drift_seconds) <= 1.0)
        state.update({"source_path": str(source_path.relative_to(self.output)),
                      "event_path": str(path.relative_to(self.output)),
                      "fork_event_rows": len(fork_rows), "exit_event_rows": len(exit_rows),
                      "fork_examples": fork_rows[:3], "exit_examples": exit_rows[:3],
                      "trace_first_event_uptime_seconds": first_ts,
                      "trace_last_event_uptime_seconds": last_ts,
                      "reuse_begin_trace_timestamp": trace_begin,
                      "reuse_end_trace_timestamp": trace_end,
                      "reuse_begin_device_uptime_seconds": uptime_begin,
                      "reuse_end_device_uptime_seconds": uptime_end,
                      "trace_clock_drift_seconds": drift_seconds,
                      "trace_spans_enabled_window": span_ok,
                      "coverage": "enabled_window_collected" if span_ok else "trace_window_not_proven"})
        state["capture_collected"] = span_ok
        state["fork_event_observation"] = ("observed" if fork_rows else
                                            "unknown_no_fork_row_observed")
        if not span_ok:
            self.result["measurement_evidence_complete"] = False
            self.result.setdefault("evidence_gaps", []).append({
                "source": "process_lifecycle_trace",
                "reason": "filtered trace lacks both reuse markers or its clock/window coverage is incomplete"})
            return
        if not fork_rows:
            self.result["measurement_evidence_complete"] = False
            self.result.setdefault("evidence_gaps", []).append({
                "source": "process_lifecycle_trace",
                "reason": "no sched_process_fork row observed; device capability probe could not record a known child fork"})
        ending = getattr(self, "lifecycle_trace_ending", None)
        if self.lifecycle_baseline is not None and ending is not None:
            gaps = target_child_counter_gaps(fork_rows, self.samples,
                self.lifecycle_baseline, ending, self.lifecycle_target_bundles)
            state["target_family_unresolved_children"] = gaps
            state["target_family_child_count_gaps"] = len(gaps)
            if gaps:
                self.result["measurement_evidence_complete"] = False
                self.result.setdefault("evidence_gaps", []).append({
                    "source": "process_lifecycle_trace",
                    "reason": "target-family child lacks matching endpoint PID:start_ticks counters",
                    "count": len(gaps)})

    def finish(self) -> Path:
        self.result["markers"] = self.markers
        self.result["counts"] = self.book.counts()
        scene4_result = self.result.get("scene4_cold_start") if self.scene == 4 else None
        if scene4_result:
            cumulative = scene4_result.get("cumulative_counts", {})
            cumulative_observed = (_scene4_cumulative_counts_observed(scene4_result) and
                                   isinstance(cumulative, dict) and
                                   cumulative.get("primary_count") is not None)
            self.result["counts"] = {
                "startup_failure": int(cumulative["startup_failure"]) if cumulative_observed else None,
                "oom_kill": int(cumulative["confirmed_oom_kill"]) if cumulative_observed else None,
                "low_memory_popup": int(cumulative["native_low_memory_popup"]) if cumulative_observed else None,
                "test_popup_separate": int(cumulative["test_popup_separate"]) if cumulative_observed else None,
                "donor_oom_side_effects": int(cumulative["donor_oom_side_effects"]) if cumulative_observed else None,
                "target_oom_side_effects": int(cumulative["target_oom_side_effects"]) if cumulative_observed else None,
                "unknown_timeout_or_gap": int(cumulative["unknown_timeout_or_gap"]) if cumulative_observed else None,
                "other_oom_side_effects": int(cumulative["other_oom_side_effects"]) if cumulative_observed else None,
                "unique_incidents": int(cumulative["unique_incidents"]) if cumulative_observed else None,
                "total_events": int(cumulative["primary_count"]) if cumulative_observed else None,
                "primary_count_observed": cumulative_observed,
                "measurement_complete": bool(scene4_result.get("measurement_complete")),
            }
        elif self.scene == 4 and self.result.get("scene4_fast_frame_counts_unknown"):
            self.result["counts"]["low_memory_popup"] = None
            self.result["counts"]["total_events"] = None
            self.result["counts"]["primary_count_observed"] = False
        self.result["events"] = self.book.export()
        save(self.output / "events.json", self.book.export())
        with (self.output / "events.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["category","case_id","bundle","pid","timestamp_ms","source","reason","incident","evidence"])
            writer.writeheader()
            for event in self.book.export():
                writer.writerow({key: json.dumps(event[key], ensure_ascii=False) if isinstance(event[key], list)
                                 else event[key] for key in writer.fieldnames})
        execution = bool(self.result.get("execution_complete", False))
        counts = self.result["counts"]
        coverage = all((counts.get(key) or 0) > 0 for key in ("startup_failure", "oom_kill", "low_memory_popup"))
        baseline_requirements = self.scene != 2 or self.phase != "baseline" or (
            coverage and counts.get("total_events", 0) >= self.config["scene2"]["baseline_min_events"])
        if self.scene == 3:
            short_cfg = self.config["scene3"]
            self.result["scene3_target"] = {
                "minimum_total_events": int(short_cfg["target_total_events_min"]),
                "maximum_total_events": int(short_cfg["target_total_events_max"]),
                "category_sum": int(counts.get("total_events", 0)),
                "all_three_categories_present": coverage,
                "met_in_raw_counts": (coverage and int(short_cfg["target_total_events_min"]) <=
                                      int(counts.get("total_events", 0)) <=
                                      int(short_cfg["target_total_events_max"])),
                "authoritative_after_ui_audit": False,
            }
        elif self.scene == 4 and scene4_result:
            primary_observed = bool(self.result["counts"].get("primary_count_observed"))
            self.result["scene4_target"] = {
                "primary_count": self.result["counts"].get("total_events"),
                "primary_count_observed": primary_observed,
                "startup_failure": self.result["counts"].get("startup_failure"),
                "confirmed_oom_kill": self.result["counts"].get("oom_kill"),
                "native_low_memory_popup": self.result["counts"].get("low_memory_popup"),
                "unique_incidents": self.result["counts"].get("unique_incidents"),
                "confirmed_count_lower_bounds": scene4_result.get("confirmed_count_lower_bounds"),
                "measurement_complete": bool(scene4_result.get("measurement_complete")),
                "formal_qualification_eligible": False,
            }
        elif self.scene == 4:
            short_cfg = self.config["scene4"]
            self.result["scene4_target"] = {
                "minimum_total_events": int(short_cfg["target_total_events_min"]),
                "maximum_total_events": int(short_cfg["target_total_events_max"]),
                "category_sum": (int(counts["total_events"]) if counts.get("total_events") is not None else None),
                "all_three_categories_present": (coverage if counts.get("low_memory_popup") is not None else None),
                "met_in_raw_counts": (coverage and int(short_cfg["target_total_events_min"]) <=
                                      int(counts["total_events"]) <= int(short_cfg["target_total_events_max"]))
                                      if counts.get("total_events") is not None else None,
                "authoritative_after_ui_audit": False,
                "sensitivity_only_no_optimization_claim": True,
            }
        measurement_complete = (execution and self.result.get("measurement_evidence_complete", True) and
                                not self.result["invalid_reasons"] and
                                (self.profile != "acceptance" or
                                 (self.config["dram_gib"] == 16 and
                                  (self.phase != "baseline" or baseline_requirements))))
        acceptance = measurement_complete and self.profile == "acceptance"
        self.result["execution_complete"] = execution
        self.result["trigger_coverage_complete"] = coverage if self.scene in (2, 3, 4) else None
        self.result["baseline_trigger_requirements"] = baseline_requirements if self.scene == 2 and self.phase == "baseline" else None
        self.result["measurement_complete"] = measurement_complete
        self.result["acceptance_valid"] = bool(acceptance)
        if self.scene == 2 and self.profile == "qualification":
            try:
                expected = case_definitions(self.config, "combined")
                qualification_reasons = qualification_gate(self.result, self.config, expected)
            except Exception:
                qualification_reasons = ["qualification_gate_evaluation_failed"]
            self.result["qualification_eligible"] = not qualification_reasons
            self.result["qualification_reasons"] = qualification_reasons
            self.result["qualification_scope"] = ("formal_16gb" if self.config.get("dram_gib") == 16
                                                  else f"prevalidation_{self.config.get('dram_gib')}gb")
        self.result["status"] = "valid" if self.result["acceptance_valid"] else (
            "prevalidated" if execution and not self.result["invalid_reasons"] else "invalid")
        save(self.output / "result.json", self.result)
        save(self.output / "progress.json", {"status": self.result["status"], "markers": self.markers})
        report = [
            "# 内存场景运行报告", "", f"场景：{self.scene}；阶段：{self.phase}；模式：{self.profile}",
            f"状态：{self.result['status']}", f"版本：{self.config['version_label']}",
            f"设备：{self.device.target}；配置内存：{self.config['dram_gib']} GiB", "",
            f"execution_complete：{self.result.get('execution_complete')}；measurement_complete：{self.result.get('measurement_complete')}",
            f"trigger_coverage_complete：{self.result.get('trigger_coverage_complete')}；baseline_trigger_requirements：{self.result.get('baseline_trigger_requirements')}",
            "## 计数", "", "|指标|次数|", "|---|---:|",
            *[f"|{key}|{value}|" for key, value in self.result["counts"].items()], "",
            "## 验证结果", "", *[f"- {reason}" for reason in self.result["invalid_reasons"]]
]

        if self.scene == 1:
            report += ["", "复用缺页：" + json.dumps(self.result.get("reuse_faults", {}), ensure_ascii=False)]
            load = self.result.get("application_load", {})
            report += ["应用触碰匿名+文件映射页：" + str(load.get("touched_bytes", 0)) +
                       " (anon=" + str(load.get("anon_touched_bytes", 0)) +
                       ", file=" + str(load.get("file_touched_bytes", 0)) +
                       ") / 要求 " + str(load.get("required_bytes", 0)) + " bytes"]
            report += ["pressure退出：" + str(self.result.get("pressure", {}))]
        mem = self.result.get("environment", {}).get("meminfo", {})
        report += ["设备MemTotal/SwapTotal：" + str(mem.get("MemTotal", "unknown")) + "/" +
                   str(mem.get("SwapTotal", "unknown")) + " bytes"]
        if self.scene == 3:
            report += ["", "场景3为两用例短时预验证；不属于场景2 qualification、freeze或正式验收。",
                       "目标区间只按离线UI审计后的三类事件分类合计判读：" +
                       json.dumps(self.result.get("scene3_target", {}), ensure_ascii=False)]
        if self.scene == 4:
            report += ["", "场景4为自有HAP供体与冷启动压力预验证，不属于场景2 qualification或正式验收。",
                       "主计数要求四例完整且原生界面覆盖通过；不完整时总计为N/A，已确认分项下界见结果：" +
                       json.dumps(self.result.get("scene4_target", {}), ensure_ascii=False)]
        if self.profile == "smoke" or self.config["dram_gib"] != 16 and self.scene == 2:
            report += ["", "本轮用于设备预验证，不满足第二场景16GB正式验收条件。"]
        (self.output / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        print(f"结果：{self.output}；状态：{self.result['status']}", flush=True)
        return self.output

def validate_environment(session: Session, formal: bool = False) -> dict:
    env = session.device.environment()
    session.result["environment"] = env
    save(session.output / "environment.json", env)
    actual = env["meminfo"].get("MemTotal", 0)
    expected = session.config["dram_gib"] * GIB
    if not 0.90 * expected <= actual <= 1.02 * expected:
        raise RuntimeError(f"配置内存与设备实际容量不符：{expected} vs {actual}")
    if formal and session.scene == 2 and session.config["dram_gib"] != 16:
        raise RuntimeError("场景2正式验收需要设备实际配置为16GB；当前仅支持预验证")
    if session.scene == 2:
        spec = session.config["scene2"]
        if not 0 <= spec["daily_mib"] <= spec["peak_mib"] <= session.config["dram_gib"] * 1024:
            raise RuntimeError("场景2负载范围无效：daily <= peak <= 实际DRAM")
        if not 1 <= spec["lifetime_seconds"] <= 3600 or not 0 <= spec["hold_seconds"] < spec["lifetime_seconds"]:
            raise RuntimeError("场景2 hold/设备TTL范围无效")
        if not 1 <= spec["ready_timeout_seconds"] <= spec["lifetime_seconds"]:
            raise RuntimeError("场景2主机观察超时必须不超过设备TTL")
    if session.scene == 3:
        runtime = session.config["scene2"]
        if not 0 <= runtime["daily_mib"] <= session.config["dram_gib"] * 1024:
            raise RuntimeError("场景3 daily负载超出实际DRAM")
        if not 1 <= runtime["lifetime_seconds"] <= 3600 or not 1 <= runtime["ready_timeout_seconds"] <= runtime["lifetime_seconds"]:
            raise RuntimeError("场景3观察超时/设备TTL范围无效")
        for case in case_definitions(session.config, "combined", "scene3"):
            if not 0 <= case["daily_mib"] <= case["peak_mib"] <= session.config["dram_gib"] * 1024:
                raise RuntimeError("场景3用例daily/peak不在设备实际DRAM范围内")
            if not 0 <= case["hold_seconds"] < runtime["lifetime_seconds"]:
                raise RuntimeError("场景3用例hold必须小于设备TTL")
    if session.scene == 4 and session.config["scene4"].get("oracle_cold_start_runner"):
        cfg = session.config["scene4"]
        memtotal_bytes = int(actual)
        target_peak = int(cfg.get("target_peak_mib", 0))
        donor_peak = int(cfg.get("donor_peak_mib", cfg.get("donor_memory_mib", 0)))
        expected_peak = math.ceil((1.2 * memtotal_bytes / 6) / (64 * 1024**2)) * 64
        if target_peak != expected_peak:
            raise RuntimeError(f"Scene4 oracle target peak must be frozen formula value {expected_peak} MiB; config has {target_peak}")
        role_peak_bytes = (6 * target_peak + 2 * donor_peak) * 1024**2
        if role_peak_bytes > 1.4 * memtotal_bytes:
            raise RuntimeError("Scene4 six-target plus two-donor peak exceeds 1.4x measured MemTotal")
        if 6 * int(cfg.get("target_daily_mib", 0)) + 2 * int(cfg.get("donor_daily_mib", 0)) > memtotal_bytes // (1024**2):
            raise RuntimeError("Scene4 six-target plus two-donor daily load exceeds measured MemTotal")
        if len(session.config["test_apps"]) != 8 or len(cfg.get("cold_start_targets", [])) != 6 or len(cfg.get("donor_bundles", [])) != 2:
            raise RuntimeError("Scene4 oracle roles require exactly two donors and six targets")
        fallback_files = [int(value) for value in cfg.get("baseline_file_mib_sequence", [2048, 1536, 1024])]
        selected_file = int(cfg.get("target_file_mib", 0))
        if selected_file not in fallback_files or selected_file > int(cfg.get("target_daily_mib", 0)):
            raise RuntimeError("Scene4 oracle file workload must use one of the frozen 2048/1536/1024-MiB baseline loads")
        if int(cfg.get("target_file_sync_bytes", selected_file * 1024**2)) != selected_file * 1024**2:
            raise RuntimeError("Scene4 target file-sync byte proof does not match selected file load")
        if int(cfg.get("donor_file_mib", -1)) != 0:
            raise RuntimeError("Scene4 donor workloads must be private anonymous memory only")
    if session.scene == 4 and not session.config["scene4"].get("oracle_cold_start_runner"):
        cfg = session.config["scene4"]
        memtotal = int(actual)
        apps = len(session.config["test_apps"])
        cases = case_definitions(session.config, "combined", "scene4")
        if apps != 8 or len(cases) != sum(int(v) for v in cfg["case_repetitions"].values()):
            raise RuntimeError("场景4固定要求8个隔离test app及按配置展开的固定用例")
        if int(cfg.get("single_app_peak_mib", 0)) <= 0 or int(cfg["single_app_peak_mib"]) * 1024**2 > memtotal:
            raise RuntimeError("场景4单app校准峰值必须大于0且不超过设备实际MemTotal")
        if int(cfg.get("daily_file_mib", -1)) + int(cfg.get("daily_anon_mib", -1)) != int(cfg["daily_mib"]):
            raise RuntimeError("场景4日常文件映射与匿名页总量必须精确构成daily目标")
        if not 1 <= int(cfg.get("calibration_hold_seconds", 0)) < int(cfg["lifetime_seconds"]):
            raise RuntimeError("场景4单app校准hold必须为正且小于设备TTL")
        daily_total = memory_roles(cfg, session.config["test_apps"],
            cases[0]["peak_mib"], cases[0].get("file_mib", 0))["daily_total_mib"] * 1024**2
        for case in cases:
            if not 0 <= case["daily_mib"] <= case["peak_mib"] <= memtotal // (1024**2):
                raise RuntimeError("场景4单app daily/peak必须不超过本机MemTotal")
            if not 0 <= case.get("file_mib", 0) <= case["daily_mib"]:
                raise RuntimeError("场景4clean文件映射应包含在daily基线内")
            if int(case.get("file_mib", 0)) != int(cfg["daily_file_mib"]):
                raise RuntimeError("场景4每个用例的file_mib必须匹配daily_file_mib")
            if not 0 <= case["hold_seconds"] < cfg["lifetime_seconds"]:
                raise RuntimeError("场景4用例hold必须小于设备TTL")
            roles = memory_roles(cfg, session.config["test_apps"], case["peak_mib"], case.get("file_mib", 0))
            if any(x["peak_mib"] * 1024**2 > memtotal for x in roles["per_app"].values()):
                raise RuntimeError("Scene4 per-role peak exceeds measured MemTotal")
            case_peak_total = roles["peak_total_mib"] * 1024**2
            case_ratio = case_peak_total / memtotal if memtotal else 0
            if not float(cfg["combined_peak_floor_ratio"]) <= case_ratio <= float(cfg["combined_peak_ceiling_ratio"]):
                raise RuntimeError(f"场景4用例{case['kind']}组合peak/MemTotal比例不在限制内：{case_ratio:.3f}")
        if daily_total > memtotal:
            raise RuntimeError("场景4全部app daily合计超过本机MemTotal")
    for bundle in session.config["test_apps"]:
        session.device.app(bundle)
    if session.scene == 4:
        # Do not mix an older workload generation into Scene 4's measured
        # daily total. This check is read-only; old bundles are never stopped.
        old_workload_bundles = [f"io.memscene.workload.a{i}" for i in range(1, 9)]
        resident_old = {}
        for bundle in old_workload_bundles:
            try:
                session.device.app(bundle)
            except Exception:
                continue
            processes = session.device.app_processes(bundle)
            if processes:
                resident_old[bundle] = list(processes.values())
        if resident_old:
            raise RuntimeError("场景4校准前检测到旧workload仍驻留；为避免混入daily总量，本轮停止：" +
                               json.dumps(resident_old, ensure_ascii=False))
    session.result["app_uids"] = {bundle: session.device.app(bundle)["uid"]
                                  for bundle in session.config["test_apps"]}
    configured_manifest = (session.config.get("scene4", {}).get("hap_manifest")
                           if session.scene == 4 else None)
    manifest = (ROOT / configured_manifest if configured_manifest else
                ROOT / ("bin/scene4_apps/manifest.json" if session.scene == 4 else "bin/apps/manifest.json"))
    if manifest.is_file():
        manifest_rows = read(manifest)
        session.result["app_build_manifest"] = manifest_rows
        if session.scene == 4 and session.config.get("scene4", {}).get("execution_profile") == "legacy_peak_transition":
            if not isinstance(manifest_rows, list):
                raise RuntimeError("legacy Scene4 HAP manifest must be a list")
            by_bundle = {str(row.get("bundle")): row for row in manifest_rows if isinstance(row, dict)}
            if set(by_bundle) != set(session.config["test_apps"]):
                raise RuntimeError("legacy Scene4 HAP manifest bundle set differs from configured eight apps")
            for bundle, row in by_bundle.items():
                hap = (ROOT / str(row.get("path", ""))).resolve()
                expected = str(row.get("sha256", "")).lower()
                if not hap.is_file() or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise RuntimeError(f"legacy Scene4 HAP path/hash missing for {bundle}")
                actual_hash = hashlib.sha256(hap.read_bytes()).hexdigest()
                if actual_hash != expected:
                    raise RuntimeError(f"legacy Scene4 HAP SHA mismatch for {bundle}")
    elif session.scene == 4 and session.config.get("scene4", {}).get("execution_profile") == "legacy_peak_transition":
        raise RuntimeError("legacy Scene4 HAP manifest does not exist")
    return env

def scene1(session: Session) -> Path:
    d, cfg = session.device, session.config
    spec = cfg["scene1"]
    real = [cfg["real_apps"][name]["bundle"] for name in ORDER[:6]]
    background = cfg["test_apps"]
    screenshot_extras = [
        {"label": "图库", "bundle": "com.huawei.hmos.photos"},
        {"label": "备忘录", "bundle": "com.huawei.hmos.notepad"},
        {"label": "好压", "bundle": "com.haozip2345.app"},
        {"label": "剪映", "bundle": "com.lemon.lvpro"},
        {"label": "迅雷", "bundle": "com.xunlei.thunder"},
    ]
    session.result["scene1_additional_screenshot_apps"] = {
        "identified_apps": screenshot_extras,
        "unidentified_or_not_installed": [
            {"label": "QQ音乐", "bundle": None, "reason": "bm dump -a -l did not show a matching installed label"},
            {"label": "腾讯文档", "bundle": None, "reason": "bm dump -a -l did not show a matching installed label"},
        ],
        "note": "Only observational cgroup samples; these apps are not launched by the scenario."
    }
    pressure_snapshot_bundles = real + background + [row["bundle"] for row in screenshot_extras]
    try:
        validate_environment(session)
        d.dismiss_owned_install_prompts(session.remote + "/install_prompts.json")
        for bundle in background:
            d.force_stop(bundle)
            case_id = session.id + "_background"
            session.owned_page_files.add((bundle, case_id))
            target = spec["background_mib_per_app"]
            session.launch_test(bundle, case_id, target, target, 0, spec["lifetime_seconds"],
                                int(target * spec["file_ratio"]))
            session.wait_ready(bundle, case_id, spec["allocation_timeout_seconds"])
        for bundle in real:
            d.force_stop(bundle)
        initial = session.snapshot("before_sequence", real + background)
        # Record the fixed foreground sequence without the high-volume
        # background-map preparation trace preceding it.
        session.start_trace("trace_sequence.ftrace", 262144,
                            ("app", "ability", "sched", "window"))
        session.mark("sequence_begin", order=list(ORDER))
        session.start_sampler(real)
        actions = []
        originals = {}
        def enter(stage: int, name: str):
            app = cfg["real_apps"][name]
            output = d.launch(app["bundle"], app["ability"], app["module"])
            focused = False
            for _ in range(8):
                tree = d.dump_ui(session.remote + "/focus.json")
                if app["bundle"] in focused_bundles(tree):
                    focused = True
                    break
                time.sleep(0.5)
            if not focused:
                raise RuntimeError(f"应用未到前台：{name}")
            processes = d.app_processes(app["bundle"])
            if not processes:
                raise RuntimeError(f"应用进程未就绪：{name}")
            if stage <= 6 and name in ORDER[-2:]:
                originals[name] = processes
            if stage > 6 and not any(key in processes for key in originals[name]):
                raise RuntimeError(f"复用应用已重启：{name}")
            actions.append({"stage": stage, "app": name, "action": "launch", "output": output})
            session.mark(f"stage_{stage}_launch", app=name, processes=processes)
            d.shell("uitest uiInput swipe " + " ".join(map(str, spec["swipe"])))
            actions.append({"stage": stage, "app": name, "action": "swipe"})
            session.mark(f"stage_{stage}_swipe", app=name)
            save(session.output / "actions.json", actions)
        for stage, name in enumerate(ORDER[:6], 1):
            enter(stage, name)
        owned_bytes = 0
        anon_touched_bytes = 0
        file_touched_bytes = 0
        background_checks = {}
        for bundle in background:
            rows = session.journal(bundle, session.id + "_background")
            ready = [row for row in rows if row.get("event") == "startup_ready"]
            alive = d.app_processes(bundle)
            ready_row = ready[-1] if ready else None
            app_uid = d.app(bundle)["uid"]
            live_process = next((process for process in alive.values()
                                 if ready_row and int(process["pid"]) == int(ready_row["pid"])), None)
            if (ready_row and live_process and int(ready_row.get("uid", -1)) == app_uid):
                anon_bytes = int(ready_row.get("anon_bytes", 0))
                file_bytes = int(ready_row.get("file_bytes", 0))
                touched_bytes = anon_bytes + file_bytes
                mapping = process_mapping_snapshot(d, {k: v for k, v in alive.items()
                                                       if int(v["pid"]) == int(ready_row["pid"])})
                owned_bytes += touched_bytes
                anon_touched_bytes += anon_bytes
                file_touched_bytes += file_bytes
                background_checks[bundle] = {**ready_row, "anon_touched_bytes": anon_bytes,
                    "file_touched_bytes": file_bytes, "touched_bytes": touched_bytes,
                    "process_stat": live_process, "process_start_ticks": live_process["start_ticks"],
                    "smaps_rollup": mapping}
                if not all(row.get("smaps_rollup_available") for row in mapping.values()):
                    session.result.setdefault("evidence_gaps", []).append({"source": "smaps_rollup",
                        "bundle": bundle, "reason": "live workload smaps_rollup unavailable"})
                    session.result["measurement_evidence_complete"] = False
            else:
                raise RuntimeError(f"后台应用负载退出或未分配完成：{bundle}")
        session.result["application_load"] = {"touched_bytes": owned_bytes,
                                             "anon_touched_bytes": anon_touched_bytes,
                                             "file_touched_bytes": file_touched_bytes,
                                             "file_ratio": spec["file_ratio"],
                                             "file_backed_bytes": file_touched_bytes,
                                             "required_bytes": int(cfg["dram_gib"] * GIB * 1.5),
                                             "apps": background_checks, "pressure_excluded": True}
        if session.profile == "acceptance" and owned_bytes < 1.5 * cfg["dram_gib"] * GIB:
            raise RuntimeError("应用实际触碰负载未达到DRAM的150%")
        before_pressure = session.snapshot("before_pressure", pressure_snapshot_bundles)
        pressure_local = ROOT / "bin/pressure"
        if not pressure_local.is_file():
            raise RuntimeError("加压程序未编译")
        d.call("file", "send", str(pressure_local), session.remote + "/pressure")
        d.shell("chmod 700 " + quote(session.remote + "/pressure"))
        command = (f"{quote(session.remote + '/pressure')} {spec['pressure_mib']} 900 "
                   f">{quote(session.remote + '/pressure.log')} 2>&1 </dev/null & echo $!")
        pid_text = d.shell(command).strip().splitlines()[-1]
        if not pid_text.isdigit():
            raise RuntimeError("加压进程PID无效")
        session.pressure_pid = int(pid_text)
        session.mark("pressure_started", pid=session.pressure_pid)
        deadline = time.monotonic() + spec["allocation_timeout_seconds"]
        while time.monotonic() < deadline:
            raw = d.shell("cat " + quote(session.remote + "/pressure.log"), check=False)
            if f"ALLOCATED_MIB {spec['pressure_mib']}\n" in raw:
                break
            if "FAILED" in raw or "STOP" in raw:
                raise RuntimeError("加压分配未完成")
            if not d.shell(f"test -d /proc/{session.pressure_pid} && echo alive", check=False).strip():
                raise RuntimeError("加压进程在分配期间退出")
            time.sleep(0.5)
        else:
            raise TimeoutError("加压分配超时")
        session.mark("pressure_allocated", mib=spec["pressure_mib"])
        time.sleep(spec["pressure_wait_seconds"])
        after_pressure = session.snapshot("after_pressure", pressure_snapshot_bundles)
        old, new = before_pressure["disk_write_bytes"], after_pressure["disk_write_bytes"]
        app_eswap = {bundle: after_pressure["apps"][bundle]["eswap_bytes"] for bundle in real + background}
        confirmed = old is not None and new is not None and new > old and any(
            amount is not None and amount > (before_pressure["apps"][bundle]["eswap_bytes"] or 0)
            for bundle, amount in app_eswap.items())
        session.result["disk_swap"] = {"confirmed": confirmed, "before_write_bytes": old,
                                       "after_write_bytes": new, "application_eswap_bytes": app_eswap}
        session.result["pressure"] = {"pid": session.pressure_pid, "target_mib": spec["pressure_mib"],
                                      "released_before_reuse_baseline": True}
        # Smoke still executes the exact timing, but cannot be promoted to acceptance.
        if not confirmed and session.profile == "acceptance":
            raise RuntimeError("本轮缺少应用换出及Hyperhold磁盘写出联合证据")
        session.stop_pressure()
        # Finalize the sequence segment immediately after owned pressure is
        # fully released. A separate shorter segment protects reuse counters
        # and lifecycle markers from the preceding UI trace volume.
        session.finish_trace()
        if session.trace_started:
            raise RuntimeError("sequence Hitrace segment did not finish; reuse segment not started")
        session.start_trace("trace_reuse.ftrace", 262144,
                            ("app", "ability", "sched", "window"))
        reuse_bundles = [cfg["real_apps"][name]["bundle"] for name in ORDER[-2:]]
        # Begin and read back lifecycle events before the fresh counter
        # baseline, closing the gap where a short-lived target child could be
        # born and exit between baseline sampling and trace enable.
        session.start_process_lifecycle_trace(reuse_bundles)
        baseline = session.snapshot("reuse_baseline", real)
        session.lifecycle_baseline = baseline
        lifecycle = session.result.get("process_lifecycle_trace", {})
        lifecycle["scope"] = "lifecycle_enable_readback_before_reuse_baseline_through_reuse_end"
        lifecycle["baseline_uptime_seconds"] = baseline.get("device_uptime_seconds")
        session.mark("reuse_begin")
        if not release_precedes_reuse(session.markers):
            raise RuntimeError("加压进程未在复用缺页基线前释放")
        enter(7, ORDER[6])
        enter(8, ORDER[7])
        ending = session.snapshot("reuse_end", real)
        session.mark("reuse_end")
        session.stop_process_lifecycle_trace(ending)
        session.finish_trace()
        reuse = {}
        for name in ORDER[-2:]:
            bundle = cfg["real_apps"][name]["bundle"]
            delta = fault_delta(baseline["apps"][bundle]["processes"], ending["apps"][bundle]["processes"])
            reuse[name] = delta
            if not delta["valid"]:
                session.result["invalid_reasons"].extend(delta["reasons"])
            # Reject exits observed between endpoint snapshots as well.
            expected = set(baseline["apps"][bundle]["processes"])
            for sample in session.samples:
                if baseline["device_uptime_seconds"] <= sample["device_uptime_seconds"] <= ending["device_uptime_seconds"]:
                    observed = set(sample["apps"].get(bundle, {}).get("processes", {}))
                    if not expected <= observed:
                        session.result["invalid_reasons"].append(f"reuse_process_disappeared:{bundle}")
                        break
        session.result["reuse_faults"] = {"apps": reuse, "total": sum(x["total"] for x in reuse.values()),
                                           "minor": sum(x["minor"] for x in reuse.values()),
                                           "major": sum(x["major"] for x in reuse.values())}
        session.result["full_window_faults"] = {
            "coverage": "partial_lower_bound_since_first_sample", "complete": False,
            "reason": "initial snapshot precedes real-app launches; one-second sampling cannot prove short-lived child lifecycle coverage",
            "per_bundle_diagnostics": process_window_diagnostics(
                session.samples, real + background, initial, ending)}
        vmstat_keys = ("pagefault_major_anon", "pagefault_minor_anon",
                       "pagefault_major_file", "pagefault_minor_file")
        vmstat_deltas = {key: (ending["vmstat"][key] - baseline["vmstat"][key]
                               if key in ending["vmstat"] and key in baseline["vmstat"] else None)
                         for key in vmstat_keys}
        session.result["global_anon_file_diagnostics"] = {
            "deltas": vmstat_deltas,
            "field_coverage": {key: vmstat_deltas[key] is not None for key in vmstat_keys},
            "complete": all(value is not None for value in vmstat_deltas.values())}
        if not session.result["global_anon_file_diagnostics"]["complete"]:
            session.result.setdefault("evidence_gaps", []).append({
                "source": "vmstat_anon_file_pagefaults", "reason": "one or more required vmstat fields unavailable; values remain unknown"})
        if session.result.get("sampling_errors"):
            session.result["measurement_evidence_complete"] = False
            session.result.setdefault("invalid_reasons", []).append("process_sampling_incomplete")
        session.result["execution_complete"] = len(actions) == 16
    except (Exception, KeyboardInterrupt) as exc:
        session.result["invalid_reasons"].append(str(exc))
    finally:
        session.cleanup()
    return session.finish()

def scene2_case(session: Session, case_id: str, bundles: list[str], peak: int, hold: int,
                strategy: str = "concurrent_launch_peak_hold_notify", seed: int | None = None,
                kind: str | None = None, file_mib: int = 0) -> dict:
    d, cfg = session.device, session.config["scene4" if session.scene == 4 else "scene2"]
    scene4_v3_profile = (session.scene == 4 and
        cfg.get("execution_profile") == "legacy_peak_transition" and
        cfg.get("oracle_profile") == "peak_transition_os_snapshot")
    roles = memory_roles(cfg, bundles, peak, file_mib) if session.scene == 4 else None
    peak_targets = roles["peak_targets"] if roles else bundles
    background_donors = roles["donors"] if roles else []
    measurement_command_timeout = d.command_timeout
    if scene4_v3_profile:
        preparation_timeout = float(cfg.get("preparation_read_timeout_seconds", measurement_command_timeout))
        if not 8 <= preparation_timeout <= 30:
            raise ValueError("Scene4 preparation read timeout must be between 8 and 30 seconds")
        # Only pre-registration preparation gets a longer transport allowance.
        # The fixed oracle/peak window below retains its original timeout.
        d.command_timeout = preparation_timeout
        session.result.setdefault("scene4_preparation_transport", []).append({
            "case_id": case_id, "preparation_timeout_seconds": preparation_timeout,
            "measurement_timeout_seconds": measurement_command_timeout})
    session.owned_page_files.update((bundle, case_id) for bundle in bundles)
    popup_baseline = None
    try:
        baseline_raw = d.shell("aa dump -l", timeout=10, check=False)
        baseline_missions = parse_low_memory_missions(baseline_raw)
        popup_baseline = {"mission_ids": sorted(row["mission_id"] for row in baseline_missions),
                          "missions": baseline_missions,
                          "raw": baseline_raw}
        session.popup_baselines[case_id] = popup_baseline
        if scene4_v3_profile:
            # Reuse the same lightweight WMS/AA identity parsers as the fast
            # Scene4 frame worker. This is a pre-BEGIN baseline only; no tree
            # dump or layout walk is performed in the measured window.
            wms_baseline_raw = d.shell("hidumper -s WindowManagerService -a '-a'", timeout=8, check=False)
            aa_baseline_raw = d.shell("aa dump -r", timeout=8, check=False)
            wms_baseline = _scene4_wms_window_inventory(wms_baseline_raw)
            aa_baseline = _scene4_aa_process_inventory(aa_baseline_raw)
            visible_window_ids = []
            taskmanager_pids = {row.get("pid") for row in aa_baseline.get("rows", [])
                if row.get("bundle") == "com.huawei.hmos.taskmanager" and row.get("pid") is not None}
            for wms_row in wms_baseline.get("rows", []):
                pid, win_id = wms_row.get("pid"), wms_row.get("win_id")
                if pid not in taskmanager_pids or win_id is None:
                    continue
                detail = d.shell("hidumper -s WindowManagerService -a " + quote(f"-w {win_id}"),
                                 timeout=5, check=False)
                parsed = _scene4_wms_window_snapshot(wms_baseline_raw, detail, pid, win_id)
                if parsed.get("known") and parsed.get("visible") is True:
                    visible_window_ids.append(int(win_id))
            popup_baseline.update({"window_ids": sorted(set(visible_window_ids)),
                "window_inventory_complete": bool(wms_baseline.get("complete") and aa_baseline.get("complete")),
                "window_inventory_raw": wms_baseline_raw, "aa_inventory_raw": aa_baseline_raw,
                "window_inventory_count": len(wms_baseline.get("rows", [])),
                "window_inventory_reported_count": wms_baseline.get("reported_count"),
                "window_inventory_parsed_count": wms_baseline.get("parsed_count"),
                "taskmanager_visible_window_ids": sorted(set(visible_window_ids))})
    except Exception as exc:
        session.result.setdefault("ui_evidence_errors", []).append({
            "case_id": case_id, "phase": "baseline", "error": str(exc)})
    for bundle in session.config["test_apps"]:
        try:
            d.force_stop(bundle)
        except Exception as exc:
            session.result.setdefault("case_setup_errors", []).append({
                "case_id": case_id, "bundle": bundle, "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
    case_begin_uptime = None
    case_begin_wall_ms = None
    fast_frame_capture_state = None
    fast_frame_proof = None
    try:
        session.mark("case_begin", case_id=case_id, bundles=bundles, peak_mib=peak, hold_seconds=hold,
                     strategy=strategy, daily_mib=cfg["daily_mib"])
        case_begin_uptime, case_begin_wall_ms = d.clock_pair()
        if popup_baseline is not None:
            popup_baseline["case_begin_wall_ms"] = case_begin_wall_ms
            popup_baseline["case_begin_uptime_seconds"] = case_begin_uptime
            popup_baseline["case_begin_boottime_seconds"] = case_begin_uptime
            if scene4_v3_profile:
                baseline_record = {"case_id": case_id,
                    **{key: value for key, value in popup_baseline.items() if key not in ("raw", "window_inventory_raw", "aa_inventory_raw")}}
                session.result.setdefault("popup_case_baselines", []).append(baseline_record)
                baseline_path = session.output / "ui" / f"{case_id}_baseline_missions.txt"
                baseline_path.parent.mkdir(parents=True, exist_ok=True)
                baseline_path.write_text(popup_baseline.get("raw", ""), encoding="utf-8")
                (session.output / "ui" / f"{case_id}_baseline_wms.txt").write_text(
                    popup_baseline.get("window_inventory_raw", ""), encoding="utf-8")
                (session.output / "ui" / f"{case_id}_baseline_aa.txt").write_text(
                    popup_baseline.get("aa_inventory_raw", ""), encoding="utf-8")
        if scene4_v3_profile and case_begin_uptime is not None:
            try:
                fast_frame_capture_state = session.start_scene4_frame_capture(
                    case_id, case_begin_uptime,
                    float(cfg.get("ready_timeout_seconds", 180)) + float(hold) + 10.0)
                session.result.setdefault("scene4_fast_frame_capture_started", []).append({
                    "case_id": case_id, "case_begin_boottime_s": case_begin_uptime,
                    "capture_period_seconds": SCENE4_FRAME_PERIOD_SECONDS,
                    "preflight_complete": fast_frame_capture_state.get("preflight_complete"),
                    "manifest_path": fast_frame_capture_state.get("manifest_path")})
            except Exception as exc:
                session.result.setdefault("case_evidence_gaps", []).append({
                    "case_id": case_id, "reason": "fast_frame_capture_start_failed", "error": str(exc)})
                session.result["measurement_evidence_complete"] = False
    except Exception as exc:
        session.result.setdefault("case_evidence_gaps", []).append({
            "case_id": case_id, "reason": "case_begin_host_marker_failed", "error": str(exc)})
        session.result["measurement_evidence_complete"] = False
    errors = {}
    seed = cfg.get("seed", 1) if seed is None else seed
    case_started_mono = time.monotonic()
    case_deadline = case_started_mono + max(1, cfg["lifetime_seconds"] - 60)
    ready_deadline = min(case_started_mono + cfg["ready_timeout_seconds"], case_deadline)
    records: dict[str, list[dict]] = {bundle: [] for bundle in bundles}
    daily_records: dict[str, list[dict]] = {bundle: [] for bundle in bundles}
    profile_daily_journal_complete: set[str] = set()
    profile_peak_journal_complete: set[str] = set()
    observed_pids: dict[int, list[dict]] = {}
    pre_promote_identity_checks: list[dict] = []
    observed_daily_private = {bundle: 0 for bundle in bundles}
    reclaim_diagnostics: dict[str, Any] = {"enabled": session.scene == 4, "snapshots": []}
    peak_reclaim_sampled: set[str] = set()
    hold_reclaim_sampled: set[str] = set()
    release_reclaim_sampled: set[str] = set()
    def observe_processes(bundle: str, uid: int, processes: dict, uptime: float) -> None:
        current = {(int(process["pid"]), str(process["start_ticks"])) for process in processes.values()}
        for pid, identities in observed_pids.items():
            for identity in identities:
                if identity["bundle"] == bundle and identity.get("first_absent_uptime") is None:
                    if (pid, str(identity["start_ticks"])) not in current:
                        identity["first_absent_uptime"] = uptime
                        identity["end_reason"] = "pid_absent_or_replaced"
        for process in processes.values():
            pid, ticks = int(process["pid"]), str(process["start_ticks"])
            identities = observed_pids.setdefault(pid, [])
            identity = next((item for item in identities if item["bundle"] == bundle and
                             item["uid"] == uid and str(item["start_ticks"]) == ticks), None)
            if identity is None:
                identity = {"bundle": bundle, "pid": pid, "uid": uid, "start_ticks": ticks,
                            "first_uptime": uptime, "last_seen_uptime": uptime,
                            "first_absent_uptime": None, "end_reason": None}
                identities.append(identity)
            else:
                identity["last_seen_uptime"] = uptime
    def launch(bundle):
        try:
            app_memory = roles["per_app"][bundle] if roles else {"daily_mib": cfg["daily_mib"], "peak_mib": peak}
            session.launch_test(bundle, case_id, app_memory["daily_mib"], app_memory["peak_mib"], hold,
                                cfg["lifetime_seconds"], file_mib=file_mib,
                                seed=seed, await_peak=True,
                                no_periodic_msync=bool(cfg.get("oracle_file_dirty_variant", False)))
        except Exception as exc:
            errors[bundle] = str(exc)

    def collect_daily(targets: list[str], deadline: float) -> bool:
        ready = set()
        while time.monotonic() < deadline:
            failed = set()
            for bundle in targets:
                uptime = float(d.shell("cat /proc/uptime").split()[0])
                uid = d.app(bundle)["uid"]
                processes = d.app_processes(bundle)
                observe_processes(bundle, uid, processes, uptime)
                rows = session.journal(bundle, case_id + "_daily")
                daily_records[bundle] = rows
                daily_ready = next((row for row in rows if row.get("event") == "daily_ready"), None)
                if daily_ready:
                    ready.add(bundle)
                    observed_daily_private[bundle] = max(observed_daily_private[bundle], daily_ready.get("ready_bytes", 0))
                elif (any(row.get("event") == "allocation_failed" for row in rows) or
                      bundle in errors or (rows and not processes)):
                    failed.add(bundle)
            if daily_promotion_targets(targets, ready, set()) == targets:
                return True
            if failed:
                session.result.setdefault("warmup_failures", []).extend(
                    {"case_id": case_id, "bundle": bundle,
                     "reason": daily_records.get(bundle, [{}])[-1].get("event", "launch_failed"),
                     "events": daily_records.get(bundle, [])} for bundle in sorted(failed))
                return False
            time.sleep(0.5)
        session.result.setdefault("warmup_failures", []).extend(
            {"case_id": case_id, "bundle": bundle, "reason": "daily_ready_timeout",
             "events": daily_records.get(bundle, [])} for bundle in targets if bundle not in ready)
        return False

    if strategy == "foreground_popup_with_background_pressure" and bundles:
        launch(bundles[0])
        anchor_daily = collect_daily([bundles[0]], ready_deadline)
        rest = [bundle for bundle in bundles[1:]] if anchor_daily else []
        if anchor_daily and rest:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(rest)) as pool:
                list(pool.map(launch, rest))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(bundles)) as pool:
            list(pool.map(launch, bundles))
    barrier_deadline = ready_deadline
    anchor_failed = strategy == "foreground_popup_with_background_pressure" and bundles and not anchor_daily
    ready_targets = {bundle for bundle in bundles if any(
        row.get("event") == "daily_ready" for row in daily_records.get(bundle, []))}
    failed_targets = {row["bundle"] for row in session.result.get("warmup_failures", [])
                      if row.get("case_id") == case_id}
    promote_targets = daily_promotion_targets(bundles, ready_targets, failed_targets)
    daily_barrier_complete = promote_targets == bundles
    if not daily_barrier_complete and not anchor_failed:
        remaining = [bundle for bundle in bundles if not any(
            row.get("event") == "daily_ready" for row in daily_records.get(bundle, []))]
        daily_barrier_complete = collect_daily(remaining, barrier_deadline) if remaining else daily_barrier_complete
        ready_targets = {bundle for bundle in bundles if any(
            row.get("event") == "daily_ready" for row in daily_records.get(bundle, []))}
        failed_targets = {row["bundle"] for row in session.result.get("warmup_failures", [])
                          if row.get("case_id") == case_id}
        promote_targets = daily_promotion_targets(bundles, ready_targets, failed_targets)
        daily_barrier_complete = promote_targets == bundles
    start_ms = 0
    start_uptime = 0.0
    wall_uptime_anchor = None
    if daily_barrier_complete:
        # The daily journal's PID is authoritative for the readiness event.
        # Read that exact /proc entry directly: cgroup.procs can transiently be
        # empty even while the process itself remains alive.
        barrier_failures = []
        for bundle in bundles:
            rows = daily_records.get(bundle, [])
            ready_row = next((row for row in reversed(rows) if row.get("event") == "daily_ready"), None)
            check = {"case_id": case_id, "bundle": bundle, "attempts": [],
                     "verified": False, "reason": None}
            try:
                installed_uid = int(d.app(bundle)["uid"])
            except Exception as exc:
                installed_uid = None
                check["reason"] = "installed_bundle_uid_unavailable"
                check["setup_error"] = str(exc)
            try:
                ready_pid = int(ready_row.get("pid", -1)) if ready_row else -1
                ready_uid = int(ready_row.get("uid", -1)) if ready_row else -1
            except (TypeError, ValueError, AttributeError):
                ready_pid, ready_uid = -1, -1
            if ready_row is None or ready_pid <= 0:
                check["reason"] = "daily_ready_pid_missing"
            elif ready_uid != installed_uid:
                check["reason"] = "daily_ready_uid_differs_from_installed_bundle"
            for attempt_number in range(1, 4):
                if check["reason"]:
                    break
                remaining = ready_deadline - time.monotonic()
                if remaining <= 0:
                    check["reason"] = "pre_promote_identity_deadline_elapsed"
                    break
                timeout = min(20.0, max(1.0, remaining))
                attempt = {"attempt": attempt_number, "timeout_seconds": timeout}
                try:
                    observation = d.inspect_process_identity(ready_pid, timeout=timeout)
                    attempt["raw_response"] = observation.get("raw", "")
                    attempt["parse_error"] = observation.get("parse_error")
                    identity = observation.get("identity")
                    attempt["identity"] = identity
                    attempt["uptime_seconds"] = observation.get("uptime_seconds")
                    identity_error = proc_identity_mismatch(
                        identity, ready_pid, ready_uid,
                        ready_row.get("start_ticks") if ready_row else None)
                    if identity is None:
                        attempt["error"] = observation.get("parse_error") or identity_error
                    elif identity_error:
                        attempt["error"] = identity_error
                    elif ready_uid != installed_uid:
                        attempt["error"] = "daily_ready_uid_differs_from_installed_bundle"
                    else:
                        known = [item for item in observed_pids.get(ready_pid, [])
                                 if item.get("bundle") == bundle and int(item.get("uid", -1)) == ready_uid]
                        active_known = [item for item in known if item.get("first_absent_uptime") is None]
                        ready_ticks = ready_row.get("start_ticks")
                        if ready_ticks is not None and str(ready_ticks) != str(identity.get("start_ticks")):
                            attempt["error"] = "daily_ready_start_ticks_mismatch"
                        elif active_known and any(str(item.get("start_ticks")) !=
                                                  str(identity.get("start_ticks")) for item in active_known):
                            attempt["error"] = "observed_process_start_ticks_changed"
                        else:
                            check["verified"] = True
                            check["identity_source"] = "direct_/proc/pid/stat_and_status"
                            check["identity"] = identity
                            check["reason"] = None
                            uptime = float(observation["uptime_seconds"])
                            exact = next((item for item in known if str(item.get("start_ticks")) ==
                                          str(identity.get("start_ticks"))), None)
                            for item in active_known:
                                if str(item.get("start_ticks")) != str(identity.get("start_ticks")):
                                    item["first_absent_uptime"] = uptime
                                    item["end_reason"] = "pid_replaced_by_direct_proc_observation"
                            if exact is None:
                                observed_pids.setdefault(ready_pid, []).append({
                                    "bundle": bundle, "pid": ready_pid, "uid": ready_uid,
                                    "start_ticks": str(identity["start_ticks"]),
                                    "first_uptime": uptime, "last_seen_uptime": uptime,
                                    "first_absent_uptime": None, "end_reason": None})
                            else:
                                # A prior cgroup sample can omit a still-live process;
                                # exact PID+start_ticks proc evidence disproves that gap.
                                exact["last_seen_uptime"] = uptime
                                exact["first_absent_uptime"] = None
                                exact["end_reason"] = None
                except Exception as exc:
                    attempt["error"] = str(exc)
                    partial = getattr(exc, "stdout", None)
                    if isinstance(partial, bytes):
                        partial = partial.decode("utf-8", errors="replace")
                    if partial:
                        attempt["raw_response"] = str(partial)
                check["attempts"].append(attempt)
                if check["verified"]:
                    break
                if attempt_number < 3 and time.monotonic() < ready_deadline:
                    time.sleep(0.25)
            if not check["verified"] and check["reason"] is None:
                check["reason"] = next((attempt.get("error") for attempt in reversed(check["attempts"])
                                         if attempt.get("error")), "direct_proc_identity_unverified")
            safe_bundle = re.sub(r"[^a-zA-Z0-9_.-]", "_", bundle)
            check_path = session.output / "identity_checks" / f"{case_id}_{safe_bundle}.json"
            save(check_path, check)
            check_summary = {k: v for k, v in check.items() if k != "attempts"}
            check_summary["raw_evidence_path"] = str(check_path.relative_to(session.output))
            check_summary["attempt_count"] = len(check["attempts"])
            pre_promote_identity_checks.append(check_summary)
            if not check["verified"]:
                barrier_failures.append({"bundle": bundle, "reason": check["reason"],
                                         "raw_evidence_path": check_summary["raw_evidence_path"]})
        if barrier_failures:
            daily_barrier_complete = False
            session.result.setdefault("case_evidence_gaps", []).append({
                "case_id": case_id, "reason": "daily_ready_process_lost_before_promote",
                "bundles": barrier_failures})
            session.result.setdefault("framework_preparation_failures", []).append({
                "case_id": case_id, "phase": "pre_promote_identity_barrier",
                "aborted_targets": [failure["bundle"] for failure in barrier_failures],
                "failures": barrier_failures})
        if time.monotonic() >= ready_deadline:
            daily_barrier_complete = False
            session.result.setdefault("case_evidence_gaps", []).append({
                "case_id": case_id, "reason": "ttl_safe_observation_deadline_elapsed_before_promote"})
    if daily_barrier_complete and session.scene == 4:
        try:
            daily_start = time.monotonic()
            reclaim_diagnostics["snapshots"].append(
                scene4_reclaim_snapshot(session, case_id, bundles, "daily_cold_begin"))
            reclaim_diagnostics["daily_cold_seconds_requested"] = float(cfg.get("daily_cold_seconds", 0))
            if not scene4_v3_profile:
                time.sleep(float(cfg.get("daily_cold_seconds", 0)))
                reclaim_diagnostics["snapshots"].append(
                    scene4_reclaim_snapshot(session, case_id, bundles, "daily_cold_end"))
                reclaim_diagnostics["daily_cold_seconds_observed"] = time.monotonic() - daily_start
            else:
                # The profile's 15-second daily-cold window starts only after
                # marker/reader/private-memory preparation below, so T cannot
                # drift behind slow setup reads. The window itself is measured.
                reclaim_diagnostics["daily_cold_seconds_observed"] = None
                reclaim_diagnostics["profile_window_deferred_until_measurement_begin"] = True
        except Exception as exc:
            reclaim_diagnostics.setdefault("errors", []).append({"phase": "daily_cold", "error": str(exc)})
            session.result.setdefault("telemetry_errors", []).append({
                "case_id": case_id, "source": "scene4_daily_cold_reclaim_snapshot",
                "error": str(exc), "classification": "reclaim metrics unknown; no missing process inferred"})
    if daily_barrier_complete:
        try:
            trace_name = f"trace_{case_id}.ftrace"
            session.start_trace(trace_name, 262144,
                                ("ability",))
            session.mark("measurement_begin_" + case_id)
            if not session.ensure_kernel_dmesg_reader(case_id):
                raise RuntimeError("owned dmesg reader could not be restored before case BEGIN")
            for bundle in bundles:
                observed_daily_private[bundle] = d.private_memory(bundle)
            start_uptime, start_ms = d.clock_pair()
            if popup_baseline is not None:
                popup_baseline["peak_start_uptime_seconds"] = start_uptime
                popup_baseline["peak_start_ms"] = start_ms
                session.result.setdefault("popup_case_baselines", []).append({
                    "case_id": case_id, **{k: v for k, v in popup_baseline.items() if k != "raw"}})
                baseline_file = session.output / "ui" / f"{case_id}_baseline_missions.txt"
                baseline_file.parent.mkdir(parents=True, exist_ok=True)
                baseline_file.write_text(popup_baseline.get("raw", ""), encoding="utf-8")
            session.mark_kernel_dmesg_boundary(case_id, "BEGIN", start_uptime, start_ms)
            anchor_rows = [(bundle, row) for bundle, rows in daily_records.items() for row in rows
                           if row.get("event") == "daily_ready" and row.get("time_ms") and row.get("monotonic_ns")]
            if anchor_rows:
                anchor_bundle, anchor = max(anchor_rows, key=lambda pair: pair[1]["monotonic_ns"])
                wall_uptime_anchor = {"wall_ms": int(anchor["time_ms"]),
                                      "uptime_seconds": float(anchor["monotonic_ns"]) / 1e9,
                                      "bundle": anchor_bundle, "pid": anchor.get("pid"), "uid": anchor.get("uid")}
                session.result.setdefault("system_event_time_anchors", []).append({"case_id": case_id, **wall_uptime_anchor})
            else:
                raise RuntimeError("daily journal lacks paired wall/uptime anchor")
        except Exception as exc:
            daily_barrier_complete = False
            session.result.setdefault("sampling_errors", []).append({"case_id": case_id,
                                                                       "error": str(exc), "classification": "unknown before peak; promote withheld"})
    profile_window = None
    profile_window_deadline_boot = None
    dirty_prepare_result = None
    dirty_prepare_complete = True
    if daily_barrier_complete and scene4_v3_profile and bool(cfg.get("oracle_file_dirty_variant", False)):
        try:
            redirty_required = bool(cfg.get("oracle_prepare_redirty", True))
            if not redirty_required and cfg.get("oracle_backend") != "force_swapout":
                raise ValueError("Background-only preparation is restricted to the anonymous disk experiment")
            prepare = _scene4_dirty_prepare if redirty_required else _scene4_minimize_prepare
            dirty_prepare_result = prepare(session, case_id, bundles,
                float(cfg.get("oracle_redirty_prepare_timeout_s", 90)))
            dirty_prepare_result["redirty_required"] = redirty_required
            # Preserve preparation evidence even if the subsequent clock read fails.
            reclaim_diagnostics["dirty_file_prepare"] = dirty_prepare_result
            dirty_prepare_result["completed_boottime_s"] = float(d.shell("cat /proc/uptime").split()[0])
            reclaim_diagnostics["dirty_file_prepare"] = dirty_prepare_result
            dirty_prepare_complete = bool(dirty_prepare_result.get("complete"))
            if not dirty_prepare_complete:
                session.result.setdefault("case_evidence_gaps", []).append({
                    "case_id": case_id, "reason": "dirty_file_redirty_or_minimize_unproven",
                    "bundles": dirty_prepare_result.get("errors", {})})
                session.result["measurement_evidence_complete"] = False
        except Exception as exc:
            dirty_prepare_complete = False
            reclaim_diagnostics["dirty_file_prepare"] = {
                "complete": False, "error": str(exc), "prior_prepare_result": dirty_prepare_result,
                "classification": "required preparation or its completion timestamp unknown"}
            session.result.setdefault("case_evidence_gaps", []).append({
                "case_id": case_id, "reason": "dirty_file_prepare_failed", "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
    if scene4_v3_profile:
        d.command_timeout = measurement_command_timeout
    if daily_barrier_complete and scene4_v3_profile:
        window_seconds = float(cfg.get("daily_cold_seconds", 0))
        # Anchor the fixed window immediately after the common preparation
        # barrier. Setup/registration failures consume this same interval and
        # can never silently shorten it or move the promote deadline.
        window_anchor_host_start = time.monotonic()
        try:
            window_anchor_boot = float(d.shell("cat /proc/uptime").split()[0])
        except Exception as exc:
            window_anchor_boot = None
            session.result.setdefault("case_evidence_gaps", []).append({
                "case_id": case_id, "reason": "fixed_profile_window_boot_anchor_missing",
                "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
        window_anchor_host_end = time.monotonic()
        window_start_host = (window_anchor_host_start + window_anchor_host_end) / 2.0
        host_deadline = window_start_host + window_seconds
        start_boot = window_anchor_boot
        deadline_boot = (window_anchor_boot + window_seconds) if window_anchor_boot is not None else None
        profile_window_deadline_boot = deadline_boot
        try:
            if dirty_prepare_complete and deadline_boot is not None:
                profile_window = _scene4_legacy_v3_window(
                    session, case_id, bundles, daily_records, window_seconds, peak,
                    int(cfg.get("daily_mib", 0)),
                    window_start_host_monotonic=window_start_host,
                    window_start_boottime_s=start_boot)
                # Function preflight is inside the already-running fixed
                # interval. It cannot shift BOOTTIME T or add a wait.
            else:
                # Preserve the A/B timing even when the dirty-file proof is
                # unknown. No schema row is published and no reclaim action is
                # attempted for an unproven dirty input.
                pass
        except Exception as exc:
            reclaim_diagnostics["peak_transition_profile"] = {
                "error": str(exc), "window_seconds": window_seconds,
                "window_start_boottime_s": start_boot,
                "deadline_boottime_s": deadline_boot,
                "host_deadline_monotonic": host_deadline,
                "window_anchor_host_start_monotonic": window_anchor_host_start,
                "window_anchor_host_end_monotonic": window_anchor_host_end,
                "registered": False,
                "classification": "preflight failed; preserve fixed promote deadline and record oracle unknown"}
            session.result.setdefault("case_evidence_gaps", []).append({
                "case_id": case_id, "reason": "peak_transition_profile_preflight_failed",
                "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
            session.result.setdefault("telemetry_errors", []).append({
                "case_id": case_id, "source": f"scene4_peak_transition_schema{3 if cfg.get('oracle_backend') == 'proc_file_writeback' else 2}", "error": str(exc),
                "classification": "oracle effect unknown; workload timing continues unchanged"})
        while time.monotonic() < host_deadline:
            time.sleep(min(0.05, max(0.0, host_deadline - time.monotonic())))
        if profile_window is not None:
            # Request stop at the fixed deadline. Do not join before dispatching
            # promote. A read-only snapshot may still be in progress; that alone
            # is not a measurement failure and must never hold application start.
            profile_window["stop"].set()
            state = profile_window["writer_state"]
            with profile_window["writer_lock"]:
                state["stop_requested_host_monotonic_ns"] = time.monotonic_ns()
                state["snapshot_inflight_at_stop"] = bool(state.get("snapshot_inflight"))
                state["publish_inflight_at_stop"] = bool(state.get("publish_inflight"))
            reclaim_diagnostics["peak_transition_profile"] = {
                "event_id": profile_window["event_id"],
                "schedule_remote": profile_window["schedule_remote"],
                "local_schedule_path": str(Path(profile_window["local_schedule_path"]).relative_to(session.output)),
                "observations_path": str(Path(profile_window["observations_path"]).relative_to(session.output)),
                "window_start_boottime_s": start_boot,
                "deadline_boottime_s": deadline_boot,
                "host_deadline_monotonic": host_deadline,
                "window_anchor_host_start_monotonic": window_anchor_host_start,
                "window_anchor_host_end_monotonic": window_anchor_host_end,
                "registered": bool(profile_window["writer_state"].get("registered")),
                "rows_written": int(profile_window["writer_state"].get("rows_written", 0)),
                "refresh_rounds": int(profile_window["writer_state"].get("rounds", 0)),
                "candidate_bundles": list(profile_window["writer_state"].get("candidate_bundles", [])),
                "errors": list(profile_window["writer_state"].get("errors", [])),
                "planned_needed_bytes": profile_window["planned_needed_bytes"],
                "control_readback": profile_window["control_readback"],
                "writer_stop_requested_at_deadline": True,
                "snapshot_inflight_at_stop": bool(state.get("snapshot_inflight_at_stop")),
                "publish_inflight_at_stop": bool(state.get("publish_inflight_at_stop"))}
        elif "peak_transition_profile" not in reclaim_diagnostics:
            reclaim_diagnostics["peak_transition_profile"] = {
                "window_start_boottime_s": start_boot, "deadline_boottime_s": deadline_boot,
                "host_deadline_monotonic": host_deadline,
                "window_anchor_host_start_monotonic": window_anchor_host_start,
                "window_anchor_host_end_monotonic": window_anchor_host_end,
                "registered": False,
                "rows_written": 0, "writer_stopped_by_deadline": True,
                "errors": ["dirty_file_prepare_incomplete_no_schedule_written"]
                    if not dirty_prepare_complete else ["peak_transition_window_preflight_failed"]}
    promote_errors = {}
    promote_call_timings: dict[str, dict] = {}
    promote_timing_lock = threading.Lock()
    if daily_barrier_complete:
        promote_started_ms = (int(start_ms + (profile_window_deadline_boot - start_uptime) * 1000)
                              if profile_window_deadline_boot is not None else start_ms)
        if profile_window is not None:
            dispatch_host_start = time.monotonic()
            reclaim_diagnostics["peak_transition_profile"]["promote_dispatch_host_start_monotonic"] = dispatch_host_start
            reclaim_diagnostics["peak_transition_profile"]["promote_expected_wall_ms_from_begin_anchor"] = promote_started_ms
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(peak_targets)) as pool:
            def timed_promote(bundle: str):
                host_start_ns = time.monotonic_ns()
                try:
                    return d.promote(bundle, case_id)
                finally:
                    host_end_ns = time.monotonic_ns()
                    with promote_timing_lock:
                        promote_call_timings[bundle] = {
                            "host_start_monotonic_ns": host_start_ns,
                            "host_end_monotonic_ns": host_end_ns,
                            "duration_seconds": (host_end_ns-host_start_ns)/1e9}
            futures = {pool.submit(timed_promote, bundle): bundle for bundle in peak_targets}
            if profile_window is not None:
                dispatch_submitted_host = time.monotonic()
                reclaim_diagnostics["peak_transition_profile"]["promote_requests_submitted_host_monotonic"] = \
                    dispatch_submitted_host
                reclaim_diagnostics["peak_transition_profile"]["promote_requests_submitted_host_monotonic_ns"] = \
                    time.monotonic_ns()
                clock_host_start = time.monotonic()
                try:
                    reclaim_diagnostics["peak_transition_profile"]["promote_dispatch_following_boottime_sample_s"] = \
                        float(d.shell("cat /proc/uptime").split()[0])
                except Exception as exc:
                    reclaim_diagnostics["peak_transition_profile"]["promote_dispatch_boottime_sample_error"] = str(exc)
                reclaim_diagnostics["peak_transition_profile"]["promote_dispatch_clock_sample_host_start"] = clock_host_start
                reclaim_diagnostics["peak_transition_profile"]["promote_dispatch_clock_sample_host_end"] = time.monotonic()
            for future, bundle in futures.items():
                try:
                    future.result()
                except Exception as exc:
                    promote_errors[bundle] = str(exc)
        if profile_window is not None:
            # The join is strictly after promote calls have completed. An
            # unfinished read-only worker does not invalidate the case; only a
            # schedule publication whose measured interval crossed T is a gap.
            profile_window["thread"].join(timeout=0)
            state = profile_window["writer_state"]
            with profile_window["writer_lock"]:
                profile_summary = reclaim_diagnostics["peak_transition_profile"]
                snapshots = list(state.get("snapshot_intervals", []))
                publications = list(state.get("schedule_publish_intervals", []))
                profile_summary.update({
                    "writer_alive_after_promote_completion": profile_window["thread"].is_alive(),
                    "writer_stop_requested_host_monotonic_ns": state.get("stop_requested_host_monotonic_ns"),
                    "snapshot_intervals": snapshots,
                    "schedule_publish_intervals": publications,
                    "publish_after_stop_rejected": int(state.get("publish_after_stop_rejected", 0)),
                    "registered": bool(state.get("registered")),
                    "rows_written": int(state.get("rows_written", 0)),
                    "refresh_rounds": int(state.get("rounds", 0)),
                    "candidate_bundles": list(state.get("candidate_bundles", [])),
                    "promote_call_timings": dict(promote_call_timings),
                    "errors": list(state.get("errors", []))})
            cutoff_status = _scene4_profile_cutoff_status(
                bool(state.get("snapshot_inflight_at_stop")),
                profile_window["thread"].is_alive(), publications)
            profile_summary["writer_cutoff_status"] = cutoff_status
            crossed = cutoff_status["late_publications"]
            if crossed:
                profile_summary["late_schedule_publication"] = True
                session.result.setdefault("case_evidence_gaps", []).append({
                    "case_id": case_id,
                    "reason": "schedule_publication_started_before_but_completed_after_fixed_deadline",
                    "publications": crossed})
                session.result["measurement_evidence_complete"] = False
        if promote_errors:
            session.result.setdefault("promote_errors", []).append({"case_id": case_id, "errors": promote_errors})
        time.sleep(1)
        if strategy == "foreground_popup_with_background_pressure" and bundles:
            try:
                session.device.focus(peak_targets[0])
                foreground_restore_ms = session.safe_now_ms(case_id, "foreground_restore")
            except Exception as exc:
                foreground_restore_ms = None
                session.result.setdefault("sampling_errors", []).append({
                    "case_id": case_id, "stage": "foreground_restore", "error": str(exc),
                    "classification": "unknown; foreground restore unconfirmed"})
                session.result["measurement_evidence_complete"] = False
        else:
            foreground_restore_ms = None
    else:
        foreground_restore_ms = None
        promote_started_ms = None
        try:
            session.mark("daily_barrier_failed", case_id=case_id)
        except Exception as exc:
            session.result.setdefault("case_evidence_gaps", []).append({
                "case_id": case_id, "reason": "daily_barrier_host_marker_failed", "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
    deadline = ready_deadline
    observed_peak = {bundle: 0 for bundle in bundles}
    observed_peak_private = {bundle: 0 for bundle in bundles}
    completed = set()
    last_ui = 0.0
    foreground_history = []
    while daily_barrier_complete and time.monotonic() < deadline:
        for bundle in bundles:
            if time.monotonic() >= deadline:
                break
            processes = None
            try:
                uptime = float(d.shell("cat /proc/uptime").split()[0])
                uid = d.app(bundle)["uid"]
                processes = d.app_processes(bundle)
                observe_processes(bundle, uid, processes, uptime)
            except Exception as exc:
                # Process/cgroup sampling is supplemental to the native journal
                # and marker-bounded kernel evidence. Keep it unknown without
                # discarding independently complete event classification.
                session.result.setdefault("telemetry_errors", []).append({
                    "case_id": case_id, "bundle": bundle, "source": "process_identity_sample",
                    "error": str(exc), "classification": "unknown; no process exit inferred"})
            try:
                warm_rows = session.journal(bundle, case_id + "_daily")
                daily_records[bundle] = warm_rows
                rows = session.journal(bundle, case_id)
                records[bundle] = rows
                if rows:
                    observed_peak[bundle] = max(observed_peak[bundle], max(row["anon_bytes"] + row["file_bytes"] for row in rows))
                    if rows[-1].get("phase") in ("peak_allocating", "peak"):
                        try:
                            observed_peak_private[bundle] = max(observed_peak_private[bundle], d.private_memory(bundle))
                        except Exception as exc:
                            session.result.setdefault("telemetry_errors", []).append({
                                "case_id": case_id, "bundle": bundle, "source": "private_memory_sample",
                                "error": str(exc), "classification": "unknown private-memory value"})
                    if session.scene == 4 and bundle not in peak_reclaim_sampled and any(
                            row.get("event") == "peak_reached" for row in rows):
                        snap = scene4_reclaim_snapshot(session, case_id, [bundle], "peak_reached")
                        reclaim_diagnostics["snapshots"].append(snap)
                        peak_reclaim_sampled.add(bundle)
                    if session.scene == 4 and bundle in peak_reclaim_sampled and bundle not in hold_reclaim_sampled:
                        reached_row = next((row for row in rows if row.get("event") == "peak_reached"), None)
                        hold_sample_seconds = min(7.0, max(1.0, float(hold) / 2.0))
                        if reached_row and rows[-1].get("phase") == "peak" and (
                                time.monotonic_ns() - int(reached_row["monotonic_ns"])) / 1e9 >= hold_sample_seconds:
                            reclaim_diagnostics["snapshots"].append(
                                scene4_reclaim_snapshot(session, case_id, [bundle], "peak_hold_cold"))
                            hold_reclaim_sampled.add(bundle)
                    if session.scene == 4 and bundle not in release_reclaim_sampled and any(
                            row.get("event") == "startup_ready" for row in rows):
                        reclaim_diagnostics["snapshots"].append(
                            scene4_reclaim_snapshot(session, case_id, [bundle], "post_release_daily"))
                        release_reclaim_sampled.add(bundle)
                if any(row["event"] == "startup_ready" for row in rows):
                    completed.add(bundle)
                if (any(row["event"] in ("allocation_failed", "stopped") for row in rows) or
                        any(row["event"] in ("allocation_failed", "stopped") for row in warm_rows)):
                    completed.add(bundle)
                if (processes is not None and not processes) or bundle in errors:
                    completed.add(bundle)
            except Exception as exc:
                session.result.setdefault("sampling_errors", []).append({"case_id": case_id,
                                                                           "bundle": bundle,
                                                                           "error": str(exc), "source": "native_journal",
                                                                           "classification": "required attempt evidence missing"})
                session.result["measurement_evidence_complete"] = False
        if not scene4_v3_profile and kind == "popup" and time.monotonic() - last_ui >= 45:
            focused = session.ui_evidence(case_id, bundles + [x["bundle"] for x in session.config["real_apps"].values()],
                                          observation_phase="peak")
            foreground_history.append({"time_ms": session.safe_now_ms(case_id, "popup_observation"),
                                       "host_monotonic": time.monotonic(),
                                       "bundles": sorted(focused or [])})
            last_ui = time.monotonic()
        if set(peak_targets).issubset(completed):
            break
        time.sleep(0.5)
    # Final UI/journal evidence must precede cleanup.
    if scene4_v3_profile:
        # The independent screenCap/WMS/AA worker owns popup evidence for this
        # profile. Avoid the legacy 20+ second tree/screenshot RPC in-window.
        session.stop_scene4_frame_capture(case_id)
    elif kind == "popup":
        session.ui_evidence(case_id, bundles + [x["bundle"] for x in session.config["real_apps"].values()],
                             observation_phase="post_release")
    else:
        # Every fixed-suite case gets one in-window UI observation before END,
        # so native popup triggers are not silently omitted from non-popup kinds.
        session.ui_evidence(case_id, bundles + [x["bundle"] for x in session.config["real_apps"].values()],
                            observation_phase="pre_end")
    in_window_ui_observations = [row for row in session.ui_observations.get(case_id, [])
                                 if row.get("success") and row.get("case_attribution_allowed") and
                                 row.get("capture_clock_valid") and
                                 popup_attribution_in_window(
                                     True, row.get("capture_wall_ms"),
                                     popup_baseline.get("case_begin_wall_ms") if popup_baseline else None,
                                     popup_baseline.get("case_end_ms") if popup_baseline else None) and
                                 (not row.get("screenshot_needed") or
                                  (row.get("screenshot_captured") and
                                   popup_attribution_in_window(
                                       True, row.get("screenshot_complete_wall_ms"),
                                       popup_baseline.get("case_begin_wall_ms") if popup_baseline else None,
                                       popup_baseline.get("case_end_ms") if popup_baseline else None)))]
    popup_observation_complete = (None if scene4_v3_profile else bool(in_window_ui_observations and
                                      (kind != "popup" or len(completed) == len(bundles))))
    if not scene4_v3_profile and not popup_observation_complete:
        session.result.setdefault("case_evidence_gaps", []).append({
            "case_id": case_id, "reason": "popup_observation_incomplete_or_attempts_not_terminal",
            "completed_attempts": len(completed), "total_attempts": len(bundles),
            "observations": session.ui_observations.get(case_id, [])})
        session.result["measurement_evidence_complete"] = False
    if session.trace_started and session.trace_segment_name == f"trace_{case_id}.ftrace":
        try:
            session.mark("measurement_end_" + case_id)
        except Exception as exc:
            session.result.setdefault("evidence_collection_errors", []).append({
                "source": "hitrace_case_end_marker", "case_id": case_id, "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
    try:
        end_uptime, end_ms = d.clock_pair()
        measurement_end_uptime, measurement_end_ms = end_uptime, end_ms
        if popup_baseline is not None:
            popup_baseline["case_end_ms"] = end_ms
        session.mark_kernel_dmesg_boundary(case_id, "END", end_uptime, end_ms)
        end_ms += 999
    except Exception as exc:
        measurement_end_uptime, measurement_end_ms = None, None
        end_uptime = max((identity.get("last_seen_uptime", start_uptime)
                          for identities in observed_pids.values() for identity in identities),
                         default=start_uptime)
        end_ms = start_ms
        session.result.setdefault("evidence_collection_errors", []).append({
            "source": "end_clock_pair", "case_id": case_id, "error": str(exc)})
        session.result["measurement_evidence_complete"] = False
    if scene4_v3_profile and fast_frame_capture_state is not None and measurement_end_uptime is not None:
        # END is already stamped before any worker join/remote screenshot cleanup.
        fast_frame_proof = session.finish_scene4_frame_capture(
            case_id, measurement_end_uptime, collection_end_boottime_s=measurement_end_uptime)
        reclaim_diagnostics["fast_frame_capture"] = fast_frame_proof
        popup_observation_complete = bool(fast_frame_proof.get("complete"))
        if not popup_observation_complete:
            observed_gaps = fast_frame_proof.get("capture_gap_seconds", [])
            session.result.setdefault("case_evidence_gaps", []).append({
                "case_id": case_id, "reason": "fast_frame_visual_review_or_coverage_incomplete",
                "errors": fast_frame_proof.get("errors", []),
                "maximum_frame_gap_seconds": fast_frame_proof.get("maximum_observed_gap_seconds",
                                                                   max(observed_gaps, default=None)),
                "manifest_path": fast_frame_proof.get("manifest_path"),
                "review_path": fast_frame_proof.get("review_path")})
            session.result["measurement_evidence_complete"] = False
            # A frame awaiting visual/identity review is unknown, not a
            # negative popup observation. Preserve numeric startup/OOM counts,
            # but do not publish native-popup or total counts as zero.
            session.result["scene4_fast_frame_counts_unknown"] = True
    elif scene4_v3_profile:
        popup_observation_complete = False
        session.result.setdefault("case_evidence_gaps", []).append({
            "case_id": case_id, "reason": "fast_frame_capture_or_end_clock_unknown"})
        session.result["measurement_evidence_complete"] = False
        session.result["scene4_fast_frame_counts_unknown"] = True
    if session.trace_started and session.trace_segment_name == f"trace_{case_id}.ftrace":
        session.finish_trace()
    if daily_barrier_complete:
        try:
            session.collect_system_events(case_id, bundles, start_ms, end_ms,
                                          observed_pids, start_uptime, end_uptime,
                                          wall_uptime_anchor["wall_ms"], wall_uptime_anchor["uptime_seconds"])
        except Exception as exc:
            session.result.setdefault("evidence_collection_errors", []).append({"source": "hisysevent", "case_id": case_id, "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
        try:
            session.read_kernel_trace(case_id, observed_pids, start_uptime, end_uptime)
        except Exception as exc:
            session.result.setdefault("evidence_collection_errors", []).append({
                "source": "kernel_trace_auxiliary", "case_id": case_id,
                "required_evidence_source": "marker_bounded_dmesg",
                "error": str(exc)})
    else:
        end_uptime = start_uptime
        session.result.setdefault("case_evidence_gaps", []).append({"case_id": case_id,
                                                                       "reason": "daily barrier failed; no peak command sent"})
    for bundle in bundles:
        try:
            if scene4_v3_profile:
                daily_proof = _scene4_journal_readback(session, bundle, case_id + "_daily")
                peak_proof = _scene4_journal_readback(session, bundle, case_id, allow_absent=True)
                daily_records[bundle] = daily_proof["rows"]
                records[bundle] = peak_proof["rows"]
                for journal_name, proof in (("daily", daily_proof), ("peak", peak_proof)):
                    proof_path = session.output / "journals" / f"{case_id}_{bundle}_{journal_name}_completeness.json"
                    proof_path.parent.mkdir(parents=True, exist_ok=True)
                    save(proof_path, {key: value for key, value in proof.items()
                                      if key not in ("raw", "rows")})
                    raw_path = session.output / "journals" / f"{case_id}_{bundle}_{journal_name}_readback.jsonl"
                    raw_path.write_text(proof.get("raw", ""), encoding="utf-8")
                if not daily_proof["complete"] or not peak_proof["complete"]:
                    session.result.setdefault("case_evidence_gaps", []).append({
                        "case_id": case_id, "bundle": bundle,
                        "reason": "peak_transition_journal_readback_incomplete",
                        "daily": {key: value for key, value in daily_proof.items() if key not in ("raw", "rows")},
                        "peak": {key: value for key, value in peak_proof.items() if key not in ("raw", "rows")}})
                    session.result["measurement_evidence_complete"] = False
                else:
                    profile_daily_journal_complete.add(bundle)
                    profile_peak_journal_complete.add(bundle)
                warm_rows = daily_records[bundle]
                rows = records[bundle]
            else:
                warm_rows = session.journal(bundle, case_id + "_daily")
                daily_records[bundle] = warm_rows
                rows = session.journal(bundle, case_id)
                records[bundle] = rows
        except Exception as exc:
            session.result.setdefault("evidence_collection_errors", []).append({
                "source": "native_journal", "case_id": case_id, "bundle": bundle,
                "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
            continue
        ready = any(row["event"] == "startup_ready" for row in rows)
        if not any(row["event"] == "daily_ready" for row in warm_rows):
            session.result.setdefault("warmup_failures", []).append({
                "case_id": case_id, "bundle": bundle,
                "reason": warm_rows[-1].get("event") if warm_rows else "daily_ready_unobserved",
                "events": warm_rows})
    if daily_barrier_complete and wall_uptime_anchor:
        try:
            session.collect_kernel_dmesg_case(case_id, observed_pids, bundles, records,
                                              start_uptime, end_uptime,
                                              wall_uptime_anchor["wall_ms"],
                                              wall_uptime_anchor["uptime_seconds"],
                                              scene4_peak_transition=scene4_v3_profile,
                                               startup_failure_bundles=set(peak_targets) if background_donors else None,
                                              daily_attempt_rows=daily_records if scene4_v3_profile else None,
                                              daily_journal_readback_complete_bundles=profile_daily_journal_complete,
                                              journal_readback_complete_bundles=profile_peak_journal_complete,
                                              journal_case_ids={bundle: case_id + "_daily" for bundle in bundles}
                                                if scene4_v3_profile else None)
        except Exception as exc:
            session.result.setdefault("evidence_collection_errors", []).append({
                "source": "kernel_dmesg", "case_id": case_id, "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
    # Startup outcomes are finalized only after dmesg/ftrace/system collection,
    # so a late-confirmed kernel OOM can be linked to the exact peak PID lifetime.
    for bundle in peak_targets:
        session.book.finalize_startup_attempt(
            case_id, bundle, records.get(bundle, []), observed_pids, end_ms,
            f"journals/{case_id}_{bundle}.jsonl", allow_timeout=daily_barrier_complete,
            peak_transition_profile=scene4_v3_profile)
    measured = []
    for bundle in peak_targets:
        rows = records[bundle]
        reached = [row for row in rows if row["event"] == "peak_reached"]
        ready = [row for row in rows if row["event"] == "startup_ready"]
        daily_ready = [row for row in daily_records[bundle] if row["event"] == "daily_ready"]
        peak_begin = [row for row in rows if row["event"] == "peak_begin"]
        terminal_events = [event for event in session.book.events if event.get("case_id") == case_id and
                           event.get("bundle") == bundle and event.get("category") in
                           ("oom_kill", "memory_policy_kill", "startup_failure")]
        memory_failure = bool(terminal_events)
        if scene4_v3_profile and reached and memory_failure:
            outcome = "confirmed_oom_after_peak"
        elif scene4_v3_profile and reached and not ready:
            outcome = "peak_reached_hold_unconfirmed"
        else:
            outcome = attempt_outcome(bool(peak_begin), bool(reached), bool(ready), memory_failure)
        if not daily_barrier_complete and not peak_begin and not memory_failure:
            outcome = "aborted_before_peak"
        terminal_complete = outcome in ("success", "confirmed_memory_failure")
        identities = [identity for entries in observed_pids.values() for identity in entries
                      if identity["bundle"] == bundle]
        measured.append({"bundle": bundle, "measured": terminal_complete,
                         "terminal_complete": terminal_complete,
                         "outcome": outcome,
                         "terminal_events": terminal_events,
                         "process_identities": identities,
                         "peak_observed": bool(reached), "ready_observed": bool(ready),
                         "daily_stage_observed": bool(daily_ready),
                         "peak_begin_observed": bool(peak_begin),
                         "daily_ready_boottime_s": _scene4_event_time(daily_ready[-1]) if daily_ready else None,
                         "peak_begin_boottime_s": _scene4_event_time(peak_begin[0]) if peak_begin else None,
                         "peak_reached_boottime_s": _scene4_event_time(reached[-1]) if reached else None,
                         "startup_ready_boottime_s": _scene4_event_time(ready[-1]) if ready else None,
                         "peak_bytes": max(observed_peak[bundle], reached[-1]["peak_bytes"] if reached else 0),
                         "journal_touched_peak_bytes": max((row.get("anon_bytes", 0) + row.get("file_bytes", 0) for row in rows), default=0),
                         "measured_peak_private_bytes": observed_peak_private[bundle],
                         "measured_daily_private_bytes": observed_daily_private[bundle],
                         "daily_journal_touched_bytes": daily_ready[-1]["ready_bytes"] if daily_ready else 0,
                         "daily_bytes": max(observed_daily_private[bundle], daily_ready[-1]["ready_bytes"] if daily_ready else 0),
                         "failure_stage": next((row.get("phase") for row in rows if row["event"] == "allocation_failed"), None)})
    case_attempts_complete = attempts_complete([row["outcome"] for row in measured], daily_barrier_complete)
    case_kernel_coverage_complete = any(
        row.get("case_id") == case_id and row.get("coverage_complete")
        for row in session.result.get("kernel_dmesg_observations", []))
    dirty_gate_complete = not (scene4_v3_profile and bool(cfg.get("oracle_file_dirty_variant", False))) or \
        bool(dirty_prepare_complete)
    profile_gate_complete = not scene4_v3_profile or bool(
        reclaim_diagnostics.get("peak_transition_profile", {}).get("registered"))
    case_evidence_complete = case_attempts_complete and case_kernel_coverage_complete and \
        dirty_gate_complete and profile_gate_complete
    session.result["measurement_evidence_complete"] = (
        session.result.get("measurement_evidence_complete", True) and case_evidence_complete)
    intervals = []
    for bundle, rows in records.items():
        began = next((row for row in rows if row.get("event") == "peak_begin"), None)
        reached = next((row for row in rows if row.get("event") == "peak_reached"), None)
        ready = next((row for row in rows if row.get("event") == "startup_ready"), None)
        if began and reached:
            intervals.append({"bundle": bundle, "begin_ns": began["monotonic_ns"],
                              "peak_ns": reached["monotonic_ns"],
                              "ready_ns": ready["monotonic_ns"] if ready else None})
    peak_overlap = []
    for interval in intervals:
        for stamp in (interval["begin_ns"], interval["peak_ns"]):
            active = sum(other["begin_ns"] <= stamp <= other["peak_ns"] for other in intervals)
            peak_overlap.append(active)
    residency_overlap = []
    for interval in intervals:
        if interval["ready_ns"] is not None:
            for stamp in (interval["peak_ns"], interval["ready_ns"]):
                active = sum(other["peak_ns"] <= stamp <= other["ready_ns"]
                             for other in intervals if other["ready_ns"] is not None)
                residency_overlap.append(active)
    if strategy == "foreground_popup_with_background_pressure" and bundles:
        try:
            d.focus(peak_targets[0])
            foreground_restore_ms = session.safe_now_ms(case_id, "foreground_restore")
            time.sleep(1)
            session.ui_evidence(case_id, bundles, observation_phase="post_end",
                                allow_case_attribution=False)
        except Exception as exc:
            session.result.setdefault("ui_evidence_errors", []).append({"case_id": case_id, "error": str(exc)})
    owned_cleanup = []
    owned_page_cleanup = []
    for bundle in bundles:
        try:
            d.force_stop(bundle)
            remaining = d.app_processes(bundle)
            app_row = {"bundle": bundle, "force_stop_sent": True,
                       "remaining_processes": list(remaining.values()), "empty": not bool(remaining)}
            owned_cleanup.append(app_row)
            if remaining:
                raise RuntimeError(f"owned app process remains after force-stop: {bundle}")
            page_result = d.remove_owned_case_pages(bundle, case_id)
            owned_page_cleanup.append({"bundle": bundle, "case_id": case_id,
                                       "verified_absent": bool(page_result.get("absent")),
                                       "path": page_result.get("path")})
            if not page_result.get("absent"):
                raise RuntimeError(f"owned backing page absence not verified: {bundle}")
        except Exception as exc:
            session.result.setdefault("case_cleanup_errors", []).append({
                "case_id": case_id, "bundle": bundle, "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
    case_end_marker = None
    try:
        case_end_marker = session.mark("case_end", case_id=case_id, counts=session.book.counts())
    except Exception as exc:
        session.result.setdefault("case_evidence_gaps", []).append({
            "case_id": case_id, "reason": "case_end_host_marker_failed", "error": str(exc)})
        session.result["measurement_evidence_complete"] = False
    native_popup_restoration = session.restore_native_low_memory_dialog(case_id)
    if not native_popup_restoration.get("verified_absent"):
        session.result.setdefault("case_evidence_gaps", []).append({
            "case_id": case_id, "reason": "native_popup_restore_not_verified",
            "restoration": native_popup_restoration})
        session.result["measurement_evidence_complete"] = False
    case_events = [event for event in session.book.events if event.get("case_id") == case_id]
    target_category = {"startup": "startup_failure", "oom": "oom_kill", "popup": "low_memory_popup"}.get(kind)
    target_events = [event for event in case_events if event["category"] in
                     (("low_memory_popup_native", "low_memory_popup_test") if target_category == "low_memory_popup"
                      else ((target_category,) if target_category else ()))]
    result_case = {"case_id": case_id, "bundles": bundles, "peak_mib": peak, "hold_seconds": hold,
            "daily_mib": cfg["daily_mib"], "seed": seed,
            "strategy": strategy,
            "daily_case_suffix": "_daily",
            "daily_stage_complete": daily_stage_complete(bundles, daily_records),
            "daily_barrier_released": daily_barrier_complete,
            "attempts_terminal_complete": case_attempts_complete,
            "kernel_coverage_complete": case_kernel_coverage_complete,
            "case_measurement_complete": case_evidence_complete,
            "peak_start_ms": promote_started_ms, "foreground_restore_ms": foreground_restore_ms,
            "peak_start_ms_semantics": ("deadline_derived_expected_from_begin_clock_anchor"
                if scene4_v3_profile and promote_started_ms is not None else "legacy_recorded_value"),
            "pre_promote_identity_checks": pre_promote_identity_checks,
            "preparation_failure": next((row for row in session.result.get("framework_preparation_failures", [])
                                           if row.get("case_id") == case_id), None),
            "case_outer_interval": {"begin_uptime_seconds": case_begin_uptime,
                                     "begin_wall_ms": case_begin_wall_ms,
                                     "end_uptime_seconds": (case_end_marker or {}).get("device_uptime_seconds")},
            "measurement_interval": {"begin_uptime_seconds": start_uptime,
                                     "begin_wall_ms": start_ms,
                                     "end_uptime_seconds": measurement_end_uptime,
                                     "end_wall_ms": measurement_end_ms},
            "trace_segment": session.trace_segments.get(f"trace_{case_id}.ftrace", {}),
        "ui_capture_interval": {
                "first_wall_ms": min((row["capture_wall_ms"] for row in session.ui_observations.get(case_id, [])
                                       if row.get("capture_wall_ms") is not None), default=None),
                "last_wall_ms": max((row["capture_wall_ms"] for row in session.ui_observations.get(case_id, [])
                                     if row.get("capture_wall_ms") is not None), default=None),
                "case_attribution_capture_count": sum(bool(row.get("case_attribution_allowed") and
                    row.get("capture_clock_valid")) for row in session.ui_observations.get(case_id, []))},
            "foreground_observations": foreground_history,
            "target_trigger": {"category": target_category,
                "observed": (None if scene4_v3_profile and target_category == "low_memory_popup" and
                              fast_frame_proof is not None and not fast_frame_proof.get("complete")
                              else bool(target_events)),
                "evidence": target_events,
                "frame_review_pending": bool(scene4_v3_profile and target_category == "low_memory_popup" and
                    fast_frame_proof is not None and not fast_frame_proof.get("complete"))},
            "popup_observation_complete": popup_observation_complete,
            "fast_frame_capture": fast_frame_proof,
            "popup_observations": session.ui_observations.get(case_id, []),
            "restore_initial_state": native_popup_restoration,
            "owned_app_cleanup": owned_cleanup,
            "owned_page_cleanup": owned_page_cleanup,
            "peak_overlap_max": max(peak_overlap, default=0),
            "peak_residency_overlap_max": max(residency_overlap, default=0),
            "peak_timeline": intervals,
            "start_ms": start_ms, "start_uptime": start_uptime,
            "end_ms": end_ms, "end_uptime": end_uptime,
            "wall_uptime_anchor": wall_uptime_anchor, "measurements": measured,
            "process_identities": {bundle: [identity for entries in observed_pids.values()
                                             for identity in entries if identity["bundle"] == bundle]
                                   for bundle in bundles},
            "launch_errors": errors}
    if session.scene == 4:
        result_case["file_mib"] = file_mib
        if background_donors:
            result_case["memory_roles"] = roles
            result_case["background_donor_observations"] = [{
                "bundle": bundle, "daily_ready": any(r.get("event") == "daily_ready" for r in daily_records[bundle]),
                "unexpected_peak_events": [r for r in records[bundle] if r.get("event") in
                    ("peak_begin", "peak_reached", "startup_ready") or
                    (r.get("event") == "allocation_failed" and r.get("phase") == "peak_allocating")],
                "oom_events": [e for e in case_events if e.get("bundle") == bundle and e.get("category") == "oom_kill"],
                "lifetime_observations": [i for entries in observed_pids.values() for i in entries if i["bundle"] == bundle]
            } for bundle in background_donors]
            if any(x["unexpected_peak_events"] for x in result_case["background_donor_observations"]):
                result_case["case_measurement_complete"] = False
                session.result["measurement_evidence_complete"] = False
        if scene4_v3_profile:
            result_case["dirty_file_prepare"] = reclaim_diagnostics.get("dirty_file_prepare")
            redirty_required = bool(cfg.get("oracle_prepare_redirty", True))
            result_case["dirty_file_prepare_complete"] = dirty_gate_complete if redirty_required else None
            if not redirty_required:
                result_case["background_prepare_complete"] = dirty_gate_complete
                result_case["extra_file_redirty_requested"] = False
        result_case["reclaim_diagnostics"] = reclaim_diagnostics
        result_case["file_sync_proofs"] = {
            bundle: {
                "expected_bytes": int(file_mib) * 1024**2,
                "daily_synced_bytes": max((int(row.get("file_synced_bytes", 0)) for row in daily_records.get(bundle, [])), default=0),
                "peak_synced_bytes": max((int(row.get("file_synced_bytes", 0)) for row in records.get(bundle, [])), default=0),
                "daily_sync_success": any(row.get("event") == "file_sync_complete" for row in daily_records.get(bundle, [])),
                "peak_sync_success": any(row.get("event") == "file_sync_complete" for row in records.get(bundle, [])),
            } for bundle in bundles}
    return result_case

def normalized_cases(cases: list[dict]) -> list[dict]:
    volatile = {"case_id", "created_at", "timestamp", "version_label", "phase", "session"}
    return [{key: value for key, value in case.items() if key not in volatile} for case in cases]

def cases_hash(cases: list[dict]) -> str:
    return canonical_hash(normalized_cases(cases))

def case_definitions(config: dict, subcase: str = "combined", scene_key: str = "scene2") -> list[dict]:
    cfg = config[scene_key]
    kinds = ("popup", "startup", "oom") if subcase == "combined" else (subcase,)
    apps = config["test_apps"]
    levels = cfg.get("calibration_peak_mib_levels", [cfg["peak_mib"]])
    safe_levels = [value for value in levels if 0 < value <= config["dram_gib"] * 1024]
    if not safe_levels:
        raise RuntimeError("校准峰值档位超过配置DRAM或为空")
    peak = max(safe_levels)
    popup_peak = next((value for value in safe_levels if value >= cfg["peak_mib"]), safe_levels[0])
    cases = []
    repetitions = cfg.get("case_repetitions")
    for kind in kinds:
        count = int(repetitions.get(kind, 0)) if repetitions is not None else int(cfg["case_cycles"])
        if count < 0:
            raise RuntimeError(f"{kind}用例次数不能为负数")
        for cycle in range(count):
            profile = cfg.get("case_profiles", {}).get(kind, {})
            ordered = apps[cycle % len(apps):] + apps[:cycle % len(apps)]
            if kind == "startup":
                strategy, hold, requested_peak = ("simultaneous_peak_during_startup", 0, peak)
            elif kind == "oom":
                strategy, hold, requested_peak = ("simultaneous_peak_then_hold",
                                                   max(15, cfg["hold_seconds"]), peak)
            else:
                strategy, hold, requested_peak = ("foreground_popup_with_background_pressure",
                                                   cfg["hold_seconds"], popup_peak)
            strategy = profile.get("strategy", strategy)
            hold = int(profile.get("hold_seconds", hold))
            requested_peak = int(profile.get("peak_mib", requested_peak))
            if not 0 < requested_peak <= config["dram_gib"] * 1024:
                raise RuntimeError(f"{kind}用例峰值超出配置DRAM: {requested_peak}MiB")
            cases.append({"kind": kind, "strategy": strategy, "bundles": ordered,
                          "daily_mib": cfg["daily_mib"], "peak_mib": requested_peak,
                          "hold_seconds": hold, "seed": cfg.get("seed", 1)})
            if scene_key == "scene4":
                cases[-1]["file_mib"] = int(profile.get("file_mib", cfg.get("file_mib", 0)))
                if cfg.get("fixed_background_donors"):
                    cases[-1]["memory_roles"] = memory_roles(cfg, ordered, requested_peak, cases[-1]["file_mib"])
    return cases

def calibration_profiles(config: dict) -> list[dict]:
    """Return only the distinct single-app peak/hold profiles used by this fixed suite."""
    grouped: dict[int, dict] = {}
    for case in case_definitions(config, "combined"):
        peak = int(case["peak_mib"])
        row = grouped.setdefault(peak, {"peak_mib": peak, "hold_seconds": 0, "kinds": []})
        row["hold_seconds"] = max(row["hold_seconds"], int(case["hold_seconds"]))
        if case["kind"] not in row["kinds"]:
            row["kinds"].append(case["kind"])
    return [grouped[peak] for peak in sorted(grouped)]

def scene2(session: Session, subcase: str = "combined", frozen: dict | None = None) -> Path:
    try:
        validate_environment(session, formal=session.profile == "acceptance")
        session.device.dismiss_owned_install_prompts(session.remote + "/install_prompts.json")
        session.enable_oom_trace()
        session.start_kernel_dmesg_reader()
        cfg = session.config["scene4" if session.scene == 4 else "scene2"]
        scene_key = "scene4" if session.scene == 4 else ("scene3" if session.scene == 3 else "scene2")
        expected_cases = case_definitions(session.config, subcase, scene_key)
        if frozen:
            if frozen["suite_hash"] != session.result["suite_hash"]:
                raise RuntimeError("冻结用例配置与本轮配置不一致")
            validate_frozen_scope(frozen, session.config, session.profile)
            if frozen.get("cases_hash") != cases_hash(frozen.get("cases", [])):
                raise RuntimeError("冻结用例文件自哈希不匹配")
            if frozen["cases_hash"] != cases_hash(expected_cases):
                raise RuntimeError("冻结用例定义与当前场景配置不匹配")
            cases = frozen["cases"]
        else:
            cases = expected_cases
        session.result["cases"] = cases
        session.result["cases_hash"] = cases_hash(cases)
        save(session.output / "cases.json", cases)
        session.device.command_timeout = 8
        results = []
        case_loop_complete = True
        for index, case in enumerate(cases):
            case_id = f"{session.id}_c{index:04d}"
            try:
                result = scene2_case(session, case_id, case["bundles"],
                                     case["peak_mib"], case["hold_seconds"], case["strategy"],
                                     case["seed"], case["kind"], case.get("file_mib", 0))
            except Exception as exc:
                case_loop_complete = False
                session.result["execution_complete"] = False
                session.result["measurement_evidence_complete"] = False
                session.result["invalid_reasons"].append(f"case_aborted:{case_id}:{exc}")
                case_rows = {}
                for bundle in case["bundles"]:
                    path = session.output / "journals" / f"{case_id}_{bundle}.jsonl"
                    case_rows[bundle] = json_lines(path.read_text(encoding="utf-8", errors="replace")) if path.exists() else []
                markers = [row for row in session.result.get("kernel_dmesg_markers", [])
                           if row.get("case_id") == case_id]
                has_begin = any(row.get("phase") == "BEGIN" and row.get("written") for row in markers)
                has_end = any(row.get("phase") == "END" and row.get("written") for row in markers)
                session.result.setdefault("case_evidence_gaps", []).append({
                    "case_id": case_id, "reason": "scene2_case_aborted",
                    "error": str(exc), "kernel_begin_marker": has_begin,
                    "kernel_end_marker": has_end,
                    "journaled_attempts": {bundle: [row.get("event") for row in rows]
                                           for bundle, rows in case_rows.items()}})
                partial = []
                for bundle in case["bundles"]:
                    rows = case_rows[bundle]
                    partial.append({"bundle": bundle, "outcome": "unconfirmed",
                                    "terminal_complete": False,
                                    "peak_begin_observed": any(row.get("event") == "peak_begin" for row in rows),
                                    "peak_observed": any(row.get("event") == "peak_reached" for row in rows),
                                    "ready_observed": any(row.get("event") == "startup_ready" for row in rows),
                                    "last_event": rows[-1].get("event") if rows else None,
                                    "last_pid": rows[-1].get("pid") if rows else None})
                result = {"case_id": case_id, "kind": case["kind"],
                          "strategy": case["strategy"], "execution_complete": False,
                          "measurement_complete": False, "fatal_error": str(exc),
                          "kernel_markers": markers, "measurements": partial}
                results.append(result)
                save(session.output / "case_results.json", results)
                break
            result["kind"] = case["kind"]
            result["strategy"] = case.get("strategy", "frozen")
            result["seed"] = case.get("seed", cfg.get("seed", 1))
            results.append(result)
            save(session.output / "case_results.json", results)
            restore = result.get("restore_initial_state", {})
            if not restore.get("verified_absent"):
                case_loop_complete = False
                session.result["execution_complete"] = False
                session.result.setdefault("invalid_reasons", []).append(
                    f"native_popup_restore_not_verified:{case_id}")
                break
        session.result["execution_complete"] = case_loop_complete and len(results) == len(cases)
        counts = session.book.counts()
        if session.profile == "acceptance" and session.phase == "baseline":
            for key in ("startup_failure", "oom_kill", "low_memory_popup"):
                if counts[key] == 0:
                    session.result["invalid_reasons"].append("not_reproduced:" + key)
            if counts["total_events"] < cfg["baseline_min_events"]:
                session.result["invalid_reasons"].append("baseline_below_100_events")
        session.result["case_results"] = results
    except (Exception, KeyboardInterrupt) as exc:
        session.result["invalid_reasons"].append(str(exc))
    finally:
        session.cleanup()
    return session.finish()

def scene3(session: Session) -> Path:
    """Run the fixed short two-case diagnostic through the scene2 evidence engine."""
    if session.scene != 3 or session.profile != "development" or session.phase != "baseline":
        raise ValueError("场景3仅支持development baseline预验证")
    return scene2(session, subcase="combined", frozen=None)

def _scene4_read_journal(session: Session, bundle: str, case_id: str,
                         timeout_seconds: float = 8) -> list[dict]:
    """Persist scene-4 journals without letting the legacy EventBook classify them."""
    remote_path = session.device.app(bundle)["files"] + f"/memscene_{case_id}.jsonl"
    begin_token = f"__S4J_{session.id}_{case_id}_BEGIN__"
    end_prefix = f"__S4J_{session.id}_{case_id}_END__ rc=%s bytes=%s\\n"
    command = (f"if test -r {quote(remote_path)}; then printf {quote(begin_token)}; "
        f"cat {quote(remote_path)}; cat_rc=$?; source_bytes=$(wc -c < {quote(remote_path)} 2>/dev/null); "
        f"printf {quote(end_prefix)} \"$cat_rc\" \"$source_bytes\"; "
        f"else printf {quote('__S4J_' + session.id + '_' + case_id + '_MISSING__')}; fi")
    read_started = time.monotonic()
    wrapper = session.device.shell(command, timeout=timeout_seconds, check=False)
    read_duration = time.monotonic() - read_started
    begin_pos = wrapper.find(begin_token)
    end_pos = wrapper.find(end_prefix.split(" rc=")[0], begin_pos + len(begin_token)) if begin_pos >= 0 else -1
    payload = wrapper[begin_pos + len(begin_token):end_pos] if begin_pos >= 0 and end_pos >= 0 else ""
    tail = wrapper[end_pos + len(end_prefix.split(" rc=")[0]):] if end_pos >= 0 else ""
    end_match = re.match(r"\s*rc=(-?\d+)\s+bytes=(\d+)", tail)
    cat_exit = int(end_match.group(1)) if end_match else None
    source_bytes = int(end_match.group(2)) if end_match else None
    lines = payload.splitlines()
    parsed_rows, malformed_lines = [], []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("JSONL row is not an object")
            parsed_rows.append(item)
        except (ValueError, TypeError) as exc:
            malformed_lines.append({"line": index, "error": str(exc), "text": line[:300]})
    case_ids_match = all(row.get("case_id") == case_id for row in parsed_rows)
    bytes_match = source_bytes is not None and len(payload.encode("utf-8")) == source_bytes
    newline_complete = not payload or payload.endswith("\n")
    proof = {"case_id": case_id, "bundle": bundle, "remote_path": remote_path,
        "explicit_begin_end_markers": begin_pos >= 0 and end_pos >= 0,
        "cat_exit_code": cat_exit, "cat_succeeded": cat_exit == 0,
        "source_byte_count": source_bytes, "readback_byte_count": len(payload.encode("utf-8")),
        "byte_count_matches": bytes_match, "newline_complete": newline_complete,
        "source_line_count": sum(bool(line.strip()) for line in lines),
        "parsed_row_count": len(parsed_rows), "malformed_lines": malformed_lines,
        "read_duration_seconds": read_duration, "read_timeout_seconds": timeout_seconds,
        "all_rows_match_case_id": case_ids_match,
        "complete": bool(begin_pos >= 0 and end_pos >= 0 and cat_exit == 0 and bytes_match and
            newline_complete and not malformed_lines and
            sum(bool(line.strip()) for line in lines) == len(parsed_rows) and case_ids_match),
        "raw_payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "wrapper_error": None if begin_pos >= 0 and end_pos >= 0 else wrapper[:500]}
    raw = payload
    rows = parsed_rows
    path = session.output / "journals" / f"{case_id}_{bundle}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    proof["local_path"] = str(path.relative_to(session.output))
    session.scene4_journal_readback[(case_id, bundle)] = proof
    save(session.output / "journals" / f"{case_id}_{bundle}_readback.json", proof)
    return rows


def _scene4_event_time(row: dict) -> float | None:
    try:
        value = int(row.get("monotonic_ns", 0))
        return value / 1e9 if value > 0 else None
    except (TypeError, ValueError):
        return None


def _scene4_journal_readback(session: "Session", bundle: str, journal_case_id: str,
                             *, allow_absent: bool = False) -> dict:
    """Read one journal twice and prove byte-stability/complete JSON lines."""
    path = session.device.app(bundle)["files"] + f"/memscene_{journal_case_id}.jsonl"
    raw_first, _ = session.device.journal(bundle, journal_case_id)
    raw_second, rows_second = session.device.journal(bundle, journal_case_id)
    absent_text = "No such file or directory"
    absent = (raw_first == raw_second and absent_text in raw_first and path in raw_first)
    if absent and allow_absent:
        return {"complete": True, "state": "confirmed_absent", "path": path,
                "raw": raw_second, "rows": [], "byte_stable": True,
                "reason": "peak journal does not exist before promotion"}
    parsed = []
    malformed = []
    for number, line in enumerate(raw_second.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("case_id") != journal_case_id:
                raise ValueError("journal row case_id mismatch or non-object row")
            parsed.append(row)
        except (ValueError, TypeError) as exc:
            malformed.append({"line": number, "error": str(exc), "raw": line})
    stable = raw_first == raw_second
    complete = bool(stable and raw_second.endswith("\n") and parsed and not malformed)
    return {"complete": complete, "state": "present" if parsed else "unknown_or_empty",
            "path": path, "raw": raw_second, "rows": parsed,
            "byte_stable": stable, "ends_with_newline": raw_second.endswith("\n"),
            "malformed_lines": malformed,
            "reason": None if complete else "journal_not_byte_stable_complete_jsonl"}


def _scene4_minimize_prepare(session: "Session", case_id: str, bundles: list[str],
                            timeout_seconds: float) -> dict:
    """Only minimize owned test windows; never touch or claim to redirty file pages."""
    if not re.fullmatch(r"s4_[a-f0-9]+_c\d{4}", case_id) or not bundles or any(
            not re.fullmatch(r"io\.memscene\.scene4\.workload\.a[1-8]", b) for b in bundles):
        raise ValueError("background preparation requires owned Scene4 identities")
    started = time.monotonic()
    deadline = started + max(1.0, min(90.0, float(timeout_seconds)))
    completed, errors, launched = {}, {}, {}
    for bundle in bundles:
        try:
            launched[bundle] = session.device.launch(bundle,
                params={"op": "minimize_only", "case_id": case_id})
        except Exception as exc:
            errors[bundle] = {"launch_error": str(exc)}
    while time.monotonic() < deadline and len(completed) + len(errors) < len(bundles):
        for bundle in bundles:
            if bundle in completed or bundle in errors:
                continue
            try:
                rows = session.journal(bundle, case_id + "_daily")
                requested = [r for r in rows if r.get("event") == "background_prepare_requested"]
                success = [r for r in rows if r.get("event") == "background_prepare_succeeded"]
                failure = [r for r in rows if r.get("event") in (
                    "background_prepare_failed", "background_prepare_rejected")]
                if failure:
                    errors[bundle] = "owned_window_minimize_failed_or_rejected"
                elif requested and success:
                    first, last = requested[-1], success[-1]
                    uid = int(session.device.app(bundle)["uid"])
                    valid = (int(first.get("pid", 0)) > 0 and int(first.get("start_ticks", 0)) > 0 and
                        all(str(first.get(k)) == str(last.get(k)) for k in ("pid", "uid", "start_ticks")) and
                        int(last.get("uid", -1)) == uid and
                        first.get("case_id") == last.get("case_id") == case_id + "_daily" and
                        _scene4_event_time(first) is not None and _scene4_event_time(last) is not None and
                        _scene4_event_time(last) >= _scene4_event_time(first))
                    if not valid:
                        errors[bundle] = "minimize_request_completion_identity_or_time_mismatch"
                    else:
                        completed[bundle] = {"pid": last["pid"], "uid": uid,
                            "start_ticks": last["start_ticks"], "minimize_event": last}
            except Exception as exc:
                errors[bundle] = {"journal_or_identity_error": str(exc)}
        if len(completed) + len(errors) < len(bundles):
            time.sleep(0.2)
    for bundle in bundles:
        if bundle not in completed and bundle not in errors:
            errors[bundle] = "minimize_completion_unknown"
    return {"complete": len(completed) == len(bundles) and not errors,
        "mode": "background_only_no_redirty", "redirty_required": False,
        "started_host_monotonic": started, "completed_host_monotonic": time.monotonic(),
        "elapsed_seconds": time.monotonic() - started, "bundles": completed,
        "errors": errors, "launch_output": launched,
        "file_redirty_performed": False, "file_dirty_state": "not_established_by_this_preparation"}


def _scene4_dirty_prepare(session: "Session", case_id: str, bundles: list[str],
                          timeout_seconds: float) -> dict:
    """Issue one redirty+minimize Want per owned app and await its journal proof."""
    started = time.monotonic()
    timeout_seconds = max(1.0, min(90.0, float(timeout_seconds)))
    launched = {}
    errors = {}
    def issue(bundle: str) -> tuple[str, str | None]:
        try:
            output = session.device.launch(bundle, params={"op": "redirty_minimize", "case_id": case_id})
            return output, None
        except Exception as exc:
            return "", str(exc)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(bundles))) as pool:
        futures = {pool.submit(issue, bundle): bundle for bundle in bundles}
        for future, bundle in futures.items():
            output, error = future.result()
            launched[bundle] = output
            if error:
                errors[bundle] = error
    completed = {}
    deadline = time.monotonic() + timeout_seconds
    latest_rows: dict[str, list[dict]] = {bundle: [] for bundle in bundles}
    while time.monotonic() < deadline and len(completed) + len(errors) < len(bundles):
        for bundle in bundles:
            if bundle in completed or bundle in errors:
                continue
            try:
                rows = session.journal(bundle, case_id + "_daily")
                latest_rows[bundle] = rows
                redirty = [row for row in rows if row.get("event") == "file_redirty_complete" and
                           row.get("case_id") in (case_id, case_id + "_daily")]
                failures = [row for row in rows if row.get("event") == "file_redirty_failed" and
                            row.get("case_id") in (case_id, case_id + "_daily")]
                minimized = [row for row in rows if row.get("event") == "file_minimize_succeeded" and
                             row.get("case_id") in (case_id, case_id + "_daily")]
                minimize_failed = [row for row in rows if row.get("event") == "file_minimize_failed" and
                                   row.get("case_id") in (case_id, case_id + "_daily")]
                if failures or minimize_failed:
                    errors[bundle] = {"redirty_failures": failures, "minimize_failures": minimize_failed}
                elif redirty and minimized:
                    record = redirty[-1]
                    try:
                        bytes_written = int(record.get("file_redirty_bytes", -1))
                        pages_written = int(record.get("file_redirty_pages", -1))
                        start_ns = int(record.get("file_redirty_start_boottime_ns", -1))
                        end_ns = int(record.get("file_redirty_end_boottime_ns", -1))
                        device_id = int(record.get("file_dev", record.get("file_device", -1)))
                        inode = int(record.get("file_ino", record.get("file_inode", -1)))
                        size = int(record.get("file_size", -1))
                    except (TypeError, ValueError, OverflowError):
                        errors[bundle] = "redirty_completion_fields_invalid"
                        continue
                    if (bytes_written <= 0 or pages_written <= 0 or start_ns <= 0 or
                            end_ns < start_ns or device_id <= 0 or inode <= 0 or size <= 0 or
                            bytes_written != size or
                            pages_written != math.ceil(size / 4096) or
                            str(record.get("file_backing_path", "")).replace("\\", "/").rsplit("/", 1)[-1] !=
                                f"memscene_{case_id}.pages"):
                        errors[bundle] = "redirty_completion_does_not_cover_exact_backing_file"
                        continue
                    minimize_row = minimized[-1]
                    try:
                        same_generation = (int(minimize_row.get("pid", -1)) == int(record.get("pid", -2)) and
                            int(minimize_row.get("uid", -1)) == int(record.get("uid", -2)) and
                            str(minimize_row.get("start_ticks", "")) == str(record.get("start_ticks", "")))
                    except (TypeError, ValueError, OverflowError):
                        same_generation = False
                    if not same_generation:
                        errors[bundle] = "minimize_event_process_generation_mismatch"
                        continue
                    completed[bundle] = {"pid": record.get("pid"), "uid": record.get("uid"),
                        "start_ticks": record.get("start_ticks"), "file_sync_mode": record.get("file_sync_mode"),
                        "file_synced_bytes": record.get("file_synced_bytes"),
                        "file_backing_path": record.get("file_backing_path"),
                        "file_dev": device_id, "file_ino": inode, "file_size": size,
                        "redirty_bytes": bytes_written, "redirty_pages": pages_written,
                        "redirty_start_boottime_s": start_ns / 1e9,
                        "redirty_end_boottime_s": end_ns / 1e9,
                        "minimize_event": minimize_row}
            except Exception as exc:
                errors[bundle] = {"journal_read_error": str(exc)}
        if len(completed) + len(errors) < len(bundles):
            time.sleep(0.2)
    for bundle in bundles:
        if bundle not in completed and bundle not in errors:
            errors[bundle] = "redirty_or_minimize_event_timeout"
    return {"complete": len(completed) == len(bundles) and not errors,
        "started_host_monotonic": started, "completed_host_monotonic": time.monotonic(),
        "elapsed_seconds": time.monotonic() - started, "timeout_seconds": timeout_seconds,
        "bundles": completed, "errors": errors,
        "launch_output": launched,
        "journal_rows": latest_rows}


def _scene4_cold_ready(row: dict, expected_bytes: int, donor: bool = False,
                       expected_file_bytes: int = 0) -> bool:
    try:
        return bool(row.get("event") == "cold_start_ready" and
            row.get("memory_ready") is True and row.get("window_ready") is True and
            row.get("phase") == "cold_start_ready" and
            int(row.get("peak_bytes", -1)) == int(row.get("touched_bytes", -2)) == expected_bytes and
            int(row.get("file_synced_bytes", -1)) == expected_file_bytes and
            int(row.get("file_bytes", -1)) == expected_file_bytes and
            int(row.get("anon_bytes", -1)) + expected_file_bytes == expected_bytes and
            row.get("cold_start_mode") is True and row.get("donor_mode") is donor and
            int(row.get("start_ticks", 0)) > 0 and _scene4_event_time(row) is not None)
    except (TypeError, ValueError):
        return False


def _scene4_clock_probe(session: Session) -> dict:
    """Read BOOTTIME/MONOTONIC/REALTIME together from a hash-verified device helper."""
    cfg = session.config["scene4"]
    path = str(cfg.get("oracle_clock_probe_remote_path", "")).strip()
    expected_hash = str(cfg.get("oracle_clock_probe_sha256", "")).strip().lower()
    if not path or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise RuntimeError("scene4 needs the configured, hashed BOOTTIME clock probe")
    if not getattr(session, "scene4_clock_probe_verified", False):
        hash_raw = session.device.shell("sha256sum " + quote(path), timeout=10, check=False)
        match = re.search(r"(?m)^([0-9a-fA-F]{64})\s+" + re.escape(path) + r"\s*$", hash_raw)
        actual_hash = match.group(1).lower() if match else None
        if actual_hash != expected_hash:
            raise RuntimeError(f"scene4 clock probe hash mismatch: expected={expected_hash}, actual={actual_hash}, raw={hash_raw!r}")
        session.scene4_clock_probe_verified = True
        session.result["scene4_clock_probe_binary"] = {
            "remote_path": path, "expected_sha256": expected_hash,
            "readback_sha256": actual_hash, "verified": True}
    trace_verified = bool(getattr(session, "scene4_trace_clock_verified", False))
    if trace_verified:
        # trace_clock is fixed by the owned trace setup and checked at session
        # start. Re-reading it on every BOOTTIME sample added a blocking procfs
        # read to the hot path and timed out under the real pressure window.
        # Kernel marker correlation remains the independent end-to-end check.
        probe_command = quote(path)
    else:
        trace_root = getattr(session, "oom_trace_instance", None) or "/sys/kernel/debug/tracing"
        probe_command = (quote(path) + "; echo __SCENE4_TRACE_CLOCK__; cat " +
            quote(trace_root + "/trace_clock") + " 2>/dev/null || " +
            "cat /sys/kernel/tracing/trace_clock 2>/dev/null")
    constants_attempted = bool(getattr(session, "scene4_cpu_clock_constants_attempted", False))
    if not constants_attempted:
        # Obtain CPU tick/page units once per session. The cached values are
        # reused by later clock samples so this adds no recurring HDC traffic.
        probe_command += ("; echo __SCENE4_CLK_TCK__; getconf CLK_TCK 2>&1" 
            "; echo __SCENE4_PAGE_SIZE__; getconf PAGESIZE 2>&1")
    combined_raw = session.device.shell(probe_command, timeout=10, check=False)
    if trace_verified:
        raw = combined_raw
        trace_clock = session.result.get("scene4_clock_domain", {}).get("trace_clock_raw", "")
    else:
        raw, _, trace_clock = combined_raw.partition("__SCENE4_TRACE_CLOCK__")
    values = {key: float(value) for key, value in
              re.findall(r"(boottime_s|monotonic_s|realtime_s|proc_uptime_s)=([0-9]+(?:\.[0-9]+)?)", raw)}
    clk_tck_match = re.search(r"(?m)(?:^|\s)clk_tck=(-?\d+)\b", raw)
    page_size_match = re.search(r"(?m)(?:^|\s)page_size_bytes=(-?\d+)\b", raw)
    clk_tck = int(clk_tck_match.group(1)) if clk_tck_match else None
    page_size_bytes = int(page_size_match.group(1)) if page_size_match else None
    required = {"boottime_s", "monotonic_s", "realtime_s", "proc_uptime_s"}
    if not required.issubset(values):
        raise RuntimeError(f"scene4 clock probe output incomplete: {raw!r}")
    trace_clock_raw = trace_clock.split("__SCENE4_CLK_TCK__", 1)[0].strip()
    selected = re.search(r"\[([^\]]+)\]", trace_clock_raw)
    if not selected:
        raise RuntimeError(f"scene4 active kernel trace clock is unknown: {trace_clock_raw!r}")
    constants = getattr(session, "scene4_cpu_clock_constants", None)
    constants_raw = None
    if not constants_attempted:
        tck_suffix = re.search(r"__SCENE4_CLK_TCK__\s*\r?\n\s*([0-9]+)\s*\r?\n", combined_raw)
        page_suffix = re.search(r"__SCENE4_PAGE_SIZE__\s*\r?\n\s*([0-9]+)\s*(?:\r?\n|$)", combined_raw)
        constants_raw = combined_raw.split("__SCENE4_CLK_TCK__", 1)[1] if "__SCENE4_CLK_TCK__" in combined_raw else ""
        constants = {
            "clk_tck": int(clk_tck_match.group(1)) if clk_tck_match else
                (int(tck_suffix.group(1)) if tck_suffix else None),
            "page_size_bytes": int(page_size_match.group(1)) if page_size_match else
                (int(page_suffix.group(1)) if page_suffix else None),
            "raw": constants_raw,
            "source": "clock_probe" if clk_tck_match and page_size_match else "getconf_fallback"}
        session.scene4_cpu_clock_constants = constants
        session.scene4_cpu_clock_constants_attempted = True
        session.result["scene4_cpu_clock_constants"] = constants
    else:
        constants = constants or {"clk_tck": None, "page_size_bytes": None,
                                  "raw": None, "source": "unknown"}
    clk_tck = constants.get("clk_tck")
    page_size_bytes = constants.get("page_size_bytes")
    sample = {**values, "boottime_minus_monotonic_s": values["boottime_s"] - values["monotonic_s"],
        "boottime_minus_proc_uptime_s": values["boottime_s"] - values["proc_uptime_s"],
        "proc_uptime_minus_monotonic_s": values["proc_uptime_s"] - values["monotonic_s"],
        "clk_tck": clk_tck if clk_tck and clk_tck > 0 else None,
        "page_size_bytes": page_size_bytes if page_size_bytes and page_size_bytes > 0 else None,
        "cpu_clock_constants_valid": bool(clk_tck and clk_tck > 0 and
                                             page_size_bytes and page_size_bytes > 0),
        "clock_constants_source": constants.get("source"),
        "trace_clock_raw": trace_clock_raw, "trace_clock_selected": selected.group(1), "raw": raw}
    domain = session.result.setdefault("scene4_clock_domain", {"samples": [], "stable": True})
    if domain["samples"]:
        initial_offset = domain["samples"][0]["boottime_minus_proc_uptime_s"]
        sample["offset_drift_from_initial_s"] = sample["boottime_minus_proc_uptime_s"] - initial_offset
        if abs(sample["offset_drift_from_initial_s"]) > 1.0:
            domain["stable"] = False
            raise RuntimeError("scene4 BOOTTIME-to-proc/trace offset drift exceeded 1s; "
                f"possible suspend or unstable clock mapping: {sample}")
    else:
        sample["offset_drift_from_initial_s"] = 0.0
        domain["initial_boottime_minus_proc_uptime_s"] = sample["boottime_minus_proc_uptime_s"]
    if sample["trace_clock_selected"] != "boot":
        domain["stable"] = False
        raise RuntimeError("scene4 requires active trace_clock=boot to align kernel trace with HAP/daemon CLOCK_BOOTTIME; "
                           f"observed {trace_clock!r}")
    session.scene4_trace_clock_verified = True
    domain["samples"].append(sample)
    domain["trace_clock_selected"] = sample["trace_clock_selected"]
    domain["trace_clock_raw"] = trace_clock_raw
    domain["boottime_source"] = "hash_verified_device_clock_gettime(CLOCK_BOOTTIME)"
    return sample


def _scene4_device_boottime_s(session: Session) -> float:
    return _scene4_clock_probe(session)["boottime_s"]


def _scene4_clock_anchor(device_or_session) -> dict:
    if isinstance(device_or_session, Session):
        session = device_or_session
        before_ns = time.monotonic_ns()
        sample = _scene4_clock_probe(session)
        after_ns = time.monotonic_ns()
        midpoint_ns = (before_ns + after_ns) // 2
        return {"device_uptime_seconds": sample["boottime_s"],
            "device_boottime_seconds": sample["boottime_s"], "device_wall_ms": int(sample["realtime_s"] * 1000),
            "device_clock_sample": sample, "host_monotonic_before_ns": before_ns,
            "host_monotonic_after_ns": after_ns, "host_monotonic_midpoint_ns": midpoint_ns,
            "query_uncertainty_ns": after_ns - before_ns,
            "host_minus_device_seconds": midpoint_ns / 1e9 - sample["boottime_s"]}
    before_ns = time.monotonic_ns()
    device = device_or_session
    raw = device.shell("cat /proc/uptime", timeout=10)
    after_ns = time.monotonic_ns()
    try:
        uptime_s = float(raw.split()[0])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("device /proc/uptime unavailable for oracle clock anchor") from exc
    midpoint_ns = (before_ns + after_ns) // 2
    return {"device_uptime_seconds": uptime_s, "device_clock_source": "legacy_proc_uptime",
            "host_monotonic_before_ns": before_ns,
            "host_monotonic_after_ns": after_ns, "host_monotonic_midpoint_ns": midpoint_ns,
            "query_uncertainty_ns": after_ns - before_ns,
            "host_minus_device_seconds": midpoint_ns / 1e9 - uptime_s}


def _scene4_memcg_snapshot(device: Device, bundle: str, include_smaps: bool = False) -> dict:
    """Read one exact scene-4 app leaf under the verified /dev/memcg hierarchy."""
    uid = int(device.app(bundle)["uid"])
    leaf_name = f"{bundle}_{uid}"
    leaf = f"/dev/memcg/100/{leaf_name}"
    # Read each exact PID's identity tuple in one shell/HDC round trip. The
    # previous implementation used six round trips per PID, making the
    # required 2.5-second freshness proof impossible for two donors.
    script = (
        f"pids=$(cat {quote(leaf + '/cgroup.procs')} 2>/dev/null); "
        "echo __MEMCG_PIDS__; printf '%s\\n' \"$pids\"; "
        "for p in $pids; do "
        "echo __PROC_BEGIN__=$p; "
        "echo __STAT_BEFORE__; cat /proc/$p/stat 2>/dev/null; "
        "echo __STATUS__; cat /proc/$p/status 2>/dev/null; "
        "echo __STAT_AFTER__; cat /proc/$p/stat 2>/dev/null; "
        "echo __CMDLINE__; tr '\\000' ' ' < /proc/$p/cmdline 2>/dev/null; echo; "
        "echo __CGROUP__; cat /proc/$p/cgroup 2>/dev/null; "
        + ("echo __SMAPS_ROLLUP__; cat /proc/$p/smaps_rollup 2>/dev/null; " if include_smaps else "")
        + "echo __PROC_END__; done; "
        f"echo __MEMORY_CURRENT__; cat {quote(leaf + '/memory.current')} 2>/dev/null; "
        f"echo __LEAF_EXISTS__; test -d {quote(leaf)} && echo yes"
    )
    raw = device.shell(script, check=False)
    pids_match = re.search(r"(?ms)^__MEMCG_PIDS__\n(.*?)(?=^__PROC_BEGIN__=|^__MEMORY_CURRENT__)", raw)
    raw_pids = pids_match.group(1).strip() if pids_match else ""
    result = {}
    process_chunks = re.split(r"(?m)^__PROC_BEGIN__=(\d+)\s*$", raw)
    labels = ("STAT_BEFORE", "STATUS", "STAT_AFTER", "CMDLINE", "CGROUP", "SMAPS_ROLLUP")
    for index in range(1, len(process_chunks), 2):
        if index + 1 >= len(process_chunks):
            break
        pid = int(process_chunks[index])
        chunk = process_chunks[index + 1]
        def section(label: str) -> str:
            match = re.search(rf"(?ms)^__{label}__\n(.*?)(?=^__(?:{'|'.join(labels)})__|^__PROC_END__)", chunk)
            return match.group(1).strip() if match else ""
        stat_raw = section("STAT_BEFORE")
        status_raw = section("STATUS")
        stat_after_raw = section("STAT_AFTER")
        cmdline = section("CMDLINE")
        cgroups = section("CGROUP")
        smaps = key_values(section("SMAPS_ROLLUP")) if include_smaps else {}
        try:
            process = parse_stat(stat_raw)
        except (ValueError, IndexError):
            continue
        # Treat proc identity as evidence only when the kernel-reported real
        # UID and two bracketing start-time reads agree with the cgroup PID.
        uid_match = re.search(r"(?m)^Uid:\s+(\d+)", status_raw)
        try:
            stat_after = parse_stat(stat_after_raw)
        except (ValueError, IndexError):
            stat_after = {}
        proc_uid = int(uid_match.group(1)) if uid_match else None
        exact_leaf = any(line.rstrip().endswith("/" + leaf_name) for line in cgroups.splitlines())
        bundle_arg = bool(re.search(r"(?<![A-Za-z0-9_.])" + re.escape(bundle) + r"(?![A-Za-z0-9_.])", cmdline))
        result[f"{pid}:{process['start_ticks']}"] = {**process, "uid": proc_uid,
            "installed_uid": uid, "status_raw": status_raw,
            "cmdline": cmdline, "proc_cgroup": cgroups, "cgroup_leaf": leaf,
            "exact_cgroup_leaf": exact_leaf, "bundle_in_cmdline": bundle_arg,
            # key_values normalizes kB fields to bytes.
            "resident_anon_bytes": int(smaps.get("Pss_Anon", smaps.get("RssAnon", 0)))
                if include_smaps else int(key_values(status_raw).get("RssAnon", 0)),
            "resident_anon_source": ("smaps_rollup:Pss_Anon_or_RssAnon" if include_smaps
                                     else "proc_status:RssAnon"),
            "stat_before_raw": stat_raw, "stat_after_raw": stat_after_raw,
            "stat_stable": str(stat_after.get("start_ticks", "")) == str(process["start_ticks"]),
            "verified": exact_leaf and bundle_arg and proc_uid == uid and
                        str(stat_after.get("start_ticks", "")) == str(process["start_ticks"])}
    raw_current_match = re.search(r"(?ms)^__MEMORY_CURRENT__\n(.*?)(?=^__LEAF_EXISTS__)", raw)
    raw_current = raw_current_match.group(1).strip() if raw_current_match else ""
    try:
        memory_current_bytes = int(raw_current)
    except ValueError:
        memory_current_bytes = 0
    leaf_exists = re.search(r"(?m)^__LEAF_EXISTS__\s*\n?yes\s*$", raw) is not None
    return {"bundle": bundle, "uid": uid, "leaf": leaf, "raw_cgroup_procs": raw_pids,
            "processes": result, "memory_current_raw": raw_current,
            "memory_current_bytes": memory_current_bytes,
            "leaf_exists": leaf_exists, "batched_snapshot_raw": raw}


def _scene4_parse_target_process_inventory(raw: str, targets: list[str]) -> dict:
    """Parse one complete `ps -A -o PID,ARGS` snapshot for exact Scene4 package tokens."""
    text = str(raw or "")
    exit_match = re.search(r"(?m)^__S4_TARGET_PS_EXIT__=(\d+)\s*$", text)
    lines = text.splitlines()
    header_index = next((index for index, line in enumerate(lines)
                         if re.match(r"^\s*PID\s+ARGS\s*$", line)), None)
    if not exit_match or int(exit_match.group(1)) != 0 or header_index is None:
        return {"complete": False, "reason": "ps_exit_or_header_missing", "processes": {},
                "exit_code": int(exit_match.group(1)) if exit_match else None,
                "raw": text}
    found = {bundle: [] for bundle in targets}
    for line in lines[header_index + 1:]:
        if line.startswith("__S4_TARGET_PS_EXIT__="):
            break
        match = re.match(r"^\s*(\d+)\s+(.+?)\s*$", line)
        if not match:
            continue
        pid, args = int(match.group(1)), match.group(2)
        for bundle in targets:
            if re.search(rf"(?<![A-Za-z0-9_.]){re.escape(bundle)}(?![A-Za-z0-9_.])", args):
                found[bundle].append({"pid": pid, "args": args})
    return {"complete": True, "reason": None, "processes": found,
            "exit_code": 0, "raw": text}


def _scene4_target_start_ticks_gate(target_hold_intervals: dict, targets: list[str],
                                    launch_boottime_s: float, clk_tck: int,
                                    clock_offset_s: float = 0.0,
                                    anchor_uncertainty_s: float = 0.0) -> dict:
    """Record proc start ticks in BOOTTIME without inferring app launch from a pooled PID."""
    if not math.isfinite(float(launch_boottime_s)) or launch_boottime_s <= 0 or clk_tck <= 0:
        return {"complete": False, "valid": False, "reason": "launch_clock_or_clk_tck_unknown", "targets": {}}
    if (not math.isfinite(float(clock_offset_s)) or not math.isfinite(float(anchor_uncertainty_s))
            or anchor_uncertainty_s < 0):
        return {"complete": False, "valid": False, "reason": "clock_mapping_or_uncertainty_unknown", "targets": {}}
    rows = {}
    for bundle in targets:
        ready = target_hold_intervals.get(bundle, {}).get("cold_start_ready")
        if not ready:
            rows[bundle] = {"ready": False, "valid": True, "reason": "no_ready_event"}
            continue
        try:
            ticks = int(str(ready.get("start_ticks", "")))
            if ticks <= 0:
                raise ValueError("nonpositive_start_ticks")
        except (TypeError, ValueError):
            rows[bundle] = {"ready": True, "valid": False, "reason": "start_ticks_unknown"}
            continue
        started = ticks / float(clk_tck) + clock_offset_s
        tolerance = 1.0 / float(clk_tck) + anchor_uncertainty_s
        before_t = started < launch_boottime_s - tolerance
        rows[bundle] = {"ready": True, "pid": ready.get("pid"), "uid": ready.get("uid"),
            "start_ticks": str(ticks), "start_boottime_estimate_s": started,
            "launch_boottime_s": launch_boottime_s, "clock_offset_s": clock_offset_s,
            "anchor_uncertainty_s": anchor_uncertainty_s,
            "start_tick_quantization_s": 1.0 / float(clk_tck),
            "conservative_tolerance_s": tolerance,
            "started_before_T": before_t,
            "classification": "pre_T_process_generation_identity_unresolved" if before_t else "start_tick_at_or_after_T",
            "valid": True, "reason": None}
    return {"complete": all(row.get("valid") is not False for row in rows.values()),
            "valid": all(row.get("valid") is True for row in rows.values()),
            "reason": None if all(row.get("valid") is True for row in rows.values()) else
                "target_process_start_generation_unknown", "targets": rows}


def _scene4_donor_ready_identity(device: Device, bundle: str, ready: dict) -> dict:
    """Verify cold-ready PID/UID/start_ticks against the installed UID and exact cgroup leaf."""
    pid = int(ready.get("pid", -1))
    ready_uid = int(ready.get("uid", -1))
    ready_ticks = str(ready.get("start_ticks", ""))
    installed_uid = int(device.app(bundle)["uid"])
    observed = device.inspect_process_identity(pid, timeout=10) if pid > 0 else {}
    identity = observed.get("identity")
    identity_error = (proc_identity_mismatch(identity, pid, installed_uid, ready_ticks)
                      if pid > 0 else "pid_missing")
    memcg = _scene4_memcg_snapshot(device, bundle)
    process = memcg.get("processes", {}).get(f"{pid}:{ready_ticks}") if pid > 0 else None
    verified = bool(ready_uid == installed_uid and identity_error is None and process and
                    process.get("verified") and process.get("uid") == installed_uid and
                    str(process.get("start_ticks")) == ready_ticks and
                    process.get("exact_cgroup_leaf") and process.get("bundle_in_cmdline"))
    return {"verified": verified, "pid": pid, "ready_uid": ready_uid,
        "installed_uid": installed_uid, "start_ticks": ready_ticks,
        "identity": identity, "identity_error": identity_error,
        "identity_raw": observed.get("raw"), "memcg": memcg,
        "matched_process": process}


def _scene4_resident_anon_bytes(device: Device, snapshot: dict) -> int:
    total = 0
    for process in snapshot.get("processes", {}).values():
        cached = process.get("resident_anon_bytes")
        if isinstance(cached, int):
            total += cached
        else:
            raw = device.shell(f"cat /proc/{int(process['pid'])}/smaps_rollup", check=False)
            values = key_values(raw)
            total += int(values.get("Pss_Anon", values.get("RssAnon", 0)))
    return total


def _scene4_daemon_ps_rows(ps_raw: str) -> list[dict]:
    """Parse explicit PID/NAME/ARGS output, excluding grep and shell rows."""
    rows = []
    for line in ps_raw.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 2 or not fields[0].isdigit() or fields[1] != "memshiftd":
            continue
        rows.append({"pid": int(fields[0]), "name": fields[1],
                     "args": fields[2] if len(fields) > 2 else "", "ps_line": line})
    return rows


def _scene4_daemon_proc_sample(device: Device, pid: int) -> dict:
    """Read a daemon's stat/status/stat identity and BOOTTIME bracket together."""
    raw = device.shell(
        f"echo __SCENE4_DAEMON_PRE__; cat /proc/uptime; "
        f"echo __SCENE4_DAEMON_STAT_BEFORE__; cat /proc/{pid}/stat 2>&1; "
        f"echo __SCENE4_DAEMON_STATUS__; cat /proc/{pid}/status 2>&1; "
        f"echo __SCENE4_DAEMON_STAT_AFTER__; cat /proc/{pid}/stat 2>&1; "
        f"echo __SCENE4_DAEMON_POST__; cat /proc/uptime", check=False)
    def part(label: str, next_label: str) -> str:
        start = raw.find(label)
        if start < 0:
            return ""
        start += len(label)
        if raw[start:start + 2] == "\r\n": start += 2
        elif raw[start:start + 1] in ("\r", "\n"): start += 1
        end = raw.find(next_label, start)
        return raw[start:end].strip() if end >= 0 else ""
    before_raw = part("__SCENE4_DAEMON_STAT_BEFORE__", "__SCENE4_DAEMON_STATUS__")
    status_raw = part("__SCENE4_DAEMON_STATUS__", "__SCENE4_DAEMON_STAT_AFTER__")
    after_raw = part("__SCENE4_DAEMON_STAT_AFTER__", "__SCENE4_DAEMON_POST__")
    pre_raw = part("__SCENE4_DAEMON_PRE__", "__SCENE4_DAEMON_STAT_BEFORE__")
    post_raw = part("__SCENE4_DAEMON_POST__", "__SCENE4_DAEMON_POST__")
    # The post marker is the final token; parse its first uptime row directly.
    post_at = raw.find("__SCENE4_DAEMON_POST__")
    post_raw = raw[post_at + len("__SCENE4_DAEMON_POST__"):].strip() if post_at >= 0 else ""
    try:
        pre_s = float(pre_raw.split()[0])
        post_s = float(post_raw.split()[0])
        before = parse_stat(before_raw)
        after = parse_stat(after_raw)
        before_tail = before_raw.rpartition(") ")[2].split()
        after_tail = after_raw.rpartition(") ")[2].split()
        if len(before_tail) < 13 or len(after_tail) < 13:
            raise ValueError("daemon stat lacks utime/stime fields")
        uid_match = re.search(r"(?m)^Uid:\s*(\d+)", status_raw)
        rss_match = re.search(r"(?m)^VmRSS:\s*(\d+)\s+kB", status_raw)
        uid = int(uid_match.group(1)) if uid_match else None
        identity_verified = bool(before["pid"] == after["pid"] == pid and
            before["start_ticks"] == after["start_ticks"] and uid is not None and post_s >= pre_s)
        return {"pid": pid, "uid": uid, "start_ticks": str(after["start_ticks"]),
            "utime_ticks": int(after_tail[11]), "stime_ticks": int(after_tail[12]),
            "VmRSS_bytes": int(rss_match.group(1)) * 1024 if rss_match else None,
            "sample_boottime_start_s": pre_s, "sample_boottime_end_s": post_s,
            "sample_boottime_s": (pre_s + post_s) / 2 if post_s >= pre_s else None,
            "identity_verified": identity_verified, "status_raw": status_raw,
            "stat_before_raw": before_raw, "stat_after_raw": after_raw, "raw": raw}
    except (ValueError, IndexError):
        return {"pid": pid, "identity_verified": False, "unknown": True, "raw": raw,
            "status_raw": status_raw, "stat_before_raw": before_raw, "stat_after_raw": after_raw}


def _scene4_system_sample(device: Device, prior_daemons: dict[str, dict], *,
                          clk_tck: int | None = None, page_size_bytes: int | None = None,
                          reference_boottime_s: float | None = None) -> dict:
    """Best-effort system/daemon diagnostics; preserve gaps as unknown, never zero."""
    mem_raw = device.shell("cat /proc/meminfo", check=False)
    mem = key_values(mem_raw)
    zram_raw = device.shell(
        "for f in /sys/block/zram*/mm_stat /sys/block/zram*/stat; do "
        "[ -f \"$f\" ] && { echo PATH:$f; cat \"$f\"; }; done", check=False)
    ps_raw = device.shell("ps -A -o PID,NAME,ARGS", check=False)
    daemon_rows = []
    for process_row in _scene4_daemon_ps_rows(ps_raw):
        line = process_row["ps_line"]
        pid = process_row["pid"]
        current = _scene4_daemon_proc_sample(device, pid)
        if current.get("identity_verified"):
            key = f"{pid}:{current['start_ticks']}"
            prior = prior_daemons.get(key)
            delta = None
            if prior:
                elapsed = current["sample_boottime_s"] - prior["sample_boottime_s"]
                user_delta = current["utime_ticks"] - prior["utime_ticks"]
                system_delta = current["stime_ticks"] - prior["stime_ticks"]
                if elapsed > 0 and user_delta >= 0 and system_delta >= 0:
                    delta = {"utime_ticks": user_delta, "stime_ticks": system_delta,
                        "total_ticks": user_delta + system_delta,
                        "elapsed_boottime_s": elapsed,
                        "cpu_percent_one_core": ((user_delta + system_delta) / clk_tck / elapsed * 100.0)
                            if clk_tck and clk_tck > 0 else None,
                        "cpu_percent_status": "measured" if clk_tck and clk_tck > 0 else "unknown_clk_tck"}
                else:
                    delta = {"utime_ticks": user_delta, "stime_ticks": system_delta,
                        "elapsed_boottime_s": elapsed, "cpu_percent_one_core": None,
                        "cpu_percent_status": "unknown_invalid_interval_or_counter"}
            prior_daemons[key] = {"utime_ticks": current["utime_ticks"],
                "stime_ticks": current["stime_ticks"],
                "sample_boottime_s": current["sample_boottime_s"],
                "uid": current["uid"], "start_ticks": current["start_ticks"]}
            current.update(cpu_delta_since_previous_sample=delta,
                cpu_clk_tck=clk_tck if clk_tck and clk_tck > 0 else None,
                page_size_bytes=page_size_bytes if page_size_bytes and page_size_bytes > 0 else None,
                reference_boottime_s=reference_boottime_s)
        else:
            current.update(cpu_delta_since_previous_sample=None,
                cpu_clk_tck=clk_tck if clk_tck and clk_tck > 0 else None,
                page_size_bytes=page_size_bytes if page_size_bytes and page_size_bytes > 0 else None,
                reference_boottime_s=reference_boottime_s)
        current["ps_line"] = line
        daemon_rows.append(current)
    return {"meminfo_raw": mem_raw,
        "MemAvailable_bytes": mem.get("MemAvailable"), "MemFree_bytes": mem.get("MemFree"),
        "SwapFree_bytes": mem.get("SwapFree"), "zram_nodes_raw": zram_raw,
        "zram_nodes_available": bool(zram_raw.strip()), "daemon_ps_raw": ps_raw,
        "daemon_rows": daemon_rows, "daemon_found": bool(daemon_rows),
        "reference_boottime_s": reference_boottime_s,
        "clk_tck": clk_tck if clk_tck and clk_tck > 0 else None,
        "page_size_bytes": page_size_bytes if page_size_bytes and page_size_bytes > 0 else None}


def _scene4_identity_events(rows: list[dict], pid: int, uid: int,
                            start_ticks: str | int | None = None) -> list[dict]:
    """Keep lifecycle evidence bound to the full Linux process identity."""
    return [row for row in rows if int(row.get("pid", -1)) == pid and
            int(row.get("uid", -1)) == uid and
            (start_ticks is None or str(row.get("start_ticks", "")) == str(start_ticks)) and
            _scene4_event_time(row) is not None]


def _scene4_native_process_identity(device: Device, pid: int, bundle: str,
                                   expected_uid: int) -> dict:
    """Bracket native popup PID identity and package command line in one proc read."""
    script = (f"echo __STAT_BEFORE__; cat /proc/{pid}/stat 2>&1; "
              f"echo __STATUS__; cat /proc/{pid}/status 2>&1; "
              f"echo __CMDLINE__; tr '\\000' ' ' < /proc/{pid}/cmdline 2>&1; echo; "
              f"echo __STAT_AFTER__; cat /proc/{pid}/stat 2>&1")
    raw = device.shell(script, timeout=8, check=False)
    def part(label: str, next_label: str) -> str:
        ending = r"\Z" if next_label == "END" else rf"(?=^__{next_label}__)"
        match = re.search(rf"(?ms)^__{label}__\s*\n(.*?){ending}", raw)
        return match.group(1).strip() if match else ""
    try:
        before = parse_stat(part("STAT_BEFORE", "STATUS"))
        status = part("STATUS", "CMDLINE")
        cmdline = part("CMDLINE", "STAT_AFTER")
        after = parse_stat(part("STAT_AFTER", "END"))
        uid_match = re.search(r"(?m)^Uid:\s+(\d+)", status)
        uid = int(uid_match.group(1)) if uid_match else None
        package_match = bool(re.search(r"(?<![A-Za-z0-9_.])" + re.escape(bundle) +
                                       r"(?![A-Za-z0-9_.])", cmdline))
        verified = (before["pid"] == int(pid) == after["pid"] and
                    before["start_ticks"] == after["start_ticks"] and
                    uid == int(expected_uid) and package_match)
        return {"verified": verified, "pid": int(pid), "uid": uid,
                "start_ticks": before["start_ticks"], "stat_after_start_ticks": after["start_ticks"],
                "cmdline": cmdline, "bundle_in_cmdline": package_match,
                "expected_uid": int(expected_uid), "raw": raw,
                "reason": None if verified else "pid_uid_startticks_or_package_cmdline_mismatch"}
    except (ValueError, IndexError) as exc:
        return {"verified": False, "pid": int(pid), "expected_uid": int(expected_uid),
                "raw": raw, "reason": f"native_proc_identity_parse_error:{exc}"}


def _scene4_dump_layout_including_invisible(session: Session, name: str) -> tuple[dict, str]:
    remote = session.remote + "/" + re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".json"
    message = session.device.shell("uitest dumpLayout -i -p " + quote(remote), timeout=20)
    raw = session.device.shell("cat " + quote(remote), timeout=10, check=False)
    try:
        tree = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"dumpLayout -i returned invalid UI tree: {message[:200]}") from exc
    local = session.output / "ui" / (re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".json")
    save(local, tree)
    return tree, str(local.relative_to(session.output))


def _scene4_dump_layout_visible_windows(session: Session, name: str) -> tuple[dict, str]:
    """Capture visible, separate windows for popup evidence; -i stays for own-window diagnostics."""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    remote = session.remote + "/" + safe_name + ".json"
    message = session.device.shell("uitest dumpLayout -m false -p " + quote(remote), timeout=20)
    raw = session.device.shell("cat " + quote(remote), timeout=10, check=False)
    try:
        tree = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"dumpLayout -m false returned invalid UI tree: {message[:200]}") from exc
    local = session.output / "ui" / (safe_name + ".json")
    save(local, tree)
    return tree, str(local.relative_to(session.output))


def _scene4_wms_window_inventory(raw: str) -> dict:
    """Parse the complete WMS table without guessing ownership or visibility."""
    lines = (raw or "").splitlines()
    header_index = next((i for i, line in enumerate(lines)
        if re.search(r"\bWindowName\s+DisplayId\s+Pid\s+WinId\b", line)), None)
    focus_match = re.search(r"(?im)^\s*Focus window:\s*(\d+)\s*$", raw or "")
    total_match = re.search(r"(?im)^\s*Total window num:\s*(\d+)\s*$", raw or "")
    rows, malformed = [], []
    ended = False
    if header_index is not None:
        for line in lines[header_index + 1:]:
            if re.match(r"^\s*Focus window:", line, re.I):
                ended = True
                break
            stripped = line.strip()
            if not stripped or set(stripped) == {"-"}:
                continue
            columns = stripped.split()
            if len(columns) >= 4 and all(columns[index].isdigit() for index in (1, 2, 3)):
                rows.append({"window_name": columns[0], "display_id": int(columns[1]),
                    "pid": int(columns[2]), "win_id": int(columns[3]), "raw_line": line})
            else:
                malformed.append(line)
    focus_id = int(focus_match.group(1)) if focus_match else None
    focus_rows = [row for row in rows if row["win_id"] == focus_id] if focus_id is not None else []
    ids = [row["win_id"] for row in rows]
    duplicate_win_ids = sorted({win_id for win_id in ids if ids.count(win_id) > 1})
    reported_window_count = int(total_match.group(1)) if total_match else None
    window_count_matches = reported_window_count is not None and reported_window_count == len(rows)
    return {"complete": header_index is not None and ended and focus_id is not None and
            len(focus_rows) == 1 and bool(rows) and not malformed and
            window_count_matches and not duplicate_win_ids,
        "header_found": header_index is not None, "table_ended_at_focus": ended,
        "focused_win_id": focus_id, "focused_row_count": len(focus_rows),
        "reported_window_count": reported_window_count, "parsed_window_count": len(rows),
        "window_count_matches": window_count_matches, "duplicate_win_ids": duplicate_win_ids,
        "rows": rows, "malformed_lines": malformed, "raw": raw or ""}


def _scene4_aa_process_inventory(raw: str) -> dict:
    rows, malformed = [], []
    for block in re.split(r"(?=^\s*AppRunningRecord\b)", raw or "", flags=re.I | re.M):
        if not re.search(r"\bAppRunningRecord\b", block, re.I):
            continue
        name = re.search(r"(?im)^\s*(?:process\s*name|bundle(?:\s*name)?|app\s*name)\s*(?:[:=#]\s*|\s+)\[?([^\]\s,;]+)", block)
        pid = re.search(r"(?im)^\s*pid\s*[#:=]\s*(\d+)\b", block)
        uid = re.search(r"(?im)\buid\s*[#:=]\s*(\d+)\b", block)
        state = re.search(r"(?im)^\s*state\s*[#:= ]\s*([A-Z][A-Z_]*)\b", block)
        if not (name and pid and uid and state):
            malformed.append(block[:500])
            continue
        rows.append({"bundle": name.group(1).strip("[]{}"), "pid": int(pid.group(1)),
            "uid": int(uid.group(1)), "state": state.group(1).upper()})
    return {"rows": rows, "malformed_record_count": len(malformed),
        "malformed_records": malformed, "raw": raw or ""}


def _scene4_observer_mission_inventory_complete(raw: str) -> bool:
    """Require the platform's explicit mission-list frame, including empty lists."""
    value = raw or ""
    framed = bool(re.search(r"(?m)^\s*User ID #\d+\s*$", value) and
        re.search(r"current mission lists:\s*\{", value) and
        re.search(r"(?m)^\s*}\s*$", value))
    if not framed:
        return False
    target_blocks = [block for block in re.split(r"(?=^\s*Mission ID #)", value, flags=re.M)
        if "LowMemoryAbility" in block and "com.huawei.hmos.taskmanager" in block]
    parsed = parse_low_memory_missions(value, include_process_identity=True)
    # Mission records on this device's TaskManager output do not carry a PID.
    # The observer binds process identity independently from the complete AA,
    # WMS, and proc snapshots; requiring a mission PID incorrectly made every
    # otherwise complete mission inventory unknown.
    return len(target_blocks) == len(parsed)


def _scene4_observer_aa_inventory_complete(raw: str) -> bool:
    """Require the AA inventory header and a contiguous, fully parsed record list."""
    value = raw or ""
    inventory = _scene4_aa_process_inventory(value)
    ids = [int(match.group(1)) for match in re.finditer(
        r"(?im)^\s*AppRunningRecord ID #(\d+)\s*$", value)]
    return (bool(re.search(r"(?im)^\s*AppRunningRecords\s*:\s*$", value)) and
        bool(ids) and ids == list(range(len(ids))) and
        inventory.get("malformed_record_count") == 0 and
        len(inventory.get("rows", [])) == len(ids))


def _scene4_dump_layout_for_bundle(session: Session, bundle: str, name: str) -> tuple[dict, str, str, dict]:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    remote = session.remote + "/" + safe_name + ".json"
    host_start = time.monotonic()
    command_result = session.device.shell(
        "echo __SCENE4_LAYOUT_PRE__; cat /proc/uptime; uitest dumpLayout -m false -b " + quote(bundle) +
        " -p " + quote(remote) + "; echo __SCENE4_LAYOUT_TREE__; cat " + quote(remote) +
        "; printf '\\n'; echo __SCENE4_LAYOUT_POST__; cat /proc/uptime; printf '\\n'; echo __SCENE4_LAYOUT_END__",
        timeout=20, check=False)
    host_end = time.monotonic()
    def marker(name: str, next_name: str) -> str:
        token = f"__SCENE4_LAYOUT_{name}__"
        next_token = f"__SCENE4_LAYOUT_{next_name}__"
        begin = command_result.find(token)
        if begin < 0:
            return ""
        begin += len(token)
        if command_result[begin:begin + 2] == "\r\n":
            begin += 2
        elif command_result[begin:begin + 1] in ("\r", "\n"):
            begin += 1
        end = command_result.find(next_token, begin)
        return command_result[begin:end].strip() if end >= 0 else command_result[begin:].strip()
    pre_raw = marker("PRE", "TREE")
    raw = marker("TREE", "POST")
    post_raw = marker("POST", "END")
    try:
        pre_s = float(pre_raw.splitlines()[0].split()[0])
        post_s = float(post_raw.splitlines()[0].split()[0])
    except (ValueError, IndexError):
        pre_s = post_s = None
    timing = {"boottime_start_s": pre_s, "boottime_end_s": post_s,
        "host_start_monotonic": host_start, "host_end_monotonic": host_end,
        "host_duration_s": max(0.0, host_end - host_start),
        "device_bracket_valid": pre_s is not None and post_s is not None and post_s >= pre_s,
        "device_duration_s": post_s - pre_s if pre_s is not None and post_s is not None and post_s >= pre_s else None}
    try:
        tree = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"targeted dumpLayout failed for {bundle}: {command_result[:200]}") from exc
    local = session.output / "ui" / (safe_name + ".json")
    save(local, tree)
    return tree, str(local.relative_to(session.output)), command_result, timing


def _scene4_dump_layout_for_window(session: Session, win_id: int, name: str) -> tuple[dict, str, str, dict]:
    """Read one exact WMS window using the help-supported UITest window selector."""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    remote = session.remote + "/" + safe_name + ".json"
    host_start = time.monotonic()
    command_result = session.device.shell(
        f"echo __SCENE4_LAYOUT_PRE__; cat /proc/uptime; uitest dumpLayout -m false -w {int(win_id)} -p " +
        quote(remote) + "; echo __SCENE4_LAYOUT_TREE__; cat " + quote(remote) +
        "; printf '\\n'; echo __SCENE4_LAYOUT_POST__; cat /proc/uptime; printf '\\n'; echo __SCENE4_LAYOUT_END__",
        timeout=12, check=False)
    host_end = time.monotonic()
    def marker(label: str, following: str) -> str:
        token = f"__SCENE4_LAYOUT_{label}__"
        next_token = f"__SCENE4_LAYOUT_{following}__"
        begin = command_result.find(token)
        if begin < 0:
            return ""
        begin += len(token)
        if command_result[begin:begin + 2] == "\r\n":
            begin += 2
        elif command_result[begin:begin + 1] in ("\r", "\n"):
            begin += 1
        end = command_result.find(next_token, begin)
        return command_result[begin:end].strip() if end >= 0 else command_result[begin:].strip()
    pre_raw, raw, post_raw = marker("PRE", "TREE"), marker("TREE", "POST"), marker("POST", "END")
    try:
        pre_s = float(pre_raw.splitlines()[0].split()[0])
        post_s = float(post_raw.splitlines()[0].split()[0])
    except (ValueError, IndexError):
        pre_s = post_s = None
    timing = {"boottime_start_s": pre_s, "boottime_end_s": post_s,
        "host_start_monotonic": host_start, "host_end_monotonic": host_end,
        "host_duration_s": max(0.0, host_end - host_start),
        "device_bracket_valid": pre_s is not None and post_s is not None and post_s >= pre_s,
        "device_duration_s": post_s - pre_s if pre_s is not None and post_s is not None and post_s >= pre_s else None}
    try:
        tree = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"targeted dumpLayout failed for window {win_id}: {command_result[:200]}") from exc
    local = session.output / "ui" / (safe_name + ".json")
    save(local, tree)
    return tree, str(local.relative_to(session.output)), command_result, timing


def _scene4_dump_layout_global_visible(session: Session, name: str) -> tuple[dict, str, str, dict]:
    """Capture the light -m false visible tree once with direct BOOTTIME markers."""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    remote = session.remote + "/" + safe_name + ".json"
    host_start = time.monotonic()
    command_result = session.device.shell(
        "echo __SCENE4_LAYOUT_PRE__; cat /proc/uptime; uitest dumpLayout -m false -p " + quote(remote) +
        "; echo __SCENE4_LAYOUT_TREE__; cat " + quote(remote) +
        "; printf '\\n'; echo __SCENE4_LAYOUT_POST__; cat /proc/uptime; printf '\\n'; echo __SCENE4_LAYOUT_END__",
        timeout=12, check=False)
    host_end = time.monotonic()
    def marker(label: str, following: str) -> str:
        token, next_token = f"__SCENE4_LAYOUT_{label}__", f"__SCENE4_LAYOUT_{following}__"
        begin = command_result.find(token)
        if begin < 0:
            return ""
        begin += len(token)
        if command_result[begin:begin + 2] == "\r\n": begin += 2
        elif command_result[begin:begin + 1] in ("\r", "\n"): begin += 1
        end = command_result.find(next_token, begin)
        return command_result[begin:end].strip() if end >= 0 else command_result[begin:].strip()
    pre_raw, raw, post_raw = marker("PRE", "TREE"), marker("TREE", "POST"), marker("POST", "END")
    try:
        pre_s, post_s = float(pre_raw.splitlines()[0].split()[0]), float(post_raw.splitlines()[0].split()[0])
    except (ValueError, IndexError):
        pre_s = post_s = None
    timing = {"boottime_start_s": pre_s, "boottime_end_s": post_s,
        "host_start_monotonic": host_start, "host_end_monotonic": host_end,
        "host_duration_s": max(0.0, host_end - host_start),
        "device_bracket_valid": pre_s is not None and post_s is not None and post_s >= pre_s,
        "device_duration_s": post_s - pre_s if pre_s is not None and post_s is not None and post_s >= pre_s else None}
    try:
        tree = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"global dumpLayout -m false failed: {command_result[:200]}") from exc
    local = session.output / "ui" / (safe_name + ".json")
    save(local, tree)
    return tree, str(local.relative_to(session.output)), command_result, timing


def _scene4_layout_identity_proof(tree: dict, expected: dict) -> dict:
    """Require a real node tree bound to the exact observed WMS window/owner."""
    expected_win = str(expected.get("win_id"))
    expected_bundle = expected.get("bundle")
    nodes = _scene4_ui_nodes(tree)
    matched = [node for node in nodes if str(node.get("hostWindowId", "")) == expected_win and
        node.get("__ownerBundleName") == expected_bundle]
    raw_attributes, raw_child_lists = [], 0
    def inspect_raw(node: object) -> None:
        nonlocal raw_child_lists
        if not isinstance(node, dict):
            return
        attrs = node.get("attributes")
        if isinstance(attrs, dict):
            raw_attributes.append(attrs)
        children = node.get("children")
        if isinstance(children, list):
            raw_child_lists += 1
            for child in children:
                inspect_raw(child)
    for root in tree if isinstance(tree, list) else [tree]:
        inspect_raw(root)
    has_structure = bool(raw_attributes and raw_child_lists)
    component_nodes = [node for node in matched if node.get("type") and
        str(node.get("type")).lower() not in ("windowscene", "root", "window") and
        (node.get("id") or node.get("key"))]
    # A window/root title wrapper alone is not a captured content tree. Require
    # multiple typed descendants under the exact WMS identity.
    component_count = len(component_nodes)
    wms_identity_verified = bool(expected.get("parsed", {}).get("known") and
        expected.get("pid") == expected.get("parsed", {}).get("pid") and
        expected.get("win_id") == expected.get("parsed", {}).get("win_id"))
    proc_identity_verified = bool((expected.get("process_identity") or {}).get("verified") and
        (expected.get("process_identity") or {}).get("uid") == expected.get("uid") and
        (expected.get("process_identity") or {}).get("start_ticks"))
    complete = bool(expected_bundle and expected_win and has_structure and matched and
        component_count >= 2 and wms_identity_verified and proc_identity_verified)
    return {"complete": complete, "expected_win_id": expected_win,
        "expected_bundle": expected_bundle, "node_count": len(nodes),
        "matched_node_count": len(matched), "component_node_count": component_count,
        "wms_identity_verified": wms_identity_verified,
        "proc_identity_verified": proc_identity_verified,
        "reason": "complete" if complete else "window_or_owner_identity_or_component_tree_missing"}


def _scene4_global_layout_coverage(tree: dict, details: list[dict]) -> dict:
    """Match every WMS-visible window to a real visible tree and verified process identity."""
    nodes = _scene4_ui_nodes(tree)
    tree_ids = {str(node.get("hostWindowId")) for node in nodes if node.get("hostWindowId") not in (None, "")}
    visible_details = [item for item in details if item.get("parsed", {}).get("known") and
        item.get("parsed", {}).get("visible") is True]
    expected_ids = {str(item.get("win_id")) for item in visible_details}
    per_window = [_scene4_layout_identity_proof(tree, item) for item in visible_details]
    unresolved_details = [item.get("win_id") for item in details if not item.get("parsed", {}).get("known")]
    missing_ids = sorted(expected_ids - tree_ids)
    unexpected_ids = sorted(tree_ids - expected_ids)
    complete = bool(details and not unresolved_details and not missing_ids and not unexpected_ids and
        all(item.get("complete") for item in per_window))
    return {"complete": complete, "visible_wms_window_count": len(visible_details),
        "tree_node_count": len(nodes), "expected_visible_winids": sorted(expected_ids),
        "observed_tree_winids": sorted(tree_ids), "missing_visible_winids": missing_ids,
        "unexpected_tree_winids": unexpected_ids, "unresolved_detail_winids": unresolved_details,
        "per_window_identity": per_window,
        "reason": "complete" if complete else "visible_window_tree_or_identity_coverage_gap"}


def _scene4_content_freshness(content_samples: list[dict], observation_end_boottime_s: float | None,
                              max_age_s: float = 2.5, max_duration_s: float = 2.5) -> dict:
    """Require every content read to be recent at one explicit observation-end anchor."""
    checked = []
    errors = []
    if not isinstance(observation_end_boottime_s, (int, float)):
        errors.append("observation_end_boottime_missing")
    for index, sample in enumerate(content_samples):
        start = sample.get("boottime_start_s", sample.get("boottime_pre_s"))
        end = sample.get("boottime_end_s", sample.get("boottime_post_s"))
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            errors.append(f"sample_{index}_clock_missing")
            continue
        duration = end - start
        age = observation_end_boottime_s - end if isinstance(observation_end_boottime_s, (int, float)) else None
        valid = duration >= 0 and duration <= max_duration_s and age is not None and 0 <= age <= max_age_s
        checked.append({"index": index, "start_boottime_s": start, "end_boottime_s": end,
            "duration_s": duration, "age_at_observation_end_s": age, "fresh": valid})
        if not valid:
            errors.append(f"sample_{index}_stale_or_overlong")
    return {"complete": not errors, "observation_end_boottime_s": observation_end_boottime_s,
        "max_age_s": max_age_s, "max_duration_s": max_duration_s,
        "samples": checked, "errors": errors}


def _scene4_proc_identity_inventory(device: Device, processes: list[dict]) -> dict:
    """Read AA-listed processes in one shell call, binding owner to UID/start_ticks/cmdline."""
    by_pid: dict[int, list[dict]] = {}
    for row in processes:
        by_pid.setdefault(int(row["pid"]), []).append(row)
    pids = sorted(by_pid)
    if not pids:
        return {"complete": False, "rows": [], "reason": "AA_process_inventory_empty"}
    chunks = []
    for pid in pids:
        chunks.append(f"echo __P{pid}_STAT__; cat /proc/{pid}/stat 2>&1; "
            f"echo __P{pid}_STATUS__; cat /proc/{pid}/status 2>&1; "
            f"echo __P{pid}_CMDLINE__; tr '\\000' ' ' < /proc/{pid}/cmdline 2>&1; echo; "
            f"echo __P{pid}_END__")
    raw = device.shell("; ".join(chunks), timeout=12, check=False)
    result, errors = [], []
    for pid, aa_rows in by_pid.items():
        if len(aa_rows) != 1:
            errors.append({"pid": pid, "reason": "AA_PID_has_multiple_process_records"})
            continue
        def part(label: str, next_label: str) -> str:
            pattern = rf"(?ms)^__P{pid}_{label}__\s*\n(.*?)(?=^__P{pid}_{next_label}__|\Z)"
            match = re.search(pattern, raw)
            return match.group(1).strip() if match else ""
        stat_raw = part("STAT", "STATUS")
        status_raw = part("STATUS", "CMDLINE")
        cmdline = part("CMDLINE", "END")
        aa = aa_rows[0]
        try:
            stat = parse_stat(stat_raw)
            uid_match = re.search(r"(?m)^Uid:\s+(\d+)", status_raw)
            uid = int(uid_match.group(1)) if uid_match else None
            bundle = str(aa["bundle"])
            package_match = bool(re.search(r"(?<![A-Za-z0-9_.])" + re.escape(bundle) +
                r"(?![A-Za-z0-9_.])", cmdline))
            verified = (stat["pid"] == pid and uid == int(aa["uid"]) and package_match)
            item = {"pid": pid, "uid": uid, "bundle": bundle,
                "start_ticks": str(stat["start_ticks"]), "cmdline": cmdline,
                "package_match": package_match, "verified": verified,
                "raw_stat": stat_raw, "raw_status": status_raw}
            result.append(item)
            if not verified:
                errors.append({"pid": pid, "bundle": bundle,
                    "reason": "proc_uid_start_ticks_or_cmdline_mismatch", "identity": item})
        except (ValueError, IndexError, KeyError, TypeError) as exc:
            errors.append({"pid": pid, "bundle": aa.get("bundle"),
                "reason": f"proc_identity_parse_error:{exc}", "raw_stat": stat_raw,
                "raw_status": status_raw, "cmdline": cmdline})
    return {"complete": len(result) == len(by_pid) and not errors,
        "rows": result, "errors": errors, "raw": raw}


def _scene4_verified_app_window_change(row: dict, known_apps: set[str]) -> bool:
    """Only a verified, non-native app identity can explain benign WMS churn."""
    bundle = row.get("owner_bundle")
    identity = row.get("process_identity") or {}
    return bool(bundle in known_apps and bundle not in NATIVE_LOW_MEMORY_OWNERS and
        identity.get("verified") is True and identity.get("uid") == row.get("owner_uid") and
        identity.get("start_ticks") not in (None, "", 0, "0"))


def _scene4_ui_candidate_plan(mapped_rows: list[dict], unknown_rows: list[dict],
                              focused_bundle: str | None, targets: list[str],
                              known_apps: set[str] | None = None) -> dict:
    """Pick only windows that need content inspection; global WMS rows are not visibility proof."""
    native = [row for row in mapped_rows if row.get("owner_bundle") in NATIVE_LOW_MEMORY_OWNERS]
    trusted = set(targets) | set(known_apps or set()) | NATIVE_LOW_MEMORY_OWNERS
    unknown_owner = [row for row in mapped_rows if row.get("owner_bundle") not in trusted and
        row.get("owner_bundle")]
    # Unmapped/reused PIDs remain explicit gaps and are also queried with -w
    # so the raw detail can help diagnose them; it never upgrades identity.
    focused_test = [row for row in mapped_rows if row.get("owner_bundle") == focused_bundle and
        focused_bundle in targets and _scene4_test_dialog_bundle(focused_bundle)]
    detail_rows = {int(row["win_id"]): row for row in [*native, *unknown_owner, *unknown_rows, *focused_test]}
    layout_bundles = sorted({row["owner_bundle"] for row in native + focused_test
        if row.get("owner_bundle")})
    return {"detail_rows": list(detail_rows.values()), "layout_bundles": layout_bundles,
        "native_candidate_winids": sorted({int(row["win_id"]) for row in native}),
        "unknown_owner_winids": sorted({int(row["win_id"]) for row in unknown_owner}),
        "unmapped_winids": sorted({int(row["win_id"]) for row in unknown_rows})}


def _scene4_wms_details_batch(session: Session, table_raw: str, rows: list[dict]) -> list[dict]:
    """Fetch candidate details in one pass; request element trees only for visible/unknown windows."""
    unique = {int(row["win_id"]): row for row in rows}
    if not unique:
        return []
    parts = []
    for win_id in sorted(unique):
        parts.append(f"echo __WMS_{win_id}_PRE__; cat /proc/uptime; "
            f"hidumper -s WindowManagerService -a '-w {win_id}'; "
            f"echo __WMS_{win_id}_POST__; cat /proc/uptime; echo __WMS_{win_id}_END__")
    raw = session.device.shell("; ".join(parts), timeout=12, check=False)
    result = []
    for win_id, row in unique.items():
        def part(label: str, next_label: str) -> str:
            match = re.search(rf"(?ms)^__WMS_{win_id}_{label}__\s*\n(.*?)(?=^__WMS_{win_id}_{next_label}__|\Z)", raw)
            return match.group(1).strip() if match else ""
        pre_raw = part("PRE", "POST")
        detail_raw = pre_raw.splitlines()[1:] if pre_raw else []
        detail_raw = "\n".join(detail_raw)
        post_raw = part("POST", "END")
        try:
            pre = float(pre_raw.splitlines()[0].split()[0])
            post = float(post_raw.split()[0])
        except (ValueError, IndexError):
            pre = post = None
        detail = _scene4_wms_window_snapshot(table_raw, detail_raw, int(row.get("pid", 0)) or None, win_id)
        result.append({"pid": row.get("pid"), "uid": row.get("owner_uid"),
            "bundle": row.get("owner_bundle"), "win_id": win_id,
            "detail_raw": detail_raw, "element_raw": "", "parsed": detail,
            "boottime_pre_s": pre, "boottime_post_s": post,
            "bracket_valid": pre is not None and post is not None and post >= pre,
            "element_complete": False,
            "process_identity": row.get("process_identity"),
            "identity_error": row.get("reason") if row in rows and row.get("reason") else None,
            "raw_batch": raw})
    return result


def _scene4_light_ui_inventory(session: Session, name: str, targets: list[str]) -> dict:
    """Inventory WMS/AA first; inspect only candidate and focused test windows."""
    started_host = time.monotonic()
    started_clock = _scene4_clock_probe(session)
    aa_raw = session.device.shell("aa dump -r", timeout=8, check=False)
    wms_raw = session.device.shell("hidumper -s WindowManagerService -a '-a'", timeout=8, check=False)
    table = _scene4_wms_window_inventory(wms_raw)
    processes = _scene4_aa_process_inventory(aa_raw)
    proc_identity = _scene4_proc_identity_inventory(session.device, processes["rows"])
    identities_by_pid = {int(row["pid"]): row for row in proc_identity["rows"]}
    by_pid: dict[int, list[dict]] = {}
    for row in processes["rows"]:
        by_pid.setdefault(row["pid"], []).append(row)
    mapped_rows, unknown_rows = [], []
    for row in table["rows"]:
        owners = by_pid.get(row["pid"], [])
        if len(owners) != 1:
            unknown_rows.append({**row, "reason": "WMS_PID_has_no_unique_AA_process_identity"})
        else:
            identity = identities_by_pid.get(row["pid"])
            mapped = {**row, "owner_bundle": owners[0]["bundle"],
                "owner_uid": owners[0]["uid"], "owner_state": owners[0]["state"],
                "process_identity": identity}
            if not identity or not identity.get("verified") or identity.get("uid") != owners[0]["uid"]:
                unknown_rows.append({**mapped, "reason": "AA_owner_not_bound_to_proc_UID_start_ticks_cmdline"})
            else:
                mapped_rows.append(mapped)
    focus_rows = [row for row in mapped_rows if row["win_id"] == table["focused_win_id"]]
    # TaskManager/native windows are inspected for popup evidence, but their
    # appearance or focus transitions are never treated as benign app churn.
    # Only owned workloads and explicitly configured real apps may change
    # inside a stable inventory interval without opening an evidence gap.
    known_apps = (set(targets) - NATIVE_LOW_MEMORY_OWNERS) | (
        {str(item.get("bundle", "")) for item in session.config.get("real_apps", {}).values()} -
        NATIVE_LOW_MEMORY_OWNERS)
    # A process name is not a trust list. Unfamiliar owners are accepted only
    # after the exact AA PID/UID is bound to /proc start_ticks and cmdline,
    # then their current visible layout is inspected below for popup content.
    unknown_owner_rows = [row for row in mapped_rows if row["owner_bundle"] not in known_apps and
        row["owner_bundle"] not in NATIVE_LOW_MEMORY_OWNERS]
    focused_bundle = focus_rows[0]["owner_bundle"] if len(focus_rows) == 1 else None
    # Mission inventory is cheap and is read before any tree transfer. Native
    # windows are always content candidates; a mission alone never proves a popup.
    mission_raw = session.device.shell("aa dump -l", timeout=8, check=False)
    missions_before_content = parse_low_memory_missions(mission_raw, include_process_identity=True)
    candidate_plan = _scene4_ui_candidate_plan(mapped_rows, unknown_rows, focused_bundle, targets, known_apps)
    bundle_layouts, trees, layout_errors = [], [], []
    global_tree, global_path, global_command, global_timing = _scene4_dump_layout_global_visible(
        session, f"{name}_global")
    trees.append(global_tree)
    details = _scene4_wms_details_batch(session, wms_raw, mapped_rows + unknown_rows)
    detail_errors = []
    for detail in details:
        identity = detail.get("process_identity")
        identity_error = detail.get("identity_error") or (None if identity and identity.get("verified") and
            identity.get("uid") == detail.get("uid") and identity.get("start_ticks") else
            "AA_owner_not_bound_to_proc_UID_start_ticks_cmdline")
        detail["identity_error"] = identity_error
        if not detail.get("parsed", {}).get("known") or identity_error or not detail.get("bracket_valid"):
            detail_errors.append({"pid": detail.get("pid"), "win_id": detail["win_id"],
                "reason": identity_error or detail.get("parsed", {}).get("reason", "WMS_detail_unknown")})
    global_layout_proof = _scene4_global_layout_coverage(global_tree, details)
    bundle_layouts.append({"bundle": "<all-visible>", "path": global_path,
        "command_result": global_command, "timing": global_timing,
        "identity_proof": global_layout_proof})
    if not global_layout_proof["complete"]:
        layout_errors.append({"reason": global_layout_proof["reason"],
            "missing_visible_winids": global_layout_proof["missing_visible_winids"],
            "unexpected_tree_winids": global_layout_proof["unexpected_tree_winids"],
            "unresolved_detail_winids": global_layout_proof["unresolved_detail_winids"]})
    capture_clock = _scene4_clock_probe(session)
    aa_after_raw = session.device.shell("aa dump -r", timeout=8, check=False)
    wms_after_raw = session.device.shell("hidumper -s WindowManagerService -a '-a'", timeout=8, check=False)
    table_after = _scene4_wms_window_inventory(wms_after_raw)
    processes_after = _scene4_aa_process_inventory(aa_after_raw)
    identities_after = _scene4_proc_identity_inventory(session.device, processes_after["rows"])
    identity_after_by_pid = {int(row["pid"]): row for row in identities_after["rows"]}
    by_pid_after: dict[int, list[dict]] = {}
    for process in processes_after["rows"]:
        by_pid_after.setdefault(process["pid"], []).append(process)
    mapped_after, unknown_after = [], []
    for row in table_after["rows"]:
        owners = by_pid_after.get(row["pid"], [])
        identity = identity_after_by_pid.get(row["pid"])
        if len(owners) != 1 or not identity or not identity.get("verified") or identity.get("uid") != owners[0]["uid"]:
            unknown_after.append({**row, "reason": "post_snapshot_owner_identity_unknown"})
        else:
            mapped_after.append({**row, "owner_bundle": owners[0]["bundle"],
                "owner_uid": owners[0]["uid"], "process_identity": identity})
    end_clock = _scene4_clock_probe(session)
    end_host = time.monotonic()
    before_signature = {(row["pid"], row["win_id"]):
        (row["owner_bundle"], row["owner_uid"], row["process_identity"]["start_ticks"])
        for row in mapped_rows}
    after_signature = {(row["pid"], row["win_id"]):
        (row["owner_bundle"], row["owner_uid"], row["process_identity"]["start_ticks"])
        for row in mapped_after}
    changed_window_keys = sorted((set(before_signature) ^ set(after_signature)) |
        {key for key in set(before_signature) & set(after_signature)
         if before_signature[key] != after_signature[key]})
    changed_rows = [row for row in mapped_rows + mapped_after
        if (row["pid"], row["win_id"]) in changed_window_keys]
    benign_changed_rows = [row for row in changed_rows
        if _scene4_verified_app_window_change(row, known_apps)]
    unsafe_changed_rows = [row for row in changed_rows if row not in benign_changed_rows]
    focused_before = next((row for row in mapped_rows if row["win_id"] == table["focused_win_id"]), None)
    focused_after = next((row for row in mapped_after if row["win_id"] == table_after["focused_win_id"]), None)
    focus_changed = table["focused_win_id"] != table_after["focused_win_id"]
    benign_focus_change = bool(focus_changed and focused_before and focused_after and
        _scene4_verified_app_window_change(focused_before, known_apps) and
        _scene4_verified_app_window_change(focused_after, known_apps))
    unsafe_focus_change = focus_changed and not benign_focus_change
    content_samples = [{"boottime_pre_s": detail.get("boottime_pre_s"),
        "boottime_post_s": detail.get("boottime_post_s")} for detail in details]
    content_samples.extend(layout["timing"] for layout in bundle_layouts)
    content_freshness = _scene4_content_freshness(content_samples,
        capture_clock.get("boottime_s") if capture_clock.get("trace_clock_selected") == "boot" else None)
    content_fresh = bool(capture_clock.get("trace_clock_selected") == "boot" and
        end_clock.get("trace_clock_selected") == "boot" and content_freshness["complete"])
    unknown_visible_content = [detail for detail in details if detail.get("parsed", {}).get("known") and
        detail.get("parsed", {}).get("visible") is True and detail.get("bundle") not in
        (known_apps | NATIVE_LOW_MEMORY_OWNERS)]
    popup_terms = tuple(str(item).lower() for item in session.config.get("popup_text_patterns", []))
    unknown_popup_nodes = [node for tree in trees for node in _scene4_ui_nodes(tree)
        if node.get("__ownerBundleName") not in (known_apps | NATIVE_LOW_MEMORY_OWNERS) and
        any(term and term in " ".join(str(node.get(field, "")) for field in
            ("text", "content", "description")).lower() for term in popup_terms)]
    if unknown_popup_nodes:
        detail_errors.append({"reason": "low_memory_text_visible_but_native_owner_not_confirmed",
            "candidate_node_count": len(unknown_popup_nodes)})
    inventory_complete = bool(table["complete"] and table_after["complete"] and
        not unsafe_changed_rows and not unsafe_focus_change and
        not unknown_rows and not unknown_after and len(focus_rows) == 1 and
        not processes["malformed_record_count"] and not processes_after["malformed_record_count"] and
        not detail_errors and not layout_errors and
        capture_clock.get("trace_clock_selected") == "boot" and end_clock.get("trace_clock_selected") == "boot" and
        content_fresh and end_clock.get("boottime_s", 0) >= started_clock.get("boottime_s", 0))
    ui_dir = session.output / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    wms_path = ui_dir / f"{safe}_wms_inventory.txt"
    wms_after_path = ui_dir / f"{safe}_wms_inventory_after.txt"
    aa_path = ui_dir / f"{safe}_aa_processes.txt"
    aa_after_path = ui_dir / f"{safe}_aa_processes_after.txt"
    mission_path = ui_dir / f"{safe}_aa_missions.txt"
    wms_path.write_text(wms_raw or "", encoding="utf-8")
    wms_after_path.write_text(wms_after_raw or "", encoding="utf-8")
    aa_path.write_text(aa_raw or "", encoding="utf-8")
    aa_after_path.write_text(aa_after_raw or "", encoding="utf-8")
    mission_path.write_text(mission_raw or "", encoding="utf-8")
    return {"tree": trees, "targeted_layouts": bundle_layouts,
        "targeted_layout_captured": bool(bundle_layouts) and all(
            row.get("identity_proof", {}).get("complete") for row in bundle_layouts),
        "targeted_layout_paths": [row["path"] for row in bundle_layouts],
        "mission_raw": mission_raw, "wms_raw": wms_raw, "aa_raw": aa_raw,
        "capture_clock": capture_clock,
        "window_set_stable": not unsafe_changed_rows and not unsafe_focus_change,
        "window_changes": {"changed_keys": [list(key) for key in changed_window_keys],
            "identity_changed_keys": [list(key) for key in set(before_signature) & set(after_signature)
                if before_signature[key] != after_signature[key]],
            "benign_known_app_rows": benign_changed_rows, "unsafe_rows": unsafe_changed_rows,
            "focus_changed": focus_changed, "benign_focus_change": benign_focus_change,
            "unsafe_focus_change": unsafe_focus_change},
        "content_fresh_le_2_5s": content_fresh,
        "content_freshness": content_freshness,
        "window_snapshot_captured": table["complete"], "mission_snapshot_captured": bool(mission_raw.strip()),
        "complete": inventory_complete, "reason": "complete" if inventory_complete else "WMS_AA_window_identity_or_targeted_content_gap",
        "table": table, "process_inventory": processes, "proc_identity_inventory": proc_identity,
        "process_inventory_after": processes_after, "proc_identity_inventory_after": identities_after,
        "unknown_window_rows": unknown_rows + unknown_after,
        "unknown_owner_rows": unknown_owner_rows, "details": details,
        "unknown_visible_content_candidate": bool(unknown_visible_content),
        "candidate_plan": candidate_plan,
        "detail_errors": detail_errors, "layout_errors": layout_errors,
        "relevant_bundles": sorted({row.get("bundle") for row in bundle_layouts if row.get("bundle")}),
        "focused_bundle": focused_bundle,
        "started_boottime_s": started_clock.get("boottime_s"),
        "content_capture_boottime_s": capture_clock.get("boottime_s"),
        "ended_boottime_s": end_clock.get("boottime_s"),
        "duration_host_seconds": end_host-started_host,
        "raw_paths": {"wms_before": str(wms_path.relative_to(session.output)),
            "wms_after": str(wms_after_path.relative_to(session.output)),
            "aa_processes": str(aa_path.relative_to(session.output)),
            "aa_processes_after": str(aa_after_path.relative_to(session.output)),
            "aa_missions": str(mission_path.relative_to(session.output))}}


def _scene4_minimize_button(tree: dict, bundle: str) -> dict | None:
    candidates = []
    def walk(node: dict, ancestors: list[dict]) -> None:
        attrs = node.get("attributes", {}) if isinstance(node, dict) else {}
        chain = ancestors + [attrs]
        if attrs.get("id") == "EnhanceMinimizeBtn" or attrs.get("key") == "EnhanceMinimizeBtn":
            owners = [item for item in chain if item.get("bundleName") == bundle or
                      item.get("__ownerBundleName") == bundle or item.get("ownerBundleName") == bundle]
            bounds = str(attrs.get("origBounds", ""))
            match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if owners and match and str(attrs.get("enabled", "true")).lower() == "true" and \
                    str(attrs.get("visible", "true")).lower() == "true":
                owner = owners[-1]
                focused = str(owner.get("focused", "false")).lower() == "true"
                candidates.append({"x": (int(match[1]) + int(match[3])) // 2,
                    "y": (int(match[2]) + int(match[4])) // 2,
                    "bounds": bounds, "focused_window": focused,
                    "window_bundle": bundle, "button_attributes": attrs,
                    "window_attributes": owner})
        for child in node.get("children", []) if isinstance(node, dict) else []:
            if isinstance(child, dict):
                walk(child, chain)
    roots = tree if isinstance(tree, list) else [tree]
    for root in roots:
        if isinstance(root, dict):
            walk(root, [])
    focused = [item for item in candidates if item["focused_window"]]
    return focused[0] if len(focused) == 1 else None


def _scene4_bundle_window_visibility(tree: dict, bundle: str) -> dict:
    windows = []
    def walk(node: dict) -> None:
        attrs = node.get("attributes", {}) if isinstance(node, dict) else {}
        if attrs.get("bundleName") == bundle or attrs.get("__ownerBundleName") == bundle:
            windows.append({"visible": str(attrs.get("visible", "true")).lower() == "true",
                            "focused": str(attrs.get("focused", "false")).lower() == "true",
                            "attributes": attrs})
        for child in node.get("children", []) if isinstance(node, dict) else []:
            if isinstance(child, dict):
                walk(child)
    roots = tree if isinstance(tree, list) else [tree]
    for root in roots:
        if isinstance(root, dict):
            walk(root)
    visible = [window for window in windows if window["visible"]]
    return {"bundle": bundle, "window_node_count": len(windows),
            "visible_window_count": len(visible), "visible": bool(visible),
            "invisible_confirmed": not visible,
            "invisible_evidence": "all_bundle_windows_invisible" if windows else "bundle_window_absent_from_dumpLayout_i",
            "windows": windows}


def _scene4_wms_window_snapshot(table_raw: str, detail_raw: str, pid: int | None,
                                win_id: int | None = None) -> dict:
    """Parse WindowManagerService list + targeted detail bound to one PID."""
    focus_match = re.search(r"(?im)^\s*Focus window:\s*(\d+)\s*$", table_raw or "")
    focus_id = int(focus_match.group(1)) if focus_match else None
    rows = []
    in_table = False
    for line in (table_raw or "").splitlines():
        if re.search(r"\bWindowName\s+DisplayId\s+Pid\s+WinId\b", line):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.strip() or set(line.strip()) == {"-"} or line.lstrip().startswith("Focus window:"):
            continue
        columns = line.split()
        if len(columns) >= 4 and columns[1].isdigit() and columns[2].isdigit() and columns[3].isdigit():
            rows.append({"window_name": columns[0], "display_id": int(columns[1]),
                         "pid": int(columns[2]), "win_id": int(columns[3])})
    matching = [row for row in rows if (pid is None or row["pid"] == int(pid)) and
                (win_id is None or row["win_id"] == int(win_id))]
    expected_pid = int(pid) if pid is not None else (
        matching[0]["pid"] if len(matching) == 1 else None)
    if focus_id is None:
        return {"known": False, "reason": "focused_window_id_missing",
                "pid": expected_pid, "matching_windows": matching}
    if len(matching) != 1:
        return {"known": False, "reason": "pid_window_match_missing_or_ambiguous",
                "pid": expected_pid, "matching_windows": matching, "focused_win_id": focus_id}
    row = matching[0]
    def value(name):
        match = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*([^\s]+)", detail_raw or "")
        return match.group(1) if match else None
    try:
        detail_pid, detail_win = int(value("Pid")), int(value("WinId"))
    except (TypeError, ValueError):
        return {"known": False, "reason": "targeted_detail_identity_missing",
                "pid": expected_pid, "list_row": row, "focused_win_id": focus_id}
    visible_raw, focusable_raw = value("IsVisible"), value("Focusable")
    if detail_pid != row["pid"] or (expected_pid is not None and detail_pid != expected_pid) or detail_win != row["win_id"] or \
            visible_raw not in ("true", "false") or focusable_raw not in ("true", "false"):
        return {"known": False, "reason": "targeted_detail_identity_or_state_mismatch",
                "pid": expected_pid, "list_row": row, "detail_pid": detail_pid,
                "detail_win_id": detail_win, "focused_win_id": focus_id,
                "visible_raw": visible_raw, "focusable_raw": focusable_raw}
    return {"known": True, "pid": row["pid"], "win_id": row["win_id"],
            "visible": visible_raw == "true", "focused": focus_id == row["win_id"],
            "focusable": focusable_raw == "true", "focused_win_id": focus_id,
            "window_name": row["window_name"], "list_row": row}


def _scene4_native_popup_candidate(session: Session, nodes: list[dict], missions: list[dict],
                                   baseline_mission_ids: set[str],
                                   case_begin_boottime: float | None,
                                   capture_clock: dict | None,
                                   case_end_boottime: float | None,
                                   patterns: list[str]) -> tuple[dict | None, dict]:
    """Attribute a visible native popup only with a stable MONOTONIC→BOOTTIME map and WMS proof."""
    terms = tuple(str(item).lower() for item in patterns)
    candidates = []
    owner_unknown_candidates = []
    for node in nodes:
        text = " ".join(str(node.get(field, "")) for field in ("text", "content", "description")).lower()
        if not any(term and term in text for term in terms):
            continue
        owner = node.get("__ownerBundleName")
        if owner in NATIVE_LOW_MEMORY_OWNERS:
            candidates.append(node)
        else:
            real_bundles = {str(item.get("bundle", "")) for item in
                session.config.get("real_apps", {}).values()}
            owned_test_bundles = {f"io.memscene.scene4.workload.a{index}" for index in range(1, 9)}
            if owner in real_bundles or owner in owned_test_bundles:
                continue
            # Visible or visibility-unknown low-memory text with no owning
            # trusted non-system bundle cannot be safely dismissed.
            if str(node.get("visible", "")).lower() != "false":
                owner_unknown_candidates.append(node)
    proof: dict[str, Any] = {"candidate_nodes_seen": bool(candidates or owner_unknown_candidates),
        "candidate_node_count": len(candidates) + len(owner_unknown_candidates), "complete": True,
        "clock_mapping": None, "wms": []}
    if owner_unknown_candidates:
        proof.update(complete=False, reason="native_candidate_owner_unknown",
            owner_unknown_candidate_count=len(owner_unknown_candidates),
            owner_unknown_candidates=[{"visible": node.get("visible"),
                "hostWindowId": node.get("hostWindowId"),
                "text": " ".join(str(node.get(field, "")) for field in ("text", "content", "description"))}
                for node in owner_unknown_candidates])
        return None, proof
    if not candidates:
        proof["reason"] = "no_native_low_memory_candidate_node"
        return None, proof
    unknown_visibility = [node for node in candidates
        if str(node.get("visible", "")).lower() not in ("true", "false")]
    visible_nodes = [node for node in candidates if str(node.get("visible", "")).lower() == "true"]
    proof["candidate_visibility"] = [{"hostWindowId": node.get("hostWindowId"),
        "visible": node.get("visible")} for node in candidates]
    if unknown_visibility:
        proof.update(complete=False, reason="native_candidate_visibility_unknown")
        return None, proof
    if not visible_nodes:
        proof["reason"] = "native_candidate_nodes_explicitly_invisible"
        return None, proof
    domain = session.result.get("scene4_clock_domain", {})
    offset = capture_clock.get("boottime_minus_monotonic_s") if isinstance(capture_clock, dict) else None
    offset_drift = capture_clock.get("offset_drift_from_initial_s") if isinstance(capture_clock, dict) else None
    mapping_valid = bool(domain.get("stable") is True and capture_clock and
        capture_clock.get("trace_clock_selected") == "boot" and
        isinstance(offset, (int, float)) and isinstance(offset_drift, (int, float)) and
        abs(offset_drift) <= 1.0 and
        isinstance(case_begin_boottime, (int, float)) and
        isinstance(capture_clock.get("boottime_s"), (int, float)) and
        case_begin_boottime <= capture_clock["boottime_s"] and
        (case_end_boottime is None or capture_clock["boottime_s"] <= case_end_boottime))
    proof["clock_mapping"] = {"mission_domain": "MONOTONIC", "boundary_and_capture_domain": "BOOTTIME",
        "boottime_minus_monotonic_s": offset, "offset_drift_from_initial_s": offset_drift,
        "mapping_valid": mapping_valid, "capture_boottime_s": capture_clock.get("boottime_s") if capture_clock else None,
        "capture_monotonic_s": capture_clock.get("monotonic_s") if capture_clock else None}
    if not mapping_valid:
        proof.update(complete=False, reason="monotonic_to_boottime_mapping_unavailable_or_unstable")
        return None, proof
    mapped_missions = []
    for mission in missions:
        mono_start = mission.get("start_monotonic_seconds", mission.get("start_uptime_seconds"))
        row = dict(mission)
        if isinstance(mono_start, (int, float)):
            row["start_uptime_seconds"] = mono_start + offset
            row["start_boottime_seconds"] = mono_start + offset
        mapped_missions.append(row)
    candidate = native_low_memory_popup_candidate(visible_nodes, mapped_missions,
        baseline_mission_ids, case_begin_boottime, capture_clock["boottime_s"], case_end_boottime)
    if candidate is None:
        proof.update(complete=False, reason="visible_native_node_without_correlated_foreground_mission")
        return None, proof
    mission = next((row for row in mapped_missions if row.get("mission_id") == candidate["mission_id"]), None)
    pid = int(mission.get("pid", 0)) if mission and mission.get("pid") else 0
    table = session.device.shell("hidumper -s WindowManagerService -a '-a'", timeout=8, check=False)
    for node in visible_nodes:
        try:
            host_window_id = int(node.get("hostWindowId"))
        except (TypeError, ValueError):
            proof.update(complete=False, reason="native_popup_host_window_id_missing")
            return None, proof
        listed = _scene4_wms_window_snapshot(table, "", pid if pid > 0 else None,
                                              host_window_id if pid <= 0 else None)
        list_row = listed.get("list_row")
        if not list_row or list_row.get("win_id") != host_window_id:
            proof["wms"].append({"host_window_id": host_window_id, "known": False,
                "reason": "native_window_not_uniquely_listed_for_mission_pid_or_winid",
                "table_raw": table, "parsed_list": listed})
            continue
        resolved_pid = pid if pid > 0 else int(list_row["pid"])
        if pid > 0 and list_row.get("pid") != pid:
            proof["wms"].append({"host_window_id": host_window_id, "known": False,
                "reason": "aa_pid_and_wms_pid_disagree", "table_raw": table,
                "list_row": list_row, "aa_pid": pid})
            continue
        pre = _scene4_device_boottime_s(session)
        detail = session.device.shell(
            f"hidumper -s WindowManagerService -a '-w {host_window_id}'", timeout=8, check=False)
        wms = _scene4_wms_window_snapshot(table, detail, resolved_pid, host_window_id)
        native_identity = None
        native_identity_error = None
        try:
            expected_uid = int(session.device.app(candidate["owner"])["uid"])
            native_identity = _scene4_native_process_identity(
                session.device, resolved_pid, candidate["owner"], expected_uid)
            if not native_identity.get("verified"):
                native_identity_error = native_identity.get("reason")
            if mission and mission.get("uid") is not None and \
                    int(native_identity.get("uid", -1)) != int(mission["uid"]):
                native_identity_error = "wms_pid_uid_disagrees_with_aa_mission"
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            native_identity_error = f"native_process_identity_unavailable:{exc}"
        # Take the post clock only after detail and process identity reads so
        # the two-and-a-half-second freshness test covers every expensive step.
        post = _scene4_device_boottime_s(session)
        fresh = 0 <= post - capture_clock["boottime_s"] <= 2.5
        proof["wms"].append({"host_window_id": host_window_id, "pre_boottime_s": pre,
            "post_boottime_s": post, "age_from_layout_capture_s": post - capture_clock["boottime_s"],
            "fresh_le_2_5s": fresh, "table_raw": table, "detail_raw": detail, "parsed": wms,
            "native_process_identity": native_identity, "native_process_identity_error": native_identity_error})
        if wms.get("known") and wms.get("visible") is True and wms.get("win_id") == host_window_id and fresh and \
                native_identity_error is None:
            candidate.update(mission_start_monotonic_seconds=mission.get("start_monotonic_seconds"),
                mission_start_boottime_seconds=mission.get("start_boottime_seconds"),
                boottime_minus_monotonic_s=offset, wms_window_id=host_window_id,
                wms_visible=True, wms_observation_boottime_s=post, pid=resolved_pid,
                pid_source="aa_mission" if pid > 0 else "unique_wms_winid_and_installed_uid")
            proof.update(complete=True, reason="native_popup_visible_and_correlated", verified=True)
            return candidate, proof
    proof.update(complete=False, reason="native_popup_window_visibility_not_verified")
    return None, proof


def _scene4_focused_bundles(tree: dict) -> set[str]:
    focused = set()
    def walk(node: dict, inherited_bundle: str = "") -> None:
        attrs = node.get("attributes", {}) if isinstance(node, dict) else {}
        bundle = attrs.get("bundleName") or attrs.get("ownerBundleName") or inherited_bundle
        if bundle and str(attrs.get("focused", "false")).lower() == "true":
            focused.add(bundle)
        for child in node.get("children", []) if isinstance(node, dict) else []:
            if isinstance(child, dict):
                walk(child, bundle)
    for root in tree if isinstance(tree, list) else [tree]:
        if isinstance(root, dict):
            walk(root)
    return focused


def _scene4_aa_running_record_state(raw: str, bundle: str, pid: int, uid: int) -> str | None:
    """Return state only from an aa dump -r block matching this full process identity."""
    blocks = re.split(r"(?=AppRunningRecord\b)", raw or "", flags=re.I)
    for block in blocks:
        if not re.search(r"AppRunningRecord\b", block, re.I):
            continue
        process_name = re.search(
            r"(?im)^\s*(?:process\s*name|bundle(?:\s*name)?|app\s*name)\s*(?:[:=#]\s*|\s+)\[?([^\]\s,;]+)\]?", block)
        block_pid = re.search(r"(?im)^\s*pid\s*[#:=]\s*(\d+)\b", block)
        block_uid = re.search(r"(?im)\buid\s*[#:=]\s*(\d+)\b", block)
        state = re.search(r"(?im)^\s*state\s*[#:= ]\s*(FOREGROUND|BACKGROUND)\b", block)
        if process_name and process_name.group(1).strip("[]{}") == bundle and block_pid and block_uid and state and \
                int(block_pid.group(1)) == pid and int(block_uid.group(1)) == uid:
            return state.group(1).upper()
    return None


def _scene4_aa_foreground_bundles(raw: str) -> set[str]:
    focused = set()
    for block in re.split(r"(?=AppRunningRecord\b)", raw or "", flags=re.I):
        name = re.search(r"(?im)^\s*process\s*name\s*(?:[:=]\s*|\s+)\[?([^\]\s,;]+)\]?", block)
        state = re.search(r"(?im)^\s*state\s*[#:= ]\s*FOREGROUND\b", block)
        if name and state:
            focused.add(name.group(1).strip("[]{}"))
    return focused


def _scene4_ui_nodes(tree: dict | list) -> list[dict]:
    roots = tree if isinstance(tree, list) else [tree]
    return [node for root in roots if isinstance(root, dict) for node in ui_nodes(root)]


def _scene4_marker_raw_correlation(raw_dmesg: str, marker: dict) -> dict:
    payload = str(marker.get("marker_payload", ""))
    token = f"MEMSCENES_CASE_{marker.get('phase')} session={marker.get('session_id')} case_id={marker.get('case_id')}"
    line = next((item for item in (raw_dmesg or "").splitlines()
                 if token in item and re.search(r"^\[\s*\d+(?:\.\d+)?\]", item)), None)
    boottime_ns = marker.get("marker_boottime_ns")
    raw_timestamp = None
    if line:
        match = re.search(r"^\[\s*(\d+(?:\.\d+)?)\]", line)
        if match:
            raw_timestamp = float(match.group(1))
    offset = raw_timestamp - float(boottime_ns) / 1e9 if raw_timestamp is not None and boottime_ns is not None else None
    return {"matched": line is not None and raw_timestamp is not None and bool(payload),
        "raw_line": line, "raw_timestamp_seconds": raw_timestamp,
        "marker_boottime_seconds": float(boottime_ns) / 1e9 if boottime_ns is not None else None,
        "raw_minus_boottime_seconds": offset}


def _scene4_confirm_unattributed_kernel_victims(lines: list[str], start_boottime: float,
                                                end_boottime: float, dmesg_to_boottime: float,
                                                owned_bundles: list[str],
                                                excluded_victim_lines: set[str] | None = None) -> tuple[list[dict], list[str]]:
    """Confirm raw OOM select+exit pairs without inventing app/process identity."""
    victim_re = re.compile(r"\[mem_oom:\d+\].*?out of Memory\s*\(OOM\),\s*start to kill process\s*<\s*([^>]+)>\s*\(pid:\s*(\d+),", re.I)
    exit_re = re.compile(r"process has exited, pid=(\d+)")
    owned_names = {suffix for bundle in owned_bundles if ".workload." in bundle
                   for suffix in (bundle[-15:], bundle[-14:])}
    confirmed, unknown = [], []
    confirmed_deaths: dict[tuple[int, str], dict] = {}
    for index, line in enumerate(lines):
        match = victim_re.search(line)
        stamp_match = re.match(r"^\[\s*(\d+(?:\.\d+)?)\]", line)
        if not match or not stamp_match:
            continue
        name, pid = match.group(1).strip(), int(match.group(2))
        raw_s = float(stamp_match.group(1))
        event_s = raw_s + dmesg_to_boottime
        if (name in owned_names or line.strip() in (excluded_victim_lines or set()) or
                not (start_boottime <= event_s <= end_boottime)):
            continue
        exit_line = None
        for later in lines[index + 1:]:
            later_stamp = re.match(r"^\[\s*(\d+(?:\.\d+)?)\]", later)
            if not later_stamp:
                continue
            later_s = float(later_stamp.group(1))
            if later_s - raw_s > 2.0:
                break
            exit_match = exit_re.search(later)
            if exit_match and int(exit_match.group(1)) == pid:
                exit_line = later
                break
        if exit_line:
            exit_stamp = re.match(r"^\[\s*(\d+(?:\.\d+)?)\]", exit_line)
            exit_key = (pid, exit_stamp.group(1) if exit_stamp else exit_line.strip())
            incident_id = f"unattributed:{pid}:{exit_key[1]}"
            item = {"bundle": None, "identity_status": "unattributed",
                "pid": pid, "event_uptime_s": event_s, "victim_name": name,
                "victim_line": line, "exit_line": exit_line, "incident_id": incident_id}
            if exit_key in confirmed_deaths:
                prior = confirmed_deaths[exit_key]
                prior.setdefault("duplicate_victim_lines", []).append(line)
            else:
                confirmed_deaths[exit_key] = item
        else:
            unknown.append(line)
    return list(confirmed_deaths.values()), unknown


def _scene4_parse_marker_helper_output(raw: str) -> dict:
    """Parse clock helper's single-line key/value prefix and trailing payload."""
    line = next((item.strip() for item in (raw or "").splitlines()
                 if item.strip().startswith("marker_phase=")), "")
    if not line:
        return {}
    prefix, sep, payload = line.partition(" marker_payload=")
    values = dict(re.findall(r"([a-z_]+)=([^\s]+)", prefix))
    if sep:
        values["marker_payload"] = payload
    return values


def _scene4_dynamic_donor_fields(bundle: str, identity: dict, installed_uid: int,
                                 now_boottime: float, aa_raw: str, aa_time: float,
                                 window_tree: dict | list, window_time: float,
                                 proc_snapshot: dict, proc_time: float,
                                 lifecycle: dict, lifecycle_time: float) -> tuple[list[str], dict]:
    pid = int(identity.get("pid", -1))
    ticks = str(identity.get("start_ticks", ""))
    aa_state = _scene4_aa_running_record_state(aa_raw, bundle, pid, installed_uid) or "unknown"
    wms = window_tree.get("wms_window_snapshot") if isinstance(window_tree, dict) else None
    if isinstance(wms, dict):
        window_visible = bool(wms.get("known") and wms.get("visible"))
        window_focused = bool(wms.get("known") and wms.get("focused"))
        windows = {"visible": window_visible,
                   "invisible_confirmed": bool(wms.get("known") and not wms.get("visible")),
                   "known": bool(wms.get("known")), "wms": wms}
    else:
        windows = _scene4_bundle_window_visibility(window_tree, bundle)
        window_focused = bundle in _scene4_focused_bundles(window_tree)
        window_visible = bool(windows["visible"])
    processes = proc_snapshot.get("processes", {})
    live = processes.get(f"{pid}:{ticks}")
    proc_verified = bool(live and live.get("verified") and live.get("uid") == installed_uid and
                         str(live.get("start_ticks", "")) == ticks and live.get("exact_cgroup_leaf") and
                         live.get("bundle_in_cmdline"))
    rows = _scene4_identity_events(lifecycle.get("journal_rows", []), pid, installed_uid, ticks)
    enter = next((row for row in reversed(rows) if row.get("event") == "donor_background_enter"), None)
    enter_s = _scene4_event_time(enter) if enter else None
    post_enter_terminal = [row for row in rows if enter_s is not None and
        (_scene4_event_time(row) or 0) > enter_s and
        row.get("event") in ("donor_foreground_resume", "donor_ability_destroy")]
    journal_complete = bool(enter and any(row.get("event") == "donor_background_heartbeat" for row in rows))
    lifecycle_clear = bool(journal_complete and not post_enter_terminal)
    freshness = lambda stamp: bool(stamp is not None and stamp > 0 and
                                   now_boottime >= stamp and now_boottime - stamp <= 2.5)
    aa_fresh = freshness(aa_time)
    window_fresh = freshness(window_time)
    proc_fresh = freshness(proc_time)
    lifecycle_fresh = freshness(lifecycle_time)
    aa_ok = aa_state == "BACKGROUND" and aa_fresh
    window_ok = windows.get("known", True) and windows["invisible_confirmed"] and \
        not window_visible and not window_focused and window_fresh
    proc_ok = proc_verified and proc_fresh
    lifecycle_ok = lifecycle_clear and lifecycle_fresh
    heartbeat_time = lifecycle.get("last_heartbeat_s")
    heartbeat_fresh = bool(heartbeat_time is not None and
                            0 <= now_boottime - heartbeat_time <= 2.5)
    os_snapshot_ok = aa_ok and window_ok and proc_ok and lifecycle_ok
    source = "hap_heartbeat" if heartbeat_fresh and os_snapshot_ok else (
        "os_snapshot" if os_snapshot_ok else "unknown")
    fields = [str(pid) if aa_state != "unknown" else "unknown",
        str(installed_uid) if aa_state != "unknown" else "unknown",
        aa_state if aa_ok else "unknown", f"{aa_time:.6f}" if aa_ok else "0",
        "true" if window_visible and window_fresh else "false",
        "true" if window_focused and window_fresh else "false",
        f"{window_time:.6f}" if window_fresh else "0",
        "true" if proc_ok else "false", ticks if proc_ok else "unknown",
        f"{proc_time:.6f}" if proc_ok else "0",
        "true" if lifecycle_ok else "false", f"{lifecycle_time:.6f}" if lifecycle_ok else "0",
        source]
    proof = {"aa_state": aa_state, "aa_fresh": aa_fresh, "window": windows,
        "window_focused": window_focused, "window_fresh": window_fresh,
        "proc_verified": proc_verified, "proc_fresh": proc_fresh,
        "proc_identity": live, "lifecycle_clear": lifecycle_clear,
        "lifecycle_fresh": lifecycle_fresh, "heartbeat_fresh": heartbeat_fresh,
        "source": source, "valid": os_snapshot_ok and source in ("hap_heartbeat", "os_snapshot"),
        "observed_boottime_s": {"aa": aa_time, "window": window_time,
            "proc": proc_time, "lifecycle": lifecycle_time, "now": now_boottime}}
    return fields, proof


def _scene4_minimize_donor(session: Session, bundle: str, donor_case_id: str,
                           identity: dict, ui_lock=None) -> dict:
    d = session.device
    uid = int(d.app(bundle)["uid"])
    lock = ui_lock
    def ui_dump(name: str):
        if lock:
            with lock:
                return _scene4_dump_layout_including_invisible(session, name)
        return _scene4_dump_layout_including_invisible(session, name)
    if lock:
        with lock:
            focus_output = d.focus(bundle)
    else:
        focus_output = d.focus(bundle)
    if re.search(r"(?i)\b(error|failed|invalid|not found)\b", focus_output):
        raise RuntimeError(f"aa focus failed for {bundle}: {focus_output[:300]}")
    before_tree, before_path = ui_dump(f"{donor_case_id}_{bundle.rsplit('.', 1)[-1]}_minimize_before")
    button = _scene4_minimize_button(before_tree, bundle)
    if not button:
        raise RuntimeError(f"no unique focused EnhanceMinimizeBtn for donor {bundle}")
    click_start_boottime = _scene4_device_boottime_s(session)
    if lock:
        with lock:
            click_output = d.shell(f"uitest uiInput click {button['x']} {button['y']}", timeout=10, check=False)
    else:
        click_output = d.shell(f"uitest uiInput click {button['x']} {button['y']}", timeout=10, check=False)
    deadline = time.monotonic() + float(session.config["scene4"].get("donor_background_timeout_seconds", 30))
    last_tree = None
    after_path = None
    window_proof = None
    state_raw = ""
    lifecycle = {}
    while time.monotonic() < deadline:
        if lock:
            with lock:
                state_raw = d.shell("aa dump -r", timeout=8, check=False)
        else:
            state_raw = d.shell("aa dump -r", timeout=8, check=False)
        if last_tree is None or window_proof is None or window_proof.get("visible"):
            last_tree, after_path = ui_dump(
                f"{donor_case_id}_{bundle.rsplit('.', 1)[-1]}_minimize_after")
            window_proof = _scene4_bundle_window_visibility(last_tree, bundle)
        now_boottime = _scene4_device_boottime_s(session)
        lifecycle = _scene4_donor_evidence(session, bundle, donor_case_id, identity, uid, now_boottime)
        entered_after_click = bool(lifecycle.get("background_since_s") is not None and
                                   lifecycle["background_since_s"] >= click_start_boottime)
        is_background = _scene4_aa_running_record_state(state_raw, bundle, int(identity["pid"]), uid) == "BACKGROUND"
        if is_background and entered_after_click and lifecycle.get("verified") and \
                window_proof.get("invisible_confirmed"):
            return {"bundle": bundle, "identity": identity, "aa_focus": focus_output,
                "minimize_button": button, "click": click_output,
                "click_start_boottime_s": click_start_boottime,
                "aa_state_readback": state_raw, "aa_background_confirmed": True,
                "window_before_path": before_path, "window_after_path": after_path,
                "window_before": _scene4_bundle_window_visibility(before_tree, bundle),
                "window_after": window_proof, "window_invisible_confirmed": True,
                "background_enter": lifecycle.get("background_enter"),
                "latest_heartbeat": lifecycle.get("last_heartbeat"),
                "heartbeat_fresh": lifecycle.get("verified"),
                "lifecycle_proof": lifecycle, "verified": True}
        time.sleep(0.4)
    failure = {"bundle": bundle, "identity": identity,
        "click_start_boottime_s": click_start_boottime,
        "click_output": click_output,
        "aa_state": _scene4_aa_running_record_state(state_raw, bundle,
                                                      int(identity["pid"]), uid),
        "aa_state_raw": state_raw, "window_proof": window_proof,
        "entered_after_click": bool(lifecycle.get("background_since_s") is not None and
            lifecycle["background_since_s"] >= click_start_boottime),
        "lifecycle": lifecycle}
    failure_path = session.output / (f"{donor_case_id}_{bundle.rsplit('.', 1)[-1]}_"
                                     "background_transition_failure.json")
    save(failure_path, failure)
    raise RuntimeError("donor background transition was not fully verified for " + bundle +
        "; raw evidence=" + str(failure_path.name) +
        "; lifecycle_fresh=" + str(lifecycle.get("lifecycle_fresh")) +
        "; identity_error=" + repr(lifecycle.get("identity_error")) +
        "; verification_checks=" + json.dumps(lifecycle.get("verification_checks", {}), sort_keys=True) +
        "; cgroup_check=" + repr(lifecycle.get("identity_check")))


def _scene4_donor_evidence(session: Session, bundle: str, case_id: str,
                           identity: dict, installed_uid: int,
                           now_boottime: float, include_smaps: bool = True) -> dict:
    """Revalidate one donor identity and its recent HAP background heartbeat."""
    pid = int(identity["pid"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as io_pool:
        journal_future = io_pool.submit(_scene4_read_journal, session, bundle, case_id)
        cgroup_future = io_pool.submit(_scene4_memcg_snapshot, session.device, bundle, include_smaps)
        raw_rows = journal_future.result()
    rows = _scene4_identity_events(raw_rows, pid, installed_uid, identity.get("start_ticks"))
    donor_peak = int(session.config["scene4"].get("donor_peak_mib",
                       session.config["scene4"].get("donor_memory_mib", 3072))) * 1024**2
    cold = next((row for row in reversed(rows) if _scene4_cold_ready(row, donor_peak, donor=True)), None)
    enters = [row for row in rows if row.get("event") == "donor_background_enter"]
    enter = enters[-1] if enters else None
    enter_s = _scene4_event_time(enter) if enter else None
    after_enter = [row for row in rows if enter_s is not None and
                   (_scene4_event_time(row) or 0) >= enter_s]
    heartbeat_rows = [row for row in after_enter if row.get("event") == "donor_background_heartbeat"]
    foreground_or_destroy = [row for row in after_enter if row.get("event") in
                             ("donor_foreground_resume", "donor_ability_destroy")]
    heartbeat = heartbeat_rows[-1] if heartbeat_rows else None
    heartbeat_s = _scene4_event_time(heartbeat) if heartbeat else None
    cold_s = _scene4_event_time(cold) if cold else None
    heartbeat_gaps = []
    heartbeat_sequence_errors = []
    for earlier, later in zip(heartbeat_rows, heartbeat_rows[1:]):
        gap = (_scene4_event_time(later) or 0) - (_scene4_event_time(earlier) or 0)
        if gap > 2.5 or gap <= 0:
            heartbeat_gaps.append({"seconds": gap, "from": earlier, "to": later})
        try:
            if int(later.get("donor_heartbeat_sequence", -1)) <= int(earlier.get("donor_heartbeat_sequence", -1)):
                heartbeat_sequence_errors.append({"from": earlier, "to": later})
        except (TypeError, ValueError):
            heartbeat_sequence_errors.append({"from": earlier, "to": later, "reason": "sequence_missing"})
    # Caller supplies CLOCK_BOOTTIME from the same device probe used by the
    # HAP/daemon; never compare it to /proc/uptime (MONOTONIC on this device).
    # Freshness is based on the live heartbeat and on absence of a later
    # foreground/destroy event. The original background-enter timestamp is
    # an event-order anchor; it must not age out during the fixed lead/window.
    lifecycle_ok = _scene4_lifecycle_fresh(
        now_boottime, enter_s, heartbeat_s, cold_s,
        has_enter=enter is not None, has_heartbeat=heartbeat is not None,
        has_foreground_or_destroy=bool(foreground_or_destroy),
        heartbeat_gaps=heartbeat_gaps, heartbeat_sequence_errors=heartbeat_sequence_errors)
    # The exact-leaf batch already brackets /proc/<pid>/stat, reads the real
    # UID, cmdline and cgroup membership, and now also captures smaps_rollup.
    # Reuse that single fresh identity snapshot instead of issuing a second
    # inspect_process_identity HDC request for the same PID.
    cgroup_snapshot = cgroup_future.result()
    cgroup_processes = cgroup_snapshot.get("processes", {})
    cgroup_match = cgroup_processes.get(f"{pid}:{identity.get('start_ticks')}")
    current_identity = cgroup_match
    identity_error = proc_identity_mismatch(current_identity, pid, installed_uid,
                                            identity.get("start_ticks"))
    raw_current = {"identity": current_identity,
                   "raw": cgroup_snapshot.get("batched_snapshot_raw"),
                   "uptime_seconds": None,
            "cgroup_identity_present": bool(cgroup_match),
                   "cgroup_leaf": cgroup_snapshot.get("leaf"),
                   "exact_cgroup_leaf": bool(cgroup_match and cgroup_match.get("exact_cgroup_leaf")),
                   "bundle_in_cmdline": bool(cgroup_match and cgroup_match.get("bundle_in_cmdline"))}
    verification_checks = {
        "cold_ready_found": cold is not None,
        "background_enter_found": enter is not None,
        "heartbeat_found": heartbeat is not None,
        "no_foreground_or_destroy_after_enter": not foreground_or_destroy,
        "background_enter_not_in_future": bool(enter_s is not None and now_boottime >= enter_s),
        "heartbeat_within_2_5s": bool(heartbeat_s is not None and now_boottime >= heartbeat_s and
                                       now_boottime - heartbeat_s <= 2.5),
        "heartbeat_gaps_clear": not heartbeat_gaps,
        "heartbeat_sequence_clear": not heartbeat_sequence_errors,
        "process_identity_matches": identity_error is None,
        "cgroup_identity_present": bool(cgroup_match),
        "cgroup_leaf_exact": bool(cgroup_match and cgroup_match.get("exact_cgroup_leaf")),
        "cmdline_bundle_matches": bool(cgroup_match and cgroup_match.get("bundle_in_cmdline")),
        "cgroup_process_verified": bool(cgroup_match and cgroup_match.get("verified")),
    }
    return {"bundle": bundle, "pid": pid, "uid": installed_uid,
            "start_ticks": str(identity.get("start_ticks")),
            "cold_ready": cold, "cold_ready_boottime_s": cold_s,
            "background_enter": enter, "background_since_s": enter_s,
            "last_heartbeat": heartbeat,
            "last_heartbeat_s": heartbeat_s, "heartbeat_gaps": heartbeat_gaps,
            "heartbeat_sequence_errors": heartbeat_sequence_errors,
            "foreground_or_destroy_after_enter": foreground_or_destroy,
            "identity_check": raw_current, "identity_error": identity_error,
            "verification_checks": verification_checks,
            "lifecycle_fresh": lifecycle_ok,
            "cgroup_snapshot": cgroup_snapshot,
            "verified": lifecycle_ok and identity_error is None and bool(cgroup_match) and
                        bool(cgroup_match.get("verified")),
            "journal_rows": raw_rows}


def _scene4_lifecycle_fresh(now_boottime: float, enter_s: float | None,
        heartbeat_s: float | None, cold_s: float | None, *, has_enter: bool,
        has_heartbeat: bool, has_foreground_or_destroy: bool,
        heartbeat_gaps: list, heartbeat_sequence_errors: list) -> bool:
    """Check continuing background state without expiring its original entry event."""
    return bool(has_enter and has_heartbeat and not has_foreground_or_destroy and
        enter_s is not None and heartbeat_s is not None and cold_s is not None and
        now_boottime >= enter_s and 0 <= now_boottime - heartbeat_s <= 2.5 and
        now_boottime >= cold_s and not heartbeat_gaps and not heartbeat_sequence_errors)


def _scene4_qualifier_failure_is_predeadline(schedule_valid: bool,
        initial_registration: bool, now_boottime: float, launch_boottime: float) -> bool:
    """Only pre-launch donor proof failures affect experiment completeness."""
    return bool(not schedule_valid and
                (initial_registration or now_boottime < launch_boottime))


def _scene4_atomic_schedule_write(session: Session, local_path: Path,
                                  remote_path: str, payload: str) -> None:
    temporary_local = local_path.with_suffix(local_path.suffix + ".tmp")
    temporary_local.write_text(payload, encoding="utf-8", newline="")
    temporary_remote = remote_path + ".tmp"
    session.device.call("file", "send", str(temporary_local), temporary_remote, timeout=45)
    readback = session.device.shell("mv -f " + quote(temporary_remote) + " " +
        quote(remote_path) + " && cat " + quote(remote_path), timeout=10, check=False)
    if readback.strip() != payload.strip():
        raise RuntimeError("scene4 oracle schedule atomic write readback mismatch")


def _scene4_control_readback(session: Session, schedule_remote: str) -> dict:
    control_path = schedule_remote.rsplit("/", 1)[0] + "/control.conf"
    raw = session.device.shell("cat " + quote(control_path), timeout=10, check=False)
    parsed = {line.split("=", 1)[0]: line.split("=", 1)[1]
              for line in raw.splitlines() if "=" in line}
    expected_paused = session.phase == "baseline"
    valid = (parsed.get("paused") == ("true" if expected_paused else "false") and
             parsed.get("protected_apps") == "" and parsed.get("reserve_extra_mb") == "0" and
             parsed.get("boost_until_unix") == "0")
    return {"path": control_path, "expected_paused": expected_paused,
            "raw_readback": raw, "verified": valid}


def _scene4_set_oracle_control(session: Session, schedule_remote: str) -> dict:
    expected_paused = session.phase == "baseline"
    control_path = schedule_remote.rsplit("/", 1)[0] + "/control.conf"
    paused = "true" if expected_paused else "false"
    content = f"paused={paused}\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0\n"
    local = session.output / ("oracle_control_sampling_only.conf" if expected_paused
                              else "oracle_control_active.conf")
    local.write_text(content, encoding="utf-8", newline="")
    session.device.call("file", "send", str(local), control_path, timeout=30)
    readback = _scene4_control_readback(session, schedule_remote)
    if not readback["verified"]:
        raise RuntimeError(f"scene4 oracle control readback mismatch: {readback['raw_readback']!r}")
    return readback


def _scene4_ledger_completeness(rows: list[dict], phase: str,
                                control_begin: dict, control_end: dict) -> dict:
    statuses = [str(row.get("status", "")).strip().lower() for row in rows]
    action_ids: dict[str, list[str]] = {}
    write_lengths = []
    missing_action_id = False
    for row, status in zip(rows, statuses):
        action_id = str(row.get("action_id", "")).strip()
        if phase != "baseline" and status in {
                "reserved_before_write_pending_result", "reserved_action_still_inflight_at_deadline_unknown",
                "write_accepted_feedback_measured", "write_accepted_feedback_measured_positive_rss_observation",
                "write_accepted_feedback_nonpositive_halt", "write_accepted_feedback_unknown",
                "write_accepted_feedback_unknown_after_exception",
                "write_accepted_identity_or_background_changed_postwrite", "write_failed_or_partial_no_retry"
        } and not action_id:
            missing_action_id = True
        action_ids.setdefault(action_id, []).append(status)
        try:
            write_lengths.append(int(row.get("write_length", "")))
        except (TypeError, ValueError):
            write_lengths.append(None)
    if phase == "baseline":
        allowed = {"event_registered_once", "rejected:control_paused_or_unknown",
                   "deadline_expired_no_action"}
        control_ok = bool(control_begin.get("verified") and control_end.get("verified"))
        valid = bool(rows) and all(status in allowed for status in statuses) and \
            all(value == -1 for value in write_lengths) and control_ok
        return {"complete": valid, "expected": "paused_registered_or_explicit_no_action_no_reservation",
                "statuses": statuses, "write_lengths": write_lengths,
                "no_reserved_or_write_rows": all(status in allowed for status in statuses),
                "control_readback_complete": control_ok}
    definitive = {"write_accepted_feedback_measured", "write_accepted_feedback_measured_positive_rss_observation",
        "write_accepted_feedback_nonpositive_halt", "write_accepted_feedback_unknown",
        "write_accepted_feedback_unknown_after_exception",
        "write_accepted_identity_or_background_changed_postwrite",
        "write_failed_or_partial_no_retry"}
    inflight = "reserved_action_still_inflight_at_deadline_unknown"
    no_action = {"deadline_expired_no_action",
        "skipped_both_donors_below_min_batch_or_budget_floor",
        "skipped_no_ratio_increase_after_positive_feedback"}
    known_exact = definitive | no_action | {"event_registered_once",
        "reserved_before_write_pending_result", inflight}
    valid = bool(rows) and not missing_action_id and all(value is not None for value in write_lengths)
    saw_decision = False
    for grouped in action_ids.values():
        if any(status not in known_exact and not status.startswith("rejected:") for status in grouped):
            valid = False
            continue
        # The service's event_registered_once row is event metadata with its
        # own action_id, not a donor reservation/terminal action.
        if grouped == ["event_registered_once"]:
            continue
        reserved = [status for status in grouped if status == "reserved_before_write_pending_result"]
        outcomes = [status for status in grouped if status in definitive or status == inflight or
                    status.startswith("rejected:")]
        refused = [status for status in grouped if status.startswith("rejected:")]
        if reserved and (len(reserved) != 1 or len(outcomes) < 1 or
                len([status for status in outcomes if status in definitive]) > 1 or
                len([status for status in outcomes if status == inflight]) > 1 or
                len(refused) > 1 or len(outcomes) > 2):
            valid = False
        elif not reserved and not (any(status in no_action for status in grouped) or refused):
            valid = False
        if reserved and outcomes or any(status in no_action for status in grouped) or refused:
            saw_decision = True
    valid = valid and saw_decision
    return {"complete": valid, "expected": "each_reserved_action_has_one_terminal_or_explicit_no_action",
            "statuses": statuses, "write_lengths": write_lengths,
            "action_ids": action_ids, "missing_action_id": missing_action_id,
            "saw_event_decision": saw_decision,
            "terminal_states": sorted(definitive | {inflight})}


SCENE4_TIMING_V3_HEADER = (
    "action_id,event_id,case_id,ledger_status,legacy_deadline_boottime_s,"
    "syscall_start_boottime_s,syscall_return_boottime_s,write_duration_ms,"
    "feedback_complete_boottime_s,write_crossed_deadline,feedback_crossed_deadline,"
    "legacy_elapsed_ms,legacy_deadline_overlap")


def _scene4_timing_v3_completeness(v2_rows: list[dict], v3_rows: list[dict],
                                    event_id: str) -> dict:
    """Pair every V3 state by (action_id,status) and validate actual syscall timing."""
    scoped_v2 = [row for row in v2_rows if str(row.get("event_id", "")) == event_id]
    scoped_v3 = [row for row in v3_rows if str(row.get("event_id", "")) == event_id]
    v2_groups: dict[str, list[dict]] = {}
    v3_groups: dict[str, list[dict]] = {}
    errors = []
    expected_state_counts: dict[tuple[str, str], int] = {}
    observed_state_counts: dict[tuple[str, str], int] = {}
    write_attempt_statuses = {
        "write_accepted_feedback_measured", "write_accepted_feedback_measured_positive_rss_observation",
        "write_accepted_feedback_nonpositive_halt", "write_accepted_feedback_unknown",
        "write_accepted_feedback_unknown_after_exception",
        "write_accepted_identity_or_background_changed_postwrite", "write_failed_or_partial_no_retry"}
    def bool_value(value: object) -> bool | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized in ("true", "1"): return True
        if normalized in ("false", "0"): return False
        return None
    for row in scoped_v2:
        action_id = str(row.get("action_id", "")).strip()
        status = str(row.get("status", "")).strip()
        if not action_id:
            errors.append("v2_missing_action_id")
        else:
            v2_groups.setdefault(action_id, []).append(row)
            if not status:
                errors.append(f"v2_status_missing:{action_id}")
            expected_state_counts[(action_id, status)] = expected_state_counts.get((action_id, status), 0) + 1
    for row in scoped_v3:
        action_id = str(row.get("action_id", "")).strip()
        status = str(row.get("ledger_status", "")).strip()
        if not action_id:
            errors.append("v3_missing_action_id")
            continue
        if not status:
            errors.append(f"v3_status_missing:{action_id}")
        else:
            observed_state_counts[(action_id, status)] = observed_state_counts.get((action_id, status), 0) + 1
        if not str(row.get("case_id", "")).strip():
            errors.append(f"v3_case_id_missing:{action_id}")
        deadline = None
        for field in ("legacy_deadline_boottime_s", "syscall_start_boottime_s",
                      "syscall_return_boottime_s", "write_duration_ms",
                      "feedback_complete_boottime_s", "legacy_elapsed_ms"):
            value = str(row.get(field, "")).strip()
            if value:
                try:
                    parsed = float(value)
                    if not math.isfinite(parsed):
                        raise ValueError("non-finite")
                except (TypeError, ValueError):
                    errors.append(f"v3_invalid_{field}:{action_id}")
                else:
                    if field == "legacy_deadline_boottime_s":
                        deadline = parsed
        for field in ("write_crossed_deadline", "feedback_crossed_deadline", "legacy_deadline_overlap"):
            value = str(row.get(field, "")).strip().lower()
            if value and value not in {"true", "false", "0", "1"}:
                errors.append(f"v3_invalid_{field}:{action_id}")
        try:
            syscall_start = float(row["syscall_start_boottime_s"]) if str(row.get("syscall_start_boottime_s", "")).strip() else None
            syscall_return = float(row["syscall_return_boottime_s"]) if str(row.get("syscall_return_boottime_s", "")).strip() else None
            write_ms = float(row["write_duration_ms"]) if str(row.get("write_duration_ms", "")).strip() else None
            feedback = float(row["feedback_complete_boottime_s"]) if str(row.get("feedback_complete_boottime_s", "")).strip() else None
            legacy_overlap = bool_value(row.get("legacy_deadline_overlap"))
            write_overlap = bool_value(row.get("write_crossed_deadline"))
            feedback_overlap = bool_value(row.get("feedback_crossed_deadline"))
            syscall_fields = (syscall_start, syscall_return, write_ms)
            if status in write_attempt_statuses:
                if any(value is None for value in syscall_fields):
                    errors.append(f"v3_write_timing_missing:{action_id}:{status}")
                elif syscall_return < syscall_start:
                    errors.append(f"v3_syscall_order_invalid:{action_id}")
                elif abs(write_ms - (syscall_return - syscall_start) * 1000.0) > max(1.0, abs(write_ms) * 0.02):
                    errors.append(f"v3_write_duration_mismatch:{action_id}")
                elif deadline is None:
                    errors.append(f"v3_deadline_missing:{action_id}")
                elif write_overlap is None or write_overlap != (syscall_start < deadline < syscall_return):
                    errors.append(f"v3_write_deadline_overlap_mismatch:{action_id}")
            elif any(value is not None for value in syscall_fields):
                if any(value is None for value in syscall_fields):
                    errors.append(f"v3_partial_syscall_timing:{action_id}")
                elif syscall_return < syscall_start or abs(write_ms - (syscall_return - syscall_start) * 1000.0) > max(1.0, abs(write_ms) * 0.02):
                    errors.append(f"v3_syscall_timing_invalid:{action_id}")
            if feedback is not None:
                if deadline is None or feedback_overlap is None or feedback_overlap != (feedback > deadline):
                    errors.append(f"v3_feedback_deadline_overlap_mismatch:{action_id}")
            elif feedback_overlap is not None:
                errors.append(f"v3_feedback_overlap_without_timestamp:{action_id}")
            if (write_overlap is True or feedback_overlap is True) and legacy_overlap is not True:
                errors.append(f"v3_legacy_overlap_inconsistent:{action_id}")
        except (TypeError, ValueError, OverflowError):
            errors.append(f"v3_timing_value_invalid:{action_id}")
        v3_groups.setdefault(action_id, []).append(row)
    missing_v3 = sorted(set(v2_groups) - set(v3_groups))
    v3_without_v2 = sorted(set(v3_groups) - set(v2_groups))
    mismatched = []
    for action_id in sorted(set(v2_groups) & set(v3_groups)):
        v2_cases = {str(row.get("case_id", "")) for row in v2_groups[action_id]}
        v3_cases = {str(row.get("case_id", "")) for row in v3_groups[action_id]}
        if len(v2_cases) != 1 or v2_cases != v3_cases:
            mismatched.append(action_id)
    missing_states = {f"{action_id}:{status}": count - observed_state_counts.get((action_id, status), 0)
        for (action_id, status), count in expected_state_counts.items()
        if observed_state_counts.get((action_id, status), 0) < count}
    extra_states = {f"{action_id}:{status}": count - expected_state_counts.get((action_id, status), 0)
        for (action_id, status), count in observed_state_counts.items()
        if count > expected_state_counts.get((action_id, status), 0)}
    duplicate_states = sorted(f"{action_id}:{status}" for (action_id, status), count in observed_state_counts.items()
                              if count > 1 and count > expected_state_counts.get((action_id, status), 0))
    if missing_states:
        errors.append("v3_action_status_state_missing")
    if extra_states:
        errors.append("v3_duplicate_or_unpaired_status_state")
    if missing_v3:
        errors.append("missing_v3_action_rows")
    if v3_without_v2:
        errors.append("v3_action_without_v2_row")
    if mismatched:
        errors.append("action_case_mismatch")
    return {"complete": bool(scoped_v2 and scoped_v3 and not errors),
        "event_id": event_id, "v2_event_row_count": len(scoped_v2),
        "v3_event_row_count": len(scoped_v3),
        "v2_action_ids": sorted(v2_groups), "v3_action_ids": sorted(v3_groups),
        "missing_v3_action_ids": missing_v3, "v3_unpaired_action_ids": v3_without_v2,
        "case_mismatched_action_ids": mismatched, "missing_action_status_counts": missing_states,
        "extra_action_status_counts": extra_states, "duplicate_action_statuses": duplicate_states,
        "errors": errors,
        "action_status_sequences": {key: [str(row.get("ledger_status", "")) for row in rows]
                                     for key, rows in v3_groups.items()}}


def _scene4_preserve_remote_ledger(device: Device, remote_path: str, session_id: str,
                                   output_dir: Path, local_name: str) -> dict:
    """Preserve any pre-existing exact ledger before starting this run."""
    exists = device.shell("test -f " + quote(remote_path) + " && echo present || echo absent",
                          timeout=8, check=False).strip()
    if exists != "present":
        return {"remote_path": remote_path, "status": "absent_before_run", "verified": exists == "absent"}
    text = device.shell("cat " + quote(remote_path), timeout=15, check=False)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    archive = remote_path + ".preserved." + re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    archive_exists = device.shell("test -e " + quote(archive) + " && echo present || echo absent",
                                  timeout=8, check=False).strip()
    if archive_exists != "absent":
        return {"remote_path": remote_path, "status": "archive_collision", "verified": False,
                "archive_path": archive}
    move_output = device.shell("mv " + quote(remote_path) + " " + quote(archive), timeout=10, check=False)
    active_after = device.shell("test -e " + quote(remote_path) + " && echo present || echo absent",
                                timeout=8, check=False).strip()
    archived_text = device.shell("cat " + quote(archive), timeout=15, check=False)
    verified = (active_after == "absent" and
        hashlib.sha256(archived_text.encode("utf-8")).hexdigest() == digest)
    local_path = output_dir / local_name
    local_path.write_text(text, encoding="utf-8", newline="")
    return {"remote_path": remote_path, "archive_path": archive,
        "local_path": local_name, "sha256": digest, "bytes": len(text.encode("utf-8")),
        "status": "preserved" if verified else "preservation_failed", "verified": verified,
        "move_output": move_output, "active_readback": active_after}


def _scene4_should_end_counter_window(now_host: float, minimum_end_host: float,
                                      hold_end_hosts: list[float], unresolved_count: int) -> bool:
    return unresolved_count == 0 and now_host >= max([minimum_end_host, *hold_end_hosts])


def _scene4_hold_coverage(bundle: str, ready_event: dict | None, hold_event: dict | None,
                          hold_seconds: int, victims: list[dict]) -> dict:
    ready_time = _scene4_event_time(ready_event) if ready_event else None
    hold_time = _scene4_event_time(hold_event) if hold_event else None
    pid = str((ready_event or {}).get("pid", ""))
    uid = str((ready_event or {}).get("uid", ""))
    ticks = str((ready_event or {}).get("start_ticks", ""))
    hold_identity_match = bool(hold_event and
        str(hold_event.get("pid", "")) == pid and
        str(hold_event.get("uid", "")) == uid and uid and
        str(hold_event.get("start_ticks", "")) == ticks and pid and ticks)
    oom_terminated = any(victim.get("bundle") == bundle and
        str(victim.get("pid", "")) == pid and str(victim.get("uid", "")) == uid and uid and
        str(victim.get("start_ticks", "")) == ticks and
        ready_time is not None and victim.get("event_uptime_s", float("inf")) >= ready_time
        for victim in victims)
    observed_end = bool(hold_identity_match and ready_time is not None and hold_time is not None and
                        hold_time - ready_time >= hold_seconds)
    return {"hold_end_observed": observed_end, "confirmed_oom_termination": oom_terminated,
            "complete": ready_event is None or observed_end or oom_terminated,
            "hold_identity_match": hold_identity_match,
            "ready_boottime_s": ready_time, "hold_end_boottime_s": hold_time}


def _scene4_count_window_cutoff(end_marker_boottime: float, minimum_end_boottime: float,
                                outcomes: dict[str, dict], hold_seconds: int) -> dict:
    """Cap at actual hold ends, or the planned hold when a ready process died early."""
    observed = {}
    gaps = []
    planned_after_ready = {}
    for bundle, row in outcomes.items():
        if row.get("readiness_status") != "cold_start_ready":
            continue
        ready = row.get("cold_ready_event")
        ended = row.get("cold_start_hold_end")
        ready_s = _scene4_event_time(ready) if ready else None
        end_s = _scene4_event_time(ended) if ended else None
        same_identity = bool(ready and ended and
            all(str(ready.get(key, "")) == str(ended.get(key, "")) and str(ready.get(key, ""))
                for key in ("pid", "uid", "start_ticks")))
        if ready_s is None:
            gaps.append(bundle)
        elif end_s is None or not same_identity or end_s - ready_s < hold_seconds:
            # A post-ready OOM has no HAP hold_end. Keep the originally planned
            # window; the later identity-matched kernel evidence decides whether
            # the early death is a complete adverse outcome or an evidence gap.
            planned_after_ready[bundle] = ready_s + hold_seconds
            gaps.append(bundle)
        else:
            observed[bundle] = end_s
    requested = max([minimum_end_boottime, *observed.values(), *planned_after_ready.values()])
    cutoff = min(end_marker_boottime, requested)
    return {"counter_end_boottime": cutoff, "requested_end_boottime": requested,
            "end_marker_boottime": end_marker_boottime,
            "marker_after_cutoff_seconds": max(0.0, end_marker_boottime - cutoff),
            "marker_precedes_required_hold_end": requested > end_marker_boottime,
            "observed_hold_end_boottime": observed,
            "planned_hold_end_without_observed_event_boottime": planned_after_ready,
            "hold_end_evidence_gaps": gaps}


def _scene4_finalize_target_hold_journals(session: Session, targets: list[str],
        case_id: str, outcomes: dict[str, dict], hold_seconds: int,
        anchor: dict, read_timeout_seconds: float = 2.0) -> dict:
    """Do one short, targeted pre-END refresh for ready processes lacking a real hold_end.

    Readback completion establishes data integrity only. Exact process identity and
    the elapsed hold interval are separate requirements; missing events remain gaps.
    """
    started = time.monotonic()
    reads = []
    needs_refresh = []
    for bundle in targets:
        row = outcomes.get(bundle, {})
        ready = row.get("cold_ready_event") if row.get("readiness_status") == "cold_start_ready" else None
        if not ready:
            continue
        current_end = row.get("cold_start_hold_end")
        ready_s = _scene4_event_time(ready)
        end_s = _scene4_event_time(current_end) if current_end else None
        same = bool(current_end and all(str(current_end.get(key, "")) == str(ready.get(key, "")) and
                    str(ready.get(key, "")) for key in ("pid", "uid", "start_ticks")))
        if not (ready_s is not None and end_s is not None and same and end_s - ready_s >= hold_seconds):
            needs_refresh.append(bundle)
    for bundle in needs_refresh:
        row = outcomes[bundle]
        ready = row["cold_ready_event"]
        ready_s = _scene4_event_time(ready)
        rows = _scene4_read_journal(session, bundle, case_id,
                                    timeout_seconds=read_timeout_seconds)
        proof = session.scene4_journal_readback.get((case_id, bundle), {})
        hold_end = next((item for item in reversed(rows)
            if item.get("event") == "cold_start_hold_end" and
            all(str(item.get(key, "")) == str(ready.get(key, "")) and
                str(ready.get(key, "")) for key in ("pid", "uid", "start_ticks"))), None)
        end_s = _scene4_event_time(hold_end) if hold_end else None
        valid_end = bool(proof.get("complete") is True and ready_s is not None and
                         end_s is not None and end_s - ready_s >= hold_seconds)
        reads.append({"bundle": bundle, "readback_complete": proof.get("complete") is True,
            "readback_sha256": proof.get("raw_payload_sha256"),
            "readback_rows": proof.get("parsed_row_count"),
            "read_duration_seconds": proof.get("read_duration_seconds"),
            "ready_boottime_s": ready_s, "hold_end_boottime_s": end_s,
            "hold_end_identity_match": hold_end is not None,
            "hold_duration_s": end_s - ready_s if ready_s is not None and end_s is not None else None,
            "hold_end_valid": valid_end})
        if valid_end:
            row["cold_start_hold_end"] = hold_end
            row["estimated_hold_end_host"] = end_s + anchor["host_minus_device_seconds"]
    elapsed = time.monotonic() - started
    all_readbacks_complete = all(row.get("readback_complete") is True for row in reads)
    latest_read = {row["bundle"]: row for row in reads}
    final = {"complete": all_readbacks_complete,
        "all_readbacks_complete": all_readbacks_complete,
        "all_ready_hold_ends_observed": all(latest_read.get(bundle, {}).get("hold_end_valid") is True
            for bundle in needs_refresh),
        "refresh_scope": "only_cold_ready_targets_missing_a_valid_hold_end",
        "max_one_read_per_target": True, "per_read_timeout_seconds": read_timeout_seconds,
        "elapsed_seconds_including_remote_reads": elapsed,
        "elapsed_is_not_a_global_deadline": True,
        "hold_seconds": hold_seconds, "targets_refreshed": needs_refresh,
        "reads": reads, "latest_by_bundle": latest_read,
        "final_readback_proofs": {bundle: session.scene4_journal_readback.get((case_id, bundle))
                                   for bundle in needs_refresh}}
    return final


def _scene4_donor_validation_complete(donor_validation: dict[str, dict],
                                      expected_donors: set[str],
                                      donor_oom_bundles: set[str]) -> bool:
    return set(donor_validation) == expected_donors and all(value.get("status") == "complete" or
        (value.get("status") == "unknown_donor_missing_after_window" and bundle in donor_oom_bundles)
        for bundle, value in donor_validation.items())


def _scene4_donor_checksum_complete(first_touch: dict | None,
                                   validation_end: dict | None) -> bool:
    if not first_touch or not validation_end:
        return False
    expected = str(first_touch.get("donor_expected_checksum", ""))
    initial_read = str(first_touch.get("donor_initial_read_checksum", ""))
    return bool(first_touch.get("donor_initial_read_complete") is True and
        validation_end.get("donor_validation_complete") is True and expected and
        initial_read == expected and
        str(validation_end.get("donor_expected_checksum", "")) == expected and
        str(validation_end.get("donor_observed_checksum", "")) == expected and
        int(first_touch.get("touched_bytes", 0)) == 3072 * 1024**2)


def _scene4_ui_evidence_proof(rows: list[dict], errors: list[dict],
                              max_gap_seconds: float = 10.0,
                              case_id: str | None = None,
                              begin_host_monotonic: float | None = None,
                              end_host_monotonic: float | None = None) -> dict:
    """Keep UI read/clock/screenshot faults and long sampling gaps explicit."""
    case_id = case_id or (rows[0].get("case_id") if rows else None)
    case_errors = [item for item in errors if case_id is not None and item.get("case_id") == case_id]
    ordered = sorted((row for row in rows if isinstance(row.get("host_monotonic"), (int, float))),
                     key=lambda row: float(row["host_monotonic"]))
    times = [float(row["host_monotonic"]) for row in ordered]
    durations = [max(0.0, float(row.get("capture_completed_host_monotonic", row["host_monotonic"])) -
                     float(row.get("capture_started_host_monotonic", row["host_monotonic"])))
                 for row in ordered]
    boundary_times = ([float(begin_host_monotonic)] if begin_host_monotonic is not None else []) + \
        times + ([float(end_host_monotonic)] if end_host_monotonic is not None else [])
    boundary_gaps = [right - left for left, right in zip(boundary_times, boundary_times[1:])]
    coverage_start = float(begin_host_monotonic) if begin_host_monotonic is not None else (times[0] if times else None)
    coverage_end = float(end_host_monotonic) if end_host_monotonic is not None else (times[-1] if times else None)
    coverage_seconds = max(0.0, coverage_end - coverage_start) if coverage_start is not None and coverage_end is not None else None
    row_validity = []
    for row in rows:
        snapshot_captured = bool(row.get("layout_captured") or
            (row.get("window_snapshot_captured") and row.get("mission_snapshot_captured")))
        light = row.get("scene4_light_inventory")
        inventory_complete = light.get("complete") is True if isinstance(light, dict) else True
        row_validity.append(bool(snapshot_captured and
            inventory_complete and
            row.get("capture_clock_valid") and not row.get("error") and
            not row.get("mission_error") and not row.get("clock_error") and
            (not row.get("native_candidate_nodes_seen") or
             row.get("native_window_visibility_complete") is True) and
            (not row.get("screenshot_needed") or row.get("screenshot_captured"))))
    valid = bool(rows) and all(row_validity) and not case_errors and bool(times) and \
        all(gap <= max_gap_seconds for gap in boundary_gaps)
    return {"complete": valid, "observation_count": len(rows),
        "all_observations_valid": bool(rows) and all(row_validity),
        "case_error_count": len(case_errors), "case_errors": case_errors,
        "expected_sample_period_seconds": 5.0, "maximum_allowed_gap_seconds": max_gap_seconds,
        "sample_intervals_seconds": [times[index + 1] - times[index] for index in range(len(times) - 1)],
        "sample_durations_seconds": durations,
        "sample_host_monotonic": times,
        "case_begin_host_monotonic": begin_host_monotonic,
        "case_end_host_monotonic": end_host_monotonic,
        "coverage_seconds": coverage_seconds,
        "boundary_intervals_seconds": boundary_gaps,
        "boundary_gap_count": sum(gap > max_gap_seconds for gap in boundary_gaps),
        "maximum_sample_gap_seconds": max(boundary_gaps) if boundary_gaps else None,
        "long_gap_count": sum(gap > max_gap_seconds for gap in boundary_gaps),
        "popup_absence_claim": "no popup was visible in captured snapshots; this does not prove no transient popup occurred between snapshots"}


def _scene4_unique_incident_ids(case_id: str, startup_classes: list[dict],
                                kernel_victims: list[dict],
                                native_popups: list[dict]) -> set[str]:
    """Deduplicate startup/kernel identity evidence while keeping AA and UI events case-local."""
    identities = {item.get("incident_id") or
                  f"{item.get('bundle')}:{item.get('pid')}:{item.get('start_ticks')}"
                  for item in kernel_victims}
    result = set(identities)
    for item in startup_classes:
        if item.get("classification") != "startup_failure":
            continue
        identity = item.get("incident_identity")
        result.add(identity if identity else f"{case_id}:{item.get('bundle')}:startup_attempt")
    for index, event in enumerate(native_popups):
        event_key = event.get("incident") or event.get("source_id") or \
            f"{event.get('bundle')}:{event.get('timestamp_ms')}:{index}"
        result.add(f"{case_id}:native_popup:{event_key}")
    return result


def _scene4_all_ui_evidence_complete(cases: list[dict]) -> bool:
    return len(cases) == 4 and all(
        case.get("ui_evidence_complete") is True and
        case.get("ui_evidence", {}).get("complete") is True for case in cases)


def _scene4_cumulative_counts_observed(result: dict) -> bool:
    """The run total is complete only when all four fixed cases are complete."""
    cases = result.get("cases", [])
    return bool(result.get("measurement_complete") is True and
        result.get("donor_proof_only_probe") is None and
        len(cases) == 4 and all(case.get("measurement_complete") is True for case in cases))


def _scene4_finalize_cumulative_counts(result: dict) -> bool:
    """Re-apply the whole-run count gate after cleanup/finalization can invalidate it."""
    observed = _scene4_cumulative_counts_observed(result)
    result["cumulative_counts_observed"] = observed
    if not observed and isinstance(result.get("cumulative_counts"), dict):
        result["cumulative_counts"] = {key: None for key in result["cumulative_counts"]}
    return observed


def _scene4_primary_count_snapshot(scene4_result: dict) -> dict:
    cumulative = scene4_result.get("cumulative_counts")
    observed = (_scene4_cumulative_counts_observed(scene4_result) and
                isinstance(cumulative, dict) and cumulative.get("primary_count") is not None)
    return {"primary_count": int(cumulative["primary_count"]) if observed else None,
            "primary_count_observed": observed,
            "measurement_complete": bool(scene4_result.get("measurement_complete"))}


def _scene4_write_checkpoint(output: Path, result: dict, status: str,
                             error: str | None = None) -> dict:
    """Durably preserve completed Scene4 cases and explicitly mark partial counts N/A."""
    cumulative = result.get("cumulative_counts")
    observed = (_scene4_cumulative_counts_observed(result) and
                isinstance(cumulative, dict) and cumulative.get("primary_count") is not None)
    marker = {"schema": "scene4-checkpoint-v1", "status": status,
        "completed_case_count": len(result.get("cases", [])), "planned_case_count": 4,
        "measurement_complete": bool(result.get("measurement_complete")),
        "primary_count": cumulative.get("primary_count") if observed else None,
        "primary_count_observed": observed,
        "confirmed_count_lower_bounds": result.get("confirmed_count_lower_bounds"),
        "error": error, "cases": result.get("cases", [])}
    path = Path(output) / "scene4_checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(marker, ensure_ascii=False, indent=2, default=str) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
    return {"path": str(path), "status": status,
        "completed_case_count": marker["completed_case_count"],
        "primary_count_observed": observed}


def _scene4_append_durable_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _scene4_invalidate_schedule(session: Session, schedule_remote: str) -> dict:
    """Atomically retire only this run's active schedule and verify both paths."""
    device = session.device
    presence = device.shell("test -e " + quote(schedule_remote) + " && echo present || echo absent",
                            timeout=5, check=False).strip()
    if presence == "absent":
        return {"status": "already_absent", "verified": True, "active_path": schedule_remote}
    current = device.shell("cat " + quote(schedule_remote), timeout=10, check=False)
    if not current.strip():
        return {"status": "already_absent", "verified": True, "active_path": schedule_remote}
    if session.id not in current:
        return {"status": "ownership_conflict", "verified": False,
                "active_path": schedule_remote, "observed_session_id": None}
    retired = schedule_remote + ".invalidated." + re.sub(r"[^A-Za-z0-9_.-]", "_", session.id)
    moved = device.shell("mv -f " + quote(schedule_remote) + " " + quote(retired), timeout=10, check=False)
    active_after = device.shell("test -e " + quote(schedule_remote) + " && echo present || echo absent",
                                timeout=5, check=False).strip()
    retired_after = device.shell("cat " + quote(retired), timeout=10, check=False)
    verified = active_after == "absent" and session.id in retired_after
    return {"status": "invalidated" if verified else "invalidation_failed", "verified": verified,
            "active_path": schedule_remote, "retired_path": retired,
            "active_readback": active_after, "retired_contains_session": session.id in retired_after,
            "command_output": moved}


def scene4_legacy_reclaim_diagnostic(session: Session) -> Path:
    """Run the oracle-backed cold-start case; scene 4 remains diagnostic only."""
    if session.scene != 4 or session.profile != "development" or session.phase not in ("baseline", "optimized"):
        raise ValueError("场景4仅支持baseline/optimized + development敏感性预验证")
    cfg = session.config["scene4"]
    all_bundles = list(session.config["test_apps"])
    donors = list(cfg.get("donor_bundles", all_bundles[:2]))
    targets = list(cfg.get("cold_start_targets", all_bundles[2:8]))
    if len(all_bundles) != 8 or len(donors) != 2 or len(targets) != 6 or set(donors + targets) != set(all_bundles):
        raise ValueError("scene4 cold start requires exactly a1/a2 donors and a3-a8 targets")
    schedule_remote = cfg.get("oracle_schedule_remote_path")
    ledger_remote = cfg.get("oracle_ledger_remote_path")
    if not schedule_remote or not ledger_remote:
        raise ValueError("scene4 oracle schedule/ledger paths are required")
    case_id = f"{session.id}_c0001"
    ready_timeout = float(cfg.get("cold_start_ready_timeout_seconds", cfg.get("ready_timeout_seconds", 180)))
    launch_lead = float(cfg.get("launch_lead_seconds", 5.0))
    lifetime = int(cfg.get("lifetime_seconds", 720))
    donor_daily = int(cfg.get("donor_daily_mib", cfg["daily_mib"]))
    donor_peak = int(cfg.get("donor_peak_mib", cfg["peak_mib"]))
    target_daily = int(cfg.get("target_daily_mib", cfg["daily_mib"]))
    target_peak = int(cfg.get("target_peak_mib", cfg["peak_mib"]))
    planned_needed_bytes = int(cfg.get("planned_needed_bytes", cfg.get("cold_start_planned_needed_bytes", 0)))
    seed = int(cfg.get("seed", 1))
    if (ready_timeout <= 0 or lifetime <= ready_timeout or launch_lead < 3 or planned_needed_bytes <= 0):
        raise ValueError("scene4 cold-start timeout/lifetime/lead/planned_needed_bytes config invalid")

    result: dict = {"schema": "memscenes-scene4-cold-start-v2", "case_id": case_id,
        "scope": "oracle_backed_sensitivity_diagnostic_only", "donors": {}, "targets": {},
        "schedule": {}, "ledger": {}, "startup_classifications": [],
        "oom_victims": [], "other_oom_side_effects": [], "measurement_complete": False,
        "formal_qualification_eligible": False,
        "readiness_definition": "peak_reached + startup_ready + cold_start_ready(memory_ready=true,window_ready=true,peak_bytes>0)"}
    d = session.device
    try:
        env = validate_environment(session)
        result["environment"] = env
        session.enable_oom_trace()
        session.start_kernel_dmesg_reader()
        for bundle in all_bundles:
            d.force_stop(bundle)
        for bundle in all_bundles:
            if _scene4_memcg_snapshot(d, bundle)["processes"]:
                raise RuntimeError(f"owned app is not absent before scene4: {bundle}")
        for bundle in all_bundles:
            remaining = _scene4_memcg_snapshot(d, bundle)
            if remaining["processes"]:
                raise RuntimeError(f"owned process remains before scene4 run: {bundle}")

        # Warm both donors simultaneously. Their exact cold-ready identity is
        # captured before either ability is sent to the background.
        donor_launch = {}
        donor_mib = {"daily_mib": donor_daily, "peak_mib": donor_peak,
            "file_mib": int(cfg.get("donor_file_mib", cfg["file_mib"])),
            "hold_seconds": int(cfg.get("donor_hold_seconds", cfg["hold_seconds"])),
            "lifetime_seconds": lifetime, "seed": seed, "bundle": "",
            "case_id": case_id, "daily_case_id": case_id + "_daily",
            "await_peak": 0, "donor_evidence": 1, "cold_start_ready": 1,
            "window_ready_gate": 1}
        def launch_donor(bundle: str) -> None:
            session.owned_page_files.add((bundle, case_id))
            params = dict(donor_mib, bundle=bundle)
            donor_launch[bundle] = d.launch(bundle, params=params, timeout=60)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(launch_donor, donors))
        donor_deadline = time.monotonic() + ready_timeout
        donor_identities: dict[str, dict] = {}
        donor_ready_rows: dict[str, dict] = {}
        donor_errors: dict[str, str] = {}
        process_history: dict[int, list[dict]] = {}
        while time.monotonic() < donor_deadline and len(donor_ready_rows) + len(donor_errors) < 2:
            for bundle in donors:
                if bundle in donor_ready_rows or bundle in donor_errors:
                    continue
                try:
                    rows = _scene4_read_journal(session, bundle, case_id)
                    ready = next((row for row in reversed(rows) if row.get("event") == "cold_start_ready" and
                                  row.get("memory_ready") is True and row.get("window_ready") is True and
                                  int(row.get("peak_bytes", 0)) > 0), None)
                    if ready:
                        pid, uid = int(ready.get("pid", -1)), int(ready.get("uid", -1))
                        installed_uid = int(d.app(bundle)["uid"])
                        proc = d.inspect_process_identity(pid, timeout=10) if pid > 0 else {}
                        identity = proc.get("identity")
                        error = proc_identity_mismatch(identity, pid, installed_uid, None) if pid > 0 else "pid_missing"
                        process = d.app_processes(bundle).get(f"{pid}:{identity.get('start_ticks')}") if identity else None
                        if uid != installed_uid or error or not process:
                            donor_errors[bundle] = "cold_ready_identity_unverified"
                        else:
                            donor_ready_rows[bundle] = ready
                            donor_identities[bundle] = identity
                            donor_uptime = _scene4_event_time(ready) or float(d.shell("cat /proc/uptime").split()[0])
                            process_history.setdefault(pid, []).append({"bundle": bundle,
                                "uid": installed_uid, "start_ticks": str(identity["start_ticks"]),
                                "first_uptime": donor_uptime, "first_absent_uptime": None,
                                "last_seen_uptime": donor_uptime})
                except Exception as exc:
                    result.setdefault("donor_evidence_errors", {})[bundle] = str(exc)
            if len(donor_ready_rows) + len(donor_errors) < 2:
                time.sleep(0.5)
        for bundle in donors:
            if bundle not in donor_ready_rows and bundle not in donor_errors:
                donor_errors[bundle] = "cold_start_ready_timeout_unknown"
        if donor_errors:
            result["donor_setup_classification"] = "unknown_donor_cold_start_or_identity"
            result["donor_setup_errors"] = donor_errors
            raise RuntimeError("scene4 donors did not reach verified cold_start_ready")

        # Each owned donor is explicitly minimized from its focused app window.
        donor_transitions = [_scene4_minimize_donor(session, bundle, case_id,
            donor_identities[bundle]) for bundle in donors]
        result["donor_background_transitions"] = donor_transitions
        schedule_fields = ["event_id", "case_id", "launch_boottime_s", "planned_needed_bytes"]
        donor_field_suffixes = ["bundle", "uid", "pid", "start_ticks", "cold_ready",
            "window_ready", "cold_ready_boottime_s", "background_since_s", "last_heartbeat_s"]
        for prefix in ("donor_a", "donor_b"):
            schedule_fields.extend(f"{prefix}_{field}" for field in donor_field_suffixes)
        event_id = f"{case_id}_cold_start"
        before = _scene4_clock_anchor(d)
        launch_host_ns = before["host_monotonic_midpoint_ns"] + int(launch_lead * 1e9)
        launch_boottime = before["device_uptime_seconds"] + (
            launch_host_ns - before["host_monotonic_midpoint_ns"]) / 1e9
        result["clock_anchor"] = before
        result["launch_boottime_s"] = launch_boottime

        schedule_path = session.output / "oracle_schedule.tsv"
        remote_parent = schedule_remote.rsplit("/", 1)[0] if "/" in schedule_remote else "."
        d.shell("mkdir -p " + quote(remote_parent), check=False)
        prior = d.shell("cat " + quote(schedule_remote), check=False)
        if prior.strip():
            prior_local = session.output / "oracle_schedule_previous.tsv"
            prior_local.write_text(prior.rstrip("\n") + "\n", encoding="utf-8")
            d.call("file", "send", str(prior_local), schedule_remote + ".previous", timeout=45)
        d.shell("rm -f " + quote(ledger_remote), check=False)

        def refresh_schedule() -> list[dict]:
            uptime_now = float(d.shell("cat /proc/uptime").split()[0])
            # Capture independent focused-window and mission evidence before each refresh.
            layout = d.dump_ui(session.remote + "/scene4_window.json", timeout=20)
            layout_path = session.output / "ui" / "scene4_window_latest.json"
            save(layout_path, layout)
            focused = focused_bundles(layout)
            missions = d.shell("aa dump -l", timeout=10, check=False)
            missions_path = session.output / "ui" / "scene4_missions_latest.txt"
            missions_path.parent.mkdir(parents=True, exist_ok=True)
            missions_path.write_text(missions, encoding="utf-8")
            status_rows = []
            donor_payloads = []
            for bundle in donors:
                installed_uid = int(d.app(bundle)["uid"])
                identity = donor_identities[bundle]
                proof = _scene4_donor_evidence(session, bundle, case_id, identity,
                                               installed_uid, uptime_now)
                active_processes = d.app_processes(bundle)
                current_key = f"{identity['pid']}:{identity['start_ticks']}"
                proof["window_focused"] = bundle in focused
                proof["ability_mission_visible"] = bundle in missions
                proof["cgroup_identity_present_at_write"] = current_key in active_processes
                proof["verified"] = bool(proof["verified"] and current_key in active_processes and
                                         not proof["window_focused"])
                status_rows.append(proof)
                donor_payloads.append({"bundle": bundle, "uid": installed_uid,
                    "pid": int(identity["pid"]), "start_ticks": str(identity["start_ticks"]),
                    "cold_ready": int(proof["cold_ready"] is not None),
                    "window_ready": int(proof["cold_ready"].get("window_ready") is True
                                         if proof["cold_ready"] else 0),
                    "cold_ready_boottime_s": proof["cold_ready_boottime_s"] or 0,
                    "background_since_s": proof["background_since_s"] or 0,
                    "last_heartbeat_s": proof["last_heartbeat_s"] or 0,
                    "verified": proof["verified"]})
            # All six cold-start targets must still have no process before the barrier.
            cold_targets_absent = {bundle: not bool(d.app_processes(bundle)) for bundle in targets}
            if not all(cold_targets_absent.values()):
                raise RuntimeError("scene4 target process appeared before synchronized launch")
            if not all(row["verified"] for row in donor_payloads):
                raise RuntimeError("scene4 donor background lifecycle/window/heartbeat evidence became stale")
            values = [event_id, case_id, f"{launch_boottime:.6f}", str(planned_needed_bytes)]
            for donor in donor_payloads:
                values.extend([donor["bundle"], str(donor["uid"]), str(donor["pid"]),
                    donor["start_ticks"], str(donor["cold_ready"]), str(donor["window_ready"]),
                    f"{donor['cold_ready_boottime_s']:.6f}",
                    f"{donor['background_since_s']:.6f}", f"{donor['last_heartbeat_s']:.6f}"])
            with schedule_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, delimiter="|", lineterminator="\n")
                writer.writerow(schedule_fields); writer.writerow(values)
            payload = schedule_path.read_text(encoding="utf-8")
            _scene4_atomic_schedule_write(session, schedule_path, schedule_remote, payload)
            result.setdefault("donor_refreshes", []).append({"device_uptime_seconds": uptime_now,
                "donors": status_rows, "focused_bundles": sorted(focused),
                "targets_absent": cold_targets_absent,
                "schedule_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()})
            return status_rows

        session.mark("scene4_cold_start_begin", case_id=case_id, donors=donors, targets=targets,
                     launch_boottime_s=launch_boottime)
        begin_uptime, begin_wall = d.clock_pair()
        session.mark_kernel_dmesg_boundary(case_id, "BEGIN", begin_uptime, begin_wall)
        refresh_schedule()
        next_refresh = time.monotonic() + 1.0
        while time.monotonic_ns() < launch_host_ns:
            delay = next_refresh - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, max(0.01, (launch_host_ns - time.monotonic_ns()) / 1e9)))
            if time.monotonic_ns() >= launch_host_ns:
                break
            refresh_schedule()
            next_refresh = time.monotonic() + 1.0
        result["schedule"].update({"event_id": event_id, "remote_path": schedule_remote,
            "fields": schedule_fields, "launch_host_monotonic_ns": launch_host_ns,
            "launch_boottime_s": launch_boottime, "refresh_count": len(result.get("donor_refreshes", [])),
            "atomic_updates": True, "minimum_write_spacing_seconds": 1.0})

        target_results = {bundle: {"bundle": bundle, "launch_status": "unknown",
            "readiness_status": "pending", "scheduled_host_monotonic_ns": launch_host_ns,
            "actual_host_monotonic_ns": None, "journal_evidence_complete": False}
            for bundle in targets}
        def direct_target_launch(bundle: str) -> None:
            remaining_ns = launch_host_ns - time.monotonic_ns()
            if remaining_ns > 0:
                time.sleep(remaining_ns / 1e9)
            row = target_results[bundle]
            row["actual_host_monotonic_ns"] = time.monotonic_ns()
            session.owned_page_files.add((bundle, case_id))
            params = {"case_id": case_id, "daily_case_id": case_id + "_daily",
                "daily_mib": target_daily, "peak_mib": target_peak,
                "file_mib": int(cfg.get("target_file_mib", cfg["file_mib"])),
                "hold_seconds": int(cfg.get("target_hold_seconds", cfg["hold_seconds"])),
                "lifetime_seconds": lifetime, "seed": seed, "bundle": bundle,
                "await_peak": 1, "cold_start_ready": 1, "window_ready_gate": 1}
            try:
                row["launch_output"] = d.launch(bundle, params=params, timeout=60)
                row["launch_status"] = "accepted"
            except Exception as exc:
                message = str(exc)
                row["launch_error"] = message
                if "hdc操作失败" not in message and re.search(r"(?i)\b(error|failed|invalid|rejected)\b", message):
                    row.update(launch_status="failure", reason="aa_explicit_launch_rejection")
                else:
                    row.update(launch_status="unknown", reason="launch_transport_or_timeout_unknown")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
            list(pool.map(direct_target_launch, targets))
        result["targets"] = target_results

        # Wait for both daily memory and the actual page-load callback before promoting.
        daily_deadline = time.monotonic() + ready_timeout
        daily_rows: dict[str, list[dict]] = {bundle: [] for bundle in targets}
        daily_status: dict[str, str] = {bundle: "pending" for bundle in targets}
        while time.monotonic() < daily_deadline and any(state == "pending" for state in daily_status.values()):
            for bundle in targets:
                if daily_status[bundle] != "pending":
                    continue
                if target_results[bundle]["launch_status"] != "accepted":
                    daily_status[bundle] = "unknown"
                    continue
                try:
                    rows = _scene4_read_journal(session, bundle, case_id + "_daily")
                    daily_rows[bundle] = rows
                    target_results[bundle]["journal_evidence_complete"] = True
                    daily_ready = any(row.get("event") == "daily_ready" and
                                      int(row.get("ready_bytes", 0)) > 0 for row in rows)
                    window_loaded = any(row.get("event") == "window_content_loaded" for row in rows)
                    failed_alloc = next((row for row in rows if row.get("event") == "allocation_failed" and
                                         int(row.get("errno", 0)) == 12), None)
                    if failed_alloc:
                        daily_status[bundle] = "confirmed_enomem"
                        target_results[bundle]["confirmed_enomem"] = failed_alloc
                    elif daily_ready and window_loaded:
                        daily_status[bundle] = "ready_for_promote"
                    target_results[bundle]["daily_ready"] = daily_ready
                    target_results[bundle]["window_content_loaded"] = window_loaded
                except Exception as exc:
                    target_results[bundle]["daily_journal_error"] = str(exc)
                    daily_status[bundle] = "unknown"
            if any(state == "pending" for state in daily_status.values()):
                time.sleep(0.5)
        for bundle in targets:
            if daily_status[bundle] == "pending":
                daily_status[bundle] = "unknown_timeout"
        ready_to_promote = [bundle for bundle in targets if daily_status[bundle] == "ready_for_promote"]
        def promote_target(bundle: str) -> None:
            try:
                target_results[bundle]["promote_output"] = d.promote(bundle, case_id)
                target_results[bundle]["promote_status"] = "accepted"
            except Exception as exc:
                target_results[bundle].update(promote_status="unknown", promote_error=str(exc))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(ready_to_promote))) as pool:
            list(pool.map(promote_target, ready_to_promote))

        terminal_deadline = time.monotonic() + ready_timeout
        terminal_rows: dict[str, list[dict]] = {bundle: [] for bundle in targets}
        last_observed: dict[str, dict] = {}
        while time.monotonic() < terminal_deadline and any(
                target_results[bundle].get("readiness_status") == "pending"
                for bundle in targets):
            now_uptime = float(d.shell("cat /proc/uptime").split()[0])
            for observed_bundle in all_bundles:
                try:
                    observed_uid = int(d.app(observed_bundle)["uid"])
                    live = d.app_processes(observed_bundle)
                    live_ids = {(int(process["pid"]), str(process["start_ticks"]))
                                for process in live.values()}
                    for prior_pid, prior_rows in process_history.items():
                        for prior in prior_rows:
                            if (prior["bundle"] == observed_bundle and prior.get("first_absent_uptime") is None and
                                    (prior_pid, str(prior["start_ticks"])) not in live_ids):
                                prior["first_absent_uptime"] = now_uptime
                    for process in live.values():
                        pid, ticks = int(process["pid"]), str(process["start_ticks"])
                        identities = process_history.setdefault(pid, [])
                        item = next((candidate for candidate in identities if candidate["bundle"] == observed_bundle and
                                     candidate["uid"] == observed_uid and candidate["start_ticks"] == ticks), None)
                        if item is None:
                            identities.append({"bundle": observed_bundle, "uid": observed_uid,
                                "start_ticks": ticks, "first_uptime": now_uptime,
                                "first_absent_uptime": None, "last_seen_uptime": now_uptime})
                        else:
                            item["last_seen_uptime"] = now_uptime
                except Exception as exc:
                    result.setdefault("process_observation_errors", []).append({
                        "bundle": observed_bundle, "uptime_seconds": now_uptime, "error": str(exc)})
            for bundle in targets:
                row = target_results[bundle]
                if row.get("readiness_status") != "pending":
                    continue
                if daily_status[bundle] in ("confirmed_enomem", "unknown", "unknown_timeout"):
                    row["readiness_status"] = ("confirmed_startup_failure" if daily_status[bundle] == "confirmed_enomem"
                                               else "unknown")
                    row.setdefault("reason", daily_status[bundle])
                    continue
                try:
                    rows = _scene4_read_journal(session, bundle, case_id)
                    terminal_rows[bundle] = rows
                    ready_row = next((item for item in reversed(rows) if item.get("event") == "cold_start_ready" and
                                      item.get("memory_ready") is True and item.get("window_ready") is True and
                                      int(item.get("peak_bytes", 0)) > 0), None)
                    required = {"peak_reached", "startup_ready", "cold_start_ready"}
                    events = {item.get("event") for item in rows}
                    if ready_row and required.issubset(events):
                        pid, uid = int(ready_row.get("pid", -1)), int(ready_row.get("uid", -1))
                        installed_uid = int(d.app(bundle)["uid"])
                        observed = d.inspect_process_identity(pid, timeout=10) if pid > 0 else {}
                        identity = observed.get("identity")
                        identity_error = proc_identity_mismatch(identity, pid, installed_uid, None) if pid > 0 else "ready_pid_missing"
                        if uid == installed_uid and identity_error is None:
                            row.update(readiness_status="cold_start_ready", ready_event=ready_row,
                                       identity=identity, identity_verified=True)
                        else:
                            row.update(readiness_status="unknown", reason="ready_identity_unverified",
                                       identity=identity, identity_error=identity_error)
                    else:
                        failures = [item for item in rows if item.get("event") == "allocation_failed" and
                                    int(item.get("errno", 0)) == 12]
                        if failures:
                            row.update(readiness_status="confirmed_startup_failure", enomem=failures)
                        else:
                            row["readiness_status"] = "pending"
                    row["journal_evidence_complete"] = True
                    processes = d.app_processes(bundle)
                    last_observed[bundle] = {"uptime_seconds": now_uptime, "processes": list(processes.values())}
                except Exception as exc:
                    row.update(readiness_status="unknown", reason="readiness_evidence_unavailable",
                               evidence_error=str(exc))
            time.sleep(0.5)
        for bundle, row in target_results.items():
            if row.get("readiness_status") == "pending":
                row.update(readiness_status="unknown", reason="cold_start_ready_timeout_or_missing_journal")
        result["targets"] = target_results
        result["daily_status"] = daily_status

        end_uptime, end_wall = d.clock_pair()
        session.mark_kernel_dmesg_boundary(case_id, "END", end_uptime, end_wall)
        all_rows: dict[str, list[dict]] = {}
        attempts: dict[str, list[dict]] = {}
        for bundle in all_bundles:
            rows = _scene4_read_journal(session, bundle, case_id)
            if bundle in targets:
                rows = daily_rows.get(bundle, []) + rows
            all_rows[bundle] = rows
            attempts[bundle] = [row for row in rows if row.get("event") == "peak_begin"]
        kernel_info = session.collect_kernel_dmesg_case(case_id, process_history,
            all_bundles, attempts, begin_uptime, end_uptime, begin_wall, begin_uptime,
            str(session.kernel_dmesg_path.relative_to(session.output)))
        result["kernel_oom_evidence"] = kernel_info

        ready_times = {bundle: next((_scene4_event_time(row) for row in reversed(terminal_rows.get(bundle, []))
                                     if row.get("event") == "cold_start_ready" and
                                     row.get("memory_ready") is True and row.get("window_ready") is True), None)
                       for bundle in targets}
        startup_classes = []
        oom_victims = kernel_info.get("victims", [])
        for bundle in targets:
            row = target_results[bundle]
            ready_s = ready_times.get(bundle)
            enomem = [item for item in all_rows.get(bundle, []) if item.get("event") == "allocation_failed" and
                      int(item.get("errno", 0)) == 12 and
                      (ready_s is None or (_scene4_event_time(item) or float("inf")) <= ready_s)]
            aa_failure = row.get("launch_status") == "failure"
            victim = next((item for item in oom_victims if item.get("bundle") == bundle and
                           (ready_s is None or item.get("event_uptime_s", float("inf")) <= ready_s)), None)
            if enomem or aa_failure or victim:
                classification = {"bundle": bundle, "classification": "startup_failure",
                    "evidence_kind": "ENOMEM" if enomem else ("aa_explicit_rejection" if aa_failure else "identity_kernel_kill"),
                    "evidence": enomem or ([row.get("launch_error")] if aa_failure else [victim]),
                    "unique_incident": f"{bundle}:{(victim or {}).get('pid', row.get('ready_event', {}).get('pid', 'unknown'))}:{((victim or {}).get('start_ticks', 'unknown'))}"}
            elif row.get("readiness_status") == "cold_start_ready":
                classification = {"bundle": bundle, "classification": "cold_start_ready",
                    "ready_boottime_s": ready_s, "unique_incident": None}
            else:
                classification = {"bundle": bundle, "classification": "unknown_startup_timeout_or_evidence_gap",
                    "reason": row.get("reason", row.get("readiness_status")), "unique_incident": None}
            startup_classes.append(classification)
        result["startup_classifications"] = startup_classes
        result["oom_victims"] = oom_victims
        owned_names = set(all_bundles)
        raw_dmesg = session.kernel_dmesg_path.read_text(encoding="utf-8", errors="replace") if session.kernel_dmesg_path.exists() else ""
        result["other_oom_side_effects"] = [line for line in raw_dmesg.splitlines()
            if re.search(r"mem_oom.*(?:out of Memory|kill process)", line, re.I) and
            not any(str(victim.get("pid", "")) in line for victim in oom_victims)]
        incidents = {row["unique_incident"] for row in startup_classes + [
            {"unique_incident": f"{item.get('bundle')}:{item.get('pid')}:{item.get('start_ticks')}"}
            for item in oom_victims] if row.get("unique_incident")}
        counts = {"startup_failure": sum(row["classification"] == "startup_failure" for row in startup_classes),
            "cold_start_ready": sum(row["classification"] == "cold_start_ready" for row in startup_classes),
            "unknown_timeout_or_gap": sum(row["classification"].startswith("unknown") for row in startup_classes),
            "oom_kill": len(oom_victims), "donor_oom_side_effects": sum(item.get("bundle") in donors for item in oom_victims),
            "target_oom_side_effects": sum(item.get("bundle") in targets for item in oom_victims),
            "other_oom_side_effects": len(result["other_oom_side_effects"]),
            "unique_incidents": len(incidents),
            "classification_events_total": sum(row["classification"] == "startup_failure" for row in startup_classes) + len(oom_victims)}
        result["classification_counts"] = counts

        ledger_until = time.monotonic() + float(cfg.get("oracle_ledger_timeout_seconds", 30))
        ledger_text = ""
        while time.monotonic() < ledger_until:
            ledger_text = d.shell("cat " + quote(ledger_remote), timeout=10, check=False)
            parsed_ledger = list(csv.DictReader(ledger_text.splitlines())) if ledger_text.strip() else []
            if any(row.get("event_id") == event_id for row in parsed_ledger):
                break
            time.sleep(1.0)
            if event_id in ledger_text and len(ledger_text.splitlines()) > 1:
                break
            time.sleep(1)
        ledger_path = session.output / "oracle_action_ledger.csv"
        ledger_path.write_text(ledger_text, encoding="utf-8")
        ledger_rows = list(csv.DictReader(ledger_text.splitlines())) if ledger_text.strip() else []
        ledger_rows = [row for row in ledger_rows if row.get("event_id") == event_id]
        result["ledger"] = {"remote_path": ledger_remote, "local_path": "oracle_action_ledger.csv",
            "rows": ledger_rows, "observed": bool(ledger_rows),
            "classification": "observed" if ledger_rows else "unknown_oracle_ledger_timeout"}
        result["execution_complete"] = bool(len(target_results) == 6 and
            all(row.get("launch_status") in ("accepted", "failure", "unknown") for row in target_results.values()))
        result["measurement_completeness"] = {
            "environment_validated": True,
            "donors_verified_before_each_schedule_write": bool(result.get("donor_refreshes")) and
                all(all(donor.get("verified") for donor in refresh["donors"])
                    for refresh in result.get("donor_refreshes", [])),
            "target_launch_attempts_recorded": len(target_results) == 6,
            "target_journals_collected": all(row.get("journal_evidence_complete") or
                row.get("readiness_status") in ("confirmed_startup_failure", "unknown")
                for row in target_results.values()),
            "kernel_marker_window_complete": bool(kernel_info.get("coverage_complete")),
            "oracle_ledger_observed": bool(ledger_rows),
            "cleanup_complete": None,
        }
        result["measurement_complete"] = bool(result["execution_complete"] and
            result["measurement_completeness"]["donors_verified_before_each_schedule_write"] and
            result["measurement_completeness"]["target_journals_collected"] and
            result["measurement_completeness"]["kernel_marker_window_complete"] and
            result["measurement_completeness"]["oracle_ledger_observed"])
        result["formal_qualification_eligible"] = False
        result["baseline_is_new_independent_run"] = True
        session.result["scene4_cold_start"] = result
        session.result["execution_complete"] = result["execution_complete"]
        session.result["measurement_evidence_complete"] = result["measurement_complete"]
        session.result["case_results"] = [{"case_id": case_id, **result}]
    except (Exception, KeyboardInterrupt) as exc:
        result["error"] = str(exc)
        result.setdefault("classification", "unknown_collection_or_schedule_failure")
        result["execution_complete"] = False
        result["measurement_complete"] = False
        session.result["scene4_cold_start"] = result
        session.result["invalid_reasons"].append("scene4_cold_start:" + str(exc))
    finally:
        session.cleanup()
    return session.finish()


def scene4(session: Session) -> Path:
    """Run four fixed oracle-backed cold-start cases without changing scenes 1–3."""
    if session.scene != 4 or session.profile != "development" or session.phase not in ("baseline", "optimized"):
        raise ValueError("场景4仅支持baseline/optimized + development敏感性预验证")
    cfg = session.config["scene4"]
    all_bundles = list(session.config["test_apps"])
    donors = list(cfg.get("donor_bundles", all_bundles[:2]))
    targets = list(cfg.get("cold_start_targets", all_bundles[2:8]))
    if (len(all_bundles) != 8 or len(donors) != 2 or len(targets) != 6 or
            set(donors + targets) != set(all_bundles)):
        raise ValueError("scene4 requires exactly two self-owned donors and six cold-start targets")
    schedule_remote = cfg.get("oracle_schedule_remote_path")
    ledger_remote = cfg.get("oracle_ledger_remote_path")
    timing_v3_remote = str(ledger_remote or "") + ".timing_v3.csv"
    planned_needed_bytes = int(cfg.get("planned_needed_bytes", cfg.get("cold_start_planned_needed_bytes", 0)))
    if not schedule_remote or not ledger_remote or planned_needed_bytes <= 0:
        raise ValueError("scene4 oracle paths and positive planned_needed_bytes are required")
    seed = 20261005
    lifetime = int(cfg.get("lifetime_seconds", 1200))
    donor_mib = int(cfg.get("donor_memory_mib", cfg.get("donor_peak_mib", 3072)))
    target_peak_config = int(cfg.get("target_peak_mib", cfg.get("peak_mib", 0)))
    expected_planned_bytes = 6 * target_peak_config * 1024**2
    if planned_needed_bytes != expected_planned_bytes:
        raise ValueError(f"scene4 planned_needed_bytes must equal six target peaks ({expected_planned_bytes})")
    donor_file_mib = int(cfg.get("donor_file_mib", 0))
    # Donor heartbeats must start as soon as the 3072-MiB allocation is ready;
    # a long hold defers the native idle/background validation loop.
    donor_hold = 0
    donor_case_id = f"{session.id}_c0001"
    probe_mode = _scene4_probe_mode(cfg, session.phase)
    single_case_calibration = probe_mode["baseline_probe"]
    functional_calibration = probe_mode["functional_probe"]
    donor_proof_only = probe_mode["donor_proof_only"]
    single_case_probe = probe_mode["single_case_probe"]
    # This is an explicitly non-counting protocol/freshness calibration. The
    # default experiment always retains the four fixed cases below.
    single_case_kind = str(cfg.get("oracle_single_case_kind", "popup"))
    cases = _scene4_case_plan(single_case_probe, single_case_kind)
    result: dict = {"schema": "memscenes-scene4-oracle-cold-v2", "scope": "32gb_internal_mechanism_validation_only",
        "fixed_seed": seed, "formal_qualification_eligible": False,
        "fixed_workload": {"target_peak_mib": target_peak_config, "target_daily_mib": int(cfg.get("target_daily_mib", 0)),
            "target_initial_file_mib": int(cfg.get("target_file_mib", 0)),
            "baseline_file_mib_sequence": list(cfg.get("baseline_file_mib_sequence", [2048, 1536, 1024])),
            "baseline_fallback_policy": cfg.get("baseline_fallback_policy", "baseline-only; freeze first load covering all three primary classes"),
            "optimized_must_reuse_frozen_load": True, "donor_peak_mib": donor_mib},
        "primary_count_definition": "confirmed OOM kills + actual target startup failures + visible native low-memory popups",
        "case_plan": [], "cases": [], "donors": {}, "pair_comparison_authority": "not_evaluated_by_this_run",
        "single_case_calibration_only": single_case_calibration,
        "functional_calibration_only": functional_calibration,
        "measurement_complete": False, "unknown_is_not_zero": True}
    d = session.device
    start_uptime = start_wall = None
    # The donor-preparation phase can fail before the window monitor is
    # created. Keep the exception path safe so it preserves the original
    # evidence and failure instead of raising UnboundLocalError.
    monitor_stop = threading.Event()
    window_thread = None
    active_launch_cancel = None
    active_launch_timer = None
    active_schedule_writer_stop = None
    active_schedule_writer_thread = None
    active_ui_sampler_stop = None
    active_ui_sampler_thread = None
    active_reader_monitor = None
    popup_preparations_by_case: dict[str, dict] = {}
    try:
        result["environment"] = validate_environment(session)
        session.enable_oom_trace()
        result["clock_domain_initial"] = _scene4_clock_probe(session)
        session.start_kernel_dmesg_reader()
        for bundle in all_bundles:
            d.force_stop(bundle)

        # Start both owned donors in parallel and retain their private dirty load.
        donor_results: dict[str, dict] = {bundle: {"bundle": bundle, "status": "pending"} for bundle in donors}
        donor_daily = int(cfg.get("donor_daily_mib", donor_mib))
        donor_peak = int(cfg.get("donor_peak_mib", donor_mib))
        donor_params = {"case_id": donor_case_id, "daily_case_id": donor_case_id + "_daily",
            "daily_mib": donor_daily, "peak_mib": donor_peak, "file_mib": donor_file_mib,
            "hold_seconds": donor_hold, "lifetime_seconds": lifetime, "seed": seed,
            "await_peak": 0, "cold_start_ready": 1, "window_ready_gate": 1, "donor_mode": 1}
        # Clear an existing native warning before allocating donor memory. The
        # same fixed recovery wait is applied before each fresh donor set, so
        # donor preparation/background timing is treated identically in A/B.
        popup_preparations_by_case[donor_case_id] = session.restore_native_low_memory_dialog(
            donor_case_id + "_pre")
        popup_preparations_by_case[donor_case_id]["placement"] = "before_initial_donor_launch"
        popup_preparations_by_case[donor_case_id]["recovery_wait_seconds"] = float(
            cfg.get("popup_recovery_wait_seconds", 5.0))
        time.sleep(popup_preparations_by_case[donor_case_id]["recovery_wait_seconds"])
        donor_launch_errors: dict[str, str] = {}
        def launch_donor(bundle: str) -> None:
            try:
                donor_results[bundle]["launch_output"] = d.launch(bundle,
                    params=dict(donor_params, bundle=bundle), timeout=60)
                donor_results[bundle]["status"] = "accepted"
            except Exception as exc:
                donor_launch_errors[bundle] = str(exc)
                donor_results[bundle].update(status="unknown", launch_error=str(exc))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(launch_donor, donors))
        donor_ready_deadline = time.monotonic() + float(cfg.get("donor_ready_timeout_seconds", cfg.get("ready_timeout_seconds", 180)))
        donor_identities: dict[str, dict] = {}
        donor_ready_events: dict[str, dict] = {}
        donor_last_rows: dict[str, list[dict]] = {}
        while time.monotonic() < donor_ready_deadline and len(donor_identities) + len(donor_launch_errors) < 2:
            for bundle in donors:
                if bundle in donor_identities or bundle in donor_launch_errors:
                    continue
                try:
                    rows = _scene4_read_journal(session, bundle, donor_case_id)
                    donor_last_rows[bundle] = rows
                    ready = next((row for row in reversed(rows) if
                        _scene4_cold_ready(row, donor_peak * 1024**2, donor=True)), None)
                    if not ready:
                        continue
                    identity_proof = _scene4_donor_ready_identity(d, bundle, ready)
                    identity = identity_proof.get("identity")
                    if not identity_proof["verified"]:
                        donor_results[bundle].update(status="unknown",
                            identity_error=identity_proof["identity_error"],
                            identity_proof=identity_proof, ready_event=ready,
                            reason="donor_ready_identity_unverified")
                    else:
                        donor_identities[bundle] = identity
                        donor_ready_events[bundle] = ready
                        donor_results[bundle].update(status="ready", uid=identity_proof["installed_uid"],
                            pid=identity_proof["pid"], identity=identity, ready_event=ready,
                            identity_proof=identity_proof, memcg=identity_proof["memcg"],
                            ready_boottime_s=_scene4_event_time(ready))
                except Exception as exc:
                    donor_results[bundle].setdefault("evidence_errors", []).append(str(exc))
            if len(donor_identities) + len(donor_launch_errors) < 2:
                time.sleep(0.5)
        if len(donor_identities) != 2:
            result["donors"] = donor_results
            result["donor_setup_classification"] = "unknown_donor_cold_start_or_identity"
            raise RuntimeError("scene4 donor cold-ready identity was not verified")

        donor_transition_rows = [_scene4_minimize_donor(session, bundle, donor_case_id,
            donor_identities[bundle]) for bundle in donors]
        result["donor_background_transition"] = {"donors": donor_transition_rows}
        result["donor_background_transitions"] = [{"case_id": donor_case_id,
            "donors": donor_transition_rows}]
        background_anchor = _scene4_clock_anchor(session)
        session.mark("scene4_pair_begin", donors=donors, targets=targets,
                     clock_anchor=background_anchor,
                     device_clock=(background_anchor["device_boottime_seconds"],
                                   background_anchor["device_wall_ms"]))

        schedule_fields = _scene4_schedule_header()
        remote_parent = schedule_remote.rsplit("/", 1)[0] if "/" in schedule_remote else "."
        d.shell("mkdir -p " + quote(remote_parent), check=False)
        prior_schedule = d.shell("cat " + quote(schedule_remote), check=False)
        if prior_schedule.strip():
            previous_local = session.output / "oracle_schedule_previous.tsv"
            previous_local.write_text(prior_schedule.rstrip("\n") + "\n", encoding="utf-8")
            d.call("file", "send", str(previous_local), schedule_remote + ".previous", timeout=45)
        preserved_ledgers = [
            _scene4_preserve_remote_ledger(d, ledger_remote, session.id, session.output,
                "oracle_action_ledger_preexisting.csv"),
            _scene4_preserve_remote_ledger(d, timing_v3_remote, session.id, session.output,
                "oracle_action_timing_v3_preexisting.csv")]
        result["preserved_prior_oracle_ledgers"] = preserved_ledgers
        if any(not item.get("verified") for item in preserved_ledgers):
            raise RuntimeError("could not safely preserve prior oracle ledger before Scene4 run")

        # Keep a fresh, independent window/mission readback for the schedule gate.
        window_state: dict = {"updated_monotonic_ns": 0, "focused_bundles": [], "layout": None, "missions": ""}
        window_lock = threading.Lock()
        window_io_lock = threading.RLock()
        session.scene4_ui_lock = window_io_lock
        def refresh_window_snapshot() -> None:
            # aa running-record and mission snapshots are light-weight and avoid
            # repeatedly transferring multi-megabyte dumpLayout trees.
            with window_io_lock:
                layout = d.shell("aa dump -r", timeout=8, check=False)
                missions = d.shell("aa dump -l", timeout=8, check=False)
            with window_lock:
                window_state.update(updated_monotonic_ns=time.monotonic_ns(),
                    focused_bundles=sorted(_scene4_aa_foreground_bundles(layout)), layout=layout,
                    missions=missions, error=None)
        def window_monitor() -> None:
            while not monitor_stop.is_set():
                try:
                    refresh_window_snapshot()
                except Exception as exc:
                    with window_lock:
                        window_state.update(updated_monotonic_ns=time.monotonic_ns(), error=str(exc))
                monitor_stop.wait(1.0)
        # Schedule refresh performs its own paired AA/WMS read. A second
        # one-second AA poller only contends with that proof and UI capture;
        # the foreground evidence sampler below remains responsible for the
        # measured native-popup coverage.

        # A one-second writer continuously revalidates donor identity, current
        # retained anonymous memory, HAP lifecycle, and the latest window state.
        schedule_stop = threading.Event()
        schedule_errors: list[str] = []
        donor_refresh_rows: list[dict] = []
        schedule_local = session.output / "oracle_schedule.tsv"
        schedule_proof_log = session.output / "oracle_schedule_proofs.jsonl"
        launch_dispatch_log = session.output / "launch_dispatch_timing.jsonl"
        launch_dispatch_log_lock = threading.Lock()
        last_schedule_write = 0.0
        case_runtime: dict = {}
        def launch_case(case_index: int, case_id: str, target_order: list[str],
                        event_id: str, launch_host_ns: int, launch_boottime: float,
                        target_daily: int, target_peak: int, target_file_mib: int,
                        target_hold: int, target_presence_invalid: threading.Event,
                        target_absence_state: dict) -> tuple[dict[str, dict], threading.Thread, threading.Event]:
            cancel_before_dispatch = threading.Event()
            launch_status: dict[str, dict] = {bundle: {"bundle": bundle, "launch_status": "pending",
                "readiness_status": "pending", "scheduled_host_monotonic_ns": launch_host_ns,
                "actual_host_monotonic_ns": None} for bundle in target_order}
            def timed_launches() -> None:
                remaining = (launch_host_ns - time.monotonic_ns()) / 1e9
                if remaining > 0 and cancel_before_dispatch.wait(remaining):
                    with launch_dispatch_log_lock:
                        _scene4_append_durable_jsonl(launch_dispatch_log, {"event_id": event_id,
                            "case_id": case_id, "state": "timer_cancelled_before_dispatch",
                            "scheduled_host_monotonic_ns": launch_host_ns,
                            "scheduled_boottime_s": launch_boottime,
                            "cancelled_host_monotonic_ns": time.monotonic_ns()})
                    return
                if cancel_before_dispatch.is_set():
                    return
                last_absence_check = int(target_absence_state.get("last_check_host_ns", 0) or 0)
                absence_age = ((time.monotonic_ns() - last_absence_check) / 1e9
                               if last_absence_check else None)
                target_absence_state["age_at_launch_seconds"] = absence_age
                target_absence_state["launch_gate_passed"] = bool(
                    not target_presence_invalid.is_set() and target_absence_state.get("complete") and
                    target_absence_state.get("absent") is True and absence_age is not None and absence_age <= 2.5)
                if target_presence_invalid.is_set() or not target_absence_state.get("complete") or \
                        target_absence_state.get("absent") is not True or absence_age is None or absence_age > 2.5:
                    reason = ("target_process_present_or_inventory_unknown" if target_presence_invalid.is_set() or
                              not target_absence_state.get("complete") or target_absence_state.get("absent") is not True
                              else "target_absence_snapshot_stale_before_T")
                    for row in launch_status.values():
                        row.update(launch_status="unknown", reason=reason)
                    _scene4_append_durable_jsonl(launch_dispatch_log, {"event_id": event_id,
                        "case_id": case_id, "state": "suppressed_prelaunch_target_absence_gate",
                        "scheduled_host_monotonic_ns": launch_host_ns,
                        "scheduled_boottime_s": launch_boottime,
                        "absence_snapshot_age_seconds": absence_age, "reason": reason})
                    return
                monitor_state = session.scene4_reader_monitors.get(case_id)
                if monitor_state and monitor_state["failed"].is_set():
                    for row in launch_status.values():
                        row.update(launch_status="unknown", reason="kernel_reader_died_before_launch")
                    _scene4_append_durable_jsonl(launch_dispatch_log, {"event_id": event_id,
                        "case_id": case_id, "state": "suppressed_reader_dead",
                        "scheduled_host_monotonic_ns": launch_host_ns,
                        "scheduled_boottime_s": launch_boottime})
                    return
                timer_fired_host_ns = time.monotonic_ns()
                def launch_one(bundle: str) -> None:
                    row = launch_status[bundle]
                    if cancel_before_dispatch.is_set():
                        row.update(launch_status="unknown", reason="cancelled_before_ability_request")
                        return
                    row["actual_host_monotonic_ns"] = time.monotonic_ns()
                    case_params = {"case_id": case_id, "daily_case_id": case_id + "_daily",
                        "daily_mib": target_daily, "peak_mib": target_peak,
                        "file_mib": target_file_mib, "hold_seconds": target_hold,
                        "lifetime_seconds": lifetime, "seed": seed, "bundle": bundle,
                        "await_peak": 0, "cold_start_ready": 1, "window_ready_gate": 1}
                    if target_file_mib > 0:
                        session.owned_page_files.add((bundle, case_id))
                    call_started_ns = time.monotonic_ns()
                    try:
                        row["launch_output"] = d.launch(bundle, params=case_params, timeout=60)
                        row["launch_status"] = "accepted"
                    except Exception as exc:
                        message = str(exc)
                        row["launch_error"] = message
                        if re.search(r"(?i)(ability.{0,40}(create|start).{0,30}(reject|fail)|(?:failed|error|rejected).{0,40}(?:ability|start ability|create ability)|invalid ability)", message):
                            row.update(launch_status="failure", reason="aa_explicit_ability_rejection")
                        else:
                            row.update(launch_status="unknown", reason="launch_transport_or_timeout_unknown")
                    finally:
                        row["request_call_started_host_monotonic_ns"] = call_started_ns
                        row["request_call_completed_host_monotonic_ns"] = time.monotonic_ns()
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                    list(pool.map(launch_one, target_order))
                with launch_dispatch_log_lock:
                    _scene4_append_durable_jsonl(launch_dispatch_log, {
                        "event_id": event_id, "case_id": case_id,
                        "scheduled_host_monotonic_ns": launch_host_ns,
                        "scheduled_boottime_s": launch_boottime,
                        "timer_fired_host_monotonic_ns": timer_fired_host_ns,
                        "timer_fired_lateness_ns": timer_fired_host_ns - launch_host_ns,
                        "targets": [{"bundle": bundle,
                            "request_call_started_host_monotonic_ns": launch_status[bundle].get("request_call_started_host_monotonic_ns"),
                            "request_call_completed_host_monotonic_ns": launch_status[bundle].get("request_call_completed_host_monotonic_ns"),
                            "launch_status": launch_status[bundle].get("launch_status"),
                            "reason": launch_status[bundle].get("reason")}
                            for bundle in target_order]})
            timer = threading.Thread(target=timed_launches, daemon=True)
            _scene4_append_durable_jsonl(launch_dispatch_log, {"event_id": event_id,
                "case_id": case_id, "state": "timer_armed",
                "scheduled_host_monotonic_ns": launch_host_ns,
                "scheduled_boottime_s": launch_boottime, "targets": target_order})
            timer.start()
            return launch_status, timer, cancel_before_dispatch

        pair_plan = []
        for case_number, (kind, rotation, profile_key) in enumerate(cases, 1):
            if result.get("reader_health_failures"):
                raise RuntimeError("Scene4 kernel reader died; subsequent cases are suppressed")
            case_id = f"{session.id}_c{case_number:04d}"
            if not session.ensure_kernel_dmesg_reader(case_id):
                raise RuntimeError(f"Scene4 kernel reader unavailable before {case_id}")
            control_begin = _scene4_set_oracle_control(session, schedule_remote)
            if case_number > 1:
                # Fresh donors isolate compression/reclaim/fault history by case.
                for owned_bundle in all_bundles:
                    d.force_stop(owned_bundle)
                for owned_bundle in all_bundles:
                    if _scene4_memcg_snapshot(d, owned_bundle)["processes"]:
                        raise RuntimeError(f"owned cgroup is not empty before {case_id}: {owned_bundle}")
                # Keep restoration and its fixed wait ahead of the next donor
                # allocation, rather than spending that time after the donors
                # have entered background.
                popup_preparations_by_case[case_id] = session.restore_native_low_memory_dialog(
                    case_id + "_pre")
                popup_preparations_by_case[case_id]["placement"] = "before_fresh_donor_launch"
                popup_preparations_by_case[case_id]["recovery_wait_seconds"] = float(
                    cfg.get("popup_recovery_wait_seconds", 5.0))
                time.sleep(popup_preparations_by_case[case_id]["recovery_wait_seconds"])
                donor_case_id = case_id + "_donor"
                donor_identities.clear(); donor_ready_events.clear(); donor_last_rows.clear()
                donor_launch_errors.clear(); donor_results.clear()
                donor_results.update({bundle: {"bundle": bundle, "status": "pending"} for bundle in donors})
                def restart_donor(bundle: str) -> None:
                    try:
                        donor_results[bundle]["launch_output"] = d.launch(bundle,
                            params=dict(donor_params, bundle=bundle, case_id=donor_case_id,
                                        daily_case_id=donor_case_id + "_daily"), timeout=60)
                        donor_results[bundle]["status"] = "accepted"
                    except Exception as exc:
                        donor_launch_errors[bundle] = str(exc)
                        donor_results[bundle].update(status="unknown", launch_error=str(exc))
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(restart_donor, donors))
                donor_ready_deadline = time.monotonic() + float(cfg.get("donor_ready_timeout_seconds", cfg.get("ready_timeout_seconds", 180)))
                while time.monotonic() < donor_ready_deadline and len(donor_identities) + len(donor_launch_errors) < 2:
                    for donor_bundle in donors:
                        if donor_bundle in donor_identities or donor_bundle in donor_launch_errors:
                            continue
                        rows = _scene4_read_journal(session, donor_bundle, donor_case_id)
                        donor_last_rows[donor_bundle] = rows
                        ready = next((item for item in reversed(rows) if
                            _scene4_cold_ready(item, donor_peak * 1024**2, donor=True)), None)
                        if not ready:
                            continue
                        pid, uid = int(ready.get("pid", -1)), int(ready.get("uid", -1))
                        installed_uid = int(d.app(donor_bundle)["uid"])
                        ready_ticks = str(ready.get("start_ticks", ""))
                        observed = d.inspect_process_identity(pid, timeout=8) if pid > 0 else {}
                        identity = observed.get("identity")
                        mismatch = proc_identity_mismatch(identity, pid, installed_uid, ready_ticks) if pid > 0 else "pid_missing"
                        memcg = _scene4_memcg_snapshot(d, donor_bundle)
                        live = memcg["processes"].get(f"{pid}:{ready_ticks}")
                        if uid == installed_uid and mismatch is None and live and live.get("verified"):
                            donor_identities[donor_bundle] = identity
                            donor_ready_events[donor_bundle] = ready
                            donor_results[donor_bundle].update(status="ready", uid=uid, pid=pid,
                                identity=identity, ready_event=ready, memcg=memcg,
                                ready_boottime_s=_scene4_event_time(ready))
                    if len(donor_identities) + len(donor_launch_errors) < 2:
                        time.sleep(0.5)
                if len(donor_identities) != 2:
                    raise RuntimeError(f"fresh donor readiness incomplete for {case_id}")
                transition_rows = [_scene4_minimize_donor(session, bundle, donor_case_id,
                    donor_identities[bundle], ui_lock=window_io_lock) for bundle in donors]
                refresh_window_snapshot()
                result.setdefault("donor_background_transitions", []).append({"case_id": case_id,
                    "donors": transition_rows})
            profile = cfg["case_profiles"][profile_key]
            target_order = list(targets)
            if rotation:
                target_order = target_order[1:] + target_order[:1]
            hold = int(profile["hold_seconds"])
            target_peak = int(profile.get("peak_mib", cfg.get("peak_mib", 0)))
            target_file = int(cfg.get("target_file_mib", profile.get("file_mib", cfg.get("file_mib", 0))))
            target_daily = int(cfg.get("target_daily_mib", cfg.get("daily_mib", 0)))
            if min(target_daily, target_peak) <= 0:
                raise ValueError("scene4 cold-start daily/peak settings must be positive")
            popup_preparation = popup_preparations_by_case.get(case_id, {
                "case_id": case_id, "status": "missing", "verified_absent": False,
                "placement": "unknown"})
            event_id = f"{case_id}_oracle"
            case_runtime = {"process_history": {}, "daemon_cpu_state": {}}
            sample_dir = session.output / "system_samples"
            sample_dir.mkdir(parents=True, exist_ok=True)
            sample_path = sample_dir / f"{case_id}.jsonl"
            sample_path.write_text("", encoding="utf-8")
            case_runtime["sample_path"] = str(sample_path)
            case_runtime["sample_persist_errors"] = []

            def persist_system_sample(sample: dict, destination: list[dict]) -> None:
                destination.append(sample)
                try:
                    _scene4_append_jsonl(sample_path, sample)
                except Exception as exc:
                    case_runtime["sample_persist_errors"].append(str(exc))

            def update_process_history(bundle: str, snapshot: dict, interval: dict) -> None:
                if snapshot.get("unknown") or not interval.get("valid"):
                    return
                observed_start = float(interval["boottime_start_s"])
                observed_end = float(interval["boottime_end_s"])
                present_keys = set(snapshot.get("processes", {}))
                for key, process in snapshot.get("processes", {}).items():
                    pid, ticks = int(process["pid"]), str(process["start_ticks"])
                    history = case_runtime.setdefault("process_history", {}).setdefault(pid, [])
                    entry = next((item for item in history if item["bundle"] == bundle and
                                  item["start_ticks"] == ticks), None)
                    if entry is None:
                        history.append({"bundle": bundle, "uid": process["uid"],
                            "start_ticks": ticks, "identity_verified": process.get("verified", False),
                            "first_uptime": observed_start, "first_absent_uptime": None,
                            "last_seen_uptime": observed_end,
                            "last_seen_interval_start_s": observed_start,
                            "last_seen_interval_end_s": observed_end})
                    else:
                        entry["last_seen_uptime"] = observed_end
                        entry["last_seen_interval_start_s"] = observed_start
                        entry["last_seen_interval_end_s"] = observed_end
                for pid, histories in case_runtime.get("process_history", {}).items():
                    for entry in histories:
                        key = f"{pid}:{entry['start_ticks']}"
                        if (entry["bundle"] == bundle and entry.get("first_absent_uptime") is None and
                                key not in present_keys):
                            entry["first_absent_uptime"] = observed_end
                            entry["first_absent_interval_start_s"] = observed_start
                            entry["first_absent_interval_end_s"] = observed_end

            launch_boottime = 0.0
            launch_host_ns = 0
            anchor = None
            target_absence_checks: list[dict] = []
            target_absence_log = session.output / "target_process_absence.jsonl"
            target_absence_state = {"complete": False, "absent": False, "last_check_host_ns": 0}
            target_presence_invalid = threading.Event()
            def refresh_case_schedule(require_targets_absent: bool = True,
                                      initial_registration: bool = False) -> None:
                nonlocal launch_boottime, launch_host_ns, anchor
                # Check target absence before the final donor observations so no
                # intervening HDC round trip can stale the proof before write.
                if require_targets_absent:
                    check_start_ns = time.monotonic_ns()
                    raw_inventory = d.shell("ps -A -o PID,ARGS; echo __S4_TARGET_PS_EXIT__=$?",
                                            timeout=5, check=False)
                    check_end_ns = time.monotonic_ns()
                    inventory = _scene4_parse_target_process_inventory(raw_inventory, targets)
                    before_fixed_T = launch_host_ns == 0 or check_end_ns < launch_host_ns
                    absent = bool(inventory.get("complete") and all(
                        not inventory["processes"].get(bundle) for bundle in targets))
                    absence_row = {"case_id": case_id, "host_start_monotonic_ns": check_start_ns,
                        "host_end_monotonic_ns": check_end_ns, "elapsed_seconds": (check_end_ns-check_start_ns)/1e9,
                        "complete": inventory.get("complete") is True, "absent": absent,
                        "prelaunch_snapshot": before_fixed_T,
                        "crossed_fixed_T": launch_host_ns > 0 and check_start_ns < launch_host_ns <= check_end_ns,
                        "processes": inventory.get("processes"), "reason": inventory.get("reason"),
                        "raw": inventory.get("raw")}
                    target_absence_checks.append(absence_row)
                    _scene4_append_durable_jsonl(target_absence_log, absence_row)
                    if before_fixed_T:
                        target_absence_state.update(complete=inventory.get("complete") is True,
                            absent=absent, last_check_host_ns=check_end_ns)
                    if before_fixed_T and not absent:
                        target_presence_invalid.set()
                        present = [bundle for bundle in targets if inventory.get("processes", {}).get(bundle)]
                        raise RuntimeError("cold target process prelaunch gate failed: " +
                            ("inventory_unknown" if not inventory.get("complete") else ",".join(present)))
                with window_lock:
                    focused = set(window_state.get("focused_bundles", []))
                    window_error = window_state.get("error")
                    window_age = (time.monotonic_ns() - window_state.get("updated_monotonic_ns", 0)) / 1e9
                window_gate_valid = not window_error and window_age <= 2.5
                refresh_rows = []
                donor_static_fields = {}
                donor_dynamic_fields = {}
                donor_os_inputs = {}
                window_observations = {}
                window_brackets = {}
                # Use the lightweight WMS table + PID-targeted detail for schedule
                # freshness. The larger dumpLayout tree remains dedicated to UI
                # evidence sampling and is never treated as a continuous monitor.
                # Start the independent AA/WMS and identity/lifecycle reads from
                # one conservative device BOOTTIME anchor. Parallel reads keep
                # every observation inside the existing 2.5s freshness bound.
                fresh_sample_start = _scene4_device_boottime_s(session)
                sample_host_start_ns = time.monotonic_ns()
                # Use the same conservative batch-start timestamp for every
                # freshness check. A second clock probe here adds latency but
                # cannot make the independently-read evidence newer.
                current_anchor = {"device_uptime_seconds": fresh_sample_start,
                    "device_boottime_seconds": fresh_sample_start,
                    "host_monotonic_midpoint_ns": sample_host_start_ns,
                    "sample_kind": "freshness_batch_start"}
                def read_window_evidence():
                    table = d.shell("hidumper -s WindowManagerService -a '-a'", timeout=8, check=False)
                    snapshots, details = {}, {}
                    selected_rows = {}
                    for window_bundle in donors:
                        pid = int(donor_identities[window_bundle]["pid"])
                        listed = _scene4_wms_window_snapshot(table, "", pid)
                        selected_rows[window_bundle] = listed.get("list_row")
                    detail_rows = [(bundle, row) for bundle, row in selected_rows.items() if row]
                    if detail_rows:
                        commands = []
                        for index, (_, row) in enumerate(detail_rows):
                            win_id = str(row.get("win_id", ""))
                            if not re.fullmatch(r"\d+", win_id):
                                raise RuntimeError(f"invalid WMS WinId for donor detail: {win_id!r}")
                            commands.append(f"echo __SCENE4_WMS_DETAIL_{index}__; "
                                f"hidumper -s WindowManagerService -a '-w {win_id}'")
                        detail_raw = d.shell("; ".join(commands), timeout=12, check=False)
                        sections = re.split(r"(?m)^__SCENE4_WMS_DETAIL_\d+__\s*$", detail_raw)
                        marker_indexes = [int(value) for value in re.findall(
                            r"(?m)^__SCENE4_WMS_DETAIL_(\d+)__\s*$", detail_raw)]
                        if len(marker_indexes) != len(detail_rows) or len(sections) < len(detail_rows) + 1:
                            raise RuntimeError("batched WMS detail output missing a marker")
                        for section_index, (bundle, _) in enumerate(detail_rows):
                            details[bundle] = sections[section_index + 1].strip()
                    for window_bundle in donors:
                        pid = int(donor_identities[window_bundle]["pid"])
                        detail = details.get(window_bundle, "")
                        snapshots[window_bundle] = _scene4_wms_window_snapshot(table, detail, pid)
                    return table, details, snapshots
                def read_donor_evidence(bundle):
                    installed_uid = int(d.app(bundle)["uid"])
                    proof = _scene4_donor_evidence(session, bundle, donor_case_id,
                        donor_identities[bundle], installed_uid, current_anchor["device_uptime_seconds"],
                        include_smaps=False)
                    memcg = proof["cgroup_snapshot"]
                    resident = _scene4_resident_anon_bytes(d, memcg)
                    return bundle, installed_uid, proof, memcg, resident
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    wms_future = pool.submit(read_window_evidence)
                    aa_future = pool.submit(d.shell, "aa dump -r", timeout=8, check=False)
                    donor_futures = [pool.submit(read_donor_evidence, bundle) for bundle in donors]
                    wms_table, window_details, window_snapshots = wms_future.result()
                    aa_records = aa_future.result()
                    with window_lock:
                        window_state.update(updated_monotonic_ns=time.monotonic_ns(),
                            focused_bundles=sorted(_scene4_aa_foreground_bundles(aa_records)),
                            layout=None, missions=aa_records, error=None)
                    focused = set(window_state["focused_bundles"])
                    window_age = 0.0
                    for future in donor_futures:
                        bundle, installed_uid, proof, memcg, resident_anon = future.result()
                        proc_observed_time = fresh_sample_start
                        donor_key = f"{donor_identities[bundle]['pid']}:{donor_identities[bundle]['start_ticks']}"
                        live = memcg["processes"].get(donor_key)
                        required_keep = int(cfg.get("donor_min_resident_anon_mib", 512)) * 1024**2
                        proof.update(memcg=memcg, resident_anon_bytes=resident_anon,
                            required_resident_anon_bytes=required_keep,
                            memory_current_bytes=memcg.get("memory_current_bytes", 0),
                            window_focused=bundle in focused, window_state_age_seconds=window_age,
                            ability_state_raw=aa_records,
                            proc_cmdline=live.get("cmdline") if live else None,
                            exact_cgroup_leaf=bool(live and live.get("exact_cgroup_leaf")),
                            base_schedule_valid=bool(len(memcg.get("processes", {})) == 1 and
                                live and live.get("verified") and
                                resident_anon >= required_keep and memcg.get("memory_current_bytes", 0) >= required_keep and
                                all(process.get("verified") for process in memcg["processes"].values())))
                        cold = proof["cold_ready"]
                        donor_static_fields[bundle] = [bundle, str(installed_uid), str(donor_identities[bundle]["pid"]),
                            str(donor_identities[bundle]["start_ticks"]),
                            "true" if cold else "false",
                            "true" if cold and cold.get("window_ready") is True else "false",
                            f"{proof['cold_ready_boottime_s']:.6f}" if proof["cold_ready_boottime_s"] else "0",
                            f"{proof['background_since_s']:.6f}" if proof["background_since_s"] else "0",
                            f"{proof['last_heartbeat_s']:.6f}" if proof["last_heartbeat_s"] else "0"]
                        donor_os_inputs[bundle] = (proof, memcg, proc_observed_time, installed_uid)
                        refresh_rows.append(proof)
                sample_host_end_ns = time.monotonic_ns()
                window_list_pre = window_list_post = aa_observed_time = aa_post = fresh_sample_start
                for bundle in donors:
                    window_observations[bundle] = fresh_sample_start
                    window_brackets[bundle] = {"sample_start_boottime_s": fresh_sample_start,
                        "sample_start_host_monotonic_ns": sample_host_start_ns,
                        "sample_end_host_monotonic_ns": sample_host_end_ns}
                dynamic_clock_host_start_ns = time.monotonic_ns()
                dynamic_now = _scene4_device_boottime_s(session)
                dynamic_clock_host_end_ns = time.monotonic_ns()
                dynamic_clock_host_midpoint_ns = (dynamic_clock_host_start_ns + dynamic_clock_host_end_ns) // 2
                for bundle in donors:
                    proof, memcg, proc_observed_time, installed_uid = donor_os_inputs[bundle]
                    dynamic_fields, dynamic_proof = _scene4_dynamic_donor_fields(
                        bundle, donor_identities[bundle], installed_uid, dynamic_now,
                        aa_records, aa_observed_time,
                        {"wms_window_snapshot": window_snapshots[bundle]}, window_observations[bundle],
                        memcg, proc_observed_time, proof, proc_observed_time)
                    cold = proof["cold_ready"]
                    dynamic_proof["query_brackets_boottime_s"] = {
                        "concurrent_snapshot_start": fresh_sample_start,
                        "concurrent_snapshot_host_start_ns": sample_host_start_ns,
                        "concurrent_snapshot_host_end_ns": sample_host_end_ns,
                        "submission_preflight": dynamic_now}
                    dynamic_proof["submission_age_seconds"] = {
                        "window": dynamic_now - window_observations[bundle]
                            if window_observations[bundle] else None,
                        "aa": dynamic_now - aa_observed_time,
                        "proc": dynamic_now - proc_observed_time}
                    dynamic_proof["submission_host_monotonic_midpoint_ns"] = dynamic_clock_host_midpoint_ns
                    dynamic_proof["window_raw"] = {"wms_table": wms_table,
                        "wms_target_detail": window_details[bundle]}
                    proof.update(dynamic_os_evidence=dynamic_proof,
                        schedule_valid=bool(proof["base_schedule_valid"] and dynamic_proof["valid"]))
                    if _scene4_qualifier_failure_is_predeadline(
                            proof["schedule_valid"], initial_registration,
                            dynamic_now, launch_boottime):
                        schedule_errors.append(f"{event_id}: donor dynamic OS gate invalid: {bundle}")
                    static = donor_static_fields[bundle]
                    static[4] = "true" if cold and proof["schedule_valid"] else "false"
                    static[5] = "true" if cold and cold.get("window_ready") is True and proof["schedule_valid"] else "false"
                    donor_dynamic_fields[bundle] = dynamic_fields
                if not initial_registration and dynamic_now >= launch_boottime:
                    # Schedule eligibility is a pre-launch gate. A proof read
                    # begun before T may finish after T, but it must never
                    # publish a new oracle row after the event deadline.
                    _scene4_append_durable_jsonl(schedule_proof_log, {
                        "event_id": event_id, "case_id": case_id,
                        "schedule_submission_status": "skipped_after_launch_deadline",
                        "device_boottime_at_gate": dynamic_now,
                        "launch_boottime_s": launch_boottime,
                        "donors": refresh_rows})
                    return
                if initial_registration:
                    # Freeze T only after every donor's fresh WMS/AA/proc/lifecycle
                    # proof has completed; this prevents a slow proof from eating
                    # the lead before the event is even registered.
                    anchor = _scene4_clock_anchor(session)
                    launch_boottime = anchor["device_boottime_seconds"] + 30.0
                    launch_host_ns = anchor["host_monotonic_midpoint_ns"] + 30_000_000_000
                    current_anchor = anchor
                    final_now = anchor["device_boottime_seconds"]
                    for bundle in donors:
                        proof = next(row for row in refresh_rows if row["bundle"] == bundle)
                        dynamic_proof = proof["dynamic_os_evidence"]
                        observations = dynamic_proof["observed_boottime_s"]
                        fresh_at_submit = all(isinstance(stamp, (int, float)) and
                            0 <= final_now - stamp <= 2.5 for key, stamp in observations.items()
                            if key != "now")
                        proof["schedule_valid"] = bool(proof["base_schedule_valid"] and
                            dynamic_proof["valid"] and fresh_at_submit)
                        dynamic_proof["schedule_submit_age_seconds"] = {
                            key: final_now - stamp for key, stamp in observations.items() if key != "now"}
                        dynamic_proof["fresh_at_schedule_submit"] = fresh_at_submit
                        if not proof["schedule_valid"]:
                            schedule_errors.append(f"{event_id}: donor proof stale at initial schedule submit: {bundle}")
                    for bundle, static in donor_static_fields.items():
                        proof = next(row for row in refresh_rows if row["bundle"] == bundle)
                        cold = proof["cold_ready"]
                        static[4] = "true" if cold and proof["schedule_valid"] else "false"
                        static[5] = "true" if cold and cold.get("window_ready") is True and proof["schedule_valid"] else "false"
                values = [event_id, case_id, f"{launch_boottime:.6f}", str(planned_needed_bytes)]
                for bundle in donors:
                    values.extend(donor_static_fields[bundle])
                    values.extend(donor_dynamic_fields[bundle])
                if len(schedule_fields) != 48 or len(values) != 48:
                    raise RuntimeError(f"scene4 oracle schedule schema mismatch: header={len(schedule_fields)} row={len(values)}")
                write_started_host_ns = time.monotonic_ns()
                with schedule_local.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.writer(stream, delimiter="|", lineterminator="\n")
                    writer.writerow(schedule_fields); writer.writerow(values)
                payload = schedule_local.read_text(encoding="utf-8")
                _scene4_atomic_schedule_write(session, schedule_local, schedule_remote, payload)
                write_completed_host_ns = time.monotonic_ns()
                write_duration_s = (write_completed_host_ns - write_started_host_ns) / 1e9
                proof_write_ages = {}
                for proof in refresh_rows:
                    dynamic_proof = proof.get("dynamic_os_evidence", {})
                    preflight_ages = (dynamic_proof.get("schedule_submit_age_seconds")
                                      if initial_registration else dynamic_proof.get("submission_age_seconds", {}))
                    preflight_host_ns = (anchor["host_monotonic_midpoint_ns"] if initial_registration
                                         else dynamic_proof.get("submission_host_monotonic_midpoint_ns"))
                    elapsed_since_preflight_s = ((write_completed_host_ns - preflight_host_ns) / 1e9
                        if isinstance(preflight_host_ns, int) else None)
                    estimated_ages = ({key: value + elapsed_since_preflight_s
                        for key, value in preflight_ages.items()
                        if key != "now" and isinstance(value, (int, float))}
                        if elapsed_since_preflight_s is not None else {})
                    proof_write_ages[proof["bundle"]] = estimated_ages
                    dynamic_proof["schedule_write_timing"] = {
                        "write_started_host_monotonic_ns": write_started_host_ns,
                        "write_completed_host_monotonic_ns": write_completed_host_ns,
                        "write_duration_seconds": write_duration_s,
                        "preflight_to_write_complete_seconds": elapsed_since_preflight_s,
                        "estimated_evidence_age_at_write_seconds": estimated_ages,
                        "estimated_fresh_at_write_le_2_5s": bool(estimated_ages) and
                            all(age <= 2.5 for age in estimated_ages.values())}
                refresh_record = {"event_id": event_id,
                    "write_started_host_monotonic_ns": write_started_host_ns,
                    "write_host_monotonic_ns": write_completed_host_ns,
                    "write_duration_seconds": write_duration_s,
                    "proof_estimated_ages_at_write_seconds": proof_write_ages,
                    "donors": refresh_rows,
                    "window_focused_bundles": sorted(focused), "window_state_age_seconds": window_age,
                    "clock_anchor": current_anchor,
                    "schedule_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()}
                donor_refresh_rows.append(refresh_record)
                _scene4_append_durable_jsonl(schedule_proof_log, refresh_record)

            session.owned_page_files.update((bundle, case_id) for bundle in targets)
            # Capture boundaries and popup baseline before freezing T. The 30s
            # launch lead begins only after this preparation is complete.
            # Complete the pre-event mission baseline before the counter
            # boundary, so baseline I/O cannot consume the first UI coverage gap.
            popup_baseline_raw = d.shell("aa dump -l", timeout=10, check=False)
            popup_missions = parse_low_memory_missions(popup_baseline_raw)
            popup_baseline_wms_raw = d.shell("hidumper -s WindowManagerService -a '-a'", timeout=8, check=False)
            popup_baseline_wms = _scene4_wms_window_inventory(popup_baseline_wms_raw)
            popup_baseline_process_raw = d.shell("aa dump -r", timeout=8, check=False)
            popup_baseline_processes = _scene4_aa_process_inventory(popup_baseline_process_raw)
            popup_baseline_visible_window_ids = []
            for wms_row in popup_baseline_wms.get("rows", []):
                pid = wms_row.get("pid")
                if pid is None or not any(proc.get("bundle") == "com.huawei.hmos.taskmanager" and
                                          proc.get("pid") == pid
                                          for proc in popup_baseline_processes.get("rows", [])):
                    continue
                baseline_detail = d.shell("hidumper -s WindowManagerService -a " +
                    quote(f"-w {wms_row['win_id']}"), timeout=5, check=False)
                parsed_baseline_detail = _scene4_wms_window_snapshot(popup_baseline_wms_raw,
                    baseline_detail, pid, wms_row["win_id"])
                if parsed_baseline_detail.get("known") and parsed_baseline_detail.get("visible") is True:
                    popup_baseline_visible_window_ids.append(int(wms_row["win_id"]))
            case_begin_marker = session.mark_scene4_kernel_dmesg_boundary(case_id, "BEGIN")
            case_begin_clock_anchor = _scene4_clock_anchor(session)
            case_begin_uptime = case_begin_marker["boottime_seconds"]
            case_begin_wall = case_begin_marker["realtime_ms"]
            case_begin_host_monotonic = time.monotonic()
            active_reader_monitor = session.start_scene4_reader_health_monitor(case_id, result)
            if not active_reader_monitor["verified_alive_at_begin"] or active_reader_monitor["failed"].is_set():
                raise RuntimeError(f"Scene4 kernel reader was not live at BEGIN for {case_id}")
            session.popup_baselines[case_id] = {
                "mission_ids": sorted(item["mission_id"] for item in popup_missions),
                "missions": popup_missions, "raw": popup_baseline_raw,
                "window_ids": sorted(set(popup_baseline_visible_window_ids)),
                "window_inventory_complete": bool(popup_baseline_wms.get("complete") and
                    popup_baseline_processes.get("complete")),
                "window_inventory_raw": popup_baseline_wms_raw,
                "case_begin_wall_ms": case_begin_wall,
                "case_begin_uptime_seconds": case_begin_uptime,
                "case_begin_boottime_seconds": case_begin_uptime}
            # Start the independent capture worker immediately after the case
            # boundary and baseline are durable. Initial donor refresh can be
            # slow; it must not leave the early counter window unobserved.
            # The donor-proof-only path has no counter window and never starts it.
            frame_capture_state = None if donor_proof_only else session.start_scene4_frame_capture(
                case_id, case_begin_uptime,
                30.0 + float(cfg.get("cold_start_ready_timeout_seconds", 180)) + float(hold) + 5.0)
            # The initial refresh completes all fresh donor/WMS evidence before
            # it freezes T, writes the schedule, and returns for immediate timer arm.
            refresh_case_schedule(initial_registration=True)
            if active_reader_monitor["failed"].is_set():
                raise RuntimeError(f"Scene4 kernel reader died during initial registration for {case_id}")
            if donor_proof_only:
                # No target Ability requests and no reclaim actions: this probe
                # only measures whether fresh donor proof can be submitted.
                refresh_count = max(1, int(cfg.get("donor_proof_only_refresh_count", 5)))
                refresh_interval = max(1.0, float(cfg.get("donor_proof_only_refresh_interval_seconds", 1.0)))
                for refresh_index in range(refresh_count):
                    refresh_case_schedule(require_targets_absent=False, initial_registration=False)
                result["donor_proof_only_probe"] = {
                    "status": "completed_no_targets_no_reclaim",
                    "refresh_count": refresh_count + 1,
                    "requested_refresh_interval_seconds": refresh_interval,
                    "schedule_proof_log": str(schedule_proof_log),
                    "actual_write_to_write_intervals_seconds": [
                        (right["write_host_monotonic_ns"] - left["write_host_monotonic_ns"]) / 1e9
                        for left, right in zip(donor_refresh_rows, donor_refresh_rows[1:])],
                    "control_begin": control_begin,
                    "measurement_complete": False,
                    "counts": None}
                for owned_bundle in donors:
                    d.force_stop(owned_bundle)
                cleanup_deadline = time.monotonic() + 15.0
                cleanup_rows = {bundle: _scene4_memcg_snapshot(d, bundle) for bundle in donors}
                while time.monotonic() < cleanup_deadline and any(
                        row.get("processes") for row in cleanup_rows.values()):
                    time.sleep(0.5)
                    cleanup_rows = {bundle: _scene4_memcg_snapshot(d, bundle) for bundle in donors}
                result["donor_proof_only_probe"]["donor_cleanup"] = {
                    bundle: cleanup_rows[bundle] for bundle in donors}
                result["donor_proof_only_probe"]["donor_cleanup_complete"] = all(
                    not row.get("processes") for row in result["donor_proof_only_probe"]["donor_cleanup"].values())
                break
            schedule_writer_stop = threading.Event()
            active_schedule_writer_stop = schedule_writer_stop
            def schedule_writer_loop():
                next_due = time.monotonic()
                while not schedule_writer_stop.is_set():
                    if launch_host_ns and time.monotonic_ns() >= launch_host_ns:
                        break
                    if schedule_writer_stop.wait(max(0.0, next_due - time.monotonic())):
                        break
                    if launch_host_ns and time.monotonic_ns() >= launch_host_ns:
                        break
                    try:
                        refresh_case_schedule(require_targets_absent=True)
                    except Exception as exc:
                        schedule_errors.append(f"{event_id}: background schedule writer: {exc}")
                    next_due += 1.0
                    # If a refresh overran the one-second cadence, start the
                    # next single-flight refresh immediately and log its actual
                    # timestamps; never overlap writes to the schedule file.
                    if next_due < time.monotonic():
                        next_due = time.monotonic()
            schedule_writer_thread = threading.Thread(target=schedule_writer_loop, daemon=True)
            active_schedule_writer_thread = schedule_writer_thread
            schedule_writer_thread.start()
            target_launch_status, launch_timer, launch_cancel = launch_case(case_number, case_id, target_order, event_id,
                launch_host_ns, launch_boottime, target_daily, target_peak, target_file, hold,
                target_presence_invalid, target_absence_state)
            active_launch_timer, active_launch_cancel = launch_timer, launch_cancel
            # Independent screenCap worker: it never takes window_io_lock and
            # cannot block the already-armed launch barrier or target RPCs.
            session.mark(f"scene4_{case_id}_begin", device_clock=(case_begin_uptime, case_begin_wall),
                         kind=kind, hold_seconds=hold,
                         target_order=target_order, launch_boottime_s=launch_boottime,
                         clock_anchor=anchor)
            result["case_plan"].append({"case_id": case_id, "kind": kind,
                "hold_seconds": hold, "seed": seed, "target_order": target_order,
                "target_peak_mib": target_peak, "target_daily_mib": target_daily,
                "target_file_mib": target_file, "launch_boottime_s": launch_boottime,
                "launch_host_monotonic_ns": launch_host_ns, "launch_clock_anchor": anchor})

            # Schedule refreshes run on a dedicated single writer while the
            # main thread collects process/system/UI evidence.
            next_pre_sample = time.monotonic()
            pre_samples = []
            while time.monotonic_ns() < launch_host_ns:
                if active_reader_monitor["failed"].is_set():
                    raise RuntimeError(f"Scene4 kernel reader died before launch barrier for {case_id}")
                if time.monotonic() >= next_pre_sample:
                    clock_host_start_ns = time.monotonic_ns()
                    clock_error = None
                    try:
                        sample_clock = _scene4_clock_probe(session)
                    except Exception as exc:
                        # A transient read-only clock timeout is a missing
                        # sample, never a fabricated device timestamp. Keep the
                        # fixed launch timer running and persist the gap.
                        sample_clock = None
                        clock_error = str(exc)
                    clock_host_end_ns = time.monotonic_ns()
                    now_uptime = sample_clock["boottime_s"] if sample_clock else None
                    sample = {"uptime_seconds": now_uptime, "phase": "pre_barrier", "bundles": {},
                              "clock_host_start_ns": clock_host_start_ns,
                              "clock_host_end_ns": clock_host_end_ns,
                              "clock_sample_status": "observed" if sample_clock else "unknown_timeout",
                              "clock_sample_error": clock_error,
                              "system": (_scene4_system_sample(d, case_runtime["daemon_cpu_state"],
                                  clk_tck=sample_clock.get("clk_tck"),
                                  page_size_bytes=sample_clock.get("page_size_bytes"),
                                  reference_boottime_s=now_uptime) if sample_clock else
                                  {"unknown": True, "reason": "device_clock_sample_missing"})}
                    for owned_bundle in all_bundles:
                        if sample_clock is None:
                            sample["bundles"][owned_bundle] = {
                                "unknown": True, "reason": "device_clock_sample_missing"}
                            continue
                        try:
                            observation_host_start_ns = time.monotonic_ns()
                            snapshot = _scene4_memcg_snapshot(d, owned_bundle)
                            observation_host_end_ns = time.monotonic_ns()
                            interval = _scene4_observation_boottime_interval(sample_clock,
                                clock_host_start_ns, clock_host_end_ns,
                                observation_host_start_ns, observation_host_end_ns)
                            snapshot["observation_interval"] = interval
                            sample["bundles"][owned_bundle] = snapshot
                            update_process_history(owned_bundle, snapshot, interval)
                        except Exception as exc:
                            sample["bundles"][owned_bundle] = {"unknown": True, "error": str(exc)}
                    persist_system_sample(sample, pre_samples)
                    next_pre_sample = time.monotonic() + 1.0
                time.sleep(0.05)
            minimum_observation_end = launch_host_ns / 1e9 + hold
            classification_deadline_host = launch_host_ns / 1e9 + float(
                cfg.get("cold_start_ready_timeout_seconds", 180))
            # Continue sampling and classification through the full hold, including
            # donor and unrelated OOM side effects; never stop at cold-ready.
            case_outcomes = target_launch_status
            samples = []
            while True:
                if active_reader_monitor["failed"].is_set():
                    raise RuntimeError(f"Scene4 kernel reader died during counter window for {case_id}")
                now_host = time.monotonic()
                target_hold_ends = [row.get("estimated_hold_end_host") for row in case_outcomes.values()
                                    if row.get("readiness_status") == "cold_start_ready" and
                                    row.get("estimated_hold_end_host") is not None]
                needed_end = max([minimum_observation_end] + target_hold_ends)
                unresolved = [row for row in case_outcomes.values()
                    if row.get("readiness_status") == "pending" and row.get("launch_status") != "failure"]
                deadline_elapsed = now_host >= classification_deadline_host
                if _scene4_should_end_counter_window(now_host, minimum_observation_end,
                                                     target_hold_ends, len(unresolved)):
                    break
                # Schedule refresh runs on its dedicated single writer thread.
                clock_host_start_ns = time.monotonic_ns()
                clock_error = None
                try:
                    sample_clock = _scene4_clock_probe(session)
                except Exception as exc:
                    # Preserve host-side hold/deadline progress and continue
                    # journal/kernel collection. Device-clock-dependent process
                    # history remains explicitly unknown for this interval.
                    sample_clock = None
                    clock_error = str(exc)
                clock_host_end_ns = time.monotonic_ns()
                now_uptime = sample_clock["boottime_s"] if sample_clock else None
                sample = {"uptime_seconds": now_uptime, "bundles": {},
                          "clock_host_start_ns": clock_host_start_ns,
                          "clock_host_end_ns": clock_host_end_ns,
                          "clock_sample_status": "observed" if sample_clock else "unknown_timeout",
                          "clock_sample_error": clock_error,
                          "system": (_scene4_system_sample(d, case_runtime["daemon_cpu_state"],
                              clk_tck=sample_clock.get("clk_tck"),
                              page_size_bytes=sample_clock.get("page_size_bytes"),
                              reference_boottime_s=now_uptime) if sample_clock else
                              {"unknown": True, "reason": "device_clock_sample_missing"})}
                for bundle in all_bundles:
                    try:
                        if sample_clock is not None:
                            observation_host_start_ns = time.monotonic_ns()
                            snapshot = _scene4_memcg_snapshot(d, bundle)
                            observation_host_end_ns = time.monotonic_ns()
                            interval = _scene4_observation_boottime_interval(sample_clock,
                                clock_host_start_ns, clock_host_end_ns,
                                observation_host_start_ns, observation_host_end_ns)
                            snapshot["observation_interval"] = interval
                            update_process_history(bundle, snapshot, interval)
                        else:
                            snapshot = {"unknown": True, "reason": "device_clock_sample_missing"}
                        sample["bundles"][bundle] = snapshot
                        if bundle in targets:
                            rows = _scene4_read_journal(session, bundle, case_id)
                            case_outcomes[bundle]["journal_rows"] = rows
                            case_outcomes[bundle]["journal_evidence_complete"] = bool(rows)
                            cold = next((row for row in reversed(rows) if
                            _scene4_cold_ready(row, target_peak * 1024**2,
                                expected_file_bytes=target_file * 1024**2) and
                                (_scene4_event_time(row) or float("inf")) <=
                                    launch_boottime + float(cfg.get("cold_start_ready_timeout_seconds", 180))), None)
                            hold_end_event = next((item for item in reversed(rows)
                                if cold and item.get("event") == "cold_start_hold_end" and
                                str(item.get("pid", "")) == str(cold.get("pid", "")) and
                                str(item.get("uid", "")) == str(cold.get("uid", "")) and
                                str(item.get("start_ticks", "")) == str(cold.get("start_ticks", ""))), None)
                            if hold_end_event:
                                case_outcomes[bundle]["cold_start_hold_end"] = hold_end_event
                                hold_end_s = _scene4_event_time(hold_end_event)
                                if hold_end_s is not None:
                                    case_outcomes[bundle]["estimated_hold_end_host"] = (
                                        hold_end_s + anchor["host_minus_device_seconds"])
                            if cold and case_outcomes[bundle]["readiness_status"] == "pending":
                                case_outcomes[bundle].update(candidate_cold_ready_event=cold,
                                    cold_ready_boottime_s=_scene4_event_time(cold))
                                pid, uid = int(cold.get("pid", -1)), int(cold.get("uid", -1))
                                installed_uid = int(d.app(bundle)["uid"])
                                observed = d.inspect_process_identity(pid, timeout=8) if pid > 0 else {}
                                identity = observed.get("identity")
                                ready_ticks = str(cold.get("start_ticks", ""))
                                mismatch = proc_identity_mismatch(identity, pid, installed_uid, ready_ticks) if pid > 0 else "ready_pid_missing"
                                snapshot = _scene4_memcg_snapshot(d, bundle)
                                live = snapshot["processes"].get(f"{pid}:{ready_ticks}") if identity else None
                                verified_history = any(item.get("bundle") == bundle and
                                    int(item.get("uid", -1)) == installed_uid and
                                    str(item.get("start_ticks")) == ready_ticks and
                                    item.get("identity_verified") for item in
                                    case_runtime.get("process_history", {}).get(pid, []))
                                if uid == installed_uid and ((mismatch is None and live and live.get("verified")) or
                                                            verified_history):
                                    case_outcomes[bundle].update(readiness_status="cold_start_ready",
                                        cold_ready_event=cold, ready_boottime_s=_scene4_event_time(cold),
                                        identity=identity, identity_verified=True,
                                        estimated_hold_end_host=(_scene4_event_time(cold) +
                                            anchor["host_minus_device_seconds"] + hold + 1.0))
                                else:
                                    case_outcomes[bundle].update(readiness_status="unknown",
                                        reason="cold_ready_identity_unverified", identity_error=mismatch)
                            elif case_outcomes[bundle].get("readiness_status") == "pending":
                                enomem_row = next((item for item in reversed(rows)
                                    if item.get("event") == "allocation_failed" and
                                    int(item.get("errno", 0)) == 12 and
                                    (_scene4_event_time(item) or float("inf")) <=
                                        launch_boottime + float(cfg.get("cold_start_ready_timeout_seconds", 180))), None)
                                if enomem_row:
                                    case_outcomes[bundle].update(readiness_status="confirmed_startup_failure",
                                        enomem_event=enomem_row)
                    except Exception as exc:
                        if bundle in case_outcomes:
                            case_outcomes[bundle].setdefault("sampling_errors", []).append(str(exc))
                        else:
                            case_runtime.setdefault("donor_sampling_errors", {}).setdefault(bundle, []).append(str(exc))
                            schedule_errors.append(f"{event_id}: donor sampling failed for {bundle}: {exc}")
                        sample["bundles"][bundle] = {"unknown": True, "error": str(exc)}
                if deadline_elapsed:
                    # The final journal read above happens before freezing ordinary
                    # timeout; a coldready at the fixed deadline still gets its hold.
                    for bundle, row in case_outcomes.items():
                        if row.get("readiness_status") == "pending":
                            journal = row.get("journal_rows", [])
                            row.update(readiness_status="unknown_timeout" if journal else "unknown_evidence_gap",
                                reason="cold_start_ready_timeout" if journal else "target_journal_missing_at_timeout")
                persist_system_sample(sample, samples)
                time.sleep(0.5)
            schedule_writer_stop.set()
            schedule_writer_thread.join()
            active_schedule_writer_stop = active_schedule_writer_thread = None
            # Do not retire a case while scheduled Ability RPCs are still live.
            launch_timer.join()
            active_launch_timer = active_launch_cancel = None
            case_runtime["samples"] = pre_samples + samples
            sample_artifact = {"path": str(sample_path), "rows": len(pre_samples) + len(samples),
                "sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest() if sample_path.is_file() else None,
                "persist_errors": list(case_runtime["sample_persist_errors"]),
                "complete": not case_runtime["sample_persist_errors"] and sample_path.is_file()}
            for bundle, row in case_outcomes.items():
                if row["launch_status"] == "pending":
                    row.update(launch_status="unknown", reason="scheduled_launch_did_not_finish")
            if active_reader_monitor["failed"].is_set():
                raise RuntimeError(f"Scene4 kernel reader died before END marker for {case_id}")
            # Re-read only ready targets whose last observed journal lacked a
            # valid hold_end, before END/cutoff. The case sampler and schedule
            # writer have finished; frame capture and reader-health monitoring
            # remain active. This refresh never invents a timestamp.
            hold_journal_finalization = _scene4_finalize_target_hold_journals(
                session, targets, case_id, case_outcomes, hold, anchor,
                read_timeout_seconds=float(cfg.get("scene4_hold_journal_read_timeout_seconds", 2.0)))
            case_runtime["hold_journal_finalization"] = hold_journal_finalization
            if active_reader_monitor["failed"].is_set():
                raise RuntimeError(f"Scene4 kernel reader died during final hold journal refresh for {case_id}")
            counter_end_host = time.monotonic()
            case_end_marker = session.mark_scene4_kernel_dmesg_boundary(case_id, "END")
            reader_health = session.stop_scene4_reader_health_monitor(case_id)
            active_reader_monitor = None
            if reader_health.get("failure") is not None:
                raise RuntimeError(f"Scene4 kernel reader died at case end for {case_id}")
            reader_health_complete = bool(reader_health.get("verified_alive_at_begin") and
                reader_health.get("reader_alive_at_end") and reader_health.get("failure") is None)
            try:
                case_end_clock_anchor = _scene4_clock_anchor(session)
                case_end_clock_anchor_error = None
            except Exception as exc:
                # The END marker already carries an independently captured
                # BOOTTIME. Keep the case result, but mark this redundant
                # cross-domain anchor unavailable rather than aborting it.
                case_end_clock_anchor = None
                case_end_clock_anchor_error = str(exc)
            case_end_uptime = case_end_marker["boottime_seconds"]
            case_end_wall = case_end_marker["realtime_ms"]
            case_end_host_monotonic = time.monotonic()
            # The timeout bounds readiness classification only. A different
            # target may have become ready just before the bound and still owns
            # a full hold interval that extends the actual evidence window.
            counter_window = _scene4_count_window_cutoff(
                case_end_uptime, launch_boottime + hold, case_outcomes, hold)
            counter_end_device_uptime = counter_window["counter_end_boottime"]
            session.stop_scene4_frame_capture(case_id)
            frame_proof = session.finish_scene4_frame_capture(case_id, counter_end_device_uptime,
                                                               collection_end_boottime_s=case_end_uptime)
            counter_end_wall_ms = case_end_wall + int((counter_end_device_uptime - case_end_uptime) * 1000)
            counter_end_host_monotonic = case_begin_host_monotonic + (counter_end_device_uptime - case_begin_uptime)
            session.popup_baselines[case_id]["case_end_ms"] = counter_end_wall_ms
            # Keep the raw marker as the collection envelope, but count only
            # through the fixed minimum window or the latest actual hold_end.
            session.popup_baselines[case_id]["case_end_boottime_seconds"] = counter_end_device_uptime
            # Donors emit lifecycle and peak evidence under their own per-case
            # journal ID; using the target case ID silently dropped donor OOMs.
            attempts = {bundle: _scene4_read_journal(
                            session, bundle, donor_case_id if bundle in donors else case_id)
                        for bundle in all_bundles}
            for bundle in targets:
                attempts[bundle] = _scene4_read_journal(session, bundle, case_id)
            start_case_uptime = case_begin_uptime
            verified_process_history = {pid: [item for item in identities
                if item.get("identity_verified") is True]
                for pid, identities in case_runtime.get("process_history", {}).items()}
            journal_case_ids = {bundle: (donor_case_id if bundle in donors else case_id)
                                for bundle in all_bundles}
            test_popup_journal_signals = _scene4_journal_popup_signals(
                attempts, session.scene4_journal_readback, journal_case_ids,
                case_begin_uptime, counter_end_device_uptime)
            journal_readback_complete_bundles = {bundle for bundle in all_bundles
                if session.scene4_journal_readback.get(
                    (journal_case_ids[bundle], bundle), {}).get("complete") is True}
            kernel_info = session.collect_kernel_dmesg_case(case_id,
                verified_process_history, all_bundles, attempts,
                start_case_uptime, counter_end_device_uptime, case_begin_wall, case_begin_uptime,
                scene4_cold_only=True,
                journal_readback_complete_bundles=journal_readback_complete_bundles,
                journal_case_ids=journal_case_ids,
                startup_failure_bundles=set(targets))
            kernel_info["scene4_journal_readback_gates"] = {
                "case_ids": journal_case_ids,
                "complete_bundles": sorted(journal_readback_complete_bundles),
                "startup_failure_bundles": sorted(targets),
                "proofs": {bundle: session.scene4_journal_readback.get(
                    (journal_case_ids[bundle], bundle)) for bundle in all_bundles}}
            raw_kernel_for_markers = session.kernel_dmesg_path.read_text(encoding="utf-8", errors="replace") \
                if session.kernel_dmesg_path.exists() else ""
            begin_marker_correlation = _scene4_marker_raw_correlation(raw_kernel_for_markers, case_begin_marker)
            end_marker_correlation = _scene4_marker_raw_correlation(raw_kernel_for_markers, case_end_marker)
            correlation_offsets = [value["raw_minus_boottime_seconds"]
                for value in (begin_marker_correlation, end_marker_correlation)
                if value.get("raw_minus_boottime_seconds") is not None]
            marker_offset_drift = abs(correlation_offsets[1] - correlation_offsets[0]) \
                if len(correlation_offsets) == 2 else None
            marker_correlation_complete = bool(begin_marker_correlation["matched"] and
                end_marker_correlation["matched"] and marker_offset_drift is not None and marker_offset_drift <= 1.0)
            raw_event_times_after_cutoff = []
            # Correlation stores raw_minus_boottime (raw - BOOT); convert to
            # the additive raw→BOOT offset before mapping event timestamps.
            dmesg_to_boottime_offset = (-sum(correlation_offsets) / len(correlation_offsets)
                if len(correlation_offsets) == 2 and marker_correlation_complete else None)
            if dmesg_to_boottime_offset is not None:
                for line in raw_kernel_for_markers.splitlines():
                    stamp = re.match(r"^\[\s*(\d+(?:\.\d+)?)\]", line)
                    if (stamp and re.search(r"\[mem_oom:\d+\].*out of Memory.*start to kill process", line, re.I)
                            and float(stamp.group(1)) + dmesg_to_boottime_offset > counter_end_device_uptime):
                        raw_event_times_after_cutoff.append({"raw_line": line,
                            "mapped_boottime_s": float(stamp.group(1)) + dmesg_to_boottime_offset})
            kernel_info["same_process_marker_correlation"] = {
                "begin": begin_marker_correlation, "end": end_marker_correlation,
                "raw_minus_boottime_offset_drift_seconds": marker_offset_drift,
                "complete": marker_correlation_complete}
            kernel_info["events_after_metric_cutoff"] = raw_event_times_after_cutoff
            if not marker_correlation_complete:
                kernel_info["coverage_complete"] = False
                kernel_info["same_process_marker_correlation"]["reason"] = "missing_marker_or_unstable_dmesg_boottime_mapping"
            # Donor checksum/fault validation is deliberately triggered only
            # after the measured counter and kernel windows have closed.
            donor_validation = {}
            for donor_bundle in donors:
                expected_identity = donor_identities.get(donor_bundle, {})
                snap = _scene4_memcg_snapshot(d, donor_bundle)
                identity_key = f"{expected_identity.get('pid')}:{expected_identity.get('start_ticks')}"
                if identity_key not in snap.get("processes", {}):
                    donor_validation[donor_bundle] = {"status": "unknown_donor_missing_after_window",
                        "identity": expected_identity}
                    continue
                validation_error = None
                validation_started_host = time.monotonic()
                validation_timeout = max(1.0, float(cfg.get(
                    "donor_post_window_validation_timeout_seconds", 180)))
                validation_deadline = validation_started_host + validation_timeout
                try:
                    d.launch(donor_bundle, params={"op": "donor_validate"}, timeout=15)
                except Exception as exc:
                    validation_error = str(exc)
                validation_rows = []
                validation_terminal = None
                while time.monotonic() < validation_deadline:
                    validation_rows = _scene4_identity_events(
                        _scene4_read_journal(session, donor_bundle, donor_case_id),
                        int(expected_identity["pid"]), int(d.app(donor_bundle)["uid"]),
                        expected_identity.get("start_ticks"))
                    ending = next((item for item in reversed(validation_rows)
                                   if item.get("event") == "donor_validation_end"), None)
                    validation_terminal = next((item for item in reversed(validation_rows)
                        if item.get("event") == "donor_validation_error"), None)
                    if ending or validation_terminal:
                        break
                    # Keep this donor foreground until it completes or reaches
                    # the explicit bound; starting the next donor sooner can
                    # freeze an unfinished read of swapped pages.
                    time.sleep(min(1.0, max(0.0, validation_deadline - time.monotonic())))
                ending = next((item for item in reversed(validation_rows)
                               if item.get("event") == "donor_validation_end"), None)
                first = next((item for item in validation_rows
                              if item.get("event") == "donor_first_touch_complete"), None)
                expected_checksum = str((first or {}).get("donor_expected_checksum", ""))
                observed_initial = str((first or {}).get("donor_initial_read_checksum", ""))
                validation_ok = _scene4_donor_checksum_complete(first, ending)
                first_begin = next((item for item in validation_rows
                    if item.get("event") == "donor_post_alloc_read_begin"), None)
                first_end = next((item for item in validation_rows
                    if item.get("event") == "donor_post_alloc_read_end"), None)
                initial_read_seconds = None
                if first_begin and first_end:
                    begin_s, end_s = _scene4_event_time(first_begin), _scene4_event_time(first_end)
                    if begin_s is not None and end_s is not None and end_s >= begin_s:
                        initial_read_seconds = end_s - begin_s
                validation_elapsed = time.monotonic() - validation_started_host
                donor_validation[donor_bundle] = {"status": "complete" if validation_ok else
                    ("failed" if ending or validation_terminal else "unknown_timeout"),
                    "error": validation_error, "first_touch": first,
                    "validation_end": ending, "identity_events": validation_rows,
                    "validation_error_event": validation_terminal,
                    "expected_checksum": expected_checksum,
                    "observed_initial_checksum": observed_initial,
                    "initial_read_seconds": initial_read_seconds,
                    "validation_timeout_seconds": validation_timeout,
                    "validation_elapsed_host_seconds": validation_elapsed,
                    "validation_request_accepted": validation_error is None,
                    "validation_end_seen": ending is not None,
                    "expected_touched_bytes": 3072 * 1024**2}
            ledger_text = ""
            timing_v3_text = ""
            ledger_deadline = time.monotonic() + float(cfg.get("oracle_ledger_timeout_seconds", 30))
            while time.monotonic() < ledger_deadline:
                ledger_text = d.shell("cat " + quote(ledger_remote), timeout=10, check=False)
                timing_v3_text = d.shell("cat " + quote(timing_v3_remote), timeout=10, check=False)
                parsed_ledger = list(csv.DictReader(ledger_text.splitlines())) if ledger_text.strip() else []
                event_rows = [row for row in parsed_ledger if row.get("event_id") == event_id]
                parsed_timing = list(csv.DictReader(timing_v3_text.splitlines())) if timing_v3_text.strip() else []
                timing_event_rows = [row for row in parsed_timing if row.get("event_id") == event_id]
                timing_pair_proof = _scene4_timing_v3_completeness(event_rows,
                    timing_event_rows, event_id)
                timing_header_valid = bool(timing_v3_text.splitlines() and
                    timing_v3_text.splitlines()[0].strip() == SCENE4_TIMING_V3_HEADER)
                if (_scene4_ledger_completeness(event_rows, session.phase,
                        control_begin, control_begin)["complete"] and timing_pair_proof["complete"] and
                        timing_header_valid):
                    break
                time.sleep(1.0)
            control_end = _scene4_control_readback(session, schedule_remote)
            ledger_copy = session.output / f"oracle_action_ledger_{case_id}.csv"
            ledger_copy.write_text(ledger_text, encoding="utf-8")
            ledger_rows = list(csv.DictReader(ledger_text.splitlines())) if ledger_text.strip() else []
            ledger_rows = [row for row in ledger_rows if row.get("event_id") == event_id]
            timing_v3_copy = session.output / f"oracle_action_timing_v3_{case_id}.csv"
            timing_v3_copy.write_text(timing_v3_text, encoding="utf-8")
            swap_telemetry_remote = ledger_remote + ".swap_telemetry_v1.csv"
            swap_telemetry_text = d.shell(
                "if [ -r " + quote(swap_telemetry_remote) + " ]; then cat " +
                quote(swap_telemetry_remote) + "; else printf ''; fi",
                timeout=10, check=False)
            swap_telemetry_copy = session.output / f"oracle_action_swap_telemetry_v1_{case_id}.csv"
            swap_telemetry_copy.write_text(swap_telemetry_text, encoding="utf-8")
            swap_telemetry_header = (
                "action_id,event_id,case_id,bundle,pid,uid,start_ticks,write_attempted,write_return,"
                "write_errno,syscall_start_boottime_s,syscall_return_boottime_s,stage,read_start_boottime_s,"
                "read_end_boottime_s,read_duration_ms,group_scope,group_path,node,read_status,read_errno,raw")
            swap_telemetry_lines = swap_telemetry_text.splitlines()
            swap_telemetry_header_valid = bool(swap_telemetry_lines and
                swap_telemetry_lines[0].strip() == swap_telemetry_header)
            swap_telemetry_proof = {
                "remote_path": swap_telemetry_remote,
                "local_path": str(swap_telemetry_copy.name),
                "exists": bool(swap_telemetry_text.strip()),
                "header_valid": swap_telemetry_header_valid,
                "sha256": hashlib.sha256(swap_telemetry_text.encode("utf-8")).hexdigest(),
                "bytes": len(swap_telemetry_text.encode("utf-8")),
                "rows": max(0, len(swap_telemetry_lines) - (1 if swap_telemetry_header_valid else 0)),
                "required_for_force_shrink_all": False,
                "note": "service swap telemetry sidecar is retained when present; no action means an absent/empty sidecar is expected"
            }
            timing_v3_all_rows = list(csv.DictReader(timing_v3_text.splitlines())) if timing_v3_text.strip() else []
            timing_v3_rows = [row for row in timing_v3_all_rows if row.get("event_id") == event_id]
            timing_v3_proof = _scene4_timing_v3_completeness(ledger_rows, timing_v3_rows, event_id)
            timing_v3_header_valid = bool(timing_v3_text.splitlines() and
                timing_v3_text.splitlines()[0].strip() == SCENE4_TIMING_V3_HEADER)
            if not timing_v3_header_valid:
                timing_v3_proof["complete"] = False
                timing_v3_proof["errors"].append("timing_v3_header_missing_or_mismatch")
            timing_v3_proof.update(remote_path=timing_v3_remote,
                local_path=str(timing_v3_copy.name), sha256=hashlib.sha256(
                    timing_v3_text.encode("utf-8")).hexdigest(),
                bytes=len(timing_v3_text.encode("utf-8")), header_valid=timing_v3_header_valid)
            result.setdefault("oracle_sidecars", {})["swap_telemetry_v1"] = swap_telemetry_proof
            ledger_proof = _scene4_ledger_completeness(ledger_rows, session.phase,
                                                        control_begin, control_end)
            ledger_evidence_complete = ledger_proof["complete"]
            ledger_write_lengths = ledger_proof["write_lengths"]
            ledger_expectation = ledger_proof["expected"]
            ready_times = {bundle: case_outcomes[bundle].get("cold_ready_boottime_s",
                            case_outcomes[bundle].get("ready_boottime_s")) for bundle in targets}
            classes = []
            for bundle in targets:
                row = case_outcomes[bundle]
                journal_rows = _scene4_read_journal(session, bundle, case_id)
                ready_s = ready_times[bundle]
                enomem = [item for item in journal_rows if item.get("event") == "allocation_failed" and
                          int(item.get("errno", 0)) == 12 and
                          (ready_s is None or (_scene4_event_time(item) or float("inf")) <= ready_s) and
                          (_scene4_event_time(item) or float("inf")) <=
                            launch_boottime + float(cfg.get("cold_start_ready_timeout_seconds", 180)) and
                          (_scene4_event_time(item) or float("inf")) <= counter_end_device_uptime]
                aa_reject = row.get("launch_status") == "failure"
                kernel_kill = next((victim for victim in kernel_info.get("victims", [])
                    if victim.get("bundle") == bundle and
                    victim.get("startup_failure_eligible") is True and
                    victim.get("readiness_relation") == "pre_ready" and
                    victim.get("event_uptime_s", float("inf")) <=
                        (ready_s if ready_s is not None else launch_boottime +
                         float(cfg.get("cold_start_ready_timeout_seconds", 180)))), None)
                if enomem or aa_reject or kernel_kill:
                    identity_incident = (f"{bundle}:{kernel_kill.get('pid')}:{kernel_kill.get('start_ticks')}"
                                         if kernel_kill else None)
                    classes.append({"bundle": bundle, "classification": "startup_failure",
                        "evidence_kind": "ENOMEM" if enomem else ("aa_explicit_rejection" if aa_reject else "exact_identity_kernel_kill"),
                        "evidence": enomem or ([row.get("launch_error")] if aa_reject else [kernel_kill]),
                        "incident_identity": identity_incident,
                        "incident": identity_incident or f"{case_id}:{bundle}:startup_attempt"})
                elif row.get("readiness_status") == "cold_start_ready":
                    classes.append({"bundle": bundle, "classification": "cold_start_ready",
                                    "ready_boottime_s": ready_s, "incident": None})
                elif row.get("readiness_status") == "unknown_timeout":
                    classes.append({"bundle": bundle, "classification": "ordinary_startup_timeout",
                                    "reason": row.get("reason"), "incident": None,
                                    "evidence_complete": bool(row.get("journal_evidence_complete"))})
                elif row.get("readiness_status") == "unknown_evidence_gap":
                    classes.append({"bundle": bundle, "classification": "unknown_evidence_gap",
                                    "reason": row.get("reason"), "incident": None})
                else:
                    classes.append({"bundle": bundle, "classification": "unknown_evidence_gap",
                                    "reason": row.get("reason", row.get("readiness_status")), "incident": None})
            # Classify raw kernel OOM victims separately from startup status; an
            # OOM after cold-ready never becomes a startup failure retroactively.
            own = set(all_bundles)
            unmatched = []
            raw_dmesg = session.kernel_dmesg_path.read_text(encoding="utf-8", errors="replace") if session.kernel_dmesg_path.exists() else ""
            raw_lines = raw_dmesg.splitlines()
            begin_line = next((i for i, line in enumerate(raw_lines)
                               if kernel_info.get("begin_marker") and kernel_info["begin_marker"] in line), None)
            end_line = next((i for i, line in enumerate(raw_lines)
                             if kernel_info.get("end_marker") and kernel_info["end_marker"] in line), None)
            case_dmesg_lines = raw_lines[begin_line + 1:end_line] if begin_line is not None and end_line is not None else []
            matched_victim_lines = {item.get("victim_line", "").strip() for item in kernel_info.get("victims", [])}
            confirmed_unattributed_oom, unknown_unattributed_oom = ([], [])
            if marker_correlation_complete:
                confirmed_unattributed_oom, unknown_unattributed_oom = _scene4_confirm_unattributed_kernel_victims(
                    case_dmesg_lines, case_begin_uptime, counter_end_device_uptime,
                    -sum(correlation_offsets) / len(correlation_offsets), all_bundles,
                    excluded_victim_lines=matched_victim_lines)
            confirmed_unattributed_lines = {item["victim_line"].strip() for item in confirmed_unattributed_oom}
            for line in case_dmesg_lines:
                raw_stamp = re.match(r"^\[\s*(\d+(?:\.\d+)?)\]", line)
                after_cutoff = bool(marker_correlation_complete and raw_stamp and
                    float(raw_stamp.group(1)) - sum(correlation_offsets) / len(correlation_offsets) >
                    counter_end_device_uptime)
                if (not after_cutoff and
                        re.search(r"\[mem_oom:\d+\].*out of Memory.*start to kill process", line, re.I) and
                        line.strip() not in matched_victim_lines and line.strip() not in confirmed_unattributed_lines):
                    unmatched.append(line)
            kernel_info["confirmed_unattributed_victims"] = confirmed_unattributed_oom
            kernel_info["unknown_unattributed_victim_marks"] = unknown_unattributed_oom
            native_events_all = [event for event in session.book.events if event.get("case_id") == case_id and
                                 event.get("category") == "low_memory_popup_native"]
            native_events = [event for event in native_events_all
                if int(event.get("timestamp_ms", 0)) <= counter_end_wall_ms]
            native_events_after_cutoff = [event for event in native_events_all
                if int(event.get("timestamp_ms", 0)) > counter_end_wall_ms]
            test_popups = [event for event in session.book.events if event.get("case_id") == case_id and
                           event.get("category") == "low_memory_popup_test"]
            ui_rows_all = session.ui_observations.get(case_id, [])
            ui_rows_in_window = [row for row in ui_rows_all
                if float(row.get("capture_completed_host_monotonic", float("inf"))) <=
                   counter_end_host_monotonic]
            ui_rows_after_cutoff = [row for row in ui_rows_all
                if float(row.get("capture_started_host_monotonic", float("inf"))) >
                   counter_end_host_monotonic]
            ui_rows_crossing_cutoff = [row for row in ui_rows_all
                if float(row.get("capture_started_host_monotonic", float("inf"))) <=
                   counter_end_host_monotonic <
                   float(row.get("capture_completed_host_monotonic", float("inf")))]
            tree_ui_diagnostic = _scene4_ui_evidence_proof(ui_rows_in_window,
                session.result.get("ui_evidence_errors", []) +
                    session.result.get("scene4_ui_errors", []),
                float(cfg.get("ui_max_sampling_gap_seconds", 10.0)), case_id=case_id,
                begin_host_monotonic=case_begin_host_monotonic,
                end_host_monotonic=counter_end_host_monotonic)
            ui_proof = {"complete": bool(frame_proof.get("complete")),
                "evidence_mode": "2s_sha_bound_screenCap_manual_review",
                "maximum_frame_gap_seconds": SCENE4_FRAME_MAX_GAP_SECONDS,
                "frame_manifest": frame_proof,
                "tree_diagnostic": tree_ui_diagnostic,
                "pending_review_count": sum(row.get("review_classification") == "pending_review"
                                             for row in frame_proof.get("frames", [])),
                "review_manifest_path": frame_proof.get("review_path")}
            ui_proof["review_revalidation_helper"] = "memscenes.runner._scene4_revalidate_frame_manifest_files"
            # Scene4 popup counts come only from image-reviewed, identity-bound
            # frame episodes. The old tree event path remains diagnostic and is
            # not allowed to turn missing screen evidence into a zero.
            native_events = [{**event, "category": "low_memory_popup_native",
                "incident": event.get("incident_id"), "source_id": event.get("frame_id"),
                "case_id": case_id, "visual_reviewed": True,
                "timestamp_boottime_s": event.get("capture_boottime_pre_s")}
                for event in frame_proof.get("native_popup_events", [])]
            native_events_after_cutoff = []
            owned_oom = kernel_info.get("victims", [])
            confirmed_unattributed_oom = kernel_info.get("confirmed_unattributed_victims", [])
            oom = owned_oom + confirmed_unattributed_oom
            donor_oom_bundles = {item.get("bundle") for item in owned_oom if item.get("bundle") in donors}
            hold_completion = {}
            for bundle in targets:
                target_row = case_outcomes[bundle]
                hold_completion[bundle] = _scene4_hold_coverage(bundle,
                    target_row.get("cold_ready_event"), target_row.get("cold_start_hold_end"),
                    hold, owned_oom)
                if target_row.get("readiness_status") != "cold_start_ready":
                    hold_completion[bundle]["complete"] = True
            clock_constants = case_begin_clock_anchor.get("device_clock_sample", {})
            case_plan_rows = result.get("case_plan", [])
            launch_anchor = case_plan_rows[-1].get("launch_clock_anchor", {}) if case_plan_rows else {}
            launch_clock_sample = launch_anchor.get("device_clock_sample", {})
            target_start_ticks_proof = _scene4_target_start_ticks_gate(
                {bundle: {"cold_start_ready": case_outcomes[bundle].get("cold_ready_event")}
                 for bundle in targets}, targets, launch_boottime,
                int(clock_constants.get("clk_tck", 0) or 0),
                float(launch_clock_sample.get("boottime_minus_proc_uptime_s", 0.0)),
                float(launch_anchor.get("query_uncertainty_ns", 0) or 0) / 1e9)
            prelaunch_absence_proof = {"checks": target_absence_checks,
                "latest_check_complete": target_absence_state.get("complete") is True,
                "latest_check_absent": target_absence_state.get("absent") is True,
                "age_at_launch_seconds": target_absence_state.get("age_at_launch_seconds"),
                "launch_gate_passed": target_absence_state.get("launch_gate_passed") is True,
                "invalid_event_set": target_presence_invalid.is_set(),
                "log_path": str(target_absence_log)}
            incidents = _scene4_unique_incident_ids(case_id, classes, oom, native_events)
            measurement_gates_without_ui = {
                "kernel_coverage_complete": bool(kernel_info.get("coverage_complete")),
                "marker_correlation_complete": bool(marker_correlation_complete),
                "all_targets_terminal_or_classified": all(
                    row.get("launch_status") != "pending" for row in case_outcomes.values()),
                "prelaunch_target_absence_gate_passed": prelaunch_absence_proof["launch_gate_passed"],
                "ready_target_start_tick_evidence_complete": target_start_ticks_proof["complete"],
                "target_journals_complete_or_explicit_launch_failure": all(
                    row.get("journal_evidence_complete") or row.get("launch_status") == "failure"
                    for row in case_outcomes.values()),
                "final_target_journal_readbacks_complete": bool(hold_journal_finalization.get("complete")),
                "no_startup_evidence_gap": not any(
                    x["classification"] == "unknown_evidence_gap" for x in classes),
                "all_target_holds_covered": all(row["complete"] for row in hold_completion.values()),
                "counter_window_covers_required_hold": not counter_window[
                    "marker_precedes_required_hold_end"],
                "kernel_reader_alive_for_whole_case": reader_health_complete,
                "schedule_refresh_complete": not any(event_id in error for error in schedule_errors),
                "oracle_ledger_complete": bool(ledger_evidence_complete),
                "timing_v3_complete": bool(timing_v3_proof["complete"]),
                "no_unmatched_kernel_victim": not bool(unmatched),
                "control_end_verified": bool(control_end.get("verified")),
                "donor_validation_complete": _scene4_donor_validation_complete(
                    donor_validation, set(donors), donor_oom_bundles)}
            measurement_gates_without_ui["complete"] = all(
                value is True for key, value in measurement_gates_without_ui.items() if key != "complete")
            case_summary = {"case_id": case_id, "kind": kind, "hold_seconds": hold,
                "seed": seed, "target_order": target_order, "targets": case_outcomes,
                "prelaunch_target_absence": prelaunch_absence_proof,
                "target_process_start_ticks_gate": target_start_ticks_proof,
                "clock_domain_begin": case_begin_clock_anchor,
                "clock_domain_end": case_end_clock_anchor,
                "clock_domain_end_error": case_end_clock_anchor_error,
                "clock_domain_offset_drift_s": (
                    case_end_clock_anchor["device_clock_sample"]["boottime_minus_proc_uptime_s"] -
                    case_begin_clock_anchor["device_clock_sample"]["boottime_minus_proc_uptime_s"]
                    if case_end_clock_anchor is not None else None),
                "donor_case_id": donor_case_id, "donor_state": deepcopy(donor_results),
                "donor_validation_after_window": donor_validation,
                "popup_preparation": popup_preparation,
                "target_classifications": classes, "kernel_oom": kernel_info,
                "target_hold_intervals": {bundle: {
                    "cold_start_ready": case_outcomes[bundle].get("cold_ready_event"),
                    "cold_start_hold_end": case_outcomes[bundle].get("cold_start_hold_end"),
                "hold_end_observed": hold_completion[bundle]["hold_end_observed"],
                "confirmed_oom_termination": hold_completion[bundle]["confirmed_oom_termination"],
                "hold_complete": hold_completion[bundle]["complete"],
                    "required_hold_seconds": hold,
                    "estimated_hold_end_host_monotonic": case_outcomes[bundle].get("estimated_hold_end_host")}
                    for bundle in targets},
                "target_hold_journal_finalization": hold_journal_finalization,
                "system_sampling_artifact": sample_artifact,
                "native_popup_events": native_events, "test_popup_events_separate": test_popups,
                "test_popup_journal_signals_separate": test_popup_journal_signals,
                "native_popup_events_after_metric_cutoff": native_events_after_cutoff,
                "ui_observations_after_metric_cutoff": ui_rows_after_cutoff,
                "ui_observations_crossing_metric_cutoff": ui_rows_crossing_cutoff,
                "other_oom_side_effects": unmatched,
                "unattributed_kernel_victim_evidence_gap": bool(unmatched),
                "counter_end_host_monotonic": counter_end_host,
                "metric_window_end_host_monotonic": counter_end_host_monotonic,
                "counter_end_device_boottime": counter_end_device_uptime,
                "kernel_reader_health": reader_health,
                "raw_end_marker_device_boottime": case_end_uptime,
                "raw_end_marker_wall_ms": case_end_wall,
                "counter_end_wall_ms": counter_end_wall_ms,
                "counter_window_cutoff": counter_window,
                "raw_end_marker_device_boottime": case_end_uptime,
                "counter_window_cutoff": counter_window,
                "classification_deadline_host_monotonic": classification_deadline_host,
                "classification_grace_seconds": max(0.0, classification_deadline_host-counter_end_host),
                "timeout_evidence_complete": all(
                    x.get("classification") != "ordinary_startup_timeout" or x.get("evidence_complete")
                    for x in classes),
                "unknown_evidence_gap": any(x.get("classification") == "unknown_evidence_gap" for x in classes),
                "hold_completion_gaps": [bundle for bundle, row in hold_completion.items()
                                         if not row["complete"]],
                "unique_incident_ids": sorted(incidents),
                "ui_evidence_complete": ui_proof["complete"],
                "same_process_marker_correlation": kernel_info.get("same_process_marker_correlation"),
                "counts": {"startup_failure": sum(x["classification"] == "startup_failure" for x in classes),
                    "confirmed_oom_kill": len(oom), "native_low_memory_popup":
                        len(native_events) if ui_proof["complete"] else None,
                    "owned_confirmed_oom_kill": len(owned_oom),
                    "confirmed_unattributed_kernel_victim": len(confirmed_unattributed_oom),
                    "unknown_kernel_victim_marks": len(unmatched),
                    "donor_oom_side_effects": sum(item.get("bundle") in donors for item in owned_oom),
                    "target_oom_side_effects": sum(item.get("bundle") in targets for item in owned_oom),
                    "test_popup_separate": len(test_popups),
                    "ordinary_startup_timeout": sum(x["classification"] == "ordinary_startup_timeout" for x in classes),
                    "unknown_evidence_gap": sum(x["classification"] == "unknown_evidence_gap" for x in classes),
                    "unknown_timeout_or_gap": sum(x["classification"] in
                        ("ordinary_startup_timeout", "unknown_evidence_gap") for x in classes),
                    "unique_incidents": len(incidents) if ui_proof["complete"] else None,
                    "primary_count": (sum(x["classification"] == "startup_failure" for x in classes) +
                                      len(oom) + len(native_events)) if ui_proof["complete"] else None,
                    "native_popup_count_observed": bool(ui_proof["complete"]),
                    "other_oom_side_effects": len(unmatched)},
                "schedule": {"event_id": event_id, "remote_path": schedule_remote,
                    "fields": schedule_fields, "launch_boottime_s": launch_boottime,
                    "launch_host_monotonic_ns": launch_host_ns,
                    "atomic_updates": True, "refreshes": [x for x in donor_refresh_rows if x.get("event_id") == event_id],
                    "errors": [x for x in schedule_errors if event_id in x]},
                "launch_barrier": {"scheduled_host_monotonic_ns": launch_host_ns,
                    "actual_host_monotonic_ns": {bundle: case_outcomes[bundle].get("actual_host_monotonic_ns")
                        for bundle in target_order},
                    "dispatch_spread_ns": (max((row.get("actual_host_monotonic_ns") or 0 for row in case_outcomes.values()), default=0) -
                        min((row.get("actual_host_monotonic_ns") or 0 for row in case_outcomes.values()), default=0)),
                    "direct_launches": True},
                "oracle_ledger": {"remote_path": ledger_remote,
                    "local_path": str(ledger_copy.relative_to(session.output)),
                    "rows": ledger_rows, "observed": bool(ledger_rows),
                    "expected_evidence": ledger_expectation,
                    "write_lengths": ledger_write_lengths,
                    "validation": ledger_proof,
                    "complete": ledger_evidence_complete},
                "oracle_control": {"begin": control_begin, "end": control_end,
                    "complete": bool(control_begin.get("verified") and control_end.get("verified"))},
                "ui_evidence": {"observations": session.ui_observations.get(case_id, []),
                    **ui_proof},
                "outcome_measurement_complete": bool(kernel_info.get("coverage_complete") and
                    all(row.get("launch_status") != "pending" for row in case_outcomes.values()) and
                    all(row.get("journal_evidence_complete") or row.get("launch_status") == "failure"
                        for row in case_outcomes.values()) and
                    not any(x["classification"] == "unknown_evidence_gap" for x in classes)),
                "oracle_action_evidence_complete": (not any(event_id in error for error in schedule_errors) and
                    ledger_evidence_complete and timing_v3_proof["complete"]),
                "measurement_gates_without_ui": measurement_gates_without_ui,
                "measurement_complete": bool(measurement_gates_without_ui["complete"] and ui_proof["complete"]),
                "unknown_is_not_zero": True}
            result["cases"].append(case_summary)
            session.mark(f"scene4_{case_id}_end", device_clock=(case_end_uptime, case_end_wall),
                         counts=case_summary["counts"])
            # Self-owned targets are cold before the next fixed case; donors stay resident.
            for bundle in targets:
                d.force_stop(bundle)
                absent = not bool(_scene4_memcg_snapshot(d, bundle)["processes"])
                case_summary.setdefault("target_cleanup", {})[bundle] = {"processes_absent": absent}
            case_summary["donor_cleanup"] = {}
            for bundle in donors:
                d.force_stop(bundle)
                absent = not bool(_scene4_memcg_snapshot(d, bundle)["processes"])
                case_summary["donor_cleanup"][bundle] = {"processes_absent": absent}
            case_summary["donor_cleanup_complete"] = all(
                row["processes_absent"] for row in case_summary["donor_cleanup"].values())
            popup_restoration = session.restore_native_low_memory_dialog(case_id)
            time.sleep(float(cfg.get("popup_recovery_wait_seconds", 5.0)))
            case_summary["popup_restoration"] = popup_restoration
            case_summary["case_state_restored"] = bool(popup_preparation.get("verified_absent") and
                                                          popup_restoration.get("verified_absent") and
                                                          case_summary["donor_cleanup_complete"] and
                                                          all(row.get("processes_absent") for row in
                                                              case_summary["target_cleanup"].values()))
            case_summary["measurement_complete"] = bool(case_summary["measurement_complete"] and
                                                          case_summary["case_state_restored"])
            case_summary["measurement_gates_without_ui"]["case_state_restored"] = bool(
                case_summary["case_state_restored"])
            case_summary["measurement_gates_without_ui"]["complete"] = all(
                value is True for key, value in case_summary["measurement_gates_without_ui"].items()
                if key != "complete")
            case_summary["confirmed_count_lower_bounds"] = deepcopy(case_summary["counts"])
            case_summary["primary_counts_observed"] = case_summary["measurement_complete"] is True
            if not case_summary["primary_counts_observed"]:
                case_summary["primary_counts_incomplete_reason"] = {
                    "ui_evidence_complete": case_summary.get("ui_evidence_complete"),
                    "kernel_coverage_complete": case_summary.get("kernel_oom", {}).get("coverage_complete"),
                    "oracle_action_evidence_complete": case_summary.get("oracle_action_evidence_complete"),
                    "case_state_restored": case_summary.get("case_state_restored")}
            result["latest_checkpoint"] = _scene4_write_checkpoint(
                session.output, result, "case_completed", None)

        stop_status = ("completed_functional_calibration_probe" if functional_calibration else
                       "completed_single_case_calibration_probe" if single_case_calibration
                       else "completed_four_fixed_cases")
        monitor_stop.set()
        if window_thread is not None and window_thread.is_alive():
            window_thread.join(timeout=5)
        result["donors"] = donor_results
        result["donor_identity_events"] = {bundle: _scene4_read_journal(session, bundle, donor_case_id)
                                            for bundle in donors}
        result["clock_domain"] = deepcopy(session.result.get("scene4_clock_domain", {}))
        run_gates_without_ui = {
            "not_calibration_probe": not single_case_probe,
            "exactly_four_fixed_cases": len(result["cases"]) == 4,
            "all_case_non_ui_gates_complete": all(
                case.get("measurement_gates_without_ui", {}).get("complete") is True
                for case in result["cases"]),
            "all_target_cleanup_complete": all(case.get("target_cleanup") and all(
                row.get("processes_absent") for row in case["target_cleanup"].values())
                for case in result["cases"]),
            "clock_domain_stable": result["clock_domain"].get("stable") is True,
            "trace_clock_is_boot": result["clock_domain"].get("trace_clock_selected") == "boot"}
        run_gates_without_ui["complete"] = all(run_gates_without_ui.values())
        result["measurement_gates_without_ui"] = run_gates_without_ui
        result["measurement_complete"] = bool(run_gates_without_ui["complete"] and
            len(result["cases"]) == 4 and all(case["measurement_complete"] for case in result["cases"]))
        aggregate = {key: (None if any(case["counts"].get(key) is None for case in result["cases"])
                           else sum(case["counts"].get(key, 0) for case in result["cases"]))
            for key in ("startup_failure", "confirmed_oom_kill", "owned_confirmed_oom_kill",
                        "confirmed_unattributed_kernel_victim", "unknown_kernel_victim_marks",
                        "native_low_memory_popup",
                        "donor_oom_side_effects", "target_oom_side_effects",
                        "test_popup_separate", "ordinary_startup_timeout", "unknown_evidence_gap",
                        "unknown_timeout_or_gap", "unique_incidents",
                        "primary_count", "other_oom_side_effects")}
        unique_ids = set()
        for case in result["cases"]:
            unique_ids.update(case.get("unique_incident_ids", []))
        if aggregate.get("unique_incidents") is not None:
            aggregate["unique_incidents"] = len(unique_ids)
        result["confirmed_count_lower_bounds"] = aggregate
        counts_observed = _scene4_cumulative_counts_observed(result)
        result["cumulative_counts_observed"] = counts_observed
        if donor_proof_only:
            # A proof-only freshness probe has no startup window or UI/OOM
            # counter coverage. Keep it out of aggregate incident totals.
            result.pop("cumulative_counts", None)
        elif counts_observed:
            result["cumulative_counts"] = aggregate
        else:
            result["cumulative_counts"] = {key: None for key in aggregate}
        result["measurement_completeness"] = {"environment_validated": True,
            "all_four_fixed_cases_completed": len(result["cases"]) == 4,
            "six_synchronized_targets_per_case": all(len(case["targets"]) == 6 for case in result["cases"]),
            "atomic_schedule_refresh_and_launch_timer": True,
            "direct_aa_launches_await_peak_zero": True,
            "all_case_donor_states_isolated": len(result.get("donor_background_transitions", [])) == 4,
            "all_six_target_results_classified_or_unknown": all(len(case.get("target_classifications", [])) == 6 for case in result["cases"]),
            "target_journal_evidence_collected": all(all(row.get("journal_evidence_complete") or row.get("launch_status") == "failure"
                for row in case["targets"].values()) for case in result["cases"]),
            "all_case_kernel_windows_complete": all(case["kernel_oom"].get("coverage_complete") for case in result["cases"]),
            "same_process_clock_marker_correlation_complete": all(
                case.get("same_process_marker_correlation", {}).get("complete") for case in result["cases"]),
            "clock_domain_mapping_stable": result["clock_domain"].get("stable") is True,
            "kernel_trace_clock_is_boot": result["clock_domain"].get("trace_clock_selected") == "boot",
            "clock_source": result["clock_domain"].get("boottime_source"),
            "maximum_boottime_to_proc_uptime_offset_drift_s": max(
                (abs(sample.get("offset_drift_from_initial_s", 0.0))
                 for sample in result["clock_domain"].get("samples", [])), default=None),
            "schedule_evidence_complete": not schedule_errors,
            "oracle_action_evidence_complete": all(case.get("oracle_action_evidence_complete") for case in result["cases"]),
            "ui_evidence_complete": _scene4_all_ui_evidence_complete(result["cases"]),
            "target_cleanup_complete": all(case.get("target_cleanup") and all(row.get("processes_absent")
                for row in case["target_cleanup"].values()) for case in result["cases"]),
            "donor_cleanup_complete_each_case": all(case.get("donor_cleanup_complete") for case in result["cases"]),
            "formal_qualification_eligible": False}
        result["schedule_refresh_errors"] = schedule_errors
        result["fixed_plan_status"] = stop_status
        session.result["scene4_cold_start"] = result
        session.result["execution_complete"] = len(result["cases"]) == 4
        session.result["measurement_evidence_complete"] = result["measurement_complete"]
        session.result["case_results"] = result["cases"]
        report_counts = aggregate if counts_observed else {key: None for key in aggregate}
        session.result["counts"] = ({key: None for key in (
            "scene4_primary_startup_failure", "scene4_primary_oom_kill",
            "scene4_owned_confirmed_oom_kill", "scene4_confirmed_unattributed_kernel_victim",
            "scene4_unknown_kernel_victim_marks", "scene4_primary_native_popup",
            "scene4_test_popup_separate", "scene4_unknown_timeout_or_gap",
            "scene4_primary_count", "scene4_unique_incidents",
            "scene4_other_oom_side_effects")} if donor_proof_only else {
            "scene4_primary_startup_failure": report_counts["startup_failure"],
            "scene4_primary_oom_kill": report_counts["confirmed_oom_kill"],
            "scene4_owned_confirmed_oom_kill": report_counts["owned_confirmed_oom_kill"],
            "scene4_confirmed_unattributed_kernel_victim": report_counts["confirmed_unattributed_kernel_victim"],
            "scene4_unknown_kernel_victim_marks": report_counts["unknown_kernel_victim_marks"],
            "scene4_primary_native_popup": report_counts["native_low_memory_popup"],
            "scene4_test_popup_separate": report_counts["test_popup_separate"],
            "scene4_unknown_timeout_or_gap": report_counts["unknown_timeout_or_gap"],
            "scene4_primary_count": report_counts["primary_count"],
            "scene4_unique_incidents": report_counts["unique_incidents"],
            "scene4_other_oom_side_effects": report_counts["other_oom_side_effects"]})
    except (Exception, KeyboardInterrupt) as exc:
        monitor_stop.set()
        if window_thread is not None and window_thread.is_alive():
            window_thread.join(timeout=5)
        result["error"] = str(exc)
        result["measurement_complete"] = False
        result["unknown_is_not_zero"] = True
        result["partial_run_marker"] = {"status": "aborted_or_invalid",
            "primary_count": None, "primary_count_observed": False,
            "completed_case_count": len(result.get("cases", []))}
        result["partial_frame_manifests"] = {case_id: {
            "manifest_path": state.get("manifest_path"),
            "review_path": state.get("review_path"),
            "durably_recorded_frame_rows": len(state.get("rows", [])),
            "worker_error": state.get("error")}
            for case_id, state in session.scene4_frame_workers.items()}
        try:
            result["latest_checkpoint"] = _scene4_write_checkpoint(
                session.output, result, "aborted_or_invalid", str(exc))
        except Exception as checkpoint_exc:
            result["checkpoint_write_error"] = str(checkpoint_exc)
        session.result["scene4_cold_start"] = result
        session.result["execution_complete"] = False
        session.result["measurement_evidence_complete"] = False
        session.result["invalid_reasons"].append("scene4_oracle_cold_start:" + str(exc))
    finally:
        if active_reader_monitor is not None:
            try:
                active_reader_monitor["stop"].set()
                if active_reader_monitor.get("thread"):
                    active_reader_monitor["thread"].join(timeout=10)
            except Exception:
                pass
        for frame_state in session.scene4_frame_workers.values():
            frame_state["stop"].set()
            frame_worker = frame_state.get("worker")
            if frame_worker is not None and frame_worker.is_alive():
                frame_worker.join(timeout=40)
        if active_ui_sampler_stop is not None:
            active_ui_sampler_stop.set()
        if active_ui_sampler_thread is not None and active_ui_sampler_thread.is_alive():
            active_ui_sampler_thread.join()
        if active_schedule_writer_stop is not None:
            active_schedule_writer_stop.set()
        if active_schedule_writer_thread is not None and active_schedule_writer_thread.is_alive():
            active_schedule_writer_thread.join()
        monitor_stop.set()
        if window_thread is not None and window_thread.is_alive():
            window_thread.join(timeout=10)
        if active_launch_cancel is not None:
            active_launch_cancel.set()
        if active_launch_timer is not None and active_launch_timer.is_alive():
            # A dispatched launch thread can still be inside an RPC. Wait for
            # that RPC before cleanup so no new owned process appears later.
            active_launch_timer.join()
        if donor_proof_only and "donor_proof_only_probe" in result:
            result["donor_proof_only_probe"]["target_launch_timer_created"] = False
        if "schedule_remote" in locals() and schedule_remote:
            try:
                invalidation = _scene4_invalidate_schedule(session, schedule_remote)
            except Exception as exc:
                invalidation = {"status": "invalidation_error", "verified": False, "error": str(exc)}
            result["schedule_invalidation"] = invalidation
            if not invalidation.get("verified"):
                result["measurement_complete"] = False
                result.setdefault("measurement_completeness", {})["schedule_invalidation_verified"] = False
                session.result["measurement_evidence_complete"] = False
            else:
                result.setdefault("measurement_completeness", {})["schedule_invalidation_verified"] = True
        if not result.get("measurement_complete"):
            result.setdefault("partial_run_marker", {"status": "partial_or_invalid",
                "primary_count": None, "primary_count_observed": False,
                "completed_case_count": len(result.get("cases", []))})
            result["partial_frame_manifests"] = {case_id: {
                "manifest_path": state.get("manifest_path"),
                "review_path": state.get("review_path"),
                "durably_recorded_frame_rows": len(state.get("rows", [])),
                "worker_finished": bool(state.get("worker") and not state["worker"].is_alive()),
                "worker_error": state.get("error")}
                for case_id, state in session.scene4_frame_workers.items()}
        _scene4_finalize_cumulative_counts(result)
        try:
            checkpoint_status = "completed" if result.get("measurement_complete") else \
                result.get("partial_run_marker", {}).get("status", "partial_or_invalid")
            result["latest_checkpoint"] = _scene4_write_checkpoint(
                session.output, result, checkpoint_status, result.get("error"))
        except Exception as checkpoint_exc:
            result["checkpoint_write_error"] = str(checkpoint_exc)
        session.cleanup()
    return session.finish()

def calibrate_scene4(session: Session, output: Path) -> Path:
    """One-app peak check followed by an eight-app cold daily-base residency check."""
    cfg = session.config["scene4"]
    report: dict[str, Any] = {
        "schema": "memscenes-scene4-calibration-v1", "scene": 4,
        "scope": "candidate_capacity_and_daily_cold_base_only",
        "suite_hash": session.result["suite_hash"], "single_app": None,
        "eight_app_daily_base": None, "measurement_complete": False,
    }
    try:
        env = validate_environment(session)
        session.device.dismiss_owned_install_prompts(session.remote + "/install_prompts.json")
        session.enable_oom_trace()
        session.start_kernel_dmesg_reader()
        peak = int(cfg["single_app_peak_mib"])
        file_mib = int(cfg["file_mib"])
        single_case_id = f"{session.id}_single0_l0"
        single = scene2_case(session, single_case_id, [session.config["test_apps"][0]],
                             peak, int(cfg["calibration_hold_seconds"]),
                             "concurrent_launch_peak_hold_notify", int(cfg["seed"]),
                             "scene4_single_calibration", file_mib)
        single_row = single["measurements"][0]
        single_sync = single.get("file_sync_proofs", {}).get(session.config["test_apps"][0], {})
        single_peak_snapshot = next((snapshot for snapshot in single.get("reclaim_diagnostics", {}).get("snapshots", [])
                                     if snapshot.get("phase") == "peak_reached"), None)
        single_peak_identity = None
        if single_peak_snapshot:
            identity_rows = single_row.get("process_identities", [])
            peak_identity = next((row for row in identity_rows
                                  if row.get("bundle") == session.config["test_apps"][0] and
                                  row.get("first_absent_uptime") is None), None)
            peak_pid = int(peak_identity.get("pid", -1)) if peak_identity else -1
            peak_ticks = str(peak_identity.get("start_ticks", "")) if peak_identity else ""
            single_peak_identity = next((item for key, item in single_peak_snapshot.get("apps", {}).get(
                session.config["test_apps"][0], {}).get("processes", {}).items()
                if key == f"{peak_pid}:{peak_ticks}"), None)
        report["single_app"] = {
            "case_id": single_case_id, "bundle": session.config["test_apps"][0],
            "peak_mib": peak, "file_mib": file_mib,
            "measurement": single_row, "file_sync": single_sync,
            "peak_file_vma_snapshot": single_peak_identity,
            "reclaim_diagnostics": single.get("reclaim_diagnostics"),
            "case_measurement_complete": single.get("case_measurement_complete"),
        }
        expected_file_bytes = file_mib * 1024**2
        single_ok = (single_row.get("outcome") == "success" and single_row.get("peak_observed") and
                     single_row.get("ready_observed") and
                     single_row.get("journal_touched_peak_bytes", 0) >= peak * 1024**2 and
                     single_sync.get("daily_sync_success") and
                     single_sync.get("daily_synced_bytes", 0) >= expected_file_bytes and
                     single_peak_identity is not None and
                     single_peak_identity.get("owned_file_vma_evidence_complete") is True and
                     single_peak_identity.get("owned_file_vma_size_bytes") == expected_file_bytes)
        report["single_app"]["file_vma_verified_at_peak"] = bool(
            single_peak_identity and single_peak_identity.get("owned_file_vma_evidence_complete") is True)

        bundles = list(session.config["test_apps"])
        base_case_id = f"{session.id}_c0001"
        session.owned_page_files.update((bundle, base_case_id) for bundle in bundles)
        daily_errors: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(bundles)) as pool:
            futures = {pool.submit(session.launch_test, bundle, base_case_id,
                                   int(cfg["daily_mib"]), int(cfg["peak_mib"]),
                                   int(cfg["hold_seconds"]), int(cfg["lifetime_seconds"]),
                                   file_mib=file_mib, seed=int(cfg["seed"]), await_peak=True): bundle
                       for bundle in bundles}
            for future, bundle in futures.items():
                try:
                    future.result()
                except Exception as exc:
                    daily_errors[bundle] = str(exc)
        deadline = time.monotonic() + int(cfg["ready_timeout_seconds"])
        ready_rows: dict[str, dict] = {}
        while time.monotonic() < deadline and len(ready_rows) + len(daily_errors) < len(bundles):
            for bundle in bundles:
                if bundle in ready_rows or bundle in daily_errors:
                    continue
                _, rows = session.device.journal(bundle, base_case_id + "_daily")
                ready = next((row for row in reversed(rows) if row.get("event") == "daily_ready"), None)
                if ready:
                    ready_rows[bundle] = ready
                elif any(row.get("event") == "allocation_failed" for row in rows):
                    daily_errors[bundle] = "daily_allocation_failed"
            if len(ready_rows) + len(daily_errors) < len(bundles):
                time.sleep(0.5)
        for bundle in bundles:
            if bundle not in ready_rows and bundle not in daily_errors:
                daily_errors[bundle] = "daily_ready_timeout"
        daily_barrier = len(ready_rows) == len(bundles) and not daily_errors
        identities_before: dict[str, Any] = {}
        for bundle, row in ready_rows.items():
            pid = int(row.get("pid", -1))
            observed = session.device.inspect_process_identity(pid, timeout=20) if pid > 0 else {}
            identity = observed.get("identity")
            identity_error = proc_identity_mismatch(identity, pid, int(row.get("uid", -1)),
                                                    row.get("start_ticks")) if pid > 0 else "daily_ready_pid_missing"
            identities_before[bundle] = {"ready": row, "identity": identity,
                                         "verified": identity_error is None,
                                         "error": identity_error,
                                         "raw_response": observed.get("raw", "")}
            if identity_error:
                daily_errors[bundle] = identity_error
        daily_barrier = daily_barrier and not daily_errors
        snapshots: list[dict[str, Any]] = []
        if daily_barrier:
            snapshots.append(scene4_reclaim_snapshot(session, base_case_id, bundles, "daily_cold_begin"))
            time.sleep(float(cfg["daily_cold_seconds"]))
            snapshots.append(scene4_reclaim_snapshot(session, base_case_id, bundles, "daily_cold_end"))
        daily_vma_summary: dict[str, Any] = {}
        daily_end_snapshot = next((snapshot for snapshot in reversed(snapshots)
                                   if snapshot.get("phase") == "daily_cold_end"), None)
        if daily_barrier and daily_end_snapshot:
            for bundle in bundles:
                ready_row = ready_rows.get(bundle, {})
                before_identity = identities_before.get(bundle, {}).get("identity") or {}
                after_identity = identities_after.get(bundle, {}).get("identity") or {}
                expected_identity = f"{before_identity.get('pid')}:{before_identity.get('start_ticks')}"
                process = daily_end_snapshot.get("apps", {}).get(bundle, {}).get("processes", {}).get(expected_identity)
                daily_vma_summary[bundle] = process or {"verified": False, "reason": "ready_identity_missing_from_daily_snapshot"}
                if (int(before_identity.get("pid", -1)) != int(ready_row.get("pid", -2)) or
                        before_identity.get("start_ticks") != after_identity.get("start_ticks") or
                        before_identity.get("pid") != after_identity.get("pid") or
                        before_identity.get("uid") != after_identity.get("uid")):
                    daily_errors[bundle] = "daily_process_identity_not_continuous_across_cold_window"
                if (not process or process.get("owned_file_vma_evidence_complete") is not True or
                        process.get("owned_file_vma_size_bytes") != expected_file_bytes):
                    daily_errors.setdefault(bundle, "daily_owned_file_vma_or_inode_evidence_incomplete")
        identities_after: dict[str, Any] = {}
        if daily_barrier:
            for bundle, prior in identities_before.items():
                pid = int(prior["ready"].get("pid", -1))
                observed = session.device.inspect_process_identity(pid, timeout=20) if pid > 0 else {}
                identity = observed.get("identity")
                error = proc_identity_mismatch(identity, pid, int(prior["ready"].get("uid", -1)),
                                               prior["ready"].get("start_ticks")) if pid > 0 else "daily_ready_pid_missing"
                identities_after[bundle] = {"identity": identity, "verified": error is None,
                                            "error": error, "raw_response": observed.get("raw", "")}
                if error:
                    daily_errors[bundle] = error
            daily_barrier = not daily_errors
        file_sync: dict[str, Any] = {}
        for bundle in bundles:
            _, rows = session.device.journal(bundle, base_case_id + "_daily")
            synced = max((int(row.get("file_synced_bytes", 0)) for row in rows), default=0)
            file_sync[bundle] = {"expected_bytes": expected_file_bytes,
                                 "synced_bytes": synced,
                                 "success_event": any(row.get("event") == "file_sync_complete" for row in rows),
                                 "daily_ready": any(row.get("event") == "daily_ready" for row in rows)}
            if file_sync[bundle]["synced_bytes"] < expected_file_bytes or not file_sync[bundle]["success_event"]:
                daily_errors[bundle] = "file_backing_msync_not_verified"
        daily_complete = daily_barrier and not daily_errors and all(
            row.get("daily_ready") for row in file_sync.values())
        report["eight_app_daily_base"] = {
            "case_id": base_case_id, "daily_mib_per_app": int(cfg["daily_mib"]),
            "file_mib_per_app": file_mib,
            "anonymous_mib_per_app": int(cfg["daily_mib"]) - file_mib,
            "requested_cold_seconds": float(cfg["daily_cold_seconds"]),
            "daily_barrier_complete": daily_barrier,
            "measurement_complete": daily_complete,
            "errors": daily_errors, "identities_before": identities_before,
            "identities_after": identities_after, "file_sync": file_sync,
            "owned_file_vma_at_daily_cold_end": daily_vma_summary,
            "snapshots": snapshots,
        }
        # The daily-only group is deliberately never promoted during this calibration.
        owned_cleanup = {}
        for bundle in bundles:
            try:
                session.device.force_stop(bundle)
                remaining = session.device.app_processes(bundle)
                pages = session.device.remove_owned_case_pages(bundle, base_case_id)
                owned_cleanup[bundle] = {"empty": not bool(remaining), "remaining": list(remaining.values()),
                                         "pages_absent": bool(pages.get("absent")), "page_path": pages.get("path")}
            except Exception as exc:
                owned_cleanup[bundle] = {"empty": False, "pages_absent": False, "error": str(exc)}
        report["eight_app_daily_base"]["cleanup"] = owned_cleanup
        report["eight_app_daily_base"]["cleanup_complete"] = all(
            row.get("empty") and row.get("pages_absent") for row in owned_cleanup.values())
        report["measurement_complete"] = bool(
            single_ok and daily_complete and report["eight_app_daily_base"]["cleanup_complete"])
        report["environment"] = env
        report["not_a_reclaim_benefit_claim"] = True
        session.result["scene4_calibration"] = report
        session.result["execution_complete"] = True
        session.result["measurement_evidence_complete"] = report["measurement_complete"]
        save(output, report)
    except (Exception, KeyboardInterrupt) as exc:
        session.result["invalid_reasons"].append("scene4_calibration:" + str(exc))
        session.result["execution_complete"] = False
        session.result["measurement_evidence_complete"] = False
    finally:
        session.cleanup()
    return session.finish()

def calibrate(session: Session, output: Path) -> Path:
    """Measure each app in isolation. Calibration never fabricates fault evidence."""
    try:
        validate_environment(session)
        session.device.dismiss_owned_install_prompts(session.remote + "/install_prompts.json")
        cfg = session.config["scene2"]
        measured = []
        level_history = []
        profiles = calibration_profiles(session.config)
        if not profiles:
            raise RuntimeError("固定用例没有可校准的峰值档位")
        level_checks = {}
        for level_index, profile in enumerate(profiles):
            level_rows = []
            for app_index, bundle in enumerate(session.config["test_apps"]):
                case_id = f"{session.id}_single{app_index}_l{level_index}"
                result = scene2_case(session, case_id, [bundle], profile["peak_mib"],
                                     int(cfg.get("calibration_hold_seconds", 15)),
                                     seed=cfg.get("seed", 1))
                row = result["measurements"][0]
                evidence = {"bundle": bundle, "peak_mib": profile["peak_mib"],
                            "calibration_hold_seconds": int(cfg.get("calibration_hold_seconds", 15)),
                            "suite_hold_seconds": profile["hold_seconds"],
                            "used_by_kinds": profile["kinds"], "case_id": case_id, **row}
                level_history.append(evidence)
                level_rows.append(row)
                measured.append(evidence)
            level_checks[str(profile["peak_mib"])] = combination_checks(
                level_rows, int(session.config["dram_gib"] * GIB))
        errors = [f"peak_{peak}:{error}" for peak, check in level_checks.items()
                  for error in check["errors"]]
        checks = {"valid": not errors, "errors": errors,
                  "by_peak_mib": level_checks,
                  "required_peak_mib": [profile["peak_mib"] for profile in profiles]}
        session.result["combination_checks"] = checks
        if not checks["valid"] and session.profile == "acceptance":
            session.result["invalid_reasons"].extend(checks["errors"])
        calibration = {"schema_version": 1, "suite_hash": session.result["suite_hash"],
                       "profile": session.profile, "measurements": measured, "checks": checks,
                       "level_history": level_history, "settings": cfg,
                       "calibration_profiles": profiles,
                       "environment": session.result["environment"]}
        save(output, calibration)
        session.result["execution_complete"] = True
    except (Exception, KeyboardInterrupt) as exc:
        session.result["invalid_reasons"].append(str(exc))
    finally:
        session.cleanup()
    return session.finish()

def freeze(result_path: Path, calibration_path: Path, destination: Path,
           audit_path: Path | None = None) -> None:
    run = read(result_path)
    calibration = read(calibration_path)
    if run["scene"] != 2 or run["phase"] != "baseline" or not run.get("execution_complete"):
        raise RuntimeError("只能冻结完成的场景2 qualification baseline")
    audit = None
    if audit_path is not None:
        from tools.audit_scene2_ui import verify_audit_document
        verified, verify_reason = verify_audit_document(audit_path)
        if not verified:
            raise RuntimeError(f"qualification审计来源或哈希校验失败：{verify_reason}")
        audit = read(audit_path)
        audit_source = Path(audit.get("source_result", ""))
        if not audit_source.is_absolute():
            audit_source = (audit_path.parent / audit_source).resolve()
        if audit_source.resolve() != result_path.resolve():
            raise RuntimeError("qualification审计没有绑定本次原始result.json")
        if (audit.get("schema") != "memscenes-scene2-ui-audit-v1" or
                not audit.get("primary_evidence_complete") or
                not audit.get("case_measurement_complete") or
                not audit.get("progress_complete") or
                not audit.get("case_results_content_matches") or
                not audit.get("journal_manifest", {}).get("all_pairs_complete")):
            raise RuntimeError("qualification离线审计未证明全部固定用例和原始主证据完整")
        if not audit.get("qualification_eligible"):
            raise RuntimeError("去重遮挡后仍未满足3类触发及分类事件总数门槛")
        if run.get("qualification_scope") != "prevalidation_32gb" or audit.get("qualification_scope") != "prevalidation_32gb":
            raise RuntimeError("该离线审计只允许冻结32GiB预验证scope")
        physical_mem_total = run.get("environment", {}).get("meminfo", {}).get("MemTotal")
        configured_dram = run.get("config", {}).get("dram_gib", run.get("suite", {}).get("dram_gib"))
        if (configured_dram != 32 or physical_mem_total is None or
                not int(0.9 * 32 * GIB) <= int(physical_mem_total) <= 32 * GIB):
            raise RuntimeError("32GiB预验证冻结必须与实测物理MemTotal相符")
        counts = audit["counts_after"]
        if run.get("profile") != "qualification":
            raise RuntimeError("审计冻结仅适用于qualification profile")
    else:
        if not run.get("qualification_eligible") or run.get("profile") != "qualification":
            raise RuntimeError("qualification基线未满足配置容量相符的实测、完整测量、三类触发、分类事件合计>=100和固定用例门槛")
        if run.get("qualification_scope") == "prevalidation_32gb":
            raise RuntimeError("32GiB预验证冻结必须提供与原始结果绑定的离线UI证据审计")
        counts = run["counts"]
    if (run["suite_hash"] != calibration["suite_hash"] or not calibration["checks"]["valid"] or
            calibration.get("profile") != "qualification"):
        raise RuntimeError("缺少同配置qualification校准或独立单应用峰值不满足容量要求")
    baseline_env = run.get("environment", {})
    calibration_env = calibration.get("environment", {})
    if (baseline_env.get("target") != calibration_env.get("target") or
            baseline_env.get("meminfo", {}).get("MemTotal") !=
            calibration_env.get("meminfo", {}).get("MemTotal")):
        raise RuntimeError("基线与校准的实测设备或物理MemTotal不一致")
    if any(counts[key] == 0 for key in ("startup_failure", "oom_kill", "low_memory_popup")):
        raise RuntimeError("三类故障尚未全部复现，不能冻结")
    if counts["total_events"] < 100:
        raise RuntimeError("分类事件计数合计不足100次，不能冻结")
    if not run.get("cases_hash") or run["cases_hash"] != cases_hash(run["cases"]):
        raise RuntimeError("基线用例集合哈希不匹配")
    def package_reference(path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            return str(resolved)

    save(destination, {"schema_version": 2, "qualification_scope": run["qualification_scope"],
                       "dram_gib": run["config"]["dram_gib"] if "config" in run else run["suite"]["dram_gib"],
                       "environment": baseline_env,
                       "qualification_source": package_reference(result_path),
                       "qualification_source_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                       "suite_hash": run["suite_hash"], "suite": run["suite"],
                       "cases": run["cases"], "cases_hash": run["cases_hash"],
                       "calibration": calibration, "calibration_source": package_reference(calibration_path),
                       "calibration_sha256": hashlib.sha256(calibration_path.read_bytes()).hexdigest(),
                       "baseline_source": package_reference(result_path),
                       "qualification_counts": counts,
                       "raw_measurement_complete": run.get("measurement_complete"),
                       "raw_measurement_evidence_complete": run.get("measurement_evidence_complete"),
                       "ui_audit_source": package_reference(audit_path) if audit_path else None,
                       "ui_audit_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest() if audit_path else None,
                       "ui_audit_source_hashes": audit.get("source_hashes") if audit else None,
                       "prevalidation_only": run["qualification_scope"] == "prevalidation_32gb"})

def _comparison_rows(paths: list[Path], audit_paths: list[Path] | None, label: str) -> list[dict]:
    rows = [read(path) for path in paths]
    if audit_paths is None:
        audit_paths = [None] * len(paths)
    if len(audit_paths) != len(paths):
        raise RuntimeError(f"{label} audit数量必须与对应result数量一致")
    for path, row, audit_path in zip(paths, rows, audit_paths):
        row["_compare_counts"] = row.get("counts", {})
        row["_compare_measurement_complete"] = bool(row.get("measurement_complete"))
        row["_compare_acceptance_valid"] = bool(row.get("acceptance_valid"))
        row["_compare_status_valid"] = row.get("status") == "valid"
        if audit_path is None:
            continue
        if row.get("scene") != 2:
            raise RuntimeError("离线UI审计仅支持场景2 compare")
        from tools.audit_scene2_ui import verify_audit_document
        verified, reason = verify_audit_document(audit_path)
        if not verified:
            raise RuntimeError(f"{label} audit/source哈希验证失败：{reason}")
        audit = read(audit_path)
        audit_source = Path(audit.get("source_result", ""))
        if not audit_source.is_absolute():
            audit_source = (audit_path.parent / audit_source).resolve()
        if audit_source.resolve() != path.resolve():
            raise RuntimeError(f"{label} audit未绑定对应原始result")
        expected_source_hash = audit.get("source_hashes", {}).get("result_json")
        if not expected_source_hash or hashlib.sha256(path.read_bytes()).hexdigest() != expected_source_hash:
            raise RuntimeError(f"{label} audit原始result哈希不匹配")
        if (audit.get("schema") != "memscenes-scene2-ui-audit-v1" or
                not audit.get("primary_evidence_complete") or
                not audit.get("case_measurement_complete") or
                not audit.get("progress_complete") or
                not audit.get("case_results_content_matches") or
                not audit.get("journal_manifest", {}).get("all_pairs_complete")):
            raise RuntimeError(f"{label} audit没有证明完整的场景2主证据")
        if audit.get("source_run_id") != row.get("session"):
            raise RuntimeError(f"{label} audit run/session不匹配")
        if audit.get("source_hashes", {}).get("cases_canonical") != row.get("cases_hash"):
            raise RuntimeError(f"{label} audit用例集合哈希不匹配")
        row["_compare_counts"] = audit["counts_after"]
        # An audit can repair evidence coverage, never establish formal acceptance:
        # profile, 16GiB hardware, phase, and all other compare gates still apply.
        row["_compare_measurement_complete"] = True
        row["_compare_acceptance_valid"] = bool(
            row.get("profile") == "acceptance" and
            row.get("config", {}).get("dram_gib", row.get("suite", {}).get("dram_gib")) == 16 and
            audit.get("primary_evidence_complete"))
        row["_compare_status_valid"] = row["_compare_acceptance_valid"]
        row["_compare_audit"] = {"path": str(audit_path),
                                 "sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
                                 "source_result_sha256": expected_source_hash,
                                 "counts": audit["counts_after"]}
    return rows


def compare(baseline: list[Path], optimized: list[Path], output: Path,
            baseline_audits: list[Path] | None = None,
            optimized_audits: list[Path] | None = None) -> dict:
    before = _comparison_rows(baseline, baseline_audits, "baseline")
    after = _comparison_rows(optimized, optimized_audits, "optimized")
    scene = before[0]["scene"]
    expected = 10 if scene == 1 else 3
    reasons = []
    if len(before) != expected or len(after) != expected:
        reasons.append(f"requires_{expected}_rounds_per_version")
    if len({row.get("session") for row in before}) != len(before) or len({row.get("session") for row in after}) != len(after):
        reasons.append("duplicate_session_rounds")
    if len({str(path.resolve()) for path in baseline}) != len(baseline) or len({str(path.resolve()) for path in optimized}) != len(optimized):
        reasons.append("duplicate_result_files")
    hashes = {row["suite_hash"] for row in before + after}
    if len(hashes) != 1 or any(row["scene"] != scene for row in before + after):
        reasons.append("workload_mismatch")
    if any(not row.get("_compare_status_valid", row.get("status") == "valid") or
           row["profile"] != "acceptance" for row in before + after):
        reasons.append("contains_unverified_or_prevalidation_rounds")
    if any(not row.get("_compare_acceptance_valid", row.get("acceptance_valid")) or
           not row.get("_compare_measurement_complete", row.get("measurement_complete"))
           for row in before + after):
        reasons.append("measurement_incomplete")
    if any(row["phase"] != "baseline" for row in before) or any(row["phase"] != "optimized" for row in after):
        reasons.append("version_phase_mismatch")
    if scene == 2:
        case_hashes = {row.get("cases_hash", cases_hash(row.get("cases", []))) for row in before + after}
        if len(case_hashes) != 1 or any(row.get("cases_hash") != cases_hash(row.get("cases", [])) for row in before + after):
            reasons.append("case_set_mismatch")
    devices = {row.get("environment", {}).get("target") for row in before + after}
    totals = {row.get("environment", {}).get("meminfo", {}).get("MemTotal") for row in before + after}
    uids = {canonical_hash(row.get("app_uids", {})) for row in before + after}
    if len(devices) != 1 or len(totals) != 1 or len(uids) != 1:
        reasons.append("device_capacity_or_app_identity_mismatch")
    def value(row):
        return row.get("reuse_faults", {}).get("total", 0) if scene == 1 else row["_compare_counts"]["total_events"]
    old = statistics.mean(value(row) for row in before)
    new = statistics.mean(value(row) for row in after)
    reduction = (old - new) / old if old else None
    if not old:
        reasons.append("zero_baseline")
    report = {"scene": scene, "valid": not reasons, "reasons": reasons, "baseline_mean": old,
              "optimized_mean": new, "reduction": reduction,
              "target_met": not reasons and reduction is not None and reduction >= (0.2 if scene == 1 else 0.3),
              "challenge_met": not reasons and scene == 1 and reduction is not None and reduction >= 0.3,
              "baseline_files": [str(x) for x in baseline], "optimized_files": [str(x) for x in optimized],
              "baseline_audits": [row.get("_compare_audit") for row in before],
              "optimized_audits": [row.get("_compare_audit") for row in after],
              "baseline_event_counts": [row["_compare_counts"].get("total_events") for row in before] if scene == 2 else None,
              "optimized_event_counts": [row["_compare_counts"].get("total_events") for row in after] if scene == 2 else None}
    save(output, report)
    return report
