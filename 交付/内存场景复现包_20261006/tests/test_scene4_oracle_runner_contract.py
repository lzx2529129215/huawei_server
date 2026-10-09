import shlex
import inspect
import json
import hashlib
from pathlib import Path
import pytest
from memscenes import runner as scene4_runner

from memscenes.runner import (
    _scene4_donor_validation_complete,
    _scene4_daemon_ps_rows,
    _scene4_hold_coverage,
    _scene4_finalize_target_hold_journals,
    _scene4_invalidate_schedule,
    _scene4_ledger_completeness,
    _scene4_timing_v3_completeness,
    SCENE4_TIMING_V3_HEADER,
    _scene4_should_end_counter_window,
    _scene4_ui_evidence_proof,
    _scene4_unique_incident_ids,
    _scene4_all_ui_evidence_complete,
    _scene4_minimize_button,
    _scene4_bundle_window_visibility,
    _scene4_focused_bundles,
    _scene4_ui_nodes,
    _scene4_primary_count_snapshot,
    _scene4_aa_running_record_state,
    _scene4_minimize_donor,
    _scene4_lifecycle_fresh,
    _scene4_parse_target_process_inventory,
    _scene4_target_start_ticks_gate,
    _scene4_qualifier_failure_is_predeadline,
    _scene4_clock_probe,
    _scene4_dynamic_donor_fields,
    _scene4_wms_window_snapshot,
    _scene4_wms_window_inventory,
    _scene4_dump_layout_for_window,
    _scene4_layout_identity_proof,
    _scene4_content_freshness,
    _scene4_global_layout_coverage,
    _scene4_dump_layout_global_visible,
    _scene4_ui_candidate_plan,
    _scene4_wms_details_batch,
    _scene4_verified_app_window_change,
    _scene4_proc_identity_inventory,
    _scene4_daemon_proc_sample,
    _scene4_system_sample,
    _scene4_parse_marker_helper_output,
    _scene4_marker_raw_correlation,
    _scene4_write_checkpoint,
    _scene4_schedule_header,
    _scene4_case_plan,
    _scene4_probe_mode,
    _scene4_frame_manifest_proof,
    _scene4_readonly_observation_proof,
    _scene4_observer_aa_inventory_complete,
    _scene4_observer_mission_inventory_complete,
    _scene4_marker_payload,
    _scene4_frame_capture_result,
    _scene4_advance_frame_slot,
    _scene4_frame_identity_snapshot,
    _scene4_revalidate_frame_manifest_files,
    reconcile_scene4_frame_reviews,
    _scene4_read_journal,
    _scene4_donor_ready_identity,
    _scene4_native_popup_candidate,
    _scene4_screenshot_needed,
    _scene4_test_dialog_bundle,
    _scene4_test_popup_journal_needed,
    _scene4_journal_popup_signals,
    _scene4_cumulative_counts_observed,
    _scene4_finalize_cumulative_counts,
    _scene4_primary_count_snapshot,
    SCENE4_V2_DONOR_SUFFIXES,
    SCENE4_V3_DONOR_SUFFIXES,
    _scene4_schedule_v2_header,
    _scene4_schedule_v2_row,
    _scene4_schedule_v3_header,
    _scene4_schedule_v3_row,
)


def _frame_review_pair(slot, pre, post, classification="confirmed_not_native_low_memory",
                       case_id="c1", candidate=None):
    frame_id = f"session:{case_id}:{slot:06d}"
    digest = f"{slot + 1:064x}"
    frame = {"case_id": case_id, "frame_id": frame_id, "slot_index": slot,
        "slot_expected_boottime_s": pre, "capture_boottime_pre_s": pre,
        "capture_boottime_post_s": post, "capture_host_start_ns": int(pre*1e9),
        "capture_host_end_ns": int(post*1e9), "transfer_host_start_ns": int(post*1e9),
        "transfer_host_end_ns": int((post+.01)*1e9), "capture_status": "captured",
        "transfer_status": "received", "capture_clock_valid": True,
        "frame_sha256": digest, "native_identity_candidates": [candidate] if candidate else []}
    review = {"case_id": case_id, "frame_id": frame_id, "frame_sha256": digest,
        "capture_boottime_pre_s": pre, "capture_boottime_post_s": post,
        "review_classification": classification,
        "visual_modal_confirmed": classification == "confirmed_native_low_memory",
        "full_screen_reviewed": classification == "confirmed_not_native_low_memory",
        "reviewer": "test-reviewer", "reviewed_at": "2026-10-08T12:00:00Z"}
    if classification == "confirmed_native_low_memory" and candidate:
        review.update(pid=candidate["pid"], uid=candidate["uid"],
            start_ticks=candidate["start_ticks"], win_id=candidate["win_id"],
            mission_id=candidate["mission_id"])
    return frame, review


def _scene4_capture_clock_raw(slot):
    marker = f"__S4_CAP_{slot:06d}_"
    return (f"{marker}CLOCK_PRE__\nboottime_s=10.0 monotonic_s=10.0 realtime_s=100.0 "
        f"proc_uptime_s=10.0 clk_tck=100 page_size_bytes=4096\n{marker}EXIT__0\n"
        f"{marker}CLOCK_POST__\nboottime_s=10.1 monotonic_s=10.1 realtime_s=100.1 "
        "proc_uptime_s=10.1 clk_tck=100 page_size_bytes=4096\n")


def test_scene4_frame_review_requires_exact_case_sha_clock_and_fullscreen_negative():
    pairs = [_frame_review_pair(i, 100+i*2, 100+i*2+.7) for i in range(4)]
    frames, reviews = [x[0] for x in pairs], [x[1] for x in pairs]
    proof = _scene4_frame_manifest_proof(frames, reviews, 100, 108, case_id="c1")
    assert proof["complete"]
    assert proof["maximum_observed_gap_seconds"] == pytest.approx(2.7)

    wrong_case = [dict(row) for row in reviews]
    wrong_case[1]["case_id"] = "c2"
    assert not _scene4_frame_manifest_proof(frames, wrong_case, 100, 108, case_id="c1")["complete"]
    bad_sha = [dict(row) for row in reviews]
    bad_sha[1]["frame_sha256"] = "nope"
    assert not _scene4_frame_manifest_proof(frames, bad_sha, 100, 108, case_id="c1")["complete"]
    bad_clock = [dict(row) for row in reviews]
    bad_clock[1]["capture_boottime_pre_s"] = float("nan")
    assert not _scene4_frame_manifest_proof(frames, bad_clock, 100, 108, case_id="c1")["complete"]
    nan_frame = [dict(row) for row in frames]
    nan_frame[1]["capture_boottime_post_s"] = float("nan")
    assert not _scene4_frame_manifest_proof(nan_frame, reviews, 100, 108, case_id="c1")["complete"]
    missing_id = [dict(row) for row in frames]
    missing_id[2]["frame_id"] = ""
    assert not _scene4_frame_manifest_proof(missing_id, reviews, 100, 108, case_id="c1")["complete"]
    not_fullscreen = [dict(row) for row in reviews]
    not_fullscreen[1]["full_screen_reviewed"] = False
    assert not _scene4_frame_manifest_proof(frames, not_fullscreen, 100, 108, case_id="c1")["complete"]
    # An absent AA/WMS candidate is not a negative unless a person reviewed the full frame.
    pending = [dict(row) for row in reviews]
    pending[0].update(review_classification="pending_review", full_screen_reviewed=False)
    assert not _scene4_frame_manifest_proof(frames, pending, 100, 108, case_id="c1")["complete"]


def test_scene4_frame_window_rejects_boundary_overlap_duplicate_or_out_of_order_sequence():
    pairs = [_frame_review_pair(i, 100+i*2, 100+i*2+.7) for i in range(4)]
    frames, reviews = [x[0] for x in pairs], [x[1] for x in pairs]
    cross = [dict(row) for row in frames]
    cross[0]["capture_boottime_pre_s"] = 99.9
    begin_cross_proof = _scene4_frame_manifest_proof(cross, reviews, 100, 108, case_id="c1")
    assert begin_cross_proof["complete"]
    assert begin_cross_proof["supplemental_cross_boundary_count"] == 1
    duplicate = [dict(row) for row in frames]
    duplicate[1]["slot_index"] = duplicate[0]["slot_index"]
    assert not _scene4_frame_manifest_proof(duplicate, reviews, 100, 108, case_id="c1")["complete"]
    out_of_order = [frames[1], frames[0], *frames[2:]]
    assert not _scene4_frame_manifest_proof(out_of_order, reviews, 100, 108, case_id="c1")["complete"]
    wrong_frame_case = [dict(row) for row in frames]
    wrong_frame_case[1]["case_id"] = "c2"
    assert not _scene4_frame_manifest_proof(wrong_frame_case, reviews, 100, 108, case_id="c1")["complete"]
    long_capture = [dict(row) for row in frames]
    long_capture[0]["capture_boottime_post_s"] = 108.1
    assert not _scene4_frame_manifest_proof(long_capture, reviews, 100, 108, case_id="c1")["complete"]
    long_span_even_without_intercapture_gap = [dict(row) for row in frames]
    long_span_even_without_intercapture_gap[0]["capture_boottime_post_s"] = 108
    long_span_even_without_intercapture_gap[1]["capture_boottime_pre_s"] = 108
    assert not _scene4_frame_manifest_proof(long_span_even_without_intercapture_gap,
        reviews, 100, 110, case_id="c1")["complete"]


def test_scene4_metric_end_crossings_are_supplemental_and_never_counted():
    pairs = [_frame_review_pair(0, 100, 100.7),
             _frame_review_pair(1, 102, 102.7),
             _frame_review_pair(2, 104, 105, "confirmed_not_native_low_memory"),
             _frame_review_pair(3, 106, 106.7)]
    proof = _scene4_frame_manifest_proof([x[0] for x in pairs], [x[1] for x in pairs],
        100, 104.5, case_id="c1", collection_end_boottime_s=107)
    assert proof["complete"]
    assert proof["frame_ids"] == [pairs[0][0]["frame_id"], pairs[1][0]["frame_id"]]
    assert proof["supplemental_frame_ids"] == [pairs[2][0]["frame_id"], pairs[3][0]["frame_id"]]
    assert proof["supplemental_cross_boundary_count"] == 1
    assert not pairs[2][0].get("counted_in_metric_window", False)
    assert proof["native_popup_events"] == []


def test_scene4_frame_native_events_require_exact_visible_identity_and_dedup_episodes():
    identity = {"complete": True, "bundle": "com.huawei.hmos.taskmanager",
        "ability": "LowMemoryAbility", "mission_id": "822", "mission_start_boottime_s": 100.05,
        "visible_episode_start_boottime_s": 100.05,
        "pid": 51731, "uid": 20020188, "start_ticks": "898", "win_id": 696,
        "visible": True, "focused": False, "baseline_visible": False,
        "new_visible_episode": True, "identity_boottime_pre_s": 100.8,
        "identity_boottime_post_s": 100.9}
    identity1 = dict(identity, identity_boottime_pre_s=102.8, identity_boottime_post_s=102.9,
                     mission_start_boottime_s=100.05, visible_episode_start_boottime_s=102.1)
    identity3 = dict(identity, identity_boottime_pre_s=106.8, identity_boottime_post_s=106.9,
                     mission_start_boottime_s=100.05, visible_episode_start_boottime_s=106.1)
    pairs = [_frame_review_pair(0, 100, 100.7, "confirmed_native_low_memory", candidate=identity),
             _frame_review_pair(1, 102, 102.7, "confirmed_native_low_memory", candidate=identity1),
             _frame_review_pair(2, 104, 104.7),
             _frame_review_pair(3, 106, 106.7, "confirmed_native_low_memory", candidate=identity3)]
    proof = _scene4_frame_manifest_proof([x[0] for x in pairs], [x[1] for x in pairs],
        100, 108, case_id="c1")
    assert proof["complete"]
    assert len(proof["native_popup_events"]) == 2
    assert proof["native_popup_events"][0]["visible_episode_start_boottime_s"] == 100.05
    assert proof["native_popup_events"][1]["visible_episode_start_boottime_s"] == 106.1
    assert all(row["focused"] is False for row in proof["native_popup_events"])
    wrong_identity = dict(identity, pid=51732)
    bad_pair = _frame_review_pair(0, 100, 100.7, "confirmed_native_low_memory", candidate=wrong_identity)
    bad_pair[1]["pid"] = 51731
    assert not _scene4_frame_manifest_proof([bad_pair[0]], [bad_pair[1]], 100, 102,
        case_id="c1")["complete"]
    recycled = dict(identity1, start_ticks="899")
    pair2 = _frame_review_pair(1, 102, 102.7, "confirmed_native_low_memory", candidate=recycled)
    recycled_proof = _scene4_frame_manifest_proof([pairs[0][0], pair2[0]],
        [pairs[0][1], pair2[1]], 100, 104, case_id="c1")
    assert recycled_proof["complete"] and len(recycled_proof["native_popup_events"]) == 2


def test_scene4_unknown_gap_does_not_inflate_same_popup_episode_and_bad_slot_fails_closed():
    identity = {"complete": True, "bundle": "com.huawei.hmos.taskmanager",
        "ability": "LowMemoryAbility", "mission_id": "822", "mission_start_boottime_s": 100.05,
        "visible_episode_start_boottime_s": 100.05, "pid": 51731, "uid": 20020188,
        "start_ticks": "898", "win_id": 696, "visible": True, "focused": False,
        "baseline_visible": False, "new_visible_episode": True,
        "identity_boottime_pre_s": 100.8, "identity_boottime_post_s": 100.9}
    later = dict(identity, identity_boottime_pre_s=104.8, identity_boottime_post_s=104.9,
        visible_episode_start_boottime_s=104.1)
    first = _frame_review_pair(0, 100, 100.7, "confirmed_native_low_memory", candidate=identity)
    last = _frame_review_pair(2, 104, 104.7, "confirmed_native_low_memory", candidate=later)
    proof = _scene4_frame_manifest_proof([first[0], last[0]], [first[1], last[1]],
        100, 108, case_id="c1")
    assert not proof["complete"]
    assert len(proof["native_popup_events"]) == 1
    pairs = [_frame_review_pair(i, 100+i*2, 100+i*2+.7) for i in range(4)]
    pairs[1][0]["slot_index"] = None
    bad = _scene4_frame_manifest_proof([x[0] for x in pairs], [x[1] for x in pairs],
        100, 108, case_id="c1")
    assert not bad["complete"]
    assert any("frame_slot_invalid" in error for error in bad["errors"])


def _observer_row(index, pre, post, identity="no_aa_low_memory_candidate", candidates=None):
    commands = ("wms_pre", "aa_pre", "missions_pre", "wms_post", "aa_post", "missions_post")
    return {"case_id": "c1", "observation_index": index,
        "boottime_pre_s": pre, "boottime_post_s": post,
        "identity_status": identity, "native_identity_candidates": candidates or [],
        "command_status": {name: {"exit_code": 0, "complete": True,
            "inventory_complete": True} for name in commands}}


