from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from restore_prepared import (live_dynamic_process_group, live_process_identity, load_ui_layout,
                              build_parser, manifest_binding, navigate_url,
                              parse_page_settle_seconds, process_group_sample_command,
                              validate_window_state_dynamic)


RECTS = ((515, 281, 2090, 1394), (582, 348, 2090, 1394),
         (648, 414, 2090, 1394), (715, 481, 2090, 1394),
         (781, 76, 2090, 1394), (848, 143, 2090, 1394))
PID = 24001
START = "99200420"
UID = 20020064


def detail(i: int, visible: bool = False, *, pid: int = PID, win_id: int | None = None,
           rect=None) -> str:
    if win_id is None:
        win_id = 70 + i
    if rect is None:
        rect = RECTS[i]
    x, y, w, h = rect
    return (f"WindowName: browser{i}\nDisplayId: 0\nWinId: {win_id}\nPid: {pid}\n"
            f"IsVisible: {'true' if visible else 'false'}\n"
            f"WindowRect: [ {x}, {y}, {w}, {h} ]\n")


def manifest(*, win_id_delta: int = 0, rect_delta: int = 0, pid: int = PID,
             stable: bool = True, visible: bool = False) -> dict:
    samples = []
    for sample_index in range(3):
        rows = []
        for i in range(6):
            row_id = 70 + i + (win_id_delta if sample_index == 2 else 0)
            rect = RECTS[i]
            if rect_delta and sample_index == 2 and i == 0:
                rect = (rect[0] + rect_delta, *rect[1:])
            row_visible = visible if i == 2 and sample_index == 2 else False
            rows.append({"name": f"browser{i}", "pid": pid, "win_id": row_id,
                         "is_browser_window": True, "detail_pid": pid,
                         "detail_win_id": row_id, "is_visible": row_visible,
                         "detail_raw": detail(i, row_visible, pid=pid, win_id=row_id, rect=rect)})
        proc_rows = [{"pid": pid, "start_ticks": START, "uid": UID,
                      "verified": True, "oom_score_adj": 200,
                      "cgroup_matches_expected_leaf": True}]
        samples.append({"identity_verified": True, "browser_window_count": 6,
                        "browser_windows": rows, "processes": proc_rows,
                        "member_pids": [pid], "member_pids_after": [pid],
                        "app_score": 280, "all_browser_windows_hidden": not any(r["is_visible"] for r in rows)})
    if not stable:
        samples[-1]["processes"][0]["start_ticks"] = "99200421"
    return {"schema": "browser-donor-preparation-v1", "bundle": "com.huawei.hmos.browser",
            "uid": UID, "expected_windows": 6, "eligible_for_review": False, "samples": samples}


def wms(binding: dict, visible_indices=()) -> tuple[str, dict]:
    table = "WindowName DisplayId Pid WinId\n"
    details = {}
    for i in range(6):
        name = f"browser{i}"
        row = binding[name]
        table += f"{name} 0 {row['pid']} {row['win_id']}\n"
        details[name] = detail(i, i in visible_indices, pid=row["pid"],
                               win_id=row["win_id"], rect=row["rect"])
    return table, details


def tiny_png() -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" +
            (3120).to_bytes(4, "big") + (2080).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")


def proc_stat(pid: int, start: int) -> str:
    fields = ["S"] + ["0"] * 19
    fields[19] = str(start)
    return f"{pid} (browser-main) " + " ".join(fields) + "\n"


def proc_dump(processes: list[tuple]) -> str:
    pids = "\n".join(str(row[0]) for row in processes)
    blocks = []
    for values in processes:
        pid, start = values[:2]
        uid = values[2] if len(values) > 2 else UID
        blocks.append(f"__PROC_BEGIN__={pid}\n__STAT_BEFORE__\n{proc_stat(pid, start)}"
                      f"__STATUS__\nName:\tbrowser-main\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
                      f"__CGROUP__\n2:memory:/dev/memcg/100/com.huawei.hmos.browser_{UID}\n"
                      f"__STAT_AFTER__\n{proc_stat(pid, start)}__OOM_ADJ__\n0\n__PROC_END__\n")
    return (f"__MEMBER_PIDS_BEFORE__\n{pids}\n" + "".join(blocks) +
            f"__MEMBER_PIDS_AFTER__\n{pids}\n")


