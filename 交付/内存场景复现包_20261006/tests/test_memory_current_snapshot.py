import tempfile
import unittest
from pathlib import Path

from memscenes.runner import Session


class SnapshotDevice:
    def __init__(self):
        self.uptime = 10.0

    def shell(self, command, **kwargs):
        if command == "cat /proc/uptime":
            self.uptime += 0.05
            return f"{self.uptime} 0.0"
        if command == "cat /proc/meminfo":
            return "MemTotal: 1000 kB\n"
        if command == "cat /proc/vmstat":
            return "pagefault_minor_anon 2\n"
        if command == "cat /sys/block/zram0/hyperhold_io_info_file":
            return ""
        if "__MEMORY_CURRENT__" in command:
            return ("__MEMORY_CURRENT__\n123456\n"
                    "__MEMORY_USAGE__\n/bin/sh: no such file\n"
                    "__MEMORY_STAT__\nEswap: 12 kB\n"
                    "__CGROUP_PROCS__\n321\n")
        return ""

    def app_processes(self, bundle):
        return {"321:9000": {"pid": 321, "minor": 4, "major": 5}}

    def app(self, bundle):
        return {"uid": 20001, "cgroup": f"/dev/cgroup/memory/default/100/{bundle}_20001"}


class MemoryCurrentSnapshotTest(unittest.TestCase):
    def test_snapshot_captures_current_source_uid_and_bundle_procs(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Session.__new__(Session)
            session.device = SnapshotDevice()
            session.samples = []
            session.output = Path(temp)
            sample = session.snapshot("before_pressure", ["sample.bundle"])
            app = sample["apps"]["sample.bundle"]
            cgroup = app["cgroup_memory"]
            self.assertEqual(cgroup["current_bytes"], 123456)
            self.assertEqual(cgroup["source"], "memory.current")
            self.assertEqual(cgroup["uid"], 20001)
            self.assertEqual(cgroup["cgroup_procs"], [321])
            self.assertEqual(cgroup["process_identity_keys"], ["321:9000"])
            self.assertEqual(app["eswap_bytes"], 12 * 1024)
            self.assertGreater(sample["snapshot_duration_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