def test_scene4_observer_covers_capture_gaps_but_requires_visual_episode_binding():
    identity = {"complete": True, "bundle": "com.huawei.hmos.taskmanager",
        "ability": "LowMemoryAbility", "mission_id": "m1", "mission_start_boottime_s": 102.05,
        "visible_episode_start_boottime_s": 102.05, "pid": 51731, "uid": 20020188,
        "start_ticks": "898", "win_id": 696, "visible": True, "focused": False,
        "new_visible_episode": True, "baseline_visible": False,
        "identity_boottime_pre_s": 102.8, "identity_boottime_post_s": 102.9}
    frames_reviews = [_frame_review_pair(0, 100, 100.7, "pending_review"),
        _frame_review_pair(1, 102, 102.7, "confirmed_native_low_memory", candidate=identity),
        _frame_review_pair(2, 104, 104.7, "pending_review"),
        _frame_review_pair(3, 106, 106.7, "pending_review")]
    # The missing human reviews on blank frames are covered by the complete
    # observer stream; the native episode still needs one SHA-bound positive.
    observations = [_observer_row(0, 100, 100.2),
        _observer_row(1, 102, 102.2, "identity_candidate_observed", [identity]),
        _observer_row(2, 104, 104.2, "identity_candidate_observed", [identity]),
        _observer_row(3, 106, 106.2)]
    proof = _scene4_frame_manifest_proof([pair[0] for pair in frames_reviews],
        [pair[1] for pair in frames_reviews], 100, 108, case_id="c1",
        readonly_observations=observations)
    assert proof["complete"]
    assert len(proof["native_popup_events"]) == 1

    no_visual = _scene4_frame_manifest_proof([pair[0] for pair in frames_reviews],
        [dict(pair[1], review_classification="pending_review") for pair in frames_reviews],
        100, 108, case_id="c1", readonly_observations=observations)
    assert not no_visual["complete"]
    assert any("missing_in-episode_visual_confirmation" in err for err in no_visual["errors"])


def test_scene4_observer_missing_status_partial_candidate_reuse_and_boundary_stay_unknown():
    valid = [_observer_row(i, 100 + i * 2, 100.2 + i * 2) for i in range(5)]
    missing_rc = [dict(row) for row in valid]
    missing_rc[2] = dict(missing_rc[2], command_status={**missing_rc[2]["command_status"],
        "wms_post": {"exit_code": None, "complete": False}})
    assert not _scene4_readonly_observation_proof(missing_rc, 100, 108, "c1", 5)["complete"]

    bad_identity = {"complete": False, "mission_id": "m1", "win_id": 7,
        "pid": 10, "uid": 20, "start_ticks": "30", "visible": True}
    partial = list(valid)
    partial[1] = _observer_row(1, 102, 102.2, "identity_candidate_observed_partial", [bad_identity])
    assert not _scene4_readonly_observation_proof(partial, 100, 108, "c1", 5)["complete"]

    # A verified negative observation closes the episode. Same PID/WinId with
    # a different start_ticks is a new process generation, never deduped.
    first_id = {"complete": True, "mission_id": "m1", "win_id": 7,
        "pid": 10, "uid": 20, "start_ticks": "30", "visible": True}
    second_id = dict(first_id, start_ticks="31")
    rows = [_observer_row(0, 100, 100.2),
        _observer_row(1, 102, 102.2, "identity_candidate_observed", [first_id]),
        _observer_row(2, 104, 104.2),
        _observer_row(3, 106, 106.2, "identity_candidate_observed", [second_id])]
    proof = _scene4_readonly_observation_proof(rows, 100, 108, "c1", 5)
    assert proof["complete"]
    assert len(proof["visible_native_episodes"]) == 2
    assert proof["visible_native_episodes"][0]["identity_key"] != proof["visible_native_episodes"][1]["identity_key"]

    crossed = [_observer_row(0, 99.9, 100.1), *valid[1:4]]
    crossed_proof = _scene4_readonly_observation_proof(crossed, 100, 108, "c1", 5)
    assert crossed_proof["complete"]
    assert crossed_proof["supplemental_cross_boundary_count"] == 1


def test_scene4_observer_requires_nonempty_complete_inventory_for_negative_and_rejects_overlap():
    empty_inventory = [_observer_row(i, 100 + i * 2, 100.2 + i * 2) for i in range(5)]
    empty_inventory[2]["command_status"]["aa_pre"]["inventory_complete"] = False
    assert not _scene4_readonly_observation_proof(empty_inventory, 100, 108, "c1", 5)["complete"]

    # A verified visible candidate may remain a positive despite a global WMS
    # count mismatch; the per-window identity is independently complete.
    identity = {"complete": True, "mission_id": "m1", "win_id": 7,
        "pid": 10, "uid": 20, "start_ticks": "30", "visible": True}
    positive = [_observer_row(i, 100 + i * 2, 100.2 + i * 2) for i in range(5)]
    positive[1] = _observer_row(1, 102, 102.2, "identity_candidate_observed", [identity])
    positive[1]["command_status"]["wms_pre"]["inventory_complete"] = False
    assert _scene4_readonly_observation_proof(positive, 100, 108, "c1", 5)["complete"]

    overlapping = [_observer_row(0, 100, 102), _observer_row(1, 101.9, 102.2),
        *[_observer_row(i, 102 + (i - 2) * 2, 102.2 + (i - 2) * 2) for i in range(2, 5)]]
    assert not _scene4_readonly_observation_proof(overlapping, 100, 108, "c1", 5)["complete"]
    assert any("observer_interval_overlap" in error for error in
        _scene4_readonly_observation_proof(overlapping, 100, 108, "c1", 5)["errors"])


