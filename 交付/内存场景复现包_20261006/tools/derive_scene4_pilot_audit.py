from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memscenes.model import EventBook, COUNTS  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_evidence(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else base / path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a source-bound Scene4 pilot event reclassification; leaves raw result untouched.")
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    base = args.result_dir.resolve()
    result_path = base / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    raw_path = base / "kernel_dmesg.raw.log"
    raw = raw_path.read_text(encoding="utf-8", errors="replace")
    targets: dict[str, int] = {str(k): int(v) for k, v in result["app_uids"].items()}
    dmesg_book = EventBook()
    cases_report: list[dict[str, Any]] = []
    matched_ooms: list[dict[str, Any]] = []
    source_paths: set[Path] = {result_path, base / "config.json", base / "cases.json", raw_path,
                               ROOT / "memscenes" / "model.py", Path(__file__).resolve()}

    for case in result["case_results"]:
        case_id = case["case_id"]
        interval = case["measurement_interval"]
        observed: dict[int, list[dict]] = {}
        for bundle, identities in case.get("process_identities", {}).items():
            for identity in identities:
                observed.setdefault(int(identity["pid"]), []).append(identity)

        attempts: dict[str, list[dict]] = {}
        journal_rows: dict[str, list[dict]] = {}
        daily_rows: dict[str, list[dict]] = {}
        for bundle in case["bundles"]:
            journal = base / "journals" / f"{case_id}_{bundle}.jsonl"
            rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
            journal_rows[bundle] = rows
            attempts[bundle] = rows
            source_paths.add(journal)
            daily_journal = base / "journals" / f"{case_id}_daily_{bundle}.jsonl"
            daily_rows[bundle] = [json.loads(line) for line in daily_journal.read_text(encoding="utf-8").splitlines()
                                 if line.strip()]
            source_paths.add(daily_journal)

        anchor = case.get("wall_uptime_anchor") or {}
        if not anchor.get("wall_ms") or not anchor.get("uptime_seconds"):
            raise ValueError(f"{case_id}: missing paired wall/uptime anchor")
        parsed = dmesg_book.kernel_dmesg_oom_victims(
            raw, case_id, observed, targets, attempts,
            float(interval["begin_uptime_seconds"]), float(interval["end_uptime_seconds"]),
            int(anchor["wall_ms"]), float(anchor["uptime_seconds"]), "kernel_dmesg.raw.log")

        # Reclassify only attempts with an exact kernel OOM incident; all other
        # no-ready attempts remain unconfirmed/timeout in the derived view.
        per_case_ooms = [event for event in dmesg_book.events
                         if event["case_id"] == case_id and event["category"] == "oom_kill"]
        startup_results = []
        for bundle in case["bundles"]:
            rows = journal_rows[bundle]
            outcome = dmesg_book.finalize_startup_attempt(
                case_id, bundle, rows, observed, int(interval.get("end_wall_ms") or anchor["wall_ms"]),
                f"journals/{case_id}_{bundle}.jsonl")
            startup_results.append({
                "bundle": bundle,
                "pid": next((int(row["pid"]) for row in reversed(rows)
                             if row.get("event") == "peak_begin" and row.get("case_id") == case_id), None),
                "has_daily_ready": any(row.get("event") == "daily_ready" for row in daily_rows[bundle]),
                "peak_begin_count": sum(row.get("event") == "peak_begin" for row in rows),
                "startup_ready": any(row.get("event") == "startup_ready" for row in rows),
                "outcome": outcome,
            })
        matched_ooms.extend(per_case_ooms)

        native_observations = []
        native_events = [event for event in result.get("events", [])
                         if event.get("case_id") == case_id and event.get("category") == "low_memory_popup_native"]
        for event in native_events:
            mission_id = event.get("incident", "").rsplit(":", 2)[-2]
            observation = next((obs for obs in case.get("popup_observations", [])
                                if (obs.get("native_candidate") or {}).get("mission_id") == mission_id
                                and obs.get("case_attribution_allowed") is True
                                and obs.get("screenshot_captured") is True), None)
            if observation is None:
                native_observations.append({"event_key": event.get("_key"), "valid": False,
                                            "reason": "no in-case native screenshot observation"})
                continue
            capture = float(observation["capture_uptime_seconds"])
            complete = float(observation["screenshot_complete_uptime_seconds"])
            begin = float(interval["begin_uptime_seconds"])
            end = float(interval["end_uptime_seconds"])
            evidence_files = {}
            for key in ("layout", "mission_list", "screenshot"):
                path = resolve_evidence(base, observation.get(key))
                if path is None or not path.is_file():
                    evidence_files[key] = {"exists": False, "path": observation.get(key)}
                else:
                    source_paths.add(path)
                    evidence_files[key] = {"exists": True, "path": str(path.relative_to(base)),
                                           "sha256": sha256(path)}
            valid = (begin <= capture <= end and begin <= complete <= end and
                     all(item.get("exists") for item in evidence_files.values()) and
                     bool((observation.get("native_candidate") or {}).get("window_ids")))
            native_observations.append({
                "event_key": event.get("_key"), "valid": valid,
                "mission_id": mission_id, "capture_uptime_seconds": capture,
                "screenshot_complete_uptime_seconds": complete,
                "measurement_interval": [begin, end], "evidence_files": evidence_files,
            })

        cases_report.append({
            "case_id": case_id,
            "measurement_interval": interval,
            "original_case_measurement_complete": case.get("case_measurement_complete"),
            "daily_stage_complete": case.get("daily_stage_complete"),
            "daily_barrier_released": case.get("daily_barrier_released"),
            "kernel_marker_coverage": parsed,
            "startup_attempts": startup_results,
            "oom_incidents": [{k: event.get(k) for k in
                               ("bundle", "pid", "incident", "timestamp_ms", "reason", "evidence")}
                              for event in per_case_ooms],
            "native_popup_observations": native_observations,
            "native_popup_count_after_audit": sum(row["valid"] for row in native_observations),
            "test_popup_count_after_audit": 0,
            "test_popup_reason": "no canonical test-popup event is present in the raw result",
        })

    # Preserve native events from the original result, remove only timeouts
    # superseded by a same-case/bundle/PID OOM+startup-failure proof, then add
    # the parsed kernel OOM and linked startup-failure records.
    proven = {(e["case_id"], e["bundle"], e["pid"], e["incident"]) for e in matched_ooms}
    retained = []
    removed_timeouts = []
    for event in result.get("events", []):
        if event.get("category") == "startup_timeout" and any(
                event.get("case_id") == case_id and event.get("bundle") == bundle and
                event.get("pid") == pid for case_id, bundle, pid, _ in proven):
            removed_timeouts.append({"event": event, "reason": "same PID has marker-bounded mem_oom victim+exit and no startup_ready"})
        elif event.get("category") in ("oom_kill", "startup_failure") and any(
                event.get("case_id") == case_id and event.get("bundle") == bundle and
                event.get("pid") == pid for case_id, bundle, pid, _ in proven):
            # Replace runtime-classified events with the independently reparsed
            # source-bound versions; do not double count fixed and pre-fix runs.
            continue
        else:
            retained.append(event)
    derived_events = retained + dmesg_book.events
    counts = {category: sum(event.get("category") == category for event in derived_events)
              for category in COUNTS}
    counts["low_memory_popup"] = counts["low_memory_popup_native"] + counts["low_memory_popup_test"]
    counts["total_events"] = sum(counts[name] for name in
                                  ("startup_failure", "oom_kill", "memory_policy_kill",
                                   "low_memory_popup_native", "low_memory_popup_test"))
    counts["unique_incidents"] = len({event.get("incident") for event in derived_events
                                       if event.get("category") in
                                       ("startup_failure", "oom_kill", "memory_policy_kill",
                                        "low_memory_popup_native", "low_memory_popup_test")})

    native_valid = all(all(row.get("valid") for row in case["native_popup_observations"])
                       for case in cases_report)
    primary_complete = all(case["daily_stage_complete"] and case["daily_barrier_released"] and
                           case["kernel_marker_coverage"].get("coverage_complete") and
                           all(attempt["outcome"] in ("success", "ready", "confirmed_memory_failure")
                               for attempt in case["startup_attempts"]) and
                           case["native_popup_count_after_audit"] == len([
                               event for event in result.get("events", [])
                               if event.get("case_id") == case["case_id"] and
                               event.get("category") == "low_memory_popup_native"])
                           for case in cases_report) and native_valid

    for path in source_paths:
        if path.is_file():
            pass
        else:
            raise FileNotFoundError(path)
    files = [{"path": str(path.relative_to(base)) if path.is_relative_to(base) else str(path),
              "sha256": sha256(path), "size_bytes": path.stat().st_size}
             for path in sorted(source_paths, key=lambda p: str(p).lower())]
    output = args.output or (base / "scene4_source_bound_reclassification.json")
    audit = {
        "schema_version": 1,
        "audit_type": "scene4_pilot_source_bound_reclassification",
        "source_result": {"path": "result.json", "sha256": sha256(result_path),
                          "original_measurement_complete": result.get("measurement_complete"),
                          "original_measurement_evidence_complete": result.get("measurement_evidence_complete"),
                          "original_counts": result.get("counts")},
        "raw_result_modified": False,
        "classification_rule": "Only marker-bounded mem_oom victim selection plus same PID process-exit, exact participating UID/start_ticks lifetime, case peak_begin, and no startup_ready count as OOM and linked startup_failure.",
        "source_files": files,
        "cases": cases_report,
        "removed_timeouts": removed_timeouts,
        "derived_events": derived_events,
        "derived_counts": counts,
        "primary_event_evidence_complete": primary_complete,
        "measurement_complete_original_preserved": result.get("measurement_complete"),
        "auxiliary_gaps_preserved": result.get("evidence_collection_errors", []) + result.get("telemetry_errors", []),
        "auxiliary_tracefs_oom_coverage": result.get("oom_trace_observations"),
        "limitations": [
            "This is a derived audit; original result.json counts and completeness flags are unchanged.",
            "Auxiliary tracefs and hisysevent errors remain visible and are not repaired by this audit.",
            "The audit confirms event-source evidence only; it does not make the run formal acceptance or a completed repeatability measurement.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"audit_path": str(output.resolve()), "source_result_sha256": sha256(result_path),
                      "derived_counts": counts, "primary_event_evidence_complete": primary_complete,
                      "raw_result_modified": False}, ensure_ascii=False, indent=2))
    return 0 if primary_complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
