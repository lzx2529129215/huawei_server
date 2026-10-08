from __future__ import annotations
import hashlib
import json
import math
import re
from collections import Counter
from typing import Any

MIB = 1024 ** 2
GIB = 1024 ** 3
ORDER = ("腾讯会议", "浏览器", "海泰浏览器", "文件管理器", "WPS", "应用市场", "腾讯会议", "浏览器")
COUNTS = ("startup_failure", "oom_kill", "memory_policy_kill",
          "low_memory_popup_native", "low_memory_popup_test", "startup_timeout")

def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()

def key_values(raw: str) -> dict[str, int]:
    result = {}
    for line in raw.splitlines():
        match = re.match(r"^\s*([\w().]+):?\s+(\d+)\s*(kB|MB|B)?", line)
        if match:
            multiplier = {"kB": 1024, "MB": MIB, "B": 1, None: 1}[match[3]]
            result[match[1]] = int(match[2]) * multiplier
    return result

def parse_stat(raw: str) -> dict:
    tail = raw.rpartition(") ")[2].split()
    pid = int(raw.partition(" ")[0])
    if len(tail) < 22:
        raise ValueError("Incomplete /proc/PID/stat")
    return {"pid": pid, "start_ticks": int(tail[19]), "minor": int(tail[7]),
            "major": int(tail[9]), "vsize": int(tail[20]), "rss_pages": int(tail[21])}

def parse_proc_identity(stat_raw: str, status_raw: str) -> dict:
    """Parse one direct /proc/<pid> stat+status observation."""
    process = parse_stat(stat_raw)
    match = re.search(r"(?m)^Uid:\s*(\d+)", status_raw)
    if not match:
        raise ValueError("/proc status has no real UID")
    return {**process, "uid": int(match.group(1))}

def proc_identity_mismatch(identity: dict | None, expected_pid: int, expected_uid: int,
                           expected_start_ticks: str | int | None = None) -> str | None:
    if not identity:
        return "proc_identity_unavailable"
    if int(identity.get("pid", -1)) != int(expected_pid):
        return "direct_proc_pid_mismatch"
    if int(identity.get("uid", -1)) != int(expected_uid):
        return "direct_proc_uid_mismatch"
    if expected_start_ticks is not None and str(identity.get("start_ticks")) != str(expected_start_ticks):
        return "direct_proc_start_ticks_mismatch"
    return None

def fault_delta(before: dict[str, dict], after: dict[str, dict]) -> dict:
    reasons = []
    totals = {"minor": 0, "major": 0}
    if not before:
        reasons.append("no_target_processes_at_baseline")
    for identity, old in before.items():
        new = after.get(identity)
        if new is None:
            reasons.append(f"process_exited:{identity}")
            continue
        for name in totals:
            delta = new[name] - old[name]
            if delta < 0:
                reasons.append(f"counter_regressed:{identity}:{name}")
            else:
                totals[name] += delta
    # New children can fault during reuse; their counters begin at process creation.
    for identity in after.keys() - before.keys():
        for name in totals:
            totals[name] += after[identity][name]
    return {**totals, "total": sum(totals.values()), "valid": not reasons, "reasons": reasons}

def release_precedes_reuse(markers: list[dict]) -> bool:
    released = [row.get("device_uptime_seconds", float("inf")) for row in markers
                if row.get("name") == "pressure_released"]
    reuse = [row.get("device_uptime_seconds", float("-inf")) for row in markers
             if row.get("name") == "reuse_begin"]
    return bool(released and reuse and max(released) < min(reuse))

def daily_promotion_targets(targets: list[str], daily_ready: set[str], failed: set[str]) -> list[str]:
    """Release the peak barrier only when every target is already at daily-ready."""
    if not targets or failed or set(targets) != daily_ready:
        return []
    return list(targets)

def attempt_outcome(peak_begin: bool, peak_reached: bool, ready: bool,
                    confirmed_memory_failure: bool) -> str:
    if peak_begin and peak_reached and ready:
        return "success"
    if confirmed_memory_failure:
        return "confirmed_memory_failure"
    return "unconfirmed"

