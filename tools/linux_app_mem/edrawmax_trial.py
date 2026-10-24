#!/usr/bin/env python3
"""EdrawMax 15.0.6 (Linux, free mode, not logged in): one cold start per operation.

Usage: python3 edrawmax_trial.py OUT_DIR [--rep=r1] [run ...]
Before every run ~/Edraw/edrawmax is restored from a baseline profile in which the tutorial
and first-use tips were already seen (made by closing EdrawMax gracefully with xclose.py), so
each run starts from the same "returning user" state with no autosave/recovery files.
Not measured: S06 export / S09 background export (user chose not to log in), S08 autosave.
The free version allows 60 shapes per document. Coordinates assume the window at 0,0 3840x2160.
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, open_main, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/edrawmax"
PROFILE = Path.home() / "linux_app_mem/profiles/edrawmax_baseline_v2"
EVICT = ["/opt/apps/edrawmax", "~/Edraw/edrawmax", "/usr/share/fonts"]
LAUNCH = ["sh", "/opt/apps/edrawmax/edrawmax.sh"]
SHAPE = (1585, 822)                 # "需求简介" box in normal.eddx
MORE_SYMBOLS = (162, 2098)          # left panel: 更多符号
LIB_OK = (767, 647)                 # 添加符号库 dialog: 确定 (dialog coordinates)
LIB_BOXES_BOTTOM_UP = [637, 606, 573, 541, 509, 477, 444, 221]  # x=100; ticking bottom-up keeps rows in place
ALIGN_MENU = (1160, 124)            # ribbon: 对齐 ▾ (the menu stays open after a choice)
LEFT, TOP, H_DIST = (1147, 223), (1147, 257), (1147, 343)


def restore_profile():
    subprocess.run(["rsync", "-a", "--delete", f"{PROFILE}/", str(Path.home() / "Edraw/edrawmax") + "/"], check=True)


def canvas_clear():
    px = X(["sh", "-c", "import -window root -crop 1x1+3300+900 txt:- | tail -1"]).stdout
    rgb = [int(v) for v in px.split("(")[1].split(")")[0].split(",")[:3]] if "(" in px else [0, 0, 0]
    return min(rgb) >= 200, rgb


def prepare(r, w, settle_timeout=90):
    """Close the login window shown at every start, place the window over the whole screen."""
    r.mark("settle"); settle(r, timeout=settle_timeout)
    for wid, name in r.windows():
        if name == "账户登录":
            click(wid, 679, 60); time.sleep(2)
            r.events.write('{"login_window": "closed"}\n')
    for _ in range(5):
        r.maximize(w); time.sleep(2)
        if "WIDTH=3840" in X(["xdotool", "getwindowgeometry", "--shell", str(w)]).stdout:
            break
    settle(r, timeout=30)
    ok, rgb = canvas_clear()
    r.shot("loaded")
    if not ok:
        raise RuntimeError(f"canvas covered by an overlay (pixel {rgb})")
    return w


def paste_copies(r, w, n):
    click(w, *SHAPE); time.sleep(0.5); r.key(w, "Escape"); click(w, *SHAPE); time.sleep(0.5); r.key(w, "ctrl+c")
    for i in range(n):  # paste at empty spots right of the page; Escape leaves text editing
        click(w, 2650 + (i % 5) * 140, 800 + (i // 5) * 200); time.sleep(0.4)
        r.key(w, "ctrl+v"); time.sleep(0.8); r.key(w, "Escape"); time.sleep(0.3)


def s01_blank(r, d):
    w = prepare(r, open_main(r, LAUNCH + [str(d / "blank.eddx")], "blank"))
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_edit(r, d):
    w = prepare(r, open_main(r, LAUNCH + [str(d / "normal.eddx")], "normal"))
    r.mark("edit"); paste_copies(r, w, 10); time.sleep(2); r.shot("edited")
    r.mark("idle"); time.sleep(60); r.shot("idle")


def s03_bigdiagram(r, d):
    w = prepare(r, open_main(r, LAUNCH + [str(d / "big.eddx")], "big"), 120)
    r.mark("browse")
    X(["xdotool", "mousemove", "--window", str(w), "1900", "1100"])
    X(["xdotool", "keydown", "ctrl", "click", "--repeat", "8", "--delay", "300", "4", "keyup", "ctrl"])  # zoom in
    time.sleep(2); r.shot("zoomed")
    for button in ("5", "5", "4", "4"):  # pan down and back up
        X(["xdotool", "click", "--repeat", "15", "--delay", "120", button])
    X(["xdotool", "keydown", "ctrl", "click", "--repeat", "8", "--delay", "300", "5", "keyup", "ctrl"])  # zoom out
    time.sleep(2); r.shot("browsed")
    r.mark("idle"); time.sleep(30); r.shot("idle")


def s04_libraries(r, d):
    w = prepare(r, open_main(r, LAUNCH + [str(d / "normal.eddx")], "normal"))
    r.mark("load_libraries")
    click(w, *MORE_SYMBOLS)
    dlg = r.wait_window("添加符号库", timeout=30); time.sleep(2)
    for y in LIB_BOXES_BOTTOM_UP:
        click(dlg, 100, y); time.sleep(0.5)
    r.shot("libraries_ticked")
    click(dlg, *LIB_OK); settle(r, timeout=60)
    X(["xdotool", "mousemove", "--window", str(w), "160", "1000"])
    for _ in range(8):  # scroll the whole symbol panel so every library is drawn
        X(["xdotool", "click", "--repeat", "15", "--delay", "100", "5"])
    r.shot("panel_scrolled")
    r.mark("idle"); time.sleep(30); r.shot("idle")


def s05_relayout(r, d):
    w = prepare(r, open_main(r, LAUNCH + [str(d / "big.eddx")], "big"), 120)
    r.mark("relayout")
    click(w, 2600, 1500); time.sleep(1); r.key(w, "ctrl+a"); time.sleep(2)
    click(w, *ALIGN_MENU); time.sleep(1.5)
    for name, pos in (("left", LEFT), ("top", TOP), ("hdist", H_DIST)):
        click(w, *pos); time.sleep(6); r.shot(f"align_{name}")
    r.key(w, "Escape"); time.sleep(1)
    for _ in range(3):
        r.key(w, "ctrl+z"); time.sleep(3)
    r.shot("undone")
    r.mark("idle"); time.sleep(30); r.shot("idle")


def s07_s10_background(r, d):
    w = prepare(r, open_main(r, LAUNCH + [str(d / "normal.eddx")], "normal"))
    r.mark("edit"); paste_copies(r, w, 3)
    r.mark("idle_fg"); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(5); r.shot("background"); time.sleep(115)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    X(["xdotool", "mousemove", "--window", str(w), "1900", "1100"])
    X(["xdotool", "click", "--repeat", "20", "--delay", "150", "5"]); r.shot("restored")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_blank, s02_edit, s03_bigdiagram, s04_libraries, s05_relayout, s07_s10_background]

if __name__ == "__main__":
    run_trials("edrawmax", RUNS, M, EVICT, "[a]pps/edrawmax/EdrawMax", before_each=restore_profile)
