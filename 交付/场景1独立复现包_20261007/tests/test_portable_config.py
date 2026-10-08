import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("make_scene1_config", ROOT / "tools/make_scene1_config.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PortableConfigTests(unittest.TestCase):
    def test_16_gib_is_exactly_150_percent_and_pressure_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "16.json"
            result = MODULE.generate(16, None, None, ROOT / "config/scene1_full48_pressure12.json", out)
            self.assertEqual(result["app_target_per_app_mib"], 3072)
            self.assertEqual(result["app_total_mib"], 24576)
            self.assertEqual(result["pressure_mib_separate_from_app_budget"], 6144)
            self.assertEqual(result["file_target_mib_per_app"], 2304)

    def test_nonstandard_memory_rounds_up_to_64_mib_quanta(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "other.json"
            result = MODULE.generate(24, 4096, "serial", ROOT / "config/scene1_full48_pressure12.json", out)
            self.assertEqual(result["app_target_per_app_mib"] % 64, 0)
            self.assertGreaterEqual(result["app_target_ratio_to_nominal_dram"], 1.5)
            self.assertEqual(result["pressure_mib_separate_from_app_budget"], 4096)

    def test_rejects_under_16_gib(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                MODULE.generate(8, None, None, ROOT / "config/scene1_full48_pressure12.json", Path(temp) / "bad.json")


if __name__ == "__main__":
    unittest.main()
