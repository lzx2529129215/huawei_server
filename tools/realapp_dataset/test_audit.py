import hashlib
import tempfile
import unittest
from pathlib import Path
from tools.realapp_dataset.audit import audit_partial


class AuditTests(unittest.TestCase):
    def test_corrupt_tail_retains_original(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder);raw=b'{"time":1,"stage":"prepare"}\n'+b'\0'*100
            (p/"system.jsonl").write_bytes(raw)
            r=audit_partial(p)
            self.assertFalse(r["valid_for_training"])
            self.assertEqual(r["files"]["system.jsonl"]["complete_prefix_records"],1)
            self.assertEqual((p/"system.jsonl").read_bytes(),raw)
            self.assertEqual(r["files"]["system.jsonl"]["sha256"],hashlib.sha256(raw).hexdigest())

    def test_missing_source_remains_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            r=audit_partial(Path(folder))
            self.assertTrue(r["files"]["kernel.jsonl"]["missing"])
            self.assertFalse(r["valid_for_training"])


if __name__=="__main__":unittest.main()
