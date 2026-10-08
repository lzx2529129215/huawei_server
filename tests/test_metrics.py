import json
import shutil
import tempfile
import unittest
from pathlib import Path
import hashlib
from unittest.mock import patch
from memscenes.device import Device
from memscenes.model import (EventBook, GIB, combination_checks, fault_delta,
                             key_values, parse_stat, parse_proc_identity, proc_identity_mismatch,
                             disk_bytes, release_precedes_reuse,
                             daily_promotion_targets, attempt_outcome, attempts_complete)
from memscenes.runner import (compare, process_window_diagnostics, qualification_gate, freeze,
                              validate_frozen_scope, target_child_counter_gaps, Session,
                              parse_low_memory_missions, native_low_memory_popup_candidate,
                              test_popup_is_unoccluded, visible_native_low_memory_modal,
                              native_low_memory_close_target,
                              popup_attribution_in_window, daily_stage_complete, cases_hash,
                              case_definitions, calibration_profiles,
                              _scene4_confirm_unattributed_kernel_victims,
                              _scene4_append_jsonl, _scene4_frame_remote_path,
                              _scene4_observation_boottime_interval)
from memscenes.model import canonical_hash

class ProcessStatParsing(unittest.TestCase):
    def test_scene4_samples_preserve_unknowns_and_frame_paths_are_slot_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c0001.jsonl"
            sample = {"uptime_seconds": 12.345678901, "phase": "hold",
                      "system": {"mem_available_kb": 99, "zram_mm_stat_raw": None},
                      "daemon": {"cpu_ticks": 120}}
            _scene4_append_jsonl(path, sample)
            loaded = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(loaded, sample)
            self.assertIsNone(loaded["system"]["zram_mm_stat_raw"])
        first = _scene4_frame_remote_path("/data/local/tmp/run", "session-A", "c0001", 0)
        second = _scene4_frame_remote_path("/data/local/tmp/run", "session-A", "c0001", 1)
        other_run = _scene4_frame_remote_path("/data/local/tmp/run", "session-B", "c0001", 0)
        self.assertEqual(len({first, second, other_run}), 3)
        with self.assertRaises(ValueError):
            _scene4_frame_remote_path("/tmp", "s", "c1", -1)

    def test_scene4_proc_observation_uses_conservative_clock_and_read_brackets(self):
        clock = {"boottime_s": 100.0}
        interval = _scene4_observation_boottime_interval(clock, 1_000_000_000,
            1_100_000_000, 2_000_000_000, 2_200_000_000)
        self.assertTrue(interval["valid"])
        self.assertEqual(interval["boottime_start_s"], 100.9)
        self.assertEqual(interval["boottime_end_s"], 101.2)
        self.assertFalse(_scene4_observation_boottime_interval(
            {"boottime_s": float("nan")}, 1, 2, 3, 4)["valid"])

    def test_comm_with_spaces_and_parentheses(self):
        fields = ["S"] + ["0"] * 23
        fields[7], fields[9], fields[19] = "123", "9", "456"
        row = parse_stat("42 (browser (worker)) " + " ".join(fields))
        self.assertEqual((row["pid"], row["minor"], row["major"], row["start_ticks"]),
                         (42, 123, 9, 456))

    def test_direct_proc_stat_status_identity_checks_pid_uid_and_birth(self):
        fields = ["S"] + ["0"] * 23
        fields[19] = "9876"
        identity = parse_proc_identity("61699 (memscene worker) " + " ".join(fields),
                                       "Name:\tmemscene\nUid:\t20020185 20020185 20020185 20020185\n")
        self.assertEqual((identity["pid"], identity["uid"], identity["start_ticks"]),
                         (61699, 20020185, 9876))
        self.assertIsNone(proc_identity_mismatch(identity, 61699, 20020185, "9876"))
        self.assertEqual(proc_identity_mismatch(identity, 61699, 20020186), "direct_proc_uid_mismatch")
        self.assertEqual(proc_identity_mismatch(identity, 61699, 20020185, "9877"),
                         "direct_proc_start_ticks_mismatch")

    def test_direct_proc_device_readback_preserves_raw_response(self):
        fields = ["S"] + ["0"] * 23
        fields[19] = "9876"
        response = ("__MEMSCENE_UPTIME__\n123.456 987.000\n__MEMSCENE_STAT__\n"
                    "61699 (memscene worker) " + " ".join(fields) +
                    "\n__MEMSCENE_STATUS__\nName:\tmemscene\nUid:\t20020185 20020185 20020185 20020185\n")
        device = object.__new__(Device)
        device.shell = lambda command, **kwargs: response
        observed = device.inspect_process_identity(61699, timeout=20)
        self.assertEqual(observed["raw"], response)
        self.assertEqual(observed["uptime_seconds"], 123.456)
        self.assertEqual((observed["identity"]["pid"], observed["identity"]["uid"]),
                         (61699, 20020185))


