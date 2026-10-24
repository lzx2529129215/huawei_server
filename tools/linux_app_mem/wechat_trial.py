#!/usr/bin/env python3
"""WeChat 4.1.13 (Linux) memory, measured on the user's PERSONAL account.

Rules agreed with the user (2026-10-21):
  * Reading friends/groups is allowed (open chats, scroll history, Moments, Channels);
    writing is NOT: no messages, likes, comments, follows, forwards to anyone.
  * The only output allowed goes to File Transfer (send an image and a file to self).
    Before every click that could send, the chat header is compared pixel-for-pixel with a
    saved "File Transfer" header; on any mismatch the run stops.
  * Keystrokes are never sent while a friend or group chat is open (mouse only there).
  * Screenshots are low-resolution, only for verification, and are deleted afterwards.
Every cold start needs a confirmation on the user's phone, so each repeat is ONE session in
which the operations run one after another (phases), instead of one cold start per operation.

Usage: python3 wechat_trial.py OUT_DIR [--rep=r1]
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, run_trials, settle  # noqa: E402
import appmem  # noqa: E402

M = Path.home() / "linux_app_mem/materials/wechat"
FT_HEADER = Path.home() / "linux_app_mem/profiles/wechat_ft_header.png"
EVICT = ["/opt/wechat", "~/.xwechat", "/usr/share/fonts"]
NOTIFY = Path(__file__).resolve().parent / "notify_windows.py"
FT_GREEN = "#0101C2C26060"            # File Transfer icon background (16-bit per channel)
ROW0, ROW_H, ROW_X = 115, 65, 170     # chat list rows (maximized window)
SIDEBAR = {"moments": (31, 259), "channels": (31, 307)}
FOLDER, SEND = (402, 2128), (3787, 2127)
BUBBLE_X = 3660                       # sent messages sit at the right edge of the chat area


class Abort(RuntimeError):
    pass


def shot_low(r, label, window="root"):
    """Low-resolution screenshot only (no readable chat text)."""
    p = r.out / "shots" / f"{len(list((r.out / 'shots').glob('*.png'))):02d}_{label}.png"
    X(["import", "-window", str(window), "-resize", "15%", str(p)], check=False)


def pixel(x, y):
    out = X(["sh", "-c", f"import -window root -crop 1x1+{x}+{y} txt:- | tail -1"]).stdout
    return out.split()[2] if len(out.split()) > 2 else ""


def wechat_windows(r):
    """All visible windows on the private Xvfb screen (only WeChat runs there). Searching by
    PID misses the Chromium-based Channels window, so list every window instead."""
    ids = set()
    for args in (["--name", "."], ["--class", "."]):
        ids.update(X(["xdotool", "search", "--onlyvisible", *args], check=False).stdout.split())
    out = {}
    for wid in ids:
        name = X(["xdotool", "getwindowname", wid], check=False).stdout.strip()
        geo = X(["xdotool", "getwindowgeometry", "--shell", wid], check=False).stdout
        if "WIDTH=1\n" in geo or "WIDTH=0" in geo:
            continue
        out[wid] = (name, geo)
    return out


def ensure_file_transfer(r, w):
    """Open File Transfer from the chat list by its icon colour, then verify the header."""
    for k in range(10):
        if pixel(78, ROW0 - 12 + k * ROW_H) == FT_GREEN:
            click(w, ROW_X, ROW0 + k * ROW_H); time.sleep(2)
            break
    else:
        raise Abort("File Transfer not found in the first 10 chats")
    assert_file_transfer(r)


def assert_file_transfer(r):
    cur = r.out / "hdr.png"
    X(["import", "-window", "root", "-crop", "200x30+295+40", "+repage", str(cur)])
    diff = subprocess.run(["compare", "-metric", "AE", str(FT_HEADER), str(cur), "null:"],
                          capture_output=True, text=True).stderr.strip()
    cur.unlink(missing_ok=True)
    if diff != "0":
        raise Abort(f"open chat is not File Transfer (header differs by {diff} px); nothing sent")


def close_extra_windows(r, main):
    """Close every window except the main one (Moments, Channels, viewer...) so that nothing
    keeps running into the next phase; report what had to be closed."""
    xclose = str(Path(__file__).resolve().parent / "xclose.py")
    for wid, (name, _) in wechat_windows(r).items():
        if wid != str(main) and wid not in getattr(r, "base_windows", set()):
            X([sys.executable, "-I", xclose, wid], check=False)
            r.events.write('{"closed_extra_window": "%s"}\n' % name)
    time.sleep(2)


def wait_new_window(r, before, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        new = []
        for w, (n, g) in wechat_windows(r).items():
            kv = dict(line.split("=") for line in g.split() if "=" in line)
            if w not in before and int(kv.get("WIDTH", 0)) >= 400 and int(kv.get("HEIGHT", 0)) >= 400:
                new.append(w)
        if new:
            return new[0]
        time.sleep(1)
    return None


def newest_image_y():
    """Scan the right edge of the message area bottom-up; return the centre of the lowest
    tall non-background block (the image just sent), or None."""
    out = X(["sh", "-c", f"import -window root -crop 1x1900+{BUBBLE_X}+60 txt:- | tail -n +2"]).stdout.splitlines()
    rows = []
    for line in out:
        parts = line.split()
        y = int(parts[0].split(",")[1].rstrip(":"))
        rgb = [int(v) for v in parts[1].strip("()").split(",")[:3]]
        rows.append((y + 60, max(rgb) - min(rgb) > 25 or max(rgb) < 200))  # coloured or dark = image
    runs, start = [], None
    for y, on in rows + [(rows[-1][0] + 1, False)]:
        if on and start is None:
            start = y
        if not on and start is not None:
            runs.append((start, y)); start = None
    tall = [(a, b) for a, b in runs if b - a >= 120]
    return (tall[-1][0] + tall[-1][1]) // 2 if tall else None


def send_file(r, w, path):
    assert_file_transfer(r)
    click(w, *FOLDER)
    dlg = r.wait_window("Select File", timeout=20); time.sleep(1)
    r.type(dlg, str(path), delay_ms=20); r.key(dlg, "Return"); time.sleep(3)
    assert_file_transfer(r)          # still File Transfer before pressing Send
    click(w, *SEND); time.sleep(2)


def session(r, d):
    r.launch(["/usr/bin/wechat"])
    login = None
    for _ in range(90):
        for wid, (name, g) in wechat_windows(r).items():
            if name == "Weixin" and "WIDTH=280" in g:
                login = wid
        if login:
            break
        time.sleep(1)
    if not login:
        raise Abort("login window did not appear")
    time.sleep(2); r.mark("login"); click(login, 140, 295)
    subprocess.run([sys.executable, "-I", str(NOTIFY), "微信测量：请在手机上确认登录", f"{r.run_id}，5 分钟内有效"])
    main = None
    for _ in range(300):
        for wid, (name, g) in wechat_windows(r).items():
            if name == "Weixin" and "WIDTH=280" not in g:
                main = wid
        if main:
            break
        time.sleep(1)
    if not main:
        raise Abort("not logged in within 5 minutes")
    r.mark("startup"); time.sleep(3)
    for wid, (name, g) in wechat_windows(r).items():   # "what's new" window after an update
        if wid != main and "WIDTH=560" in g:
            click(wid, 536, 17); time.sleep(1)
    r.maximize(main); settle(r, timeout=60); shot_low(r, "chat_list")
    r.base_windows = set(wechat_windows(r))  # helper windows present at start are never closed
    r.events.write('{"base_windows": %d}\n' % len(r.base_windows))
    r.mark("steady_list"); time.sleep(30)

    r.mark("switch_chats")            # read only: mouse clicks on the first 8 chats
    for k in range(8):
        click(main, ROW_X, ROW0 + k * ROW_H); time.sleep(3)
    shot_low(r, "after_switch")
    r.mark("history")                 # read only: scroll up the last opened chat
    X(["xdotool", "mousemove", "--window", str(main), "2000", "900"])
    for _ in range(10):
        X(["xdotool", "click", "--repeat", "10", "--delay", "150", "4"]); time.sleep(1.5)
    shot_low(r, "history")

    close_extra_windows(r, main)
    r.mark("moments")                 # read only: scroll Moments, never click inside
    before = set(wechat_windows(r))
    click(main, *SIDEBAR["moments"])
    mw = wait_new_window(r, before, timeout=30)
    if not mw:
        raise Abort("Moments window did not open within 30 s")
    time.sleep(3)
    if mw:
        X(["xdotool", "windowmove", mw, "1300", "80"]); X(["xdotool", "windowsize", mw, "1200", "2000"]); time.sleep(2)
        X(["xdotool", "mousemove", "--window", mw, "600", "1000"])
        for _ in range(12):
            X(["xdotool", "click", "--repeat", "5", "--delay", "200", "5"]); time.sleep(1.5)
        shot_low(r, "moments", mw)

    close_extra_windows(r, main)
    r.mark("channels")                # read only: watch, scroll to next video
    before = set(wechat_windows(r))
    click(main, *SIDEBAR["channels"])
    cw = wait_new_window(r, before, timeout=30)
    if not cw:
        raise Abort("Channels window did not open within 30 s")
    time.sleep(5)
    X(["xdotool", "mousemove", "--window", cw, "460", "446"]); time.sleep(5)
    for _ in range(5):
        X(["xdotool", "click", "5"]); time.sleep(6)
    shot_low(r, "channels", cw)
    close_extra_windows(r, main)

    r.mark("view_image")              # File Transfer only
    ensure_file_transfer(r, main)
    send_file(r, main, d / "hd_photo.jpg"); time.sleep(4)
    y = newest_image_y()
    if y is None:
        raise Abort("sent image not found in File Transfer")
    X(["xdotool", "mousemove", "--window", str(main), str(BUBBLE_X), str(y), "click", "--repeat", "2", "--delay", "120", "1"])
    time.sleep(4)
    vw = next((w for w, (n, _) in wechat_windows(r).items() if n == "Photos and Videos"), None)
    if vw:
        X(["xdotool", "mousemove", "--window", vw, "1342", "1026"])
        X(["xdotool", "keydown", "ctrl", "click", "--repeat", "6", "--delay", "300", "4", "keyup", "ctrl"]); time.sleep(2)
        shot_low(r, "viewer", vw); r.key(vw, "Escape"); time.sleep(2)
    close_extra_windows(r, main)

    r.mark("background_transfer")     # File Transfer only: send 50 MB, then hide the window
    send_file(r, main, d / "big_file.bin")
    X(["xdotool", "windowunmap", "--sync", str(main)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(main)], check=False); time.sleep(2)
    click(main, ROW_X, ROW0 + ROW_H); time.sleep(3); shot_low(r, "restored")
    r.mark("idle"); time.sleep(30)


if __name__ == "__main__":
    import os
    os.environ["QT_QPA_PLATFORM"] = "xcb"      # otherwise WeChat picks the WSLg Wayland desktop
    os.environ["XDG_SESSION_TYPE"] = "x11"
    run_trials("wechat", [session], M, EVICT, "^/usr/bin/wechat")
