#!/usr/bin/env python3
"""MindMaster 10.8.0 (Linux, free/trial mode, not logged in): one cold start per operation.

Usage: python3 mindmaster_trial.py OUT_DIR [--rep=r1] [run ...]
Not measured: S06 export / S09 background export (export needs a Wondershare login),
S08 background autosave (no reliable trigger). The free version caps editing at 100 topics,
so S02 adds only 10 topics to a 67-topic map; the 502-topic map can still be viewed,
expanded and re-laid-out.
Coordinates are for the window placed at 0,0 with size 3840x2160.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, filler, open_main, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/mindmaster"
EVICT = ["/opt/MindMaster-10", "~/Edraw/MindMaster", "~/.config/cef_user_data", "/usr/share/fonts"]
LAUNCH = ["sh", "/opt/MindMaster-10/mindmaster.sh"]
NORMAL_CENTRE = (1255, 1218)   # central topic of normal.emmx (年度个人工作计划)
BIG_CENTRE = (1255, 222)       # central topic of big.emmx in its own S-shaped timeline layout
LAYOUT_BOX = (3770, 252)       # right panel: layout dropdown
LAYOUTS = [(3657, 322), (3657, 377), (3602, 435), (3602, 322)]  # four entries of the layout grid


def prepare(r, w, settle_timeout=90):
    """MindMaster restores its own window size after loading, and shows a tutorial on every
    start (it was skipped, but the app is always killed, so that is never saved). Wait for the
    load, put the window over the whole screen, then skip the tutorial and its info box."""
    r.mark("settle"); settle(r, timeout=settle_timeout)
    # The window found at launch can be a transient one; the main window ("亿图脑图MindMaster
    # （试用版）") appears later, so pick it again now.
    for _ in range(30):
        main = [x for x in r.windows() if "MindMaster（" in x[1]]
        if main:
            w = main[0][0]
            break
        time.sleep(1)
    r.events.write('{"main_window": "%s"}\n' % w)
    for _ in range(5):
        r.maximize(w); time.sleep(2)
        g = X(["xdotool", "getwindowgeometry", "--shell", str(w)]).stdout
        if "WIDTH=3840" in g and "HEIGHT=2160" in g:
            break
    r.shot("loaded")
    before = {x[0] for x in r.windows()}
    click(w, 262, 242); time.sleep(2)  # tutorial bubble: 跳过
    for wid, name in r.windows():
        if wid not in before and name == "MindMaster":
            g = X(["xdotool", "getwindowgeometry", "--shell", wid]).stdout
            if "WIDTH=290" in g:
                click(wid, 231, 119); time.sleep(1)  # info box: 确定
                r.events.write('{"tutorial": "skipped"}\n')
    settle(r, timeout=30)
    # A modal overlay (tutorial or recovery dialog) greys out the canvas; refuse to measure then.
    px = X(["sh", "-c", "import -window root -crop 1x1+3300+900 txt:- | tail -1"]).stdout
    grey = [int(v) for v in px.split("(")[1].split(")")[0].split(",")[:3]] if "(" in px else [0, 0, 0]
    if min(grey) < 200:
        r.shot("blocked")
        raise RuntimeError(f"canvas covered by an overlay (pixel {grey})")
    return w


def s01_blank(r, d):
    w = open_main(r, LAUNCH + [str(d / "blank.emmx")], "MindMaster")
    w = prepare(r, w)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def add_topics(r, w, centre, n, seed=0):
    """Tab adds a child topic in edit mode; Return ends editing, a second Return adds a sibling."""
    click(w, *centre); time.sleep(1)
    r.key(w, "Tab"); time.sleep(0.5); r.type(w, f"topic {seed} " + filler(24, seed=seed), delay_ms=40); r.key(w, "Return")
    for k in range(1, n):
        time.sleep(0.5); r.key(w, "Return"); time.sleep(0.5)
        r.type(w, f"topic {seed + k} " + filler(24, seed=seed + k), delay_ms=40); r.key(w, "Return")
    time.sleep(0.5); r.key(w, "Escape")


def s02_edit(r, d):
    w = open_main(r, LAUNCH + [str(d / "normal.emmx")], "MindMaster")
    w = prepare(r, w)
    r.mark("edit"); add_topics(r, w, NORMAL_CENTRE, 10); time.sleep(2); r.shot("edited")
    r.mark("idle"); time.sleep(60); r.shot("idle")


def s03_bigmap(r, d):
    w = open_main(r, LAUNCH + [str(d / "big.emmx")], "MindMaster")
    w = prepare(r, w, 120)
    r.mark("browse")
    X(["xdotool", "mousemove", "--window", str(w), "1700", "1100"])
    for _ in range(6):  # scroll down the long timeline, then back up
        X(["xdotool", "click", "--repeat", "15", "--delay", "120", "5"])
    r.shot("scrolled")
    for _ in range(6):
        X(["xdotool", "click", "--repeat", "15", "--delay", "120", "4"])
    r.mark("idle"); time.sleep(30); r.shot("idle")


def s04_expand_all(r, d):
    w = open_main(r, LAUNCH + [str(d / "big.emmx")], "MindMaster")
    w = prepare(r, w, 120)
    r.mark("expand"); click(w, *BIG_CENTRE); time.sleep(1); r.key(w, "alt+0")
    settle(r, timeout=60); r.shot("expanded")
    X(["xdotool", "mousemove", "--window", str(w), "1700", "1100"])
    for _ in range(6):
        X(["xdotool", "click", "--repeat", "15", "--delay", "120", "5"])
    r.shot("expanded_scrolled")
    r.mark("idle"); time.sleep(30); r.shot("idle")


def s05_layout(r, d):
    w = open_main(r, LAUNCH + [str(d / "big.emmx")], "MindMaster")
    w = prepare(r, w, 120)
    r.mark("layout")
    for i, pos in enumerate(LAYOUTS):
        click(w, *LAYOUT_BOX); time.sleep(1.5); click(w, *pos); time.sleep(8); r.shot(f"layout{i + 1}")
    r.mark("idle"); time.sleep(30); r.shot("idle")


def s07_s10_background(r, d):
    w = open_main(r, LAUNCH + [str(d / "normal.emmx")], "MindMaster")
    w = prepare(r, w)
    r.mark("edit"); add_topics(r, w, NORMAL_CENTRE, 3, seed=50)
    r.mark("idle_fg"); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(5); r.shot("background"); time.sleep(115)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    X(["xdotool", "mousemove", "--window", str(w), "1700", "1100"])
    X(["xdotool", "click", "--repeat", "20", "--delay", "150", "5"]); r.shot("restored")
    r.mark("idle"); time.sleep(30)


def clear_autosave():
    """A killed MindMaster leaves autosave backups of our test copies; on the next start it shows
    a modal "abnormal exit, files recovered" dialog that blocks every click. Remove them first."""
    for f in (Path.home() / "Edraw/MindMaster/autosave").glob("*_backup_*.emmx"):
        f.unlink()


RUNS = [s01_blank, s02_edit, s03_bigmap, s04_expand_all, s05_layout, s07_s10_background]

if __name__ == "__main__":
    run_trials("mindmaster", RUNS, M, EVICT, "[M]indMaster-10/MindMaster", before_each=clear_autosave)
