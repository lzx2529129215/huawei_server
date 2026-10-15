"""Local no-device regressions for 9 GiB plan closure and donor identities."""
from __future__ import annotations

import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path

from native_plan import BUNDLES, serialize, validate_serialized
from native_bridge import gate_interpretation
from run_trial import (PROFILE, TARGETS, _anon_sample_stats, _verified_browser_member_identities,
                       validate_baseline_reference, validate_browser_manifest, validate_plan)


MIB = 1024 * 1024
OLD_PLAN_PATH = Path(__file__).resolve().parents[1] / "browser_trial" / "native_plan.py"
OLD_PLAN_SPEC = importlib.util.spec_from_file_location("browser_trial_8g_native_plan", OLD_PLAN_PATH)
if OLD_PLAN_SPEC is None or OLD_PLAN_SPEC.loader is None:
    raise RuntimeError("cannot load sibling 8 GiB serializer for mismatch regression")
OLD_PLAN_MODULE = importlib.util.module_from_spec(OLD_PLAN_SPEC)
OLD_PLAN_SPEC.loader.exec_module(OLD_PLAN_MODULE)
TEN_GIB_PLAN_PATH = Path(__file__).resolve().parents[1] / "browser_trial_10g" / "native_plan.py"
TEN_GIB_PLAN_SPEC = importlib.util.spec_from_file_location("browser_trial_10g_native_plan", TEN_GIB_PLAN_PATH)
if TEN_GIB_PLAN_SPEC is None or TEN_GIB_PLAN_SPEC.loader is None:
    raise RuntimeError("cannot load sibling 10 GiB serializer for mismatch regression")
TEN_GIB_PLAN_MODULE = importlib.util.module_from_spec(TEN_GIB_PLAN_SPEC)
TEN_GIB_PLAN_SPEC.loader.exec_module(TEN_GIB_PLAN_MODULE)


def donor_manifest(count: int = 21) -> dict:
    processes = [{"pid": 100 + i, "uid": 20020064, "start_ticks": str(7000 + i),
                  "start_ticks_after": str(7000 + i), "oom_score_adj": 280,
                  "cgroup_matches_expected_leaf": True, "verified": True} for i in range(count)]
    windows = [{"is_browser_window": True, "pid": 100, "win_id": 200 + i,
                "detail_pid": 100, "detail_win_id": 200 + i, "is_visible": False}
               for i in range(6)]
    samples = []
    anon_base = 7_530_804 * 1024
    for i, anon in enumerate((anon_base - 10 * 1024, anon_base, anon_base + 10 * 1024)):
        samples.append({"device_boottime_seconds": 100.0 + i * 30,
            "member_count": count, "member_pids": [p["pid"] for p in processes],
            "member_pids_after": [p["pid"] for p in processes], "processes": copy.deepcopy(processes),
            "browser_windows": copy.deepcopy(windows), "identity_verified": True,
            "uid": 20020064, "app_score": 120, "anon_bytes": anon})
    readiness = {key: True for key in (
        "all_browser_windows_hidden_each_sample", "identity_verified_each_sample",
        "every_browser_window_pid_is_group_member", "expected_window_count_stable",
        "member_pid_sets_equal_across_samples", "member_pid_start_sets_equal_across_samples",
        "member_pid_uid_sets_equal_across_samples", "all_group_app_scores_positive_each_sample",
        "all_group_oom_score_adj_positive_each_sample", "anon_capacity_within_two_percent_across_samples",
        "browser_primary_pid_uses_package_uid_each_sample")}
    local_inputs = [{"window_label": f"w{i // 3 + 1}", "page_label": f"w{i // 3 + 1}p{i % 3 + 1}",
        "size_bytes": 1234 + i, "sha256": f"{i + 1:064x}", "device_path": f"/offline/page{i}.html"}
        for i in range(18)]
    device_inputs = [{"device_path": row["device_path"], "sha256": row["sha256"], "verified": True}
        for row in local_inputs]
    return {"schema": "browser-donor-preparation-v1", "bundle": "com.huawei.hmos.browser",
        "uid": 20020064, "expected_windows": 6, "pages_per_window_operator_assertion": 3,
        "samples": samples, "latest_sample": copy.deepcopy(samples[-1]),
        "readiness": readiness, "eligible_for_review": True,
        "local_inputs": local_inputs, "device_inputs": device_inputs,
        "operator_assertions": {"pages_loaded_and_settled": True, "all_windows_minimized": True}}


