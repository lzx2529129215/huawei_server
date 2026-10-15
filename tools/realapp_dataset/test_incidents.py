import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools.realapp_dataset.incidents import pressure_blockers
from tools.realapp_dataset.pipeline import smoke


class IncidentTests(unittest.TestCase):
    def test_known_panic_does_not_block_small_diagnostic(self):
        self.assertEqual([], pressure_blockers("3QC0124C03000514", 8))
        self.assertTrue(pressure_blockers("3QC0124C03000514", 23721))

    def test_full_fill_blocks_before_device_mutation(self):
        dev = Mock(target="3QC0124C03000514")
        dev.read.return_value = "MemTotal: 32679652 kB\n"
        with tempfile.TemporaryDirectory() as folder:
            result = smoke(dev, Path(folder), Path("unused"), full_fill=True)
            self.assertEqual("blocked", result["status"])
            self.assertFalse(result["device_mutated"])
            self.assertFalse(result["valid_for_training"])
        dev.checked.assert_not_called()
        dev.push.assert_not_called()
        dev.text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
