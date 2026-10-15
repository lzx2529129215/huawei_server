"""Gone-process /proc parsing regressions for this isolated 8.5 GiB runner."""
from __future__ import annotations

import unittest

from native_bridge import start_ticks


def proc_stat(ticks: int) -> str:
    # Field 22 is suffix token 19 after state (field 3).
    suffix = ["S"] + ["0"] * 18 + [str(ticks)]
    return "321 (browser process ) name) " + " ".join(suffix)


class FakeDevice:
    def __init__(self, response: str):
        self.response = response

    def shell(self, command: str, **kwargs) -> str:
        assert command == "cat /proc/321/stat"
        assert kwargs.get("check") is False
        return self.response


class GonePidStatTests(unittest.TestCase):
    def test_cat_error_text_means_gone_process(self):
        device = FakeDevice("cat: /proc/321/stat: No such file or directory")
        self.assertEqual(start_ticks(device, 321), 0)

    def test_empty_proc_text_means_gone_process(self):
        self.assertEqual(start_ticks(FakeDevice(""), 321), 0)

    def test_normal_proc_stat_returns_start_ticks_even_with_parens_in_comm(self):
        self.assertEqual(start_ticks(FakeDevice(proc_stat(987654)), 321), 987654)

    def test_truncated_proc_text_means_unbound_identity(self):
        self.assertEqual(start_ticks(FakeDevice("321 (gone) S 0"), 321), 0)


if __name__ == "__main__":
    unittest.main()