def donor_plan(increment_mib: int, count: int = 21) -> str:
    members = ",".join(f"{100+i}:20020064:{7000+i}" for i in range(count))
    launch = 250.0
    return ("device_browser_donor_plan_v1\n"
        f"s4_0123456789ab_c0001_peak_transition|s4_0123456789ab_c0001|{launch:.9f}|{increment_mib*MIB}|1\n"
        f"com.huawei.hmos.browser|20020064|100|7000|160.000000000|{members}\n")


class NineGiBPlanFingerprintTests(unittest.TestCase):
    def test_nine_gib_native_plan_is_closed_and_sibling_workloads_are_rejected(self):
        identities = {bundle: {"uid": 10000 + i, "pid": 20000 + i, "start_ticks": 30000 + i}
                      for i, bundle in enumerate(BUNDLES)}
        valid = serialize("s4_0123456789ab_c0001",
            "12345678-1234-1234-1234-123456789abc", 12345.0, identities)
        checked = validate_serialized(valid)
        self.assertEqual(checked["fingerprint"][1:5], ("3", "23040", "1536", "9216"))
        for sibling in (OLD_PLAN_MODULE, TEN_GIB_PLAN_MODULE):
            foreign_plan = sibling.serialize("s4_0123456789ab_c0001",
                "12345678-1234-1234-1234-123456789abc", 12345.0, identities)
            with self.assertRaisesRegex(ValueError, "schema/row count mismatch|fingerprint mismatch"):
                validate_serialized(foreign_plan)

    def test_browser_event_rejects_eight_and_ten_gib_and_accepts_23040_mib(self):
        manifest = donor_manifest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "browser_plan.tsv"
            path.write_text(donor_plan(19968), encoding="ascii")
            with self.assertRaisesRegex(ValueError, "exact 23040-MiB"):
                validate_plan(path, manifest)
            path.write_text(donor_plan(26112), encoding="ascii")
            with self.assertRaisesRegex(ValueError, "exact 23040-MiB"):
                validate_plan(path, manifest)
            path.write_text(donor_plan(23040), encoding="ascii")
            result = validate_plan(path, manifest)
        self.assertEqual(result["needed_bytes"], 23040 * MIB)
        self.assertEqual(result["member_count"], 21)

    def test_dynamic_member_count_accepts_nineteen_and_binds_every_identity(self):
        manifest = donor_manifest(19)
        identities = _verified_browser_member_identities(manifest)
        self.assertEqual(len(identities), 19)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "browser_plan.tsv"
            path.write_text(donor_plan(23040, 19), encoding="ascii")
            checked = validate_plan(path, manifest)
        self.assertEqual(checked["member_count"], 19)
        self.assertEqual(len(checked["member_identities"]), 19)

    def test_real_anon_mean_is_admitted_without_old_absolute_threshold(self):
        manifest = donor_manifest(19)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "browser_manifest.json"
            path.write_text(__import__("json").dumps(manifest), encoding="utf-8")
            checked = validate_browser_manifest(path)
        self.assertEqual(checked["anon_samples"]["sample_count"], 3)
        self.assertAlmostEqual(checked["anon_samples"]["mean_bytes"], 7_530_804 * 1024, delta=1024)
        self.assertTrue(checked["anon_samples"]["within_limit"])

    def test_anon_gate_rejects_nonpositive_or_spread_over_two_percent(self):
        manifest = donor_manifest()
        manifest["samples"][1]["anon_bytes"] = 0
        with self.assertRaisesRegex(ValueError, "positive integer"):
            _anon_sample_stats(manifest)
        manifest = donor_manifest()
        manifest["samples"][2]["anon_bytes"] = int(manifest["samples"][0]["anon_bytes"] * 1.03)
        with self.assertRaisesRegex(ValueError, "exceed the 2%"):
            _anon_sample_stats(manifest)

    def test_empty_group_is_rejected(self):
        manifest = donor_manifest(19)
        for sample in manifest["samples"]:
            sample.update(member_count=0, member_pids=[], member_pids_after=[], processes=[])
        with self.assertRaisesRegex(ValueError, "empty or exceeds the service limit of 64"):
            _verified_browser_member_identities(manifest)

    def test_member_count_mismatch_is_rejected(self):
        manifest = donor_manifest(19)
        manifest["samples"][-1]["member_count"] = 18
        with self.assertRaisesRegex(ValueError, "count/PID lists"):
            _verified_browser_member_identities(manifest)

    def test_identity_drift_between_samples_is_rejected(self):
        manifest = donor_manifest(19)
        changed = manifest["samples"][-1]["processes"][3]
        changed["start_ticks"] = "999999"
        changed["start_ticks_after"] = "999999"
        with self.assertRaisesRegex(ValueError, "identities drift"):
            _verified_browser_member_identities(manifest)

    def test_service_member_limit_of_64_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "service limit of 64"):
            _verified_browser_member_identities(donor_manifest(65))

    @staticmethod
    def _valid_baseline(anon_stats: dict) -> dict:
        return {"schema": "browser_donor_trial_9g_preflight_v1",
            "variant": PROFILE["variant"], "phase": "baseline", "profile": copy.deepcopy(PROFILE),
            "device_execution": "completed", "pressure_targets": list(TARGETS),
            "native_execution": {"device_execution": "completed", "coverage_through_t_plus_930": True,
                "gate_audit": {"valid": True}},
            "service_restore": {"verified": True},
            "target_cleanup": {bundle: {"verified_absent": True} for bundle in TARGETS},
            "donor": {"bundle": "com.huawei.hmos.browser", "uid": 20020064,
                "windows": 6, "pages": 18, "anon_samples": copy.deepcopy(anon_stats)},
            "plan": {"case_id": "s4_ffffffffffff_c0001", "needed_bytes": 23040 * MIB},
            "service": {"daily_disk_mode": "owned_apply",
                "effective_browser_whole_work_cap_mib": PROFILE["load"]["disk_whole_work_cap_mib"]}}

    def test_optimized_requires_valid_completed_baseline_and_matches_anon_mean(self):
        current = _anon_sample_stats(donor_manifest())
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(__import__("json").dumps(donor_manifest()), encoding="utf-8")
            workload_fingerprint = validate_browser_manifest(manifest_path)["workload_fingerprint_sha256"]
        donor = {"bundle": "com.huawei.hmos.browser", "uid": 20020064,
            "windows": 6, "pages": 18, "anon_samples": current,
            "workload_fingerprint_sha256": workload_fingerprint}
        plan = {"case_id": "s4_0123456789ab_c0002", "needed_bytes": 23040 * MIB}
        with self.assertRaisesRegex(ValueError, "requires --baseline-reference"):
            validate_baseline_reference(None, "optimized", donor, plan)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            baseline = self._valid_baseline(current)
            baseline["donor"]["workload_fingerprint_sha256"] = workload_fingerprint
            path.write_text(__import__("json").dumps(baseline), encoding="utf-8")
            result = validate_baseline_reference(path, "optimized", donor, plan)
        self.assertTrue(result["matched"])
        self.assertEqual(result["baseline_anon_samples"], result["optimized_anon_samples"])

    def test_optimized_rejects_anon_mean_mismatch_over_two_percent(self):
        baseline_stats = _anon_sample_stats(donor_manifest())
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_path.write_text(__import__("json").dumps(donor_manifest()), encoding="utf-8")
            workload_fingerprint = validate_browser_manifest(manifest_path)["workload_fingerprint_sha256"]
        donor = {"bundle": "com.huawei.hmos.browser", "uid": 20020064,
            "windows": 6, "pages": 18, "anon_samples": copy.deepcopy(baseline_stats),
            "workload_fingerprint_sha256": workload_fingerprint}
        donor["anon_samples"]["mean_bytes"] *= 1.021
        plan = {"case_id": "s4_0123456789ab_c0002", "needed_bytes": 23040 * MIB}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            baseline = self._valid_baseline(baseline_stats)
            baseline["donor"]["workload_fingerprint_sha256"] = workload_fingerprint
            path.write_text(__import__("json").dumps(baseline), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "above 2%"):
                validate_baseline_reference(path, "optimized", donor, plan)

    def test_baseline_reference_rejects_different_page_workload(self):
        baseline_stats = _anon_sample_stats(donor_manifest())
        donor = {"bundle": "com.huawei.hmos.browser", "uid": 20020064,
            "windows": 6, "pages": 18, "anon_samples": baseline_stats,
            "workload_fingerprint_sha256": "a" * 64}
        plan = {"case_id": "s4_0123456789ab_c0002", "needed_bytes": 23040 * MIB}
        baseline = self._valid_baseline(baseline_stats)
        baseline["donor"]["workload_fingerprint_sha256"] = "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            path.write_text(__import__("json").dumps(baseline), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "same donor/9 GiB workload"):
                validate_baseline_reference(path, "optimized", donor, plan)

    def test_gate_interpretation_never_claims_success_when_invalid(self):
        text = gate_interpretation(False, "GATE_INVALID|10.0|readonly_state_lost\n")
        self.assertIn("not verified", text)
        self.assertIn("readonly_state_lost", text)
        self.assertNotIn("no new disk swapout", text)
        self.assertIn("no new disk swapout", gate_interpretation(True, "GATE_RESULT|valid=1"))


if __name__ == "__main__":
    unittest.main()