def test_scene4_reconcile_requires_pinned_observer_jsonl_sha(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    review = tmp_path / "review.jsonl"
    observations = tmp_path / "observer.jsonl"
    manifest.write_text("{}\n", encoding="utf-8")
    review.write_text("{}\n", encoding="utf-8")
    observations.write_text("{}\n", encoding="utf-8")
    result = _scene4_revalidate_frame_manifest_files(manifest, review, 100, 108, "c1",
        readonly_observations_path=observations, readonly_observations_sha256="0" * 64)
    assert not result["complete"]
    assert result["errors"] == ["readonly_observations_sha256_missing_or_mismatch"]


def test_scene4_observer_requires_structural_aa_and_mission_inventory():
    assert not _scene4_observer_aa_inventory_complete("successful but empty output")
    assert not _scene4_observer_aa_inventory_complete(
        "AppRunningRecords:\nAppRunningRecord ID #0\n process name [pkg]")
    assert _scene4_observer_aa_inventory_complete(
        "AppRunningRecords:\nAppRunningRecord ID #0\n  process name [pkg.a]\n"
        "  pid #10  uid #20\n  state #BACKGROUND\n")

    assert not _scene4_observer_mission_inventory_complete("User ID #100\ncurrent mission lists:{")
    assert _scene4_observer_mission_inventory_complete("User ID #100\n  current mission lists:{\n }")
    assert not _scene4_observer_mission_inventory_complete(
        "User ID #100\n current mission lists:{\n Mission ID #1 LowMemoryAbility "
        "com.huawei.hmos.taskmanager\n }")
    # This device's TaskManager mission listing has no PID field; identity is
    # supplied and checked independently by AA/WMS/proc observations.
    assert _scene4_observer_mission_inventory_complete(
        "User ID #100\n current mission lists:{\n"
        " Mission ID #1 mission name #[#com.huawei.hmos.taskmanager:manager:LowMemoryAbility]\n"
        "   main name [LowMemoryAbility]\n"
        "   bundle name [com.huawei.hmos.taskmanager]\n"
        "   state #FOREGROUND  start time [101000]\n }")


def test_scene4_frame_clock_marker_payload_handles_single_line_helper_output():
    raw = "before\n__S4_FRAME_CLOCK_POST__\nboottime_s=8191.2 monotonic_s=8191.2 clk_tck=100\n__AFTER__"
    assert _scene4_marker_payload(raw, "__S4_FRAME_CLOCK_POST__", "__AFTER__") == \
        "boottime_s=8191.2 monotonic_s=8191.2 clk_tck=100"


def test_scene4_capture_parser_accepts_saved_device_raw_and_rejects_bad_markers():
    raw = (Path(__file__).parent / "fixtures" / "scene4_frame_capture_saved_exit0_raw.txt").read_text(
        encoding="utf-8")
    parsed = _scene4_frame_capture_result(raw, "__S4_CAP_000000_")
    assert parsed["valid"] is True
    assert parsed["exit_code"] == 0
    assert parsed["pre_boottime_s"] == pytest.approx(47816.590882969)
    assert parsed["post_boottime_s"] == pytest.approx(47817.206185313)
    assert _scene4_frame_capture_result(raw.replace("__S4_CAP_000000_CLOCK_POST__", ""),
        "__S4_CAP_000000_")["valid"] is False
    assert _scene4_frame_capture_result(raw.replace("__S4_CAP_000000_EXIT__0", "__S4_CAP_000000_EXIT__1"),
        "__S4_CAP_000000_")["valid"] is False
    assert _scene4_frame_capture_result(raw.replace("boottime_s=47817.206185313", "boottime_s=47816.0"),
        "__S4_CAP_000000_")["valid"] is False


def test_scene4_capture_worker_advances_slot_once_for_success_and_failures():
    worker_source = inspect.getsource(scene4_runner.Session.start_scene4_frame_capture)
    append_at = worker_source.index("append_row(row, review)")
    advance = "slot = _scene4_advance_frame_slot(slot)"
    assert worker_source.count(advance) == 1
    advance_at = worker_source.index(advance)
    assert append_at < advance_at < worker_source.index("if not captured:", advance_at)
    assert "slot += 1" not in worker_source

    # Both a successful transfer and returned failures consume one slot.
    slot = 0
    frame_ids = []
    for _outcome in ("received", "capture_failed", "received"):
        frame_ids.append(f"frame_{slot:06d}")
        slot = _scene4_advance_frame_slot(slot)
    assert frame_ids == ["frame_000000", "frame_000001", "frame_000002"]
    assert len(set(frame_ids)) == 3


def test_scene4_capture_timeout_keeps_readonly_observations_without_overlapping_screencap():
    source = inspect.getsource(scene4_runner.Session.start_scene4_frame_capture)
    assert 'readonly_observation_worker' in source
    assert source.index('state["readonly_observation_worker"] = observation') < \
        source.index('state["worker"] = thread')
    timeout_branch = source.index('if capture_error:')
    timeout_end = source.index('break', timeout_branch)
    branch = source[timeout_branch:timeout_end]
    assert 'observation.start()' not in branch
    assert timeout_end < source.index('if row.get("transfer_status")', timeout_branch)
    observer_start = source.index('def readonly_observation_worker()')
    observer_end = source.index('def worker()', observer_start)
    observer = source[observer_start:observer_end]
    assert 'hidumper -s WindowManagerService' in observer
    assert 'aa dump -r' in observer
    assert 'aa dump -l' in observer
    assert '_scene4_frame_identity_snapshot(self, identity_frame' in observer
    assert '"visual_frame": None' in observer
    # On timeout, no replacement screenCap is issued; the capture loop exits
    # while the separate read-only observer remains active until case stop.
    assert 'screenCap -p' in source[:timeout_branch]
    assert 'screenCap -p' not in branch


def test_scene4_identity_snapshot_call_contract_accepts_frame_and_six_sections():
    class SessionStub:
        scene4_frame_clock_hash_verified = True
        config = {"scene4": {}}
        popup_baselines = {}

    result = _scene4_frame_identity_snapshot(SessionStub(), {"case_id": "c1", "slot_index": 0,
        "capture_raw": _scene4_capture_clock_raw(0)},
        "", "", "", "", "", "")
    assert result["status"] == "no_aa_low_memory_candidate"


def test_scene4_legacy_identity_path_verifies_clock_helper_once_without_global_preinit(monkeypatch):
    helper_hash = "a" * 64
    helper_path = "/data/local/tmp/clockprobe"
    class SessionStub:
        scene4_frame_clock_hash_verified = False
        config = {"scene4": {"oracle_clock_probe_remote_path": helper_path,
                              "oracle_clock_probe_sha256": helper_hash}}
        popup_baselines = {}
        result = {}
        class Device:
            def shell(self, command, **_kwargs):
                assert command == "sha256sum /data/local/tmp/clockprobe"
                return f"{helper_hash} {helper_path}\n"
        device = Device()

    session = SessionStub()
    result = _scene4_frame_identity_snapshot(session, {"case_id": "c1", "slot_index": 0,
        "capture_raw": _scene4_capture_clock_raw(0)}, "", "", "", "", "", "")
    assert session.scene4_frame_clock_hash_verified is True
    assert session.result["scene4_frame_clock_helper"]["readback_sha256"] == helper_hash
    assert result["status"] == "no_aa_low_memory_candidate"


def test_scene4_identity_path_keeps_clock_failure_unknown(monkeypatch):
    helper_path = "/data/local/tmp/clockprobe"
    class SessionStub:
        scene4_frame_clock_hash_verified = False
        config = {"scene4": {"oracle_clock_probe_remote_path": helper_path,
                              "oracle_clock_probe_sha256": "a" * 64}}
        popup_baselines = {}
        result = {}
        class Device:
            def shell(self, _command, **_kwargs):
                return "b" * 64 + " /data/local/tmp/clockprobe\n"
        device = Device()

    result = _scene4_frame_identity_snapshot(SessionStub(), {"case_id": "c1", "slot_index": 0,
        "capture_raw": _scene4_capture_clock_raw(0)}, "", "", "", "", "", "")
    assert result["status"] == "unknown_clock_helper_unverified"
    assert "clock_helper_hash_mismatch" in result["errors"][0]


def test_scene4_frame_review_jsonl_revalidation_is_sha_bound_and_auditable(tmp_path):
    pairs = [_frame_review_pair(i, 100+i*2, 100+i*2+.7) for i in range(4)]
    manifest = tmp_path / "frames.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    manifest.write_text("".join(json.dumps(pair[0]) + "\n" for pair in pairs), encoding="utf-8")
    reviews.write_text("".join(json.dumps(pair[1]) + "\n" for pair in pairs), encoding="utf-8")
    proof = _scene4_revalidate_frame_manifest_files(manifest, reviews, 100, 108, "c1")
    assert proof["complete"]
    rows = [pair[1] for pair in pairs]
    rows[0]["reviewer"] = ""
    reviews.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    proof = _scene4_revalidate_frame_manifest_files(manifest, reviews, 100, 108, "c1")
    assert not proof["complete"]
    assert any("frame_review_audit_identity_missing" in error for error in proof["errors"])


def test_scene4_frame_worker_starts_before_blocking_initial_schedule_refresh():
    source = inspect.getsource(scene4_runner.scene4)
    worker_start = source.index("frame_capture_state = None if donor_proof_only")
    initial_refresh = source.index("refresh_case_schedule(initial_registration=True)")
    assert worker_start < initial_refresh
    assert "donor_proof_only else session.start_scene4_frame_capture" in source[worker_start:initial_refresh]


def test_scene4_reader_monitor_records_death_and_durable_diagnostics(tmp_path, monkeypatch):
    class FakeProcess:
        pid = 4321
        def __init__(self):
            self.calls = 0
        def poll(self):
            self.calls += 1
            return None if self.calls < 2 else 137

    class FakeDevice:
        base = ["hdc", "-t", "test-device"]

    session = scene4_runner.Session.__new__(scene4_runner.Session)
    session.output = tmp_path
    session.kernel_dmesg_process = FakeProcess()
    session.kernel_dmesg_path = tmp_path / "kernel.raw"
    session.kernel_dmesg_path.write_bytes(b"raw-kernel-evidence")
    stderr_path = tmp_path / "reader.stderr"
    stderr_path.write_bytes(b"reader failure diagnostic")
    session.kernel_dmesg_reader = {"stderr": str(stderr_path), "health_checks": []}
    session.kernel_dmesg_stream = session.kernel_dmesg_path.open("ab", buffering=0)
    session.result = {}
    session.device = FakeDevice()
    session.scene4_reader_monitors = {}
    monkeypatch.setattr(scene4_runner.subprocess, "run", lambda *args, **kwargs:
        __import__("subprocess").CompletedProcess(args[0], 0, b"01234567-89ab-cdef-0123-456789abcdef\n"))
    monitor = session.start_scene4_reader_health_monitor("case-death", {})
    monitor["thread"].join(timeout=3)
    record = monitor["failure_record"]
    assert monitor["failed"].is_set()
    assert record["return_code"] == 137
    assert record["stderr_bytes"] == len(b"reader failure diagnostic")
    assert record["dmesg_raw_bytes"] == len(b"raw-kernel-evidence")
    assert record["hdc_device_probe"]["online"] is True
    assert record["hdc_device_probe"]["boot_id"] == "01234567-89ab-cdef-0123-456789abcdef"
    assert Path(record["death_checkpoint_path"]).is_file()
    session.kernel_dmesg_stream.close()


def test_scene4_offline_review_reconciliation_is_reproducible_and_keeps_original(tmp_path):
    cases = []
    for case_index in range(4):
        case_id = f"c{case_index + 1}"
        pairs = [_frame_review_pair(i, 100+i*2, 100+i*2+.7, case_id=case_id)
                 for i in range(4)]
        manifest = tmp_path / f"{case_id}_frames.jsonl"
        reviews_path = tmp_path / f"{case_id}_reviews.jsonl"
        for frame, _ in pairs:
            image = tmp_path / f"{frame['frame_id'].replace(':', '_')}.png"
            image.write_bytes(b"png" + frame["frame_id"].encode())
            frame["local_path"] = str(image)
            frame["frame_sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
        for frame, review in pairs:
            review["frame_sha256"] = frame["frame_sha256"]
        manifest.write_text("".join(json.dumps(pair[0]) + "\n" for pair in pairs), encoding="utf-8")
        reviews_path.write_text("".join(json.dumps(pair[1]) + "\n" for pair in pairs), encoding="utf-8")
        cases.append({"case_id": case_id, "kind": "popup" if case_index < 2 else "startup",
            "counts": {"startup_failure": 0,
            "confirmed_oom_kill": 0, "native_low_memory_popup": None,
            "primary_count": None, "unique_incidents": None},
            "target_classifications": [], "kernel_oom": {"victims": [],
                "confirmed_unattributed_victims": []},
            "measurement_gates_without_ui": {"complete": True},
            "ui_evidence": {"frame_manifest": {"manifest_path": str(manifest),
                "review_path": str(reviews_path), "case_begin_boottime_s": 100,
                "case_end_boottime_s": 108, "collection_end_boottime_s": 108}}})
    original = {"scene4_cold_start": {"cases": cases,
        "measurement_gates_without_ui": {"complete": True},
        "confirmed_count_lower_bounds": {}, "cumulative_counts": {},
        "measurement_complete": False}, "counts": {"primary_count_observed": False}}
    original_path = tmp_path / "result.json"
    original_path.write_text(json.dumps(original), encoding="utf-8")
    original_bytes = original_path.read_bytes()

    output_path = reconcile_scene4_frame_reviews(original_path)

    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert original_path.read_bytes() == original_bytes
    assert result["scene4_cold_start"]["measurement_complete"] is True
    assert result["scene4_cold_start"]["cumulative_counts"]["native_low_memory_popup"] == 0
    assert result["scene4_cold_start"]["cumulative_counts"]["primary_count"] == 0
    assert all(pair["pair_complete"] for pair in result["scene4_cold_start"]["paired_case_review_summary"])
    assert len(result["review_reconciliation"]["source_result_sha256"]) == 64
    assert all(case["ui_evidence"]["frame_manifest"]["image_integrity_complete"]
               for case in result["scene4_cold_start"]["cases"])
    repeated_path = reconcile_scene4_frame_reviews(original_path)
    assert repeated_path != output_path and repeated_path.is_file()
    partial = json.loads(json.dumps(original))
    partial["scene4_cold_start"]["cases"][0]["measurement_gates_without_ui"]["complete"] = False
    partial_path = tmp_path / "non_ui_incomplete.json"
    partial_path.write_text(json.dumps(partial), encoding="utf-8")
    partial_result = json.loads(reconcile_scene4_frame_reviews(partial_path).read_text(encoding="utf-8"))
    assert partial_result["scene4_cold_start"]["cases"][0]["ui_evidence"]["complete"] is True
    assert partial_result["scene4_cold_start"]["measurement_complete"] is False
    assert partial_result["counts"]["total_events"] is None
    assert partial_result["scene4_target"]["primary_count"] is None
    assert partial_result["scene4_cold_start"]["confirmed_count_lower_bounds"]["native_low_memory_popup"] == 0


def test_scene4_offline_review_reconciliation_does_not_promote_pending_or_bad_image(tmp_path):
    case_id = "c1"
    pairs = [_frame_review_pair(i, 100+i*2, 100+i*2+.7, case_id=case_id)
             for i in range(4)]
    manifest, reviews_path = tmp_path / "frames.jsonl", tmp_path / "reviews.jsonl"
    for frame, _ in pairs:
        image = tmp_path / f"{frame['slot_index']}.png"
        image.write_bytes(b"image" + str(frame["slot_index"]).encode())
        frame["local_path"] = str(image)
        frame["frame_sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
    pairs[0][1]["review_classification"] = "pending_review"
    for frame, review in pairs:
        review["frame_sha256"] = frame["frame_sha256"]
    manifest.write_text("".join(json.dumps(pair[0]) + "\n" for pair in pairs), encoding="utf-8")
    reviews_path.write_text("".join(json.dumps(pair[1]) + "\n" for pair in pairs), encoding="utf-8")
    cases = [{"case_id": case_id, "counts": {"startup_failure": 0,
        "confirmed_oom_kill": 0, "native_low_memory_popup": None,
        "primary_count": None, "unique_incidents": None},
        "target_classifications": [], "kernel_oom": {"victims": [],
            "confirmed_unattributed_victims": []},
        "measurement_gates_without_ui": {"complete": True},
        "ui_evidence": {"frame_manifest": {"manifest_path": str(manifest),
            "review_path": str(reviews_path), "case_begin_boottime_s": 100,
            "case_end_boottime_s": 108, "collection_end_boottime_s": 108}}}]
    source = tmp_path / "partial.json"
    source.write_text(json.dumps({"scene4_cold_start": {"cases": cases,
        "measurement_gates_without_ui": {"complete": True}}}), encoding="utf-8")
    image = Path(pairs[1][0]["local_path"])
    image.write_bytes(b"corrupted")
    out = reconcile_scene4_frame_reviews(source)
    reconciled = json.loads(out.read_text(encoding="utf-8"))
    assert reconciled["scene4_cold_start"]["measurement_complete"] is False
    assert reconciled["scene4_cold_start"]["cases"][0]["counts"]["primary_count"] is None
    errs = reconciled["scene4_cold_start"]["cases"][0]["ui_evidence"]["frame_manifest"]["errors"]
    assert any("frame_review_pending_or_invalid" in error for error in errs)
    assert any("frame_image_missing_or_sha_mismatch" in error for error in errs)


def test_scene4_offline_review_missing_manifest_fails_closed_without_exception(tmp_path):
    source = tmp_path / "missing.json"
    source.write_text(json.dumps({"scene4_cold_start": {
        "cases": [{"case_id": "c1", "measurement_gates_without_ui": {"complete": True},
            "counts": {}, "ui_evidence": {"frame_manifest": {
                "manifest_path": str(tmp_path / "absent.jsonl"),
                "review_path": str(tmp_path / "absent_reviews.jsonl"),
                "case_begin_boottime_s": 1, "case_end_boottime_s": 3}}}],
        "measurement_gates_without_ui": {"complete": True}}}), encoding="utf-8")
    output = reconcile_scene4_frame_reviews(source)
    result = json.loads(output.read_text(encoding="utf-8"))
    case = result["scene4_cold_start"]["cases"][0]
    assert case["ui_evidence_complete"] is False
    assert case["measurement_complete"] is False
    assert any("frame_review_file_read_error" in error
               for error in case["ui_evidence"]["frame_manifest"]["errors"])


def test_scene4_journal_readback_proof_requires_exact_complete_raw_jsonl(tmp_path):
    class FakeDevice:
        def __init__(self, payload, exit_code=0, reported_bytes=None):
            self.payload, self.exit_code = payload, exit_code
            self.reported_bytes = len(payload.encode("utf-8")) if reported_bytes is None else reported_bytes
        def app(self, bundle):
            return {"files": f"/data/app/el2/100/base/{bundle}/haps/entry/files"}
        def shell(self, command, **kwargs):
            return ("__S4J_s4_test_c0001_BEGIN__" + self.payload +
                f"__S4J_s4_test_c0001_END__ rc={self.exit_code} bytes={self.reported_bytes}\\n")

    class FakeSession:
        id = "s4_test"
        def __init__(self, device):
            self.device, self.output = device, tmp_path
            self.scene4_journal_readback = {}

    row = {"case_id": "c0001", "event": "cold_start_ready", "pid": 1001,
           "uid": 20001, "start_ticks": "12345"}
    valid_payload = json.dumps(row) + "\n"
    good = FakeSession(FakeDevice(valid_payload))
    parsed = _scene4_read_journal(good, "io.memscene.scene4.workload.a3", "c0001")
    assert parsed == [row]
    assert good.scene4_journal_readback[("c0001", "io.memscene.scene4.workload.a3")]["complete"]

    bad_rows = [
        (valid_payload.rstrip("\n"), 0, len(valid_payload.rstrip("\n").encode("utf-8"))),
        (valid_payload, 1, len(valid_payload.encode("utf-8"))),
        (valid_payload, 0, len(valid_payload.encode("utf-8")) + 1),
        (json.dumps({**row, "case_id": "other"}) + "\n", 0,
         len((json.dumps({**row, "case_id": "other"}) + "\n").encode("utf-8"))),
        ("{malformed}\n", 0, len(b"{malformed}\n")),
    ]
    for payload, exit_code, byte_count in bad_rows:
        session = FakeSession(FakeDevice(payload, exit_code, byte_count))
        _scene4_read_journal(session, "io.memscene.scene4.workload.a3", "c0001")
        proof = session.scene4_journal_readback[("c0001", "io.memscene.scene4.workload.a3")]
        assert not proof["complete"]


CONTROL_OK = {"verified": True}


def test_scene4_wms_inventory_requires_reported_count_and_unique_window_ids():
    header = "WindowName DisplayId Pid WinId Type Mode Flag ZOrd Orientation\n"
    row = "SCBDesktop4 0 4698 7 2001 1 0 2 0\n"
    tail = "Focus window: 7\nTotal window num: 1\nHighlighted windows: 1\n"
    complete = _scene4_wms_window_inventory(header + row + tail)
    assert complete["complete"]
    assert complete["reported_window_count"] == complete["parsed_window_count"] == 1
    assert complete["window_count_matches"] and not complete["duplicate_win_ids"]

    missing_row = _scene4_wms_window_inventory(header + row +
        "Focus window: 7\nTotal window num: 2\nHighlighted windows: 1\n")
    assert not missing_row["complete"]
    assert missing_row["reported_window_count"] == 2
    assert missing_row["parsed_window_count"] == 1
    assert not missing_row["window_count_matches"]

    missing_total = _scene4_wms_window_inventory(header + row + "Focus window: 7\n")
    assert not missing_total["complete"]
    assert missing_total["reported_window_count"] is None

    duplicate = _scene4_wms_window_inventory(header + row + row +
        "Focus window: 7\nTotal window num: 2\nHighlighted windows: 1\n")
    assert not duplicate["complete"]
    assert duplicate["duplicate_win_ids"] == [7]


def test_scene4_timing_v3_pairs_real_action_status_history_and_validates_write_timing():
    v2_csv = ("action_id,event_id,case_id,status,write_length\n"
        "reg-1,evt-1,case-1,event_registered_once,-1\n"
        "act-1,evt-1,case-1,reserved_before_write_pending_result,-1\n"
        "act-1,evt-1,case-1,write_accepted_feedback_measured,4096\n")
    v3_csv = (SCENE4_TIMING_V3_HEADER + "\n"
        "reg-1,evt-1,case-1,event_registered_once,10,,,,,,,1,false\n"
        "act-1,evt-1,case-1,reserved_before_write_pending_result,10,,,,,,,2,false\n"
        "act-1,evt-1,case-1,write_accepted_feedback_measured,10,9.95,10.05,100,10.08,true,true,130,true\n")
    v2 = list(__import__("csv").DictReader(v2_csv.splitlines()))
    v3_reader = __import__("csv").DictReader(v3_csv.splitlines())
    assert v3_reader.fieldnames == SCENE4_TIMING_V3_HEADER.split(",")
    v3 = list(v3_reader)
    proof = _scene4_timing_v3_completeness(v2, v3, "evt-1")
    assert proof["complete"]
    assert proof["action_status_sequences"]["act-1"] == [
        "reserved_before_write_pending_result", "write_accepted_feedback_measured"]

    missing_terminal = _scene4_timing_v3_completeness(v2, v3[:-1], "evt-1")
    assert not missing_terminal["complete"]
    assert "v3_action_status_state_missing" in missing_terminal["errors"]

    duplicate_terminal = _scene4_timing_v3_completeness(v2, v3 + [v3[-1]], "evt-1")
    assert not duplicate_terminal["complete"]
    assert "v3_duplicate_or_unpaired_status_state" in duplicate_terminal["errors"]

    bad_duration = dict(v3[-1], write_duration_ms="300")
    duration_proof = _scene4_timing_v3_completeness(v2, [*v3[:-1], bad_duration], "evt-1")
    assert not duration_proof["complete"]
    assert any("v3_write_duration_mismatch" in error for error in duration_proof["errors"])


def test_scene4_timing_v3_allows_explicit_inflight_unknown_and_prewrite_refusal_without_write_times():
    v2 = [
        {"action_id": "reg", "event_id": "evt", "case_id": "case", "status": "event_registered_once"},
        {"action_id": "inflight", "event_id": "evt", "case_id": "case",
         "status": "reserved_action_still_inflight_at_deadline_unknown"},
        {"action_id": "refusal", "event_id": "evt", "case_id": "case", "status": "rejected:donor_stale"},
    ]
    v3 = [
        {"action_id": "reg", "event_id": "evt", "case_id": "case", "ledger_status": "event_registered_once",
         "legacy_deadline_boottime_s": "10", "legacy_elapsed_ms": "1", "legacy_deadline_overlap": "false"},
        {"action_id": "inflight", "event_id": "evt", "case_id": "case",
         "ledger_status": "reserved_action_still_inflight_at_deadline_unknown",
         "legacy_deadline_boottime_s": "10", "legacy_elapsed_ms": "20", "legacy_deadline_overlap": "true"},
        {"action_id": "refusal", "event_id": "evt", "case_id": "case", "ledger_status": "rejected:donor_stale",
         "legacy_deadline_boottime_s": "10", "legacy_elapsed_ms": "2", "legacy_deadline_overlap": "false"},
    ]
    assert _scene4_timing_v3_completeness(v2, v3, "evt")["complete"]

    bad_overlap = [dict(row) for row in v3]
    bad_overlap[-1]["write_crossed_deadline"] = "true"
    assert not _scene4_timing_v3_completeness(v2, bad_overlap, "evt")["complete"]

    bad_order = [dict(row) for row in v3]
    bad_order[-1].update(syscall_start_boottime_s="10.1",
        syscall_return_boottime_s="10.0", write_duration_ms="-100")
    assert not _scene4_timing_v3_completeness(v2, bad_order, "evt")["complete"]


def test_scene4_test_popup_journals_are_read_only_for_owned_visible_candidates():
    bundle = "io.memscene.scene4.workload.a7"
    owned = [{"__ownerBundleName": bundle, "visible": "true"}]
    assert _scene4_test_dialog_bundle(bundle)
    assert not _scene4_test_popup_journal_needed(bundle, True, [], True)
    assert not _scene4_test_popup_journal_needed(bundle, True, owned, False)
    assert _scene4_test_popup_journal_needed(bundle, True, owned, True)
    assert not _scene4_test_dialog_bundle("io.memscene.scene4.workload.a2")
    assert _scene4_test_popup_journal_needed("io.memscene.workload.a1", True, [], False)


def test_journal_popup_callbacks_are_separate_from_visible_native_counts():
    rows = [
        {"event": "memory_notification", "case_id": "c1", "monotonic_ns": 11_000_000_000,
         "pid": 23, "level": 2, "phase": "peak"},
        {"event": "popup_requested", "case_id": "c1", "monotonic_ns": 12_000_000_000,
         "pid": 23},
        {"event": "popup_requested", "case_id": "c1", "monotonic_ns": 25_000_000_000},
        {"event": "memory_notification", "case_id": "wrong", "monotonic_ns": 13_000_000_000},
    ]
    evidence = _scene4_journal_popup_signals({"a3": rows}, {"a3": {"complete": True}},
        {"a3": "c1"}, 10.0, 20.0)
    assert evidence["complete_journal_coverage"]
    assert evidence["memory_notification_callback_count_observed"] == 1
    assert evidence["popup_requested_count_observed"] == 1
    assert all("not proof of visible popup" in row["source"] for row in evidence["signals"])
    incomplete = _scene4_journal_popup_signals({"a3": rows}, {"a3": {"complete": False}},
        {"a3": "c1"}, 10.0, 20.0)
    assert incomplete["unknown_if_any_journal_incomplete"]
    assert incomplete["popup_requested_count_observed"] == 1
    missing_time = _scene4_journal_popup_signals({"a3": [
        {"event": "popup_requested", "case_id": "c1"}]}, {"a3": {"complete": True}},
        {"a3": "c1"}, 10.0, 20.0)
    assert not missing_time["complete_journal_coverage"]
    assert missing_time["evidence_gaps"][0]["reason"] == "signal_row_missing_device_boottime"


def test_scene4_hidden_text_does_not_trigger_screenshot_but_unknown_visibility_does():
    hidden = [{"visible": "false"}]
    unknown = [{"visible": ""}]
    assert _scene4_screenshot_needed(True, None, hidden) == (False, 0, 0)
    assert _scene4_screenshot_needed(True, None, unknown) == (True, 0, 1)
    assert _scene4_screenshot_needed(False, None, hidden) == (True, 1, 0)


def test_scene4_primary_total_stays_na_when_any_case_or_final_run_is_incomplete():
    cases = [{"measurement_complete": True} for _ in range(4)]
    complete = {"measurement_complete": True, "cases": cases,
                "cumulative_counts_observed": True,
                "cumulative_counts": {"primary_count": 3}}
    assert _scene4_cumulative_counts_observed(complete)
    assert _scene4_primary_count_snapshot(complete) == {
        "primary_count": 3, "primary_count_observed": True, "measurement_complete": True}
    invalid_case = {**complete, "cases": [*cases[:3], {"measurement_complete": False}]}
    assert not _scene4_cumulative_counts_observed(invalid_case)
    assert _scene4_primary_count_snapshot(invalid_case)["primary_count"] is None
    assert not _scene4_primary_count_snapshot(invalid_case)["primary_count_observed"]
    invalidated_after_aggregation = {**complete, "measurement_complete": False}
    assert not _scene4_primary_count_snapshot(invalidated_after_aggregation)["primary_count_observed"]


def test_scene4_final_cleanup_invalidation_clears_full_cumulative_counts():
    result = {"measurement_complete": True,
        "cases": [{"measurement_complete": True} for _ in range(4)],
        "cumulative_counts_observed": True,
        "cumulative_counts": {"primary_count": 4, "startup_failure": 1}}
    result["measurement_complete"] = False  # e.g. final schedule invalidation failed
    assert not _scene4_finalize_cumulative_counts(result)
    assert result["cumulative_counts_observed"] is False
    assert result["cumulative_counts"] == {"primary_count": None, "startup_failure": None}


def test_baseline_accepts_explicit_registration_and_control_pause_not_negative_write_as_zero():
    row = {"event_id": "e", "action_id": "e", "status": "event_registered_once", "write_length": "-1"}
    assert _scene4_ledger_completeness([row], "baseline", CONTROL_OK, CONTROL_OK)["complete"]
    refused = {**row, "status": "rejected:control_paused_or_unknown"}
    assert _scene4_ledger_completeness([refused], "baseline", CONTROL_OK, CONTROL_OK)["complete"]
    reserved = {**row, "status": "reserved_before_write_pending_result"}
    assert not _scene4_ledger_completeness([reserved], "baseline", CONTROL_OK, CONTROL_OK)["complete"]
    assert not _scene4_ledger_completeness([row], "baseline", CONTROL_OK, {"verified": False})["complete"]


def test_active_ledger_groups_reservation_and_deadline_then_terminal_once():
    rows = [
        {"action_id": "a1", "status": "reserved_before_write_pending_result", "write_length": "-1"},
        {"action_id": "a1", "status": "reserved_action_still_inflight_at_deadline_unknown", "write_length": "-1"},
        {"action_id": "a1", "status": "write_accepted_feedback_unknown_after_exception", "write_length": "65536"},
    ]
    proof = _scene4_ledger_completeness(rows, "optimized", CONTROL_OK, CONTROL_OK)
    assert proof["complete"]
    assert len(proof["action_ids"]) == 1
    assert not _scene4_ledger_completeness(rows[:1], "optimized", CONTROL_OK, CONTROL_OK)["complete"]


def test_scene4_prelaunch_inventory_requires_complete_ps_and_matches_exact_package_tokens():
    targets = [f"io.memscene.scene4.workload.a{i}" for i in range(3, 9)]
    raw = ("  PID ARGS\n"
           "  100 init\n"
           "  29213 io.memscene.scene4.workload.a7\n"
           "  400 /system/bin/app --name=io.memscene.scene4.workload.a70\n"
           "__S4_TARGET_PS_EXIT__=0\n")
    parsed = _scene4_parse_target_process_inventory(raw, targets)
    assert parsed["complete"] and parsed["processes"][targets[4]][0]["pid"] == 29213
    assert parsed["processes"][targets[4]][0]["args"] == "io.memscene.scene4.workload.a7"
    assert len(parsed["processes"][targets[4]]) == 1
    assert not _scene4_parse_target_process_inventory("PID ARGS\n100 init\n", targets)["complete"]
    assert not _scene4_parse_target_process_inventory(
        "PID ARGS\n100 init\n__S4_TARGET_PS_EXIT__=1\n", targets)["complete"]


def test_scene4_ready_process_start_tick_is_diagnostic_not_app_launch_proof():
    targets = ["io.memscene.scene4.workload.a7"]
    ready = {"pid": 29213, "uid": 20020194, "start_ticks": "3479435"}
    intervals = {targets[0]: {"cold_start_ready": ready}}
    pre_t = _scene4_target_start_ticks_gate(intervals, targets, 34799.654358594, 100)
    assert pre_t["valid"]
    assert pre_t["targets"][targets[0]]["classification"] == "pre_T_process_generation_identity_unresolved"
    ready["start_ticks"] = "3480000"
    assert _scene4_target_start_ticks_gate(intervals, targets, 34799.654358594, 100)["valid"]
    # proc start ticks are MONOTONIC; compare in BOOTTIME using the measured
    # offset and only the actual anchor-query uncertainty plus tick quantization.
    ready["start_ticks"] = "9980"
    bounded = _scene4_target_start_ticks_gate(intervals, targets, 100.0, 100,
        clock_offset_s=0.0, anchor_uncertainty_s=0.2)
    assert bounded["valid"] and bounded["targets"][targets[0]]["conservative_tolerance_s"] == pytest.approx(0.21)
    ready["start_ticks"] = "9970"
    unresolved = _scene4_target_start_ticks_gate(intervals, targets, 100.0, 100,
        clock_offset_s=0.0, anchor_uncertainty_s=0.2)
    assert unresolved["valid"]
    assert unresolved["targets"][targets[0]]["classification"] == "pre_T_process_generation_identity_unresolved"


def test_scene4_ratio_ceiling_skip_is_a_complete_no_action_ledger_decision():
    rows = [{"action_id": "evt_ratio_skip", "status": "skipped_no_ratio_increase_after_positive_feedback",
             "write_length": "-1"}]
    proof = _scene4_ledger_completeness(rows, "optimized", CONTROL_OK, CONTROL_OK)
    assert proof["complete"] and proof["saw_event_decision"]
    terminal_statuses = [
        "write_accepted_feedback_measured",
        "write_accepted_feedback_measured_positive_rss_observation",
        "write_accepted_feedback_nonpositive_halt",
        "write_accepted_feedback_unknown",
        "write_accepted_feedback_unknown_after_exception",
        "write_accepted_identity_or_background_changed_postwrite",
        "write_failed_or_partial_no_retry",
    ]
    for terminal in terminal_statuses:
        pair = [{"action_id": "x", "status": "reserved_before_write_pending_result", "write_length": "-1"},
                {"action_id": "x", "status": terminal, "write_length": "1"}]
        assert _scene4_ledger_completeness(pair, "optimized", CONTROL_OK, CONTROL_OK)["complete"]
    csv_rows = [
        {"action_id": "ev", "status": "event_registered_once", "write_length": "-1"},
        {"action_id": "same", "status": "reserved_before_write_pending_result", "write_length": "-1"},
        {"action_id": "same", "status": "write_accepted_feedback_measured", "write_length": "65536"},
    ]
    assert _scene4_ledger_completeness(csv_rows, "optimized", CONTROL_OK, CONTROL_OK)["complete"]
    # A V2 positive observation and a V2 zero/growth halt are both the one
    # terminal outcome for their reserved action; neither is counted as two writes.
    v2_positive = [
        {"action_id": "v2p", "status": "reserved_before_write_pending_result", "write_length": "-1"},
        {"action_id": "v2p", "status": "write_accepted_feedback_measured_positive_rss_observation", "write_length": "2"},
    ]
    v2_nonpositive = [
        {"action_id": "v2z", "status": "reserved_before_write_pending_result", "write_length": "-1"},
        {"action_id": "v2z", "status": "write_accepted_feedback_nonpositive_halt", "write_length": "2"},
    ]
    assert _scene4_ledger_completeness(v2_positive, "optimized", CONTROL_OK, CONTROL_OK)["complete"]
    assert _scene4_ledger_completeness(v2_nonpositive, "optimized", CONTROL_OK, CONTROL_OK)["complete"]
    assert not _scene4_ledger_completeness([
        {"action_id": "v2x", "status": "reserved_before_write_pending_result", "write_length": "-1"},
        {"action_id": "v2x", "status": "write_accepted_feedback_zero_or_growth_halt", "write_length": "2"},
    ], "optimized", CONTROL_OK, CONTROL_OK)["complete"]
    assert not _scene4_ledger_completeness([
        {"action_id": "extra", "status": "reserved_before_write_pending_result", "write_length": "-1"},
        {"action_id": "extra", "status": "write_accepted_feedback_measured_positive_rss_observation", "write_length": "2"},
        {"action_id": "extra", "status": "unrecognized_extra_status", "write_length": "2"},
    ], "optimized", CONTROL_OK, CONTROL_OK)["complete"]
    assert not _scene4_ledger_completeness([
        {"action_id": "twice", "status": "reserved_before_write_pending_result", "write_length": "-1"},
        {"action_id": "twice", "status": "reserved_before_write_pending_result", "write_length": "-1"},
        {"action_id": "twice", "status": "write_accepted_feedback_measured_positive_rss_observation", "write_length": "2"},
    ], "optimized", CONTROL_OK, CONTROL_OK)["complete"]
    refusal = [{"action_id": "same", "status": "reserved_before_write_pending_result", "write_length": "-1"},
               {"action_id": "same", "status": "rejected:control_paused_or_unknown", "write_length": "-1"}]
    assert _scene4_ledger_completeness(refusal, "optimized", CONTROL_OK, CONTROL_OK)["complete"]


def test_ready_at_t179_keeps_full_popup_hold_after_another_target_times_out():
    t = 100.0
    deadline = t + 180.0
    ready_hold_end = t + 179.0 + 60.0
    assert deadline == 280.0 and ready_hold_end == 339.0
    assert not _scene4_should_end_counter_window(deadline, t + 60.0, [ready_hold_end], 0)
    assert not _scene4_should_end_counter_window(ready_hold_end - 0.01, t + 60.0, [ready_hold_end], 0)
    assert _scene4_should_end_counter_window(ready_hold_end, t + 60.0, [ready_hold_end], 0)
    assert not _scene4_should_end_counter_window(deadline, t + 15.0, [], 1)
    startup_ready_hold_end = t + 20.0 + 15.0
    assert not _scene4_should_end_counter_window(t + 34.99, t + 15.0,
                                                [startup_ready_hold_end], 0)
    assert _scene4_should_end_counter_window(startup_ready_hold_end, t + 15.0,
                                              [startup_ready_hold_end], 0)


def test_cold_hold_requires_real_end_or_exact_identity_oom_termination():
    ready = {"monotonic_ns": 20_000_000_000, "pid": 77, "uid": 10001, "start_ticks": "900"}
    missing_end = _scene4_hold_coverage("a3", ready, None, 15, [])
    assert not missing_end["complete"]
    matching = {"bundle": "a3", "pid": 77, "uid": 10001, "start_ticks": "900", "event_uptime_s": 22.0}
    assert _scene4_hold_coverage("a3", ready, None, 15, [matching])["complete"]
    wrong_donor = {"bundle": "a4", "pid": 77, "uid": 10001, "start_ticks": "900", "event_uptime_s": 22.0}
    assert not _scene4_hold_coverage("a3", ready, None, 15, [wrong_donor])["complete"]
    end = {"monotonic_ns": 35_000_000_000, "pid": 77, "uid": 10001, "start_ticks": "900"}
    assert _scene4_hold_coverage("a3", ready, end, 15, [])["complete"]
    wrong_uid_end = {**end, "uid": 10002}
    assert not _scene4_hold_coverage("a3", ready, wrong_uid_end, 15, [])["complete"]
    wrong_generation_end = {**end, "start_ticks": "901"}
    assert not _scene4_hold_coverage("a3", ready, wrong_generation_end, 15, [])["complete"]
    wrong_uid_oom = {**matching, "uid": 10002}
    assert not _scene4_hold_coverage("a3", ready, None, 15, [wrong_uid_oom])["complete"]


def test_final_journal_refresh_uses_late_real_hold_end_before_cutoff(monkeypatch):
    class FakeSession:
        def __init__(self):
            self.scene4_journal_readback = {}

    ready = {"event": "cold_start_ready", "monotonic_ns": 100_000_000_000,
             "pid": 7, "uid": 10001, "start_ticks": "88"}
    actual_end = {"event": "cold_start_hold_end", "monotonic_ns": 115_050_000_000,
                  "pid": 7, "uid": 10001, "start_ticks": "88"}
    session = FakeSession()

    def read(_session, _bundle, _case, **_kwargs):
        _session.scene4_journal_readback[("c1", "a3")] = {"complete": True,
            "raw_payload_sha256": "a" * 64, "parsed_row_count": 2}
        return [ready, actual_end]

    monkeypatch.setattr(scene4_runner, "_scene4_read_journal", read)
    outcomes = {"a3": {"readiness_status": "cold_start_ready", "cold_ready_event": ready,
                       "estimated_hold_end_host": 114.0}}
    proof = _scene4_finalize_target_hold_journals(session, ["a3"], "c1", outcomes,
        hold_seconds=15, anchor={"host_minus_device_seconds": -1.0}, read_timeout_seconds=2)
    assert proof["complete"] and proof["all_ready_hold_ends_observed"]
    assert proof["latest_by_bundle"]["a3"]["hold_end_boottime_s"] == 115.05
    assert outcomes["a3"]["cold_start_hold_end"] is actual_end
    assert outcomes["a3"]["estimated_hold_end_host"] == pytest.approx(114.05)
    cutoff = scene4_runner._scene4_count_window_cutoff(120.0, 110.0, outcomes, 15)
    assert cutoff["counter_end_boottime"] == pytest.approx(115.05)
    assert cutoff["hold_end_evidence_gaps"] == []


@pytest.mark.parametrize("end_patch", [
    {"pid": 8}, {"uid": 10002}, {"start_ticks": "89"},
    {"monotonic_ns": 114_999_999_999},
])
def test_final_journal_refresh_rejects_wrong_identity_or_short_hold(monkeypatch, end_patch):
    class FakeSession:
        scene4_journal_readback = {}

    ready = {"event": "cold_start_ready", "monotonic_ns": 100_000_000_000,
             "pid": 7, "uid": 10001, "start_ticks": "88"}
    actual_end = {"event": "cold_start_hold_end", "monotonic_ns": 115_000_000_000,
                  "pid": 7, "uid": 10001, "start_ticks": "88", **end_patch}
    session = FakeSession()

    def read(_session, _bundle, _case, **_kwargs):
        _session.scene4_journal_readback[("c1", "a3")] = {"complete": True,
            "raw_payload_sha256": "b" * 64, "parsed_row_count": 2}
        return [ready, actual_end]

    monkeypatch.setattr(scene4_runner, "_scene4_read_journal", read)
    outcomes = {"a3": {"readiness_status": "cold_start_ready", "cold_ready_event": ready}}
    proof = _scene4_finalize_target_hold_journals(session, ["a3"], "c1", outcomes,
        hold_seconds=15, anchor={"host_minus_device_seconds": 0.0}, read_timeout_seconds=2)
    assert proof["complete"]  # complete readback does not itself claim hold coverage
    assert not proof["all_ready_hold_ends_observed"]
    assert "cold_start_hold_end" not in outcomes["a3"]


def test_final_journal_refresh_preserves_readback_gap_as_incomplete(monkeypatch):
    class FakeSession:
        scene4_journal_readback = {}

    session = FakeSession()
    ready = {"event": "cold_start_ready", "monotonic_ns": 100_000_000_000,
             "pid": 7, "uid": 10001, "start_ticks": "88"}

    def read(_session, _bundle, _case, **_kwargs):
        _session.scene4_journal_readback[("c1", "a3")] = {"complete": False,
            "raw_payload_sha256": "c" * 64, "parsed_row_count": 1}
        return [ready]

    monkeypatch.setattr(scene4_runner, "_scene4_read_journal", read)
    outcomes = {"a3": {"readiness_status": "cold_start_ready", "cold_ready_event": ready}}
    proof = _scene4_finalize_target_hold_journals(session, ["a3"], "c1", outcomes,
        hold_seconds=15, anchor={"host_minus_device_seconds": 0.0}, read_timeout_seconds=2)
    assert not proof["complete"]
    assert not proof["all_readbacks_complete"]


def test_only_matching_donor_oom_allows_missing_post_window_validation():
    states = {"a1": {"status": "unknown_donor_missing_after_window"},
              "a2": {"status": "unknown_donor_missing_after_window"}}
    assert _scene4_donor_validation_complete(states, {"a1", "a2"}, {"a1"}) is False
    assert _scene4_donor_validation_complete({"a1": states["a1"]}, {"a1"}, {"a1"}) is True
    assert _scene4_donor_validation_complete({"a2": states["a2"]}, {"a2"}, {"a1"}) is False
    assert not _scene4_donor_validation_complete({}, {"a1", "a2"}, {"a1", "a2"})


def test_ui_evidence_errors_screenshot_failures_and_long_gaps_are_not_zero_popup_proof():
    base = {"case_id": "c1", "layout_captured": True, "capture_clock_valid": True,
            "host_monotonic": 10.0, "screenshot_needed": False}
    rows = [base, {**base, "host_monotonic": 15.0}]
    assert _scene4_ui_evidence_proof(rows, []) ["complete"]
    assert not _scene4_ui_evidence_proof([base, {**base, "host_monotonic": 26.0}], [])["complete"]
    assert not _scene4_ui_evidence_proof([base, {**base, "mission_error": "failed"}], [])["complete"]
    assert not _scene4_ui_evidence_proof([base, {**base, "capture_clock_valid": False}], [])["complete"]
    assert not _scene4_ui_evidence_proof([base, {**base, "screenshot_needed": True,
                                                    "screenshot_captured": False}], [])["complete"]
    assert not _scene4_ui_evidence_proof(rows, [{"case_id": "c1", "error": "mission read failed"}])["complete"]
    no_rows = _scene4_ui_evidence_proof([], [{"case_id": "c2", "error": "layout unavailable"}],
                                         case_id="c2")
    assert not no_rows["complete"] and no_rows["case_error_count"] == 1


def test_ui_proof_includes_case_begin_and_end_boundaries():
    row = {"case_id": "c1", "layout_captured": True, "capture_clock_valid": True,
           "host_monotonic": 5.0, "screenshot_needed": False}
    row2 = {**row, "host_monotonic": 15.0}
    assert _scene4_ui_evidence_proof([row, row2], [], 10, "c1", 0, 20)["complete"]
    assert not _scene4_ui_evidence_proof([row], [], 10, "c1", 0, 30)["complete"]
    assert not _scene4_ui_evidence_proof([row, row2], [], 10, "c1", 0, 40)["complete"]


def test_minimize_button_and_visibility_parse_real_dump_layout_i_list_schema():
    tree = [{"attributes": {"bundleName": "io.memscene.scene4.workload.a1",
                            "focused": "true", "visible": "true"},
             "children": [{"attributes": {"id": "EnhanceMinimizeBtn", "key": "EnhanceMinimizeBtn",
                                            "origBounds": "[2438,290][2491,343]",
                                            "enabled": "true", "visible": "true"},
                           "children": []}]},
            {"attributes": {"bundleName": "io.memscene.scene4.workload.a2",
                            "focused": "false", "visible": "true"}, "children": []}]
    button = _scene4_minimize_button(tree, "io.memscene.scene4.workload.a1")
    assert button and (button["x"], button["y"]) == (2464, 316)
    assert _scene4_focused_bundles(tree) == {"io.memscene.scene4.workload.a1"}


def test_scene4_visible_unmerged_layout_object_schema_keeps_owner_visibility_and_window():
    tree = {"attributes": {"type": "root", "visible": "true", "hostWindowId": "700"},
        "children": [{"attributes": {"bundleName": "com.huawei.hmos.taskmanager",
            "visible": "true", "hostWindowId": "701", "text": "Low memory"}, "children": []}]}
    rows = _scene4_ui_nodes(tree)
    candidate = next(row for row in rows if row.get("text") == "Low memory")
    assert candidate["__ownerBundleName"] == "com.huawei.hmos.taskmanager"
    assert candidate["visible"] == "true" and candidate["hostWindowId"] == "701"
    assert _scene4_minimize_button(tree, "io.memscene.scene4.workload.a2") is None
    hidden = [{"attributes": {"bundleName": "io.memscene.scene4.workload.a1",
                              "focused": "false", "visible": "false"}, "children": []}]
    proof = _scene4_bundle_window_visibility(hidden, "io.memscene.scene4.workload.a1")
    assert proof["invisible_confirmed"] and proof["visible_window_count"] == 0


def test_background_state_requires_exact_aa_running_record_identity_and_focus_contract():
    raw = """AppRunningRecord {\n  process name: io.memscene.scene4.workload.a1\n  pid: 101\n  uid: 20010001\n  state #BACKGROUND\n}\nAppRunningRecord {\n  process name: io.memscene.scene4.workload.a2\n  pid: 202\n  uid: 20010002\n  state #FOREGROUND\n}\n"""
    assert _scene4_aa_running_record_state(raw, "io.memscene.scene4.workload.a1", 101, 20010001) == "BACKGROUND"
    assert _scene4_aa_running_record_state(raw, "io.memscene.scene4.workload.a1", 202, 20010001) is None
    assert _scene4_aa_running_record_state(raw, "io.memscene.scene4.workload.a2", 202, 20010002) == "FOREGROUND"
    source = inspect.getsource(_scene4_minimize_donor)
    assert "d.focus(bundle)" in source
    assert 'd.shell("aa dump -r"' in source
    assert 'd.shell("aa focus ' not in source


def test_aa_running_record_parser_matches_actual_hash_delimited_device_schema():
    raw = """AppRunningRecord ID #19
      process name [io.memscene.scene4.workload.a1]
      pid #65322  uid #20020188
      state #BACKGROUND
    AppRunningRecord ID #20
      process name [io.memscene.scene4.workload.a2]
      pid #1782  uid #20020189
      state #FOREGROUND
"""
    assert _scene4_aa_running_record_state(raw, "io.memscene.scene4.workload.a1", 65322, 20020188) == "BACKGROUND"
    assert _scene4_aa_running_record_state(raw, "io.memscene.scene4.workload.a1", 65322, 0) is None
    assert _scene4_aa_running_record_state(raw, "io.memscene.scene4.workload.a2", 1, 20020189) is None


def test_marker_helper_actual_single_line_output_fixture_and_raw_correlation():
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "marker_helper_device_probe.raw.txt"
    lines = fixture.read_text(encoding="utf-8").splitlines()
    parsed = _scene4_parse_marker_helper_output(lines[1])
    assert parsed["marker_phase"] == "BEGIN"
    assert parsed["marker_write_len"] == "142" and parsed["marker_write_errno"] == "0"
    assert parsed["marker_payload"].endswith("boottime_ns=127476899710781")
    correlation = _scene4_marker_raw_correlation("\n".join(lines), {
        "session_id": "s4_clockprobe_20261007", "case_id": "marker_probe",
        "phase": "BEGIN", "marker_payload": parsed["marker_payload"],
        "marker_boottime_ns": int(parsed["marker_boottime_ns"])})
    assert correlation["matched"]
    assert correlation["raw_minus_boottime_seconds"] == pytest.approx(12.713474, abs=0.00001)


def test_dynamic_donor_schedule_fields_require_fresh_independent_sources():
    bundle = "io.memscene.scene4.workload.a1"
    identity = {"pid": 65322, "start_ticks": "456"}
    uid = 20020188
    aa_raw = """AppRunningRecord ID #19
      process name [io.memscene.scene4.workload.a1]
      pid #65322  uid #20020188
      state #BACKGROUND
"""
    tree = [{"attributes": {"bundleName": bundle, "visible": "false", "focused": "false"}, "children": []}]
    memcg = {"processes": {"65322:456": {"verified": True, "uid": uid,
        "start_ticks": "456", "exact_cgroup_leaf": True, "bundle_in_cmdline": True}}}
    lifecycle = {"journal_rows": [
        {"event": "donor_background_enter", "pid": uid and 65322, "uid": uid,
         "start_ticks": "456", "monotonic_ns": 90_000_000_000},
        {"event": "donor_background_heartbeat", "pid": 65322, "uid": uid,
         "start_ticks": "456", "monotonic_ns": 99_000_000_000}],
        "last_heartbeat_s": 99.0}
    fields, proof = _scene4_dynamic_donor_fields(bundle, identity, uid, 100.0,
        aa_raw, 99.5, tree, 99.6, memcg, 99.7, lifecycle, 99.8)
    assert len(fields) == 13 and fields[2] == "BACKGROUND"
    assert fields[-1] == "hap_heartbeat" and proof["valid"]
    # A heartbeat that arrives after the earlier schedule anchor remains valid
    # when the final dynamic preflight is sampled after that heartbeat.
    lifecycle["last_heartbeat_s"] = 100.05
    lifecycle["journal_rows"].append({"event": "donor_background_heartbeat", "pid": 65322,
        "uid": uid, "start_ticks": "456", "monotonic_ns": 100_050_000_000})
    fields, proof = _scene4_dynamic_donor_fields(bundle, identity, uid, 100.1,
        aa_raw, 100.0, tree, 100.0, memcg, 100.0, lifecycle, 100.0)
    assert fields[-1] == "hap_heartbeat" and proof["valid"]
    lifecycle["last_heartbeat_s"] = 90.0
    fields, proof = _scene4_dynamic_donor_fields(bundle, identity, uid, 100.0,
        aa_raw, 99.5, tree, 99.6, memcg, 99.7, lifecycle, 99.8)
    assert fields[-1] == "os_snapshot" and proof["valid"]
    fields, proof = _scene4_dynamic_donor_fields(bundle, identity, uid, 100.0,
        aa_raw, 99.5, tree, 96.0, memcg, 99.7, lifecycle, 99.8)
    assert fields[-1] == "unknown" and not proof["valid"] and fields[5] == "false"


def test_schedule_serialization_matches_48_column_daemon_contract():
    import csv
    import io
    header = _scene4_schedule_header()
    static = ["io.memscene.scene4.workload.a1", "20020188", "65322", "456",
              "true", "true", "90.0", "91.0", "99.0"]
    dynamic = ["65322", "20020188", "BACKGROUND", "99.5", "false", "false",
               "99.6", "true", "456", "99.7", "true", "99.8", "hap_heartbeat"]
    row = ["e1", "c1", "130.0", "40265318400"] + static + dynamic + static + dynamic
    assert len(header) == 48 and len(row) == 48
    out = io.StringIO()
    writer = csv.writer(out, delimiter="|", lineterminator="\n")
    writer.writerow(header)
    writer.writerow(row)
    serialized_header, serialized_row = out.getvalue().splitlines()
    assert len(serialized_header.split("|")) == len(serialized_row.split("|")) == 48
    assert serialized_header.split("|")[25] == "donor_a_background_evidence_source"
    assert serialized_header.split("|")[47] == "donor_b_background_evidence_source"
    assert serialized_header.split("|")[4:26] == [
        "donor_a_" + name for name in (
            "bundle uid pid start_ticks cold_ready window_ready cold_ready_boottime_s "
            "background_since_s last_heartbeat_s aa_pid aa_uid aa_state aa_observed_boottime_s "
            "window_visible window_focused window_observed_boottime_s proc_verified "
            "proc_start_ticks proc_observed_boottime_s lifecycle_clear "
            "lifecycle_observed_boottime_s background_evidence_source").split()]


def test_single_case_calibration_plan_is_explicit_and_does_not_change_default_four_cases():
    assert _scene4_case_plan(False) == [
        ("popup", 0, "popup"), ("popup", 1, "popup"),
        ("startup", 0, "startup"), ("startup", 1, "startup")]
    assert _scene4_case_plan(True) == [("popup", 0, "popup")]
    assert _scene4_case_plan(True, "startup") == [("startup", 0, "startup")]
    with pytest.raises(ValueError, match="must be popup or startup"):
        _scene4_case_plan(True, "oom")


def test_functional_probe_is_single_popup_case_only_in_active_optimized_mode():
    mode = _scene4_probe_mode({"oracle_functional_calibration_probe": True}, "optimized")
    assert mode == {"single_case_probe": True, "baseline_probe": False,
                    "functional_probe": True, "donor_proof_only": False}
    with pytest.raises(ValueError, match="optimized active-oracle"):
        _scene4_probe_mode({"oracle_functional_calibration_probe": True}, "baseline")
    with pytest.raises(ValueError, match="mutually exclusive"):
        _scene4_probe_mode({"oracle_functional_calibration_probe": True,
                            "oracle_single_case_calibration_probe": True}, "optimized")


def test_donor_ready_identity_checks_uid_pid_ticks_and_exact_verified_cgroup(monkeypatch):
    class FakeDevice:
        def __init__(self, identity, uid=20020188):
            self.identity = identity
            self.uid = uid

        def app(self, bundle):
            assert bundle == "io.memscene.scene4.workload.a1"
            return {"uid": self.uid}

        def inspect_process_identity(self, pid, timeout=10):
            return {"identity": self.identity, "raw": "identity-fixture"}

    def proof(identity=None, ready_uid=20020188, process_present=True):
        pid, ticks = 51731, "12922496"
        device = FakeDevice(identity or {"pid": pid, "uid": 20020188, "start_ticks": ticks})
        process = {"pid": pid, "uid": 20020188, "start_ticks": ticks,
                   "verified": True, "exact_cgroup_leaf": True,
                   "bundle_in_cmdline": True}
        snapshot = {"processes": {f"{pid}:{ticks}": process} if process_present else {}}
        monkeypatch.setattr(scene4_runner, "_scene4_memcg_snapshot",
                            lambda _device, _bundle: snapshot)
        return _scene4_donor_ready_identity(device, "io.memscene.scene4.workload.a1",
            {"pid": pid, "uid": ready_uid, "start_ticks": ticks})

    assert proof()["verified"]
    assert not proof(ready_uid=20020189)["verified"]
    assert not proof(identity={"pid": 51732, "uid": 20020188, "start_ticks": "12922496"})["verified"]
    assert not proof(identity={"pid": 51731, "uid": 20020188, "start_ticks": "12922495"})["verified"]
    assert not proof(process_present=False)["verified"]


def test_scene4_native_popup_maps_mission_monotonic_to_boottime_and_requires_visible_wms():
    class FakeDevice:
        def __init__(self, detail, uid=2000, cmdline="com.huawei.hmos.taskmanager"):
            self.detail = detail
            self.uid = uid
            self.cmdline = cmdline

        def shell(self, command, **kwargs):
            if "-a '-a'" in command:
                return "WindowName DisplayId Pid WinId Type Mode Flag ZOrd Orientation\n" \
                    "low_mem_float_win 0 29054 696 2106 102 0 925 0\nFocus window: 667\n"
            if "-w 696" in command:
                return self.detail
            if "__STAT_BEFORE__" in command:
                pid = 29054
                stat = f"{pid} (taskmanager) " + " ".join(["S"] + ["0"] * 18 + ["90", "1", "1"])
                return (f"__STAT_BEFORE__\n{stat}\n__STATUS__\nUid:\t{self.uid} {self.uid} {self.uid} {self.uid}\n"
                        f"__CMDLINE__\n{self.cmdline}\n__STAT_AFTER__\n{stat}\n")
            raise AssertionError(command)

        def app(self, bundle):
            assert bundle == "com.huawei.hmos.taskmanager"
            return {"uid": 2000}

        def inspect_process_identity(self, pid, timeout=20):
            return {"identity": {"pid": pid, "uid": self.uid, "start_ticks": 90},
                    "raw": "identity fixture"}

    class FakeSession:
        def __init__(self, detail):
            self.device = FakeDevice(detail)
            self.result = {"scene4_clock_domain": {"stable": True}}
            self.config = {"real_apps": {}}

    nodes = [{"__ownerBundleName": "com.huawei.hmos.taskmanager", "visible": "true",
        "hostWindowId": "696", "text": "\u7cfb\u7edf\u5185\u5b58\u4e0d\u8db3"}]
    missions = [{"mission_id": "77", "bundle": "com.huawei.hmos.taskmanager",
        "ability": "LowMemoryAbility", "state": "FOREGROUND", "pid": 29054,
        "start_monotonic_seconds": 112220.0, "start_uptime_seconds": 112220.0,
        "start_clock_domain": "MONOTONIC"}]
    clock = {"boottime_s": 129400.0, "monotonic_s": 112223.18,
        "boottime_minus_monotonic_s": 17176.82,
        "offset_drift_from_initial_s": 0.1, "trace_clock_selected": "boot"}
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(scene4_runner, "_scene4_device_boottime_s", lambda _session: 129400.1)
    visible_session = FakeSession("Pid: 29054\nWinId: 696\nIsVisible: true\nFocusable: true\n")
    candidate, proof = _scene4_native_popup_candidate(visible_session, nodes, missions, set(),
        129390.0, clock, None, ["\u5185\u5b58\u4e0d\u8db3"])
    assert proof["complete"] and candidate["mission_start_boottime_seconds"] == pytest.approx(129396.82)
    assert candidate["wms_visible"] is True

    no_pid_mission = [dict(missions[0], pid=None)]
    recovered_session = FakeSession("Pid: 29054\nWinId: 696\nIsVisible: true\nFocusable: true\n")
    candidate, proof = _scene4_native_popup_candidate(recovered_session, nodes, no_pid_mission, set(),
        129390.0, clock, None, ["\u5185\u5b58\u4e0d\u8db3"])
    assert proof["complete"] and candidate["pid"] == 29054
    assert candidate["pid_source"] == "unique_wms_winid_and_installed_uid"

    wrong_uid_session = FakeSession("Pid: 29054\nWinId: 696\nIsVisible: true\nFocusable: true\n")
    wrong_uid_session.device.uid = 3000
    candidate, proof = _scene4_native_popup_candidate(wrong_uid_session, nodes, no_pid_mission, set(),
        129390.0, clock, None, ["\u5185\u5b58\u4e0d\u8db3"])
    assert candidate is None and not proof["complete"]

    wrong_package = FakeSession("Pid: 29054\nWinId: 696\nIsVisible: true\nFocusable: true\n")
    wrong_package.device.cmdline = "com.other.package"
    candidate, proof = _scene4_native_popup_candidate(wrong_package, nodes, no_pid_mission, set(),
        129390.0, clock, None, ["\u5185\u5b58\u4e0d\u8db3"])
    assert candidate is None and not proof["complete"]

    hidden_session = FakeSession("Pid: 29054\nWinId: 696\nIsVisible: false\nFocusable: true\n")
    candidate, proof = _scene4_native_popup_candidate(hidden_session, nodes, missions, set(),
        129390.0, clock, None, ["\u5185\u5b58\u4e0d\u8db3"])
    assert candidate is None and not proof["complete"]

    unknown_clock = dict(clock, offset_drift_from_initial_s=1.1)
    candidate, proof = _scene4_native_popup_candidate(visible_session, nodes, missions, set(),
        129390.0, unknown_clock, None, ["\u5185\u5b58\u4e0d\u8db3"])
    assert candidate is None and not proof["complete"]
    ownerless = [{"visible": "true", "hostWindowId": "696", "text": "\u7cfb\u7edf\u5185\u5b58\u4e0d\u8db3"}]
    candidate, proof = _scene4_native_popup_candidate(visible_session, ownerless, missions, set(),
        129390.0, clock, None, ["\u5185\u5b58\u4e0d\u8db3"])
    assert candidate is None and not proof["complete"]
    assert proof["reason"] == "native_candidate_owner_unknown"
    monkeypatch.undo()


def test_wms_pid_bound_snapshot_parses_real_minimized_window_and_rejects_mismatch():
    from pathlib import Path
    fixture_dir = Path(__file__).parent / "fixtures"
    foreground_list = (fixture_dir / "window_probe_a1_donor_foreground_list.txt").read_text()
    background_list = (fixture_dir / "window_probe_a1_after_minimize_raw.txt").read_text()
    background_detail = (fixture_dir / "window_probe_a1_donor_background_detail_clocked.txt").read_text()
    foreground_detail_other_pid = (fixture_dir / "window_probe_a1_other_pid_detail_clocked.txt").read_text()

    before = _scene4_wms_window_snapshot(foreground_list, "", 51731)
    assert not before["known"] and before["list_row"]["win_id"] == 696
    minimized = _scene4_wms_window_snapshot(background_list, background_detail, 51731)
    assert {key: value for key, value in minimized.items() if key != "list_row"} == {"known": True, "pid": 51731, "win_id": 696,
        "visible": False, "focused": False, "focusable": True,
        "focused_win_id": 667, "window_name": "a10"}
    aa = "AppRunningRecord ID #1\n process name [io.memscene.scene4.workload.a1]\n" \
         " pid #51731 uid #20020188\n state #BACKGROUND\n"
    proc = {"processes": {"51731:456": {"verified": True, "uid": 20020188,
        "start_ticks": "456", "exact_cgroup_leaf": True, "bundle_in_cmdline": True}}}
    lifecycle = {"last_heartbeat_s": 99.9, "journal_rows": [
        {"event": "donor_background_enter", "pid": 51731, "uid": 20020188,
         "start_ticks": "456", "monotonic_ns": 90_000_000_000},
        {"event": "donor_background_heartbeat", "pid": 51731, "uid": 20020188,
         "start_ticks": "456", "monotonic_ns": 99_900_000_000}]}
    dynamic, proof = _scene4_dynamic_donor_fields("io.memscene.scene4.workload.a1",
        {"pid": 51731, "start_ticks": "456"}, 20020188, 100.0, aa, 99.9,
        {"wms_window_snapshot": minimized}, 99.9, proc, 99.9, lifecycle, 99.9)
    assert proof["valid"] and dynamic[4:6] == ["false", "false"]
    mismatch = _scene4_wms_window_snapshot(foreground_list, foreground_detail_other_pid, 51731)
    assert not mismatch["known"] and mismatch["reason"] == "targeted_detail_identity_or_state_mismatch"
    unknown = _scene4_wms_window_snapshot("WindowManagerService output unavailable", "", 51731)
    assert not unknown["known"]
    malformed_focus = _scene4_wms_window_snapshot(
        background_list.replace("Focus window: 667", "Focus window: unknown"), background_detail, 51731)
    assert not malformed_focus["known"] and malformed_focus["reason"] == "focused_window_id_missing"


def test_wms_can_resolve_missing_aa_pid_only_from_unique_exact_window_id():
    from pathlib import Path
    fixture_dir = Path(__file__).parent / "fixtures"
    table = (fixture_dir / "window_probe_a1_donor_foreground_list.txt").read_text()
    detail = (fixture_dir / "window_probe_a1_donor_background_detail_clocked.txt").read_text()
    resolved = _scene4_wms_window_snapshot(table, detail, None, 696)
    assert resolved["known"] and resolved["pid"] == 51731 and resolved["win_id"] == 696
    ambiguous_table = table + "\nlow_mem_duplicate 0 51732 696 2106 102 0 925 0\n"
    ambiguous = _scene4_wms_window_snapshot(ambiguous_table, detail, None, 696)
    assert not ambiguous["known"]


def test_scene4_light_candidate_plan_avoids_full_app_layouts_but_keeps_native_and_unknown():
    targets = [f"io.memscene.scene4.workload.a{i}" for i in range(3, 9)]
    owned = {"win_id": 1, "owner_bundle": targets[0]}
    native = {"win_id": 2, "owner_bundle": "com.huawei.hmos.taskmanager"}
    unknown = {"win_id": 3, "owner_bundle": "com.unknown.app"}
    unmapped = {"win_id": 4, "reason": "pid_identity_missing"}
    focused_target = {"win_id": 5, "owner_bundle": targets[1]}
    plan = _scene4_ui_candidate_plan([owned, native, unknown, focused_target],
        [unmapped], targets[1], targets)
    assert plan["layout_bundles"] == ["com.huawei.hmos.taskmanager", targets[1]]
    assert set(plan["native_candidate_winids"]) == {2}
    assert set(plan["unknown_owner_winids"]) == {3}
    assert set(plan["unmapped_winids"]) == {4}
    assert {row["win_id"] for row in plan["detail_rows"]} == {2, 3, 4, 5}


def test_scene4_batched_wms_detail_preserves_visibility_identity_and_clock_bracket():
    class Device:
        def __init__(self):
            self.command = None
        def shell(self, command, **kwargs):
            self.command = command
            return ("__WMS_696_PRE__\n100.0 1.0\n"
                "Pid: 51731\nWinId: 696\nIsVisible: false\nFocusable: true\n"
                "__WMS_696_POST__\n100.2 1.0\n__WMS_696_END__\n")
    class Session:
        def __init__(self): self.device = Device()
    row = {"pid": 51731, "owner_uid": 20020188,
        "owner_bundle": "io.memscene.scene4.workload.a1",
        "win_id": 696, "process_identity": {"verified": True, "start_ticks": "456"}}
    table = "WindowName DisplayId Pid WinId\na1 0 51731 696\nFocus window: 667\n"
    session = Session()
    details = _scene4_wms_details_batch(session, table, [row])
    assert len(details) == 1 and details[0]["parsed"]["known"]
    assert details[0]["parsed"]["visible"] is False
    assert details[0]["bracket_valid"]
    assert "-w 696 -element" not in session.device.command
    assert "__WMS_696_PRE__" in session.device.command


def test_scene4_window_layout_requires_real_components_and_exact_owner_identity():
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent / "fixtures" /
        "scene4_ui_win7_structure_fixture.json").read_text(encoding="utf-8"))
    expected = {"win_id": 7, "bundle": "com.ohos.sceneboard", "uid": 20020040,
        "pid": 4698, "parsed": {"known": True, "pid": 4698, "win_id": 7},
        "process_identity": {"verified": True, "uid": 20020040, "start_ticks": "898"}}
    proof = _scene4_layout_identity_proof(fixture, expected)
    assert proof["complete"] and proof["component_node_count"] >= 2
    assert proof["wms_identity_verified"] and proof["proc_identity_verified"]

    title_shell = {"attributes": fixture["attributes"], "children": []}
    assert not _scene4_layout_identity_proof(title_shell, expected)["complete"]
    empty_children = {"attributes": fixture["attributes"], "children": [{}]}
    assert not _scene4_layout_identity_proof(empty_children, expected)["complete"]
    malformed = {"attributes": fixture["attributes"], "children": "not-a-list"}
    assert not _scene4_layout_identity_proof(malformed, expected)["complete"]
    conflict = json.loads(json.dumps(fixture))
    conflict["children"][0]["attributes"]["bundleName"] = "com.other.owner"
    assert not _scene4_layout_identity_proof(conflict, expected)["complete"]
    wrong_proc = {**expected, "process_identity": {"verified": False, "uid": 99, "start_ticks": "898"}}
    assert not _scene4_layout_identity_proof(fixture, wrong_proc)["complete"]


def test_scene4_targeted_window_layout_parses_real_marker_glued_json_and_records_bracket(tmp_path):
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent / "fixtures" /
        "scene4_ui_win7_structure_fixture.json").read_text(encoding="utf-8"))
    tree_json = json.dumps(fixture, separators=(",", ":"))
    class Device:
        def __init__(self): self.command = ""
        def shell(self, command, **kwargs):
            self.command = command
            return ("__SCENE4_LAYOUT_PRE__\n6462.05 1.0\nDumpLayout saved\n"
                f"__SCENE4_LAYOUT_TREE__\n{tree_json}__SCENE4_LAYOUT_POST__\n6463.06 1.0\n"
                "__SCENE4_LAYOUT_END__\n")
    class Session:
        def __init__(self): self.device = Device(); self.remote = "/data/local/tmp/s4probe"; self.output = tmp_path
    session = Session()
    tree, path, output, timing = _scene4_dump_layout_for_window(session, 7, "probe_win7")
    assert "uitest dumpLayout -m false -w 7 -p" in session.device.command
    assert _scene4_layout_identity_proof(tree, {"win_id": 7, "bundle": "com.ohos.sceneboard",
        "pid": 4698, "uid": 20020040, "parsed": {"known": True, "pid": 4698, "win_id": 7},
        "process_identity": {"verified": True, "uid": 20020040, "start_ticks": "898"}})["complete"]
    assert timing["device_bracket_valid"] and timing["device_duration_s"] == pytest.approx(1.01)
    assert Path(tmp_path / path).exists() and "__SCENE4_LAYOUT_POST__" in output


