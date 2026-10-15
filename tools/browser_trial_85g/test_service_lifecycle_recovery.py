"""Offline regressions for service lifecycle recovery after TERM races."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service_lifecycle import BrowserServiceLifecycle


def proc_stat(start_ticks: int) -> str:
    # /proc/PID/stat field 22 (starttime) is token 19 after the final ')' .
    fields_after_comm = ["R"] + ["0"] * 18 + [str(start_ticks)]
    return "101 (memshiftd) " + " ".join(fields_after_comm)


class ProcRaceDevice:
    def __init__(self):
        self.term_sent = False
        self.commands = []

    def shell(self, command, **kwargs):
        self.commands.append(command)
        if command == "cat /proc/101/stat":
            return "cat: /proc/101/stat: No such file or directory" if self.term_sent else proc_stat(77)
        if command == "tr '\\000' '\\n' < /proc/101/cmdline":
            return "--config\n/original.ini\n--apply\n"
        if command == "readlink /proc/101/exe":
            return "/original/memshiftd\n"
        if command == "kill -TERM 101":
            self.term_sent = True
            return ""
        raise AssertionError(f"unexpected shell command: {command}")


class RestoreDevice:
    def __init__(self):
        self.launches = 0

    def shell(self, command, **kwargs):
        if "ORIGINAL_INI_RC" in command:
            return "ORIGINAL_INI_RC=0"
        if "ORIGINAL_CONTROL_RC" in command:
            return "ORIGINAL_CONTROL_RC=0"
        if command.startswith("nohup ") and "original_restore.log" in command:
            self.launches += 1
            return "7520\n"
        if command.startswith("cat "):
            return "paused=true\n"
        if command.startswith("readlink /proc/7515/exe"):
            return BrowserServiceLifecycle.ORIGINAL_EXE + "\n"
        if command.startswith("tr ") and "/proc/7515/cmdline" in command:
            return "--config\n/original.ini\n--apply\n"
        raise AssertionError(f"unexpected shell command: {command}")


class ServiceLifecycleRecoveryTests(unittest.TestCase):
    def test_stat_start_handles_error_empty_and_normal_stat(self):
        lifecycle = BrowserServiceLifecycle.__new__(BrowserServiceLifecycle)
        responses = iter(("cat: /proc/1/stat: Permission denied", "", proc_stat(9876)))
        lifecycle._shell = lambda *args, **kwargs: next(responses)
        self.assertEqual(lifecycle._stat_start(1), 0)
        self.assertEqual(lifecycle._stat_start(1), 0)
        self.assertEqual(lifecycle._stat_start(1), 9876)

    def test_signal_exact_accepts_proc_disappearing_immediately_after_term(self):
        device = ProcRaceDevice()
        lifecycle = BrowserServiceLifecycle.__new__(BrowserServiceLifecycle)
        lifecycle.device = device
        lifecycle._signal_exact(101, 77, "/original/memshiftd", "/original.ini")
        self.assertTrue(device.term_sent)

    def test_exit_restores_if_stop_raised_after_original_disappeared(self):
        device = RestoreDevice()
        lifecycle = BrowserServiceLifecycle.__new__(BrowserServiceLifecycle)
        lifecycle.device = device
        lifecycle.original_stopped = False
        lifecycle.original_stop_attempted = True
        lifecycle.original_pid = 7515
        lifecycle.original_start = 25282730
        lifecycle.original_exe_path = BrowserServiceLifecycle.ORIGINAL_EXE
        lifecycle.original_ini_path = "/original.ini"
        lifecycle.original_control_path = "/control.conf"
        lifecycle.candidate_daemon_path = "/candidate/memshiftd"
        lifecycle.candidate_ini_path = "/candidate/service.ini"
        lifecycle.candidate_pid = None
        lifecycle.candidate_start = None
        lifecycle.remote_root = "/remote/run"
        lifecycle.id = "test"
        lifecycle.manifest = {"files": {"ini": "unused.ini", "control": "unused.conf"},
            "sha256": {"daemon": "d" * 64, "ini": "i" * 64, "control": "c" * 64}}
        lifecycle.restore_result = {"attempted": False, "verified": False}
        lifecycle.out = Path(tempfile.mkdtemp())
        lifecycle._pids = patch("service_lifecycle.BrowserServiceLifecycle._pids").start()
        pid_reads = iter(([], []))
        pid_trace = []
        lifecycle._pids.side_effect = lambda: (pid_trace.append(1) or next(pid_reads, [7515]))
        lifecycle._stat_start = lambda pid: 25282730
        lifecycle._exe = lambda pid: BrowserServiceLifecycle.ORIGINAL_EXE
        lifecycle._cmdline = lambda pid: ["--config", "/original.ini", "--apply"]
        lifecycle._config_arg = lambda argv: "/original.ini" if "--config" in argv else None
        lifecycle._remote_hash = lambda path: {
            BrowserServiceLifecycle.ORIGINAL_EXE: "d" * 64,
            "/original.ini": "i" * 64,
            "/control.conf": "c" * 64,
        }[path]
        lifecycle._control_paused = lambda path: True
        lifecycle._collect_service_artifacts = lambda: {"collected_after_candidate_exit": True}
        try:
            # This is the context-manager path after _signal_exact raises due to
            # a lost TERM response, while pidof confirms the original is gone.
            lifecycle.__exit__(RuntimeError, RuntimeError("TERM response lost"), None)
        finally:
            lifecycle._pids.stop()
        self.assertEqual(device.launches, 1)
        self.assertTrue(lifecycle.restore_result["verified"], {"result": lifecycle.restore_result,
            "pid_reads": len(pid_trace)})
        self.assertEqual(lifecycle.restore_result["pid"], 7515)


if __name__ == "__main__":
    unittest.main()