class NativePopupEvidence(unittest.TestCase):
    def setUp(self):
        self.nodes = [{"__ownerBundleName": "com.huawei.hmos.taskmanager",
                       "hostWindowId": "77", "text": "系统内存不足"}]
        self.mission = [{"mission_id": "76", "bundle": "com.huawei.hmos.taskmanager",
                         "ability": "LowMemoryAbility", "state": "FOREGROUND",
                         "start_uptime_seconds": 101.0}]

    def test_new_taskmanager_window_within_case_is_candidate(self):
        result = native_low_memory_popup_candidate(self.nodes, self.mission, {"75"}, 100.0, 105.0, 106.0)
        self.assertEqual(result["owner"], "com.huawei.hmos.taskmanager")
        self.assertEqual(result["window_ids"], ["77"])
        self.assertEqual(result["mission_id"], "76")

    def test_preexisting_mission_is_excluded(self):
        self.assertIsNone(native_low_memory_popup_candidate(self.nodes, self.mission, {"76"}, 100.0, 105.0))

    def test_case_outside_mission_or_observation_window_is_excluded(self):
        self.assertIsNone(native_low_memory_popup_candidate(self.nodes, self.mission, set(), 102.0, 105.0))
        self.assertIsNone(native_low_memory_popup_candidate(self.nodes, self.mission, set(), 100.0, 105.0, 104.0))

    def test_mission_parser_extracts_system_owner_and_timing(self):
        raw = "Mission ID #76\n  bundle name [com.huawei.hmos.taskmanager]\n  main name [LowMemoryAbility]\n  state #FOREGROUND\n  start time [101000]\n"
        self.assertEqual(parse_low_memory_missions(raw), self.mission)

    def test_same_native_mission_window_deduplicates_across_observations(self):
        book = EventBook()
        for _ in range(2):
            book.add("low_memory_popup_native", "case", "com.huawei.hmos.taskmanager", None,
                     105000, "system_ui_and_mission", "visible system dialog", "screen.png",
                     "case:popup:native:76:77")
        self.assertEqual(book.counts()["low_memory_popup_native"], 1)

    def test_test_popup_is_not_visible_when_system_modal_owns_foreground(self):
        bundle = "io.memscene.workload.a2"
        row = {"__ownerBundleName": bundle, "text": "case: s2_case bundle: io.memscene.workload.a2"}
        self.assertTrue(test_popup_is_unoccluded(bundle, "s2_case", [row], {bundle}))
        self.assertFalse(test_popup_is_unoccluded(bundle, "s2_case", [row],
                                                  {"com.huawei.hmos.taskmanager"}))

    def test_taskmanager_modal_blocks_test_prompt_even_if_app_root_reports_focus(self):
        bundle = "io.memscene.workload.a1"
        behind_modal = {"__ownerBundleName": bundle,
                        "text": "case42 io.memscene.workload.a1 系统内存不足"}
        modal = {"__ownerBundleName": "com.huawei.hmos.taskmanager",
                 "visible": True, "text": "系统内存不足"}
        self.assertTrue(visible_native_low_memory_modal([modal], ["内存不足"]))
        self.assertFalse(test_popup_is_unoccluded(
            bundle, "case42", [behind_modal], {bundle},
            visible_native_low_memory_modal([modal], ["内存不足"])))

    def test_post_end_or_unbounded_snapshot_never_attributes_test_popup(self):
        self.assertTrue(popup_attribution_in_window(True, 1500, 1000, 1600))
        self.assertFalse(popup_attribution_in_window(False, 1655, 1000, 1600))
        self.assertFalse(popup_attribution_in_window(True, 1655, 1000, 1600))
        self.assertFalse(popup_attribution_in_window(True, None, 1000, 1600))

    def test_restore_target_requires_one_dynamic_header_glyph_and_ignores_app_close(self):
        owner = "com.huawei.hmos.taskmanager"
        nodes = [
            {"__ownerBundleName": owner, "abilityName": "", "type": "root",
             "visible": "true", "hostWindowId": "77", "bounds": "[1104,446][2016,1510]"},
            {"__ownerBundleName": owner, "type": "Text", "visible": "true",
             "hostWindowId": "77", "text": "系统内存不足"},
            {"__ownerBundleName": owner, "type": "SymbolGlyph", "visible": "true",
             "enabled": "true", "clickable": "true", "hostWindowId": "77",
             "bounds": "[1910,476][1986,552]", "accessibilityId": "19"},
            {"__ownerBundleName": owner, "type": "Button", "visible": "true",
             "enabled": "false", "clickable": "false", "hostWindowId": "77",
             "bounds": "[1700,1300][1900,1400]", "id": "appCloseBtn"},
        ]
        target = native_low_memory_close_target(nodes, ["内存不足"])
        self.assertEqual((target["x"], target["y"], target["hostWindowId"]), (1948, 514, "77"))
        nodes.append({**nodes[2], "bounds": "[1900,476][1976,552]"})
        self.assertIsNone(native_low_memory_close_target(nodes, ["内存不足"]))

    def test_daily_stage_remains_complete_when_later_peak_is_unknown(self):
        targets = ["io.memscene.workload.a1", "io.memscene.workload.a2"]
        daily = {bundle: [{"event": "daily_ready"}] for bundle in targets}
        # Peak journals are deliberately not inputs: a later allocation timeout
        # must not rewrite whether the all-app daily barrier was achieved.
        self.assertTrue(daily_stage_complete(targets, daily))
        self.assertFalse(daily_stage_complete(targets, {targets[0]: daily[targets[0]]}))


class QualificationCaseProfiles(unittest.TestCase):
    def config(self):
        return {"dram_gib": 32, "test_apps": ["a1", "a2"], "scene2": {
            "daily_mib": 256, "peak_mib": 8192, "hold_seconds": 10,
            "case_cycles": 1, "seed": 17,
            "calibration_peak_mib_levels": [5120, 12288],
            "case_repetitions": {"popup": 2, "startup": 1, "oom": 9},
            "case_profiles": {
                "popup": {"peak_mib": 5120, "hold_seconds": 60},
                "startup": {"peak_mib": 12288, "hold_seconds": 0},
                "oom": {"peak_mib": 12288, "hold_seconds": 90}}}}

    def test_fixed_suite_uses_explicit_profiles_and_repetitions(self):
        cases = case_definitions(self.config())
        self.assertEqual(len(cases), 12)
        self.assertEqual({kind: sum(row["kind"] == kind for row in cases)
                          for kind in ("popup", "startup", "oom")},
                         {"popup": 2, "startup": 1, "oom": 9})
        self.assertTrue(all(row["peak_mib"] == 5120 and row["hold_seconds"] == 60
                            for row in cases if row["kind"] == "popup"))
        self.assertTrue(all(row["peak_mib"] == 12288 and row["hold_seconds"] == 0
                            for row in cases if row["kind"] == "startup"))
        self.assertTrue(all(row["peak_mib"] == 12288 and row["hold_seconds"] == 90
                            for row in cases if row["kind"] == "oom"))

    def test_calibration_uses_each_actual_suite_peak_once(self):
        profiles = calibration_profiles(self.config())
        self.assertEqual(profiles, [
            {"peak_mib": 5120, "hold_seconds": 60, "kinds": ["popup"]},
            {"peak_mib": 12288, "hold_seconds": 90, "kinds": ["startup", "oom"]}])


