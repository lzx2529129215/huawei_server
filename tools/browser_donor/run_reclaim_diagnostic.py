#!/usr/bin/env python3
"""Run one bounded browser-group anonymous-shrink service diagnostic.

This is a service-chain diagnostic, not an A/B trial or a benefit claim. It
backs up and fingerprints the one resident service, runs a separate browser
profile for one registered event with a one-submission lifetime limit, then
stops that process and restarts the original command/configuration. The tool
does not change SELinux, swap policy, or pressure-app state.

Device operations require both --execute and --confirm-service-replacement.
Review the generated config and the restore procedure before using either.
"""
from __future__ import annotations

import argparse
import csv
import configparser
import datetime as dt
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(WORKSPACE / "tools" / "browser_trial"))
from prepare import Hdc, sample  # noqa: E402
from make_plan import create_plan  # noqa: E402

BUNDLE = "com.huawei.hmos.browser"
MIB = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize_sample(row: dict) -> dict:
    mem = row.get("meminfo_bytes_or_counts", {})
    return {
        "device_boottime_seconds": row.get("device_boottime_seconds"),
        "mem_total_bytes": mem.get("MemTotal"),
        "mem_free_bytes": row.get("system_memfree_bytes"),
        "mem_available_bytes": row.get("system_memavailable_bytes"),
        "browser_anon_bytes": row.get("anon_bytes"),
        "browser_eswap_bytes": row.get("eswap_bytes"),
        "browser_app_score": row.get("app_score"),
        "member_count": row.get("member_count"),
        "member_pid_start_uid": sorted(
            (p.get("pid"), p.get("start_ticks"), p.get("uid"))
            for p in row.get("processes", [])),
        "oom_score_adj": sorted((p.get("pid"), p.get("oom_score_adj"))
                                 for p in row.get("processes", [])),
        "browser_window_count": row.get("browser_window_count"),
        "all_browser_windows_hidden": row.get("all_browser_windows_hidden"),
        "vmstat_counts": row.get("vmstat_counts"),
        "zram_mm_stat_raw": row.get("zram_mm_stat_raw"),
    }


