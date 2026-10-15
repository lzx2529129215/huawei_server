from __future__ import annotations

import configparser
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_reclaim_diagnostic import (DeviceOps, assert_one_service, calculate_needed_mib, parse_wms_focus,
                                    render_config, render_control)


class ReclaimDiagnosticTests(unittest.TestCase):
    def test_needed_is_memfree_plus_margin_and_reserve_fits_total(self) -> None:
        free = 8_192 * 1024**2 + 1
        total = 16_384 * 1024**2
        needed = calculate_needed_mib(free, total, reserve_mib=1024, margin_mib=256)
        self.assertEqual(needed, 8_449)
        self.assertLessEqual((needed + 1024) * 1024**2, total)

    def test_rejects_event_that_cannot_fit_reserve(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds MemTotal"):
            calculate_needed_mib(15_000 * 1024**2, 16_000 * 1024**2,
                                 reserve_mib=1024, margin_mib=256)

    def test_config_is_explicit_browser_only_one_submission_no_disk(self) -> None:
        text = render_config("/data/local/tmp/memshift_browser_diag_test", 20020064, {
            "disk_mode": "off", "max_submissions": 1, "reserve_mib": 1024,
            "batch_mib": 512, "global_quota_mib": 1024, "app_quota_mib": 1024})
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string(text)
        daemon = parser["daemon"]
        self.assertEqual(daemon["daily_browser_donor_profile"], "true")
        self.assertEqual(daemon["daily_known_lead_sec"], "95")
        self.assertEqual(daemon["daily_max_submissions"], "1")
        self.assertEqual(daemon["daily_disk_mode"], "off")
        self.assertEqual(daemon["batch_mb"], "512")
        self.assertEqual(daemon["max_per_minute_mb"], "1024")
        self.assertEqual(daemon["app_per_minute_mb"], "1024")
        reclaim_apps = [name for name in parser.sections() if name.startswith("app ") and
                        parser[name].get("reclaim", "false").lower() == "true"]
        self.assertEqual(reclaim_apps, ["app browser"])
        self.assertEqual(parser["app browser"]["min_keep_mb"], "512")

    def test_config_refuses_disk_or_multiple_submissions(self) -> None:
        settings = {"disk_mode": "owned_apply", "max_submissions": 1, "reserve_mib": 1024,
                    "batch_mib": 512, "global_quota_mib": 1024, "app_quota_mib": 1024}
        with self.assertRaisesRegex(ValueError, "disk mode off"):
            render_config("/data/local/tmp/memshift_browser_diag_test", 20020064, settings)
        settings["disk_mode"] = "off"
        settings["max_submissions"] = 2
        with self.assertRaisesRegex(ValueError, "limit submissions to one"):
            render_config("/data/local/tmp/memshift_browser_diag_test", 20020064, settings)

    def test_new_control_is_active_and_empty_protected_set(self) -> None:
        control = render_control()
        self.assertEqual(control,
            "paused=false\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0\n")

    def test_wms_focus_must_be_stable_outside_browser_group(self) -> None:
        raw = ("WindowName DisplayId Pid WinId\n" +
               "".join(f"browser{i} 0 17 {i + 1}\n" for i in range(6)) +
               "desktop 0 3 99\nFocus window: 99\nTotal window num: 7\n")
        parsed = parse_wms_focus(raw, {17, 18, 19})
        self.assertEqual(parsed["focus_pid"], 3)
        self.assertEqual(len(parsed["browser_windows"]), 6)
        bad = raw.replace("desktop 0 3 99", "desktop 0 17 99")
        with self.assertRaisesRegex(ValueError, "still the focused"):
            parse_wms_focus(bad, {17, 18, 19})

    def test_remote_check_requires_success_marker_and_expected_output(self) -> None:
        service = DeviceOps.__new__(DeviceOps)
        service.shell = lambda command, timeout=30: (  # type: ignore[method-assign]
            "backend=harmony-v1 mode=check schema=harmony-v1\n"
            "app=browser path=100/com.huawei.hmos.browser_20020064 populated=true candidate_budget=unknown reclaim=disabled\n"
            "No reclaim was attempted by --check.\n__MEMSHIFT_CHECK_RC=0")
        result = service.check_service_read_only("/data/local/tmp/diag/memshiftd",
            "/data/local/tmp/diag/service.ini", "100/com.huawei.hmos.browser_20020064")
        self.assertEqual(result["remote_exit_code"], 0)
        self.assertFalse(result["reclaim_write_performed"])
        service.shell = lambda command, timeout=30: "mode=check\n__MEMSHIFT_CHECK_RC=2"  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "--check failed"):
            service.check_service_read_only("/data/local/tmp/diag/memshiftd",
                "/data/local/tmp/diag/service.ini", "100/com.huawei.hmos.browser_20020064")

    def test_service_identity_uses_proc_exe_and_rejects_truncated_config_argv(self) -> None:
        service = DeviceOps.__new__(DeviceOps)
        service.pids = lambda: [42]  # type: ignore[method-assign]
        service.argv = lambda pid: ["memshiftd", "--config"]  # type: ignore[method-assign]
        service.executable = lambda pid: "/data/local/tmp/diag/memshiftd"  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "config argv"):
            assert_one_service(service, 42, "/data/local/tmp/diag/memshiftd",
                               "/data/local/tmp/diag/service.ini")
        service.argv = lambda pid: ["memshiftd", "--config", "/data/local/tmp/diag/service.ini"]  # type: ignore[method-assign]
        service.proc_start_ticks = lambda pid: 9876  # type: ignore[method-assign]
        identity = assert_one_service(service, 42, "/data/local/tmp/diag/memshiftd",
                                      "/data/local/tmp/diag/service.ini")
        self.assertEqual(identity["executable"], "/data/local/tmp/diag/memshiftd")
        self.assertEqual(identity["start_ticks"], 9876)


if __name__ == "__main__":
    unittest.main()
