import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from memscenes.model import EventBook


BUNDLE = "io.memscene.scene4.workload.a8"
UID = 20020195
PID = 808
TICKS = "14193645"
CASE = "c0001"


def journal(*, ready_ns=None, peak_ns=30_000_000_000, uid=UID,
            ticks=TICKS, case_id=CASE, extra=()):
    rows = [{"event": "peak_begin", "case_id": case_id, "pid": PID,
             "uid": uid, "start_ticks": ticks, "boottime_ns": peak_ns}]
    if ready_ns is not None:
        rows.append({"event": "cold_start_ready", "case_id": case_id,
                     "pid": PID, "uid": uid, "start_ticks": ticks,
                     "boottime_ns": ready_ns})
    rows.extend(extra)
    return rows


def dmesg(comm=None, pid=PID, victim_s=50.0, exit_s=50.1,
          victim_uid=None):
    del victim_uid  # UID is proven by the matching case journal, not kernel comm.
    comm = comm or BUNDLE[-14:]
    return "\n".join([
        f"[10.000000] MEMSCENES_CASE_BEGIN session=s case_id={CASE} uptime_ms=10000 wall_ms=100000",
        f"[{victim_s:.6f}] [mem_oom:522] out of Memory(OOM), start to kill process < {comm}> (pid: {pid}, rss 99, drv 0, adj 0)",
        f"[{exit_s:.6f}] sysmgr process has exited, pid={pid}, resource_refcnt=1",
        "[110.000000] MEMSCENES_CASE_END session=s case_id=c0001 uptime_ms=110000 wall_ms=200000",
    ])


def parse(rows, *, raw=None, observed=None, complete=True, journal_case_id=CASE,
          startup_eligible=True):
    book = EventBook()
    result = book.kernel_dmesg_oom_victims(
        raw or dmesg(), CASE, observed or {}, {BUNDLE: UID}, {BUNDLE: rows},
        10.0, 110.0, 100000, 10.0, "kernel.log", scene4_cold_only=True,
        journal_readback_complete_bundles={BUNDLE} if complete else set(),
        journal_case_ids={BUNDLE: journal_case_id},
        startup_failure_bundles={BUNDLE} if startup_eligible else set())
    return book, result


def test_short_lived_pre_ready_target_uses_complete_journal_identity_and_dual_classifies():
    book, result = parse(journal())
    assert result["coverage_complete"] is True
    assert result["victims_detected"] == 1
    victim = result["victims"][0]
    assert (victim["bundle"], victim["uid"], victim["pid"], victim["start_ticks"]) == (
        BUNDLE, UID, PID, TICKS)
    assert victim["readiness_relation"] == "pre_ready"
    assert victim["startup_failure_eligible"] is True
    assert len(book.events) == 1  # OOM evidence; caller adds the linked startup classification.


def test_same_generation_ready_before_kill_is_oom_only():
    _book, result = parse(journal(ready_ns=40_000_000_000))
    assert result["victims_detected"] == 1
    victim = result["victims"][0]
    assert victim["readiness_relation"] == "post_ready"
    assert victim["startup_failure_eligible"] is False


def test_invalid_or_pre_peak_ready_timestamp_is_unknown():
    for ready_ns in (0, -1, 29_000_000_000):
        _book, result = parse(journal(ready_ns=ready_ns))
        assert result["victims_detected"] == 0


def test_non_decimal_or_nonpositive_start_ticks_is_unknown():
    for ticks in ("0", "-4", "tick-7"):
        _book, result = parse(journal(ticks=ticks))
        assert result["victims_detected"] == 0


def test_scene4_donor_journal_case_is_independent_and_never_startup_failure():
    donor_case = "c0001_donor_a"
    donor_rows = journal(case_id=donor_case)
    _book, result = parse(donor_rows, journal_case_id=donor_case,
                          startup_eligible=False)
    assert result["victims_detected"] == 1
    victim = result["victims"][0]
    assert victim["readiness_relation"] == "pre_ready"
    assert victim["startup_failure_eligible"] is False


def test_partial_or_failed_journal_readback_cannot_confirm_owned_victim():
    _book, result = parse(journal(), complete=False)
    assert result["victims_detected"] == 0


def test_case_uid_or_start_ticks_mismatch_cannot_bind_truncated_comm():
    for rows, observed in (
        (journal(case_id="different_case"), {}),
        (journal(uid=UID + 1), {}),
        (journal(ticks=str(int(TICKS) + 1)),
         {PID: [{"bundle": BUNDLE, "uid": UID, "start_ticks": TICKS,
                 "first_uptime": 20.0, "first_absent_uptime": 60.0}]}),
    ):
        _book, result = parse(rows, observed=observed)
        assert result["victims_detected"] == 0