def test_scene4_content_freshness_uses_per_read_age_and_rejects_stale_or_missing_clock():
    fresh = _scene4_content_freshness([{"boottime_start_s": 99.4, "boottime_end_s": 100.0}], 101.0)
    assert fresh["complete"] and fresh["samples"][0]["age_at_observation_end_s"] == pytest.approx(1.0)
    stale = _scene4_content_freshness([{"boottime_start_s": 97.0, "boottime_end_s": 98.0}], 101.0)
    assert not stale["complete"] and "sample_0_stale_or_overlong" in stale["errors"]
    old_age = _scene4_content_freshness([{"boottime_start_s": 98.0, "boottime_end_s": 98.5}], 101.1)
    assert not old_age["complete"]
    missing = _scene4_content_freshness([{}], 101.0)
    assert not missing["complete"]


def test_scene4_global_layout_proof_matches_visible_wms_identity_and_rejects_gaps():
    from pathlib import Path
    tree = json.loads((Path(__file__).parent / "fixtures" /
        "scene4_ui_win7_structure_fixture.json").read_text(encoding="utf-8"))
    details = [{"win_id": 7, "bundle": "com.ohos.sceneboard", "uid": 20020040,
        "pid": 4698, "parsed": {"known": True, "pid": 4698, "win_id": 7, "visible": True},
        "process_identity": {"verified": True, "uid": 20020040, "start_ticks": "898"}}]
    proof = _scene4_global_layout_coverage(tree, details)
    assert proof["complete"] and proof["expected_visible_winids"] == ["7"]
    assert _scene4_global_layout_coverage(tree, [*details, {**details[0], "win_id": 8,
        "parsed": {"known": True, "pid": 4698, "win_id": 8, "visible": True}}])["missing_visible_winids"] == ["8"]
    conflict = json.loads(json.dumps(tree))
    conflict["children"][0]["attributes"]["bundleName"] = "com.other.owner"
    assert not _scene4_global_layout_coverage(conflict, details)["complete"]


