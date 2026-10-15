"""Single-instance browser-profile service swap with byte-exact finally restore."""
from __future__ import annotations

import configparser
import hashlib
import json
import re
import secrets
import time
from pathlib import Path


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def q(s: str | Path) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def parse_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read(path, encoding="utf-8")
    if not parser.has_section("daemon"):
        raise ValueError(f"service config lacks [daemon]: {path}")
    return parser


class BrowserServiceLifecycle:
    """Swap the one existing service; always restore its captured exact state."""
    ORIGINAL_EXE = "/data/local/tmp/memshift_wps_gate_20261013/memshiftd"
    BROWSER_WHOLE_WORK_CAP_MIB = 8192

    def __init__(self, device, phase: str, service_template: Path,
                 browser_plan: Path, browser_uid: int, local_backups: Path,
                 output_dir: Path):
        self.device = device
        self.phase = phase
        self.template = service_template.resolve(strict=True)
        self.browser_plan = browser_plan.resolve(strict=True)
        self.browser_uid = int(browser_uid)
        self.backups = local_backups.resolve(strict=True)
        self.out = output_dir
        self.manifest = self._verify_local_backup_set()
        self.id = secrets.token_hex(8)
        self.remote_root = "/data/local/tmp/browser_trial_" + self.id
        self.original_ini_path: str | None = None
        self.original_control_path: str | None = None
        self.original_pid: int | None = None
        self.original_start: int | None = None
        self.candidate_pid: int | None = None
        self.candidate_start: int | None = None
        self.original_stopped = False
        self.original_stop_attempted = False
        self.switched = False
        self.restore_result: dict = {"attempted": False, "verified": False}
        self.candidate_ini_path = self.remote_root + "/service.ini"
        self.candidate_control_path = self.remote_root + "/control.conf"
        self.candidate_plan_path = self.remote_root + "/browser_event_plan.tsv"
        self.candidate_ledger_path = self.remote_root + "/browser_evidence.tsv"
        self.candidate_daemon_path = self.remote_root + "/memshiftd"
        self.candidate_log_path = self.remote_root + "/memshiftd.log"

    def _verify_local_backup_set(self) -> dict:
        files = {"daemon": self.backups / "original_memshiftd",
                 "ini": self.backups / "original_service.ini",
                 "control": self.backups / "original_control.conf",
                 "hashes": self.backups / "pre_validation_hashes.txt"}
        missing = [key for key, path in files.items() if not path.is_file()]
        if missing:
            raise ValueError("original service backup set missing: " + ", ".join(missing))
        recorded = {}
        for line in files["hashes"].read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                recorded[parts[1]] = parts[0].lower()
        expected = {
            "daemon": (files["daemon"], "60b871e051e7fb005fc7c4882ef1641374f9cb4bbb56b69574c216aa0904881f"),
            "ini": (files["ini"], "a081620837715a385ff5c4bd681c4a4a28443fbca609bd55b89d4f75b223d722"),
            "control": (files["control"], "5062cae48b51ed17573558e265b80bed7905001e6cb2024aee3b03290becb80d"),
        }
        actual = {}
        for key, (path, digest) in expected.items():
            actual[key] = sha(path)
            if actual[key] != digest:
                raise ValueError(f"trusted original {key} backup hash mismatch")
        backup_ini = parse_ini(files["ini"])
        receipt_paths = {
            self.ORIGINAL_EXE: actual["daemon"],
            "/data/local/tmp/memshift_wps_gate_20261013/wps_window_gate.ini": actual["ini"],
            backup_ini.get("daemon", "control_file", fallback="").strip(): actual["control"],
        }
        if any(not path or recorded.get(path) != digest for path, digest in receipt_paths.items()):
            raise ValueError("pre-validation receipt does not agree with local backup hashes")
        return {"files": {k: str(v.resolve()) for k, v in files.items()},
                "sha256": actual}

    def _shell(self, command: str, *, check: bool = True, timeout: int = 20) -> str:
        return self.device.shell(command, check=check, timeout=timeout)

    def _pids(self) -> list[int]:
        return [int(x) for x in self._shell("pidof memshiftd", check=False).split() if x.isdecimal()]

    def _stat_start(self, pid: int) -> int:
        raw = self._shell(f"cat /proc/{pid}/stat", check=False).strip()
        if not raw:
            return 0
        try:
            return int(raw.rsplit(")", 1)[1].split()[19])
        except (IndexError, ValueError):
            # A concurrent exit can leave cat's stderr text as the only output.
            return 0

    def _exe(self, pid: int) -> str:
        return self._shell(f"readlink /proc/{pid}/exe", check=False).strip()

    def _cmdline(self, pid: int) -> list[str]:
        return self._shell(f"tr '\\000' '\\n' < /proc/{pid}/cmdline", check=False).splitlines()

    def _config_arg(self, argv: list[str]) -> str | None:
        try:
            return argv[argv.index("--config") + 1]
        except (ValueError, IndexError):
            return None

    def _upload(self, local: Path, remote: str, digest: str | None = None) -> None:
        local = local.resolve(strict=True)
        self.device.call("file", "send", str(local), remote, timeout=60)
        got = self._shell("sha256sum " + q(remote)).split()[0].lower()
        expected = digest or sha(local)
        if got != expected.lower():
            raise RuntimeError(f"device file hash mismatch: {remote}")

    def _remote_hash(self, remote: str) -> str:
        return self._shell("sha256sum " + q(remote)).split()[0].lower()

    def _control_paused(self, remote: str) -> bool:
        raw = self._shell("cat " + q(remote))
        match = re.search(r"(?m)^paused\s*=\s*(true|false)\s*$", raw, re.I)
        if not match:
            raise RuntimeError("service control file lacks a valid paused setting")
        return match.group(1).lower() == "true"

    def _wait_single_candidate(self, expected_exe: str, expected_config: str,
                               expected_paused: bool, timeout: float = 12) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            pids = self._pids()
            if len(pids) == 1:
                pid = pids[0]
                argv = self._cmdline(pid)
                last = {"pids": pids, "exe": self._exe(pid), "argv": argv}
                if (last["exe"] == expected_exe and self._config_arg(argv) == expected_config and
                        self._control_paused(self.candidate_control_path) == expected_paused):
                    ticks = self._stat_start(pid)
                    if ticks > 0:
                        return pid, ticks
            time.sleep(0.25)
        raise RuntimeError("candidate service failed single-instance identity/control verification: " + repr(last))

    def _signal_exact(self, pid: int, ticks: int, exe: str, config_path: str) -> None:
        current_argv = self._cmdline(pid)
        if (self._stat_start(pid) != ticks or self._exe(pid) != exe or
                self._config_arg(current_argv) != config_path):
            raise RuntimeError("refusing to signal service after process identity changed")
        self._shell(f"kill -TERM {pid}", timeout=5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._stat_start(pid) != ticks:
                return
            time.sleep(0.25)
        raise RuntimeError("service did not exit after TERM")

    def _restore_file(self, local: Path, remote: str, digest: str) -> None:
        temp = remote + ".browser_trial_restore_" + self.id
        self._upload(local, temp, digest)
        self._shell("mv " + q(temp) + " " + q(remote))
        if self._remote_hash(remote) != digest:
            raise RuntimeError(f"exact restore hash failed: {remote}")

    def __enter__(self):
        if self.device.shell("getenforce", timeout=5).strip() != "Enforcing":
            raise RuntimeError("SELinux must remain Enforcing")
        pids = self._pids()
        if len(pids) != 1:
            raise RuntimeError(f"expected one original memshiftd, observed {pids}")
        pid = pids[0]
        argv = self._cmdline(pid)
        exe = self._exe(pid)
        ini_path = self._config_arg(argv)
        if exe != self.ORIGINAL_EXE or not ini_path or "--apply" not in argv or "harmony-v1-ratio" not in argv:
            raise RuntimeError(f"active service identity/argv differs from recorded original: {exe} {argv}")
        ini_backup = Path(self.manifest["files"]["ini"])
        control_backup = Path(self.manifest["files"]["control"])
        original_ini = parse_ini(ini_backup)
        control_path = original_ini.get("daemon", "control_file", fallback="").strip()
        if not control_path.startswith("/"):
            raise RuntimeError("trusted original config has no absolute control_file")
        if (self._remote_hash(exe) != self.manifest["sha256"]["daemon"] or
                self._remote_hash(ini_path) != self.manifest["sha256"]["ini"] or
                self._remote_hash(control_path) != self.manifest["sha256"]["control"]):
            raise RuntimeError("live original daemon/config/control differs from trusted backup hashes")
        current_control = self._shell("cat " + q(control_path))
        if current_control.encode("utf-8") != control_backup.read_bytes():
            raise RuntimeError("live original control bytes differ from saved exact control")
        self.original_pid, self.original_start = pid, self._stat_start(pid)
        self.original_ini_path, self.original_control_path = ini_path, control_path
        self.original_exe_path = exe
        self.original_argv = argv
        return self

    def activate(self, before_original_stop=None):
        if not self.original_pid or not self.original_ini_path:
            raise RuntimeError("service lifecycle preflight must complete before activate")
        pid, exe, ini_path = self.original_pid, self.original_exe_path, self.original_ini_path
        self._prepare_candidate_config()
        self._shell("test ! -e " + q(self.remote_root) + " && mkdir -p " + q(self.remote_root + "/logs"))
        daemon_local = getattr(self, "daemon_local")
        self._upload(daemon_local, self.candidate_daemon_path)
        self._upload(self.generated_ini, self.candidate_ini_path)
        self._upload(self.browser_plan, self.candidate_plan_path)
        control_local = self.out / "control.generated.conf"
        control_local.write_text(self._control_payload(), encoding="utf-8", newline="\n")
        self._upload(control_local, self.candidate_control_path, sha(control_local))
        candidate_control_raw = self._shell("cat " + q(self.candidate_control_path))
        if candidate_control_raw != self._control_payload():
            raise RuntimeError("candidate control file content failed readback")
        self._shell("chmod 755 " + q(self.candidate_daemon_path) + " && test -x " + q(self.candidate_daemon_path))
        check = self._shell(q(self.candidate_daemon_path) + " --config " + q(self.candidate_ini_path) +
                            " --backend harmony-v1-ratio --check; rc=$?; echo CANDIDATE_CHECK_RC=$rc",
                            check=False, timeout=25)
        if "CANDIDATE_CHECK_RC=0" not in check:
            raise RuntimeError("candidate daemon --check failed before original service stop: " + check[-1200:])
        if self._remote_hash(self.candidate_plan_path) != sha(self.browser_plan):
            raise RuntimeError("staged browser event plan hash mismatch")
        # Configured path and transferred path must be byte-identical.
        if self.generated_ini_parser.get("daemon", "daily_known_plan_file") != self.candidate_plan_path:
            raise RuntimeError("service plan path differs from staged browser plan")
        if self._remote_hash(ini_path) != self.manifest["sha256"]["ini"]:
            raise RuntimeError("original service INI changed before switch")
        # Arm evidence capture after all candidate checks and before the active
        # service is stopped or replaced.
        if before_original_stop is not None:
            before_original_stop()
        # Record the stop attempt first. If HDC returns an error or /proc vanishes
        # between TERM and the post-signal poll, __exit__ must still restore.
        self.original_stop_attempted = True
        self._signal_exact(pid, self.original_start, exe, ini_path)
        self.original_stopped = True
        if self._pids():
            raise RuntimeError("another memshiftd instance appeared while stopping original")
        launch = ("nohup " + q(self.candidate_daemon_path) + " --config " + q(self.candidate_ini_path) +
            " --backend harmony-v1-ratio --apply > " + q(self.candidate_log_path) +
            " 2>&1 < /dev/null & echo $!")
        start_output = self._shell(launch, timeout=10).strip()
        if start_output.isdecimal():
            observed_pid = int(start_output)
            observed_start = self._stat_start(observed_pid)
            if observed_start > 0:
                self.candidate_pid, self.candidate_start = observed_pid, observed_start
        expected_paused = self.phase == "baseline"
        verified_pid, verified_start = self._wait_single_candidate(
            self.candidate_daemon_path, self.candidate_ini_path, expected_paused)
        if self.candidate_pid not in (None, verified_pid) or self.candidate_start not in (None, verified_start):
            raise RuntimeError("candidate daemon identity changed during startup")
        self.candidate_pid, self.candidate_start = verified_pid, verified_start
        self.switched = True
        self.out.joinpath("service_swap.json").write_text(json.dumps({
            "original": {"pid": self.original_pid, "start_ticks": self.original_start,
                "exe": exe, "config": ini_path, "control": self.original_control_path,
                "hashes": self.manifest["sha256"], "argv": self.original_argv},
            "candidate": {"pid": self.candidate_pid, "start_ticks": self.candidate_start,
                "exe": self.candidate_daemon_path, "config": self.candidate_ini_path,
                "control": self.candidate_control_path, "paused": expected_paused,
                "config_sha256": sha(self.generated_ini), "plan_sha256": sha(self.browser_plan),
                "start_output": start_output},
            "remote_root": self.remote_root}, indent=2) + "\n", encoding="utf-8")
        return self

    def _prepare_candidate_config(self) -> None:
        parser = parse_ini(self.template)
        d = parser["daemon"]
        d["daily_known_plan_file"] = self.candidate_plan_path
        d["daily_known_evidence_ledger"] = self.candidate_ledger_path
        d["control_file"] = self.candidate_control_path
        d["lock_file"] = self.remote_root + "/daemon.lock"
        d["daily_disk_mode"] = "owned_apply"
        # This 8192 MiB owned-apply work budget is an independent policy cap,
        # deliberately held constant for the 10 GiB pressure-size calibration.
        d["daily_disk_whole_work_mb"] = str(self.BROWSER_WHOLE_WORK_CAP_MIB)
        d["daily_async"] = "true"
        d["daily_known_lead_sec"] = "90"
        d["daily_known_capacity"] = "true"
        d["daily_known_strength"] = "true"
        d["daily_known_future_free_only"] = "true"
        d["daily_owned_os_snapshot"] = "true"
        d["daily_browser_donor_profile"] = "true"
        # Keep ordinary action limits from the reviewed owned_apply profile;
        # this is a real A/B phase, not the one-action service diagnostic.
        d["daily_max_submissions"] = "0"
        d["log_dir"] = self.remote_root + "/logs"
        browser = parser["app browser"] if parser.has_section("app browser") else parser["app:browser"]
        browser["path"] = f"100/com.huawei.hmos.browser_{self.browser_uid}"
        browser["reclaim"] = "true"
        browser["min_keep_mb"] = str(max(128, int(browser.get("min_keep_mb", "128"))))
        self.generated_ini = self.out / "service.generated.ini"
        with self.generated_ini.open("w", encoding="utf-8", newline="\n") as f:
            parser.write(f)
        self.generated_ini_parser = parser

    def _control_payload(self) -> str:
        paused = "true" if self.phase == "baseline" else "false"
        return f"paused={paused}\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0\n"

    def _collect_service_artifacts(self) -> dict:
        from memscenes.device import quote as _q
        local_dir = self.out / "service_artifacts"
        local_dir.mkdir(parents=True, exist_ok=True)
        paths = [self.candidate_ini_path, self.candidate_control_path, self.candidate_plan_path,
            self.candidate_ledger_path, self.candidate_ledger_path + ".device_proof_v1.csv",
            self.candidate_ledger_path + ".device_access_v1.csv",
            self.candidate_ledger_path + ".device_cycle_v1.csv",
            self.candidate_ledger_path + ".proc_file_writeback_v1.csv",
            self.candidate_ledger_path + ".postwrite_recheck_v1.csv",
            self.candidate_ledger_path + ".post_fsync_recheck_v1.csv",
            self.candidate_ledger_path + ".swap_telemetry_v1.csv",
            self.candidate_ledger_path + ".timing_v3.csv",
            self.candidate_ledger_path + ".disk_v2.csv",
            self.candidate_ledger_path + ".free_budget_v1.csv"]
        paths.extend(self.remote_root + "/logs/harmony-v1-ratio/" + name for name in
                     ("system.csv", "apps.csv", "reclaim.csv", "events.csv",
                      "daily_actions.csv", "daily_known_future.csv"))
        paths.append(self.candidate_log_path)
        collected, absent, errors = [], [], []
        for remote in paths:
            try:
                exists = self._shell("test -f " + _q(remote) + "; echo FILE_RC=$?", check=False)
                if "FILE_RC=0" not in exists:
                    absent.append(remote)
                    continue
                target = local_dir / remote.replace("/", "__").strip("_")
                self.device.receive(remote, target)
                collected.append({"remote": remote, "local": str(target.resolve()),
                    "size_bytes": target.stat().st_size, "sha256": sha(target)})
            except Exception as exc:
                errors.append({"remote": remote, "error": f"{type(exc).__name__}: {exc}"})
        manifest = {"collected_after_candidate_exit": True, "files": collected,
            "absent": absent, "errors": errors,
            "expected_groups": ["plan", "known_event_evidence", "system", "apps", "reclaim", "service_stdout"]}
        (self.out / "service_artifact_collection.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return manifest

    def __exit__(self, exc_type, exc, tb):
        result = {"attempted": True, "verified": False, "errors": []}
        try:
            restore_required = self.original_stopped or self.original_stop_attempted
            pids = self._pids()
            if len(pids) > 1:
                raise RuntimeError(f"multiple daemons during restore: {pids}")
            if len(pids) == 1:
                pid = pids[0]
                if self._exe(pid) == self.candidate_daemon_path and self._config_arg(self._cmdline(pid)) == self.candidate_ini_path:
                    live_start = self._stat_start(pid)
                    if self.candidate_pid is not None and (self.candidate_pid != pid or self.candidate_start != live_start):
                        raise RuntimeError("candidate PID/start changed; refusing kill")
                    # If launch output was lost, rediscover only by exact exe/config and bind start now.
                    self.candidate_pid, self.candidate_start = pid, live_start
                    self._signal_exact(pid, live_start, self.candidate_daemon_path, self.candidate_ini_path)
                    result["candidate_artifacts"] = self._collect_service_artifacts()
                elif (pid == self.original_pid and self._stat_start(pid) == self.original_start and
                      self._exe(pid) == self.original_exe_path and
                      self._config_arg(self._cmdline(pid)) == self.original_ini_path):
                    # TERM may have failed before taking effect. Verify and leave
                    # the exact original instance running instead of restoring a
                    # second daemon over it.
                    restore_required = False
                    self.original_stop_attempted = False
                    result.update({"skipped": True, "reason": "original remained live after stop attempt"})
                elif restore_required:
                    raise RuntimeError("active service is not the owned candidate or original; refusing signal/restore")
                else:
                    result.update({"skipped": True, "reason": "original service was never stopped"})
            elif restore_required and not result.get("candidate_artifacts"):
                result["candidate_artifacts"] = self._collect_service_artifacts()
            if restore_required and self._pids():
                raise RuntimeError("cannot restore original while any memshiftd is active")
            if restore_required:
                ini_exists = "ORIGINAL_INI_RC=0" in self._shell(
                    "test -f " + q(self.original_ini_path) + "; echo ORIGINAL_INI_RC=$?", check=False)
                control_exists = "ORIGINAL_CONTROL_RC=0" in self._shell(
                    "test -f " + q(self.original_control_path) + "; echo ORIGINAL_CONTROL_RC=$?", check=False)
                ini_before = self._remote_hash(self.original_ini_path) if ini_exists else None
                control_before = self._remote_hash(self.original_control_path) if control_exists else None
                if ini_before != self.manifest["sha256"]["ini"]:
                    self._restore_file(Path(self.manifest["files"]["ini"]), self.original_ini_path,
                                       self.manifest["sha256"]["ini"])
                if control_before != self.manifest["sha256"]["control"]:
                    self._restore_file(Path(self.manifest["files"]["control"]), self.original_control_path,
                                       self.manifest["sha256"]["control"])
                result["original_files_before_restore"] = {"ini_sha256": ini_before,
                    "control_sha256": control_before, "overwritten_from_backup": {
                        "ini": ini_before != self.manifest["sha256"]["ini"],
                        "control": control_before != self.manifest["sha256"]["control"]}}
                log = self.remote_root + "/original_restore.log"
                command = ("nohup " + q(self.ORIGINAL_EXE) + " --config " + q(self.original_ini_path) +
                    " --backend harmony-v1-ratio --apply > " + q(log) + " 2>&1 < /dev/null & echo $!")
                result["start_output"] = self._shell(command, timeout=10).strip()
                deadline = time.monotonic() + 15
                verified_pid = None
                while time.monotonic() < deadline:
                    pids = self._pids()
                    if len(pids) == 1:
                        pid = pids[0]
                        argv = self._cmdline(pid)
                        if self._exe(pid) == self.ORIGINAL_EXE and self._config_arg(argv) == self.original_ini_path:
                            verified_pid = pid
                            break
                    time.sleep(0.25)
                if verified_pid is None:
                    raise RuntimeError("restored original service did not reach exact single-instance identity")
                result.update({"pid": verified_pid, "exe": self._exe(verified_pid),
                    "argv": self._cmdline(verified_pid),
                    "daemon_sha256": self._remote_hash(self.ORIGINAL_EXE),
                    "ini_sha256": self._remote_hash(self.original_ini_path),
                    "control_sha256": self._remote_hash(self.original_control_path),
                    "paused": self._control_paused(self.original_control_path),
                    "instance_count": len(self._pids())})
                result["verified"] = (result["instance_count"] == 1 and
                    result["daemon_sha256"] == self.manifest["sha256"]["daemon"] and
                    result["ini_sha256"] == self.manifest["sha256"]["ini"] and
                    result["control_sha256"] == self.manifest["sha256"]["control"])
                self.original_stopped = result["verified"]
                self.original_stop_attempted = False
                if not result["verified"]:
                    raise RuntimeError("restored original bytes/instance verification failed")
            else:
                pids = self._pids()
                unchanged = (len(pids) == 1 and pids[0] == self.original_pid and
                    self._stat_start(pids[0]) == self.original_start and
                    self._exe(pids[0]) == self.original_exe_path and
                    self._config_arg(self._cmdline(pids[0])) == self.original_ini_path and
                    self._remote_hash(self.original_exe_path) == self.manifest["sha256"]["daemon"] and
                    self._remote_hash(self.original_ini_path) == self.manifest["sha256"]["ini"] and
                    self._remote_hash(self.original_control_path) == self.manifest["sha256"]["control"])
                result.update({"skipped": True, "reason": "original was never stopped",
                    "instance_count": len(pids), "pid": pids[0] if len(pids) == 1 else None,
                    "unchanged_original_verified": unchanged})
                result["verified"] = unchanged
        except Exception as restore_exc:
            result["errors"].append(type(restore_exc).__name__ + ": " + str(restore_exc))
        self.restore_result = result
        (self.out / "service_restore.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if not result["verified"] and exc is None:
            raise RuntimeError("service lifecycle failed exact restoration: " + json.dumps(result))
        return False

