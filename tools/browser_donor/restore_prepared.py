#!/usr/bin/env python3
"""Restore the named 6x3 offline browser workload into existing tabs.

This is intentionally separate from reload_prepared.py: it navigates existing
tabs to manifest URLs after a browser restore/reboot, and binds WMS identities
to a fresh preparation manifest instead of assuming a historical PID/WinId.
Default mode only validates inputs and writes a dry-run plan. Device UI input
requires both --execute and --navigate-expected-pages.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from prepare import DEFAULT_BUNDLE, Hdc, proc_rows  # noqa: E402
from reload_prepared import (SCREEN_CAP_PREFIX, SCREEN_HEIGHT, SCREEN_WIDTH,
                             Runner, parse_wms_detail, parse_wms_table, png_dimensions,
                             sha256_file, verify_device_pages, write_report)  # noqa: E402


def rect_from_detail(raw: str) -> tuple[int, int, int, int]:
    detail = parse_wms_detail(raw)
    return tuple(detail["rect"])


def manifest_binding(prep: dict) -> tuple[int, str, dict[str, dict]]:
    """Return strict main-process and WMS binding extracted from all samples."""
    if prep.get("schema") != "browser-donor-preparation-v1" or prep.get("bundle") != DEFAULT_BUNDLE:
        raise ValueError("preparation manifest is not the browser donor schema/bundle")
    if prep.get("expected_windows") != 6:
        raise ValueError("preparation manifest must describe six browser windows")
    package_uid = prep.get("uid")
    if not isinstance(package_uid, int) or package_uid <= 0:
        raise ValueError("preparation manifest lacks a valid package UID")
    samples = prep.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        raise ValueError("preparation manifest needs at least two fresh samples")
    sample_bindings = []
    sample_members = []
    owner_pid_values = set()
    owner_starts = set()
    for sample in samples:
        if sample.get("identity_verified") is not True or sample.get("browser_window_count") != 6:
            raise ValueError("each preparation sample must verify six browser windows and process identity")
        if sample.get("all_browser_windows_hidden") is not True or \
                not isinstance(sample.get("app_score"), int) or sample["app_score"] <= 0:
            raise ValueError("each preparation sample must show all browser windows hidden and positive app_score")
        process_rows = sample.get("processes", [])
        processes = {int(proc.get("pid", -1)): proc for proc in process_rows}
        member_ids = sorted(int(value) for value in sample.get("member_pids", []))
        member_ids_after = sorted(int(value) for value in sample.get("member_pids_after", []))
        if (not processes or len(processes) != len(process_rows) or
                member_ids != member_ids_after or member_ids != sorted(processes) or
                any(proc.get("verified") is not True or proc.get("cgroup_matches_expected_leaf") is not True or
                    proc.get("oom_score_adj") is None or proc.get("oom_score_adj") <= 0 or
                    not str(proc.get("start_ticks", "")).isdecimal() or
                    not isinstance(proc.get("uid"), int) or proc["uid"] <= 0
                    for proc in process_rows)):
            raise ValueError("preparation sample has incomplete or unstable browser member PID/start/UID/cgroup evidence")
        member_signature = tuple((pid, str(proc["start_ticks"]), int(proc["uid"]))
                                 for pid, proc in sorted(processes.items()))
        sample_members.append(member_signature)
        rows = sorted((row for row in sample.get("browser_windows", [])
                       if row.get("is_browser_window") is True), key=lambda row: row.get("name", ""))
        if len(rows) != 6 or [row.get("name") for row in rows] != [f"browser{i}" for i in range(6)]:
            raise ValueError("each preparation sample must contain browser0..browser5 exactly once")
        pids = {int(row.get("pid", -1)) for row in rows}
        if len(pids) != 1 or min(pids) <= 0:
            raise ValueError("browser windows must share one positive main PID")
        owner_pid = next(iter(pids))
        owner = processes.get(owner_pid)
        if not owner or owner.get("verified") is not True:
            raise ValueError("browser window owner lacks verified process identity in preparation sample")
        start_ticks = str(owner.get("start_ticks", ""))
        if not start_ticks.isdecimal() or int(start_ticks) <= 0:
            raise ValueError("browser owner start_ticks missing or invalid")
        if owner.get("uid") != package_uid:
            raise ValueError("browser main process UID differs from package UID")
        binding = {}
        win_ids = set()
        for index, row in enumerate(rows):
            name = f"browser{index}"
            win_id = int(row.get("win_id", -1))
            detail_raw = row.get("detail_raw", "")
            detail = parse_wms_detail(detail_raw)
            rect = rect_from_detail(detail_raw)
            if (win_id <= 0 or win_id in win_ids or row.get("detail_pid") != owner_pid or
                    row.get("detail_win_id") != win_id or detail["name"] != name or
                    detail["pid"] != owner_pid or detail["win_id"] != win_id or
                    row.get("pid") != owner_pid or row.get("is_visible") is not False or
                    detail["visible"] is not False):
                raise ValueError(f"{name} WMS/detail owner, identity, or hidden state is invalid")
            win_ids.add(win_id)
            binding[name] = {"name": name, "pid": owner_pid, "start_ticks": start_ticks,
                             "win_id": win_id, "rect": list(rect)}
        signature = tuple((key, val["pid"], val["start_ticks"], val["win_id"], tuple(val["rect"]))
                          for key, val in binding.items())
        sample_bindings.append(signature)
        owner_pid_values.add(owner_pid)
        owner_starts.add(start_ticks)
    if len(owner_pid_values) != 1 or len(owner_starts) != 1:
        raise ValueError("browser owner PID/start identity changed between preparation samples")
    if len(set(sample_bindings)) != 1:
        raise ValueError("browser window name/PID/start/WinId/WindowRect changed between preparation samples")
    if len(set(sample_members)) != 1:
        raise ValueError("browser member PID/start/UID set changed between preparation samples")
    return next(iter(owner_pid_values)), next(iter(owner_starts)), binding


def validate_window_state_dynamic(raw_table: str, details_raw: dict[str, str],
                                  binding: dict[str, dict],
                                  expected_visibility: dict[int, bool]) -> dict:
    table = parse_wms_table(raw_table)
    expected_names = set(binding)
    if expected_names != {f"browser{i}" for i in range(6)} or set(table) != expected_names:
        raise ValueError(f"WMS browser window names differ from bound manifest: {sorted(table)}")
    observed_ids = [row[1] for row in table.values()]
    if len(set(observed_ids)) != 6:
        raise ValueError("WMS browser WinIds are not unique")
    out = {}
    for index in range(6):
        name = f"browser{index}"
        expected = binding[name]
        row_pid, row_id = table[name]
        detail = parse_wms_detail(details_raw[name])
        if row_pid != expected["pid"] or detail["pid"] != expected["pid"]:
            raise ValueError(f"{name} owner PID mismatch against fresh preparation manifest")
        if row_id != expected["win_id"] or detail["win_id"] != expected["win_id"] or detail["name"] != name:
            raise ValueError(f"{name} WinId/name mismatch against fresh preparation manifest")
        if list(detail["rect"]) != expected["rect"]:
            raise ValueError(f"{name} WindowRect mismatch against fresh preparation manifest")
        if index in expected_visibility and detail["visible"] is not expected_visibility[index]:
            raise ValueError(f"{name} visibility mismatch: expected {expected_visibility[index]}, observed {detail['visible']}")
        out[name] = {"pid": detail["pid"], "win_id": detail["win_id"],
                     "visible": detail["visible"], "rect": list(detail["rect"])}
    visible = [i for i in range(6) if out[f"browser{i}"]["visible"]]
    expected_visible = [i for i, shown in expected_visibility.items() if shown]
    if visible != expected_visible:
        raise ValueError(f"visible browser window set mismatch: expected {expected_visible}, observed {visible}")
    return {"windows": out, "visible_indices": visible}


def load_ui_layout(path: Path, binding: dict[str, dict], target: str) -> dict:
    layout = json.loads(path.read_text(encoding="utf-8-sig"))
    if layout.get("schema") != "browser-donor-ui-layout-v1" or layout.get("device_target") != target:
        raise ValueError("UI layout must be reviewed for this exact device target")
    size = layout.get("screen_size")
    if size != [SCREEN_WIDTH, SCREEN_HEIGHT]:
        raise ValueError(f"UI layout screen_size must match captured {SCREEN_WIDTH}x{SCREEN_HEIGHT}")
    windows = layout.get("windows")
    if not isinstance(windows, list) or len(windows) != 6:
        raise ValueError("UI layout must define exactly six window controls")
    by_name = {row.get("name"): row for row in windows}
    if set(by_name) != set(binding):
        raise ValueError("UI layout window names differ from manifest binding")
    points = []
    for name, expected in binding.items():
        row = by_name[name]
        if (row.get("pid") != expected["pid"] or row.get("start_ticks") != expected["start_ticks"] or
                row.get("win_id") != expected["win_id"] or row.get("rect") != expected["rect"]):
            raise ValueError(f"UI layout binding for {name} is stale; PID/start/WinId/WindowRect must match manifest")
        for field in ("thumbnail", "address", "minimize"):
            points.append(parse_point(row.get(field), f"{name}.{field}"))
        tabs = row.get("tabs")
        if not isinstance(tabs, list) or len(tabs) != 3:
            raise ValueError(f"UI layout must define three tab coordinates for {name}")
        points.extend(parse_point(value, f"{name}.tabs[{i}]") for i, value in enumerate(tabs))
    points.append(parse_point(layout.get("browser_taskbar"), "browser_taskbar"))
    if any(not (0 <= x < SCREEN_WIDTH and 0 <= y < SCREEN_HEIGHT) for x, y in points):
        raise ValueError("UI layout includes a point outside the captured display")
    screenshot_value = layout.get("screenshot_path")
    screenshot_sha = layout.get("screenshot_sha256")
    if not isinstance(screenshot_value, str) or not screenshot_value or \
            not isinstance(screenshot_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", screenshot_sha):
        raise ValueError("UI layout must cite a reviewed local screenshot and its SHA-256")
    screenshot_path = (path.resolve().parent / screenshot_value).resolve(strict=True)
    if sha256_file(screenshot_path).lower() != screenshot_sha.lower():
        raise ValueError("UI layout reference screenshot hash mismatch")
    width, height = png_dimensions(screenshot_path.read_bytes()[:32])
    if (width, height) != (SCREEN_WIDTH, SCREEN_HEIGHT):
        raise ValueError("UI layout screenshot dimensions differ from captured display")
    return {**layout, "_windows_by_name": by_name}


def parse_point(value, label: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2 or any(not isinstance(v, int) for v in value):
        raise ValueError(f"{label} must be an [x,y] integer pair")
    return value[0], value[1]


def live_process_identity(hdc: Hdc, prep: dict) -> dict:
    """Sandwich-read the full registered process group and match PID/start/UID."""
    uid = int(prep["uid"])
    leaf = f"/dev/memcg/100/{DEFAULT_BUNDLE}_{uid}"
    raw = hdc.shell(process_group_sample_command(leaf), timeout=20)
    before = re.search(r"(?ms)^__MEMBER_PIDS_BEFORE__\n(.*?)(?=^__PROC_BEGIN__=\d+\s*$)", raw)
    after = re.search(r"(?ms)^__MEMBER_PIDS_AFTER__\n(.*)\Z", raw)
    if not before or not after:
        raise ValueError("fresh process-group sandwich sample is incomplete")
    parse_ids = lambda value: sorted({int(token) for token in value.split() if token.isdecimal()})
    before_ids, after_ids = parse_ids(before.group(1)), parse_ids(after.group(1))
    actual_rows = proc_rows(raw, DEFAULT_BUNDLE, f"{DEFAULT_BUNDLE}_{uid}")
    actual = {int(row["pid"]): row for row in actual_rows}
    expected_rows = prep["samples"][-1].get("processes", [])
    expected = {int(row["pid"]): row for row in expected_rows}
    if (not before_ids or before_ids != after_ids or before_ids != sorted(actual) or
            before_ids != sorted(expected)):
        raise ValueError("fresh browser process-group PID set differs from preparation manifest")
    for pid in before_ids:
        got, want = actual[pid], expected[pid]
        if (got.get("verified") is not True or want.get("verified") is not True or
                str(got.get("start_ticks")) != str(want.get("start_ticks")) or
                got.get("uid") != want.get("uid") or
                got.get("cgroup_matches_expected_leaf") is not True or
                want.get("cgroup_matches_expected_leaf") is not True):
            raise ValueError(f"browser process {pid} start/UID/cgroup differs from preparation manifest")
    owner_pid = int(next(row["pid"] for row in prep["samples"][-1]["browser_windows"]
                         if row.get("is_browser_window") is True))
    owner = actual[owner_pid]
    if owner.get("uid") != uid:
        raise ValueError("browser main process UID differs from package UID")
    return {"pid": owner_pid, "start_ticks": str(owner["start_ticks"]), "uid": uid,
            "member_count": len(before_ids), "member_pid_start_uid_sha256": hashlib.sha256(
                "\n".join(f"{pid}:{actual[pid]['start_ticks']}:{actual[pid]['uid']}"
                          for pid in before_ids).encode("ascii")).hexdigest(),
            "cgroup_leaf": leaf}


def live_dynamic_process_group(hdc: Hdc, uid: int, owner_pid: int,
                               owner_start: str) -> dict:
    """Verify the current (possibly renderer-changed) group is still coherent."""
    leaf = f"/dev/memcg/100/{DEFAULT_BUNDLE}_{uid}"
    raw = hdc.shell(process_group_sample_command(leaf), timeout=20)
    before = re.search(r"(?ms)^__MEMBER_PIDS_BEFORE__\n(.*?)(?=^__PROC_BEGIN__=\d+\s*$)", raw)
    after = re.search(r"(?ms)^__MEMBER_PIDS_AFTER__\n(.*)\Z", raw)
    if not before or not after:
        raise ValueError("current process-group sandwich sample is incomplete")
    ids = lambda value: sorted({int(token) for token in value.split() if token.isdecimal()})
    before_ids, after_ids = ids(before.group(1)), ids(after.group(1))
    rows = {int(row["pid"]): row for row in proc_rows(raw, DEFAULT_BUNDLE, f"{DEFAULT_BUNDLE}_{uid}")}
    if not before_ids or before_ids != after_ids or before_ids != sorted(rows):
        raise ValueError("browser process-group membership changed during live identity sampling")
    if owner_pid not in rows:
        raise ValueError("browser main PID is no longer in the registered process group")
    for pid, row in rows.items():
        if row.get("verified") is not True or row.get("cgroup_matches_expected_leaf") is not True:
            raise ValueError(f"browser group member {pid} failed live stat/UID/cgroup verification")
    owner = rows[owner_pid]
    if str(owner.get("start_ticks")) != str(owner_start) or owner.get("uid") != uid:
        raise ValueError("browser main PID/start/UID changed during page navigation")
    signature = "\n".join(f"{pid}:{rows[pid]['start_ticks']}:{rows[pid]['uid']}" for pid in before_ids)
    return {"member_pids": before_ids, "member_count": len(before_ids),
            "member_pid_start_uid_sha256": hashlib.sha256(signature.encode("ascii")).hexdigest(),
            "owner_pid": owner_pid, "owner_start_ticks": str(owner_start), "cgroup_leaf": leaf}


def process_group_sample_command(leaf: str) -> str:
    """Keep live samples compatible with prepare.proc_rows' full identity schema."""
    return (f"echo __MEMBER_PIDS_BEFORE__; cat {shlex.quote(leaf + '/cgroup.procs')}; "
            f"for p in $(cat {shlex.quote(leaf + '/cgroup.procs')}); do "
            "echo __PROC_BEGIN__=$p; echo __STAT_BEFORE__; cat /proc/$p/stat; "
            "echo __STATUS__; cat /proc/$p/status; echo __OOM_ADJ__; cat /proc/$p/oom_score_adj; "
            "echo __CGROUP__; cat /proc/$p/cgroup; echo __STAT_AFTER__; cat /proc/$p/stat; "
            "echo __PROC_END__; done; "
            f"echo __MEMBER_PIDS_AFTER__; cat {shlex.quote(leaf + '/cgroup.procs')}")