def test_scene4_global_layout_marker_parser_accepts_json_without_trailing_newline(tmp_path):
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent / "fixtures" /
        "scene4_ui_win7_structure_fixture.json").read_text(encoding="utf-8"))
    tree_json = json.dumps(fixture, separators=(",", ":"))
    class Device:
        def shell(self, command, **kwargs):
            assert "uitest dumpLayout -m false -p" in command
            return ("__SCENE4_LAYOUT_PRE__\n6462.05 1.0\n"
                f"__SCENE4_LAYOUT_TREE__\n{tree_json}__SCENE4_LAYOUT_POST__\n"
                "6463.06 1.0\n__SCENE4_LAYOUT_END__\n")
    class Session:
        def __init__(self): self.device = Device(); self.remote = "/data/local/tmp/s4global"; self.output = tmp_path
    tree, path, output, timing = _scene4_dump_layout_global_visible(Session(), "probe_global")
    assert len(_scene4_ui_nodes(tree)) == 3
    assert timing["device_bracket_valid"] and timing["device_duration_s"] == pytest.approx(1.01)
    assert Path(tmp_path / path).exists() and "__SCENE4_LAYOUT_POST__" in output


def test_window_inventory_churn_is_benign_only_for_verified_non_native_identity():
    known = {"io.memscene.scene4.workload.a3", "com.example.realapp",
             "com.huawei.hmos.taskmanager"}
    verified = {"verified": True, "uid": 20020188, "start_ticks": "143235"}
    owned = {"owner_bundle": "io.memscene.scene4.workload.a3", "owner_uid": 20020188,
             "process_identity": verified}
    real_app = {"owner_bundle": "com.example.realapp", "owner_uid": 20020188,
                "process_identity": verified}
    native = {"owner_bundle": "com.huawei.hmos.taskmanager", "owner_uid": 2000,
              "process_identity": {"verified": True, "uid": 2000, "start_ticks": "88"}}
    unknown = {"owner_bundle": "com.example.unknown", "owner_uid": 20020188,
               "process_identity": verified}
    reused_without_fresh_identity = {**owned, "process_identity": {"verified": True,
        "uid": 20020188, "start_ticks": "0"}}
    assert _scene4_verified_app_window_change(owned, known)
    assert _scene4_verified_app_window_change(real_app, known)
    assert not _scene4_verified_app_window_change(native, known)
    assert not _scene4_verified_app_window_change(unknown, known)
    assert not _scene4_verified_app_window_change(reused_without_fresh_identity, known)