def attempts_complete(outcomes: list[str], daily_barrier_complete: bool) -> bool:
    terminal = {"success", "confirmed_memory_failure"}
    if not outcomes or any(outcome not in terminal for outcome in outcomes):
        return False
    return daily_barrier_complete or all(outcome == "confirmed_memory_failure" for outcome in outcomes)

def disk_bytes(raw: str) -> int | None:
    match = re.search(r"write_total_size:\s*(\d+)", raw)
    return int(match[1]) if match else None

def eswap_bytes(raw: str) -> int | None:
    values = key_values(raw)
    for key in ("Eswap", "swapSizeCur"):
        if key in values:
            return values[key]
    return None

def combination_checks(records: list[dict], dram_bytes: int) -> dict:
    errors = []
    if not records:
        errors.append("no_measured_apps")
    for app in records:
        if not app.get("measured") or not app.get("ready_observed") or not app.get("peak_observed"):
            errors.append(f"missing_measurement:{app.get('bundle')}")
        if app.get("peak_bytes", 0) > dram_bytes:
            errors.append(f"single_app_peak_exceeds_dram:{app.get('bundle')}")
    daily = sum(app.get("daily_bytes", 0) for app in records)
    peak = sum(app.get("peak_bytes", 0) for app in records)
    if daily > dram_bytes:
        errors.append("daily_sum_exceeds_dram")
    if peak < 1.2 * dram_bytes:
        errors.append("peak_sum_below_120_percent")
    return {"valid": not errors, "errors": errors, "daily_sum_bytes": daily,
            "peak_sum_bytes": peak, "dram_bytes": dram_bytes}

def json_lines(raw: str) -> list[dict]:
    records = []
    for line in raw.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            item = json.loads(line[start:])
            if isinstance(item, dict):
                records.append(item)
        except (ValueError, TypeError):
            continue
    return records