def validate_manifest_and_live(manifest_path: Path, live: dict) -> tuple[dict, dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    latest = (manifest.get("samples") or [{}])[-1]
    readiness = manifest.get("readiness", {})
    require = {
        "schema": manifest.get("schema") == "browser-donor-preparation-v1",
        "bundle": manifest.get("bundle") == BUNDLE,
        "operator_pages_loaded": manifest.get("operator_assertions", {}).get("pages_loaded_and_settled") is True,
        "operator_all_minimized": manifest.get("operator_assertions", {}).get("all_windows_minimized") is True,
        "reviewed_eligible": manifest.get("eligible_for_review") is True,
        "registered_group_stable": readiness.get("member_pid_start_sets_equal_across_samples") is True and
                                   readiness.get("member_pid_uid_sets_equal_across_samples") is True,
        "six_windows_hidden": readiness.get("all_browser_windows_hidden_each_sample") is True and
                              manifest.get("expected_windows") == 6,
        "positive_app_score": readiness.get("all_group_app_scores_positive_each_sample") is True,
        "positive_process_scores": readiness.get("all_group_oom_score_adj_positive_each_sample") is True,
        "fresh_identity": live.get("identity_verified") is True and
                          latest.get("member_pids") == live.get("member_pids") and
                          latest.get("member_pids_after") == live.get("member_pids_after"),
        "fresh_hidden": live.get("all_browser_windows_hidden") is True and
                        live.get("browser_window_count") == 6,
        "fresh_scores": isinstance(live.get("app_score"), int) and live["app_score"] > 0 and
                        bool(live.get("processes")) and
                        all(p.get("verified") is True and isinstance(p.get("oom_score_adj"), int)
                            and p["oom_score_adj"] > 0 for p in live["processes"]),
        "fresh_member_identity": sorted((p.get("pid"), p.get("start_ticks"), p.get("uid"))
                                         for p in live.get("processes", [])) ==
                                 sorted((p.get("pid"), p.get("start_ticks"), p.get("uid"))
                                        for p in latest.get("processes", [])),
    }
    failed = [name for name, ok in require.items() if not ok]
    if failed:
        raise ValueError("browser donor precondition failed: " + ", ".join(failed))
    uid = manifest.get("uid")
    if not isinstance(uid, int) or uid <= 0:
        raise ValueError("browser package UID missing from reviewed manifest")
    return manifest, {"uid": uid, "required_checks": require,
                      "manifest_sha256": sha256_file(manifest_path),
                      "latest_reviewed_sample": summarize_sample(latest),
                      "fresh_live_sample": summarize_sample(live)}


def calculate_needed_mib(mem_free: int, mem_total: int, reserve_mib: int,
                         margin_mib: int) -> int:
    if min(mem_free, mem_total, reserve_mib, margin_mib) < 0 or not mem_total:
        raise ValueError("invalid memory inputs")
    reserve = reserve_mib * MIB
    margin = margin_mib * MIB
    needed = math.ceil((mem_free + margin) / MIB)
    if needed <= 0 or needed * MIB + reserve > mem_total:
        raise ValueError("diagnostic event demand plus reserve exceeds MemTotal")
    return needed


def render_config(remote_root: str, browser_uid: int, settings: dict) -> str:
    if not remote_root.startswith("/data/local/tmp/") or ".." in remote_root.split("/"):
        raise ValueError("diagnostic root must be an absolute private /data/local/tmp path")
    if browser_uid <= 0:
        raise ValueError("browser package UID must be positive")
    if settings["disk_mode"] != "off" or settings["max_submissions"] != 1:
        raise ValueError("diagnostic must keep disk mode off and limit submissions to one")
    rows = [
        "[daemon]",
        "oracle_mode=false",
        "daily_async=true",
        "daily_known_lead_sec=95",
        "daily_known_capacity=true",
        "daily_known_strength=true",
        "daily_known_future_free_only=true",
        f"daily_known_plan_file={remote_root}/browser_event.tsv",
        f"daily_known_evidence_ledger={remote_root}/known_evidence.jsonl",
        "daily_owned_os_snapshot=true",
        "daily_browser_donor_profile=true",
        "daily_max_submissions=1",
        "daily_disk_mode=off",
        "daily_disk_whole_work_mb=0",
        "harmony_smaps_interval_ms=30000",
        "cgroup_root=/dev/memcg",
        "proc_root=/proc",
        f"log_dir={remote_root}/logs",
        f"lock_file={remote_root}/daemon.lock",
        f"control_file={remote_root}/control.ini",
        "interval_ms=1000",
        f"reserve_mb={settings['reserve_mib']}",
        "ready_free_mb=256",
        "hysteresis_mb=64",
        "horizon_sec=5",
        "startup_grace_sec=0",
        "cooldown_sec=10",
        "backoff_sec=30",
        "min_batch_mb=16",
        f"batch_mb={settings['batch_mib']}",
        f"max_per_minute_mb={settings['global_quota_mib']}",
        f"app_per_minute_mb={settings['app_quota_mib']}",
        "swap_reserve_mb=256",
        "require_psi=false",
        "log_max_mb=8",
        "",
        "[app browser]",
        f"path=100/{BUNDLE}_{browser_uid}",
        "reclaim=true",
        "min_keep_mb=512",
        "",
    ]
    return "\n".join(rows)


def render_control() -> str:
    return "paused=false\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0\n"


def parse_wms_focus(raw: str, member_pids: set[int]) -> dict:
    header = False
    rows: list[tuple[str, int, int]] = []
    focus_id = None
    for line in raw.splitlines():
        if not header:
            if re.search(r"WindowName\s+DisplayId\s+Pid\s+WinId", line):
                header = True
            continue
        focus = re.match(r"^\s*Focus window:\s*(\d+)\s*$", line, re.I)
        if focus:
            focus_id = int(focus.group(1))
            break
        match = re.match(r"^\s*(\S+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s|$)", line)
        if match:
            rows.append((match.group(1), int(match.group(3)), int(match.group(4))))
    ids = [row[2] for row in rows]
    if not header or focus_id is None or len(ids) != len(set(ids)) or ids.count(focus_id) != 1:
        raise ValueError("fresh WMS focus/window inventory is malformed")
    focus_row = next(row for row in rows if row[2] == focus_id)
    if focus_row[1] in member_pids:
        raise ValueError("a browser group window is still the focused WMS window")
    browser = sorted((pid, win_id) for name, pid, win_id in rows
                     if pid in member_pids and re.fullmatch(r"browser\d+", name))
    if len(browser) != 6:
        raise ValueError(f"fresh WMS reports {len(browser)} browser windows; expected six")
    return {"focus_win_id": focus_id, "focus_pid": focus_row[1],
            "browser_windows": browser, "raw_sha256": sha256_bytes(raw.encode("utf-8"))}


def sample_with_focus(hdc: Hdc, bundle: str, uid: int) -> tuple[dict, dict]:
    leaf = f"/dev/memcg/100/{bundle}_{uid}"
    before_raw = hdc.shell("cat " + shlex.quote(leaf + "/cgroup.procs"), timeout=15)
    before_members = {int(x) for x in before_raw.split() if x.isdecimal()}
    if not before_members:
        raise ValueError("browser group has no current cgroup members")
    before_focus = parse_wms_focus(hdc.shell(
        "hidumper -s WindowManagerService -a '-a'", timeout=20), before_members)
    row = sample(hdc, bundle, uid)
    after_raw = hdc.shell("cat " + shlex.quote(leaf + "/cgroup.procs"), timeout=15)
    after_members = {int(x) for x in after_raw.split() if x.isdecimal()}
    after_focus = parse_wms_focus(hdc.shell(
        "hidumper -s WindowManagerService -a '-a'", timeout=20), after_members)
    sample_ids = sorted((w["pid"], w["win_id"]) for w in row.get("browser_windows", [])
                        if w.get("is_browser_window"))
    if (before_members != after_members or before_members != set(row.get("member_pids", [])) or
            before_focus["focus_win_id"] != after_focus["focus_win_id"] or
            before_focus["focus_pid"] != after_focus["focus_pid"] or
            before_focus["browser_windows"] != after_focus["browser_windows"] or
            before_focus["browser_windows"] != sample_ids):
        raise ValueError("WMS focus or browser-group window inventory changed during sampling")
    return row, {"before": before_focus, "after": after_focus,
                 "stable_focus_outside_browser_group": True}


def meminfo_for_sample(row: dict) -> tuple[int, int]:
    info = row.get("meminfo_bytes_or_counts", {})
    free, total = info.get("MemFree"), info.get("MemTotal")
    if not isinstance(free, int) or not isinstance(total, int) or free <= 0 or total <= free:
        raise ValueError("fresh MemFree/MemTotal is unavailable")
    return free, total


class DeviceOps:
    def __init__(self, hdc: Path, target: str):
        self.hdc = hdc.resolve(strict=True)
        self.target = target

    def shell(self, command: str, timeout: int = 30) -> str:
        result = subprocess.run([str(self.hdc), "-t", self.target, "shell", command],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
        output = result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        if result.returncode:
            raise RuntimeError(f"device command rc={result.returncode}: {output[-1000:]}")
        return output.strip()

    def file_send(self, source: Path, remote: str) -> None:
        result = subprocess.run([str(self.hdc), "-t", self.target, "file", "send",
                                 str(source.resolve(strict=True)), remote],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=60, check=False)
        if result.returncode:
            raise RuntimeError("HDC file send failed: " + result.stdout.decode("utf-8", "replace")[-1000:])

    def file_recv(self, remote: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([str(self.hdc), "-t", self.target, "file", "recv",
                                 remote, str(destination.resolve())],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=90, check=False)
        if result.returncode or not destination.is_file():
            raise RuntimeError("HDC file recv failed: " + result.stdout.decode("utf-8", "replace")[-1000:])

    def send_verified(self, source: Path, remote: str) -> str:
        self.file_send(source, remote)
        digest = self.shell("sha256sum " + shlex.quote(remote)).split()[0].lower()
        expected = sha256_file(source)
        if digest != expected:
            raise RuntimeError(f"remote hash mismatch for {remote}: {digest} != {expected}")
        return digest

    def pids(self) -> list[int]:
        raw = self.shell("pidof memshiftd 2>/dev/null || true")
        return [int(x) for x in raw.split() if x.isdecimal()]

    def argv(self, pid: int) -> list[str]:
        raw = self.shell(f"tr '\\000' '\\n' < /proc/{pid}/cmdline")
        return [line for line in raw.splitlines() if line]

    def executable(self, pid: int) -> str:
        return self.shell(f"readlink /proc/{pid}/exe").strip().removesuffix(" (deleted)")

    def proc_start_ticks(self, pid: int) -> int:
        raw = self.shell(f"cat /proc/{pid}/stat")
        close = raw.rfind(")")
        if close < 0:
            raise RuntimeError("resident daemon /proc stat malformed")
        fields = raw[close + 1:].split()
        if len(fields) <= 19:
            raise RuntimeError("resident daemon /proc stat truncated")
        return int(fields[19])

    def wait_no_daemon(self, timeout_s: float) -> None:
        until = time.monotonic() + timeout_s
        while time.monotonic() < until:
            if not self.pids():
                return
            time.sleep(0.25)
        raise RuntimeError("memshiftd did not exit after SIGTERM")

    def stop_pid(self, pid: int, expected_start_ticks: int,
                 expected_executable: str, timeout_s: float = 15) -> None:
        current = self.pids()
        if pid not in current:
            return
        if current != [pid]:
            raise RuntimeError(f"refusing SIGTERM: expected only PID {pid}, observed {current}")
        observed_start = self.proc_start_ticks(pid)
        observed_executable = self.executable(pid)
        if observed_start != expected_start_ticks or observed_executable != expected_executable:
            raise RuntimeError("refusing SIGTERM: registered PID start-time or executable changed")
        self.shell(f"kill -TERM {pid}")
        self.wait_no_daemon(timeout_s)

    def start(self, binary: str, argv: list[str], stdout_path: str) -> int:
        if not binary.startswith("/") or not stdout_path.startswith("/"):
            raise ValueError("device binary and stdout paths must be absolute")
        args = " ".join(shlex.quote(item) for item in argv)
        command = ("nohup " + shlex.quote(binary) + " " + args + " </dev/null >" +
                   shlex.quote(stdout_path) + " 2>&1 & echo $!")
        output = self.shell(command, timeout=15)
        matches = re.findall(r"(?m)^\s*(\d+)\s*$", output)
        if not matches:
            raise RuntimeError("could not read PID from nohup launch output: " + output[-500:])
        return int(matches[-1])

    def check_service_read_only(self, binary: str, config: str,
                                browser_path: str) -> dict:
        words = [binary, "--config", config, "--backend", "harmony-v1-ratio", "--apply", "--check"]
        command = " ".join(shlex.quote(x) for x in words)
        output = self.shell(command + "; __ms_rc=$?; echo __MEMSHIFT_CHECK_RC=$__ms_rc", timeout=45)
        match = re.search(r"(?m)^__MEMSHIFT_CHECK_RC=(\d+)\s*$", output)
        if not match:
            raise RuntimeError("remote --check output lacks an explicit process exit-code marker")
        remote_rc = int(match.group(1))
        body = output[:match.start()].strip()
        required = {
            "check_mode": bool(re.search(r"(?m)^backend=harmony-v1 mode=check\b", body)),
            "browser_entry": bool(re.search(r"(?m)^app=browser\s+path=" + re.escape(browser_path) + r"\b", body)),
            "writes_disabled": "No reclaim was attempted by --check." in body,
        }
        if remote_rc != 0 or not all(required.values()):
            raise RuntimeError(f"remote --check failed or output was incomplete: rc={remote_rc}, checks={required}, output={body[-1200:]}")
        return {"output": body, "remote_exit_code": remote_rc, "required_output_checks": required,
                "reclaim_write_performed": False}


def parse_config_text(raw: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(raw)
    if not parser.has_section("daemon"):
        raise ValueError("resident service config lacks [daemon]")
    return parser


def boot_seconds(device: DeviceOps) -> float:
    raw = device.shell("cat /proc/uptime").split()
    if not raw:
        raise RuntimeError("device boottime is unavailable")
    return float(raw[0])


def assert_one_service(device: DeviceOps, expected_pid: int, binary: str,
                       config: str) -> dict:
    pids = device.pids()
    if pids != [expected_pid]:
        raise RuntimeError(f"expected one daemon PID {expected_pid}; observed {pids}")
    argv = device.argv(expected_pid)
    executable = device.executable(expected_pid)
    if len(argv) < 2 or executable != binary:
        raise RuntimeError("resident daemon /proc/PID/exe does not match expected binary path")
    config_index = argv.index("--config") if "--config" in argv else -1
    if config_index < 0 or config_index + 1 >= len(argv) or argv[config_index + 1] != config:
        raise RuntimeError("resident daemon config argv does not match expected path")
    return {"pid": expected_pid, "start_ticks": device.proc_start_ticks(expected_pid),
            "executable": executable, "argv": argv}


def wait_until(device: DeviceOps, when: float) -> None:
    while True:
        remaining = when - boot_seconds(device)
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def copy_remote_backup(device: DeviceOps, remote: str, local: Path,
                       expected_sha256: str) -> None:
    device.file_recv(remote, local)
    actual = sha256_file(local)
    if actual != expected_sha256:
        raise RuntimeError(f"backup hash mismatch for {remote}: {actual} != {expected_sha256}")


def write_json(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict:
    if not args.execute or not args.confirm_service_replacement:
        raise ValueError("device execution requires --execute --confirm-service-replacement")
    if not args.binary.is_file() or not args.browser_manifest.is_file() or not args.hdc.is_file():
        raise ValueError("binary, browser manifest, and HDC executable must exist")
    for value, label in ((args.event_ahead_seconds, "event ahead"),
                         (args.post_event_seconds, "post-event duration"),
                         (args.margin_mib, "event margin")):
        if value <= 0:
            raise ValueError(f"{label} must be positive")
    if args.event_ahead_seconds > 250 or args.post_event_seconds > 90:
        raise ValueError("diagnostic horizon exceeds its bounded 250s/90s limit")
    if args.reserve_mib != 1024 or args.batch_mib != 512 or args.global_quota_mib != 1024 or args.app_quota_mib != 1024:
        raise ValueError("this diagnostic variant fixes reserve=1024 MiB, batch=512 MiB and both quotas=1024 MiB")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    state = {"schema": "browser_donor_reclaim_diagnostic_v1", "classification": "single bounded service-chain diagnostic; not A/B and not benefit evidence",
             "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
             "target": args.target, "requested_profile": {"bundle": BUNDLE,
             "windows": 6, "pages": 18, "disk_mode": "off", "max_submissions": 1,
             "reserve_mib": args.reserve_mib, "batch_mib": args.batch_mib,
             "global_quota_mib": args.global_quota_mib, "app_quota_mib": args.app_quota_mib,
             "min_keep_mib": 512, "allow_zram": True, "selinux": "must remain Enforcing"},
             "phase": "preflight", "device_mutation_started": False}
    report_path = output / "diagnostic.json"
    write_json(report_path, state)
    device = DeviceOps(args.hdc, args.target)
    helper = Hdc(str(args.hdc.resolve()), args.target)
    restored = False
    original_stopped = False
    original_pid = None
    diagnostic_pid = None
    original_argv: list[str] = []
    original_binary = ""
    original_config = ""
    original_control = ""
    original_config_sha = ""
    original_control_sha = ""
    diagnostic_root = ""
    try:
        enforcing = device.shell("getenforce").strip()
        if enforcing != "Enforcing":
            raise RuntimeError(f"SELinux must remain Enforcing; observed {enforcing!r}")
        pids = device.pids()
        if len(pids) != 1:
            raise RuntimeError(f"expected exactly one current memshiftd before switch, observed {pids}")
        original_pid = pids[0]
        original_argv = device.argv(original_pid)
        if not original_argv:
            raise RuntimeError("current daemon command line is unavailable")
        original_binary = device.executable(original_pid)
        if not original_binary.startswith("/"):
            raise RuntimeError("current daemon /proc/PID/exe must be an absolute remote binary path")
        config_index = original_argv.index("--config") if "--config" in original_argv else -1
        if config_index < 0 or config_index + 1 >= len(original_argv):
            raise RuntimeError("current daemon command line has no --config")
        original_config = original_argv[config_index + 1]
        if not original_config.startswith("/"):
            raise RuntimeError("current daemon config path must be absolute")
        raw_config = device.shell("cat " + shlex.quote(original_config))
        parsed_config = parse_config_text(raw_config)
        original_control = parsed_config["daemon"].get("control_file", "")
        if not original_control.startswith("/"):
            raise RuntimeError("current daemon control_file must be absolute")
        raw_control = device.shell("cat " + shlex.quote(original_control))
        original_config_sha = device.shell("sha256sum " + shlex.quote(original_config)).split()[0].lower()
        original_control_sha = device.shell("sha256sum " + shlex.quote(original_control)).split()[0].lower()
        original_binary_sha = device.shell("sha256sum " + shlex.quote(original_binary)).split()[0].lower()
        original_daemon = assert_one_service(device, original_pid, original_binary, original_config)
        original_swaps = device.shell("cat /proc/swaps")
        original_proc_swaps = original_swaps

        manifest_data = json.loads(args.browser_manifest.read_text(encoding="utf-8-sig"))
        initial_live, initial_focus = sample_with_focus(
            helper, BUNDLE, int(manifest_data["uid"]))
        manifest, evidence = validate_manifest_and_live(args.browser_manifest,
            initial_live)
        # Re-read once more immediately before service replacement below.
        live, live_focus = sample_with_focus(helper, BUNDLE, evidence["uid"])
        validate_manifest_and_live(args.browser_manifest, live)
        evidence["fresh_live_sample"] = summarize_sample(live)
        evidence["fresh_wms_focus"] = live_focus
        free, total = meminfo_for_sample(live)
        needed_mib = calculate_needed_mib(free, total, args.reserve_mib, args.margin_mib)
        t = float(live["device_boottime_seconds"]) + args.event_ahead_seconds
        case_id = "s4_" + uuid.uuid4().hex[:12] + "_c0001"
        diagnostic_root = "/data/local/tmp/memshift_browser_diag_" + case_id[3:15]
        state.update({"selinux_before": enforcing, "original_service": {
            **original_daemon, "binary": original_binary, "binary_sha256": original_binary_sha,
            "config": original_config, "config_sha256": original_config_sha,
            "control": original_control, "control_sha256": original_control_sha,
            "control_contents": raw_control, "swaps_before": original_proc_swaps},
            "browser_preflight": evidence, "event": {"case_id": case_id,
            "T_boottime_seconds": t, "lead_seconds": 95, "needed_mib": needed_mib,
            "needed_bytes": needed_mib * MIB, "reserve_mib": args.reserve_mib,
            "margin_mib_requested": args.margin_mib, "MemFree_bytes": free,
            "MemTotal_bytes": total, "target_free_bytes": needed_mib * MIB + args.reserve_mib * MIB},
            "diagnostic_root": diagnostic_root, "binary_local": str(args.binary.resolve()),
            "binary_sha256": sha256_file(args.binary)})
        write_json(report_path, state)

        # Keep a derived copy of the reviewed evidence. The final plan is
        # emitted from the last sample immediately before switching daemons.
        manifest_copy = json.loads(json.dumps(manifest))
        manifest_copy["samples"].append(live)
        fresh_manifest_path = output / "diagnostic_browser_manifest.json"
        fresh_manifest_path.write_text(json.dumps(manifest_copy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        plan_path = output / "browser_event.tsv"
        config_text = render_config(diagnostic_root, evidence["uid"], {
            "disk_mode": "off", "max_submissions": 1, "reserve_mib": args.reserve_mib,
            "batch_mib": args.batch_mib, "global_quota_mib": args.global_quota_mib,
            "app_quota_mib": args.app_quota_mib})
        config_path = output / "browser_diagnostic.ini"
        config_path.write_text(config_text, encoding="ascii", newline="\n")
        control_path = output / "control.ini"
        control_path.write_text(render_control(), encoding="ascii", newline="\n")
        state["generated_config"] = {"path": str(config_path.resolve()), "sha256": sha256_file(config_path),
                                     "control_path": str(control_path.resolve()),
                                     "control_sha256": sha256_file(control_path), "contents": config_text}

        # Pull rollback artifacts and verify hashes before touching the process.
        backup = output / "original_service_backup"
        copy_remote_backup(device, original_binary, backup / "memshiftd", original_binary_sha)
        copy_remote_backup(device, original_config, backup / "service.ini", original_config_sha)
        copy_remote_backup(device, original_control, backup / "control.ini", original_control_sha)
        state["backup_paths"] = {"binary": str((backup / "memshiftd").resolve()),
                                 "config": str((backup / "service.ini").resolve()),
                                 "control": str((backup / "control.ini").resolve()),
                                 "verified": True}

        device.shell("test ! -e " + shlex.quote(diagnostic_root) + " && mkdir -p " + shlex.quote(diagnostic_root))
        remote_binary = diagnostic_root + "/memshiftd"
        remote_config = diagnostic_root + "/browser_diagnostic.ini"
        remote_plan = diagnostic_root + "/browser_event.tsv"
        remote_control = diagnostic_root + "/control.ini"
        transfer_hashes = {
            "binary": device.send_verified(args.binary, remote_binary),
            "config": device.send_verified(config_path, remote_config),
            "control": device.send_verified(control_path, remote_control),
        }
        device.shell("chmod 0755 " + shlex.quote(remote_binary))
        state["remote_staging"] = {"hashes": transfer_hashes, "binary": remote_binary,
                                   "config": remote_config, "plan": remote_plan,
                                   "control": remote_control, "directory_created": True}
        before_sample, before_focus = sample_with_focus(helper, BUNDLE, evidence["uid"])
        manifest, evidence = validate_manifest_and_live(args.browser_manifest, before_sample)
        free, total = meminfo_for_sample(before_sample)
        needed_mib = calculate_needed_mib(free, total, args.reserve_mib, args.margin_mib)
        t = float(before_sample["device_boottime_seconds"]) + args.event_ahead_seconds
        manifest_copy = json.loads(json.dumps(manifest))
        manifest_copy["samples"].append(before_sample)
        fresh_manifest_path.write_text(json.dumps(manifest_copy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        event_info = create_plan(fresh_manifest_path, case_id, t, needed_mib, plan_path)
        device.shell("rm -f " + shlex.quote(remote_plan))
        final_plan_hash = device.send_verified(plan_path, remote_plan)
        if final_plan_hash != event_info["sha256"]:
            raise RuntimeError("event plan hash differs from locally generated immutable plan")
        transfer_hashes["plan"] = final_plan_hash
        state["event"] = {"case_id": case_id, "T_boottime_seconds": t, "lead_seconds": 95,
            "needed_mib": needed_mib, "needed_bytes": needed_mib * MIB,
            "reserve_mib": args.reserve_mib, "margin_mib_requested": args.margin_mib,
            "MemFree_bytes": free, "MemTotal_bytes": total,
            "target_free_bytes": needed_mib * MIB + args.reserve_mib * MIB,
            "sampled_immediately_before_switch": True}
        state["event_plan"] = {**event_info, "path": str(plan_path.resolve())}
        state["remote_staging"]["hashes"] = transfer_hashes
        state["before_replacement_sample"] = summarize_sample(before_sample)
        state["before_replacement_wms_focus"] = before_focus
        state["read_only_service_check"] = device.check_service_read_only(
            remote_binary, remote_config, f"100/{BUNDLE}_{evidence['uid']}")
        state["read_only_service_check"]["performed_before_stopping_original"] = True
        if boot_seconds(device) >= t - 10:
            raise RuntimeError("insufficient lead remains before the registered event")
        if device.shell("getenforce").strip() != "Enforcing":
            raise RuntimeError("SELinux state changed before service replacement")
        state["phase"] = "backups_and_plan_verified; stopping original daemon"
        state["device_mutation_started"] = True
        write_json(report_path, state)

        original_stopped = True
        device.stop_pid(original_pid, original_daemon["start_ticks"], original_binary)
        if device.pids():
            raise RuntimeError("old memshiftd did not fully exit; refusing to start diagnostic instance")
        start_output = diagnostic_root + "/daemon_stdout.log"
        diagnostic_pid = device.start(remote_binary,
            ["--config", remote_config, "--backend", "harmony-v1-ratio", "--apply"], start_output)
        state["diagnostic_service"] = assert_one_service(device, diagnostic_pid, remote_binary, remote_config)
        if device.shell("getenforce").strip() != "Enforcing":
            raise RuntimeError("SELinux state changed during diagnostic service startup")
        state["phase"] = "diagnostic service running; waiting for one event and feedback"
        state["diagnostic_started_boottime"] = boot_seconds(device)
        write_json(report_path, state)

        wait_until(device, t + args.post_event_seconds)
        if device.pids() != [diagnostic_pid]:
            raise RuntimeError("diagnostic service lost unique-process invariant")
        state["phase"] = "diagnostic event window elapsed; collecting after sample/logs"
        state["diagnostic_finished_boottime"] = boot_seconds(device)
        write_json(report_path, state)
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        state["phase"] = "error; attempting bounded cleanup and original-service restoration"
    finally:
        # Stop only the PID this tool launched after proving it is still the sole daemon.
        try:
            active = device.pids()
            staged = state.get("remote_staging", {})
            if diagnostic_pid is None and len(active) == 1 and staged.get("binary") and staged.get("config"):
                maybe_argv = device.argv(active[0])
                if maybe_argv and device.executable(active[0]) == staged["binary"] and \
                        "--config" in maybe_argv and maybe_argv.index("--config") + 1 < len(maybe_argv) and \
                        maybe_argv[maybe_argv.index("--config") + 1] == staged["config"]:
                    diagnostic_pid = active[0]
            if diagnostic_pid is not None and diagnostic_pid in active:
                diagnostic_identity = state.get("diagnostic_service")
                if diagnostic_identity is None:
                    diagnostic_identity = assert_one_service(device, diagnostic_pid,
                        staged["binary"], staged["config"])
                device.stop_pid(diagnostic_pid, diagnostic_identity["start_ticks"],
                                diagnostic_identity["executable"])
                state["diagnostic_stopped"] = True
            elif active and original_pid in active:
                current_original = assert_one_service(device, original_pid, original_binary, original_config)
                if current_original["start_ticks"] != original_daemon["start_ticks"]:
                    raise RuntimeError("original daemon PID was reused; not treating it as restored")
                state["diagnostic_stopped"] = True
            elif active:
                raise RuntimeError(f"unrecognized memshiftd process remains; not signaling it: {active}")
        except Exception as restore_exc:
            state["cleanup_error"] = f"{type(restore_exc).__name__}: {restore_exc}"

        # Read evidence only after stopping the diagnostic PID; collect the
        # service's bounded CSVs and the group/system state before restoration.
        try:
            if diagnostic_root:
                after_uid = evidence["uid"] if "evidence" in locals() else \
                    int(json.loads(args.browser_manifest.read_text(encoding="utf-8-sig"))["uid"])
                after_sample, after_focus = sample_with_focus(helper, BUNDLE, after_uid)
                state["after_sample"] = summarize_sample(after_sample)
                state["after_wms_focus"] = after_focus
                state["swaps_after_diagnostic"] = device.shell("cat /proc/swaps")
                log_root = diagnostic_root + "/logs/harmony-v1-ratio"
                for name in ("daily_actions.csv", "daily_known_future.csv", "events.csv", "system.csv", "apps.csv"):
                    remote = log_root + "/" + name
                    exists = device.shell("test -f " + shlex.quote(remote) + " && echo yes || echo no").strip() == "yes"
                    if exists:
                        device.file_recv(remote, output / "service_logs" / name)
                for suffix in (".device_proof_v1.csv", ".device_access_v1.csv", ".device_cycle_v1.csv"):
                    ledger = diagnostic_root + "/known_evidence.jsonl" + suffix
                    if device.shell("test -f " + shlex.quote(ledger) + " && echo yes || echo no").strip() == "yes":
                        device.file_recv(ledger, output / "service_logs" / ("known_evidence" + suffix))
                stdout = diagnostic_root + "/daemon_stdout.log"
                if device.shell("test -f " + shlex.quote(stdout) + " && echo yes || echo no").strip() == "yes":
                    device.file_recv(stdout, output / "service_logs" / "daemon_stdout.log")
                state["collected_service_logs"] = sorted(p.name for p in (output / "service_logs").glob("*"))
                actions_path = output / "service_logs" / "daily_actions.csv"
                if actions_path.is_file():
                    with actions_path.open("r", encoding="utf-8-sig", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    completions = [row for row in rows if row.get("phase") == "completion"]
                    action_ids = sorted({row.get("action_id", "") for row in completions if row.get("action_id")})
                    submitted_writes = [row for row in completions
                                        if row.get("write_attempted", "").lower() == "true"]
                    actual_writes = [row for row in submitted_writes
                                     if row.get("write_length", "").isdigit() and
                                     int(row["write_length"]) > 0 and row.get("errno") == "0"]
                    verified_reclaim_writes = [row for row in actual_writes
                                               if row.get("backend") == "force_shrink_anon" and
                                               row.get("feedback_verified", "").lower() == "true" and
                                               row.get("status") == "write_accepted_feedback_verified"]
                    state["action_summary"] = {
                        "completion_rows": len(completions), "unique_completed_action_ids": action_ids,
                        "completion_statuses": [row.get("status") for row in completions],
                        "write_attempted_completion_count": len(submitted_writes),
                        "actual_nonempty_successful_write_count": len(actual_writes),
                        "verified_anonymous_reclaim_write_count": len(verified_reclaim_writes),
                        "actual_write_rows": [{"action_id": row.get("action_id"), "status": row.get("status"),
                            "write_length": row.get("write_length"), "errno": row.get("errno"),
                            "feedback_verified": row.get("feedback_verified"), "backend": row.get("backend")}
                            for row in actual_writes],
                        "lifetime_submission_limit": 1,
                        "one_completion_observed": len(completions) == 1 and len(action_ids) == 1,
                        "one_verified_reclaim_write_observed": len(verified_reclaim_writes) == 1,
                    }
                    if len(action_ids) > 1:
                        raise RuntimeError("daily_max_submissions=1 invariant violated by action ledger")
                else:
                    state["action_summary"] = {"completion_rows": 0, "one_completion_observed": False,
                                                "write_attempted_completion_count": 0,
                                                "actual_nonempty_successful_write_count": 0,
                                                "verified_anonymous_reclaim_write_count": 0,
                                                "one_verified_reclaim_write_observed": False,
                                                "reason": "daily_actions.csv was not created or copied"}
        except Exception as collect_exc:
            state["evidence_collection_error"] = f"{type(collect_exc).__name__}: {collect_exc}"

        # Restore only after final browser/system sampling and log capture.
        try:
            active = device.pids()
            if original_stopped and not active:
                current_config_sha = device.shell("sha256sum " + shlex.quote(original_config)).split()[0].lower()
                current_control_sha = device.shell("sha256sum " + shlex.quote(original_control)).split()[0].lower()
                current_binary_sha = device.shell("sha256sum " + shlex.quote(original_binary)).split()[0].lower()
                if (current_config_sha, current_control_sha, current_binary_sha) != (
                        original_config_sha, original_control_sha, state["original_service"]["binary_sha256"]):
                    raise RuntimeError("original daemon/config/control changed during diagnostic; refusing restart")
                restored_pid = device.start(original_binary, original_argv[1:],
                    diagnostic_root + "/restored_original_stdout.log")
                restored_row = assert_one_service(device, restored_pid, original_binary, original_config)
                if device.shell("getenforce").strip() != "Enforcing":
                    raise RuntimeError("SELinux is not Enforcing after original service restoration")
                if device.shell("sha256sum " + shlex.quote(original_control)).split()[0].lower() != original_control_sha:
                    raise RuntimeError("original control file hash changed during restoration")
                state["restored_original_service"] = restored_row
                state["original_service_restored"] = True
                restored = True
            elif not original_stopped:
                state["original_service_restored"] = bool(original_pid is not None and active == [original_pid])
                restored = state["original_service_restored"]
            elif active and original_pid in active:
                state["original_service_restored"] = True
                restored = True
            elif active:
                raise RuntimeError(f"unexpected process active before original restore: {active}")
        except Exception as restore_exc:
            state["restore_error"] = f"{type(restore_exc).__name__}: {restore_exc}"
            state["original_service_restored"] = False

        try:
            state["selinux_after"] = device.shell("getenforce").strip()
            state["pids_at_finish"] = device.pids()
            state["original_service_restored"] = restored
        except Exception as state_exc:
            state["finish_state_error"] = f"{type(state_exc).__name__}: {state_exc}"
        state["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        if "error" not in state and "restore_error" in state:
            state["error"] = state["restore_error"]
        if "error" not in state and "cleanup_error" in state:
            state["error"] = state["cleanup_error"]
        if "error" not in state and "evidence_collection_error" in state:
            state["error"] = state["evidence_collection_error"]
        state["phase"] = "complete" if "error" not in state and restored else "incomplete"
        state["service_chain_result"] = ("one_verified_anonymous_reclaim_write_observed" if
            state.get("action_summary", {}).get("one_verified_reclaim_write_observed") else
            "no_verified_anonymous_reclaim_write_observed")
        state["claim"] = "service-linkage diagnostic only; no A/B comparison or stable-benefit claim"
        write_json(report_path, state)

    if state["phase"] != "complete":
        raise RuntimeError(state.get("error", "diagnostic ended without verified original-service restoration"))
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hdc", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--binary", type=Path, required=True,
                        help="locally built Harmony aarch64 memshiftd")
    parser.add_argument("--browser-manifest", type=Path, required=True,
                        help="reviewed 6-window/18-page preparation manifest")
    parser.add_argument("--output", type=Path, required=True,
                        help="new, empty local result directory")
    parser.add_argument("--event-ahead-seconds", type=int, default=90)
    parser.add_argument("--post-event-seconds", type=int, default=25)
    parser.add_argument("--margin-mib", type=int, default=256)
    parser.add_argument("--reserve-mib", type=int, default=1024)
    parser.add_argument("--batch-mib", type=int, default=512)
    parser.add_argument("--global-quota-mib", type=int, default=1024)
    parser.add_argument("--app-quota-mib", type=int, default=1024)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-service-replacement", action="store_true",
                        help="required acknowledgement: stop the one current daemon and restore it afterward")
    args = parser.parse_args()
    try:
        state = run(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
            configparser.Error, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"browser reclaim diagnostic failed: {exc}", file=sys.stderr)
        if args.output.exists() and (args.output / "diagnostic.json").exists():
            print("partial report: " + str(args.output.resolve() / "diagnostic.json"), file=sys.stderr)
        return 2
    print(json.dumps({"phase": state["phase"], "case_id": state["event"]["case_id"],
                      "event_T_boottime_seconds": state["event"]["T_boottime_seconds"],
                      "needed_mib": state["event"]["needed_mib"],
                      "original_service_restored": state["original_service_restored"],
                      "report": str((args.output.resolve() / "diagnostic.json"))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
