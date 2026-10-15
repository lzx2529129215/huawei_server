import unittest
from pathlib import Path

from tools.realapp_dataset.catalog import suite
from tools.realapp_dataset.core import GIB, Gate, active_mount, fill_mib, memory, schedule, segment_states, training_windows, validate_recipe


class DatasetTests(unittest.TestCase):
    def test_real_capacity(self):
        self.assertEqual(fill_mib(32679652*1024), 23721)
        self.assertEqual(fill_mib(32*GIB), 24576)
        with self.assertRaises(ValueError): fill_mib(8*GIB)

    def test_mount_overmount_uses_fd(self):
        text="8 4 0:9 / /dev/shm rw - tmpfs tmpfs rw,size=16G\n93 8 0:23 / /dev/shm rw,nosuid - tmpfs tmpfs rw,size=1048576k"
        self.assertEqual(active_mount("mnt_id:\t93\n",text)["size"],"1048576k")
        with self.assertRaises(ValueError): active_mount("mnt_id: 99",text)

    def test_gate_threshold_and_one_attempt(self):
        g=Gate()
        self.assertFalse(g.update(0,800000000,True))
        self.assertTrue(g.update(.25,800000000,True))
        self.assertFalse(g.update(.5,800000000,True))
        self.assertFalse(g.update(.75,9*GIB,True))
        self.assertFalse(g.update(1,0,True))
        self.assertFalse(g.update(1.25,0,True))

    def test_gate_gaps_and_unarmed(self):
        g=Gate()
        self.assertFalse(g.update(0,0,False))
        self.assertFalse(g.update(.25,0,True))
        self.assertFalse(g.update(2,0,True))
        self.assertFalse(g.update(2.25,None,True))
        self.assertFalse(g.update(2.5,0,True))
        self.assertTrue(g.update(2.75,0,True))

    def test_no_unknown_negative_labels(self):
        s=[{"time":i,"available":100,"gate":"enable"} for i in range(71)]
        w=training_windows(s,[],event_coverage=False)
        self.assertIsNone(w[0]["labels"]["5"]["oom"])
        self.assertFalse(w[-1]["labels"]["5"]["valid"])
        self.assertFalse(training_windows(s,[],event_coverage=True)[0]["labels"]["5"]["oom"])

    def test_future_not_in_history(self):
        s=[{"time":i,"available":100-i,"gate":"enable" if i<35 else "readonly"} for i in range(71)]
        w=training_windows(s,[{"category":"oom_kill","time":34}],event_coverage=True)[0]
        self.assertEqual(w["time"],30)
        self.assertTrue(all(r["time"]<=30 for r in w["history"]))
        self.assertTrue(w["labels"]["5"]["oom"])
        self.assertTrue(w["labels"]["5"]["gate_changed"])
        self.assertEqual(w["labels"]["5"]["time_to_oom"],4)

    def test_missing_future_sample(self):
        s=[{"time":i,"available":100} for i in range(70) if i!=33]
        w=training_windows(s,[],event_coverage=True)[0]
        self.assertFalse(w["labels"]["5"]["valid"])

    def test_unknown_memory_not_zero(self):
        self.assertNotIn("MemAvailable",memory("MemTotal: 100 kB\n"))
        self.assertEqual(memory("MemTotal: 100 kB\n")["MemTotal"],102400)

    def test_reopen_is_separate_segment(self):
        s=[{"time":i*.25,"gate":g} for i,g in enumerate(["enable","readonly","readonly","enable","bad"])]
        self.assertEqual([r["state"] for r in segment_states(s)],["enable","readonly","enable","unknown"])

    def test_all_apps_and_counterbalanced_pairs(self):
        s=suite(); self.assertEqual(len(s["apps"]),20)
        self.assertEqual(set(s["apps"]),{x["app"] for scene in s["scenes"] for x in scene["steps"]})
        plan=schedule(s); self.assertEqual(len(plan),12)
        self.assertEqual([x["phase"] for x in plan[:4]],["observe","apply","apply","observe"])

    def test_homepage_not_a_workload(self):
        s=suite();step=s["scenes"][0]["steps"][0]
        errors=validate_recipe(step["recipe"],s["apps"][step["app"]],Path.cwd())
        self.assertIn("no_real_operation",errors)
        self.assertIn("missing_test_material_or_online_fixture",errors)


if __name__=="__main__": unittest.main()