def test_proc_identity_inventory_terminates_each_cmdline_record_before_next_pid():
    class DeviceFixture:
        def __init__(self):
            self.command = None
        def shell(self, command, **kwargs):
            self.command = command
            chunks = []
            for pid, ticks, uid, bundle in [
                (101, 777, 2001, "com.example.one"),
                (202, 888, 2002, "com.example.two")]:
                stat_tail = ["S"] + ["0"] * 18 + [str(ticks), "1000", "100"]
                stat = f"{pid} (fixture) " + " ".join(stat_tail)
                chunks.append(f"__P{pid}_STAT__\n{stat}\n__P{pid}_STATUS__\nUid:\t{uid} {uid} {uid} {uid}\n"
                    f"__P{pid}_CMDLINE__\n{bundle}\n__P{pid}_END__")
            return "\n".join(chunks)
    device = DeviceFixture()
    proof = _scene4_proc_identity_inventory(device, [
        {"pid": 101, "uid": 2001, "bundle": "com.example.one"},
        {"pid": 202, "uid": 2002, "bundle": "com.example.two"}])
    assert proof["complete"]
    assert [row["cmdline"] for row in proof["rows"]] == ["com.example.one", "com.example.two"]
    assert all(row["verified"] for row in proof["rows"])
    assert "echo __P101_END__" in device.command and "echo __P202_END__" in device.command


