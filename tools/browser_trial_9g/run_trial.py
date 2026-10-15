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


def _verified_browser_member_identities(data: dict) -> list[tuple[int, int, int]]:
    """Validate the observed group across every sample; count is deliberately dynamic."""
    samples = data.get("samples", [])
    if not isinstance(samples, list) or len(samples) != 3:
        raise ValueError("browser preparation must contain exactly three identity samples")
    package_uid = int(data.get("uid", 0))
    reference = None
    for sample_index, sample in enumerate(samples):
        processes = sample.get("processes", [])
        pids = sample.get("member_pids", [])
        pids_after = sample.get("member_pids_after", [])
        count = sample.get("member_count")
        if not isinstance(processes, list) or not 1 <= len(processes) <= 64:
            raise ValueError(f"browser member group is empty or exceeds the service limit of 64 at sample {sample_index}")
        try:
            identities = [(int(row["pid"]), int(row["uid"]), int(row["start_ticks"]))
                          for row in processes]
            before = [int(pid) for pid in pids]
            after = [int(pid) for pid in pids_after]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"browser member identity is incomplete at sample {sample_index}") from exc
        observed_pids = [item[0] for item in identities]
        if (count != len(processes) or len(set(observed_pids)) != len(observed_pids) or
                sorted(before) != sorted(observed_pids) or sorted(after) != sorted(observed_pids)):
            raise ValueError(f"browser member count/PID lists do not match process identities at sample {sample_index}")
        if any(pid <= 0 or uid <= 0 or start <= 0 for pid, uid, start in identities):
            raise ValueError(f"browser member PID/UID/start identity is invalid at sample {sample_index}")
        if any(row.get("verified") is not True or
               str(row.get("start_ticks")) != str(row.get("start_ticks_after")) or
               row.get("cgroup_matches_expected_leaf") is not True or
               not isinstance(row.get("oom_score_adj"), int) or row["oom_score_adj"] <= 0
               for row in processes):
            raise ValueError(f"browser member identity/cgroup/oomadj verification failed at sample {sample_index}")
        if sample.get("identity_verified") is not True or int(sample.get("uid", -1)) != package_uid:
            raise ValueError(f"browser group identity/package UID is not verified at sample {sample_index}")
        identity_set = set(identities)
        if len(identity_set) != len(identities):
            raise ValueError(f"duplicate browser member identity at sample {sample_index}")
        if reference is None:
            reference = identity_set
        elif identity_set != reference:
            raise ValueError("browser member PID/UID/start identities drift across samples")

        windows = [row for row in sample.get("browser_windows", []) if row.get("is_browser_window")]
        member_pid_set = set(observed_pids)
        if (len(windows) != 6 or len({int(row.get("pid", -1)) for row in windows}) != 1 or
                any(int(row.get("pid", -1)) not in member_pid_set or
                    row.get("is_visible") is not False or
                    int(row.get("detail_pid", -1)) != int(row.get("pid", -2)) or
                    int(row.get("detail_win_id", -1)) != int(row.get("win_id", -2))
                    for row in windows)):
            raise ValueError(f"six hidden browser windows are not all assigned to the verified member group at sample {sample_index}")
        main_pid = int(windows[0]["pid"])
        main_member = next((row for row in processes if int(row["pid"]) == main_pid), None)
        if main_member is None or int(main_member["uid"]) != package_uid:
            raise ValueError(f"browser main PID does not use the package UID at sample {sample_index}")
        if not isinstance(sample.get("app_score"), int) or sample["app_score"] <= 0:
            raise ValueError(f"browser app score is not positive at sample {sample_index}")
    return sorted(reference or ())


