import configparser
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from tools.scene4_donor_mechanism_diagnostic import (Diag, control_content_for_mode,
    attach_runner_config_preflight, invoke_bound_scene4_runner, runner_result_metadata,
    validate_bound_scene4_runner_config)
from tools.scene4_service_wrapper import (bind_runner_paths, expected_remote_root,
    parse_daemon_ini, runner_lifecycle_succeeded, scene4_runner_command)
from memscenes import runner


def write_ini(path: Path, *, root: str, oracle: str = "true") -> Path:
    parser = configparser.ConfigParser(interpolation=None)
    parser["daemon"] = {
        "oracle_mode": oracle,
        "control_file": root + "/control.conf",
        "oracle_schedule_file": root + "/oracle_schedule.tsv",
        "oracle_ledger_file": root + "/oracle_action_ledger.csv",
    }
    with path.open("w", encoding="utf-8") as stream:
        parser.write(stream)
    return path


def base_config() -> dict:
    return {"scene4": {"oracle_clock_probe_remote_path": "/some/clock"}}


def test_scene4_runner_paths_are_bound_to_daemon_ini(tmp_path):
    root = "/data/local/tmp/s4_unique"
    ini = write_ini(tmp_path / "resident.ini", root=root)
    config, proof = bind_runner_paths(base_config(), ini)
    assert proof["paths_match"] is True
    assert config["scene4"]["oracle_schedule_remote_path"] == root + "/oracle_schedule.tsv"
    assert config["scene4"]["oracle_ledger_remote_path"] == root + "/oracle_action_ledger.csv"
    assert proof["scene4_runner"]["control_file"] == root + "/control.conf"


def test_scene4_rejects_control_outside_schedule_run_root(tmp_path):
    root = "/data/local/tmp/s4_unique"
    ini = write_ini(tmp_path / "resident.ini", root=root)
    text = ini.read_text(encoding="utf-8").replace(root + "/control.conf", "/other/control.conf")
    ini.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="control path"):
        bind_runner_paths(base_config(), ini)


def test_scene4_rejects_non_oracle_daemon_ini(tmp_path):
    ini = write_ini(tmp_path / "resident.ini", root="/data/local/tmp/s4", oracle="false")
    with pytest.raises(ValueError, match="oracle configuration"):
        parse_daemon_ini(ini)


def test_scene4_paused_a_control_is_explicit_and_active_is_unchanged():
    assert control_content_for_mode("paused-a") == (
        "paused=true\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0\n")
    assert control_content_for_mode("active") == (
        "paused=false\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0\n")
    assert control_content_for_mode("runner", "baseline").startswith("paused=true\n")
    assert control_content_for_mode("runner", "optimized").startswith("paused=false\n")


@pytest.mark.parametrize("phase", ["baseline", "optimized"])
def test_scene4_runner_command_uses_bound_config_and_one_development_round(tmp_path, phase):
    config_path = tmp_path / "bound.json"
    command = scene4_runner_command("python", config_path, phase)
    assert command[command.index("--config") + 1] == str(config_path)
    assert command[command.index("--phase") + 1] == phase
    assert command[command.index("--profile") + 1] == "development"
    assert command[command.index("--rounds") + 1] == "1"
    assert command[command.index("--scene") + 1] == "4"


def test_runner_bound_config_must_match_live_daemon_paths_and_device(tmp_path):
    root = "/data/local/tmp/s4_unique"
    config = {"scene4": {"oracle_schedule_remote_path": root + "/oracle_schedule.tsv",
        "oracle_ledger_remote_path": root + "/oracle_action_ledger.csv",
        "oracle_clock_probe_remote_path": root + "/clock", "oracle_clock_probe_sha256": "a" * 64},
        "test_apps": [f"io.memscene.scene4.workload.a{i}" for i in range(1, 9)],
        "hdc": str(Path(sys.executable).resolve()), "device": "device-id"}
    path = tmp_path / "bound.json"
    path.write_text(__import__("json").dumps(config), encoding="utf-8")
    proof = validate_bound_scene4_runner_config(path,
        schedule=root + "/oracle_schedule.tsv", ledger=root + "/oracle_action_ledger.csv",
        control=root + "/control.conf", clock_helper=root + "/clock", clock_sha256="a" * 64,
        hdc=sys.executable, device_id="device-id")
    assert proof["schedule_path_match"] and proof["clock_helper_sha256_match"]
    with pytest.raises(ValueError, match="paths"):
        validate_bound_scene4_runner_config(path,
            schedule=root + "/wrong.tsv", ledger=root + "/oracle_action_ledger.csv",
            control=root + "/control.conf", clock_helper=root + "/clock", clock_sha256="a" * 64,
            hdc=sys.executable, device_id="device-id")


def test_legacy_peak_transition_metadata_does_not_claim_donor_or_synthetic_scope():
    config = {"test_apps": [f"io.memscene.scene4.workload.a{i}" for i in range(1, 9)],
        "scene4": {"execution_profile": "legacy_peak_transition", "file_mib": 1200,
            "daily_mib": 2400, "peak_mib": 5184, "daily_cold_seconds": 15,
            "case_repetitions": {"popup": 2, "startup": 2}}}
    metadata = runner_result_metadata(runner_mode=True, runner_config=config)
    assert metadata["workload_profile"] == "legacy_peak_transition"
    assert metadata["configured_test_app_count"] == 8
    assert metadata["target_launch_count"] is None
    assert metadata["scope"] == "scene4_legacy_peak_transition_real_runner_execution"
    assert metadata["planned_needed_bytes"] is None
    assert metadata["file_mib"] == 1200