def test_pid_generation_conflict_or_observed_pid_reuse_fails_closed():
    conflicting = journal(extra=[{"event": "peak_begin", "case_id": CASE,
        "pid": PID, "uid": UID, "start_ticks": str(int(TICKS) + 1),
        "boottime_ns": 31_000_000_000}])
    _book, result = parse(conflicting)
    assert result["victims_detected"] == 0

    reused = {PID: [{"bundle": BUNDLE, "uid": UID, "start_ticks": "999999",
                     "first_uptime": 20.0, "first_absent_uptime": 60.0}]}
    _book, result = parse(journal(), observed=reused)
    assert result["victims_detected"] == 0


def test_kernel_kill_before_peak_attempt_is_not_linked():
    _book, result = parse(journal(peak_ns=60_000_000_000))
    assert result["victims_detected"] == 0


def test_malformed_journal_identity_is_unknown_not_exception_or_owned_oom():
    malformed = journal(extra=[{"event": "allocation_progress", "case_id": CASE,
        "pid": "bad-pid", "uid": UID, "start_ticks": TICKS,
        "boottime_ns": 35_000_000_000}])
    _book, result = parse(malformed)
    assert result["victims_detected"] == 0

    malformed_peak = [{"event": "peak_begin", "case_id": CASE,
        "pid": "bad-pid", "uid": UID, "start_ticks": TICKS,
        "boottime_ns": 30_000_000_000}]
    _book, result = parse(malformed_peak)
    assert result["victims_detected"] == 0


def test_default_legacy_policy_does_not_accept_pre_ready_journal_fallback():
    book = EventBook()
    result = book.kernel_dmesg_oom_victims(
        dmesg(), CASE, {}, {BUNDLE: UID}, {BUNDLE: journal()},
        10.0, 110.0, 100000, 10.0, "kernel.log")
    assert result["victims_detected"] == 0
    assert book.events == []


def peak_profile_rows(*, stage, case_id=CASE):
    start = {"event": "startup_begin", "case_id": case_id, "pid": PID,
             "uid": UID, "start_ticks": TICKS, "boottime_ns": 20_000_000_000}
    daily = [start]
    peak = []
    if stage != "pre_ready":
        daily.append({"event": "daily_ready", "case_id": case_id, "pid": PID,
                      "uid": UID, "start_ticks": TICKS, "boottime_ns": 30_000_000_000})
    if stage in ("peak_allocating", "post_peak"):
        peak.append({"event": "peak_begin", "case_id": case_id, "pid": PID,
                     "uid": UID, "start_ticks": TICKS, "boottime_ns": 45_000_000_000})
    if stage == "post_peak":
        peak.append({"event": "peak_reached", "case_id": case_id, "pid": PID,
                     "uid": UID, "start_ticks": TICKS, "boottime_ns": 49_000_000_000})
    # A different later PID must not shadow the exact generation being killed.
    daily.append({"event": "startup_begin", "case_id": case_id, "pid": PID + 1,
                  "uid": UID, "start_ticks": str(int(TICKS) + 1),
                  "boottime_ns": 60_000_000_000})
    return daily, peak


@pytest.mark.parametrize(("stage", "expected_failure", "expected_stage"), [
    ("pre_ready", 1, "pre_ready"),
    ("before_promote", 0, "before_promote"),
    ("peak_allocating", 1, "peak_allocating"),
    ("post_peak", 0, "post_peak"),
])
def test_peak_transition_oom_classification_uses_actual_promote_and_peak_events(
        stage, expected_failure, expected_stage):
    daily, peak = peak_profile_rows(stage=stage)
    book = EventBook()
    result = book.kernel_dmesg_oom_victims(
        dmesg(victim_s=50.05, exit_s=50.1), CASE, {}, {BUNDLE: UID},
        {BUNDLE: peak}, 10.0, 110.0, 100000, 10.0, "kernel.log",
        scene4_peak_transition=True, daily_attempt_rows={BUNDLE: daily},
        daily_journal_readback_complete_bundles={BUNDLE},
        journal_readback_complete_bundles={BUNDLE})
    assert result["victims_detected"] == 1
    assert result["victims"][0]["peak_transition_stage"] == expected_stage
    assert sum(event["category"] == "oom_kill" for event in book.events) == 1
    assert sum(event["category"] == "startup_failure" for event in book.events) == expected_failure