def _anon_sample_stats(data: dict) -> dict:
    samples = data.get("samples", [])
    if not isinstance(samples, list) or len(samples) != 3:
        raise ValueError("browser anonymous-capacity gate requires exactly three samples")
    values = []
    for index, sample in enumerate(samples):
        value = sample.get("anon_bytes") if isinstance(sample, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"browser anon_bytes must be a positive integer at sample {index}")
        values.append(value)
    low, high = min(values), max(values)
    spread = (high - low) / high
    if spread > 0.02:
        raise ValueError(f"browser anon samples exceed the 2% stability spread: {spread:.6%}")
    return {"sample_count": len(values), "min_bytes": low, "max_bytes": high,
            "mean_bytes": sum(values) / len(values), "spread_fraction_of_max": spread,
            "max_spread_fraction": 0.02, "within_limit": True}


def _target_cleanup_verified(proof: dict) -> bool:
    cleanup = proof.get("target_cleanup", {})
    return set(cleanup) == set(TARGETS) and all(
        cleanup.get(bundle, {}).get("verified_absent") is True for bundle in TARGETS)


def _workload_fingerprint(data: dict) -> str:
    """Fingerprint the exact 18 offline page payloads and their window slots."""
    local_rows = data.get("local_inputs", [])
    device_rows = data.get("device_inputs", [])
    if not isinstance(local_rows, list) or not isinstance(device_rows, list) or len(local_rows) != 18 or len(device_rows) != 18:
        raise ValueError("browser workload fingerprint requires exactly 18 local and device inputs")
    local_by_path = {}
    for row in local_rows:
        if not isinstance(row, dict):
            raise ValueError("browser workload local input row is malformed")
        path = row.get("device_path")
        if not isinstance(path, str) or not path or path in local_by_path:
            raise ValueError("browser workload has a missing or duplicate device path")
        local_by_path[path] = row
    device_by_path = {}
    for row in device_rows:
        if not isinstance(row, dict):
            raise ValueError("browser workload device input row is malformed")
        path = row.get("device_path")
        if not isinstance(path, str) or not path or path in device_by_path or row.get("verified") is not True:
            raise ValueError("browser workload has an unverified, missing, or duplicate device input")
        device_by_path[path] = row
    if set(local_by_path) != set(device_by_path):
        raise ValueError("browser workload local/device page paths differ")
    rows = []
    for path in sorted(local_by_path):
        local, device = local_by_path[path], device_by_path[path]
        digest = local.get("sha256")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                device.get("sha256") != digest or
                not isinstance(local.get("window_label"), str) or
                not isinstance(local.get("page_label"), str) or
                not isinstance(local.get("size_bytes"), int) or local["size_bytes"] <= 0):
            raise ValueError("browser workload local/device content hashes or page metadata differ")
        rows.append({"device_path": path, "window_label": local["window_label"],
                     "page_label": local["page_label"], "size_bytes": local["size_bytes"],
                     "sha256": digest})
    labels = {(row["window_label"], row["page_label"]) for row in rows}
    if len(labels) != 18:
        raise ValueError("browser workload must contain 18 distinct window/page slots")
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_baseline_reference(path: Path | None, phase: str, donor: dict, plan: dict) -> dict:
    if phase == "baseline":
        if path is not None:
            raise ValueError("--baseline-reference is only valid for the optimized phase")
        return {"required": False, "reason": "first baseline phase"}
    if path is None:
        raise ValueError("optimized phase requires --baseline-reference to a completed 9 GiB baseline result")
    reference_path = path.resolve(strict=True)
    baseline = json.loads(reference_path.read_text(encoding="utf-8"))
    if (baseline.get("schema") != "browser_donor_trial_9g_preflight_v1" or
            baseline.get("variant") != PROFILE["variant"] or baseline.get("phase") != "baseline" or
            baseline.get("device_execution") != "completed"):
        raise ValueError("baseline reference must be a completed result from this 9 GiB baseline variant")
    if baseline.get("profile") != PROFILE:
        raise ValueError("baseline and optimized static workload/profile parameters differ")
    native = baseline.get("native_execution", {})
    if (native.get("device_execution") != "completed" or
            native.get("coverage_through_t_plus_930") is not True or
            native.get("gate_audit", {}).get("valid") is not True or
            baseline.get("service_restore", {}).get("verified") is not True or
            not _target_cleanup_verified(baseline)):
        raise ValueError("baseline reference lacks a valid full gate/OOM horizon or verified restore/cleanup")
    baseline_donor = baseline.get("donor", {})
    baseline_plan = baseline.get("plan", {})
    baseline_anon = baseline_donor.get("anon_samples", {})
    required = (baseline_donor.get("bundle") == donor.get("bundle") == EXPECTED_BROWSER and
                baseline_donor.get("uid") == donor.get("uid") and
                baseline_donor.get("windows") == donor.get("windows") == 6 and
                baseline_donor.get("pages") == donor.get("pages") == 18 and
                baseline_donor.get("workload_fingerprint_sha256") == donor.get("workload_fingerprint_sha256") and
                baseline_anon.get("sample_count") == 3 and baseline_anon.get("within_limit") is True and
                baseline_plan.get("needed_bytes") == plan.get("needed_bytes") and
                baseline.get("pressure_targets") == list(TARGETS) and
                baseline.get("service", {}).get("daily_disk_mode") == "owned_apply" and
                baseline.get("service", {}).get("effective_browser_whole_work_cap_mib") ==
                    PROFILE["load"]["disk_whole_work_cap_mib"])
    if not required:
        raise ValueError("baseline reference does not prove the same donor/9 GiB workload parameters")
    if baseline_plan.get("case_id") == plan.get("case_id"):
        raise ValueError("optimized phase must use a fresh case ID and fixed-T event plan")
    baseline_mean = float(baseline_anon.get("mean_bytes", 0))
    optimized_mean = float(donor["anon_samples"]["mean_bytes"])
    if baseline_mean <= 0 or optimized_mean <= 0:
        raise ValueError("baseline and optimized donor anon means must be positive")
    delta = abs(optimized_mean - baseline_mean)
    ratio = delta / baseline_mean
    if ratio > 0.02:
        raise ValueError(f"optimized donor anon mean differs from baseline by {ratio:.6%}, above 2%")
    return {"required": True, "path": str(reference_path), "sha256": sha(reference_path),
        "baseline_case_id": baseline_plan["case_id"],
        "workload_fingerprint_sha256": donor["workload_fingerprint_sha256"],
        "baseline_anon_samples": baseline_anon,
        "optimized_anon_samples": donor["anon_samples"],
        "mean_delta_bytes": delta, "mean_delta_fraction_of_baseline": ratio,
        "max_pair_delta_fraction": 0.02, "matched": True}