def test_scene4_owned_fourteen_character_comm_is_not_double_counted_as_unattributed():
    bundle = "io.memscene.scene4.workload.a8"
    comm = bundle[-14:]
    raw = "\n".join([
        "[10.000000] MEMSCENES_CASE_BEGIN session=s case_id=c uptime_ms=100000000 wall_ms=100000",
        f"[50.000000] [mem_oom:522] out of Memory(OOM), start to kill process < {comm}> (pid: 808, rss 99, drv 0, adj 0)",
        "[50.100000] sysmgr process has exited, pid=808, resource_refcnt=1",
        "[110.000000] MEMSCENES_CASE_END session=s case_id=c uptime_ms=100100000 wall_ms=200000",
    ])
    attempts = {bundle: [
        {"event":"peak_begin", "case_id":"c", "pid":808, "uid":20020195,
         "start_ticks":"14193645", "boottime_ns":100010000000000},
        {"event":"cold_start_ready", "case_id":"c", "pid":808, "uid":20020195,
         "start_ticks":"14193645", "boottime_ns":100020000000000},
    ]}
    book = scene4_runner.EventBook()
    owned = book.kernel_dmesg_oom_victims(raw,"c",{}, {bundle:20020195},attempts,
        100000.0,100100.0,100000,100000.0,"raw")
    owned_lines = {item["victim_line"].strip() for item in owned["victims"]}
    # Deliberately omit the bundle-name heuristic here: the global unattributed
    # pass must exclude raw victim lines already identity-matched as owned.
    unattributed, _unknown = scene4_runner._scene4_confirm_unattributed_kernel_victims(
        raw.splitlines(),100000.0,100100.0,99990.0,[],
        excluded_victim_lines=owned_lines)
    assert owned["victims_detected"] == 1
    assert len(unattributed) == 0
    assert len(book.export()) + len(unattributed) == 1


def test_checkpoint_is_atomic_and_partial_count_is_na(tmp_path):
    import json
    partial = {"cases": [{"case_id": "c1"}], "measurement_complete": False}
    status = _scene4_write_checkpoint(tmp_path, partial, "aborted_or_invalid", "probe abort")
    saved = json.loads((tmp_path / "scene4_checkpoint.json").read_text(encoding="utf-8"))
    assert status["completed_case_count"] == 1
    assert saved["primary_count"] is None and not saved["primary_count_observed"]
    complete = {"cases": [{"case_id": f"c{i}", "measurement_complete": True}
                           for i in range(1, 5)],
                "measurement_complete": True, "cumulative_counts_observed": True,
                "cumulative_counts": {"primary_count": 7}}
    _scene4_write_checkpoint(tmp_path, complete, "completed")
    saved = json.loads((tmp_path / "scene4_checkpoint.json").read_text(encoding="utf-8"))
    assert saved["completed_case_count"] == 4 and saved["primary_count"] == 7


def test_unique_incidents_count_case_scoped_popups_and_dedupe_same_startup_kernel_identity():
    classes = [
        {"bundle": "a3", "classification": "startup_failure",
         "incident": "case1:a3:startup_attempt", "incident_identity": None},
        {"bundle": "a4", "classification": "startup_failure",
         "incident": "a4:123:456", "incident_identity": "a4:123:456"},
        {"bundle": "a5", "classification": "cold_start_ready", "incident": None},
    ]
    victims = [{"bundle": "a4", "pid": 123, "start_ticks": "456"}]
    popup = [{"incident": "popup:native:m42:w7", "bundle": "systemui"}]
    case1 = _scene4_unique_incident_ids("case1", classes, victims, popup)
    case2 = _scene4_unique_incident_ids("case2", classes[:1], [], popup)
    assert len(case1) == 3
    assert "a4:123:456" in case1
    assert "case1:a3:startup_attempt" in case1
    assert "case1:native_popup:popup:native:m42:w7" in case1
    assert "case2:a3:startup_attempt" in case2
    assert "case2:native_popup:popup:native:m42:w7" in case2
    assert case1.isdisjoint(case2)


def test_global_ui_completeness_requires_all_four_case_proofs():
    complete = [{"ui_evidence_complete": True, "ui_evidence": {"complete": True}}
                for _ in range(4)]
    assert _scene4_all_ui_evidence_complete(complete)
    complete[2]["ui_evidence"]["complete"] = False
    assert not _scene4_all_ui_evidence_complete(complete)
    assert not _scene4_all_ui_evidence_complete(complete[:3])


def test_donor_background_enter_is_order_anchor_while_heartbeat_proves_freshness():
    common = dict(now_boottime=250.0, enter_s=100.0, heartbeat_s=249.0,
                  cold_s=90.0, has_enter=True, has_heartbeat=True,
                  has_foreground_or_destroy=False, heartbeat_gaps=[],
                  heartbeat_sequence_errors=[])
    assert _scene4_lifecycle_fresh(**common)
    assert not _scene4_lifecycle_fresh(**{**common, "heartbeat_s": 247.0})
    assert not _scene4_lifecycle_fresh(**{**common, "has_foreground_or_destroy": True})


def test_donor_qualifier_failures_after_launch_deadline_do_not_invalidate_results():
    assert _scene4_qualifier_failure_is_predeadline(False, True, 129.0, 130.0)
    assert _scene4_qualifier_failure_is_predeadline(False, False, 129.9, 130.0)
    assert not _scene4_qualifier_failure_is_predeadline(False, False, 130.0, 130.0)
    assert not _scene4_qualifier_failure_is_predeadline(False, False, 131.0, 130.0)
    assert not _scene4_qualifier_failure_is_predeadline(True, False, 129.0, 130.0)