class ProcessCounters(unittest.TestCase):
    def test_child_faults_not_double_counted(self):
        old = {"42:1":{"minor":100,"major":5}}
        new = {"42:1":{"minor":110,"major":7}, "43:2":{"minor":3,"major":1}}
        result = fault_delta(old, new)
        self.assertEqual(result["total"], 16)
        self.assertTrue(result["valid"])

    def test_exit_and_counter_regression_reject_round(self):
        self.assertFalse(fault_delta({"1:1":{"minor":10,"major":2}}, {})["valid"])
        self.assertFalse(fault_delta({"1:1":{"minor":10,"major":2}},
                                    {"1:1":{"minor":9,"major":2}})["valid"])

    def test_units_and_disk_evidence(self):
        self.assertEqual(key_values("MemTotal: 123 kB\nEswap: 4 MB")["MemTotal"], 123 * 1024)
        self.assertEqual(disk_bytes("write_total_size: 456"), 456)
        self.assertIsNone(disk_bytes("Function not implemented"))

    def test_launch_window_counters_are_lower_bounds_without_lifecycle_coverage(self):
        app="com.example.app"
        process={"pid":17,"start_ticks":200,"minor":100,"major":4}
        samples=[{"device_uptime_seconds":1,"apps":{app:{"processes":{"17:200":process}}}},
                 {"device_uptime_seconds":2,"apps":{app:{"processes":{"17:200":{**process,"minor":125,"major":6}}}}}]
        report=process_window_diagnostics(samples,[app],{"apps":{app:{"processes":{}}}},
            {"apps":{app:{"processes":{"17:200":{**process,"minor":130,"major":7}}}}})[app]
        self.assertFalse(report["complete"])
        self.assertEqual((report["minor_lower_bound"],report["major_lower_bound"]),(25,2))
        self.assertEqual(report["coverage"],"partial_lower_bound_since_first_sample")

    def test_qualification_uses_category_sum_and_separate_incident_count(self):
        cases=[{"kind":"popup","bundles":["a1"],"seed":5}]
        config={"dram_gib":16,"scene2":{"baseline_min_events":100}}
        book=EventBook()
        for index in range(50):
            incident=f"case:{index}"
            book.add("oom_kill","case","app",index,1000,"kernel","OOM","raw",incident)
            book.add("startup_failure","case","app",index,1000,"journal","ENOMEM","raw",incident)
        book.add("low_memory_popup_test","case","app",0,1000,"ui","popup","image","case:0")
        counts=book.counts()
        self.assertEqual((counts["total_events"],counts["unique_incidents"]),(101,50))
        valid={"scene":2,"phase":"baseline","profile":"qualification",
               "environment":{"meminfo":{"MemTotal":15*GIB}},"execution_complete":True,
               "measurement_complete":True,"cases_hash":canonical_hash(cases),
               "case_results":[{"daily_stage_complete":True}],
               "counts":counts}
        self.assertEqual(qualification_gate(valid,config,cases),[])
        below={**valid,"counts":{**valid["counts"],"total_events":99}}
        self.assertIn("baseline_below_minimum_category_events",qualification_gate(below,config,cases))

