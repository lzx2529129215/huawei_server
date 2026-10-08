import csv
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT / "tools"))

import scene4_donor_mechanism_diagnostic as diag


FIELDS = ["action_id", "donor_bundle", "requested_ratio", "write_length", "errno", "status"]
A1 = "io.memscene.scene4.workload.a1"
A2 = "io.memscene.scene4.workload.a2"


def row(action_id, bundle, ratio, write_length, errno, status):
    return {
        "action_id": action_id,
        "donor_bundle": bundle,
        "requested_ratio": str(ratio),
        "write_length": str(write_length),
        "errno": str(errno),
        "status": status,
    }


def write_ledger(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_accepts_single_and_two_digit_payload_lengths_and_deduplicates(tmp_path):
    ledger = tmp_path / "ledger.csv"
    write_ledger(ledger, [
        row("one_digit", A1, 5, 1, 0, "write_accepted_feedback_measured_positive_rss_observation"),
        row("two_digit", A2, 20, 2, 0, "write_accepted_feedback_nonpositive_halt"),
        row("two_digit", A2, 20, 2, 0, "write_accepted_feedback_nonpositive_halt"),
        row("two_digit", "", "", -1, 0, "reserved_before_write_pending_result"),
    ])
    assert diag.accepted_write_ids(ledger) == ["one_digit", "two_digit"]
    assert diag.accepted_write_bundles(ledger) == {
        A1: ["one_digit"],
        A2: ["two_digit"],
    }


def test_excludes_reservation_failure_partial_wrong_length_and_errno(tmp_path):
    ledger = tmp_path / "ledger.csv"
    write_ledger(ledger, [
        row("reserved", A1, "", -1, 0, "reserved_before_write_pending_result"),
        row("failed", A1, 20, 2, 5, "write_failed_or_partial_no_retry"),
        row("partial", A1, 20, 1, 0, "write_accepted_feedback_unknown"),
        row("errno", A1, 20, 2, 5, "write_accepted_feedback_measured"),
    ])
    assert diag.accepted_write_ids(ledger) == []
    assert diag.accepted_write_bundles(ledger) == {}


def test_conflicting_terminal_rows_for_one_action_fail_closed(tmp_path):
    ledger = tmp_path / "ledger.csv"
    write_ledger(ledger, [
        row("conflict", A1, 5, 1, 0, "write_accepted_feedback_measured"),
        row("conflict", A1, 20, 2, 0, "write_accepted_feedback_unknown"),
    ])
    assert diag.accepted_write_ids(ledger) == []
    assert diag.accepted_write_bundles(ledger) == {}


def test_supports_legacy_ratio_column(tmp_path):
    ledger = tmp_path / "legacy.csv"
    with ledger.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["action_id", "donor_bundle", "ratio", "write_len", "error_no", "outcome"])
        writer.writeheader()
        writer.writerow({"action_id": "legacy", "donor_bundle": A1, "ratio": 8,
                         "write_len": 1, "error_no": 0, "outcome": "write_accepted_feedback_measured"})
    assert diag.accepted_write_ids(ledger) == ["legacy"]
    assert diag.accepted_write_bundles(ledger) == {A1: ["legacy"]}


def test_accepts_current_positive_rss_terminal_status(tmp_path):
    ledger = tmp_path / "positive.csv"
    write_ledger(ledger, [
        row("positive", A1, 7, 1, 0, "write_accepted_feedback_measured_positive_rss_observation"),
    ])
    assert diag.accepted_write_ids(ledger) == ["positive"]
    assert diag.accepted_write_bundles(ledger) == {A1: ["positive"]}


def test_covers_all_current_daemon_accepted_terminal_statuses(tmp_path):
    ledger = tmp_path / "daemon_statuses.csv"
    statuses = [
        "write_accepted_feedback_measured_positive_rss_observation",
        "write_accepted_feedback_nonpositive_halt",
        "write_accepted_feedback_unknown",
        "write_accepted_feedback_unknown_after_exception",
        "write_accepted_identity_or_background_changed_postwrite",
    ]
    write_ledger(ledger, [
        row(f"action_{i}", A1 if i % 2 == 0 else A2,
            7 if i % 2 == 0 else 20, 1 if i % 2 == 0 else 2, 0, status)
        for i, status in enumerate(statuses)
    ])
    assert diag.accepted_write_ids(ledger) == [f"action_{i}" for i in range(len(statuses))]


def test_accepts_legacy_measured_terminal_for_old_ledgers(tmp_path):
    ledger = tmp_path / "legacy_measured.csv"
    write_ledger(ledger, [row("legacy_measured", A1, 8, 1, 0,
                               "write_accepted_feedback_measured")])
    assert diag.accepted_write_ids(ledger) == ["legacy_measured"]
