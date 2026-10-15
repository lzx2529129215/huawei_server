"""Independent browser-donor A/B entry with owned HAP and service lifecycle."""
from __future__ import annotations

import argparse
import configparser
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FROZEN_RUNNER = ROOT / "results" / "daily_lead90_20261012_1640" / "candidate_runner"
PROFILE = json.loads((HERE / "profile.json").read_text(encoding="utf-8"))
EXPECTED_BROWSER = "com.huawei.hmos.browser"
TARGETS = PROFILE["targets"]


def local_native_plan_module():
    """Load this variant's plan serializer without colliding with frozen module names."""
    path = HERE / "native_plan.py"
    spec = importlib.util.spec_from_file_location("browser_trial_native_plan", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load browser-trial native plan serializer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_browser_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    readiness = data.get("readiness", {})
    latest = (data.get("samples") or [{}])[-1]
    members = latest.get("processes", [])
    required = {
        "schema": data.get("schema") == "browser-donor-preparation-v1",
        "bundle": data.get("bundle") == EXPECTED_BROWSER,
        "eligible": data.get("eligible_for_review") is True,
        "windows": data.get("expected_windows") == 6,
        "pages": data.get("pages_per_window_operator_assertion") == 3,
        "hidden": readiness.get("all_browser_windows_hidden_each_sample") is True,
        "identity": readiness.get("identity_verified_each_sample") is True,
        "all_window_pids_members": readiness.get("every_browser_window_pid_is_group_member") is True,
        "window_count_stable": readiness.get("expected_window_count_stable") is True,
        "members_stable": readiness.get("member_pid_start_sets_equal_across_samples") is True,
        "uid_stable": readiness.get("member_pid_uid_sets_equal_across_samples") is True,
        "positive_score": readiness.get("all_group_app_scores_positive_each_sample") is True,
        "positive_oomadj": readiness.get("all_group_oom_score_adj_positive_each_sample") is True,
        "input_count": len(data.get("device_inputs", [])) == 18,
        "inputs_verified": all(row.get("verified") is True for row in data.get("device_inputs", [])),
        "operator_pages_loaded": data.get("operator_assertions", {}).get("pages_loaded_and_settled") is True,
        "operator_hidden": data.get("operator_assertions", {}).get("all_windows_minimized") is True,
        "members": len(members) == 21 and latest.get("member_count") == 21,
        "anon": 5.07 * 1024**3 * 0.98 <= latest.get("anon_bytes", 0) <= 5.07 * 1024**3 * 1.02,
    }
    if not all(required.values()):
        failed = [key for key, value in required.items() if not value]
        raise ValueError("browser preparation evidence is stale/incomplete: " + ", ".join(failed))
    return {"path": str(path.resolve()), "sha256": sha(path), "bundle": data["bundle"],
            "uid": data["uid"], "member_count": len(members),
            "anon_bytes": latest.get("anon_bytes"), "last_sample_boottime": latest.get("device_boottime_seconds"),
            "windows": 6, "pages": 18, "latest_sample": latest}


def validate_plan(path: Path, manifest: dict) -> dict:
    raw = path.read_text(encoding="ascii")
    lines = raw.splitlines()
    if len(lines) != 3 or lines[0] != "device_browser_donor_plan_v1":
        raise ValueError("browser plan must use device_browser_donor_plan_v1 and one donor row")
    meta, donor = lines[1].split("|"), lines[2].split("|")
    if len(meta) != 5 or len(donor) != 6 or donor[0] != EXPECTED_BROWSER:
        raise ValueError("browser plan field count/scope invalid")
    if int(meta[3]) != 19968 * 1024**2 or meta[4] != "1":
        raise ValueError("browser event must register the exact 19968-MiB peak increment")
    if int(donor[1]) != int(manifest["uid"]) or int(donor[2]) <= 0 or int(donor[3]) <= 0:
        raise ValueError("browser plan UID/PID/start does not match reviewed donor evidence")
    members = donor[5].split(",")
    if len(members) != 21 or len({item.split(":", 1)[0] for item in members}) != 21:
        raise ValueError("browser plan must bind all 21 unique cgroup members")
    identities = [tuple(int(value) for value in item.split(":")) for item in members]
    if any(len(item) != 3 or min(item) <= 0 for item in identities):
        raise ValueError("browser plan member identity is invalid")
    latest = manifest["latest_sample"]
    manifest_members = latest.get("processes", [])
    expected_members = {(int(item["pid"]), int(item["uid"]), int(item["start_ticks"]))
                        for item in manifest_members if item.get("verified") is True}
    if set(identities) != expected_members:
        raise ValueError("browser event plan does not bind the exact verified PID/UID/start member set")
    windows = [item for item in latest.get("browser_windows", []) if item.get("is_browser_window")]
    main_pids = {int(item["pid"]) for item in windows}
    if len(windows) != 6 or len(main_pids) != 1:
        raise ValueError("latest sample does not show six browser windows under one primary PID")
    main_pid = next(iter(main_pids))
    main_member = next((item for item in manifest_members if int(item.get("pid", -1)) == main_pid), None)
    if (main_member is None or int(donor[2]) != main_pid or
            int(donor[3]) != int(main_member.get("start_ticks", -1))):
        raise ValueError("browser plan primary PID/start differs from the six-window browser main process")
    sample_at = float(latest.get("device_boottime_seconds", 0))
    T = float(meta[2])
    if abs((T - sample_at) - 90.0) > 1.0:
        raise ValueError("browser event T must be exactly 90 seconds after the latest verified device sample")
    if len(members) != latest.get("member_count") or int(donor[1]) != int(manifest["uid"]):
        raise ValueError("browser service plan member count/UID differs from reviewed manifest")
    return {"path": str(path.resolve()), "sha256": sha(path), "event_id": meta[0],
            "case_id": meta[0].removesuffix("_peak_transition"), "T": T,
            "sample_boottime": sample_at, "member_count": len(members), "needed_bytes": int(meta[3])}


def validate_service_ini(path: Path, phase: str) -> dict:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read(path, encoding="utf-8")
    if not parser.has_section("daemon"):
        raise ValueError("service ini has no [daemon]")
    daemon = parser["daemon"]
    expected = {
        "daily_async": "true", "daily_known_lead_sec": "90",
        "daily_known_capacity": "true", "daily_known_strength": "true",
        "daily_known_future_free_only": "true", "daily_owned_os_snapshot": "true",
        "daily_browser_donor_profile": "true", "daily_disk_mode": "owned_apply",
        "daily_max_submissions": "0",
    }
    wrong = [key for key, value in expected.items() if daemon.get(key, "").strip().lower() != value]
    if wrong:
        raise ValueError("service ini does not match browser/no-disk protocol: " + ", ".join(wrong))
    for key in ("daily_known_plan_file", "daily_known_evidence_ledger", "control_file"):
        if not daemon.get(key) or not daemon.get(key).startswith("/"):
            raise ValueError(f"service path must be an absolute device path: {key}")
    browser_sections = [section for section in parser.sections()
                        if section.lower() in ("app browser", "app:browser")]
    if len(browser_sections) != 1:
        raise ValueError("service ini must have exactly one [app browser] reclaim profile")
    app_sections = [section for section in parser.sections() if section.lower().startswith("app ") or
                    section.lower().startswith("app:")]
    if app_sections != browser_sections:
        raise ValueError("browser A/B service may register only the browser donor, not synthetic donors")
    browser = parser[browser_sections[0]]
    browser_uid_match = re.fullmatch(r"100/com\.huawei\.hmos\.browser_(\d+)", browser.get("path", ""))
    if not browser_uid_match:
        raise ValueError("browser reclaim path must be bound to its package UID")
    if browser.get("reclaim", "").lower() != "true" or int(browser.get("min_keep_mb", "0")) < 128:
        raise ValueError("browser reclaim floor must be at least 128 MiB")
    template_whole_work = int(daemon.get("daily_disk_whole_work_mb", "0"))
    if template_whole_work < 0:
        raise ValueError("daily_disk_whole_work_mb must be nonnegative before the browser-profile override")
    return {"path": str(path.resolve()), "sha256": sha(path), "phase": phase,
            "control_file": daemon["control_file"],
            "known_plan_file": daemon["daily_known_plan_file"],
            "known_evidence_ledger": daemon["daily_known_evidence_ledger"],
            "intended_paused": phase == "baseline", "daily_disk_mode": daemon["daily_disk_mode"],
            "template_daily_disk_whole_work_mb": template_whole_work,
            "effective_browser_whole_work_cap_mib": PROFILE["load"]["disk_whole_work_cap_mib"],
            "browser_section": browser_sections[0], "browser_path": browser["path"],
            "browser_uid": int(browser_uid_match.group(1))}


def device_shell(hdc: Path, target: str, command: str) -> str:
    result = subprocess.run([str(hdc), "-t", target, "shell", command],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=45, check=False)
    output = result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    if result.returncode:
        raise RuntimeError(f"device preflight failed ({result.returncode}): {output[:500]}")
    return output


def runtime_preflight(hdc: Path, target: str, phase: str, service: dict,
                      daemon_remote: str, config_remote: str, expected_pid: int | None,
                      plan: dict) -> dict:
    online_raw = subprocess.run([str(hdc), "list", "targets"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=20, check=False).stdout.decode(
                                    "utf-8", "replace")
    if target not in online_raw:
        raise RuntimeError(f"requested HDC target is not online: {target}")
    enforcing = device_shell(hdc, target, "getenforce").strip()
    if enforcing != "Enforcing":
        raise RuntimeError(f"SELinux must remain Enforcing; observed {enforcing!r}")
    pids_raw = device_shell(hdc, target, "pidof memshiftd").strip().split()
    if len(pids_raw) != 1 or not pids_raw[0].isdigit():
        raise RuntimeError(f"expected exactly one resident memshiftd, observed {pids_raw}")
    pid = int(pids_raw[0])
    if expected_pid is not None and pid != expected_pid:
        raise RuntimeError(f"resident service PID changed: expected {expected_pid}, got {pid}")
    cmdline = device_shell(hdc, target, f"tr '\\000' ' ' < /proc/{pid}/cmdline").strip()
    executable = device_shell(hdc, target, f"readlink /proc/{pid}/exe").strip()
    if executable != daemon_remote or config_remote not in cmdline:
        raise RuntimeError("resident service executable/config does not match requested browser-profile instance")
    control = device_shell(hdc, target, f"cat {service['control_file']}")
    paused_match = re.search(r'"?paused"?\s*[:=]\s*(true|false)', control, re.I)
    expected_paused = "true" if phase == "baseline" else "false"
    if not paused_match or paused_match.group(1).lower() != expected_paused:
        raise RuntimeError(f"service control state does not match {phase}: expected paused={expected_paused}")
    remote_ini_hash = device_shell(hdc, target, f"sha256sum {config_remote}").split()[0].lower()
    if remote_ini_hash != service["sha256"]:
        raise RuntimeError("device service INI hash differs from reviewed local INI")
    remote_plan_hash = device_shell(hdc, target,
        f"sha256sum {service['known_plan_file']}").split()[0].lower()
    if remote_plan_hash != plan["sha256"]:
        raise RuntimeError("device browser event plan hash differs from reviewed local plan")
    return {"target": target, "selinux": enforcing, "service_pid": pid,
            "service_cmdline": cmdline, "service_executable": executable, "control_raw": control,
            "paused": paused_match.group(1).lower() == "true",
            "device_ini_sha256": remote_ini_hash, "device_plan_sha256": remote_plan_hash,
            "read_only": True}


def _verify_targets_still_ready(device, ready_path: Path, case_id: str) -> dict:
    proof = json.loads(ready_path.read_text(encoding="utf-8"))
    if proof.get("schema") == "browser_trial_target_preparation_v1":
        proof = proof.get("target_daily_ready", {})
    if proof.get("schema") != "browser_trial_three_target_daily_ready_v1" or proof.get("validated") is not True:
        raise ValueError("target-ready proof schema is invalid")
    if proof.get("case_id") != case_id or set(proof.get("targets", {})) != set(TARGETS):
        raise ValueError("target-ready proof does not match this browser plan")
    from memscenes.device import quote
    for bundle in TARGETS:
        row = proof["targets"][bundle]
        # Latest HAP journal and live /proc identity must still be the same daily generation.
        spec = device.app(bundle)
        _, events = device.journal(bundle, case_id + "_daily")
        ready = next((item for item in reversed(events) if item.get("event") == "daily_ready"), None)
        if (not ready or int(ready.get("pid", -1)) != int(row["pid"]) or
                str(ready.get("start_ticks")) != str(row["start_ticks"]) or
                int(ready.get("uid", -1)) != int(spec["uid"]) or int(spec["uid"]) != int(row["uid"])):
            raise RuntimeError(f"daily HAP identity/readiness changed since preparation: {bundle}")
        observed = device.inspect_process_identity(int(row["pid"]), timeout=15).get("identity") or {}
        if (int(observed.get("pid", -1)) != int(row["pid"]) or
                str(observed.get("start_ticks", "")) != str(row["start_ticks"]) or
                int(observed.get("uid", -1)) != int(row["uid"])):
            raise RuntimeError(f"daily HAP process identity is stale: {bundle}")
    return proof


def _owned_case_ids(output_dir: Path, base_case_id: str) -> dict[str, list[str]]:
    """Recover only retry case IDs emitted by this native plan's request ledger."""
    BUNDLES = tuple(TARGETS)
    by_bundle = {bundle: {base_case_id + "_daily", base_case_id} for bundle in BUNDLES}
    plan_path = output_dir / "native_retry_plan.tsv"
    plan_hash = sha(plan_path) if plan_path.is_file() else ""
    attempt_prefix = "s4_" + plan_hash[:24] + "_c" if plan_hash else None
    for ledger in (output_dir / "requests.tsv", output_dir / "emergency_requests.tsv"):
        if not ledger.is_file():
            continue
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) < 4 or fields[0] != "retry_pending":
                continue
            try:
                target_index = int(fields[2])
            except ValueError:
                continue
            if target_index not in range(3):
                continue
            attempt_id = fields[3]
            if attempt_prefix and attempt_id.startswith(attempt_prefix) and re.fullmatch(r"s4_[0-9a-f]{24}_c\d{4}", attempt_id):
                by_bundle[BUNDLES[target_index]].add(attempt_id)
    return {bundle: sorted(values) for bundle, values in by_bundle.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("baseline", "optimized"), required=True)
    parser.add_argument("--service-ini", type=Path)
    parser.add_argument("--browser-manifest", type=Path)
    parser.add_argument("--browser-plan", type=Path)
    parser.add_argument("--hdc", type=Path, required=True)
    parser.add_argument("--target", default="3QC0124C03000514")
    parser.add_argument("--candidate-daemon", type=Path,
                        help="local browser-profile service binary produced by the sibling build")
    parser.add_argument("--original-backups", type=Path,
                        default=ROOT / "results" / "browser_donor_integration_20261013")
    parser.add_argument("--target-ready", type=Path,
                        help="proof emitted by --prepare-targets; execute does not silently rebuild the workload")
    parser.add_argument("--case-id", help="s4_<lowercase hex>_c0001; used by --prepare-targets")
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--prepare-targets", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.prepare_targets:
        if not args.case_id or not re.fullmatch(r"s4_[0-9a-f]{12}_c0001", args.case_id):
            parser.error("--prepare-targets requires --case-id s4_<12 lowercase hex>_c0001")
        if not args.hdc.is_file():
            raise ValueError(f"HDC executable missing: {args.hdc}")
        sys.path.insert(0, str(FROZEN_RUNNER))
        from memscenes.device import Device
        from target_prepare import prepare_targets
        device = Device(str(args.hdc), args.target)
        ready = prepare_targets(device, args.case_id, args.output.parent, PROFILE)
        proof = {"schema": "browser_trial_target_preparation_v1", "case_id": args.case_id,
            "target_daily_ready": ready, "target": args.target,
            "boot_id": device.shell("cat /proc/sys/kernel/random/boot_id").strip(),
            "selinux": device.shell("getenforce").strip(),
            "operation": "prepared_only_no_pressure_controller"}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(proof, ensure_ascii=False, indent=2))
        return 0
    if not args.service_ini or not args.browser_manifest or not args.browser_plan:
        parser.error("this mode requires --service-ini, --browser-manifest and --browser-plan")
    donor = validate_browser_manifest(args.browser_manifest)
    plan = validate_plan(args.browser_plan, donor)
    service = validate_service_ini(args.service_ini, args.phase)
    if not args.hdc.is_file() or not args.candidate_daemon or not args.candidate_daemon.is_file():
        raise ValueError(f"HDC or candidate daemon missing: {args.hdc}")
    if service["browser_uid"] != int(donor["uid"]):
        raise ValueError("service browser profile UID differs from donor manifest UID")
    proof = {"schema": "browser_donor_trial_preflight_v1", "variant": PROFILE["variant"],
             "phase": args.phase, "profile": PROFILE, "donor": donor, "plan": plan,
             "service": service, "pressure_targets": list(TARGETS),
             "target_installation_check": "bm dump + live memcg PID/start identity before T",
             "hdc": str(args.hdc.resolve()),
             "target": args.target, "device_execution": "not_run"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.preflight_only:
        proof["candidate_daemon"] = {"path": str(args.candidate_daemon.resolve()), "sha256": sha(args.candidate_daemon)}
        proof["original_backup_set"] = str(args.original_backups.resolve())
        args.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(proof, ensure_ascii=False, indent=2))
        return 0
    sys.path.insert(0, str(FROZEN_RUNNER))
    from memscenes.device import Device
    device = Device(str(args.hdc), args.target)
    if not args.target_ready:
        raise ValueError("--execute requires the --target-ready proof created for this exact browser plan")
    from target_prepare import cleanup_targets
    try:
        target_ready = _verify_targets_still_ready(device, args.target_ready, plan["case_id"])
    except BaseException:
        try:
            raw_ready = json.loads(args.target_ready.read_text(encoding="utf-8"))
            owned_ready = raw_ready.get("target_daily_ready", raw_ready)
            if (owned_ready.get("schema") == "browser_trial_three_target_daily_ready_v1" and
                    owned_ready.get("case_id") == plan["case_id"]):
                cleanup_targets(device, owned_ready, plan["case_id"])
        except Exception:
            pass
        raise
    try:
        boot_id = device.shell("cat /proc/sys/kernel/random/boot_id").strip()
        now = float(device.shell("cat /proc/uptime").split()[0])
        if boot_id == "" or plan["T"] <= now + 50:
            raise RuntimeError("fixed device T has insufficient lead for safe native staging")
        serialize_native_plan = local_native_plan_module().serialize
        native_plan_path = args.output.parent / "native_retry_plan.tsv"
        identities = {bundle: {key: target_ready["targets"][bundle][key]
                               for key in ("uid", "pid", "start_ticks")} for bundle in TARGETS}
        native_plan_path.write_text(serialize_native_plan(plan["case_id"], boot_id, plan["T"], identities),
                                    encoding="ascii", newline="\n")
        proof["target_daily_ready"] = target_ready
        proof["native_plan"] = {"path": str(native_plan_path.resolve()), "sha256": sha(native_plan_path),
            "schema": "device_retry_plan_browser_donor_3target_v1", "T": plan["T"],
            "boot_id": boot_id, "identities": identities}
        from service_lifecycle import BrowserServiceLifecycle
        from native_bridge import execute as execute_native, start_kernel_collector, stop_reader
        lifecycle = BrowserServiceLifecycle(device, args.phase, args.service_ini,
            args.browser_plan, donor["uid"], args.original_backups, args.output.parent)
        lifecycle.daemon_local = args.candidate_daemon.resolve(strict=True)
    except BaseException:
        try:
            cleanup_targets(device, target_ready, plan["case_id"])
        except Exception:
            pass
        raise
    execution_error = None
    execution_traceback = None
    collector_holder = {}
    try:
        with lifecycle:
            def arm_collector():
                state = start_kernel_collector(device, args.output.parent)
                collector_holder["state"] = state
                proof["kernel_collector_armed_before_service_stop"] = {
                    "remote_root": state["remote_root"],
                    "capture_start_boottime": state["capture_start_boottime"],
                    "collector_ready_boottime": state["collector_ready_boottime"],
                    "collector_sha256": state["collector_sha256"],
                    "pid": state["reader"].pid,
                    "start_ticks": state["reader"].start_ticks}
            lifecycle.activate(before_original_stop=arm_collector)
            proof["device_preflight"] = {"selinux": device.shell("getenforce").strip(),
                "boot_id": boot_id, "boottime": now, "service_swap": "verified_single_instance"}
            proof["native_execution"] = execute_native(device, native_plan_path, args.output.parent,
                collector_state=collector_holder.get("state"))
    except Exception as exc:
        execution_error = f"{type(exc).__name__}: {exc}"
        execution_traceback = traceback.format_exc()
    finally:
        state = collector_holder.get("state")
        if state and not state.get("done"):
            try:
                proof["pre_native_collector_cleanup"] = stop_reader(
                    state["reader"], device, state["remote_root"], args.output.parent)
                state["done"] = True
            except Exception as exc:
                proof["pre_native_collector_cleanup"] = {"terminated": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        try:
            proof["target_cleanup"] = cleanup_targets(device, target_ready, plan["case_id"],
                _owned_case_ids(args.output.parent, plan["case_id"]))
        except Exception as exc:
            proof["target_cleanup"] = {"verified": False, "error": f"{type(exc).__name__}: {exc}"}
    proof["service_restore"] = lifecycle.restore_result
    cleanup = proof.get("target_cleanup", {})
    cleanup_ok = set(cleanup) == set(TARGETS) and all(
        item.get("verified_absent") is True for item in cleanup.values())
    proof["device_execution"] = ("completed" if not execution_error and
        proof.get("native_execution", {}).get("device_execution") == "completed" and
        lifecycle.restore_result.get("verified") is True and cleanup_ok else "invalid")
    if execution_error:
        proof["execution_error"] = execution_error
        proof["execution_traceback"] = execution_traceback
    if "native_execution" not in proof:
        proof["native_execution"] = {"error": execution_error or "execution aborted before native controller"}
    args.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(proof, ensure_ascii=False, indent=2))
    return 0 if proof["device_execution"] == "completed" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
