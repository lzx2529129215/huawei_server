#!/usr/bin/env python3
"""DingTalk 8.2.8 (Linux) memory, on the user's PERSONAL account, READ-ONLY.

DingTalk 8.2.8 does not start on the Xvfb screen (its embedded browser never finishes
initialising with software GL; even with a window manager), so it runs on the WSLg desktop
(DISPLAY=:0, GPU-accelerated). The user agreed to leave the computer alone meanwhile.
"Auto login" is on, so every operation gets its own cold start.

Only reading: chat list, opening chats, scrolling history, the Docs page. Nothing is sent
(on the WSLg desktop popups cannot be screenshotted reliably, so the own-chat path could not
be verified). Mouse clicks and scrolling only; no keystrokes. Screenshots are low-resolution,
of the main window only, and are deleted after verification.

Usage: APPMEM_STOP_TIMEOUT=5 python3 dingtalk_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/dingtalk"  # empty: nothing is sent
EVICT = ["/opt/apps/com.alibabainc.dingtalk", "~/.config/DingTalk", "~/.config/com.alibabainc.dingtalk", "/usr/share/fonts"]
LAUNCH = ["/opt/apps/com.alibabainc.dingtalk/files/Elevator.sh"]
SIDEBAR = {"messages": (32, 100), "docs": (32, 228)}
TABS = {"all": (100, 60), "groups": (338, 60)}
ROW_X, ROW0, ROW_H = 250, 120, 75


class Abort(RuntimeError):
    pass


def dt_windows():
    out = {}
    for w in X(["timeout", "10", "xdotool", "search", "--onlyvisible", "--class", "dingtalk"], check=False).stdout.split():
        name = X(["timeout", "3", "xdotool", "getwindowname", w], check=False).stdout.strip()
        g = X(["timeout", "3", "xdotool", "getwindowgeometry", "--shell", w], check=False).stdout
        out[w] = (name, g)
    return out


def shot_low(r, w, label):
    p = r.out / "shots" / f"{len(list((r.out / 'shots').glob('*.png'))):02d}_{label}.png"
    X(["timeout", "10", "import", "-window", str(w), "-resize", "15%", str(p)], check=False)


def prepare(r):
    r.launch(LAUNCH)
    # Auto-login shows the 480x638 login window for about 1 s before the main window "钉钉".
    # Only a login window that stays up 30 s means the session has expired.
    main, login_since = None, None
    for _ in range(90):
        wins = dt_windows()
        main = next((w for w, (name, _g) in wins.items() if name == "钉钉"), None)
        if main:
            break
        if any("WIDTH=480" in g and "HEIGHT=638" in g for _n, g in wins.values()):
            login_since = login_since or time.time()
            if time.time() - login_since > 30:
                raise Abort("DingTalk shows the login window; the user has to log in again")
        time.sleep(1)
    if not main:
        raise Abort("DingTalk main window did not appear")
    r.mark("settle"); settle(r, timeout=90)
    X(["timeout", "5", "xdotool", "windowmove", main, "0", "0"]); X(["timeout", "5", "xdotool", "windowsize", main, "3840", "2160"])
    time.sleep(3); settle(r, timeout=30); shot_low(r, main, "loaded")
    return main


def s01_startup(r, d):
    w = prepare(r)
    r.mark("steady"); time.sleep(30); shot_low(r, w, "steady")


def s02_switch(r, d):
    w = prepare(r)
    r.mark("switch_chats")            # read only: first 8 chats of the "all" tab
    click(w, *TABS["all"]); time.sleep(2)
    for k in range(8):
        click(w, ROW_X, ROW0 + k * ROW_H); time.sleep(3)
    shot_low(r, w, "switched")
    r.mark("idle"); time.sleep(30)


def s03_history(r, d):
    w = prepare(r)
    r.mark("history")                 # read only: first group chat, scroll up
    click(w, *TABS["groups"]); time.sleep(2); click(w, ROW_X, ROW0); time.sleep(3)
    X(["xdotool", "mousemove", "--window", str(w), "2000", "1000"])
    for _ in range(10):
        X(["xdotool", "click", "--repeat", "10", "--delay", "150", "4"]); time.sleep(1.5)
    shot_low(r, w, "history")
    click(w, *TABS["all"]); time.sleep(1)
    r.mark("idle"); time.sleep(30)


def s04_docs(r, d):
    w = prepare(r)
    r.mark("docs")                    # read only: Docs page
    click(w, *SIDEBAR["docs"]); time.sleep(5); settle(r, timeout=60)
    X(["xdotool", "mousemove", "--window", str(w), "2200", "1200"])
    for button in ("5", "5", "4", "4"):
        X(["xdotool", "click", "--repeat", "10", "--delay", "150", button]); time.sleep(1)
    shot_low(r, w, "docs")
    click(w, *SIDEBAR["messages"]); time.sleep(1)
    r.mark("idle"); time.sleep(30)


def s07_s10_background(r, d):
    w = prepare(r)
    r.mark("idle_fg"); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(2)
    click(w, ROW_X, ROW0); time.sleep(3); shot_low(r, w, "restored")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_switch, s03_history, s04_docs, s07_s10_background]

if __name__ == "__main__":
    import os
    os.environ["XDG_SESSION_TYPE"] = "x11"
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    run_trials("dingtalk", RUNS, M, EVICT, "^./com.alibabainc.dingtalk", display=":0")
