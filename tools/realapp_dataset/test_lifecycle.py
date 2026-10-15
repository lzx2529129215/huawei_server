import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools.realapp_dataset.device_ops import Session
from tools.realapp_dataset.pipeline import service_interventions


class LifecycleTests(unittest.TestCase):
    def test_external_occupancy_cannot_allocate_or_be_removed(self):
        dev=Mock()
        session=Session(dev,Path('unused'),"apply",manage_fill=False)
        with self.assertRaises(RuntimeError):session.allocate(8)
        dev.checked.assert_not_called()
        session.snapshot={"boot_id":"boot", "mount":{"mount_id":93,"size":"24g","raw":"original"},
                          "gate":"enable","gate_detail":"hyperhold enable reclaim_in enable",
                          "service":{"pid":100,"start_ticks":123,"control_path":"/data/local/tmp/control"}}
        script=session.cleanup_script()
        self.assertNotIn(session.filldir,script)
        self.assertNotIn("mount -o remount",script)
        self.assertIn('original_control',script)

    def test_partial_dd_never_qualifies(self):
        with tempfile.TemporaryDirectory() as folder:
            dev=Mock();dev.checked.side_effect=["0","4194304","tmpfs allocation", "8192"]
            session=Session(dev,Path(folder),"observe")
            self.assertFalse(session.allocation_result(8)["valid"])

    def test_nonzero_dd_rc_never_qualifies(self):
        with tempfile.TemporaryDirectory() as folder:
            dev=Mock();dev.checked.side_effect=["1","8388608","tmpfs allocation", "16384"]
            self.assertFalse(Session(dev,Path(folder),"observe").allocation_result(8)["valid"])

    def test_watchdog_expiry_blocks_new_allocation(self):
        dev=Mock();dev.uptime.return_value=100
        session=Session(dev,Path("unused"),"observe");session.deadline=104
        with self.assertRaises(RuntimeError):session.allocate(8)
        dev.text.assert_not_called();dev.push.assert_not_called()

    def test_disconnect_restore_is_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            dev=Mock();dev.checked.side_effect=RuntimeError("device offline")
            session=Session(dev,Path(folder),"observe");session.started=True;session.watchdog_armed=True
            result=session.restore()
            self.assertFalse(result["verified"])
            self.assertTrue((Path(folder)/"restore.json").is_file())

    def test_observe_write_disqualifies(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)
            for suffix in ("before","after"):(p/("service_"+suffix)).mkdir()
            (p/"service_before/daily_actions.csv").write_text("action_id,write_attempted\n",encoding="utf8")
            (p/"service_after/daily_actions.csv").write_text("action_id,write_attempted\na,1\n",encoding="utf8")
            result=service_interventions(p,"observe")
            self.assertTrue(result["complete"]);self.assertFalse(result["observe_no_writes"])

    def test_rotated_ledger_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)
            for suffix in ("before","after"):(p/("service_"+suffix)).mkdir()
            (p/"service_before/daily_actions.csv").write_text("id,write_attempted\na,0\n",encoding="utf8")
            (p/"service_after/daily_actions.csv").write_text("id,write_attempted\nb,0\n",encoding="utf8")
            self.assertFalse(service_interventions(p,"observe")["complete"])


if __name__=="__main__":unittest.main()
