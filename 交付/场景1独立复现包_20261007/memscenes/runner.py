from __future__ import annotations
import concurrent.futures
import csv
import hashlib
import json
import re
import secrets
import statistics
import subprocess
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .device import Device, quote, ui_nodes, focused_bundles
from .model import (ORDER, GIB, EventBook, canonical_hash, key_values,
                    fault_delta, disk_bytes, eswap_bytes, json_lines, combination_checks,
                    release_precedes_reuse, daily_promotion_targets, attempt_outcome,
                    attempts_complete, proc_identity_mismatch)

ROOT = Path(__file__).resolve().parents[1]
NATIVE_LOW_MEMORY_OWNERS = {"com.huawei.hmos.taskmanager"}

def parse_low_memory_missions(raw: str) -> list[dict]:
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
            missions.append({"mission_id": mission.group(1), "bundle": bundle.group(1),
                "ability": ability.group(1), "state": state.group(1),
                "start_uptime_seconds": int(start.group(1)) / 1000.0})
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

def visible_native_low_memory_modal(nodes: list[dict], patterns: list[str]) -> bool:
    return any(row.get("__ownerBundleName") in NATIVE_LOW_MEMORY_OWNERS and
               str(row.get("visible", "true")).lower() == "true" and
               any(pattern.lower() in " ".join(str(row.get(field, "")) for field in
                                                ("text", "content", "description")).lower()
                   for pattern in patterns)
               for row in nodes)

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
        self.owned_page_files: set[tuple[str, str]] = set()
        self.book = EventBook()
        self.ui_observations: dict[str, list[dict]] = {}
        self.popup_baselines: dict[str, dict] = {}
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
            self.result["scene4_scope"] = "cold_reclaim_sensitivity_prevalidation"
            self.result["scene4_candidate_only"] = True
        save(self.output / "config.json", config)

    def mark(self, name: str, **extra) -> dict:
        uptime = self.device.shell("cat /proc/uptime").split()[0]
        item = {"name": name, "device_uptime_seconds": float(uptime), **extra}
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

    def collect_kernel_dmesg_case(self, case_id: str, observed: dict[int, list[dict]],
                                  targets: list[str], attempts: dict[str, list[dict]],
                                  start_uptime: float, end_uptime: float,
                                  anchor_wall_ms: int, anchor_uptime: float) -> dict:
        path = self.kernel_dmesg_path
        end_token = f"MEMSCENES_CASE_END session={self.id} case_id={case_id} "
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
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
            anchor_wall_ms, anchor_uptime, str(path.relative_to(self.output)))
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
        sample = {"label": label, "device_uptime_seconds": float(d.shell("cat /proc/uptime").split()[0]),
                  "meminfo": key_values(d.shell("cat /proc/meminfo")),
                  "vmstat": key_values(d.shell("cat /proc/vmstat")),
                  "disk_write_bytes": disk_bytes(d.shell("cat /sys/block/zram0/hyperhold_io_info_file", check=False)),
                  "apps": {}}
        for bundle in bundles:
            processes = d.app_processes(bundle)
            raw = d.shell("cat " + quote(d.app(bundle)["cgroup"] + "/memory.stat"), check=False)
            sample["apps"][bundle] = {"processes": processes, "memory_stat": key_values(raw),
                                      "eswap_bytes": eswap_bytes(raw)}
            for identity, process in processes.items():
                for kind in ("minor", "major"):
                    marker = f"C|{process['pid']}|memscenes/{bundle}/{identity}/{kind}|{process[kind]}"
                    d.shell("echo " + quote(marker) + " > /sys/kernel/debug/tracing/trace_marker", check=False)
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
                    await_peak: bool = False) -> None:
        self.device.app(bundle)
        self.device.launch(bundle, params={"case_id": case_id, "daily_mib": daily,
                           "peak_mib": peak, "file_mib": file_mib, "hold_seconds": hold,
                           "lifetime_seconds": lifetime, "seed": seed, "bundle": bundle,
                           "daily_case_id": case_id + "_daily", "await_peak": int(await_peak)})

    def ui_evidence(self, case_id: str, targets: list[str], observation_phase: str = "unspecified",
                    allow_case_attribution: bool = True) -> set[str]:
        observation = {"case_id": case_id, "phase": observation_phase,
                       "host_monotonic": time.monotonic(), "success": False,
                       "case_attribution_allowed": allow_case_attribution}
        try:
            tree = self.device.dump_ui(self.remote + "/layout.json", timeout=45)
        except Exception as exc:
            observation["error"] = str(exc)
            self.ui_observations.setdefault(case_id, []).append(observation)
            self.result.setdefault("ui_evidence_errors", []).append({"case_id": case_id,
                "phase": observation_phase, "error": str(exc)})
            return set()
        path = self.output / "ui" / f"{case_id}_{len(list((self.output / 'ui').glob('*.json'))) if (self.output / 'ui').exists() else 0}.json"
        save(path, tree)
        observation["layout"] = str(path.relative_to(self.output))
        observation["layout_captured"] = True
        try:
            capture_uptime, capture_wall_ms = self.device.clock_pair()
            observation.update(capture_uptime_seconds=capture_uptime, capture_wall_ms=capture_wall_ms)
        except Exception as exc:
            observation["clock_error"] = str(exc)
            capture_uptime = capture_wall_ms = None
        try:
            mission_raw = self.device.shell("aa dump -l", timeout=10, check=False)
        except Exception as exc:
            mission_raw = ""
            observation["mission_error"] = str(exc)
            self.result.setdefault("ui_evidence_errors", []).append({"case_id": case_id,
                "phase": observation_phase, "error": str(exc), "source": "mission_list"})
        mission_path = self.output / "ui" / f"{case_id}_{len(list((self.output / 'ui').glob('*.txt')))}_missions.txt"
        mission_path.write_text(mission_raw, encoding="utf-8")
        observation["mission_list"] = str(mission_path.relative_to(self.output))
        missions = parse_low_memory_missions(mission_raw)
        baseline = self.popup_baselines.get(case_id, {})
        case_window_open = popup_attribution_in_window(
            allow_case_attribution, capture_wall_ms,
            baseline.get("case_begin_wall_ms"), baseline.get("case_end_ms"))
        candidate = None
        if case_window_open:
            candidate = native_low_memory_popup_candidate(
                list(ui_nodes(tree)), missions, set(baseline.get("mission_ids", [])),
                baseline.get("case_begin_uptime_seconds"), capture_uptime)
        observation["native_candidate"] = candidate
        found = []
        for row in ui_nodes(tree):
            text = " ".join(str(row.get(field, "")) for field in ("text", "content", "description"))
            if any(pattern.lower() in text.lower() for pattern in self.config["popup_text_patterns"]):
                found.append(row)
        focused = focused_bundles(tree)
        screenshot = path.with_suffix(".png")
        screenshot_needed = bool(found or candidate)
        observation["screenshot_needed"] = screenshot_needed
        if screenshot_needed:
            try:
                self.device.screenshot(self.remote + "/screen.png", screenshot)
                observation["screenshot"] = str(screenshot.relative_to(self.output))
                observation["screenshot_captured"] = True
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
        observation["capture_clock_valid"] = capture_uptime is not None and capture_wall_ms is not None
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
            rows = (self.journal(bundle, case_id) + self.journal(bundle, case_id + "_daily")
                    if bundle.startswith("io.memscene.workload.") else [])
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
            owned = [row for row in found if row.get("__ownerBundleName") == bundle]
            owned_visible = test_popup_is_unoccluded(
                bundle, case_id, owned, focused,
                visible_native_low_memory_modal(list(ui_nodes(tree)), self.config["popup_text_patterns"]))
            if bundle.startswith("io.memscene.workload.") and case_window_open and screenshot_in_case:
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
        self.ui_observations.setdefault(case_id, []).append(observation)
        return focused

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
        coverage = all(counts.get(key, 0) > 0 for key in ("startup_failure", "oom_kill", "low_memory_popup"))
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
        elif self.scene == 4:
            short_cfg = self.config["scene4"]
            self.result["scene4_target"] = {
                "minimum_total_events": int(short_cfg["target_total_events_min"]),
                "maximum_total_events": int(short_cfg["target_total_events_max"]),
                "category_sum": int(counts.get("total_events", 0)),
                "all_three_categories_present": coverage,
                "met_in_raw_counts": (coverage and int(short_cfg["target_total_events_min"]) <=
                                      int(counts.get("total_events", 0)) <=
                                      int(short_cfg["target_total_events_max"])),
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
            report += ["", "场景4为匿名冷页与clean文件缓存敏感性预验证，不属于场景2 qualification或正式验收。",
                       "目标区间仅供离线审计后判读；无系统策略优化/改善结论：" +
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
    if session.scene == 4:
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
        daily_total = sum(int(case["daily_mib"]) for case in cases[:1]) * apps * 1024**2
        for case in cases:
            if not 0 <= case["daily_mib"] <= case["peak_mib"] <= memtotal // (1024**2):
                raise RuntimeError("场景4单app daily/peak必须不超过本机MemTotal")
            if not 0 <= case.get("file_mib", 0) <= case["daily_mib"]:
                raise RuntimeError("场景4clean文件映射应包含在daily基线内")
            if int(case.get("file_mib", 0)) != int(cfg["daily_file_mib"]):
                raise RuntimeError("场景4每个用例的file_mib必须匹配daily_file_mib")
            if not 0 <= case["hold_seconds"] < cfg["lifetime_seconds"]:
                raise RuntimeError("场景4用例hold必须小于设备TTL")
            case_peak_total = int(case["peak_mib"]) * apps * 1024**2
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
    manifest = ROOT / ("bin/scene4_apps/manifest.json" if session.scene == 4 else "bin/apps/manifest.json")
    if manifest.is_file():
        session.result["app_build_manifest"] = read(manifest)
    return env

def scene1(session: Session) -> Path:
    d, cfg = session.device, session.config
    spec = cfg["scene1"]
    real = [cfg["real_apps"][name]["bundle"] for name in ORDER[:6]]
    background = cfg["test_apps"]
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
        before_pressure = session.snapshot("before_pressure", real + background)
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
        after_pressure = session.snapshot("after_pressure", real + background)
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
    session.owned_page_files.update((bundle, case_id) for bundle in bundles)
    popup_baseline = None
    try:
        baseline_raw = d.shell("aa dump -l", timeout=10, check=False)
        baseline_missions = parse_low_memory_missions(baseline_raw)
        popup_baseline = {"mission_ids": sorted(row["mission_id"] for row in baseline_missions),
                          "missions": baseline_missions,
                          "raw": baseline_raw}
        session.popup_baselines[case_id] = popup_baseline
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
    try:
        session.mark("case_begin", case_id=case_id, bundles=bundles, peak_mib=peak, hold_seconds=hold,
                     strategy=strategy, daily_mib=cfg["daily_mib"])
        case_begin_uptime, case_begin_wall_ms = d.clock_pair()
        if popup_baseline is not None:
            popup_baseline["case_begin_wall_ms"] = case_begin_wall_ms
            popup_baseline["case_begin_uptime_seconds"] = case_begin_uptime
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
            session.launch_test(bundle, case_id, cfg["daily_mib"], peak, hold,
                                cfg["lifetime_seconds"], file_mib=file_mib,
                                seed=seed, await_peak=True)
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
            time.sleep(float(cfg.get("daily_cold_seconds", 0)))
            reclaim_diagnostics["snapshots"].append(
                scene4_reclaim_snapshot(session, case_id, bundles, "daily_cold_end"))
            reclaim_diagnostics["daily_cold_seconds_requested"] = float(cfg.get("daily_cold_seconds", 0))
            reclaim_diagnostics["daily_cold_seconds_observed"] = time.monotonic() - daily_start
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
    promote_errors = {}
    if daily_barrier_complete:
        promote_started_ms = start_ms
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(bundles)) as pool:
            futures = {pool.submit(d.promote, bundle, case_id): bundle for bundle in bundles}
            for future, bundle in futures.items():
                try:
                    future.result()
                except Exception as exc:
                    promote_errors[bundle] = str(exc)
        if promote_errors:
            session.result.setdefault("promote_errors", []).append({"case_id": case_id, "errors": promote_errors})
        time.sleep(1)
        if strategy == "foreground_popup_with_background_pressure" and bundles:
            try:
                session.device.focus(bundles[0])
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
        if kind == "popup" and time.monotonic() - last_ui >= 45:
            focused = session.ui_evidence(case_id, bundles + [x["bundle"] for x in session.config["real_apps"].values()],
                                          observation_phase="peak")
            foreground_history.append({"time_ms": session.safe_now_ms(case_id, "popup_observation"),
                                       "host_monotonic": time.monotonic(),
                                       "bundles": sorted(focused or [])})
            last_ui = time.monotonic()
        if len(completed) == len(bundles):
            break
        time.sleep(0.5)
    # Final UI/journal evidence must precede cleanup.
    if kind == "popup":
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
    popup_observation_complete = bool(in_window_ui_observations and
                                      (kind != "popup" or len(completed) == len(bundles)))
    if not popup_observation_complete:
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
                                              wall_uptime_anchor["uptime_seconds"])
        except Exception as exc:
            session.result.setdefault("evidence_collection_errors", []).append({
                "source": "kernel_dmesg", "case_id": case_id, "error": str(exc)})
            session.result["measurement_evidence_complete"] = False
    # Startup outcomes are finalized only after dmesg/ftrace/system collection,
    # so a late-confirmed kernel OOM can be linked to the exact peak PID lifetime.
    for bundle in bundles:
        session.book.finalize_startup_attempt(
            case_id, bundle, records.get(bundle, []), observed_pids, end_ms,
            f"journals/{case_id}_{bundle}.jsonl", allow_timeout=daily_barrier_complete)
    measured = []
    for bundle in bundles:
        rows = records[bundle]
        reached = [row for row in rows if row["event"] == "peak_reached"]
        ready = [row for row in rows if row["event"] == "startup_ready"]
        daily_ready = [row for row in daily_records[bundle] if row["event"] == "daily_ready"]
        peak_begin = [row for row in rows if row["event"] == "peak_begin"]
        terminal_events = [event for event in session.book.events if event.get("case_id") == case_id and
                           event.get("bundle") == bundle and event.get("category") in
                           ("oom_kill", "memory_policy_kill", "startup_failure")]
        memory_failure = bool(terminal_events)
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
    case_evidence_complete = case_attempts_complete and case_kernel_coverage_complete
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
            d.focus(bundles[0])
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
            "target_trigger": {"category": target_category, "observed": bool(target_events),
                               "evidence": target_events},
            "popup_observation_complete": popup_observation_complete,
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

def scene4(session: Session) -> Path:
    """Run the fixed scene-4 sensitivity suite; it is diagnostic, never qualification."""
    if session.scene != 4 or session.profile != "development" or session.phase != "baseline":
        raise ValueError("场景4仅支持baseline/development敏感性预验证")
    return scene2(session, subcase="combined", frozen=None)

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