class EvidenceCounting(unittest.TestCase):
    def test_js_crash_and_manual_kill_are_not_oom(self):
        book = EventBook()
        rows = [{"name_":"PROCESS_KILL","BUNDLE_NAME":"app","PID":5,"TIMESTAMP":1000,
                 "REASON":reason} for reason in ("Js Error", "User request", "SIGKILL")]
        book.system_kills(rows,"case",{"app":100},900,1100,"logs")
        self.assertEqual(book.counts()["oom_kill"], 0)

    def test_policy_kill_is_separate_and_historical_events_excluded(self):
        book = EventBook()
        rows = [{"name_":"PROCESS_KILL","BUNDLE_NAME":"app","PID":5,"UID":100,"TIMESTAMP":1000,
                 "REASON":"LowMemory"},
                {"name_":"PROCESS_KILL","BUNDLE_NAME":"app","PID":6,"UID":100,"TIMESTAMP":800,
                 "REASON":"OOM"},
                {"name_":"PROCESS_KILL","BUNDLE_NAME":"unrelated","PID":7,"UID":200,"TIMESTAMP":1000,
                 "REASON":"OOM"}]
        observed = {5:[{"bundle":"app","uid":100,"start_ticks":20,"first_uptime":9.0,
                        "last_seen_uptime":10.0,"first_absent_uptime":10.2}]}
        book.system_kills(rows,"case",{"app":100},900,1100,"logs",observed,10.0,10.2,900,10.0)
        self.assertEqual(book.counts()["memory_policy_kill"], 1)
        self.assertEqual(book.counts()["total_events"], 0)

    def test_dedup_sources_and_cross_category_incident(self):
        book = EventBook()
        for source in ("hisysevent","kernel"):
            book.add("oom_kill","case","app",5,1000,source,"OOM",source,"case:5")
        book.add("startup_failure","case","app",5,1000,"monitor","OOM","state","case:5")
        self.assertEqual(book.counts()["total_events"], 2)
        self.assertEqual(book.counts()["unique_incidents"], 1)
        self.assertEqual(len(book.export()[0]["evidence"]), 2)

    def test_only_enomem_allocation_failures_count(self):
        book = EventBook()
        rows = [{"event":"allocation_failed","errno":number,"case_id":"case","pid":number,"time_ms":1000}
                for number in (12, 28, 13)]
        book.native_failures(rows,"app","journal")
        self.assertEqual(book.counts()["startup_failure"], 1)

    def test_kernel_oom_requires_case_pid_lifetime_match(self):
        book = EventBook()
        trace = ("worker-1-1 [001] .... [ 12.000] oom:mark_victim: pid=44 uid=2000\n"
                 "worker-1-1 [001] .... [ 99.000] oom:mark_victim: pid=44 uid=2000\n"
                 "worker-1-1 [001] .... [ 12.500] oom:mark_victim: pid=45 uid=2000\n")
        observed = {44: [{"bundle":"app", "uid":2000, "start_ticks":900,
                          "first_uptime":11.0,"last_seen_uptime":12.0,
                          "first_absent_uptime":18.0}]}
        book.kernel_oom_victims(trace,"case",observed,10.0,20.0,"trace")
        self.assertEqual(book.counts()["oom_kill"], 1)
        self.assertEqual(book.export()[0]["bundle"], "app")
        self.assertEqual(book.export()[0]["incident"], "case:44:900")

    def test_kernel_oom_pid_reuse_uses_first_absent_bound(self):
        book = EventBook()
        trace = ("worker-1-1 [001] .... [ 12.000] oom:mark_victim: pid=44 uid=2000\n"
                 "worker-1-1 [001] .... [ 17.000] oom:mark_victim: pid=44 uid=2000\n")
        observed = {44:[
            {"bundle":"app1","uid":2000,"start_ticks":100,"first_uptime":10.0,
             "last_seen_uptime":11.0,"first_absent_uptime":15.0},
            {"bundle":"app2","uid":2001,"start_ticks":200,"first_uptime":16.0,
             "last_seen_uptime":18.0,"first_absent_uptime":None}]}
        book.kernel_oom_victims(trace,"case",observed,10.0,20.0,"trace")
        self.assertEqual(book.counts()["oom_kill"], 2)
        self.assertEqual([row["bundle"] for row in book.export()], ["app1", "app2"])
        self.assertEqual([row["incident"] for row in book.export()], ["case:44:100", "case:44:200"])

    def test_dmesg_requires_marker_window_exact_target_victim_and_exit(self):
        book = EventBook()
        raw = "\n".join([
            "[10.000000] kernel: MEMSCENES_CASE_BEGIN session=s case_id=case uptime_ms=10000 wall_ms=100000",
            "[12.000000] kernel: LOWMEM REASON=OOM_KILLER PID=101",
            "[13.000000] kernel: foundation send SIG[9] to ene.workload.a1 pid=101",
            "[13.100000] kernel: [mem_oom:458] xreclaim reaper: start when kill process, pid=101",
            "[14.000000] kernel: [mem_oom:522] out of Memory(OOM), start to kill process < ene.workload.a1> (pid: 101, rss 99, drv 0, adj 0)",
            "[14.010000] kernel: [mem_oom:999] out of Memory(OOM), start to kill process < ene.workload.a1> (pid: 101, rss 99, drv 0, adj 0)",
            "[14.100000] kernel: process has exited, pid=101",
            "[15.000000] kernel: [mem_oom:522] out of Memory(OOM), start to kill process < ene.workload.a2> (pid: 202, rss 99, drv 0, adj 0)",
            "[15.100000] kernel: process has exited, pid=202",
            "[20.000000] kernel: MEMSCENES_CASE_END session=s case_id=case uptime_ms=20000 wall_ms=110000",
        ])
        observed = {101:[{"bundle":"io.memscene.workload.a1","uid":1001,"start_ticks":"42",
                           "first_uptime":9.0,"first_absent_uptime":16.0}]}
        info = book.kernel_dmesg_oom_victims(raw,"case",observed,
            {"io.memscene.workload.a1":1001,"io.memscene.workload.a2":1002},
            {"io.memscene.workload.a1":[{"event":"peak_begin","pid":101}]},
            10.0,20.0,100000,10.0,"kernel_dmesg.raw")
        self.assertTrue(info["coverage_complete"])
        self.assertEqual(info["victims_detected"],1)
        self.assertEqual(book.counts()["oom_kill"],1)
        self.assertEqual(book.export()[0]["incident"],"case:101:42")

    def test_dmesg_missing_end_marker_and_incomplete_kill_do_not_count(self):
        book = EventBook()
        raw = "\n".join([
            "[10.000000] MEMSCENES_CASE_BEGIN session=s case_id=case uptime_ms=10000 wall_ms=100000",
            "[14.000000] [mem_oom:522] out of Memory(OOM), start to kill process < ene.workload.a1> (pid: 101, rss 99, drv 0, adj 0)",
        ])
        observed = {101:[{"bundle":"io.memscene.workload.a1","uid":1001,"start_ticks":"42",
                           "first_uptime":9.0,"first_absent_uptime":16.0}]}
        info = book.kernel_dmesg_oom_victims(raw,"case",observed,{"io.memscene.workload.a1":1001},
            {"io.memscene.workload.a1":[{"event":"peak_begin","pid":101}]},10.0,20.0,100000,10.0,"raw")
        self.assertFalse(info["coverage_complete"])
        self.assertEqual(book.counts()["oom_kill"],0)

    def test_scene4_delayed_exit_requires_same_generation_absence_bracket(self):
        case = "c1"
        bundle = "io.memscene.scene4.workload.a8"
        rows = [
            {"event": "peak_begin", "case_id": case, "pid": 101, "uid": 2008,
             "start_ticks": "900", "boottime_ns": 12_000_000_000},
            {"event": "cold_start_ready", "case_id": case, "pid": 101, "uid": 2008,
             "start_ticks": "900", "boottime_ns": 13_000_000_000},
        ]
        raw = "\n".join([
            "[10.000000] MEMSCENES_CASE_BEGIN session=s case_id=c1 uptime_ms=10000 wall_ms=100000",
            "[15.000000] [mem_oom:522] out of Memory(OOM), start to kill process <ne4.workload.a8> (pid: 101, rss 1, drv 0, adj 0)",
            "[25.000000] process has exited, pid=101",
            "[30.000000] MEMSCENES_CASE_END session=s case_id=c1 uptime_ms=30000 wall_ms=120000",
        ])
        observed = {101: [{"bundle": bundle, "uid": 2008, "start_ticks": "900",
            "first_uptime": 11.0, "last_seen_uptime": 24.5, "first_absent_uptime": 26.0,
            "last_seen_interval_start_s": 24.0, "last_seen_interval_end_s": 24.5,
            "first_absent_interval_start_s": 25.0, "first_absent_interval_end_s": 26.0}]}
        book = EventBook()
        info = book.kernel_dmesg_oom_victims(raw, case, observed, {bundle: 2008}, {bundle: rows},
            10.0, 30.0, 100000, 10.0, "raw", scene4_cold_only=True,
            journal_readback_complete_bundles={bundle}, journal_case_ids={bundle: case},
            startup_failure_bundles={bundle})
        self.assertTrue(info["coverage_complete"])
        self.assertEqual(info["victims_detected"], 1)
        victim = info["victims"][0]
        self.assertEqual(victim["readiness_relation"], "post_ready")
        self.assertFalse(victim["startup_failure_eligible"])
        self.assertEqual(victim["completion_evidence"], "unique_journal_generation_and_process_absence_bracket")
        self.assertEqual(book.counts()["oom_kill"], 1)

    def test_scene4_delayed_exit_rejects_generation_conflict_and_pre_ready_is_failure(self):
        case = "c1"
        bundle = "io.memscene.scene4.workload.a8"
        raw = "\n".join([
            "[10.000000] MEMSCENES_CASE_BEGIN session=s case_id=c1 uptime_ms=10000 wall_ms=100000",
            "[15.000000] [mem_oom:522] out of Memory(OOM), start to kill process <ne4.workload.a8> (pid: 101, rss 1, drv 0, adj 0)",
            "[25.000000] process has exited, pid=101",
            "[30.000000] MEMSCENES_CASE_END session=s case_id=c1 uptime_ms=30000 wall_ms=120000",
        ])
        peak = {"event": "peak_begin", "case_id": case, "pid": 101, "uid": 2008,
                "start_ticks": "900", "boottime_ns": 12_000_000_000}
        observed_conflict = {101: [{"bundle": bundle, "uid": 2008, "start_ticks": "901",
            "first_uptime": 11.0, "last_seen_uptime": 24.5, "first_absent_uptime": 26.0,
            "last_seen_interval_start_s": 24.0, "last_seen_interval_end_s": 24.5,
            "first_absent_interval_start_s": 25.0, "first_absent_interval_end_s": 26.0}]}
        book = EventBook()
        info = book.kernel_dmesg_oom_victims(raw, case, observed_conflict, {bundle: 2008},
            {bundle: [peak]}, 10.0, 30.0, 100000, 10.0, "raw", scene4_cold_only=True,
            journal_readback_complete_bundles={bundle}, journal_case_ids={bundle: case},
            startup_failure_bundles={bundle})
        self.assertEqual(info["victims_detected"], 0)

        rows = [peak]
        observed = {101: [{"bundle": bundle, "uid": 2008, "start_ticks": "900",
            "first_uptime": 11.0, "last_seen_uptime": 24.5, "first_absent_uptime": 26.0,
            "last_seen_interval_start_s": 24.0, "last_seen_interval_end_s": 24.5,
            "first_absent_interval_start_s": 25.0, "first_absent_interval_end_s": 26.0}]}
        book = EventBook()
        info = book.kernel_dmesg_oom_victims(raw, case, observed, {bundle: 2008},
            {bundle: rows}, 10.0, 30.0, 100000, 10.0, "raw", scene4_cold_only=True,
            journal_readback_complete_bundles={bundle}, journal_case_ids={bundle: case},
            startup_failure_bundles={bundle})
        self.assertEqual(info["victims_detected"], 1)
        self.assertEqual(info["victims"][0]["readiness_relation"], "pre_ready")
        self.assertTrue(info["victims"][0]["startup_failure_eligible"])

    def test_unattributed_duplicate_selection_pairs_count_one_terminal_death(self):
        raw = [
            "[10.000000] [mem_oom:522] out of Memory(OOM), start to kill process <system.proc> (pid: 77, rss 1, drv 0, adj 0)",
            "[10.500000] [mem_oom:523] out of Memory(OOM), start to kill process <system.proc> (pid: 77, rss 1, drv 0, adj 0)",
            "[11.000000] process has exited, pid=77",
        ]
        confirmed, unknown = _scene4_confirm_unattributed_kernel_victims(raw, 9.0, 12.0, 0.0, [])
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["incident_id"], "unattributed:77:11.000000")
        self.assertEqual(len(confirmed[0]["duplicate_victim_lines"]), 1)
        self.assertEqual(unknown, [])

    def test_late_kernel_oom_links_unready_peak_attempt_without_timeout(self):
        book=EventBook(); case="c1"; bundle="io.memscene.workload.a3"
        rows=[{"event":"peak_begin","case_id":case,"pid":42,"uid":1001},
              {"event":"allocation_progress","case_id":case,"pid":42,"uid":1001}]
        observed={42:[{"bundle":bundle,"uid":1001,"start_ticks":"900",
                        "first_uptime":1.0,"first_absent_uptime":4.0}]}
        # This event is added by the later kernel collector, before final classification.
        book.add("oom_kill",case,bundle,42,2500,"kernel:dmesg:mem_oom","confirmed victim",
                 "victim+exit raw",f"{case}:42:900")
        outcome=book.finalize_startup_attempt(case,bundle,rows,observed,5000,"journal")
        counts=book.counts()
        self.assertEqual(outcome,"confirmed_memory_failure")
        self.assertEqual((counts["oom_kill"],counts["startup_failure"],counts["startup_timeout"]),(1,1,0))
        self.assertEqual((counts["total_events"],counts["unique_incidents"]),(2,1))
        events=book.export()
        startup=next(row for row in events if row["category"]=="startup_failure")
        self.assertEqual(startup["incident"],f"{case}:42:900")

    def test_pre_promote_abort_is_not_a_startup_timeout_or_memory_failure(self):
        book=EventBook(); case="barrier"; bundle="io.memscene.workload.a6"
        outcome=book.finalize_startup_attempt(case,bundle,[],{},5000,"no peak was issued",
                                              allow_timeout=False)
        self.assertEqual(outcome,"aborted_before_peak")
        self.assertEqual(book.counts()["startup_timeout"],0)
        self.assertEqual(book.counts()["startup_failure"],0)
        self.assertEqual(book.counts()["oom_kill"],0)
        self.assertFalse(attempts_complete([outcome],False))

    def test_kernel_oom_after_startup_ready_is_not_startup_failure(self):
        book=EventBook(); case="c2"; bundle="io.memscene.workload.a4"
        book.add("oom_kill",case,bundle,43,4000,"kernel:dmesg:mem_oom","confirmed victim",
                 "victim+exit raw",f"{case}:43:901")
        rows=[{"event":"peak_begin","case_id":case,"pid":43,"uid":1002},
              {"event":"startup_ready","case_id":case,"pid":43,"uid":1002}]
        outcome=book.finalize_startup_attempt(case,bundle,rows,
            {43:[{"bundle":bundle,"uid":1002,"start_ticks":"901"}]},5000,"journal")
        self.assertEqual(outcome,"ready")
        self.assertEqual(book.counts()["startup_failure"],0)
        self.assertEqual(book.counts()["startup_timeout"],0)
        self.assertEqual(book.counts()["oom_kill"],1)

    def test_terminal_evidence_accepts_success_and_confirmed_failure_only(self):
        self.assertEqual(attempt_outcome(True,True,True,False),"success")
        self.assertEqual(attempt_outcome(True,False,False,True),"confirmed_memory_failure")
        self.assertEqual(attempt_outcome(False,False,False,False),"unconfirmed")
        self.assertTrue(attempts_complete(["success","confirmed_memory_failure"],True))
        self.assertTrue(attempts_complete(["confirmed_memory_failure"],False))
        self.assertFalse(attempts_complete(["success","unconfirmed"],True))
        self.assertFalse(attempts_complete([],True))

    def test_pressure_must_be_released_before_reuse_baseline(self):
        ordered = [{"name":"pressure_released","device_uptime_seconds":4.0},
                   {"name":"reuse_begin","device_uptime_seconds":5.0}]
        self.assertTrue(release_precedes_reuse(ordered))
        self.assertFalse(release_precedes_reuse([
            {"name":"pressure_released","device_uptime_seconds":6.0},
            {"name":"reuse_begin","device_uptime_seconds":5.0}]))
        self.assertFalse(release_precedes_reuse([
            {"name":"pressure_released","device_uptime_seconds":4.0}]))

    def test_peak_barrier_waits_for_all_daily_apps_and_aborts_on_failure(self):
        targets=["a1","a2","a3"]
        self.assertEqual(daily_promotion_targets(targets,{"a1","a2"},set()),[])
        self.assertEqual(daily_promotion_targets(targets,{"a1","a2","a3"},set()),targets)
        self.assertEqual(daily_promotion_targets(targets,{"a1","a3"},{"a2"}),[])

