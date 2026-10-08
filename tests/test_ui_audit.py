from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.audit_scene2_ui import (audit_run, foreground_low_memory_missions,
                                  _cases_hash, _test_popup_journal_proof,
                                  verify_audit_document)
from memscenes.runner import freeze


TASKMANAGER_ACTIVE_C0 = """\
Mission ID #314  mission name #[#com.huawei.hmos.taskmanager:manager:LowMemoryAbility]
  AbilityRecord ID #1728
    main name [LowMemoryAbility]
    bundle name [com.huawei.hmos.taskmanager]
    ability type [PAGE]
    state #FOREGROUND  start time [15267048]
    app state #FOREGROUND
"""

TASKMANAGER_INITIAL_C7 = """\
Mission ID #274  mission name #[#com.huawei.hmos.taskmanager:manager:LowMemoryAbility]
  AbilityRecord ID #1524
    main name [LowMemoryAbility]
    bundle name [com.huawei.hmos.taskmanager]
    ability type [PAGE]
    state #INITIAL  start time [13244970]
    app state #BEGIN
"""


class PopupAuditTests(unittest.TestCase):
    def test_real_c0000_taskmanager_mission_is_active_at_test_screenshot(self) -> None:
        active = foreground_low_memory_missions(TASKMANAGER_ACTIVE_C0, 15299.91)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["mission_id"], "314")
        self.assertEqual(active[0]["capture_uptime_ms"], 15299910)

    def test_prior_c0007_initial_mission_keeps_unoccluded_test_popup(self) -> None:
        active = foreground_low_memory_missions(TASKMANAGER_INITIAL_C7, 13311.28)
        self.assertEqual(active, [])

    def test_prior_c0007_callback_and_request_remain_a_valid_test_popup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "journals").mkdir()
            case_id = "s2_1d4d2d3973c5_c0007"
            bundle = "io.memscene.workload.a6"
            rows = [
                {"event": "memory_notification", "level": 2, "case_id": case_id,
                 "phase": "peak_allocating", "time_ms": 1791278899843},
                {"event": "popup_requested", "level": 2, "case_id": case_id,
                 "phase": "peak_allocating", "time_ms": 1791278899845},
            ]
            path = root / "journals" / f"{case_id}_{bundle}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            proof = _test_popup_journal_proof(
                root, case_id, bundle, 1791278899843, 1791278968000, 1791278800000)
            self.assertTrue(proof["notification_matched"])
            self.assertTrue(proof["popup_request_matched"])

    def test_offline_audit_removes_only_obscured_test_event_and_binds_ui_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "ui").mkdir()
            (root / "ui" / "test.png").write_bytes(b"paired screenshot bytes")
            (root / "ui" / "test.json").write_text("{}", encoding="utf-8")
            (root / "ui" / "missions.txt").write_text(TASKMANAGER_ACTIVE_C0, encoding="utf-8")
            specs = [{"case_id": f"case-{i}", "kind": "startup"} for i in range(11)]
            case = {
                "case_id": "case-0", "case_measurement_complete": True,
                "daily_stage_complete": True, "daily_barrier_released": True,
                "attempts_terminal_complete": True, "kernel_coverage_complete": True,
                "popup_observation_complete": True,
                "trace_segment": {"marker_coverage": {"expected": 2, "present": 2}},
                "bundles": [], "pre_promote_identity_checks": [],
                "restore_initial_state": {"verified_absent": True},
                "owned_app_cleanup": [], "owned_page_cleanup": [],
                "measurement_interval": {"begin_wall_ms": 1000, "end_wall_ms": 3000},
                "popup_observations": [{
                    "case_id": "case-0", "case_attribution_allowed": True,
                    "capture_clock_valid": True, "screenshot_captured": True,
                    "capture_uptime_seconds": 15299.91,
                    "screenshot_complete_wall_ms": 2000,
                    "screenshot": "ui\\test.png", "layout": "ui\\test.json",
                    "mission_list": "ui\\missions.txt",
                }],
            }
            test_event = {
                "category": "low_memory_popup_test", "case_id": "case-0",
                "bundle": "io.memscene.workload.a1", "pid": 23760,
                "timestamp_ms": 2000, "incident": "test-popup-0",
                "evidence": ["ui\\test.png"],
            }
            events = [
                {"category": "startup_failure", "case_id": "case-0", "incident": "start-0"},
                {"category": "oom_kill", "case_id": "case-0", "incident": "oom-0"},
                test_event,
            ]
            result = {
                "session": "fixture", "scene": 2, "phase": "baseline", "profile": "qualification",
                "execution_complete": True, "cases": specs, "cases_hash": _cases_hash(specs),
                "case_results": [case] + [dict(case, case_id=f"case-{i}") for i in range(1, 11)],
                "events": events, "counts": {"total_events": 4}, "qualification_scope": "prevalidation_32gb",
            }
            result_path = root / "result.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            (root / "case_results.json").write_text(json.dumps(result["case_results"]), encoding="utf-8")
            (root / "progress.json").write_text("{}", encoding="utf-8")

            audit = audit_run(result_path, root / "audit.json")
            self.assertEqual(audit["counts_after"]["low_memory_popup_test"], 0)
            self.assertEqual(audit["counts_after"]["total_events"], 2)
            self.assertEqual(audit["counts_after"]["unique_incidents"], 2)
            removed = [x["event_id"] for x in audit["decisions"] if x["action"] == "remove"]
            self.assertEqual(audit["removed_event_ids"], removed)
            self.assertIn("ui/test.png", audit["source_hashes"]["ui_evidence"])
            self.assertIn("ui/test.json", audit["source_hashes"]["ui_evidence"])
            self.assertIn("ui/missions.txt", audit["source_hashes"]["ui_evidence"])
            audit_path = root / "audit.json"
            self.assertEqual(verify_audit_document(audit_path), (True, "verified"))
            # The fixture has only one of 88 journal pairs, so a source-valid audit
            # cannot claim primary completeness or qualification eligibility.
            self.assertFalse(audit["primary_evidence_complete"])
            self.assertFalse(audit["qualification_eligible"])
            calibration = root / "calibration.json"
            calibration.write_text(json.dumps({"suite_hash": "unused", "profile": "qualification",
                                               "checks": {"valid": True}}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "全部固定用例和原始主证据完整"):
                freeze(result_path, calibration, root / "should-not-freeze.json", audit_path)
            (root / "ui" / "test.png").write_bytes(b"tampered screenshot")
            self.assertEqual(verify_audit_document(audit_path), (False, "audit_or_source_hash_mismatch"))
            (root / "ui" / "test.png").write_bytes(b"paired screenshot bytes")
            changed = json.loads(audit_path.read_text(encoding="utf-8"))
            changed["counts_after"]["total_events"] = 1000
            audit_path.write_text(json.dumps(changed), encoding="utf-8")
            self.assertEqual(verify_audit_document(audit_path), (False, "audit_or_source_hash_mismatch"))
            portable_path = root / "portable_audit.json"
            audit_run(result_path, portable_path, "result.json")
            self.assertEqual(verify_audit_document(portable_path), (True, "verified"))
            relocated = Path(tmp) / "relocated"
            shutil.copytree(root, relocated)
            self.assertEqual(verify_audit_document(relocated / "portable_audit.json"),
                             (True, "verified"))


if __name__ == "__main__":
    unittest.main()
