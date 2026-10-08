"""Produce a hash-bound, offline correction for scene-2 popup visibility.

This tool never edits a run's raw result, case_results, progress, or UI files.
It conservatively removes a test-app popup event when the paired, in-window
mission snapshot shows a foreground TaskManager LowMemoryAbility already alive
at the screenshot capture time. The screenshot, tree, and mission file are
hashed into the resulting audit so the classification can be reviewed later.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA = "memscenes-scene2-ui-audit-v1"
FAULT_CATEGORIES = {
    "startup_failure", "oom_kill", "low_memory_popup_native", "low_memory_popup_test"
}
COUNT_CATEGORIES = (
    "startup_failure", "oom_kill", "memory_policy_kill",
    "low_memory_popup_native", "low_memory_popup_test", "startup_timeout",
)
TASKMANAGER = "com.huawei.hmos.taskmanager"
LOW_MEMORY_ABILITY = "LowMemoryAbility"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(raw)


def _cases_hash(cases: list[dict[str, Any]]) -> str:
    volatile = {"case_id", "created_at", "timestamp", "version_label", "phase", "session"}
    normalized = [{key: value for key, value in case.items() if key not in volatile} for case in cases]
    return _canonical_hash(normalized)


def _relative_path(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute():
        return candidate
    return root / candidate


def foreground_low_memory_missions(text: str, capture_uptime_seconds: float) -> list[dict[str, Any]]:
    """Parse aa dump -l mission blocks and return active TaskManager warning missions."""
    blocks = re.split(r"(?=\bMission ID\s*#\d+)", text)
    found: list[dict[str, Any]] = []
    for block in blocks:
        mid = re.search(r"\bMission ID\s*#(\d+)", block)
        bundle = re.search(r"bundle name\s*\[([^\]]+)\]", block)
        ability = re.search(r"main name\s*\[([^\]]+)\]", block)
        state = re.search(r"\bstate\s*#([A-Z_]+)", block)
        start = re.search(r"\bstart time\s*\[(\d+)\]", block)
        if not (mid and bundle and ability and state and start):
            continue
        if bundle.group(1) != TASKMANAGER or ability.group(1) != LOW_MEMORY_ABILITY:
            continue
        start_ms = int(start.group(1))
        capture_ms = round(capture_uptime_seconds * 1000)
        if state.group(1) != "FOREGROUND" or start_ms > capture_ms:
            continue
        found.append({
            "mission_id": mid.group(1), "bundle": bundle.group(1),
            "ability": ability.group(1), "state": state.group(1),
            "start_uptime_ms": start_ms, "capture_uptime_ms": capture_ms,
        })
    return found


def _observation_for_event(case: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    evidence = {str(x).replace("/", "\\") for x in event.get("evidence", [])}
    for observation in case.get("popup_observations", []):
        screenshot = str(observation.get("screenshot", "")).replace("/", "\\")
        if screenshot and screenshot in evidence:
            return observation
    return None


def _test_popup_journal_proof(root: Path, case_id: str, bundle: str,
                              callback_ms: int, screenshot_wall_ms: int,
                              begin_wall_ms: int) -> dict[str, Any]:
    paths = [root / "journals" / f"{case_id}_daily_{bundle}.jsonl",
             root / "journals" / f"{case_id}_{bundle}.jsonl"]
    rows: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        hashes[str(path.relative_to(root)).replace("\\", "/")] = _sha256_file(path)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except Exception:
                continue
    notifications = [row for row in rows if row.get("event") == "memory_notification" and
                     row.get("case_id") in (case_id, case_id + "_daily") and
                     row.get("level") in (1, 2, 5, 6) and
                     int(row.get("time_ms", -1)) == callback_ms and
                     begin_wall_ms <= int(row.get("time_ms", -1)) <= screenshot_wall_ms]
    requests = [row for row in rows if row.get("event") == "popup_requested" and
                row.get("case_id") == case_id and
                begin_wall_ms <= int(row.get("time_ms", -1)) <= screenshot_wall_ms]
    return {"notification_matched": bool(notifications), "popup_request_matched": bool(requests),
            "notification_rows": notifications, "popup_request_rows": requests,
            "journal_sha256": hashes}


def _case_measurement_ok(result: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    cases = result.get("case_results", [])
    specs = result.get("cases", [])
    if len(cases) != 11 or len(specs) != 11:
        reasons.append("expected_exactly_11_cases")
    if not result.get("cases_hash") or result.get("cases_hash") != _cases_hash(specs):
        reasons.append("cases_hash_mismatch")
    for case in cases:
        cid = case.get("case_id", "<unknown>")
        for key in ("case_measurement_complete", "daily_stage_complete", "daily_barrier_released",
                    "attempts_terminal_complete", "kernel_coverage_complete", "popup_observation_complete"):
            if case.get(key) is not True:
                reasons.append(f"{cid}:{key}")
        markers = case.get("trace_segment", {}).get("marker_coverage", {})
        if markers.get("expected") != 2 or markers.get("present") != 2:
            reasons.append(f"{cid}:trace_marker_coverage")
        checks = case.get("pre_promote_identity_checks", [])
        expected_bundles = case.get("bundles", [])
        if len(checks) != len(expected_bundles) or any(row.get("verified") is not True for row in checks):
            reasons.append(f"{cid}:pre_promote_identity_checks")
        if case.get("restore_initial_state", {}).get("verified_absent") is not True:
            reasons.append(f"{cid}:native_popup_restore")
        if any(row.get("empty") is not True for row in case.get("owned_app_cleanup", [])):
            reasons.append(f"{cid}:owned_app_cleanup")
        if any(row.get("verified_absent") is not True for row in case.get("owned_page_cleanup", [])):
            reasons.append(f"{cid}:owned_page_cleanup")
    return not reasons, reasons


def _journal_manifest(result: dict[str, Any], root: Path) -> tuple[dict[str, Any], list[str]]:
    """Verify the persisted append-only daily/peak journals against each attempt."""
    manifest: dict[str, Any] = {"expected_pairs": 0, "complete_pairs": 0, "cases": [],
                                "resolved_sampling_errors": [], "unresolved_sampling_errors": []}
    gaps: list[str] = []
    errors = [row for row in result.get("sampling_errors", []) if row.get("source") == "native_journal"]
    for case in result.get("case_results", []):
        cid = str(case.get("case_id", ""))
        bundle_rows: list[dict[str, Any]] = []
        measurements = {row.get("bundle"): row for row in case.get("measurements", [])}
        for bundle in case.get("bundles", []):
            manifest["expected_pairs"] += 1
            paths = {
                "daily": root / "journals" / f"{cid}_daily_{bundle}.jsonl",
                "peak": root / "journals" / f"{cid}_{bundle}.jsonl",
            }
            streams: dict[str, Any] = {}
            path_gaps: list[str] = []
            for stage, path in paths.items():
                entry: dict[str, Any] = {"path": str(path.relative_to(root)).replace("\\", "/"),
                                         "exists": path.is_file(), "sha256": None, "bytes": 0,
                                         "parsed_lines": 0, "events": [], "parse_errors": [],
                                         "mtime_utc": None}
                parsed: list[dict[str, Any]] = []
                if path.is_file():
                    entry["sha256"] = _sha256_file(path)
                    entry["bytes"] = path.stat().st_size
                    entry["mtime_utc"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                    for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict):
                                raise ValueError("journal row is not an object")
                            parsed.append(row)
                        except Exception as exc:
                            entry["parse_errors"].append({"line": line_no, "error": str(exc)})
                    entry["parsed_lines"] = len(parsed)
                    entry["events"] = [row.get("event") for row in parsed]
                needed = {"daily": {"daily_ready", "promote_requested"}, "peak": {"peak_begin"}}[stage]
                present = set(entry["events"])
                if not entry["exists"] or entry["bytes"] == 0 or entry["parse_errors"] or not needed.issubset(present):
                    path_gaps.append(f"{cid}:{bundle}:{stage}:missing_or_invalid_raw_journal")
                streams[stage] = {"summary": entry, "rows": parsed}
            measurement = measurements.get(bundle, {})
            peak_names = set(streams["peak"]["summary"]["events"])
            outcome = measurement.get("outcome")
            terminal_ok = False
            terminal_basis = None
            if outcome == "success":
                terminal_ok = {"peak_reached", "startup_ready"}.issubset(peak_names)
                terminal_basis = "peak_reached+startup_ready"
            elif outcome == "confirmed_memory_failure":
                terminals = measurement.get("terminal_events", [])
                matched = [event for event in terminals if event.get("category") in
                           ("oom_kill", "memory_policy_kill", "startup_failure")]
                raw_complete = any(
                    event.get("source", "").startswith("kernel:") and
                    "mem_oom" in str(event.get("evidence", "")) and
                    "process has exited" in str(event.get("evidence", ""))
                    for event in matched
                )
                terminal_ok = bool(matched) and (raw_complete or any(
                    event.get("source", "").startswith("memory_policy") or
                    event.get("source", "").startswith("alloc") for event in matched))
                terminal_basis = "matched_kernel_or_memory_failure_terminal_event"
            if not terminal_ok:
                path_gaps.append(f"{cid}:{bundle}:peak:terminal_outcome_not_proven")
            if not measurement.get("terminal_complete") or not measurement.get("peak_begin_observed"):
                path_gaps.append(f"{cid}:{bundle}:measurement_terminal_or_peak_begin_missing")
            if not path_gaps:
                manifest["complete_pairs"] += 1
            bundle_rows.append({"bundle": bundle, "outcome": outcome,
                                "terminal_complete": measurement.get("terminal_complete"),
                                "terminal_basis": terminal_basis, "terminal_proven": terminal_ok,
                                "stages": {stage: stream["summary"] for stage, stream in streams.items()},
                                "gaps": path_gaps})
            gaps.extend(path_gaps)
        manifest["cases"].append({"case_id": cid, "bundles": bundle_rows})

    for error in errors:
        cid, bundle = error.get("case_id"), error.get("bundle")
        row = next((c for c in manifest["cases"] if c["case_id"] == cid), None)
        app = next((x for x in row["bundles"] if x["bundle"] == bundle), None) if row else None
        if app and not app.get("gaps"):
            manifest["resolved_sampling_errors"].append({
                "case_id": cid, "bundle": bundle, "raw_error": error.get("error"),
                "resolution": "later read timed out, but the preserved daily+peak journals parse completely and the terminal outcome is independently evidenced",
                "journal_sha256": {k: app["stages"][k]["sha256"] for k in ("daily", "peak")},
            })
        else:
            manifest["unresolved_sampling_errors"].append(error)
    manifest["all_pairs_complete"] = (
        manifest["expected_pairs"] == 88 and manifest["complete_pairs"] == 88 and not gaps and
        not manifest["unresolved_sampling_errors"])
    return manifest, gaps


def audit_run(result_path: Path, output_path: Path,
              source_reference: str | None = None) -> dict[str, Any]:
    result_path = result_path.resolve()
    root = result_path.parent
    result = _json(result_path)
    case_results_path = root / "case_results.json"
    progress_path = root / "progress.json"
    separate_case_rows = _json(case_results_path) if case_results_path.is_file() else None
    case_results_content_matches = (
        separate_case_rows is not None and
        _canonical_hash(separate_case_rows) == _canonical_hash(result.get("case_results", [])))
    progress = _json(progress_path) if progress_path.is_file() else {}
    progress_case_ends = [row for row in progress.get("markers", []) if row.get("name") == "case_end"]
    progress_complete = (progress.get("status") == result.get("status") and
                         len(progress_case_ends) == 11 and
                         {row.get("case_id") for row in progress_case_ends} ==
                         {row.get("case_id") for row in result.get("case_results", [])})
    case_rows = {row.get("case_id"): row for row in result.get("case_results", [])}
    original_events = result.get("events", [])
    retained: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    popup_evidence_gaps: list[str] = []
    evidence_hashes: dict[str, str] = {}

    for event in original_events:
        if event.get("category") not in ("low_memory_popup_test", "low_memory_popup_native"):
            retained.append(event)
            continue
        case = case_rows.get(event.get("case_id"))
        observation = _observation_for_event(case or {}, event)
        decision = {
            "event_id": _canonical_hash(event), "case_id": event.get("case_id"),
            "category_before": event.get("category"), "action": "retain",
            "reason": "no_matching_observation", "evidence": [],
        }
        if observation is None:
            decision["action"] = "remove"
            decision["reason"] = "missing_paired_ui_evidence"
            if event.get("category") == "low_memory_popup_native":
                popup_evidence_gaps.append(f"{event.get('case_id')}:native_popup_observation_missing")
        if observation:
            cid = str(event.get("case_id", ""))
            interval = (case or {}).get("measurement_interval", {})
            screenshot_wall = observation.get("screenshot_complete_wall_ms")
            in_window = (
                observation.get("case_attribution_allowed") is True and
                observation.get("capture_clock_valid") is True and
                observation.get("screenshot_captured") is True and
                screenshot_wall is not None and
                interval.get("begin_wall_ms") is not None and
                interval.get("end_wall_ms") is not None and
                interval["begin_wall_ms"] <= screenshot_wall <= interval["end_wall_ms"]
            )
            paths: dict[str, str | None] = {
                "screenshot": observation.get("screenshot"),
                "layout": observation.get("layout"),
                "missions": observation.get("mission_list"),
            }
            resolved: dict[str, Path] = {}
            for label, rel in paths.items():
                path = _relative_path(root, rel)
                if path and path.is_file():
                    resolved[label] = path
                    evidence_hashes[str(path.relative_to(root)).replace("\\", "/")] = _sha256_file(path)
            mission_text = (resolved["missions"].read_text(encoding="utf-8", errors="replace")
                            if "missions" in resolved else "")
            capture_uptime = observation.get("capture_uptime_seconds")
            active = (foreground_low_memory_missions(mission_text, float(capture_uptime))
                      if capture_uptime is not None else [])
            decision["evidence"] = {
                label: {"path": str(path.relative_to(root)).replace("\\", "/"),
                        "sha256": evidence_hashes[str(path.relative_to(root)).replace("\\", "/")]}
                for label, path in resolved.items()
            }
            decision["observation"] = {
                "phase": observation.get("phase"), "screenshot_wall_ms": screenshot_wall,
                "case_window": {"begin_wall_ms": interval.get("begin_wall_ms"),
                                "end_wall_ms": interval.get("end_wall_ms")},
                "in_window": in_window,
            }
            decision["active_taskmanager_low_memory_missions"] = active
            if event.get("category") == "low_memory_popup_native":
                candidate = observation.get("native_candidate") or {}
                mission_id = str(candidate.get("mission_id", ""))
                matching = [item for item in active if item["mission_id"] == mission_id]
                outer_begin = (case or {}).get("case_outer_interval", {}).get("begin_uptime_seconds")
                fresh = (candidate.get("owner") == TASKMANAGER and
                         candidate.get("ability") == LOW_MEMORY_ABILITY and
                         candidate.get("mission_state") == "FOREGROUND" and
                         candidate.get("capture_uptime_seconds") == capture_uptime and
                         outer_begin is not None and
                         float(candidate.get("mission_start_uptime_seconds", -1)) >= outer_begin and
                         bool(matching))
                native_ok = (in_window and fresh and observation.get("capture_wall_ms") == event.get("timestamp_ms") and
                             all(label in resolved for label in ("screenshot", "layout", "missions")))
                decision["action"] = "retain" if native_ok else "remove"
                decision["reason"] = "verified_new_in_window_native_warning" if native_ok else \
                    "native_warning_missing_fresh_visible_window_evidence"
                if not native_ok:
                    popup_evidence_gaps.append(f"{cid}:native_popup_evidence_invalid")
            elif not resolved.get("screenshot") or not resolved.get("layout") or not resolved.get("missions"):
                decision["action"] = "remove"
                decision["reason"] = "missing_paired_ui_evidence"
            elif in_window and active:
                decision["action"] = "remove"
                decision["reason"] = "foreground_taskmanager_low_memory_ability_at_paired_screenshot"
            elif not in_window:
                decision["action"] = "remove"
                decision["reason"] = "screenshot_not_authoritatively_inside_measurement_window"
            elif (event.get("bundle") not in observation.get("focused_bundles", []) or
                  not observation.get("found_popup_nodes", 0) or
                  event.get("timestamp_ms") != screenshot_wall):
                decision["action"] = "remove"
                decision["reason"] = "test_popup_not_tied_to_focused_owner_and_paired_capture"
            else:
                begin_wall = interval.get("begin_wall_ms")
                callback_match = re.search(r"callback_ms=(\d+)", str(event.get("reason", "")))
                proof = (_test_popup_journal_proof(
                    root, str(event.get("case_id", "")), str(event.get("bundle", "")),
                    int(callback_match.group(1)) if callback_match else -1,
                    int(screenshot_wall), int(begin_wall) if begin_wall is not None else -1)
                    if begin_wall is not None else {"notification_matched": False,
                                                     "popup_request_matched": False,
                                                     "journal_sha256": {}})
                decision["callback_journal_proof"] = proof
                for rel, digest in proof.get("journal_sha256", {}).items():
                    evidence_hashes[rel] = digest
                if not proof["notification_matched"] or not proof["popup_request_matched"]:
                    decision["action"] = "remove"
                    decision["reason"] = "missing_case_scoped_callback_or_popup_request_journal"
                else:
                    decision["reason"] = "visible_focused_test_popup_with_case_callback_and_request"
        if event.get("category") in ("low_memory_popup_test", "low_memory_popup_native"):
            decisions.append(decision)
        if decision["action"] != "remove":
            retained.append(event)

    counts = {name: 0 for name in COUNT_CATEGORIES}
    for event in retained:
        if event.get("category") in counts:
            counts[event["category"]] += 1
    counts["low_memory_popup"] = counts["low_memory_popup_native"] + counts["low_memory_popup_test"]
    counts["unique_incidents"] = len({event.get("incident") for event in retained
                                       if event.get("category") in FAULT_CATEGORIES})
    counts["total_events"] = sum(counts[name] for name in FAULT_CATEGORIES)
    measurement_ok, measurement_reasons = _case_measurement_ok(result)
    journal_manifest, journal_gaps = _journal_manifest(result, root)
    categories_ok = all(counts[name] > 0 for name in (
        "startup_failure", "oom_kill", "low_memory_popup"))
    aux_errors = [row for row in result.get("evidence_collection_errors", [])
                  if row.get("source") == "kernel_trace_auxiliary"]
    kernel_dir = root / "kernel_dmesg"
    kernel_hashes = {str(path.relative_to(root)).replace("\\", "/"): _sha256_file(path)
                     for path in sorted(kernel_dir.glob("*.json")) if path.is_file()}
    raw_kernel = root / "kernel_dmesg.raw.log"
    if raw_kernel.is_file():
        kernel_hashes["kernel_dmesg.raw.log"] = _sha256_file(raw_kernel)
    primary_complete = (measurement_ok and journal_manifest["all_pairs_complete"] and
                        case_results_content_matches and progress_complete and
                        not popup_evidence_gaps and
                        bool(result.get("execution_complete")) and
                        all(row.get("coverage_complete") is True for row in
                            [_json(path) for path in sorted(kernel_dir.glob("*.json"))]) and
                        len(kernel_hashes) >= 12)
    dram_gib = result.get("config", {}).get("dram_gib", result.get("suite", {}).get("dram_gib"))
    eligible = (primary_complete and categories_ok and
                counts["total_events"] >= 100 and result.get("profile") == "qualification" and
                result.get("scene") == 2 and result.get("phase") == "baseline" and
                result.get("qualification_scope") == "prevalidation_32gb" and dram_gib == 32)

    hashes = {
        "result_json": _sha256_file(result_path),
        "case_results_json": _sha256_file(case_results_path) if case_results_path.is_file() else None,
        "progress_json": _sha256_file(progress_path) if progress_path.is_file() else None,
        "events_canonical": _canonical_hash(original_events),
        "cases_canonical": _canonical_hash(result.get("cases", [])),
        "case_results_content_matches": case_results_content_matches,
        "progress_case_ends": _canonical_hash(progress_case_ends),
        "ui_evidence": evidence_hashes,
        "kernel_evidence": kernel_hashes,
    }
    if case_results_path.is_file():
        try:
            separate_case_rows = _json(case_results_path)
            hashes["case_results_content_matches"] = (
                _canonical_hash(separate_case_rows) == _canonical_hash(result.get("case_results", [])))
        except Exception:
            hashes["case_results_content_matches"] = False
    audit = {
        "schema": SCHEMA, "source_result": source_reference or str(result_path),
        "source_run_id": result.get("session"),
        "source_hashes": hashes, "rule": {
            "version": 1,
            "description": "Remove a test popup when its paired in-window screenshot has a contemporaneous foreground TaskManager LowMemoryAbility mission; reject post-window/missing screenshot attribution.",
            "taskmanager_bundle": TASKMANAGER, "ability": LOW_MEMORY_ABILITY,
        },
        "counts_before": result.get("counts", {}), "counts_after": counts,
        "removed_event_ids": [x["event_id"] for x in decisions if x["action"] == "remove"],
        "decisions": decisions,
        "raw_measurement_complete": result.get("measurement_complete"),
        "raw_measurement_evidence_complete": result.get("measurement_evidence_complete"),
        "case_measurement_complete": measurement_ok,
        "measurement_reasons": measurement_reasons,
        "primary_evidence_complete": primary_complete,
        "case_results_content_matches": case_results_content_matches,
        "progress_complete": progress_complete,
        "journal_manifest": journal_manifest,
        "journal_manifest_gaps": journal_gaps,
        "popup_evidence_gaps": popup_evidence_gaps,
        "auxiliary_trace_errors": aux_errors,
        "telemetry_errors_unknown_only": result.get("telemetry_errors", []),
        "sampling_errors_resolved_or_unresolved": journal_manifest["resolved_sampling_errors"] +
            journal_manifest["unresolved_sampling_errors"],
        "three_categories_present": categories_ok,
        "qualification_eligible": eligible,
        "qualification_scope": result.get("qualification_scope"),
        "events_after": retained,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return audit


def verify_audit_document(audit_path: Path) -> tuple[bool, str]:
    """Recompute an audit from its immutable sources and reject edits or drift."""
    import tempfile

    audit_path = audit_path.resolve()
    supplied = _json(audit_path)
    if supplied.get("schema") != SCHEMA:
        return False, "unsupported_audit_schema"
    source = Path(supplied.get("source_result", ""))
    if not source.is_absolute():
        source = (audit_path.parent / source).resolve()
    if not source.is_file():
        return False, "source_result_missing"
    with tempfile.TemporaryDirectory(prefix="memscene-audit-verify-") as tmp:
        source_ref = supplied.get("source_result") if not Path(supplied.get("source_result", "")).is_absolute() else None
        regenerated = audit_run(source, Path(tmp) / "recomputed.json", source_ref)
    if _canonical_hash(supplied) != _canonical_hash(regenerated):
        return False, "audit_or_source_hash_mismatch"
    return True, "verified"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, help="completed raw result.json")
    parser.add_argument("--output", type=Path, help="derived audit output; defaults beside result")
    parser.add_argument("--relative-source", action="store_true",
                        help="write source_result relative to the audit directory for portable packages")
    parser.add_argument("--verify", type=Path, help="recompute and verify an existing audit file")
    args = parser.parse_args()
    if args.verify:
        ok, reason = verify_audit_document(args.verify)
        print(json.dumps({"verified": ok, "reason": reason}, ensure_ascii=False, indent=2))
        return 0 if ok else 2
    if not args.result:
        parser.error("--result is required unless --verify is used")
    output = args.output or (args.result.parent / "qualification_ui_audit.json")
    source_ref = None
    if args.relative_source:
        try:
            source_ref = args.result.resolve().relative_to(output.parent.resolve()).as_posix()
        except ValueError:
            parser.error("--relative-source requires result.json to be in or below the audit output directory")
    audit = audit_run(args.result, output, source_ref)
    print(json.dumps({"audit": str(output), "counts_before": audit["counts_before"],
                      "counts_after": audit["counts_after"],
                      "measurement_complete": audit["case_measurement_complete"],
                      "qualification_eligible": audit["qualification_eligible"],
                      "removed": len(audit["removed_event_ids"])}, ensure_ascii=False, indent=2))
    return 0 if audit["case_measurement_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