class Acceptance(unittest.TestCase):
    def test_freeze_bootstraps_from_measured_qualification_without_formal_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            cases=[{"kind":"popup","bundles":["a1"],"seed":5}]
            env={"target":"device","meminfo":{"MemTotal":15*GIB}}
            run={"scene":2,"phase":"baseline","profile":"qualification",
                 "execution_complete":True,"measurement_complete":True,
                 "acceptance_valid":False,"qualification_eligible":True,
                 "qualification_scope":"formal_16gb","suite_hash":"same",
                 "suite":{"dram_gib":16},"environment":env,"cases":cases,
                 "cases_hash":canonical_hash(cases),
                 "counts":{"startup_failure":50,"oom_kill":50,"low_memory_popup":1,
                           "total_events":101,"unique_incidents":50}}
            calibration={"suite_hash":"same","profile":"qualification",
                         "checks":{"valid":True},"environment":env}
            run_path=p/"run.json"; run_path.write_text(json.dumps(run))
            cal_path=p/"cal.json"; cal_path.write_text(json.dumps(calibration))
            output=p/"frozen.json"
            freeze(run_path,cal_path,output)
            frozen=json.loads(output.read_text())
            self.assertEqual(frozen["qualification_scope"],"formal_16gb")
            self.assertEqual(frozen["dram_gib"],16)
            with self.assertRaises(RuntimeError):
                validate_frozen_scope({"qualification_scope":"prevalidation_32gb","dram_gib":32},
                                      {"dram_gib":16},"acceptance")

    def test_peak_daily_constraints_require_real_measurements(self):
        app = {"bundle":"app","measured":True,"ready_observed":True,"peak_observed":True,
               "daily_bytes":GIB,"peak_bytes":4*GIB}
        self.assertTrue(combination_checks([app] * 5,16*GIB)["valid"])
        self.assertFalse(combination_checks([{**app,"measured":False}] * 5,16*GIB)["valid"])
        self.assertFalse(combination_checks([app] * 4,16*GIB)["valid"])

    def test_lifecycle_trace_records_zero_fork_as_unknown_not_complete(self):
        class FakeDevice:
            command_timeout=45
            def __init__(self):
                self.enabled={"sched_process_fork":"0","sched_process_exit":"0"}
                self.tracing="0"
                self.instance_exists=False
                self.raw=""
            def shell(self, command, **kwargs):
                if command.startswith("mkdir "):
                    self.instance_exists=True
                    return ""
                if "test -d " in command:
                    return "exists" if self.instance_exists else ""
                if command.startswith("cat /proc/uptime"):
                    return "123.0 0.0"
                if "events/sched/" in command and "/enable" in command:
                    event=command.split("events/sched/",1)[1].split("/enable",1)[0]
                    if command.startswith("cat "):
                        return self.enabled[event]
                    value=command.split("echo ",1)[1].split(" >",1)[0]
                    self.enabled[event]=value
                    return ""
                if command.endswith("/tracing_on") and command.startswith("cat "):
                    return self.tracing
                if command.startswith("echo 1 > ") and command.endswith("/tracing_on"):
                    self.tracing="1"
                    return ""
                if command.startswith("echo 0 > ") and command.endswith("/tracing_on"):
                    self.tracing="0"
                    return ""
                if " | grep -E 'sched_process_(fork|exit):'" in command:
                    return ""
                if command.startswith("rmdir "):
                    self.instance_exists=False
                    return ""
                return ""
            def receive(self, remote, local):
                Path(local).write_text(self.raw,encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            # Session's evidence directory is redirected to the temporary tree.
            device=FakeDevice()
            session=Session(device,{"schema_version":1,"version_label":"test",
                "dram_gib":16,"test_apps":[],"real_apps":{},"popup_text_patterns":{},
                "scene1":{},"scene2":{}},1,"baseline","smoke")
            device.raw=(f"worker-1 (1) [000] .... 10.000000: tracing_mark_write: B|0|memscenes/{session.id}_reuse_begin\n"
                        f"worker-1 (1) [000] .... 10.100000: tracing_mark_write: B|0|memscenes/{session.id}_reuse_end\n")
            generated_output=session.output
            session.output=Path(directory)
            shutil.rmtree(generated_output)
            session.trace_started=True
            session.start_process_lifecycle_trace()
            session.mark("reuse_begin")
            session.mark("reuse_end")
            session.stop_process_lifecycle_trace()
            trace_path=Path(directory)/"trace.ftrace"
            trace_path.write_text(device.raw,encoding="utf-8")
            session._parse_process_lifecycle_raw(device.raw,trace_path)
            lifecycle=session.result["process_lifecycle_trace"]
            self.assertTrue(lifecycle["available"])
            self.assertEqual(lifecycle["coverage"],"enabled_window_collected")
            self.assertEqual((lifecycle["fork_event_rows"],lifecycle["exit_event_rows"]),(0,0))
            self.assertEqual(lifecycle["fork_event_observation"],"unknown_no_fork_row_observed")
            self.assertTrue(lifecycle["instance_removed"])
            self.assertFalse(session.result["measurement_evidence_complete"])

    def test_target_child_sampled_midwindow_but_absent_at_ending_is_a_count_gap(self):
        bundle="app.meet"
        baseline={"apps":{bundle:{"processes":{"10:100":
            {"pid":10,"start_ticks":100,"minor":20,"major":1}}}}}
        ending={"apps":{bundle:{"processes":{"10:100":
            {"pid":10,"start_ticks":100,"minor":30,"major":1}}}}}
        samples=[{"device_uptime_seconds":2,"apps":{bundle:{"processes":{
            "10:100":{"pid":10,"start_ticks":100,"minor":25,"major":1},
            "20:200":{"pid":20,"start_ticks":200,"minor":7,"major":0}}}}}]
        fork_rows=["worker [001] .... [ 2.100] sched_process_fork: parent_comm=app parent_pid=10 child_comm=worker child_pid=20"]
        gaps=target_child_counter_gaps(fork_rows,samples,baseline,ending,[bundle])
        self.assertEqual(len(gaps),1)
        self.assertEqual(gaps[0]["intermediate_identities"],["20:200"])
        self.assertEqual(gaps[0]["reason"],"no_matching_endpoint_pid_start_ticks")

    def test_target_child_with_ending_pid_start_ticks_has_final_counters(self):
        bundle="app.meet"
        baseline={"apps":{bundle:{"processes":{"10:100":
            {"pid":10,"start_ticks":100,"minor":20,"major":1}}}}}
        ending={"apps":{bundle:{"processes":{
            "10:100":{"pid":10,"start_ticks":100,"minor":30,"major":1},
            "20:200":{"pid":20,"start_ticks":200,"minor":7,"major":0}}}}}
        fork_rows=["worker [001] .... [ 2.100] sched_process_fork: parent_comm=app parent_pid=10 child_comm=worker child_pid=20"]
        self.assertEqual(target_child_counter_gaps(fork_rows,[],baseline,ending,[bundle]),[])

    def test_compare_refuses_smoke_or_missing_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            baseline = {"scene":1,"suite_hash":"same","status":"prevalidated","profile":"smoke",
                        "phase":"baseline","reuse_faults":{"total":100}}
            optimized = {**baseline,"phase":"optimized","reuse_faults":{"total":1}}
            (p/"a.json").write_text(json.dumps(baseline))
            (p/"b.json").write_text(json.dumps(optimized))
            result = compare([p/"a.json"],[p/"b.json"],p/"comparison.json")
            self.assertFalse(result["valid"])
            self.assertFalse(result["target_met"])

    def test_exact_round_count_and_same_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            cases = [{"kind":"oom","peak_mib":12000,"seed":2}]
            env = {"target":"device","meminfo":{"MemTotal":32*GIB}}
            def write(name, row):
                path = p / name
                path.write_text(json.dumps(row))
                return path
            before=[]; after=[]
            for n in range(3):
                old={"scene":2,"suite_hash":"same","cases":cases,"cases_hash":canonical_hash(cases),
                     "status":"valid","profile":"acceptance","acceptance_valid":True,
                     "measurement_complete":True,"app_uids":{"a1":100},"environment":env,
                     "session":f"old{n}","phase":"baseline","counts":{"total_events":100}}
                new={**old,"session":f"new{n}","phase":"optimized","counts":{"total_events":70}}
                before.append(write(f"a{n}.json",old)); after.append(write(f"b{n}.json",new))
            result=compare(before,after,p/"comparison.json")
            self.assertTrue(result["target_met"])
            self.assertAlmostEqual(result["reduction"],0.3)

    def test_compare_rejects_repeated_file_as_independent_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            row={"scene":1,"suite_hash":"same","status":"valid","profile":"acceptance",
                 "acceptance_valid":True,"measurement_complete":True,"phase":"baseline",
                 "session":"same","reuse_faults":{"total":100},"cases":[],"cases_hash":canonical_hash([]),
                 "environment":{"target":"device","meminfo":{"MemTotal":32*GIB}},"app_uids":{}}
            old=p/"old.json"; old.write_text(json.dumps(row))
            new=p/"new.json"; new.write_text(json.dumps({**row,"phase":"optimized","session":"new"}))
            result=compare([old]*10,[new]*10,p/"out.json")
            self.assertFalse(result["valid"])
            self.assertIn("duplicate_result_files",result["reasons"])

    def test_compare_uses_source_bound_audited_counts_but_not_prevalidation_as_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            cases = [{"kind": "startup", "peak_mib": 12000, "seed": 3}]
            env = {"target": "device", "meminfo": {"MemTotal": 16 * GIB}}

            def write_round(name, session, phase, count, *, audit=False):
                row = {"scene": 2, "suite_hash": "frozen-suite", "cases": cases,
                       "cases_hash": cases_hash(cases), "status": "prevalidated" if audit else "valid",
                       "profile": "acceptance", "acceptance_valid": not audit,
                       "measurement_complete": not audit, "app_uids": {"a1": 100},
                       "environment": env, "config": {"dram_gib": 16},
                       "session": session, "phase": phase,
                       "counts": {"total_events": count}}
                result_path = p / f"{name}.json"
                result_path.write_text(json.dumps(row), encoding="utf-8")
                audit_path = None
                if audit:
                    audit_path = p / f"{name}.audit.json"
                    doc = {"schema": "memscenes-scene2-ui-audit-v1",
                           "source_result": str(result_path), "source_run_id": session,
                           "source_hashes": {"result_json": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                                             "cases_canonical": cases_hash(cases)},
                           "primary_evidence_complete": True,
                           "case_measurement_complete": True, "progress_complete": True,
                           "case_results_content_matches": True,
                           "journal_manifest": {"all_pairs_complete": True},
                           "counts_after": {"total_events": count - 1}}
                    audit_path.write_text(json.dumps(doc), encoding="utf-8")
                return result_path, audit_path

            before, before_audits, after = [], [], []
            for n in range(3):
                old, audit = write_round(f"old{n}", f"old-session-{n}", "baseline", 138, audit=True)
                new, _ = write_round(f"new{n}", f"new-session-{n}", "optimized", 70)
                before.append(old); before_audits.append(audit); after.append(new)
            with patch("tools.audit_scene2_ui.verify_audit_document", return_value=(True, "verified")):
                result = compare(before, after, p / "compare.json", before_audits, None)
            self.assertTrue(result["valid"])
            self.assertEqual(result["baseline_event_counts"], [137, 137, 137])
            self.assertAlmostEqual(result["baseline_mean"], 137)
            self.assertAlmostEqual(result["reduction"], (137 - 70) / 137)
            self.assertTrue(all(item and item["source_result_sha256"] for item in result["baseline_audits"]))

            # Reusing the audited baseline as the optimized series is rejected by
            # session/path/phase checks and can never produce a success claim.
            with patch("tools.audit_scene2_ui.verify_audit_document", return_value=(True, "verified")):
                repeated = compare(before, before, p / "repeated.json", before_audits, before_audits)
            self.assertFalse(repeated["valid"])
            self.assertFalse(repeated["target_met"])
            self.assertTrue({"duplicate_session_rounds", "version_phase_mismatch"}.intersection(repeated["reasons"]))

    def test_compare_audit_source_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            result_path = p / "result.json"
            result_path.write_text(json.dumps({"scene": 2}), encoding="utf-8")
            audit_path = p / "audit.json"
            audit_path.write_text(json.dumps({"source_result": str(p / "other.json")}), encoding="utf-8")
            with patch("tools.audit_scene2_ui.verify_audit_document", return_value=(True, "verified")):
                with self.assertRaisesRegex(RuntimeError, "未绑定"):
                    compare([result_path], [], p / "out.json", [audit_path], None)

if __name__ == "__main__":
    unittest.main()
