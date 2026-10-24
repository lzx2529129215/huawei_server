#!/usr/bin/env python3
"""Google Chrome / Microsoft Edge as same-category substitutes for 华为浏览器 / 海泰浏览器.

One cold start per operation, on the Xvfb screen (window 3840x2160). Each browser uses its own
throwaway profile, reset before every run to a baseline made after a clean first start
(--no-first-run, nothing signed in, no extensions). The browser is driven through the DevTools
protocol (cdp.py: real input events and page JS, no extensions); the window is hidden with
xdotool like the other apps. Pages are public and need no login.

Operations follow the Excel list for 华为浏览器 / 海泰浏览器 (A08 / A07 S01..S10):
  s01_startup       start with https://info.cern.ch/hypertext/WWW/TheProject.html
  s02_browse        3 Wikipedia articles in turn, each scrolled to the end with the mouse wheel
  s03_tabs          open 10 tabs (Wikipedia x3, BBC, GitHub, Stack Overflow, Hacker News,
                    YouTube, Amazon, CNN), then switch through them twice
  s04_maps          Google Maps: zoom with the wheel and drag the map around for ~60 s
  s05_video         YouTube 4K video: play, 1080p -> 2160p, jump to 30/60/80 %, fullscreen
  s06_tab_restore   open the same 10 tabs, discard 9 of them (browser://discards), wake them one by one
  s07_s10_background  Maps tab + paused YouTube tab, window hidden 2 min; restore and use the map
  s08_download      download a 1 GiB file (proof.ovh.net) with the window hidden (until done, max 3 min)
  s09_bg_audio      YouTube playing with the window hidden for 2 min

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 browser_trial.py chrome|edge OUT_DIR [--rep=r1] [run ...]
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdp  # noqa: E402
from trial_common import X, run_trials, settle  # noqa: E402

BROWSERS = {
    "chrome": {"bin": "/opt/google/chrome/chrome", "install": "/opt/google/chrome", "port": 9301,
               "running": "[/]opt/google/chrome/chrome --user-data-dir", "title": "Google Chrome", "scheme": "chrome"},
    "edge": {"bin": "/opt/microsoft/msedge/msedge", "install": "/opt/microsoft/msedge", "port": 9302,
             "running": "[/]opt/microsoft/msedge/msedge --user-data-dir", "title": "Edge", "scheme": "edge"},
}
NAME = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in BROWSERS else "chrome"
B = BROWSERS[NAME]
M = Path.home() / "linux_app_mem/materials/browser"            # empty
PROFILE = Path.home() / f"linux_app_mem/browser/{NAME}"
BASE = Path.home() / f"linux_app_mem/profiles/{NAME}_baseline"
FLAGS = [f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", "--password-store=basic",
         f"--remote-debugging-port={B['port']}", "--window-position=0,0", "--window-size=3840,2160"]

# Chrome and Edge move themselves into their own systemd scope (app-*.scope) over the session
# D-Bus, which would take them out of the measured cgroup; without a session bus they stay put.
NO_BUS = ["env", "DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent"]

WIKI = ["https://en.wikipedia.org/wiki/Great_Wall_of_China", "https://en.wikipedia.org/wiki/Mount_Everest",
        "https://en.wikipedia.org/wiki/Huawei"]
TEN = WIKI + ["https://www.bbc.com/news", "https://github.com/torvalds/linux", "https://stackoverflow.com/questions",
              "https://news.ycombinator.com/", "https://www.youtube.com/", "https://www.amazon.com/",
              "https://www.cnn.com/"]
MAPS = "https://www.google.com/maps/@37.7749,-122.4194,13z"
VIDEO = "https://www.youtube.com/watch?v=LXb3EKWsInQ"     # "Costa Rica in 4K", has 2160p
BIGFILE = "https://proof.ovh.net/files/1Gb.dat"            # 1 GiB test file


class Abort(RuntimeError):
    pass


def evict_list():
    return [B["install"], str(PROFILE), "/usr/share/fonts"]


def restore_profile():
    M.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", "--delete", f"{BASE}/", f"{PROFILE}/"], check=True)


# ------------------------------------------------------------ helpers ---
def browser():
    return cdp.Browser(B["port"], timeout=60)


def page_of(b, target_id=None):
    pages = b.pages()
    t = next((p for p in pages if p["id"] == target_id), None) if target_id else pages[0]
    return t["id"], cdp.Session(t["webSocketDebuggerUrl"])


def wait_load(p, timeout=60, extra=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if p.js("document.readyState", timeout=10) == "complete":
                time.sleep(extra)
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def navigate(p, url, timeout=60):
    p.call("Page.navigate", url=url)
    time.sleep(1)
    return wait_load(p, timeout)


def wheel(p, dy, x=1900, y=1000):
    p.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=0, deltaY=dy)


def drag(p, x, y, dx, dy, steps=20):
    p.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    p.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
    for i in range(1, steps + 1):
        p.call("Input.dispatchMouseEvent", type="mouseMoved", x=x + dx * i / steps, y=y + dy * i / steps,
               button="left", buttons=1)
        time.sleep(0.02)
    p.call("Input.dispatchMouseEvent", type="mouseReleased", x=x + dx, y=y + dy, button="left", buttons=1, clickCount=1)


KEYS = {"k": ("KeyK", 75), "f": ("KeyF", 70), "3": ("Digit3", 51), "6": ("Digit6", 54), "8": ("Digit8", 56)}


def key(p, k):
    code, vk = KEYS[k]
    p.call("Input.dispatchKeyEvent", type="keyDown", key=k, code=code, windowsVirtualKeyCode=vk, text=k)
    p.call("Input.dispatchKeyEvent", type="keyUp", key=k, code=code, windowsVirtualKeyCode=vk)


def main_window(r):
    end = time.monotonic() + 60
    while time.monotonic() < end:
        for w, n in r.windows():
            if B["title"] in n:
                return w
        time.sleep(0.5)
    raise Abort("browser window not found")


def prepare(r):
    r.launch(NO_BUS + [B["bin"]] + FLAGS + ["https://info.cern.ch/hypertext/WWW/TheProject.html"])
    w = main_window(r)
    b = browser()
    tid, p = page_of(b)
    r.mark("settle"); wait_load(p, 60); settle(r, timeout=90)
    if "WIDTH=3840" not in X(["xdotool", "getwindowgeometry", "--shell", w]).stdout:
        r.maximize(w); time.sleep(2)
    r.shot("loaded")
    return w, b, tid, p


def hide(w):
    X(["xdotool", "windowunmap", "--sync", str(w)], check=False)


def show(w):
    X(["xdotool", "windowmap", "--sync", str(w)], check=False)


def maps_session(p, seconds):
    """Zoom with the wheel and drag the map around."""
    t0 = time.monotonic(); k = 0
    spots = [(1300, 800), (2500, 1300), (1900, 1000), (1000, 1500), (2900, 700)]
    while time.monotonic() - t0 < seconds:
        x, y = spots[k % len(spots)]
        for _ in range(3):
            wheel(p, -300, x, y); time.sleep(0.4)          # zoom in
        drag(p, x, y, 500, 250); time.sleep(0.8)
        drag(p, x + 500, y + 250, -700, -350); time.sleep(0.8)
        for _ in range(3):
            wheel(p, 300, x, y); time.sleep(0.4)           # zoom out
        k += 1


def youtube_ready(p, timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = p.js("(() => {const v=document.querySelector('video'); return v ? [v.paused, v.currentTime, v.readyState] : null})()")
        if st and st[2] >= 2:
            return st
        time.sleep(1)
    raise Abort("YouTube video element not ready")


def youtube_play(p):
    st = youtube_ready(p)
    if st[0]:
        p.js("document.querySelector('#movie_player') && document.querySelector('#movie_player').focus()")
        key(p, "k"); time.sleep(2)
    return p.js("!document.querySelector('video').paused")


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_browse(r, d):
    w, b, tid, p = prepare(r)
    r.mark("browse")
    for url in WIKI:
        navigate(p, url)
        for _ in range(15):
            wheel(p, 900); time.sleep(1.2)
    r.shot("browsed")
    r.mark("idle"); time.sleep(30)


def s03_tabs(r, d, discard=False):
    w, b, tid, p = prepare(r)
    r.mark("tabs")
    ids = []
    for url in TEN:
        t = b.new_tab(url); ids.append(t); time.sleep(1)
        _, s = page_of(b, t); wait_load(s, 45, extra=1); s.close()
    for _ in range(2):
        for t in ids:
            b.activate(t); time.sleep(2)
    r.shot("tabs")
    if not discard:
        r.mark("idle", tabs=len(ids)); time.sleep(30)
    return w, b, ids


def s04_maps(r, d):
    w, b, tid, p = prepare(r)
    r.mark("maps"); navigate(p, MAPS); time.sleep(5)
    maps_session(p, 60); r.shot("maps")
    r.mark("idle"); time.sleep(30)


def s05_video(r, d):
    w, b, tid, p = prepare(r)
    r.mark("video"); navigate(p, VIDEO); time.sleep(3)
    playing = youtube_play(p); time.sleep(15)
    q = []
    for level in ("hd1080", "hd2160"):
        p.js(f"(() => {{const m=document.querySelector('#movie_player'); m && m.setPlaybackQualityRange && m.setPlaybackQualityRange('{level}','{level}')}})()")
        time.sleep(15)
        q.append(p.js("(() => {const m=document.querySelector('#movie_player'); return m && m.getPlaybackQuality ? m.getPlaybackQuality() : null})()"))
    p.js("document.querySelector('#movie_player').focus()")
    for k in ("3", "6", "8"):
        key(p, k); time.sleep(5)
    key(p, "f"); time.sleep(10); r.shot("fullscreen"); key(p, "f"); time.sleep(5)
    key(p, "k")
    r.mark("idle", playing=playing, quality=q); time.sleep(30)


def s06_tab_restore(r, d):
    w, b, ids = s03_tabs(r, d, discard=True)
    r.mark("discard")
    b.activate(ids[0]); time.sleep(2)
    tid, dp = page_of(b, b.new_tab(f"{B['scheme']}://discards")); time.sleep(4)
    # the discards page lists tabs with "Urgent Discard" actions; click every one that is not the visible tab
    n = dp.js("""(() => {
        const root = document.querySelector('discards-main') || document.querySelector('discards-tab-ui');
        const deep = (node, out) => { if (!node) return out; if (node.shadowRoot) deep(node.shadowRoot, out);
            for (const c of node.children || []) deep(c, out); if (node.tagName === 'DIV' || node.tagName === 'SPAN' || node.tagName === 'A') {
            if ((node.textContent || '').trim() === '[Urgent Discard]') out.push(node);} return out; };
        const links = deep(document.body, []);
        links.forEach(l => l.click());
        return links.length; })()""")
    time.sleep(5)
    b.session.call("Target.closeTarget", targetId=tid); time.sleep(3)
    r.mark("restore_tabs", discarded=n)
    # discarded tabs have no DevTools target; switch through them like a user (Ctrl+Tab), which reloads them
    for _ in range(len(ids) + 1):
        r.key(w, "ctrl+Tab"); time.sleep(5)
    settle(r, timeout=60)
    r.shot("restored")
    r.mark("idle"); time.sleep(30)


def s07_s10_background(r, d):
    w, b, tid, p = prepare(r)
    r.mark("idle_fg"); navigate(p, MAPS); time.sleep(5)
    vt = b.new_tab(VIDEO); _, v = page_of(b, vt); wait_load(v, 45); youtube_play(v); time.sleep(10)
    v.js("document.querySelector('video').pause()"); b.activate(tid); time.sleep(10)
    r.mark("background"); hide(w); time.sleep(120)
    r.mark("restore"); show(w); time.sleep(2); maps_session(p, 30); r.shot("restored")
    r.mark("idle"); time.sleep(30)


def s08_download(r, d):
    w, b, tid, p = prepare(r)
    b.session.call("Browser.setDownloadBehavior", behavior="allow", downloadPath=str(d), eventsEnabled=True)
    r.mark("download_start"); p.call("Page.navigate", url=BIGFILE); time.sleep(3)
    r.mark("background"); hide(w)
    t0 = time.monotonic(); done = False
    while time.monotonic() - t0 < 180:
        files = list(d.glob("1Gb.dat*"))
        if any(f.name == "1Gb.dat" and f.stat().st_size == 1 << 30 for f in files):
            done = True; break
        time.sleep(2)
    secs = round(time.monotonic() - t0, 1)
    time.sleep(max(0, 120 - secs))
    r.mark("restore", download_done=done, download_s=secs); show(w); time.sleep(3); r.shot("restored")
    r.mark("idle"); time.sleep(30)


def s09_bg_audio(r, d):
    w, b, tid, p = prepare(r)
    r.mark("idle_fg"); navigate(p, VIDEO); time.sleep(3); playing = youtube_play(p); time.sleep(10)
    r.mark("background", playing=playing); hide(w); time.sleep(120)
    still = p.js("!document.querySelector('video').paused")
    r.mark("restore", still_playing=still); show(w); time.sleep(5); r.shot("restored"); key(p, "k")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_browse, s03_tabs, s04_maps, s05_video, s06_tab_restore, s07_s10_background,
        s08_download, s09_bg_audio]

if __name__ == "__main__":
    argv = sys.argv[2:] if len(sys.argv) > 1 and sys.argv[1] in BROWSERS else sys.argv[1:]
    run_trials(NAME, RUNS, M, evict_list(), B["running"], argv=argv, before_each=restore_profile)