def navigate_url(uri: str) -> str:
    if not isinstance(uri, str) or not uri.startswith("/storage/Users/currentUser/") or " " in uri:
        raise ValueError(f"unexpected browser UI URI: {uri!r}")
    return "file://" + uri


def parse_page_settle_seconds(value: str) -> int:
    try:
        seconds = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("page settle seconds must be an integer from 2 through 30") from exc
    if not 2 <= seconds <= 30:
        raise argparse.ArgumentTypeError("page settle seconds must be from 2 through 30")
    return seconds


class RestoreRunner(Runner):
    def __init__(self, *args, binding: dict[str, dict], **kwargs):
        super().__init__(*args, **kwargs)
        self.binding = binding

    def wms(self, expected_visibility: dict[int, bool]) -> dict:
        table = self.hdc.shell("hidumper -s WindowManagerService -a '-a'", timeout=20)
        rows = parse_wms_table(table)
        details = {}
        for name, (_, win_id) in sorted(rows.items()):
            details[name] = self.hdc.shell(
                "hidumper -s WindowManagerService -a " + shlex.quote(f"-w {win_id}"), timeout=15)
        state = validate_window_state_dynamic(table, details, self.binding, expected_visibility)
        state["raw_table"] = table
        state["raw_details"] = details
        return state

    def input_text(self, value: str) -> str:
        return self.shell_checked("uitest uiInput text " + shlex.quote(value) + "; __rc=$?; echo __TEXT_RC=$__rc",
                                  timeout=20, marker="TEXT_RC")

    def key_event(self, key: int, *modifiers: int) -> str:
        keys = " ".join(str(value) for value in (key, *modifiers))
        return self.shell_checked(f"uitest uiInput keyEvent {keys}; __rc=$?; echo __KEY_RC=$__rc",
                                  timeout=20, marker="KEY_RC")


