"""Local-only gate launch specification and audit for the bounded 8.75 GiB diagnostic.

This module does not connect to a device or launch a process. The standalone 875g
runner still uses the strict v7 gate and is intentionally not modified here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE_BINARY = ROOT / "results" / "scene4_retry_compare_20261010_001631" / "gate_guard_v8_candidate" / "native_source" / "gate_window"
GATE_MANIFEST = GATE_BINARY.with_name("gate_window_v8_manifest_v4.json")
EXPECTED_GATE_SHA256 = "1f21af9b06aec90543e24bffb3f234bb540b74c713a7f1b02f1dc88bed3e732e"
EXPECTED_SCHEMA = "fixed_boottime_gate_guard_v8_candidate_receipt_v4"
EXPECTED_POLLS = 1862
BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
DEVICE_PATH = re.compile(r"/data/local/tmp/[A-Za-z0-9_./-]+\Z")
ORIGINAL_HASHES = {
    "daemon_sha256": "60b871e051e7fb005fc7c4882ef1641374f9cb4bbb56b69574c216aa0904881f",
    "ini_sha256": "a081620837715a385ff5c4bd681c4a4a28443fbca609bd55b89d4f75b223d722",
    "control_sha256": "5062cae48b51ed17573558e265b80bed7905001e6cb2024aee3b03290becb80d",
}
TARGET_BUNDLES = tuple(f"io.memscene.scene4.workload.a{i}" for i in (14, 15, 16))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_candidate(binary: Path = GATE_BINARY, manifest_path: Path = GATE_MANIFEST) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = sha256(binary)
    if manifest.get("schema") != EXPECTED_SCHEMA or manifest.get("binary_sha256", "").lower() != EXPECTED_GATE_SHA256:
        raise ValueError("guarded gate manifest schema/hash does not match the reviewed v4 candidate")
    if digest.lower() != EXPECTED_GATE_SHA256:
        raise ValueError("guarded gate binary does not match the reviewed v4 candidate")
    if manifest.get("device_execution") != "not_run":
        raise ValueError("candidate receipt no longer records a local-only, not-run state")
    return {"binary": str(binary.resolve()), "manifest": str(manifest_path.resolve()),
            "sha256": digest, "schema": manifest["schema"], "candidate_not_deployed": True}


def make_gate_argv(remote_binary: str, T: float, boot_id: str, remote_prefix: str,
                   *, accept_bounded_diagnostic_only: bool = False) -> list[str]:
    if not accept_bounded_diagnostic_only:
        raise ValueError("launch spec requires explicit --accept-bounded-diagnostic-only")
    if not math.isfinite(T) or T <= 1:
        raise ValueError("fixed CLOCK_BOOTTIME T must be finite and greater than 1")
    if not BOOT_ID.fullmatch(boot_id):
        raise ValueError("expected boot ID is malformed")
    if not DEVICE_PATH.fullmatch(remote_binary) or not DEVICE_PATH.fullmatch(remote_prefix):
        raise ValueError("remote gate and output paths must be absolute /data/local/tmp paths")
    return [remote_binary, "--T", f"{T:.9f}", "--boot", boot_id, "--out", remote_prefix,
            "--open-offset", "930", "--watchdog-offset", "935", "--reclose-once"]


def _records(text: str, prefix: str) -> list[tuple[str, dict[str, str]]]:
    rows = []
    for line in text.splitlines():
        if not line.startswith(prefix + "|"):
            continue
        fields = line.strip().split("|")
        data: dict[str, str] = {}
        for item in fields[1:]:
            if "=" in item:
                key, value = item.split("=", 1)
                data[key] = value
        if prefix == "PREFLIGHT" and len(fields) > 1:
            data["T"] = fields[1]
        rows.append((line, data))
    return rows


def _one(rows: list[tuple[str, dict[str, str]]], label: str, errors: list[str]) -> dict[str, str]:
    if len(rows) != 1:
        errors.append(f"expected exactly one {label} record; found {len(rows)}")
        return {}
    return rows[0][1]


def _poll_schedule_ok(events: str, T: float, initial_counter: str) -> tuple[int, bool]:
    rows = [line.split("|") for line in events.splitlines() if line.startswith("POLL|")]
    if len(rows) != EXPECTED_POLLS:
        return len(rows), False
    enable_seen = 0
    for index, fields in enumerate(rows):
        if len(fields) < 8:
            return len(rows), False
        try:
            due = float(fields[1])
            observed = float(fields[2])
        except ValueError:
            return len(rows), False
        expected_due = T - 0.5 + index * 0.5
        if (not math.isfinite(due) or not math.isfinite(observed) or
                abs(due - expected_due) > 0.01 or observed < due or
                fields[4] != "1" or fields[5] != "reclaim_in_enable" or
                fields[6] != initial_counter):
            return len(rows), False
        if fields[3] == "enable" and fields[7] == "reclosed_once":
            enable_seen += 1
        elif fields[3] != "readonly":
            return len(rows), False
    return len(rows), enable_seen == 1


def audit_bounded_diagnostic(*, events: str, watchdog: str, expected_boot: str, T: float,
                             native_returncode: int | None, collector: dict, oom_audit: dict,
                             service_restore: dict, target_cleanup: dict,
                             observed_boot: str | None) -> dict:
    """Return strict and bounded outcomes separately; unknown evidence never passes."""
    errors: list[str] = []
    if not BOOT_ID.fullmatch(expected_boot) or observed_boot != expected_boot:
        errors.append("device boot ID is missing or differs from the registered boot")
    pre_rows = _records(events, "PREFLIGHT")
    helper_rows = _records(events, "GATE_HELPER")
    coverage_rows = _records(events, "GATE_IO_COVERAGE")
    result_rows = _records(events, "GATE_RESULT")
    final_rows = _records(events, "FINAL")
    watchdog_exit_rows = _records(events, "WATCHDOG_EXIT")
    watchdog_rows = _records(watchdog, "WATCHDOG")
    pre = _one(pre_rows, "PREFLIGHT", errors)
    _one(helper_rows, "GATE_HELPER", errors)
    coverage = _one(coverage_rows, "GATE_IO_COVERAGE", errors)
    result = _one(result_rows, "GATE_RESULT", errors)
    final_line, final = (final_rows[0] if len(final_rows) == 1 else ("", {}))
    if len(final_rows) != 1:
        errors.append(f"expected exactly one FINAL record; found {len(final_rows)}")
    exit_record = _one(watchdog_exit_rows, "WATCHDOG_EXIT", errors)
    watch_record = _one(watchdog_rows, "WATCHDOG", errors)

    reclose_results = _records(events, "RECLOSE_RESULT")
    reclose_attempts = _records(events, "RECLOSE_ATTEMPT")
    polls, poll_schedule_ok = _poll_schedule_ok(events, T, pre.get("write_total_size", "unknown"))
    try:
        t_matches = abs(float(pre.get("T", "nan")) - T) <= 0.01
    except ValueError:
        t_matches = False
    if not t_matches:
        errors.append("PREFLIGHT fixed T does not match the plan")
    try:
        offsets_match = (abs(float(pre.get("close_at", "nan")) - (T - 1.0)) <= 0.01 and
                         abs(float(pre.get("open_at", "nan")) - (T + 930.0)) <= 0.01 and
                         abs(float(pre.get("watchdog_at", "nan")) - (T + 935.0)) <= 0.01 and
                         coverage.get("open_at") is not None and
                         abs(float(coverage["open_at"]) - (T + 930.0)) <= 0.01)
    except (TypeError, ValueError):
        offsets_match = False
    if not offsets_match:
        errors.append("gate close/open/watchdog offsets differ from fixed T−1/T+930/T+935")
    preflight_ok = (pre.get("boot_match") == "1" and pre.get("proc") == "enable" and
                    pre.get("reclaim_in") == "enable" and pre.get("reclose_once") == "enabled" and
                    pre.get("write_total_size", "unknown").isdecimal())
    no_gate_invalid = "GATE_INVALID|" not in events
    if not no_gate_invalid:
        errors.append("gate log contains a GATE_INVALID record")
    if not preflight_ok:
        errors.append("initial gate/reclaim/counter/reclose preflight is not fully verified")

    coverage_ok = (coverage.get("poll_count") == str(EXPECTED_POLLS) and
                   coverage.get("expected_poll_count") == str(EXPECTED_POLLS) and
                   polls == EXPECTED_POLLS and poll_schedule_ok and coverage.get("no_new_disk_write_observed") == "1" and
                   coverage.get("bounded_control_complete") == "1" and
                   coverage.get("bounded_control_scope") == "through_open_not_restoration" and
                   coverage.get("reclose_used") == "1" and coverage.get("write_counter_stable") == "1" and
                   coverage.get("strict_continuous_readonly") == "0" and
                   result.get("no_new_disk_write_observed") == "1" and
                   result.get("bounded_control_complete") == "1" and
                   result.get("bounded_control_scope") == "through_open_not_restoration" and
                   result.get("reclose_once") == "enabled")
    if not coverage_ok:
        errors.append("gate coverage/poll schedule is incomplete, unknown, or does not prove the bounded observation")
    reclose_ok = (len(reclose_attempts) == 1 and len(reclose_results) == 1 and
                  reclose_results[0][1].get("success") == "1" and
                  reclose_results[0][1].get("write_invoked") == "1" and
                  reclose_results[0][1].get("readback") == "readonly" and
                  reclose_results[0][1].get("boot_match") == "1" and
                  reclose_results[0][1].get("counter_before", "unknown").isdecimal() and
                  reclose_results[0][1].get("counter_before") == reclose_results[0][1].get("counter_after") and
                  reclose_results[0][1].get("reclaim_after") == "enable")
    if not reclose_ok:
        errors.append("exactly one successful reclose is not proven")
    final_ok = (final.get("proc") == "enable" and final.get("boot") == expected_boot and
                final.get("boot_match") == "1" and result.get("final_boot_match") == "1" and
                result.get("restoration_verified") == "1" and exit_record.get("reaped") == "1" and
                exit_record.get("status") == "0" and watch_record.get("boot_match") == "1" and
                watch_record.get("readback") == "enable" and watch_record.get("write_ok") == "1")
    if not final_ok:
        errors.append("same-boot gate/watchdog restoration is not completely verified")
    if native_returncode != 0:
        errors.append("native controller did not complete with return code 0")

    hb = collector.get("last_heartbeat") if isinstance(collector.get("last_heartbeat"), dict) else {}
    protocol = collector.get("protocol_review") if isinstance(collector.get("protocol_review"), dict) else {}
    def explicit_zero(value) -> bool:
        return value is not None and str(value) == "0"
    capture_ok = (collector.get("continuous_capture_proven") is True and
                  collector.get("horizon_reached") is True and
                  oom_audit.get("capture_start_verified") is True and
                  protocol.get("verified") is True and
                  all(explicit_zero(hb.get(key)) for key in ("invalid", "gaps", "unconfirmed_records")) and
                  explicit_zero(collector.get("epipe_count")) and
                  explicit_zero(collector.get("async_invalid_count")) and
                  collector.get("heartbeat_interval_over_15s") is False and
                  explicit_zero(oom_audit.get("explicit_victim_rows_without_timestamp")) and
                  explicit_zero(oom_audit.get("unresolved_oom_context_count")))
    if not capture_ok:
        errors.append("full raw kernel collector/OOM-context evidence is incomplete or unknown")
    restore_ok = (service_restore.get("verified") is True and
                  all(service_restore.get(key) == value for key, value in ORIGINAL_HASHES.items()))
    if not restore_ok:
        errors.append("original service single-instance/hash restoration is not verified")
    targets_ok = (set(target_cleanup) == set(TARGET_BUNDLES) and
                  all(isinstance(target_cleanup.get(k), dict) and
                      target_cleanup[k].get("verified_absent") is True for k in TARGET_BUNDLES))
    if not targets_ok:
        errors.append("all three owned target HAPs are not verified absent")

    reclose_observed = bool(reclose_attempts or reclose_results or
                            _records(events, "REOPEN_OBSERVED"))
    strict_readonly = (not reclose_observed and result.get("strict_continuous_readonly") == "1")
    original_gate_valid = (strict_readonly and result.get("valid") == "1" and
        preflight_ok and t_matches and offsets_match and no_gate_invalid and
        len(helper_rows) == 1 and native_returncode == 0 and
        result.get("close_readback_verified") == "1" and result.get("open_readback_verified") == "1" and
        result.get("io_write_counter_stable") == "1" and "GATE_INVALID|" not in events and
        polls == EXPECTED_POLLS and coverage_ok and final_ok)
    bounded_ok = all((preflight_ok, t_matches, coverage_ok, reclose_ok, final_ok,
                      offsets_match, native_returncode == 0, capture_ok, restore_ok, targets_ok,
                      observed_boot == expected_boot, no_gate_invalid, result.get("valid") == "0",
                      result.get("strict_continuous_readonly") == "0"))
    return {"mode": "bounded_reclose_diagnostic_only",
        "strict_continuous_readonly": strict_readonly,
        "original_gate_valid": original_gate_valid,
        "valid_for_oom_comparison": original_gate_valid and capture_ok and restore_ok and targets_ok and native_returncode == 0,
        "bounded_diagnostic_valid": bounded_ok,
        "bounded_diagnostic_is_strict_no_reopen_proof": False,
        "bounded_control_complete": coverage.get("bounded_control_complete"),
        "no_new_disk_write_observed": coverage.get("no_new_disk_write_observed"),
        "physical_zero_write_guaranteed": False,
        "poll_count": polls,"expected_poll_count": EXPECTED_POLLS,
        "reclose_attempt_count": len(reclose_attempts),"reclose_success_count": sum(r[1].get("success") == "1" for r in reclose_results),
        "reclose_observed": reclose_observed,"restoration_verified": final_ok,
        "collector_complete": capture_ok,"service_restore_verified": restore_ok,
        "target_cleanup_verified": targets_ok,"native_returncode": native_returncode,
        "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    emit = sub.add_parser("emit-launch-argv", help="print a device-side argv spec; does not launch it")
    emit.add_argument("--remote-binary", required=True)
    emit.add_argument("--remote-prefix", required=True)
    emit.add_argument("--T", type=float, required=True)
    emit.add_argument("--boot-id", required=True)
    emit.add_argument("--accept-bounded-diagnostic-only", action="store_true")
    args = parser.parse_args()
    if args.command == "emit-launch-argv":
        candidate = verify_candidate()
        argv = make_gate_argv(args.remote_binary, args.T, args.boot_id, args.remote_prefix,
                              accept_bounded_diagnostic_only=args.accept_bounded_diagnostic_only)
        print(json.dumps({"candidate": candidate, "argv": argv, "shell_quoted": shlex.join(argv),
            "launched": False,"strict_mode_claim": False}, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