def validate_browser_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    readiness = data.get("readiness", {})
    latest = (data.get("samples") or [{}])[-1]
    members = latest.get("processes", [])
    identities = _verified_browser_member_identities(data)
    anon_samples = _anon_sample_stats(data)
    workload_fingerprint = _workload_fingerprint(data)
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
        "anon_stable": readiness.get("anon_capacity_within_two_percent_across_samples") is True,
        "input_count": len(data.get("device_inputs", [])) == 18,
        "inputs_verified": all(row.get("verified") is True for row in data.get("device_inputs", [])),
        "operator_pages_loaded": data.get("operator_assertions", {}).get("pages_loaded_and_settled") is True,
        "operator_hidden": data.get("operator_assertions", {}).get("all_windows_minimized") is True,
        "members": 1 <= len(members) <= 64 and latest.get("member_count") == len(members),
        "anon_samples": anon_samples["within_limit"],
    }
    if not all(required.values()):
        failed = [key for key, value in required.items() if not value]
        raise ValueError("browser preparation evidence is stale/incomplete: " + ", ".join(failed))
    return {"path": str(path.resolve()), "sha256": sha(path), "bundle": data["bundle"],
            "uid": data["uid"], "member_count": len(members),
            "member_count_observed": len(members), "member_count_is_profile_requirement": False,
            "workload_fingerprint_sha256": workload_fingerprint,
            "anon_bytes": latest.get("anon_bytes"), "anon_samples": anon_samples,
            "last_sample_boottime": latest.get("device_boottime_seconds"),
            "windows": 6, "pages": 18, "latest_sample": latest}


