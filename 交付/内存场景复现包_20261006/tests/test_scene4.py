import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from memscenes.runner import (case_definitions, parse_owned_file_vmas,
                              scene4_reclaim_snapshot, suite_spec)


PACKAGE = Path(__file__).resolve().parents[1]


class Scene4ColdReclaim(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((PACKAGE / "config/scenarios_scene4_cold_reclaim.json").read_text(encoding="utf-8"))

    def test_fixed_cases_are_isolated_and_fit_requested_budget(self):
        cases = case_definitions(self.config, "combined", "scene4")
        self.assertEqual([row["kind"] for row in cases], ["popup", "startup"])
        self.assertTrue(all(len(row["bundles"]) == 8 for row in cases))
        self.assertEqual([(row["daily_mib"], row["file_mib"], row["peak_mib"], row["hold_seconds"])
                          for row in cases], [(2400, 1200, 5184, 60), (2400, 1200, 5184, 15)])
        self.assertEqual([row["seed"] for row in cases], [20261005, 20261005])
        mem_total_mib = 33463963648 / 1024**2
        self.assertLessEqual(2400, mem_total_mib)
        self.assertLessEqual(2400 * 8, mem_total_mib)
        peak_ratio = 5184 * 8 / mem_total_mib
        self.assertGreaterEqual(peak_ratio, 1.2)
        self.assertLessEqual(peak_ratio, 1.4)
        self.assertEqual(self.config["scene4"]["daily_file_mib"] +
                         self.config["scene4"]["daily_anon_mib"], 2400)
        self.assertEqual(suite_spec(self.config, 4, "development")["settings"]["scene4_cases"],
                         self.config["scene4"])

    def test_snapshot_keeps_missing_smaps_fields_unknown_and_records_file_vma(self):
        bundle = "io.memscene.scene4.workload.a1"

        class FakeDevice:
            def clock_pair(self):
                return 101.25, 1790000000000

            def shell(self, command, **kwargs):
                if command.startswith("stat -c"):
                    return "0 42 /data/app/el2/100/base/io.memscene.scene4.workload.a1/haps/entry/files/memscene_case.pages\n"
                if command == "cat /proc/meminfo":
                    return "MemTotal: 32679648 kB\nMemAvailable: 1000 kB\nCached: 2000 kB\n"
                if command == "cat /proc/vmstat":
                    return "pgfault 42\npgmajfault 2\n"
                if "__MEMSCENE_ROLLUP__" in command:
                    return ("__MEMSCENE_STAT_FILE__\n0 42 /data/app/el2/100/base/io.memscene.scene4.workload.a1/haps/entry/files/memscene_case.pages\n"
                            "__MEMSCENE_ROLLUP__\nRss: 100 kB\nPss: 80 kB\nRssAnon: 30 kB\nRssFile: 70 kB\n"
                            "__MEMSCENE_MAPS__\n"
                            "7f000000-7f100000 rw-s 00000000 00:00 42 /data/storage/el2/base/haps/entry/files/memscene_case.pages\n"
                            "__MEMSCENE_FULL_SMAPS__\n"
                            "7f000000-7f100000 rw-s 00000000 00:00 42 /data/storage/el2/base/haps/entry/files/memscene_case.pages\n"
                            "Size: 1024 kB\nRss: 800 kB\nPss: 800 kB\nShared_Clean: 800 kB\nPrivate_Clean: 0 kB\n"
                            "7f001000-7f002000 rw-p 00000000 00:00 0 [anon]\n"
                            "Size: 4 kB\nRss: 1 kB\nPss: 1 kB\n")
                raise AssertionError(command)

            def app_processes(self, requested_bundle):
                assert requested_bundle == bundle
                return {"123:456": {"pid": 123, "start_ticks": "456", "vsize": 4096, "rss_pages": 25}}

            def app(self, requested_bundle):
                assert requested_bundle == bundle
                return {"files": "/data/app/el2/100/base/io.memscene.scene4.workload.a1/haps/entry/files"}

        snapshot = scene4_reclaim_snapshot(SimpleNamespace(device=FakeDevice(), config={"scene4": {"file_mib": 1}}), "case", [bundle], "daily_cold_end")
        process = snapshot["apps"][bundle]["processes"]["123:456"]
        self.assertEqual(process["owned_file_vma_count"], 1)
        self.assertEqual(process["owned_file_resident_bytes"], 800 * 1024)
        self.assertEqual(process["owned_file_pss_bytes"], 800 * 1024)
        self.assertEqual(process["smaps_rollup_bytes"]["Pss"], 80 * 1024)
        self.assertIsNone(process["smaps_rollup_bytes"]["SwapPss"])
        self.assertFalse(process["smaps_rollup_field_coverage"]["SwapPss"])
        self.assertTrue(process["owned_file_stat_inode_match"])
        self.assertTrue(process["owned_file_stat_device_match"])
        self.assertTrue(process["owned_file_vma_evidence_complete"])
        self.assertEqual(process["owned_file_vma_size_bytes"], 1024 * 1024)
        self.assertIsNone(snapshot["system_meminfo"]["Inactive(file)"])
        self.assertFalse(snapshot["field_coverage"]["meminfo"]["Inactive(file)"])
        self.assertTrue(snapshot["field_coverage"]["owned_file_vma_rss_pss_covered_all_processes"])

    def test_owned_file_vma_requires_alias_suffix_and_matching_inode_device(self):
        expected = "/data/app/el2/100/base/io.memscene.scene4.workload.a1/haps/entry/files/memscene_x.pages"
        maps = ("1000-2000 rw-s 00000000 68:42 99 /data/storage/el2/base/haps/entry/files/memscene_x.pages\n")
        smaps = ("1000-2000 rw-s 00000000 68:42 99 /data/storage/el2/base/haps/entry/files/memscene_x.pages\n"
                 "Size: 4 kB\nRss: 4 kB\nPss: 4 kB\nPrivate_Clean: 0 kB\nShared_Clean: 4 kB\n")
        dev = (104 << 8) | 66
        self.assertEqual(len(parse_owned_file_vmas(smaps, expected, 99, dev, maps)), 1)
        self.assertEqual(parse_owned_file_vmas(smaps, expected, 100, dev, maps), [])
        wrong = smaps.replace("memscene_x.pages", "memscene_other.pages")
        self.assertEqual(parse_owned_file_vmas(wrong, expected, 99, dev, maps), [])


if __name__ == "__main__":
    unittest.main()
