#!/usr/bin/env python3
"""VLC 3.0 (Ubuntu package) as the same-category substitute for 华为视频 (Huawei Video).

One cold start per operation, on the Xvfb screen (3840x2160, window maximized, so the video is
scaled to 4K like a maximized player). Before every run the VLC config and data dirs are reset to
a baseline saved after the first start (privacy question answered, metadata network access off),
so there is no playback history. Local files only (no network): the Shotcut clips (1080p30 120 s
~16 Mbit/s, 2160p30 60 s ~50 Mbit/s) and ten different 1080p30 30 s clips for the playlist.
VLC starts without a file; each operation then adds files and plays/pauses/seeks through VLC's
own remote-control interface (oldrc on 127.0.0.1), so no keyboard focus is needed.

Operations follow the Excel list for 华为视频 (A16-S01..S10); S08 (offline download) has no local
counterpart and is not measured:
  s01_startup        start VLC without a file
  s02_play           play the 1080p clip for 60 s
  s03_quality        play the 1080p clip 20 s, then switch to the 4K (50 Mbit/s) clip for 40 s
  s04_seek           play and jump around: 30 keyboard jumps (10 s / 1 min), 0.5 s apart
  s05_fullscreen     play 10 s, fullscreen 20 s, back to the window 10 s
  s06_playlist       10-clip playlist: play, next clip every 5 s
  s07_s10_background play 20 s, pause, hide 2 min; restore and play 30 s
  s09_bg_play        play with the window hidden for 2 min (VLC keeps playing)

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 vlc_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/vlc"            # empty: media stay in place
CLIPS = Path.home() / "linux_app_mem/materials/shotcut/media"
LIST = Path.home() / "linux_app_mem/materials/vlc_media"
BASE = Path.home() / "linux_app_mem/profiles/vlc_baseline"
CONF, DATA = Path.home() / ".config/vlc", Path.home() / ".local/share/vlc"
RC_PORT = 4213
LAUNCH = ["/usr/bin/vlc", "--loop", "--no-qt-video-autoresize", "--extraintf", "rc", "--rc-host", f"127.0.0.1:{RC_PORT}"]
C1080, C4K = CLIPS / "clip_a_1080p.mp4", CLIPS / "clip_4k.mp4"


class Abort(RuntimeError):
    pass


def evict_list():
    libs = set()
    for line in subprocess.run(["ldd", "/usr/bin/vlc"], capture_output=True, text=True).stdout.splitlines():
        m = re.search(r"=> (/\S+)", line)
        if m:
            libs.add(m.group(1))
    return sorted(libs) + ["/usr/bin/vlc", "/usr/lib/x86_64-linux-gnu/vlc", "/usr/lib/x86_64-linux-gnu/libvlc.so.5",
                           "/usr/lib/x86_64-linux-gnu/libvlccore.so.9", "/usr/share/vlc", "/usr/share/fonts",
                           str(CONF), str(DATA), str(CLIPS), str(LIST)]


def restore_profile():
    M.mkdir(parents=True, exist_ok=True)
    for src, dst in ((BASE / "config", CONF), (BASE / "data", DATA)):
        dst.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-a", "--delete", f"{src}/", f"{dst}/"], check=True)


def prepare(r):
    r.launch(LAUNCH)
    w = r.wait_window("VLC media player", timeout=60)
    r.mark("settle"); settle(r, timeout=60)
    r.maximize(w); time.sleep(2)
    if "WIDTH=3840" not in X(["xdotool", "getwindowgeometry", "--shell", w]).stdout:
        raise Abort("could not maximize the main window")
    others = [n for x, n in r.windows() if x != w and n not in ("vlc", "Qt Selection Owner for vlc", "")]
    if others:
        raise Abort(f"unexpected window(s) at start: {others}")
    settle(r, timeout=30); r.shot("loaded")
    return w, RC()


class RC:
    """VLC remote control (rc = Lua CLI) over TCP: one command per line."""

    def __init__(self, timeout=30):
        end = time.monotonic() + timeout
        while True:
            try:
                self.s = socket.create_connection(("127.0.0.1", RC_PORT), timeout=5)
                break
            except OSError:
                if time.monotonic() > end:
                    raise Abort("VLC remote control did not come up")
                time.sleep(0.5)
        self.read(0.5)

    def read(self, wait=0.3):
        self.s.settimeout(wait); out = b""
        try:
            while True:
                chunk = self.s.recv(65536)
                if not chunk:
                    break
                out += chunk
        except OSError:
            pass
        return out.decode("utf-8", "replace")

    def __call__(self, cmd, wait=0.3):
        self.s.sendall((cmd + "\n").encode())
        return self.read(wait)

    def playing(self):
        out = self("is_playing", wait=0.6)                  # replies "1" or "0" (after a "> " prompt)
        return re.findall(r"\b([01])\b", out)[-1:] == ["1"]

    def pause(self):
        if self.playing():
            self("pause")

    def resume(self):
        self("play")          # explicit: "pause" toggles, and the is_playing reply can lag behind


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_play(r, d):
    w, rc = prepare(r)
    r.mark("play"); rc(f"add {C1080}"); time.sleep(60); r.shot("playing"); rc.pause()
    r.mark("idle"); time.sleep(30)


def s03_quality(r, d):
    w, rc = prepare(r)
    r.mark("quality"); rc(f"add {C1080}"); time.sleep(20)
    rc(f"add {C4K}"); time.sleep(40); r.shot("4k")       # the same scene at 2160p / ~50 Mbit/s
    if "clip_4k" not in X(["xdotool", "getwindowname", w]).stdout:
        raise Abort("did not switch to the 4K clip")
    rc.pause()
    r.mark("idle"); time.sleep(30)


def s04_seek(r, d):
    w, rc = prepare(r)
    r.mark("seek"); rc(f"add {C1080}"); time.sleep(3)
    for t in (97, 12, 64, 33, 110, 5, 81, 49, 118, 26, 72, 3, 90, 57, 15, 101, 40, 68, 8, 115,
              45, 92, 20, 77, 1, 59, 105, 36, 84, 10):
        rc(f"seek {t}"); time.sleep(0.5)                 # 30 jumps, 0.5 s apart
    time.sleep(3); r.shot("seeked"); rc.pause()
    r.mark("idle"); time.sleep(30)


def s05_fullscreen(r, d):
    w, rc = prepare(r)
    r.mark("fullscreen"); rc(f"add {C1080}"); time.sleep(10)
    rc("fullscreen on"); time.sleep(20); r.shot("fullscreen")
    rc("fullscreen off"); time.sleep(10); rc.pause()
    r.mark("idle"); time.sleep(30)


def s06_playlist(r, d):
    w, rc = prepare(r)
    clips = sorted(LIST.glob("list_*.mp4"))
    r.mark("playlist"); rc(f"add {clips[0]}")
    for c in clips[1:]:
        rc(f"enqueue {c}")
    seen = set()
    for _ in range(10):
        time.sleep(5); seen.add(X(["xdotool", "getwindowname", w]).stdout.strip()); rc("next")
    if len(seen) < 9:
        raise Abort(f"only {len(seen)} different clips played")
    time.sleep(3); r.shot("playlist"); rc.pause()
    r.mark("idle", clips=len(seen)); time.sleep(30)


def s07_s10_background(r, d):
    w, rc = prepare(r)
    r.mark("idle_fg"); rc(f"add {C1080}"); time.sleep(20); rc.pause(); time.sleep(10)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    rc.resume(); time.sleep(30); r.shot("restored"); rc.pause()
    r.mark("idle"); time.sleep(30)


def s09_bg_play(r, d):
    w, rc = prepare(r)
    r.mark("background"); rc(f"add {C1080}"); time.sleep(2)
    X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    still = rc.playing()
    r.mark("restore", still_playing=still); X(["xdotool", "windowmap", "--sync", str(w)], check=False)
    time.sleep(5); r.shot("restored"); rc.pause()
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_play, s03_quality, s04_seek, s05_fullscreen, s06_playlist, s07_s10_background, s09_bg_play]

if __name__ == "__main__":
    run_trials("vlc", RUNS, M, evict_list(), "[/]usr/bin/vlc ", before_each=restore_profile)