def validate_plan(path: Path, manifest: dict) -> dict:
    raw = path.read_text(encoding="ascii")
    lines = raw.splitlines()
    if len(lines) != 3 or lines[0] != "device_browser_donor_plan_v1":
        raise ValueError("browser plan must use device_browser_donor_plan_v1 and one donor row")
    meta, donor = lines[1].split("|"), lines[2].split("|")
    if len(meta) != 5 or len(donor) != 6 or donor[0] != EXPECTED_BROWSER:
        raise ValueError("browser plan field count/scope invalid")
    expected_increment = int(PROFILE["load"]["future_increment_mib"])
    if int(meta[3]) != expected_increment * 1024**2 or meta[4] != "1":
        raise ValueError(f"browser event must register the exact {expected_increment}-MiB peak increment")
    if int(donor[1]) != int(manifest["uid"]) or int(donor[2]) <= 0 or int(donor[3]) <= 0:
        raise ValueError("browser plan UID/PID/start does not match reviewed donor evidence")
    members = donor[5].split(",")
    if not 1 <= len(members) <= 64:
        raise ValueError("browser plan must bind every observed member (1–64; service limit)")
    try:
        plan_identities = [tuple(int(value) for value in item.split(":")) for item in members]
    except ValueError as exc:
        raise ValueError("browser plan member identity is malformed") from exc
    if (any(len(item) != 3 or min(item) <= 0 for item in plan_identities) or
            len(set(plan_identities)) != len(plan_identities) or
            len({item[0] for item in plan_identities}) != len(plan_identities)):
        raise ValueError("browser plan member identity is invalid")
    latest = manifest["latest_sample"]
    manifest_members = latest.get("processes", [])
    expected_members = {(int(item["pid"]), int(item["uid"]), int(item["start_ticks"]))
                        for item in manifest_members if item.get("verified") is True}
    if set(plan_identities) != expected_members:
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
            "sample_boottime": sample_at, "member_count": len(members),
            "member_identities": [list(item) for item in sorted(plan_identities)],
            "needed_bytes": int(meta[3])}


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
    parser.add_argument("--baseline-reference", type=Path,
                        help="required for optimized: completed result.json from the matched 9 GiB baseline")
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
    baseline_match = validate_baseline_reference(args.baseline_reference, args.phase, donor, plan)
    service = validate_service_ini(args.service_ini, args.phase)
    if not args.hdc.is_file() or not args.candidate_daemon or not args.candidate_daemon.is_file():
        raise ValueError(f"HDC or candidate daemon missing: {args.hdc}")
    if service["browser_uid"] != int(donor["uid"]):
        raise ValueError("service browser profile UID differs from donor manifest UID")
    proof = {"schema": "browser_donor_trial_9g_preflight_v1", "variant": PROFILE["variant"],
             "phase": args.phase, "profile": PROFILE, "donor": donor, "plan": plan,
             "service": service, "pressure_targets": list(TARGETS),
             "baseline_reference_match": baseline_match,
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
        native_plan_module = local_native_plan_module()
        serialize_native_plan = native_plan_module.serialize
        native_plan_path = args.output.parent / "native_retry_plan.tsv"
        identities = {bundle: {key: target_ready["targets"][bundle][key]
                               for key in ("uid", "pid", "start_ticks")} for bundle in TARGETS}
        native_plan_raw = serialize_native_plan(plan["case_id"], boot_id, plan["T"], identities)
        native_plan_module.validate_serialized(native_plan_raw)
        native_plan_path.write_text(native_plan_raw, encoding="ascii", newline="\n")
        proof["target_daily_ready"] = target_ready
        proof["native_plan"] = {"path": str(native_plan_path.resolve()), "sha256": sha(native_plan_path),
            "schema": "device_retry_plan_browser_donor_3target_9g_v1", "T": plan["T"],
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
