#!/usr/bin/env python3
"""Feishu 7.72.23 (Linux) memory, on the user's PERSONAL account ("Stay signed in" is on,
so a cold start logs in by itself and every operation gets its own cold start).

Rules agreed with the user (2026-10-21): reading chats/groups/docs is allowed; writing is
NOT, except sending files to the user's own chat ("Message yourself"). Before every Send
the code compares the chat header and the "Send to <own name>" dialog title pixel-for-pixel
with saved references and stops on any mismatch. Keystrokes are only used in file dialogs;
friend/group chats and docs get mouse clicks and scrolling only. Screenshots are
low-resolution, only for verification, and are deleted afterwards.
Opening a single cloud doc does not show a window here (Feishu hands docs to a browser,
none is installed), so S03-docs browses the Docs home (recent list) instead.

Usage: python3 feishu_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/wechat"          # same test image / 50 MB file
REF = Path.home() / "linux_app_mem/profiles"
EVICT = ["/opt/bytedance/feishu", "~/.config/LarkShell", "~/.cache/LarkShell", "/usr/share/fonts"]
LAUNCH = ["/usr/bin/bytedance-feishu-stable"]
SIDEBAR = {"messenger": (93, 120), "docs": (93, 193)}
ROW_X, ROW0, ROW_H = 400, 168, 62.5     # chat list rows
AVATAR, PROFILE, MESSAGE = (27, 27), (90, 186), (528, 444)
PLUS, IMAGE_ITEM, FILE_ITEM = (3695, 2108), (3631, 1737), (3631, 1776)
SEND_IMAGE, SEND_FILE = (2060, 1235), (2060, 1135)
TITLE_Y = {"image": 898, "file": 1000}   # "Send to <name>" title position per dialog kind
SELF_MSG_X = 910                         # own messages in the self chat sit at this x


class Abort(RuntimeError):
    pass


def shot_low(r, label):
    p = r.out / "shots" / f"{len(list((r.out / 'shots').glob('*.png'))):02d}_{label}.png"
    X(["import", "-window", "root", "-resize", "15%", str(p)], check=False)


def same(ref, x, y, w, h, tmp):
    X(["import", "-window", "root", "-crop", f"{w}x{h}+{x}+{y}", "+repage", str(tmp)])
    d = subprocess.run(["compare", "-metric", "AE", str(ref), str(tmp), "null:"], capture_output=True, text=True).stderr.strip()
    tmp.unlink(missing_ok=True)
    return d == "0"


def main_window(r, timeout=90):
    w = r.wait_window("Feishu", timeout=timeout)
    for _ in range(20):
        g = X(["xdotool", "getwindowgeometry", "--shell", w]).stdout
        if "WIDTH=1316" in g:              # login (QR) window: the session expired
            raise Abort("Feishu shows the QR login window; the user has to log in again")
        if "WIDTH=38" in g:
            break
        time.sleep(1)
    return w


def prepare(r):
    r.launch(LAUNCH)
    w = main_window(r)
    r.mark("settle"); settle(r, timeout=90)
    for _ in range(5):
        r.maximize(w); time.sleep(2)
        if "WIDTH=3840" in X(["xdotool", "getwindowgeometry", "--shell", w]).stdout:
            break
    settle(r, timeout=30); shot_low(r, "loaded")
    return w


def open_self_chat(r, w):
    click(w, *SIDEBAR["messenger"]); time.sleep(2)
    click(w, *AVATAR); time.sleep(2); click(w, *PROFILE); time.sleep(3); click(w, *MESSAGE); time.sleep(3)
    if not same(REF / "feishu_self_header.png", 648, 40, 160, 26, r.out / "hdr.png"):
        raise Abort("open chat is not the user's own chat; nothing sent")


def send_to_self(r, w, path, kind):
    if not same(REF / "feishu_self_header.png", 648, 40, 160, 26, r.out / "hdr.png"):
        raise Abort("open chat is not the user's own chat; nothing sent")
    click(w, *PLUS); time.sleep(2); click(w, *(IMAGE_ITEM if kind == "image" else FILE_ITEM))
    dlg = r.wait_window("Open Files", timeout=20); time.sleep(1)
    r.key(dlg, "ctrl+l"); time.sleep(1); r.type(dlg, str(path), delay_ms=20); time.sleep(0.5); r.key(dlg, "Return")
    time.sleep(4)
    if not same(REF / "feishu_send_to_self.png", 1720, TITLE_Y[kind], 300, 40, r.out / "st.png"):
        raise Abort(f"send dialog is not 'Send to <own name>' ({kind}); nothing sent")
    click(w, *(SEND_IMAGE if kind == "image" else SEND_FILE)); time.sleep(3)


def newest_image_y():
    """Lowest tall coloured block at the own-message column of the self chat."""
    out = X(["sh", "-c", f"import -window root -crop 1x1900+{SELF_MSG_X}+100 txt:- | tail -n +2"]).stdout.splitlines()
    rows = []
    for line in out:
        parts = line.split()
        y = int(parts[0].split(",")[1].rstrip(":")) + 100
        rgb = [int(v) for v in parts[1].strip("()").split(",")[:3]]
        rows.append((y, max(rgb) - min(rgb) > 25 or max(rgb) < 200))
    runs, start = [], None
    for y, on in rows + [(rows[-1][0] + 1, False)]:
        if on and start is None:
            start = y
        if not on and start is not None:
            runs.append((start, y)); start = None
    tall = [(a, b) for a, b in runs if b - a >= 150]
    return (tall[-1][0] + tall[-1][1]) // 2 if tall else None


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); shot_low(r, "steady")


def s02_switch(r, d):
    w = prepare(r)
    r.mark("switch_chats")            # read only: mouse clicks on the first 8 chats
    for k in range(8):
        click(w, ROW_X, int(ROW0 + k * ROW_H)); time.sleep(3)
    shot_low(r, "switched")
    r.mark("idle"); time.sleep(30)


def s03_history(r, d):
    w = prepare(r)
    r.mark("history")                 # read only: open the 3rd chat and scroll up its history
    click(w, ROW_X, int(ROW0 + 2 * ROW_H)); time.sleep(3)
    X(["xdotool", "mousemove", "--window", str(w), "2000", "1000"])
    for _ in range(10):
        X(["xdotool", "click", "--repeat", "10", "--delay", "150", "4"]); time.sleep(1.5)
    shot_low(r, "history")
    r.mark("idle"); time.sleep(30)


def s04_docs(r, d):
    w = prepare(r)
    r.mark("docs")                    # read only: Docs home and its recent list
    click(w, *SIDEBAR["docs"]); time.sleep(3); settle(r, timeout=60)
    X(["xdotool", "mousemove", "--window", str(w), "2200", "1200"])
    for button in ("5", "5", "5", "4", "4", "4"):
        X(["xdotool", "click", "--repeat", "10", "--delay", "150", button]); time.sleep(1)
    shot_low(r, "docs")
    r.mark("idle"); time.sleep(30)


def s05_view_image(r, d):
    w = prepare(r)
    r.mark("view_image")              # own chat only
    open_self_chat(r, w)
    send_to_self(r, w, d / "hd_photo.jpg", "image"); time.sleep(3)
    y = newest_image_y()
    if y is None:
        raise Abort("sent image not found in the own chat")
    click(w, SELF_MSG_X, y); time.sleep(3)
    v = next((x for x, n in r.windows() if n == "Picture"), None)
    if not v:
        raise Abort("image viewer did not open")
    X(["xdotool", "mousemove", "--window", v, "1278", "972"])
    X(["xdotool", "keydown", "ctrl", "click", "--repeat", "6", "--delay", "300", "4", "keyup", "ctrl"]); time.sleep(2)
    shot_low(r, "viewer"); r.key(v, "Escape"); time.sleep(2)
    r.mark("idle"); time.sleep(30)


def s07_s10_background(r, d):
    w = prepare(r)
    r.mark("background")              # own chat only: send 50 MB, then hide the window
    open_self_chat(r, w)
    send_to_self(r, w, d / "big_file.bin", "file")
    X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(5); shot_low(r, "background"); time.sleep(115)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(2)
    click(w, *SIDEBAR["messenger"]); time.sleep(3); shot_low(r, "restored")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_switch, s03_history, s04_docs, s05_view_image, s07_s10_background]

if __name__ == "__main__":
    os.environ["XDG_SESSION_TYPE"] = "x11"
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    run_trials("feishu", RUNS, M, EVICT, "[/]opt/bytedance/feishu/")