class EventBook:
    """Count source-confirmed events, deduplicating repeated reports of one event."""
    def __init__(self):
        self.events: list[dict] = []
        self._keys: set[tuple] = set()

    def add(self, category: str, case_id: str, bundle: str, pid: int | None,
            timestamp_ms: int, source: str, reason: str, evidence: str,
            incident: str, source_id: str | None = None) -> None:
        key = (category, case_id, bundle, incident)
        if key in self._keys:
            existing = next(x for x in self.events if x["_key"] == key)
            if evidence not in existing["evidence"]:
                existing["evidence"].append(evidence)
            return
        self._keys.add(key)
        self.events.append({"_key": key, "category": category, "case_id": case_id,
                            "bundle": bundle, "pid": pid, "timestamp_ms": timestamp_ms,
                            "source": source, "reason": reason, "source_id": source_id,
                            "incident": incident, "evidence": [evidence]})

    def native_failures(self, records: list[dict], bundle: str, evidence: str,
                        peak_only: bool = False) -> None:
        for row in records:
            if (row.get("event") == "allocation_failed" and row.get("errno") == 12 and
                    (not peak_only or row.get("phase") == "peak_allocating")):
                self.add("startup_failure", row["case_id"], bundle, row["pid"],
                         row["time_ms"], "application_allocator", "ENOMEM", evidence,
                         f"{row['case_id']}:{row['pid']}")

    def system_kills(self, rows: list[dict], case_id: str, targets: dict[str, int],
                     start_ms: int, end_ms: int, evidence: str,
                     observed: dict[int, list[dict]] | None = None,
                     start_uptime: float | None = None,
                     end_uptime: float | None = None,
                     anchor_wall_ms: int | None = None,
                     anchor_uptime: float | None = None) -> None:
        for row in rows:
            try:
                stamp = int(row.get("TIMESTAMP", row.get("time_", 0)))
            except (TypeError, ValueError):
                continue
            bundle = row.get("BUNDLE_NAME", row.get("PROCESS_NAME", "")).split(":")[0]
            if bundle not in targets or not start_ms <= stamp <= end_ms:
                continue
            if row.get("name_") not in ("PROCESS_KILL", "APP_KILLED", "OOM", "OOM_KILL"):
                continue
            reason = str(row.get("REASON", "")) + " " + str(row.get("INNER_MSG", ""))
            if re.search(r"low.?memory|memory.?pressure|内存不足|low.?mem", reason, re.I):
                category = "memory_policy_kill"
            else:
                continue
            pid = int(row.get("PID", 0))
            try:
                uid = int(row.get("UID", row.get("uid", row.get("uid_", -1))))
            except (TypeError, ValueError):
                continue
            if (pid <= 0 or uid != int(targets[bundle]) or observed is None or
                    start_uptime is None or end_uptime is None or
                    anchor_wall_ms is None or anchor_uptime is None):
                continue
            event_uptime = anchor_uptime + (stamp - anchor_wall_ms) / 1000.0
            identities = observed.get(pid, [])
            if isinstance(identities, dict):
                identities = [identities]
            identity = next((item for item in identities
                             if item.get("bundle") == bundle and item.get("uid") == uid and
                             item.get("first_uptime", end_uptime + 1) <= event_uptime <=
                             (item.get("first_absent_uptime") if item.get("first_absent_uptime") is not None else end_uptime)), None)
            if identity is None or not start_uptime <= event_uptime <= end_uptime:
                continue
            self.add(category, case_id, bundle, pid, stamp, "hisysevent", reason.strip(),
                     evidence, f"{case_id}:{pid}:{identity['start_ticks']}", str(row.get("id_", "")))

    def kernel_oom_victims(self, trace: str, case_id: str, observed: dict[int, list[dict]],
                           start_uptime: float, end_uptime: float, evidence: str) -> None:
        # ftrace timestamps are boot uptime; process identity is bounded by actual
        # first-absent/replaced observation, or case end when still present.
        for line in trace.splitlines():
            if "oom:mark_victim" not in line:
                continue
            stamp_match = re.search(r"\[\s*(\d+\.\d+)\]", line)
            pid_match = re.search(r"\bpid[=: ]+(\d+)\b", line)
            if not stamp_match or not pid_match:
                continue
            uptime, pid = float(stamp_match[1]), int(pid_match[1])
            identities = observed.get(pid, [])
            if isinstance(identities, dict):
                identities = [identities]
            if not start_uptime <= uptime <= end_uptime:
                continue
            identity = next((item for item in identities
                             if item["first_uptime"] <= uptime <=
                             (item.get("first_absent_uptime") if item.get("first_absent_uptime") is not None else end_uptime)), None)
            if identity is None:
                continue
            self.add("oom_kill", case_id, identity["bundle"], pid,
                     int(uptime * 1000), "ftrace:oom:mark_victim", line.strip(),
                     evidence, f"{case_id}:{pid}:{identity['start_ticks']}",
                     f"{pid}:{identity['start_ticks']}:{uptime:.3f}")

    def kernel_dmesg_oom_victims(self, raw: str, case_id: str,
                                 observed: dict[int, list[dict]], targets: dict[str, int],
                                 attempt_rows: dict[str, list[dict]], start_uptime: float,
                                 end_uptime: float, anchor_wall_ms: int,
                                 anchor_uptime: float, evidence: str,
                                 max_clock_drift_s: float = 2.0, *,
                                 scene4_cold_only: bool = False,
                                 journal_readback_complete_bundles: set[str] | None = None,
                                 journal_case_ids: dict[str, str] | None = None,
                                 startup_failure_bundles: set[str] | None = None) -> dict:
        """Count only marker-bounded OOM victim selections followed by process exit.

        dmesg's kernel timestamp is not assumed to equal /proc/uptime. The two
        MEMSCENES markers carry paired device uptime and wall time; their dmesg
        timestamps establish a bounded conversion and expose clock drift.
        """
        lines = raw.splitlines()
        marker_re = re.compile(r"MEMSCENES_CASE_(BEGIN|END)\s+session=\S+\s+case_id=([^\s]+)\s+uptime_ms=(\d+)\s+wall_ms=(\d+)")
        markers = []
        for index, line in enumerate(lines):
            match = marker_re.search(line)
            stamp = re.search(r"^\[\s*(\d+\.\d+)\]", line)
            if match and match[2] == case_id and stamp:
                markers.append({"kind": match[1], "index": index, "dmesg_s": float(stamp[1]),
                                "uptime_s": int(match[3]) / 1000.0,
                                "wall_ms": int(match[4]), "raw": line})
        begins = [row for row in markers if row["kind"] == "BEGIN"]
        ends = [row for row in markers if row["kind"] == "END"]
        if len(begins) != 1 or len(ends) != 1 or begins[0]["index"] >= ends[0]["index"]:
            return {"coverage_complete": False, "reason": "missing_or_ambiguous_case_markers",
                    "begin_markers": len(begins), "end_markers": len(ends), "victims_detected": 0}
        begin, end = begins[0], ends[0]
        begin_offset = begin["uptime_s"] - begin["dmesg_s"]
        end_offset = end["uptime_s"] - end["dmesg_s"]
        drift = abs(begin_offset - end_offset)
        if drift > max_clock_drift_s:
            return {"coverage_complete": False, "reason": "dmesg_uptime_clock_mapping_unstable",
                    "begin_marker": begin["raw"], "end_marker": end["raw"],
                    "clock_offset_begin_s": begin_offset, "clock_offset_end_s": end_offset,
                    "clock_drift_s": drift, "victims_detected": 0}

        # Linux task comm is limited to TASK_COMM_LEN - 1 bytes.  HarmonyOS
        # truncates these package-derived workload names from the left, so
        # scene1/2's ``io.memscene.workload.a4`` appears as
        # ``ene.workload.a4`` while scene4's longer bundle appears as
        # ``ne4.workload.a4``.  Match the actual 15-character suffix for every
        # owned workload bundle, and reject ambiguous suffixes below.
        app_names = {}
        for bundle in targets:
            if ".workload." in bundle:
                # Firmware logger paths have emitted both TASK_COMM_LEN-1 and
                # 14-character comm names. Support both observed truncations;
                # identity still requires PID/UID/start_ticks.
                app_names[bundle] = {bundle[-15:], bundle[-14:]}
        name_owners: dict[str, list[str]] = {}
        for bundle, comm_names in app_names.items():
            for comm_name in comm_names:
                name_owners.setdefault(comm_name, []).append(bundle)
        # Kernel source line numbers move between builds. Require the stable
        # mem_oom component plus the explicit victim-selection wording.
        victim_re = re.compile(r"\[mem_oom:\d+\].*?out of Memory\s*\(OOM\),\s*start to kill process\s*<\s*([^>]+)>\s*\(pid:\s*(\d+),", re.I)
        exit_re = re.compile(r"process has exited, pid=(\d+)")
        parsed_by_incident = {}
        section = lines[begin["index"] + 1:end["index"]]
        for index, line in enumerate(section):
            match = victim_re.search(line)
            stamp_match = re.search(r"^\[\s*(\d+\.\d+)\]", line)
            if not match or not stamp_match:
                continue
            victim_name, pid = match[1].strip(), int(match[2])
            owners = name_owners.get(victim_name, [])
            bundle = owners[0] if len(owners) == 1 else None
            if bundle is None or targets.get(bundle) is None:
                continue
            bundle_case_id = (journal_case_ids or {}).get(bundle, case_id) if scene4_cold_only else case_id
            dmesg_s = float(stamp_match[1])
            event_uptime = dmesg_s + (begin_offset + end_offset) / 2.0
            if not start_uptime <= event_uptime <= end_uptime:
                continue
            try:
                has_peak_for_pid = any(row.get("event") == "peak_begin" and
                    row.get("case_id", bundle_case_id) == bundle_case_id and
                    int(row.get("pid", -1)) == pid
                    for row in attempt_rows.get(bundle, []))
            except (TypeError, ValueError, OverflowError):
                continue
            if not has_peak_for_pid:
                continue
            identities = observed.get(pid, [])
            if isinstance(identities, dict):
                identities = [identities]
            identity = next((row for row in identities
                             if row.get("bundle") == bundle and row.get("uid") == targets[bundle] and
                             row.get("first_uptime", end_uptime + 1) <= event_uptime <=
                             (row.get("first_absent_uptime") if row.get("first_absent_uptime") is not None
                             else end_uptime)), None)
            process_history_identity = identity
            identity_source = "process_history"
            try:
                journal_ticks = {str(row.get("start_ticks")) for row in attempt_rows.get(bundle, [])
                                 if row.get("case_id") == bundle_case_id and
                                 int(row.get("pid", -1)) == pid and row.get("start_ticks") is not None}
            except (TypeError, ValueError, OverflowError):
                # A malformed identity row makes this victim unassignable.
                continue
            journal_identity = None
            readiness_relation = None
            if scene4_cold_only:
                if identities and identity is None:
                    # A sampled PID known under another UID/generation/lifetime
                    # must never be reassigned using a truncated kernel comm.
                    continue
                # Scene 4 may lose a very short-lived process between process
                # polls. In this opt-in path, accept journal identity only when
                # the caller verified a complete readback, and the case journal
                # binds one launch generation to the exact target UID/PID.
                if bundle not in (journal_readback_complete_bundles or set()):
                    continue
                case_rows = [row for row in attempt_rows.get(bundle, [])
                             if row.get("case_id") == bundle_case_id]
                peak_rows = [row for row in case_rows if row.get("event") == "peak_begin"]
                try:
                    identity_rows = [row for row in case_rows
                        if int(row.get("pid", -1)) == pid and row.get("start_ticks") is not None]
                    generations = {(int(row.get("pid", -1)), int(row.get("uid", -1)),
                                    str(row.get("start_ticks"))) for row in identity_rows}
                    peak_pid = int(peak_rows[0].get("pid", -1)) if len(peak_rows) == 1 else -1
                    peak_uid = int(peak_rows[0].get("uid", -1)) if len(peak_rows) == 1 else -1
                except (TypeError, ValueError, OverflowError):
                    continue
                if (len(generations) != 1 or len(peak_rows) != 1 or
                        peak_pid != pid or peak_uid != int(targets[bundle]) or
                        str(peak_rows[0].get("start_ticks", "")) != next(iter(generations))[2]):
                    continue
                generation_ticks = next(iter(generations))[2]
                if not generation_ticks.isdecimal() or int(generation_ticks) <= 0:
                    continue
                peak_time_raw = peak_rows[0].get("boottime_ns")
                try:
                    peak_time = int(peak_time_raw) / 1e9
                except (TypeError, ValueError):
                    continue
                if peak_time <= 0 or peak_time > event_uptime:
                    continue
                journal_identity = {"bundle": bundle, "uid": int(targets[bundle]),
                                    "start_ticks": next(iter(generations))[2]}
                if journal_ticks != {str(journal_identity["start_ticks"])}:
                    continue
                if identity is not None and str(identity.get("start_ticks", "")) != journal_identity["start_ticks"]:
                    continue
                ready_rows = [row for row in case_rows if row.get("event") in
                              ("cold_start_ready", "startup_ready")]
                ready_before_victim = False
                readiness_unknown = False
                for ready_row in ready_rows:
                    try:
                        ready_s = int(ready_row.get("boottime_ns")) / 1e9
                        ready_pid = int(ready_row.get("pid", -1))
                        ready_uid = int(ready_row.get("uid", -1))
                        ready_ticks = str(ready_row.get("start_ticks", ""))
                    except (TypeError, ValueError):
                        readiness_unknown = True
                        break
                    if (ready_s <= 0 or ready_s < peak_time or ready_pid != pid or
                            ready_uid != int(targets[bundle]) or
                            ready_ticks != journal_identity["start_ticks"] or
                            not ready_ticks.isdecimal() or int(ready_ticks) <= 0):
                        # A second generation or mismatched ready record means
                        # the one-launch startup relation cannot be proven.
                        readiness_unknown = True
                        break
                    if ready_s <= event_uptime:
                        ready_before_victim = True
                if readiness_unknown:
                    continue
                readiness_relation = "post_ready" if ready_before_victim else "pre_ready"
                identity_source = "complete_hap_journal_and_process_history" if identity else \
                    "complete_hap_journal_pid_uid_start_ticks_and_kernel_comm"
                startup_eligible = bundle in (startup_failure_bundles or set()) and readiness_relation == "pre_ready"
                identity = journal_identity
            if identity is not None and journal_ticks and str(identity.get("start_ticks")) not in journal_ticks:
                continue
            if identity is None and identities:
                continue
            if identity is None:
                if identities:
                    # A PID observed under another identity or outside its
                    # lifetime cannot be reassigned from a truncated comm.
                    continue
                rows = [row for row in attempt_rows.get(bundle, [])
                        if row.get("case_id") == case_id and
                        int(row.get("pid", -1)) == pid and
                        int(row.get("uid", -1)) == int(targets[bundle]) and
                        row.get("start_ticks") is not None]
                ticks = {str(row.get("start_ticks")) for row in rows}
                peaks = [row for row in rows if row.get("event") == "peak_begin"]
                ready_rows = [row for row in rows if row.get("event") in
                              ("cold_start_ready", "startup_ready")]
                if len(ticks) != 1 or not peaks or not ready_rows:
                    continue
                ready_times = [row.get("boottime_seconds", row.get("device_uptime_seconds",
                                   (int(row["boottime_ns"]) / 1e9 if row.get("boottime_ns") is not None else None)))
                               for row in ready_rows]
                try:
                    ready_time = min(float(value) for value in ready_times if value is not None)
                except (TypeError, ValueError):
                    continue
                if not start_uptime <= ready_time <= event_uptime <= end_uptime:
                    continue
                identity = {"bundle": bundle, "uid": int(targets[bundle]),
                            "start_ticks": next(iter(ticks))}
                identity_source = "hap_journal_pid_uid_start_ticks_and_kernel_comm"
            exit_line = None
            exit_dmesg_s = None
            for later in section[index + 1:]:
                later_stamp = re.search(r"^\[\s*(\d+\.\d+)\]", later)
                if not later_stamp:
                    continue
                later_s = float(later_stamp[1])
                if later_s <= dmesg_s:
                    continue
                exit_match = exit_re.search(later)
                if exit_match and int(exit_match[1]) == pid:
                    exit_line = later
                    exit_dmesg_s = later_s
                    break
            if exit_line is None:
                continue
            if exit_dmesg_s - dmesg_s > 2.0:
                # Keep the legacy short select->exit association for all
                # callers. Scene 4 can also prove a slower OOM teardown, but
                # only when the complete one-generation HAP journal above and
                # process polling independently bracket the same exit. This
                # is not a wider name/PID-only timeout.
                if not scene4_cold_only or process_history_identity is None:
                    continue
                exit_uptime = exit_dmesg_s + (begin_offset + end_offset) / 2.0
                absent_start = process_history_identity.get("first_absent_interval_start_s")
                absent_end = process_history_identity.get("first_absent_interval_end_s")
                last_seen_start = process_history_identity.get("last_seen_interval_start_s")
                last_seen_end = process_history_identity.get("last_seen_interval_end_s")
                try:
                    absent_start, absent_end = float(absent_start), float(absent_end)
                    last_seen_start, last_seen_end = float(last_seen_start), float(last_seen_end)
                except (TypeError, ValueError):
                    continue
                if (any(not math.isfinite(value) for value in
                        (exit_uptime, absent_start, absent_end, last_seen_start, last_seen_end)) or
                        absent_end < absent_start or last_seen_end < last_seen_start or
                        absent_end - absent_start > 10.0 or last_seen_start > exit_uptime or
                        last_seen_end > exit_uptime or exit_uptime < absent_start or
                        exit_uptime > absent_end):
                    continue
            wall_ms = anchor_wall_ms + int((event_uptime - anchor_uptime) * 1000)
            incident = f"{case_id}:{pid}:{identity['start_ticks']}"
            self.add("oom_kill", case_id, bundle, pid, wall_ms, "kernel:dmesg:mem_oom",
                     "mem_oom selected target process and process exit followed",
                     evidence + ":\n" + line.strip() + "\n" + exit_line.strip(), incident,
                     f"{pid}:{identity['start_ticks']}:{dmesg_s:.6f}")
            parsed_by_incident.setdefault(incident, {"bundle": bundle, "pid": pid,
                "uid": targets[bundle], "start_ticks": identity["start_ticks"],
                "event_uptime_s": event_uptime, "identity_source": identity_source,
                "readiness_relation": readiness_relation,
                "startup_failure_eligible": startup_eligible if scene4_cold_only else False,
                "victim_line": line, "exit_line": exit_line,
                "exit_dmesg_s": exit_dmesg_s,
                "completion_evidence": "short_select_exit_pair" if exit_dmesg_s-dmesg_s <= 2.0 else
                    "unique_journal_generation_and_process_absence_bracket"})
        return {"coverage_complete": True, "begin_marker": begin["raw"], "end_marker": end["raw"],
                "clock_offset_begin_s": begin_offset, "clock_offset_end_s": end_offset,
                "clock_drift_s": drift, "victims_detected": len(parsed_by_incident),
                "victims": list(parsed_by_incident.values())}

    def finalize_startup_attempt(self, case_id: str, bundle: str, rows: list[dict],
                                 observed: dict[int, list[dict]], timestamp_ms: int,
                                 evidence: str, *, allow_timeout: bool = True) -> str:
        """Classify no-ready attempts after all kernel/system sources are collected."""
        if any(row.get("event") == "startup_ready" for row in rows):
            return "ready"
        existing_failures = [event for event in self.events if event["case_id"] == case_id and
                             event["bundle"] == bundle and event["category"] == "startup_failure"]
        if existing_failures:
            return "confirmed_memory_failure"
        begins = [row for row in rows if row.get("event") == "peak_begin" and
                  row.get("case_id") == case_id]
        if not begins and not allow_timeout:
            return "aborted_before_peak"
        for begin in begins:
            try:
                pid, uid = int(begin["pid"]), int(begin["uid"])
            except (KeyError, TypeError, ValueError):
                continue
            identities = observed.get(pid, [])
            if isinstance(identities, dict):
                identities = [identities]
            identity = next((item for item in identities if item.get("bundle") == bundle and
                             int(item.get("uid", -1)) == uid), None)
            if identity is None:
                continue
            incident = f"{case_id}:{pid}:{identity['start_ticks']}"
            kill = next((event for event in self.events if event["case_id"] == case_id and
                         event["bundle"] == bundle and event["pid"] == pid and
                         event["category"] in ("oom_kill", "memory_policy_kill") and
                         event["incident"] == incident), None)
            if kill is not None:
                self.add("startup_failure", case_id, bundle, pid, kill["timestamp_ms"],
                         "system_kill_during_startup", kill["reason"],
                         kill["evidence"][0], incident)
                return "confirmed_memory_failure"
        pid = int(begins[-1]["pid"]) if begins and str(begins[-1].get("pid", "")).isdigit() else None
        if not any(event["case_id"] == case_id and event["bundle"] == bundle and
                   event["category"] == "startup_timeout" for event in self.events):
            self.add("startup_timeout", case_id, bundle, pid, timestamp_ms,
                     "readiness_monitor", "no startup_ready; no matched memory-cause evidence",
                     evidence, f"{case_id}:{pid or bundle}")
        return "unconfirmed"

    def counts(self) -> dict:
        counters = Counter(x["category"] for x in self.events)
        result = {name: counters[name] for name in COUNTS}
        result["low_memory_popup"] = result["low_memory_popup_native"] + result["low_memory_popup_test"]
        included = {"startup_failure", "oom_kill", "low_memory_popup_native", "low_memory_popup_test"}
        result["unique_incidents"] = len({x["incident"] for x in self.events if x["category"] in included})
        result["total_events"] = sum(counters[name] for name in included)
        return result

    def export(self) -> list[dict]:
        return [{k: v for k, v in event.items() if k != "_key"} for event in self.events]