class FakeHdc:
    def __init__(self, raw: str):
        self.raw = raw

    def shell(self, command: str, timeout: int = 20) -> str:
        return self.raw


class RestorePreparedTests(unittest.TestCase):
    def test_page_settle_seconds_default_and_cli_range(self) -> None:
        common = ["--hdc", "fake-hdc", "--target", "device-A",
                  "--preparation-manifest", "prep.json", "--ui-layout", "layout.json",
                  "--output", "out", "--navigate-expected-pages"]
        self.assertEqual(build_parser().parse_args(common).page_settle_seconds, 2)
        self.assertEqual(build_parser().parse_args(
            common + ["--page-settle-seconds", "5"]).page_settle_seconds, 5)
        self.assertEqual(parse_page_settle_seconds("2"), 2)
        self.assertEqual(parse_page_settle_seconds("30"), 30)
        for invalid in ("1", "31", "0", "-2", "two"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(argparse.ArgumentTypeError):
                    parse_page_settle_seconds(invalid)
        for invalid in ("1", "31"):
            with self.subTest(cli_invalid=invalid):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        build_parser().parse_args(common + ["--page-settle-seconds", invalid])

    def test_dynamic_manifest_ids_and_rects_bind_without_historical_winids(self) -> None:
        owner, start, binding = manifest_binding(manifest())
        self.assertEqual((owner, start), (PID, START))
        self.assertEqual([binding[f"browser{i}"]["win_id"] for i in range(6)], list(range(70, 76)))
        table, details = wms(binding)
        state = validate_window_state_dynamic(table, details, binding, {i: False for i in range(6)})
        self.assertEqual(state["visible_indices"], [])

    def test_rejects_manifest_identity_or_rectangle_drift_across_samples(self) -> None:
        with self.assertRaisesRegex(ValueError, "changed between preparation samples"):
            manifest_binding(manifest(win_id_delta=2))
        with self.assertRaisesRegex(ValueError, "changed between preparation samples"):
            manifest_binding(manifest(rect_delta=1))
        with self.assertRaisesRegex(ValueError, "PID/start identity changed"):
            manifest_binding(manifest(stable=False))

    def test_rejects_wrong_runtime_window_or_unexpected_visible_peer(self) -> None:
        _, _, binding = manifest_binding(manifest())
        table, details = wms(binding)
        details["browser4"] = detail(4, False, win_id=999)
        with self.assertRaisesRegex(ValueError, "WinId/name mismatch"):
            validate_window_state_dynamic(table, details, binding, {i: False for i in range(6)})
        table, details = wms(binding, visible_indices=(1,))
        with self.assertRaisesRegex(ValueError, "visibility mismatch"):
            validate_window_state_dynamic(table, details, binding, {i: i == 2 for i in range(6)})

    def test_rejects_wrong_owner_rect_and_extra_browser_window(self) -> None:
        _, _, binding = manifest_binding(manifest())
        table, details = wms(binding)
        details["browser1"] = detail(1, False, pid=PID + 1)
        with self.assertRaisesRegex(ValueError, "owner PID mismatch"):
            validate_window_state_dynamic(table, details, binding, {i: False for i in range(6)})
        table, details = wms(binding)
        wrong_rect = (RECTS[0][0] + 1, *RECTS[0][1:])
        details["browser0"] = detail(0, False, rect=wrong_rect)
        with self.assertRaisesRegex(ValueError, "WindowRect mismatch"):
            validate_window_state_dynamic(table, details, binding, {i: False for i in range(6)})
        with self.assertRaisesRegex(ValueError, "names differ"):
            validate_window_state_dynamic(table + "browser6 0 24001 76\n", details,
                                          binding, {i: False for i in range(6)})

    def test_layout_is_bound_to_process_window_and_reviewed_screenshot(self) -> None:
        _, _, binding = manifest_binding(manifest())
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shot = root / "switcher.png"
            raw = tiny_png()
            shot.write_bytes(raw)
            layout = {"schema": "browser-donor-ui-layout-v1", "device_target": "device-A",
                      "screen_size": [3120, 2080], "screenshot_path": shot.name,
                      "screenshot_sha256": hashlib.sha256(raw).hexdigest(),
                      "browser_taskbar": [1508, 2015], "windows": []}
            for i in range(6):
                row = binding[f"browser{i}"]
                layout["windows"].append({**row, "thumbnail": [100 + i, 100],
                    "tabs": [[300 + i, 200], [400 + i, 200], [500 + i, 200]],
                    "address": [700 + i, 250], "minimize": [900 + i, 100]})
            path = root / "layout.json"
            path.write_text(json.dumps(layout), encoding="utf-8")
            loaded = load_ui_layout(path, binding, "device-A")
            self.assertEqual(loaded["_windows_by_name"]["browser0"]["win_id"], 70)
            layout["windows"][0]["win_id"] = 1068
            path.write_text(json.dumps(layout), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "layout binding for browser0 is stale"):
                load_ui_layout(path, binding, "device-A")
            layout["windows"][0]["win_id"] = 70
            layout["screenshot_sha256"] = "0" * 64
            path.write_text(json.dumps(layout), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "screenshot hash mismatch"):
                load_ui_layout(path, binding, "device-A")

    def test_expected_url_is_manifest_ui_uri_as_file_url(self) -> None:
        self.assertEqual(navigate_url("/storage/Users/currentUser/memshift_bd_w1p1_20261013.html"),
                         "file:///storage/Users/currentUser/memshift_bd_w1p1_20261013.html")
        with self.assertRaisesRegex(ValueError, "unexpected browser UI URI"):
            navigate_url("file:///etc/passwd")

    def test_live_proc_sample_includes_every_field_required_for_verified_identity(self) -> None:
        command = process_group_sample_command("/dev/memcg/100/com.huawei.hmos.browser_20020064")
        for required in ("__MEMBER_PIDS_BEFORE__", "__STAT_BEFORE__", "__STATUS__",
                         "__OOM_ADJ__", "cat /proc/$p/oom_score_adj", "__CGROUP__",
                         "__STAT_AFTER__", "__MEMBER_PIDS_AFTER__"):
            with self.subTest(required=required):
                self.assertIn(required, command)

    def test_live_members_must_match_manifest_pid_start_uid_cgroup_set(self) -> None:
        data = manifest()
        # Add one renderer to every stable sample; the windows remain owned by the main PID.
        for sample in data["samples"]:
            sample["member_pids"] = [PID, PID + 1]
            sample["member_pids_after"] = [PID, PID + 1]
            sample["processes"].append({"pid": PID + 1, "start_ticks": "99200421", "uid": UID + 4,
                "verified": True, "oom_score_adj": 180, "cgroup_matches_expected_leaf": True})
        owner, _, _ = manifest_binding(data)
        result = live_process_identity(FakeHdc(proc_dump([(PID, int(START)),
                                                          (PID + 1, 99200421, UID + 4)])), data)
        self.assertEqual(result["pid"], owner)
        self.assertEqual(result["member_count"], 2)
        with self.assertRaisesRegex(ValueError, "PID set differs"):
            live_process_identity(FakeHdc(proc_dump([(PID, int(START))])), data)
        with self.assertRaisesRegex(ValueError, "start/UID/cgroup differs"):
            live_process_identity(FakeHdc(proc_dump([(PID, int(START) + 1),
                                                      (PID + 1, 99200421, UID + 4)])), data)

    def test_live_navigation_group_allows_new_renderer_but_keeps_owner_bound(self) -> None:
        # Navigation may replace/add renderers, but each fresh member must remain
        # in the browser memcg and the original main PID/start must survive.
        result = live_dynamic_process_group(FakeHdc(proc_dump([
            (PID, int(START)), (PID + 7, 99330555, UID + 7),
            (PID + 8, 99330556, UID + 8)])),
            UID, PID, START)
        self.assertEqual(result["member_pids"], [PID, PID + 7, PID + 8])
        with self.assertRaisesRegex(ValueError, "main PID/start/UID changed"):
            live_dynamic_process_group(FakeHdc(proc_dump([
                (PID, int(START) + 1), (PID + 7, 99330555, UID + 7)])), UID, PID, START)


if __name__ == "__main__":
    unittest.main()
