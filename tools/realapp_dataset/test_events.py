import unittest
import tempfile
from pathlib import Path
from tools.realapp_dataset.events import classify_kernel, classify_system, read_kernel_capture


class VendorOomTests(unittest.TestCase):
    def fixture(self):
        event={"domain_":"KERNEL_VENDOR","name_":"LOWMEM","REASON":"OOM_KILLER",
               "time_":102000,"PID":123,"UID":7,"PROCESS_NAME":"app"}
        proc=[{"time":101,"pid":123,"uid":7,"bundle":"app","start_ticks":"80"},
              {"time":105,"event":"process_frame","members":[]}]
        gate=[{"time":100,"wall_ms":100000},{"time":110,"wall_ms":110000}]
        return [event],proc,gate

    def test_vendor_selection_with_identity_and_later_exit(self):
        r=classify_system(*self.fixture())["events"][0]
        self.assertEqual(r["category"],"oom_kill")
        self.assertEqual(r["time"],102)
        self.assertEqual(r["observed_exit_time"],105)
        self.assertEqual(r["identity"]["start_ticks"],"80")

    def test_missing_exit_remains_candidate(self):
        events,proc,gate=self.fixture()
        self.assertEqual(classify_system(events,proc[:1],gate)["events"][0]["category"],"vendor_oom_candidate")

    def test_uid_mismatch_not_attributed(self):
        events,proc,gate=self.fixture();events[0]["UID"]=8
        r=classify_system(events,proc,gate)["events"][0]
        self.assertIsNone(r["identity"])
        self.assertEqual(r["category"],"vendor_oom_candidate")

    def test_allocation_latency_and_outside_run_not_oom(self):
        events,proc,gate=self.fixture();events[0]["REASON"]="ALLOC_LATENCY"
        self.assertEqual(classify_system(events,proc,gate)["events"],[])
        events[0]["REASON"]="OOM_KILLER";events[0]["time_"]=99000
        self.assertEqual(classify_system(events,proc,gate)["events"],[])


class KernelTests(unittest.TestCase):
    def test_binary_kernel_text_preserved_without_losing_record(self):
        payload=b'{"raw":"6,1,118000000,-;read data = @\xd1\xc3j\\n"}\n'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'kernel.jsonl';path.write_bytes(payload)
            records=read_kernel_capture(path)
            self.assertEqual(records[0]['_raw_encoding'],'latin-1-byte-map')
            self.assertEqual(records[0]['raw'].encode('latin-1'),b'6,1,118000000,-;read data = @\xd1\xc3j\n')
            self.assertEqual(path.read_bytes(),payload)

    def test_truncated_binary_json_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'kernel.jsonl';path.write_bytes(b'{"raw":"\xd1')
            with self.assertRaises(ValueError):read_kernel_capture(path)

    def fixture(self,exit=True,gap=False):
        def row(seq,t,text):return {"time":t-18+.01,"raw":f"6,{seq},{int(t*1e6)},-;{text}\n"}
        raw=[row(1,118,"REALAPP_CLOCK run=/run kind=begin time=100.000000"),
             row(2,120,"[mem_oom:999] out of Memory (OOM), start to kill process <app> (pid: 123, score: 500)")]
        if exit:raw.append(row(4 if gap else 3,120.1,"process has exited, pid=123"))
        raw.append(row((5 if gap else 4) if exit else 3,125,"REALAPP_CLOCK run=/run kind=end time=107.000000"))
        gate=[{"event":"ready","kernel_open":True,"run":"/run"},{"event":"stopped","kernel_read_ok":True}]
        proc=[{"time":101.5,"pid":123,"start_ticks":"80","bundle":"app","uid":7},
              {"time":101.5,"event":"process_frame","members":["123:80"]}]
        return raw,proc,gate

    def test_offset_and_confirmed_exit(self):
        r=classify_kernel(*self.fixture())
        self.assertTrue(r["oom_label_coverage_complete"])
        self.assertEqual(r["events"][0]["time"],102)
        self.assertEqual(r["events"][0]["identity"]["start_ticks"],"80")

    def test_missing_exit_not_positive_or_negative(self):
        r=classify_kernel(*self.fixture(exit=False))
        self.assertFalse(r["oom_label_coverage_complete"])
        self.assertEqual(r["events"][0]["category"],"kernel_oom_candidate")

    def test_gap_preserves_positive_but_invalidates_negatives(self):
        r=classify_kernel(*self.fixture(gap=True))
        self.assertFalse(r["oom_label_coverage_complete"])
        self.assertEqual(r["events"][0]["category"],"oom_kill")

    def test_pid_reuse_not_attributed(self):
        raw,proc,gate=self.fixture()
        proc.append({"time":101.8,"event":"process_frame","members":[]})
        r=classify_kernel(raw,proc,gate)
        self.assertIsNone(r["events"][0]["identity"])
        self.assertEqual(r["events"][0]["category"],"oom_kill")

    def test_missing_end_marker_unknown(self):
        raw,proc,gate=self.fixture()
        r=classify_kernel(raw[:-1],proc,gate)
        self.assertFalse(r["oom_label_coverage_complete"])
        self.assertIsNone(r["events"][0]["time"])

    def test_reordered_records_preserve_coverage_and_exit_association(self):
        raw,proc,gate=self.fixture()
        raw[1],raw[2]=raw[2],raw[1]
        r=classify_kernel(raw,proc,gate)
        self.assertTrue(r["oom_label_coverage_complete"])
        self.assertEqual(r["out_of_order_records"],1)
        self.assertEqual(r["events"][0]["category"],"oom_kill")

    def test_reordering_does_not_hide_a_real_gap(self):
        raw,proc,gate=self.fixture(gap=True)
        raw[1],raw[2]=raw[2],raw[1]
        r=classify_kernel(raw,proc,gate)
        self.assertFalse(r["oom_label_coverage_complete"])
        self.assertIn("kernel_sequence_gap",r["coverage_errors"])

    def test_duplicate_sequence_invalidates_coverage(self):
        raw,proc,gate=self.fixture()
        raw.insert(2,dict(raw[1]))
        r=classify_kernel(raw,proc,gate)
        self.assertFalse(r["oom_label_coverage_complete"])
        self.assertIn("kernel_duplicate_sequence",r["coverage_errors"])


if __name__=="__main__":unittest.main()