def validate_inputs(preparation_path: Path, workload_path: Path, layout_path: Path,
                    target: str) -> tuple[dict, dict, dict, int, str]:
    prep = json.loads(preparation_path.read_text(encoding="utf-8-sig"))
    workload = json.loads(workload_path.read_text(encoding="utf-8-sig"))
    if workload.get("schema") != "browser-donor-workload-v1" or \
            workload.get("window_count") != 6 or workload.get("pages_per_window") != 3:
        raise ValueError("workload must specify six windows by three existing tabs")
    if workload.get("expected_page_sha256") != "2a791ac3d6567f14d5613f03cad6504b1f6fe9050e5e28851d5c6f3184db0506":
        raise ValueError("workload source hash differs from reviewed offline page content")
    if prep.get("device_target") != target:
        raise ValueError("selected HDC target differs from preparation manifest")
    owner_pid, owner_start, binding = manifest_binding(prep)
    layout = load_ui_layout(layout_path, binding, target)
    pages = [page for window in workload.get("windows", []) for page in window.get("pages", [])]
    if len(pages) != 18 or len({page.get("label") for page in pages}) != 18:
        raise ValueError("workload must contain 18 uniquely named expected pages")
    for wi, window in enumerate(workload["windows"], 1):
        if window.get("label") != f"w{wi}" or len(window.get("pages", [])) != 3:
            raise ValueError("workload window/page labels must be ordered w1..w6 with three pages each")
        for pi, page in enumerate(window["pages"], 1):
            if page.get("label") != f"w{wi}p{pi}" or not page.get("ui_uri"):
                raise ValueError("expected page labels must match manifest window/tab order")
            local = (workload_path.resolve().parent / page["path"]).resolve(strict=True)
            if sha256_file(local).lower() != page.get("sha256", "").lower():
                raise ValueError(f"local HTML source hash mismatch for {page['label']}")
    return prep, workload, layout, owner_pid, owner_start


