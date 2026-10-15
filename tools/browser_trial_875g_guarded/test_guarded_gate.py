import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("guarded_gate_test_module", HERE / "guarded_gate.py")
guarded = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guarded)

BOOT = "c3821db7-989a-4977-a721-b1bcd955c17c"
T = 8414.43


def fixture():
    events = [
        f"PREFLIGHT|{T:.6f}|8414.430100|boot_match=1|proc=enable|reclaim_in=enable|write_total_size=1234|reclose_once=enabled|close_at={T-1:.6f}|open_at={T+930:.6f}|watchdog_at={T+935:.6f}|expected_poll_count=1862",
        "GATE_HELPER|8414.430200|pid=100|start_ticks=200|watchdog_pid=101|watchdog_start_ticks=201|deadline=9349.430000",
        "RECLOSE_ATTEMPT|8420.0|poll_counter=12|attempt_slot=reserved",
        "RECLOSE_RESULT|8420.1|write_invoked=1|success=1|readback=readonly|boot_match=1|counter_before=1234|counter_after=1234|reclaim_after=enable",
    ]
    for i in range(guarded.EXPECTED_POLLS):
        gate, status = ("enable", "reclosed_once") if i == 12 else ("readonly", "ok")
        events.append(f"POLL|{T-0.5+i*0.5:.6f}|{T-0.49+i*0.5:.6f}|{gate}|1|reclaim_in_enable|1234|{status}")
    events.extend([
        f"GATE_IO_COVERAGE|{T+930:.6f}|write_counter_stable=1|poll_count=1862|expected_poll_count=1862|open_at={T+930:.6f}|no_new_disk_write_observed=1|strict_continuous_readonly=0|bounded_control_complete=1|bounded_control_scope=through_open_not_restoration|reclose_used=1",
        f"WATCHDOG_EXIT|{T+935:.6f}|reaped=1|wait_errno=0|status=0",
        f"GATE_RESULT|{T+935:.1f}|valid=0|close_readback_verified=1|open_readback_verified=1|io_write_counter_stable=1|reclose_once=enabled|strict_continuous_readonly=0|no_new_disk_write_observed=1|bounded_control_complete=1|bounded_control_scope=through_open_not_restoration|restoration_verified=1|final_boot_match=1",
        f"FINAL|{T+935.1:.6f}|proc=enable|boot={BOOT}|boot_match=1",
    ])
    watchdog = f"WATCHDOG|{T+935:.6f}|boot_match=1|write_ok=1|write_bytes=7|errno=0|elapsed_ms=1|readback=enable\n"
    collector = {"continuous_capture_proven": True, "horizon_reached": True,
        "protocol_review": {"verified": True}, "last_heartbeat": {"invalid":"0","gaps":"0","unconfirmed_records":"0"},
        "epipe_count":0,"async_invalid_count":0,"heartbeat_interval_over_15s":False}
    oom = {"capture_start_verified":True,"explicit_victim_rows_without_timestamp":0,
        "unresolved_oom_context_count":0}
    service = {"verified":True,**guarded.ORIGINAL_HASHES}
    targets = {bundle:{"verified_absent":True} for bundle in guarded.TARGET_BUNDLES}
    return {"events":"\n".join(events)+"\n","watchdog":watchdog,"collector":collector,
        "oom":oom,"service":service,"targets":targets,"observed_boot":BOOT}


class GuardedGateTests(unittest.TestCase):
    def audit(self, f):
        return guarded.audit_bounded_diagnostic(events=f["events"],watchdog=f["watchdog"],
            expected_boot=BOOT,T=T,native_returncode=0,collector=f["collector"],oom_audit=f["oom"],
            service_restore=f["service"],target_cleanup=f["targets"],observed_boot=f["observed_boot"])

    def test_candidate_pin_and_opt_in_argv(self):
        self.assertEqual(guarded.verify_candidate()["sha256"],guarded.EXPECTED_GATE_SHA256)
        with self.assertRaises(ValueError):
            guarded.make_gate_argv("/data/local/tmp/gate",100.0,BOOT,"/data/local/tmp/run")
        argv=guarded.make_gate_argv("/data/local/tmp/gate",100,BOOT,"/data/local/tmp/run",
            accept_bounded_diagnostic_only=True)
        self.assertEqual(argv[-1],"--reclose-once")
        self.assertEqual(argv[argv.index("--open-offset")+1],"930")
        self.assertEqual(argv[argv.index("--watchdog-offset")+1],"935")

    def test_bounded_pass_keeps_strict_and_comparison_invalid(self):
        out=self.audit(fixture())
        self.assertTrue(out["bounded_diagnostic_valid"],out["errors"])
        self.assertFalse(out["strict_continuous_readonly"])
        self.assertFalse(out["original_gate_valid"])
        self.assertFalse(out["valid_for_oom_comparison"])
        self.assertFalse(out["physical_zero_write_guaranteed"])

    def test_unknown_or_failed_evidence_cannot_pass(self):
        mutations=(
            lambda f: f.update(events=f["events"].replace("no_new_disk_write_observed=1","no_new_disk_write_observed=unknown")),
            lambda f: f.update(events="\n".join(x for x in f["events"].splitlines() if not x.startswith("POLL|"))),
            lambda f: f["collector"].update(epipe_count=1),
            lambda f: f["oom"].update(unresolved_oom_context_count=1),
            lambda f: f["service"].update(ini_sha256="unknown"),
            lambda f: f["targets"].pop(guarded.TARGET_BUNDLES[0]),
            lambda f: f.update(observed_boot="11111111-1111-1111-1111-111111111111"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                f=fixture(); mutate(f)
                self.assertFalse(self.audit(f)["bounded_diagnostic_valid"])


if __name__ == "__main__":
    unittest.main()