class _FakeClockDevice:
    def __init__(self, remote, sha, probe, trace="[boot] mono"):
        self.remote, self.sha, self.probe, self.trace = remote, sha, probe, trace
        self.combined_clock_reads = 0
        self.commands = []

    def shell(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("sha256sum"):
            return f"{self.sha} {self.remote}"
        if self.remote in command:
            self.combined_clock_reads += 1
            return self.probe + "\n__SCENE4_TRACE_CLOCK__\n" + self.trace
        raise AssertionError(command)


def test_clock_probe_uses_device_boottime_and_rejects_mapping_drift_or_wrong_trace_clock():
    remote, sha = "/tmp/scene4_clock_probe", "a" * 64
    probe = "boottime_s=125533.3 monotonic_s=108356.5 realtime_s=1791374012.9 proc_uptime_s=108356.6"
    def fake_session(line=probe, trace="[boot] mono"):
        return type("SessionFixture", (), {
            "config": {"scene4": {"oracle_clock_probe_remote_path": remote,
                                    "oracle_clock_probe_sha256": sha}},
            "device": _FakeClockDevice(remote, sha, line, trace), "result": {}})()
    session = fake_session()
    first = _scene4_clock_probe(session)
    assert first["boottime_s"] == 125533.3
    assert abs(first["boottime_minus_proc_uptime_s"] - 17176.7) < 1e-6
    assert first["clk_tck"] is None and first["page_size_bytes"] is None
    second = _scene4_clock_probe(session)
    assert second["trace_clock_selected"] == "boot"
    assert "__SCENE4_TRACE_CLOCK__" in session.device.commands[-2]
    assert "__SCENE4_TRACE_CLOCK__" not in session.device.commands[-1]
    drifted = fake_session()
    _scene4_clock_probe(drifted)
    drifted.device.probe = probe.replace("proc_uptime_s=108356.6", "proc_uptime_s=108358.0")
    with __import__("pytest").raises(RuntimeError, match="offset drift"):
        _scene4_clock_probe(drifted)
    wrong_trace = fake_session(trace="mono [local]")
    with __import__("pytest").raises(RuntimeError, match="trace_clock=boot"):
        _scene4_clock_probe(wrong_trace)


def test_clock_probe_preserves_valid_cpu_constants_and_rejects_negative_tck_for_cpu():
    remote, sha = "/tmp/scene4_clock_probe", "a" * 64
    base = "boottime_s=10 monotonic_s=9 realtime_s=100 proc_uptime_s=9"
    def run(constants):
        return _scene4_clock_probe(type("SessionFixture", (), {
            "config": {"scene4": {"oracle_clock_probe_remote_path": remote,
                "oracle_clock_probe_sha256": sha}},
            "device": _FakeClockDevice(remote, sha, base + " " + constants, "[boot] mono"),
            "result": {}})())
    good = run("clk_tck=100 page_size_bytes=4096")
    assert good["clk_tck"] == 100 and good["page_size_bytes"] == 4096
    assert good["cpu_clock_constants_valid"]
    invalid = run("clk_tck=-1 page_size_bytes=0")
    assert invalid["clk_tck"] is None and invalid["page_size_bytes"] is None
    assert not invalid["cpu_clock_constants_valid"]


def test_clock_probe_reads_getconf_cpu_units_once_and_caches_them():
    remote, sha = "/tmp/scene4_clock_probe", "a" * 64
    base = "boottime_s=10 monotonic_s=9 realtime_s=100 proc_uptime_s=9"
    class DeviceWithGetconf(_FakeClockDevice):
        def shell(self, command, **kwargs):
            self.commands.append(command)
            if command.startswith("sha256sum"):
                return f"{self.sha} {self.remote}"
            if self.remote in command:
                self.combined_clock_reads += 1
                suffix = ("\n__SCENE4_TRACE_CLOCK__\n[boot] mono"
                          "\n__SCENE4_CLK_TCK__\n100"
                          "\n__SCENE4_PAGE_SIZE__\n4096\n")
                return base + suffix
            raise AssertionError(command)
    session = type("SessionFixture", (), {
        "config": {"scene4": {"oracle_clock_probe_remote_path": remote,
            "oracle_clock_probe_sha256": sha}},
        "device": DeviceWithGetconf(remote, sha, base), "result": {}})()
    first = _scene4_clock_probe(session)
    second = _scene4_clock_probe(session)
    assert (first["clk_tck"], first["page_size_bytes"]) == (100, 4096)
    assert second["cpu_clock_constants_valid"]
    assert session.result["scene4_cpu_clock_constants"]["source"] == "getconf_fallback"
    assert "getconf CLK_TCK" in session.device.commands[1]
    assert "getconf CLK_TCK" not in session.device.commands[2]


def test_scene4_daemon_cpu_uses_same_identity_and_real_boottime_interval():
    pid, start_ticks = 17620, 900
    def stat_line(utime, stime, start=start_ticks):
        tail = ["S"] + ["0"] * 21
        tail[11], tail[12], tail[19], tail[20], tail[21] = map(str,
            (utime, stime, start, 123456, 42))
        return f"{pid} (memshiftd) " + " ".join(tail)
    class Device:
        def __init__(self, samples): self.samples = list(samples)
        def shell(self, command, **kwargs):
            if command == "cat /proc/meminfo": return "MemAvailable: 1000 kB\nMemFree: 500 kB\nSwapFree: 0 kB\n"
            if "zram" in command: return "PATH:/sys/block/zram0/mm_stat\n1 2 3\n"
            if command == "ps -A -o PID,NAME,ARGS":
                return f"PID NAME ARGS\n{pid} memshiftd memshiftd --config /x/resident.ini\n"
            if "__SCENE4_DAEMON_PRE__" in command:
                return self.samples.pop(0)
            raise AssertionError(command)
    def sample(pre, post, before_utime, before_stime, after_utime, after_stime,
               before_start=start_ticks, after_start=start_ticks):
        return (f"__SCENE4_DAEMON_PRE__\n{pre} 0\n"
            f"__SCENE4_DAEMON_STAT_BEFORE__\n{stat_line(before_utime,before_stime,before_start)}\n"
            f"__SCENE4_DAEMON_STATUS__\nUid:\t0\t0\t0\t0\nVmRSS:\t2048 kB\n"
            f"__SCENE4_DAEMON_STAT_AFTER__\n{stat_line(after_utime,after_stime,after_start)}\n"
            f"__SCENE4_DAEMON_POST__\n{post} 0\n")
    prior = {}
    device = Device([sample(100.0,100.1,100,50,150,50),
                     sample(101.0,101.1,150,50,175,50)])
    first = _scene4_system_sample(device, prior, clk_tck=100,
        page_size_bytes=4096, reference_boottime_s=100.0)["daemon_rows"][0]
    second = _scene4_system_sample(device, prior, clk_tck=100,
        page_size_bytes=4096, reference_boottime_s=101.0)["daemon_rows"][0]
    assert first["identity_verified"] and first["cpu_delta_since_previous_sample"] is None
    assert first["VmRSS_bytes"] == 2048 * 1024
    cpu = second["cpu_delta_since_previous_sample"]
    assert second["identity_verified"] and cpu["elapsed_boottime_s"] == pytest.approx(1.0)
    assert cpu["total_ticks"] == 25 and cpu["cpu_percent_one_core"] == pytest.approx(25.0)

    changed = Device([sample(102.0,102.1,175,50,180,50,after_start=start_ticks+1)])
    gap = _scene4_system_sample(changed, prior, clk_tck=100)["daemon_rows"][0]
    assert not gap["identity_verified"] and gap["cpu_delta_since_previous_sample"] is None


def test_primary_count_is_null_until_scene4_cumulative_measurement_exists():
    partial = _scene4_primary_count_snapshot({"cases": [{"case_id": "c1"}],
                                             "measurement_complete": False})
    assert partial == {"primary_count": None, "primary_count_observed": False,
                       "measurement_complete": False}
    complete = _scene4_primary_count_snapshot({
        "cases": [{"measurement_complete": True} for _ in range(4)],
        "cumulative_counts_observed": True,
        "cumulative_counts": {"primary_count": 9}, "measurement_complete": True})
    assert complete["primary_count"] == 9 and complete["primary_count_observed"]


def test_daemon_sampler_uses_explicit_pid_column_and_excludes_grep_processes():
    ps = "PID NAME ARGS\n17620 memshiftd memshiftd --config /x/resident.ini\n" \
         "58176 sh sh -c ps -A -o PID,NAME,ARGS | grep memshiftd\n" \
         "58179 grep grep memshiftd\n"
    rows = _scene4_daemon_ps_rows(ps)
    assert len(rows) == 1 and rows[0]["pid"] == 17620


class _FakeDevice:
    def __init__(self, files):
        self.files = files

    def shell(self, command, **kwargs):
        parts = shlex.split(command)
        if parts[0] == "cat":
            return self.files.get(parts[1], "")
        if parts[:2] == ["mv", "-f"]:
            self.files[parts[3]] = self.files.pop(parts[2], "")
            return ""
        if parts[0] == "test":
            return "present" if parts[2] in self.files else "absent"
        raise AssertionError(command)


def test_schedule_invalidation_moves_only_this_run_and_reads_back():
    active = "/oracle/oracle_schedule.tsv"
    previous = active + ".previous"
    payload = "event_id|case_id\nrun42_c0001_oracle|run42_c0001\n"
    device = _FakeDevice({active: payload, previous: "older schedule"})
    result = _scene4_invalidate_schedule(type("Session", (), {"id": "run42", "device": device})(), active)
    assert result["verified"] and result["status"] == "invalidated"
    assert active not in device.files
    assert device.files[previous] == "older schedule"
    assert device.files[result["retired_path"]] == payload


def test_scene4_batched_memcg_snapshot_preserves_exact_identity_in_one_hdc_call():
    bundle = "io.memscene.scene4.workload.a1"
    uid = 20020188
    pid = 29274
    tail = ["S"] + ["0"] * 18 + ["13444813", "8681291776", "577586"]
    stat = f"{pid} (scene4.workload.a1) " + " ".join(tail)
    status = f"Name:\tscene4.workload.a1\nUid:\t{uid} {uid} {uid} {uid}\nRssAnon:\t2097152 kB\n"
    cgroup = f"1:freezer:/Frozen\n4:memory:/100/{bundle}_{uid}\n"
    raw = (
        f"__MEMCG_PIDS__\n{pid}\n__PROC_BEGIN__={pid}\n"
        f"__STAT_BEFORE__\n{stat}\n__STATUS__\n{status}"
        f"__STAT_AFTER__\n{stat}\n__CMDLINE__\n{bundle}\n"
        f"__CGROUP__\n{cgroup}__SMAPS_ROLLUP__\nPss_Anon: 3145728 kB\n__PROC_END__\n"
        "__MEMORY_CURRENT__\n2286391296\n__LEAF_EXISTS__\nyes\n")
    class FakeDevice:
        calls = 0
        last_command = ""
        def app(self, name):
            assert name == bundle
            return {"uid": uid}
        def shell(self, command, **kwargs):
            self.calls += 1
            self.last_command = command
            assert "__PROC_BEGIN__" in command
            return raw
    device = FakeDevice()
    snapshot = scene4_runner._scene4_memcg_snapshot(device, bundle, include_smaps=True)
    row = snapshot["processes"][f"{pid}:13444813"]
    assert device.calls == 1
    assert snapshot["raw_cgroup_procs"] == str(pid)
    assert snapshot["memory_current_bytes"] == 2286391296
    assert snapshot["leaf_exists"]
    assert row["verified"] and row["exact_cgroup_leaf"] and row["bundle_in_cmdline"]
    assert row["uid"] == uid and row["start_ticks"] == 13444813
    assert row["resident_anon_bytes"] == 3221225472
    assert scene4_runner._scene4_resident_anon_bytes(device, snapshot) == 3221225472
    fast_snapshot = scene4_runner._scene4_memcg_snapshot(device, bundle, include_smaps=False)
    fast_row = fast_snapshot["processes"][f"{pid}:13444813"]
    assert fast_row["resident_anon_bytes"] == 2147483648
    assert fast_row["resident_anon_source"] == "proc_status:RssAnon"
    assert "smaps_rollup" not in device.last_command


def test_popup_recovery_wait_precedes_each_fresh_donor_preparation():
    source = inspect.getsource(scene4_runner.scene4)
    initial_restore = source.index('popup_preparations_by_case[donor_case_id] = session.restore_native_low_memory_dialog')
    initial_wait = source.index('time.sleep(popup_preparations_by_case[donor_case_id]["recovery_wait_seconds"])')
    initial_launch = source.index('def launch_donor(bundle: str)')
    next_restore = source.index('popup_preparations_by_case[case_id] = session.restore_native_low_memory_dialog')
    next_wait = source.index('time.sleep(popup_preparations_by_case[case_id]["recovery_wait_seconds"])')
    next_launch = source.index('def restart_donor(bundle: str)')
    later_reference = source.index('popup_preparation = popup_preparations_by_case.get(case_id')
    assert initial_restore < initial_wait < initial_launch
    assert next_restore < next_wait < next_launch < later_reference
    assert 'popup_preparation = session.restore_native_low_memory_dialog(case_id + "_pre")' not in source

def _schema2_profile_donor(index=1):
    pid = 10000 + index
    uid = 20020187 + index
    ticks = 8000000 + index
    return {
        "bundle": f"io.memscene.scene4.workload.a{index}", "uid": uid, "pid": pid,
        "start_ticks": str(ticks), "daily_ready": True, "window_ready": True,
        "daily_ready_boottime_s": 100.0, "background_since_s": 101.0,
        "aa_pid": pid, "aa_uid": uid, "aa_state": "BACKGROUND",
        "aa_observed_boottime_s": 102.0, "window_visible": False,
        "window_focused": False, "window_observed_boottime_s": 102.0,
        "proc_verified": True, "proc_start_ticks": str(ticks),
        "proc_observed_boottime_s": 102.0, "lifecycle_clear": True,
        "lifecycle_observed_boottime_s": 102.0,
        "background_evidence_source": "os_snapshot",
    }


def test_scene4_schema2_force_shrink_all_contract_supports_1_to_8_daily_ready_donors():
    assert SCENE4_V2_DONOR_SUFFIXES == SCENE4_V3_DONOR_SUFFIXES[:21]
    header = _scene4_schedule_v2_header()
    assert header[:6] == ["schema_version", "event_id", "case_id", "launch_boottime_s",
                          "planned_needed_bytes", "donor_count"]
    assert len(header) == 6 + 8 * 21
    assert not any("writeback_file_" in field or field.endswith("writeback_file_path") for field in header)

    for count in (1, 8):
        donors = [_schema2_profile_donor(index) for index in range(1, count + 1)]
        row = _scene4_schedule_v2_row("event_peak", "case", 115.0, 22_272 * 1024**2, donors)
        assert row[:6] == ["2", "event_peak", "case", "115", str(22_272 * 1024**2), str(count)]
        assert len(row) == 6 + count * 21
        assert "|".join(row).count("memscene_") == 0

    invalid = _schema2_profile_donor()
    invalid["window_visible"] = True
    with pytest.raises(ValueError, match="readiness, visibility, or identity proof"):
        _scene4_schedule_v2_row("event_peak", "case", 115.0, 1, [invalid])
    invalid = _schema2_profile_donor()
    invalid["start_ticks"] = str(int(invalid["start_ticks"]) + 1)
    with pytest.raises(ValueError, match="dynamic identity"):
        _scene4_schedule_v2_row("event_peak", "case", 115.0, 1, [invalid])


def test_scene4_schema3_writeback_contract_remains_distinct_and_unchanged():
    donors = [_schema2_profile_donor(1)]
    donors[0].update({"writeback_file_path": "memscene_case.pages", "writeback_file_dev": 10,
                      "writeback_file_ino": 20, "writeback_file_size": 4096})
    header = _scene4_schedule_v3_header()
    row = _scene4_schedule_v3_row("event_peak", "case", 115.0, 1, donors)
    assert header[0] == "schema_version" and len(header) == 6 + 8 * 25
    assert row[0] == "3" and len(row) == 6 + 25
    assert row[-4:] == ["memscene_case.pages", "10", "20", "4096"]
