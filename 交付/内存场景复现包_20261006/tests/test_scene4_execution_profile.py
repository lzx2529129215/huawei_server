import pytest

from memscenes.runner import (scene4_execution_engine, _scene4_schedule_v3_header,
                              _scene4_schedule_v3_row, _scene4_schedule_publish_decision,
                              _scene4_profile_cutoff_status)
import memscenes.runner as runner_module


def test_scene4_default_stays_on_cold_start_runner():
    assert scene4_execution_engine({"scene4": {"oracle_cold_start_runner": True}}) == "scene4"
    assert scene4_execution_engine({"scene4": {}}) == "scene4"


def test_explicit_legacy_profile_uses_eight_app_scene2_engine():
    config = {"scene4": {"execution_profile": "legacy_peak_transition",
                         "oracle_cold_start_runner": False}}
    assert scene4_execution_engine(config) == "scene2"


def test_legacy_profile_rejects_cold_six_target_flag():
    config = {"scene4": {"execution_profile": "legacy_peak_transition",
                         "oracle_cold_start_runner": True}}
    with pytest.raises(ValueError, match="cannot use the six-cold-start oracle runner"):
        scene4_execution_engine(config)


def test_legacy_profile_snapshot_inflight_at_promote_is_not_a_measurement_gap():
    status = _scene4_profile_cutoff_status(True, True, [])
    assert status == {"snapshot_inflight_at_stop": True,
                      "writer_alive_when_checked": True,
                      "late_publications": [], "measurement_gap": False}


def test_legacy_profile_late_schedule_publication_is_a_real_gap():
    status = _scene4_profile_cutoff_status(False, False, [
        {"crossed_deadline": False}, {"crossed_deadline": True, "boottime_end_s": 115.1}])
    assert status["measurement_gap"] is True
    assert len(status["late_publications"]) == 1


@pytest.mark.parametrize("stop,now,deadline,allowed,reason", [
    (False, 114.0, 115.0, True, None),
    (False, 114.5, 115.0, False, "publication_reserve_window_reached"),
    (True, 114.0, 115.0, False, "stop_requested"),
    (False, 115.0, 115.0, False, "fixed_deadline_reached"),
    (False, float("nan"), 115.0, False, "clock_unknown"),
])
def test_legacy_profile_publisher_gate(stop, now, deadline, allowed, reason):
    result = _scene4_schedule_publish_decision(stop, now, deadline)
    assert (result["allowed"], result["reason"]) == (allowed, reason)


def test_legacy_profile_publisher_reserve_is_explicit_and_invalid_values_fail_closed():
    assert _scene4_schedule_publish_decision(False, 114.5, 115.0, 0.25)["allowed"]
    assert _scene4_schedule_publish_decision(False, 114.5, 115.0, -1)["reason"] == "publish_reserve_invalid"


def _v3_donor(index=1):
    pid, uid, ticks = 4200 + index, 20020190 + index, 123400 + index
    return {
        "bundle": f"io.memscene.scene4.workload.a{index}", "uid": uid, "pid": pid,
        "start_ticks": ticks, "daily_ready": True, "window_ready": True,
        "daily_ready_boottime_s": 100.0, "background_since_s": 101.0,
        "aa_pid": pid, "aa_uid": uid, "aa_state": "BACKGROUND",
        "aa_observed_boottime_s": 102.0, "window_visible": False,
        "window_focused": False, "window_observed_boottime_s": 102.0,
        "proc_verified": True, "proc_start_ticks": ticks,
        "proc_observed_boottime_s": 102.0, "lifecycle_clear": True,
        "lifecycle_observed_boottime_s": 102.0,
        "background_evidence_source": "os_snapshot",
        "writeback_file_path": "memscene_case_0001.pages",
        "writeback_file_dev": 66662, "writeback_file_ino": 164292 + index,
        "writeback_file_size": 1258291200,
    }


def test_schema3_row_is_variable_length_and_has_exact_eight_slot_header():
    header = _scene4_schedule_v3_header()
    row = _scene4_schedule_v3_row("evt1", "case_0001", 130.125, 23353884672,
                                  [_v3_donor(1), _v3_donor(2)])
    assert len(header) == 6 + 8 * 25
    assert len(row) == 6 + 2 * 25
    assert row[:6] == ["3", "evt1", "case_0001", "130.125", "23353884672", "2"]
    assert row[6] == "io.memscene.scene4.workload.a1"
    assert row[6 + 25] == "io.memscene.scene4.workload.a2"