def restore(args: argparse.Namespace) -> dict:
    if not args.execute or not args.navigate_expected_pages:
        raise ValueError("navigation requires both --execute and --navigate-expected-pages")
    prep, workload, layout, owner_pid, owner_start = validate_inputs(
        args.preparation_manifest, args.workload_manifest, args.ui_layout, args.target)
    hdc = Hdc(str(args.hdc.resolve(strict=True)), args.target)
    live = live_process_identity(hdc, prep)
    if live["pid"] != owner_pid or live["start_ticks"] != owner_start:
        raise ValueError("fresh browser owner PID/start differs from WMS manifest binding")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "screenshots").mkdir()
    run_id = uuid.uuid4().hex[:12]
    remote_root = f"/data/local/tmp/browser_restore_{run_id}"
    runner = RestoreRunner(args.hdc, args.target, output, remote_root, owner_pid, args.guided,
                           hdc=hdc, binding=layout_binding(layout))
    report = {"schema": "browser-donor-restore-v1",
              "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "target": args.target, "bundle": DEFAULT_BUNDLE, "owner_pid": owner_pid,
              "owner_start_ticks": owner_start, "owner_identity_live": live,
              "preparation_manifest": str(args.preparation_manifest.resolve()),
              "workload_manifest": str(args.workload_manifest.resolve()),
              "ui_layout_manifest": str(args.ui_layout.resolve()),
              "ui_layout_screenshot_sha256": layout["screenshot_sha256"],
              "page_settle_seconds": args.page_settle_seconds,
              "window_binding": layout_binding(layout),
              "navigation_enabled": True, "new_tabs_created": 0, "browser_closed": False,
              "system_settings_changed": False, "status": "preflight", "actions": runner.actions,
              "screenshots": [], "verified_pages": []}
    report_path = output / "restore_report.json"
    write_report(report_path, report)
    try:
        runner.shell_checked("mkdir " + shlex.quote(remote_root) + "; __rc=$?; echo __MKDIR_RC=$__rc",
                             timeout=15, marker="MKDIR_RC")
        report["verified_pages"] = verify_device_pages(runner, workload)
        initial = runner.record("initial_screenshot", SCREEN_CAP_PREFIX,
                                lambda: runner.screenshot("initial"))
        report["screenshots"].append(initial)
        state = runner.record("preflight_all_hidden", "verify fresh WMS binding and all windows hidden",
                              lambda: runner.wms({i: False for i in range(6)}))
        report["preflight_wms"] = state
        report["status"] = "running"
        for wi, window in enumerate(workload["windows"]):
            name = f"browser{wi}"
            controls = layout["_windows_by_name"][name]
            taskbar = parse_point(layout["browser_taskbar"], "browser_taskbar")
            thumb = parse_point(controls["thumbnail"], f"{name}.thumbnail")
            runner.record("open_browser_taskbar", f"uitest click {taskbar}",
                          lambda xy=taskbar: runner.click("open_browser_taskbar", *xy))
            time.sleep(1)
            shot = runner.record("window_switcher_screenshot", SCREEN_CAP_PREFIX,
                                 lambda nm=name: runner.screenshot(nm + "_switcher"))
            report["screenshots"].append(shot)
            runner.record("select_window_thumbnail", f"uitest click {thumb}",
                          lambda xy=thumb: runner.click("select_window_thumbnail", *xy))
            time.sleep(1)
            state = runner.record("verify_selected_window", "target must be the only visible manifest-bound window",
                                  lambda i=wi: runner.wms({j: j == i for j in range(6)}))
            report.setdefault("window_checks", []).append({"window": wi + 1, "phase": "selected", "wms": state})
            selected = runner.record("selected_window_screenshot", SCREEN_CAP_PREFIX,
                                     lambda nm=name: runner.screenshot(nm + "_selected"))
            report["screenshots"].append(selected)
            if args.guided:
                runner.guided_checkpoint(f"{name} selected; verify it is the intended cascade window before navigating.")
            for pi, page in enumerate(window["pages"]):
                tab_xy = parse_point(controls["tabs"][pi], f"{name}.tabs[{pi}]")
                address_xy = parse_point(controls["address"], f"{name}.address")
                target_uri = navigate_url(page["ui_uri"])
                runner.record("select_existing_tab", f"uitest click {tab_xy}",
                              lambda xy=tab_xy: runner.click("select_existing_tab", *xy))
                runner.record("verify_tab_owner", "target browser window remains uniquely visible",
                              lambda i=wi: runner.wms({j: j == i for j in range(6)}))
                runner.record("focus_address_bar", f"uitest click {address_xy}",
                              lambda xy=address_xy: runner.click("focus_address_bar", *xy))
                runner.record("select_current_address", "uitest uiInput keyEvent Ctrl+A",
                              lambda: runner.key_event(2072, 2017))
                runner.record("enter_expected_local_url", "uitest uiInput text <manifest file URL>",
                              lambda url=target_uri: runner.input_text(url))
                runner.record("commit_navigation", "uitest uiInput keyEvent Enter",
                              lambda: runner.key_event(2054))
                time.sleep(args.page_settle_seconds)
                shot = runner.record("expected_page_screenshot", SCREEN_CAP_PREFIX,
                                     lambda label=page["label"]: runner.screenshot(label + "_navigated"))
                report["screenshots"].append(shot)
                state = runner.record("verify_navigation_window", "verify fresh WMS binding after navigation",
                                      lambda i=wi: runner.wms({j: j == i for j in range(6)}))
                live_group = runner.record(
                    "verify_live_process_group", "verify current renderer members remain in registered browser cgroup",
                    lambda: live_dynamic_process_group(runner.hdc, prep["uid"], owner_pid, owner_start))
                check = {"window": wi + 1, "tab": pi + 1, "label": page["label"],
                         "expected_url": target_uri, "screenshot": shot,
                         "wms": state, "live_process_group": live_group,
                         "visual_url_review": "pending"}
                report.setdefault("page_checks", []).append(check)
                if args.guided:
                    runner.guided_checkpoint(f"{page['label']} screenshot saved; verify selected tab, URL, and loaded table before continuing.")
                    check["visual_url_review"] = "operator_confirmed"
            minimize_xy = parse_point(controls["minimize"], f"{name}.minimize")
            runner.record("minimize_window", f"uitest click {minimize_xy}",
                          lambda xy=minimize_xy: runner.click("minimize_window", *xy))
            time.sleep(1)
            state = runner.record("verify_window_minimized", "verify selected window hidden and all peers hidden",
                                  lambda i=wi: runner.wms({j: False for j in range(6)}))
            report.setdefault("window_checks", []).append({"window": wi + 1, "phase": "minimized", "wms": state})
        final_shot = runner.record("final_all_minimized_screenshot", SCREEN_CAP_PREFIX,
                                   lambda: runner.screenshot("all_windows_minimized"))
        report["screenshots"].append(final_shot)
        report["final_hidden_wms"] = runner.record(
            "final_all_hidden_wms", "verify every manifest-bound browser window hidden",
            lambda: runner.wms({i: False for i in range(6)}))
        report["status"] = "complete_pending_visual_url_review"
    except Exception as exc:
        report["status"] = "stopped_on_error"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        try:
            shot = runner.record("failure_screenshot", SCREEN_CAP_PREFIX,
                                 lambda: runner.screenshot("failure"))
            report["screenshots"].append(shot)
            report["error"]["screenshot"] = shot["local_path"]
        except Exception as capture_exc:
            report["error"]["screenshot_error"] = f"{type(capture_exc).__name__}: {capture_exc}"
        raise
    finally:
        report["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        report["actions"] = runner.actions
        write_report(report_path, report)
    return report


def layout_binding(layout: dict) -> dict:
    return {row["name"]: {"name": row["name"], "pid": row["pid"], "win_id": row["win_id"],
                          "rect": row["rect"]}
            for row in layout["windows"]}


def dry_run(args: argparse.Namespace) -> dict:
    if not args.navigate_expected_pages:
        raise ValueError("select --navigate-expected-pages to preview existing-tab navigation")
    prep, workload, layout, owner_pid, owner_start = validate_inputs(
        args.preparation_manifest, args.workload_manifest, args.ui_layout, args.target)
    steps = []
    by_name = layout["_windows_by_name"]
    for index, window in enumerate(workload["windows"]):
        name = f"browser{index}"
        controls = by_name[name]
        steps.append({"window": name, "select_thumbnail": controls["thumbnail"]})
        for index, page in enumerate(window["pages"]):
            steps.append({"window": name, "existing_tab_index": index + 1,
                          "select_tab": controls["tabs"][index], "address_bar": controls["address"],
                          "navigate_to": navigate_url(page["ui_uri"]), "screenshot": page["label"] + "_navigated"})
        steps.append({"window": name, "minimize": controls["minimize"]})
    return {"status": "dry_run_only_no_device_input", "target": args.target,
            "owner_pid": owner_pid, "owner_start_ticks": owner_start,
            "window_binding": layout_binding(layout), "workload_pages": 18,
            "page_settle_seconds": args.page_settle_seconds,
            "steps": steps, "new_tabs_created": 0, "browser_closed": False}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hdc", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--preparation-manifest", type=Path, required=True)
    parser.add_argument("--workload-manifest", type=Path, default=HERE / "workload_6x3_20261013.json")
    parser.add_argument("--ui-layout", type=Path, required=True,
                        help="reviewed screenshot-derived UI coordinates bound to current WMS WinIds/rects")
    parser.add_argument("--output", type=Path, required=True, help="new empty local result directory")
    parser.add_argument("--navigate-expected-pages", action="store_true",
                        help="explicitly authorize navigating the 18 existing tabs to manifest URLs")
    parser.add_argument("--page-settle-seconds", type=parse_page_settle_seconds, default=2,
                        help="wait after each navigation before screenshot/minimize (2..30; default: 2)")
    parser.add_argument("--execute", action="store_true", help="allow device UI input; omitted means dry-run")
    parser.add_argument("--guided", action="store_true", help="pause for URL/table visual review after every page")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("output directory already exists; choose a new run directory")
        if not args.hdc.is_file() or not args.preparation_manifest.is_file() or \
                not args.workload_manifest.is_file() or not args.ui_layout.is_file():
            raise ValueError("HDC executable and all three manifests must exist")
        if args.execute:
            report = restore(args)
        else:
            report = dry_run(args)
            args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
            args.output.resolve().mkdir(parents=False, exist_ok=False)
            write_report(args.output.resolve() / "restore_plan.json", report)
        print(json.dumps({"status": report["status"], "owner_pid": report.get("owner_pid"),
                          "owner_start_ticks": report.get("owner_start_ticks"),
                          "planned_pages": report.get("workload_pages", 18),
                          "output": str(args.output.resolve())}, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"browser workload restore failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