def test_diagnostic_session_has_runner_journal_readback_slot():
    session = Diag()
    assert session.scene4_journal_readback == {}


def test_diagnostic_session_supports_runner_journal_readback(tmp_path):
    payload = '{"event":"cold_start_ready","case_id":"case"}\n'

    class FakeDevice:
        def app(self, bundle):
            return {"files": "/owned/files"}

        def shell(self, command, **kwargs):
            return ("__S4J_fake_case_BEGIN__" + payload +
                    f"__S4J_fake_case_END__ rc=0 bytes={len(payload.encode('utf-8'))}\n")

    session = Diag()
    session.id = "fake"
    session.device = FakeDevice()
    session.output = tmp_path
    rows = runner._scene4_read_journal(session, "owned.bundle", "case")
    assert rows == [{"event": "cold_start_ready", "case_id": "case"}]
    proof = session.scene4_journal_readback[("case", "owned.bundle")]
    assert proof["complete"] is True


def test_scene4_run_id_has_isolated_expected_remote_root():
    assert expected_remote_root("s4_012345abcdef_c0001").endswith("oracle_followup_012345abcdef")
    with pytest.raises(ValueError):
        expected_remote_root("s4_bad_c0001")


@pytest.mark.parametrize("child_code,result_status,measurement_complete", [
    (0, "pending_manual_review", False),
    (2, "invalid", False),
])
def test_bound_runner_child_result_is_retained_independent_of_measurement_status(
        tmp_path, child_code, result_status, measurement_complete):
    result_root = tmp_path / "results"
    result_root.mkdir()
    class FakeChild:
        pid = 4321
        def wait(self):
            assert started_rows and started_rows[0]["host_pid"] == self.pid
            result_dir = result_root / "scene4_baseline_fake"
            result_dir.mkdir()
            (result_dir / "result.json").write_text(__import__("json").dumps({
                "status": result_status, "measurement_complete": measurement_complete}), encoding="utf-8")
            (result_dir / "launch_dispatch_timing.jsonl").write_text(
                '{"targets":[{"request_call_started_host_monotonic_ns":123},'
                '{"request_call_started_host_monotonic_ns":null}]}' + "\n", encoding="utf-8")
            return child_code

    started_rows = []
    evidence = invoke_bound_scene4_runner(["python", "run.py"], cwd=tmp_path,
        log_path=tmp_path / "runner.log", result_root=result_root,
        popen_factory=lambda *args, **kwargs: FakeChild(), on_started=started_rows.append)
    assert evidence["execution_complete"] is True
    assert evidence["host_pid"] == 4321
    assert started_rows[0]["argv"] == ["python", "run.py"]
    assert evidence["returncode"] == child_code
    assert evidence["measurement_complete"] is measurement_complete
    assert evidence["target_launch_count"] == 1
    assert evidence["launch_evidence"]["complete"] is True


def test_runner_driver_scope_uses_real_config_and_unknown_launch_count():
    meta = runner_result_metadata(runner_mode=True, runner_config={"scene4": {
        "target_file_mib": 2048, "target_peak_mib": 6400,
        "planned_needed_bytes": 40265318400}})
    assert meta["scope"] == "scene4_bound_real_runner_execution"
    assert meta["diagnostic_only"] is False
    assert meta["synthetic_future_demand"] is False
    assert meta["target_launch_count"] is None
    assert meta["file_mib"] == 2048
    assert meta["planned_needed_bytes"] == 40265318400


def test_actual_runner_initial_result_attaches_preflight_after_metadata_exists():
    initial = runner_result_metadata(runner_mode=True, runner_config={"scene4": {
        "target_file_mib": 2048, "planned_needed_bytes": 40265318400}})
    result = attach_runner_config_preflight(initial, {"device_match": True})
    assert result["scope"] == "scene4_bound_real_runner_execution"
    assert result["runner_config_preflight"]["device_match"] is True


def test_wrapper_success_requires_execution_and_verified_sole_restore_not_measurement():
    final = {"scene4_run": {"execution_complete": True, "result_path": "result.json",
        "measurement_complete": False, "result_status": "pending_manual_review"},
        "service_restore": {"verified": True, "sole_instance": True}}
    assert runner_lifecycle_succeeded(0, final)
    assert not runner_lifecycle_succeeded(0, {**final, "service_restore": {"verified": False}})
    assert not runner_lifecycle_succeeded(2, final)


def test_bad_bound_config_is_rejected_before_any_service_stop_callback(tmp_path):
    root = "/data/local/tmp/s4_unique"
    path = tmp_path / "bound.json"
    path.write_text(__import__("json").dumps({"scene4": {
        "oracle_schedule_remote_path": root + "/wrong.tsv"}}), encoding="utf-8")
    stop_called = False
    def preflight_then_stop():
        nonlocal stop_called
        validate_bound_scene4_runner_config(path, schedule=root + "/oracle_schedule.tsv",
            ledger=root + "/oracle_action_ledger.csv", control=root + "/control.conf",
            clock_helper=root + "/clock", clock_sha256="a" * 64,
            hdc=sys.executable, device_id="device-id")
        stop_called = True
    with pytest.raises(ValueError):
        preflight_then_stop()
    assert stop_called is False


def test_wrapper_retains_child_error_result_when_restore_is_verified():
    final = {"scene4_run": {"execution_complete": True, "result_path": "result.json",
        "returncode": 2, "measurement_complete": False},
        "service_restore": {"verified": True}}
    assert runner_lifecycle_succeeded(0, final)
