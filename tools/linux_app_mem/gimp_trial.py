#!/usr/bin/env python3
"""GIMP 2.10 (Ubuntu package) as the same-category substitute for 悟空图像 (Wukong image editor).

One cold start per operation, on the Xvfb screen (3840x2160), single-window mode (the Ubuntu
default). Before every run the GIMP config dir is reset to a baseline saved after a clean first
start, and the freedesktop thumbnails of the test files are removed, so every run starts from
the same state. Materials (made once, see the stage-3 doc): photo.jpg 4000x3000 (~7 MB),
big.jpg 8000x6000 (~28 MB), layers10.xcf 4000x3000 with 10 layers (1 full-size background + 9
semi-transparent 2400x1800 layers at different offsets, ~150 MB).

Operations follow the Excel list for 悟空图像 (A06-S01..S10); GIMP has no autosave, so S08 is
not measured:
  s01_startup       start GIMP, File > New (default 1920x1080 canvas)
  s02_edit          open photo.jpg, crop with the crop tool, 4 paintbrush strokes
  s03_open_big      open big.jpg (8000x6000), view at 100 % and fit again
  s04_layers        open layers10.xcf, 5 x (select next layer, duplicate it, move it)
  s05_filters       open big.jpg, Gaussian Blur and Sharpen (Unsharp Mask) (default settings)
  s06_export        open layers10.xcf, Export As PNG (default options)
  s07_s10_background  open layers10.xcf, hide 2 min; restore and zoom the canvas
  s09_export_bg     open layers10.xcf, start the PNG export, hide until it is done

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 gimp_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import hashlib
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, cpu_idle, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/gimp"
BASE = Path.home() / "linux_app_mem/profiles/gimp_baseline"
CONF = Path.home() / ".config/GIMP/2.10"
LAUNCH = ["/usr/bin/gimp-2.10"]
MAIN = "GNU Image Manipulation Program"


class Abort(RuntimeError):
    pass


def evict_list():
    libs = set()
    for line in subprocess.run(["ldd", LAUNCH[0]], capture_output=True, text=True).stdout.splitlines():
        m = re.search(r"=> (/\S+)", line)
        if m:
            libs.add(m.group(1))
    return sorted(libs) + [LAUNCH[0], "/usr/lib/gimp/2.0", "/usr/share/gimp/2.0", "/usr/lib/x86_64-linux-gnu/gegl-0.4",
                           "/usr/lib/x86_64-linux-gnu/babl-0.1", "/usr/share/fonts", str(CONF)]


def restore_profile():
    CONF.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", "--delete", f"{BASE}/", f"{CONF}/"], check=True)
    thumbs = Path.home() / ".cache/thumbnails"
    for f in M.iterdir():                       # only the thumbnails of our own test files
        h = hashlib.md5(f.resolve().as_uri().encode()).hexdigest() + ".png"
        for p in thumbs.glob(f"*/{h}"):
            p.unlink(missing_ok=True)
        for p in thumbs.glob(f"fail/*/{h}"):
            p.unlink(missing_ok=True)


def visible(name):
    out = X(["xdotool", "search", "--onlyvisible", "--name", f"^{re.escape(name)}$"], check=False).stdout.split()
    return out[0] if out else None


def wait_visible(name, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        w = visible(name)
        if w:
            return w
        time.sleep(0.3)
    raise Abort(f"window {name!r} did not appear")


def wait_gone(name, timeout):
    t0 = time.monotonic()
    while visible(name):
        if time.monotonic() - t0 > timeout:
            raise Abort(f"window {name!r} still open after {timeout} s")
        time.sleep(0.3)
    return round(time.monotonic() - t0, 1)


def title(w):
    return X(["xdotool", "getwindowname", w], check=False).stdout.strip()


def prepare(r):
    r.launch(LAUNCH)
    w = r.wait_window(MAIN, timeout=120)
    r.mark("settle"); settle(r, timeout=90)
    r.maximize(w); time.sleep(2)
    if "WIDTH=3840" not in X(["xdotool", "getwindowgeometry", "--shell", w]).stdout:
        raise Abort("could not maximize the main window")
    settle(r, timeout=30); r.shot("loaded")
    return w


def open_image(r, w, path, expect):
    """File > Open Location (a plain path entry). The GTK file chooser is not used: with a fresh
    profile it starts in "Recently Used", where Ctrl+L has no location bar."""
    r.key(w, "slash"); p = wait_visible("Search Actions")         # action search: "Open Location..."
    r.key(p, "ctrl+a"); r.type(p, "open location", delay_ms=30); time.sleep(1.5); r.key(p, "Return")
    d = wait_visible("Open Location"); time.sleep(0.5)
    r.type(d, str(path), delay_ms=15); time.sleep(0.5); r.key(d, "Return")
    wait_title(w, expect, 120)
    settle(r, timeout=90); cpu_idle(r)


def wait_title(w, expect, timeout):
    end = time.monotonic() + timeout
    while expect not in title(w):
        if time.monotonic() > end:
            raise Abort(f"title never showed {expect!r} (title: {title(w)[:80]})")
        time.sleep(0.5)


def run_filter(r, w, search, dialog):
    cpu_idle(r)
    r.key(w, "slash"); p = wait_visible("Search Actions")
    r.key(p, "ctrl+a")                                     # the box keeps the previous query
    r.type(p, search, delay_ms=30); time.sleep(1.5); r.key(p, "Return")
    d = wait_visible(dialog, timeout=120); time.sleep(2); cpu_idle(r)
    g = dict(l.split("=") for l in X(["xdotool", "getwindowgeometry", "--shell", d]).stdout.split())
    click(d, int(g["WIDTH"]) - 49, int(g["HEIGHT"]) - 21)     # OK (bottom-right button)
    secs = wait_gone(dialog, 600); cpu_idle(r, timeout=600); settle(r, timeout=120)
    return secs


def start_export(r, w, out):
    r.key(w, "shift+ctrl+e"); d = wait_visible("Export Image")
    r.key(d, "ctrl+a"); r.type(d, str(out), delay_ms=15); time.sleep(0.5); r.key(d, "Return")
    p = wait_visible("Export Image as PNG", timeout=30); time.sleep(1); r.key(p, "Return")
    time.sleep(1)
    if visible("Export Image as PNG"):
        raise Abort("PNG options dialog did not close")


def wait_export(r, out, timeout=900):
    t0 = time.monotonic()
    while True:
        busy = any(Path(f"/proc/{pid}/comm").read_text().strip() == "file-png" for pid in r.pids()
                   if Path(f"/proc/{pid}/comm").exists())
        if not busy and out.exists():
            return round(time.monotonic() - t0, 1)
        if time.monotonic() - t0 > timeout:
            raise Abort("PNG export did not finish")
        time.sleep(0.5)


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    w = prepare(r)
    r.mark("new_canvas"); r.key(w, "ctrl+n"); n = wait_visible("Create a New Image"); time.sleep(1)
    r.key(n, "Return"); wait_gone("Create a New Image", 30); wait_title(w, "1920x1080", 30)
    settle(r, timeout=60)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_edit(r, d):
    w = prepare(r)
    r.mark("edit"); open_image(r, w, d / "photo.jpg", "[photo]")
    r.key(w, "shift+c"); time.sleep(0.5)                       # crop tool: drag a rectangle, Enter
    X(["xdotool", "mousemove", "1100", "500", "mousedown", "1"])
    for i in range(1, 31):
        X(["xdotool", "mousemove", str(1100 + i * 45), str(500 + i * 30)]); time.sleep(0.02)
    X(["xdotool", "mouseup", "1"]); time.sleep(1); r.key(w, "Return"); wait_title(w, "2700x1800", 30)
    r.key(w, "p"); time.sleep(0.5)                               # paintbrush: 4 zig-zag strokes
    for y in (800, 1000, 1200, 1400):
        X(["xdotool", "mousemove", "1200", str(y), "mousedown", "1"])
        for x in range(1250, 2601, 50):
            X(["xdotool", "mousemove", str(x), str(y + (x % 200) - 100)])
        X(["xdotool", "mouseup", "1"]); time.sleep(0.3)
    settle(r, timeout=30); r.shot("edited")
    r.mark("idle"); time.sleep(30)


def s03_open_big(r, d):
    w = prepare(r)
    r.mark("open_big"); open_image(r, w, d / "big.jpg", "8000x6000")
    r.key(w, "1"); time.sleep(3); r.key(w, "shift+ctrl+j"); time.sleep(3); settle(r, timeout=30); r.shot("big")
    r.mark("idle"); time.sleep(30)


def s04_layers(r, d):
    w = prepare(r)
    r.mark("open")
    open_image(r, w, d / "layers10.xcf", "layers10.xcf")
    r.mark("layers"); r.key(w, "m"); time.sleep(0.3)
    for _ in range(5):
        r.key(w, "Page_Down"); time.sleep(0.3); r.key(w, "shift+ctrl+d"); time.sleep(1)
        r.key(w, *(["shift+Right"] * 8 + ["shift+Down"] * 5)); time.sleep(1)
    wait_title(w, "15 layers", 30)
    settle(r, timeout=30); r.shot("layers")
    r.mark("idle"); time.sleep(30)


def s05_filters(r, d):
    w = prepare(r)
    r.mark("open")
    open_image(r, w, d / "big.jpg", "8000x6000")
    r.mark("filters")
    a = run_filter(r, w, "gaussian blur", "Gaussian Blur")
    b = run_filter(r, w, "sharpen (unsharp", "Sharpen (Unsharp Mask)")
    r.shot("filtered")
    r.mark("idle", gaussian_s=a, unsharp_s=b); time.sleep(30)


def s06_export(r, d):
    w = prepare(r)
    r.mark("open")
    open_image(r, w, d / "layers10.xcf", "layers10.xcf")
    out = d / "export.png"
    r.mark("export"); start_export(r, w, out); secs = wait_export(r, out); settle(r, timeout=30); r.shot("exported")
    r.mark("idle", export_s=secs, size=out.stat().st_size); time.sleep(30)


def s07_s10_background(r, d):
    w = prepare(r)
    r.mark("open")
    open_image(r, w, d / "layers10.xcf", "layers10.xcf")
    r.mark("idle_fg"); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    for k in ("1", "2", "minus", "minus", "shift+ctrl+j"):        # zoom the canvas
        r.key(w, k); time.sleep(2)
    r.shot("restored")
    r.mark("idle"); time.sleep(30)


def s09_export_bg(r, d):
    w = prepare(r)
    r.mark("open")
    open_image(r, w, d / "layers10.xcf", "layers10.xcf")
    out = d / "export.png"
    r.mark("export_start"); start_export(r, w, out)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    secs = wait_export(r, out); time.sleep(5)
    r.mark("restore", export_s=secs, size=out.stat().st_size)
    X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(3); r.shot("restored")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_edit, s03_open_big, s04_layers, s05_filters, s06_export, s07_s10_background, s09_export_bg]

if __name__ == "__main__":
    run_trials("gimp", RUNS, M, evict_list(), "[/]usr/bin/gimp-2.10", before_each=restore_profile)