@pytest.mark.parametrize("change", [
    {"aa_state": "UNKNOWN"}, {"window_visible": "unknown"},
    {"proc_start_ticks": 999999},
    {"writeback_file_path": "../other.pages"}, {"writeback_file_ino": "0x123"},
])
def test_schema3_rejects_unproven_or_mismatched_donor(change):
    donor = _v3_donor()
    donor.update(change)
    with pytest.raises(ValueError):
        _scene4_schedule_v3_row("evt1", "case_0001", 130.0, 100,
                                [donor])


def test_schema3_rejects_more_than_eight_donors_and_bad_deadline():
    with pytest.raises(ValueError, match="one through eight"):
        _scene4_schedule_v3_row("evt1", "case_0001", 130.0, 100,
                                [_v3_donor(index) for index in range(1, 10)])
    with pytest.raises(ValueError, match="positive finite BOOTTIME"):
        _scene4_schedule_v3_row("evt1", "case_0001", float("nan"), 100,
                                [_v3_donor()])


def test_schema3_preserves_negative_live_observation_for_service_guard():
    donor = _v3_donor()
    donor.update(aa_state="FOREGROUND", window_visible=True, lifecycle_clear=False)
    row = _scene4_schedule_v3_row("evt1", "case_0001", 130.0, 100, [donor])
    assert row[6 + 10] == "FOREGROUND"
    assert row[6 + 12] == "true"
    assert row[6 + 18] == "false"


def test_schema3_os_snapshot_parses_two_pid_proc_and_backing_file_blocks(monkeypatch):
    donors = [
        ("io.memscene.scene4.workload.a1", 4401, 20020191, "123451"),
        ("io.memscene.scene4.workload.a2", 4402, 20020192, "123452"),
    ]
    def stat_line(pid, bundle, ticks):
        fields = ["0"] * 22
        fields[18] = ticks
        fields[19] = "8192"
        fields[20] = "64"
        return f"{pid} ({bundle}) S " + " ".join(fields)

    pieces = ["__S3_BOOT_PRE__", "110.0 0.0", "__S3_AA__"]
    for bundle, pid, uid, ticks in donors:
        pieces.extend([
            "AppRunningRecord", f"process name: [{bundle}]", f"pid: {pid}",
            f"uid: {uid}", "state: BACKGROUND",
        ])
    pieces.extend(["__S3_WMS__", "fixture WMS inventory"])
    for index, (bundle, pid, uid, ticks) in enumerate(donors):
        pieces.extend([
            f"__S3_PROC_{index}_BEGIN__", stat_line(pid, bundle, ticks),
            f"__S3_STATUS_{index}__", f"Name:\t{bundle}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}",
            f"__S3_STAT2_{index}__", stat_line(pid, bundle, ticks),
            f"__S3_CMD_{index}__", f"/data/app/{bundle}",
            f"__S3_CGROUP_{index}__", f"0::/dev/memcg/100/{bundle}_{uid}",
            f"__S3_MEMBERS_{index}__", str(pid),
            f"__S3_FILE_{index}__", f"104|{182760 + index}|1258291200",
            f"__S3_PROC_{index}_END__",
        ])
    pieces.extend(["__S3_BOOT_POST__", "110.25 0.0"])

    class Device:
        def shell(self, _script, **_kwargs):
            return "\n".join(pieces)

    class Session:
        device = Device()

    monkeypatch.setattr(runner_module, "_scene4_wms_window_inventory",
                        lambda _raw: {"complete": True, "rows": []})
    daily = {bundle: [{"event": "daily_ready", "pid": pid, "uid": uid,
                       "start_ticks": ticks, "monotonic_ns": 100_000_000_000}]
             for bundle, pid, uid, ticks in donors}
    result = runner_module._scene4_legacy_os_snapshot(Session(), "case_0001", daily,
                                                       [row[0] for row in donors])

    assert result["complete"] is True, {bundle: {key: value for key, value in row.items()
        if key in ("identity_verified", "error", "pid", "uid", "start_ticks", "file_dev", "file_ino", "file_size")}
        for bundle, row in result["donors"].items()}
    assert set(result["donors"]) == {row[0] for row in donors}
    for bundle, pid, uid, ticks in donors:
        donor = result["donors"][bundle]
        assert donor["identity_verified"] is True
        assert donor["pid"] == pid and donor["uid"] == uid
        assert str(donor["start_ticks"]) == ticks
        assert donor["file_dev"] == 104
        assert donor["file_ino"] > 0 and donor["file_size"] == 1258291200
