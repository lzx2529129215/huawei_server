#!/usr/bin/env python3
"""LibreCAD 2.2 (Ubuntu package) as the same-category substitute for 中望CAD (ZWCAD).

One cold start per operation, on the Xvfb screen (3840x2160, window maximized). Before every
run the LibreCAD config is reset to a baseline saved after the first start (welcome dialog
answered: millimetre, English). Drawings are plain DXF made by make_dxf.py: small.dxf (20
buildings, ~520 entities) and large.dxf (6,000 buildings, ~156,000 lines/circles/polylines/texts).

Operations follow the Excel list for 中望CAD (A09-S01..S10); S08 (autosave) has no reliable
trigger and is not measured:
  s01_startup        start LibreCAD (it opens an empty drawing)
  s02_edit           open small.dxf; draw 5 lines on the command line, select all, move, undo x3
  s03_open_big       open large.dxf and wait until it is loaded
  s04_zoom_pan       large.dxf: wheel zoom in/out and middle-button pan at 3 places
  s05_redraw         large.dxf: 10 x (zoom in + Redraw F5), 10 x (zoom out + Redraw)
  s06_export_pdf     large.dxf: File > Export > Export as PDF
  s07_s10_background large.dxf: hide 2 min; restore, zoom and pan
  s09_export_bg      large.dxf: start the PDF export, hide until it is written

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 librecad_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, cpu_idle, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/librecad"
BASE = Path.home() / "linux_app_mem/profiles/librecad_baseline"
CONF = Path.home() / ".config/LibreCAD"
LAUNCH = ["/usr/bin/librecad"]
CMDLINE = (3750, 2042)                          # command line entry (bottom right dock)
CANVAS = (1780, 1060)                           # middle of the drawing area
FILE_MENU, EXPORT_ITEM, EXPORT_PDF = (35, 9), (60, 206), (389, 229)
AUTO_ZOOM = (708, 38)                           # toolbar: View > Auto Zoom (whole drawing)


class Abort(RuntimeError):
    pass


def evict_list():
    libs = set()
    for line in subprocess.run(["ldd", LAUNCH[0]], capture_output=True, text=True).stdout.splitlines():
        m = re.search(r"=> (/\S+)", line)
        if m:
            libs.add(m.group(1))
    return sorted(libs) + [LAUNCH[0], "/usr/share/librecad", "/usr/lib/librecad", "/usr/share/fonts", str(CONF)]


def restore_profile():
    CONF.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", "--delete", f"{BASE}/", f"{CONF}/"], check=True)


def visible(pattern):
    for x in X(["xdotool", "search", "--onlyvisible", "--name", pattern], check=False).stdout.split():
        return x
    return None


def wait_visible(pattern, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        x = visible(pattern)
        if x:
            return x
        time.sleep(0.3)
    raise Abort(f"no window matching {pattern!r}")


def title(w):
    return X(["xdotool", "getwindowname", w], check=False).stdout.strip()


def file_dialog(r, d, path):
    """Qt file dialog: type the full path into the file name field and confirm."""
    name = title(d)
    r.key(d, "ctrl+a"); r.type(d, str(path), delay_ms=15); time.sleep(0.5); r.key(d, "Return"); time.sleep(2)
    if visible(f"^{re.escape(name)}$") == d:      # completer popup ate Return: press the OK/Save button
        g = dict(l.split("=") for l in X(["xdotool", "getwindowgeometry", "--shell", d]).stdout.split())
        click(d, int(g["WIDTH"]) - 52, int(g["HEIGHT"]) - 53); time.sleep(2)


def prepare(r):
    r.launch(LAUNCH)
    w = r.wait_window("LibreCAD - ", timeout=90)
    r.mark("settle"); settle(r, timeout=90)
    r.maximize(w); time.sleep(2)
    if "WIDTH=3840" not in X(["xdotool", "getwindowgeometry", "--shell", w]).stdout:
        raise Abort("could not maximize the main window")
    others = [n for x, n in r.windows() if x != w and n not in ("LibreCAD", "Qt Selection Owner for librecad", "")]
    if others:
        raise Abort(f"unexpected window(s) at start: {others}")
    settle(r, timeout=30); r.shot("loaded")
    return w


def open_drawing(r, w, path, timeout=600):
    r.key(w, "ctrl+o"); d = wait_visible("^Open")
    file_dialog(r, d, path)
    end = time.monotonic() + timeout
    while path.name not in title(w):
        if time.monotonic() > end:
            raise Abort(f"{path.name} did not open (title: {title(w)[:80]})")
        time.sleep(0.5)
    settle(r, timeout=timeout); cpu_idle(r)
    click(w, *AUTO_ZOOM); time.sleep(1); cpu_idle(r, timeout=300); settle(r, timeout=120)   # show the whole drawing


def command(r, *lines):
    X(["xdotool", "mousemove", str(CMDLINE[0]), str(CMDLINE[1]), "click", "1"]); time.sleep(0.3)
    for s in lines:
        X(["xdotool", "type", "--delay", "20", "--", s]); X(["xdotool", "key", "Return"]); time.sleep(0.6)


def wheel(x, y, button, n):
    X(["xdotool", "mousemove", str(x), str(y)])
    X(["xdotool", "click", "--repeat", str(n), "--delay", "150", str(button)])


def pan(x, y, dx, dy):
    X(["xdotool", "mousemove", str(x), str(y), "mousedown", "2"])
    for i in range(1, 21):
        X(["xdotool", "mousemove", str(x + dx * i // 20), str(y + dy * i // 20)]); time.sleep(0.03)
    X(["xdotool", "mouseup", "2"])


def start_pdf_export(r, w, out):
    click(w, *FILE_MENU); time.sleep(1.2); click(w, *EXPORT_ITEM); time.sleep(1.2); click(w, *EXPORT_PDF)
    d = wait_visible("^Export as PDF$"); time.sleep(0.5)
    file_dialog(r, d, out)


def wait_file(out, timeout=900):
    t0 = time.monotonic(); last = -1; stable = 0
    while stable < 3:
        if time.monotonic() - t0 > timeout:
            raise Abort("PDF was not written")
        size = out.stat().st_size if out.exists() else -1
        stable = stable + 1 if size == last and size > 0 else 0
        last = size; time.sleep(1)
    return round(time.monotonic() - t0 - 3, 1)


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_edit(r, d):
    w = prepare(r)
    r.mark("edit"); open_drawing(r, w, d / "small.dxf", 60)
    for k in range(5):                                       # 5 lines through the plan
        command(r, "line", f"{5 + k * 5},{60 + k * 20}", f"{290 - k * 5},{150 + k * 10}")
        X(["xdotool", "key", "Escape"]); time.sleep(0.3); X(["xdotool", "key", "Escape"]); time.sleep(0.5)
    click(w, *CANVAS); time.sleep(0.3); r.key(w, "ctrl+a"); time.sleep(1)       # select all, move by (50,30)
    command(r, "move", "0,0", "50,30"); time.sleep(1)
    q = visible("Move/Copy")
    if q:
        r.key(q, "Return"); time.sleep(1.5)
    X(["xdotool", "key", "Escape"]); time.sleep(0.5)
    r.shot("edited")
    for _ in range(3):
        r.key(w, "ctrl+z"); time.sleep(1)
    settle(r, timeout=30); r.shot("undone")
    r.mark("idle", moved=bool(q)); time.sleep(30)


def s03_open_big(r, d):
    w = prepare(r)
    r.mark("open_big"); t0 = time.monotonic(); open_drawing(r, w, d / "large.dxf"); r.shot("large")
    r.mark("idle", open_s=round(time.monotonic() - t0, 1)); time.sleep(30)


def s04_zoom_pan(r, d):
    w = prepare(r)
    r.mark("open"); open_drawing(r, w, d / "large.dxf")
    r.mark("zoom_pan")
    for (x, y) in ((1200, 700), (2600, 1400), (1780, 1060)):
        wheel(x, y, 4, 8); time.sleep(1)
        pan(x, y, 600, 300); time.sleep(1); pan(x + 600, y + 300, -900, -500); time.sleep(1)
        wheel(x, y, 5, 8); time.sleep(1)
    settle(r, timeout=60); r.shot("zoomed")
    r.mark("idle"); time.sleep(30)


def s05_redraw(r, d):
    w = prepare(r)
    r.mark("open"); open_drawing(r, w, d / "large.dxf")
    r.mark("redraw"); X(["xdotool", "mousemove", *map(str, CANVAS)])
    for k in ["ctrl+plus"] * 10 + ["ctrl+minus"] * 10:
        r.key(w, k); time.sleep(0.8); r.key(w, "F5"); time.sleep(1.2)
    settle(r, timeout=60); r.shot("redrawn")
    r.mark("idle"); time.sleep(30)


def s06_export_pdf(r, d):
    w = prepare(r)
    r.mark("open"); open_drawing(r, w, d / "large.dxf")
    out = d / "large.pdf"
    r.mark("export"); start_pdf_export(r, w, out); secs = wait_file(out); settle(r, timeout=60); r.shot("exported")
    r.mark("idle", export_s=secs, size=out.stat().st_size); time.sleep(30)


def s07_s10_background(r, d):
    w = prepare(r)
    r.mark("open"); open_drawing(r, w, d / "large.dxf")
    r.mark("idle_fg"); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    wheel(*CANVAS, 4, 6); time.sleep(1); pan(*CANVAS, 700, 400); time.sleep(1); r.shot("restored")
    r.mark("idle"); time.sleep(30)


def s09_export_bg(r, d):
    w = prepare(r)
    r.mark("open"); open_drawing(r, w, d / "large.dxf")
    out = d / "large.pdf"
    r.mark("export_start"); start_pdf_export(r, w, out)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    secs = wait_file(out); time.sleep(5)
    r.mark("restore", export_s=secs, size=out.stat().st_size)
    X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(3); r.shot("restored")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_edit, s03_open_big, s04_zoom_pan, s05_redraw, s06_export_pdf, s07_s10_background, s09_export_bg]

if __name__ == "__main__":
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    run_trials("librecad", RUNS, M, evict_list(), "[/]usr/bin/librecad", before_each=restore_profile)
