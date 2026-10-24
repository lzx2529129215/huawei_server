#!/usr/bin/env python3
"""Rhythmbox 3.4 (Ubuntu package) as the same-category substitute for 华为音乐 (Huawei Music).

Local library only (make_music.py): 5,000 tagged MP3s in 250 albums; the first 10 albums carry an
embedded 3000x3000 cover. The library location (GSettings org.gnome.rhythmbox.rhythmdb) points at
it. Before every run the Rhythmbox data and cache dirs are reset to a baseline: either the
imported library (5,000 songs in rhythmdb.xml) or, for the scan operation, an empty database.
Playback is controlled over D-Bus (MPRIS, the standard media-player interface), so no keyboard
focus is needed. One cold start per operation on the Xvfb screen, window 3840x2160.

Operations follow the Excel list for 华为音乐 (A20-S01..S10); S06/S09 (downloads) need an account
and lyrics (part of S03) need the network, so they are not measured:
  s01_startup        start with the 5,000-song library
  s02_play           play one track of album 1 (cover shown) for 60 s
  s03_covers         play a track from each of the 10 albums with high-res covers, 6 s each
  s04_skip           next track 20 times, 1.5 s apart
  s05_scan           start with an empty database: Rhythmbox imports the 5,000 files
  s07_s10_background play 20 s, pause, hide 2 min; restore and play 30 s
  s08_bg_play        play with the window hidden for 2 min

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 rhythmbox_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, cpu_idle, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/rhythmbox_run"      # empty
LIB = Path.home() / "linux_app_mem/materials/music/library"
PROFILES = Path.home() / "linux_app_mem/profiles"
DATA, CACHE = Path.home() / ".local/share/rhythmbox", Path.home() / ".cache/rhythmbox"
LAUNCH = ["/usr/bin/rhythmbox"]
EMPTY_DB = {"s05_scan"}
_current = {"run": None}


class Abort(RuntimeError):
    pass


def evict_list():
    return ["/usr/bin/rhythmbox", "/usr/lib/x86_64-linux-gnu/rhythmbox", "/usr/share/rhythmbox",
            "/usr/lib/x86_64-linux-gnu/gstreamer-1.0", "/usr/share/fonts", str(DATA), str(CACHE), str(LIB)]


def restore_profile():
    M.mkdir(parents=True, exist_ok=True)
    subprocess.run(["gsettings", "set", "org.gnome.rhythmbox.rhythmdb", "locations", f"['{LIB.as_uri()}']"], check=True)
    subprocess.run(["gsettings", "set", "org.gnome.rhythmbox.rhythmdb", "monitor-library", "true"], check=True)
    base = PROFILES / ("rhythmbox_empty" if _current["run"] in EMPTY_DB else "rhythmbox_baseline")
    for src, dst in ((base / "data", DATA), (PROFILES / "rhythmbox_baseline/cache", CACHE)):
        dst.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-a", "--delete", f"{src}/", f"{dst}/"], check=True)


def mpris(method, *args):
    # Rhythmbox can be busy for many seconds right after a cold start (large embedded covers), so allow
    # up to 2 minutes for the call to return.
    cmd = ["gdbus", "call", "--session", "--timeout", "120", "--dest", "org.mpris.MediaPlayer2.rhythmbox",
           "--object-path", "/org/mpris/MediaPlayer2", "--method", f"org.mpris.MediaPlayer2.Player.{method}", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=130).stdout.strip()


def status():
    out = subprocess.run(["gdbus", "call", "--session", "--dest", "org.mpris.MediaPlayer2.rhythmbox", "--object-path",
                          "/org/mpris/MediaPlayer2", "--method", "org.freedesktop.DBus.Properties.Get",
                          "org.mpris.MediaPlayer2.Player", "PlaybackStatus"], capture_output=True, text=True, timeout=20).stdout
    return "Playing" if "Playing" in out else "Paused" if "Paused" in out else "Stopped"


def play_file(p):
    mpris("OpenUri", p.as_uri())


def track(album, n=1):
    return LIB / f"{album % 50:02d}" / f"album_{album:03d}" / f"{n:02d}.mp3"


def prepare(r):
    r.launch(LAUNCH)
    w = r.wait_window("Rhythmbox", timeout=60)
    r.mark("settle"); settle(r, timeout=90)
    r.maximize(w); time.sleep(2); cpu_idle(r)
    settle(r, timeout=30); r.shot("loaded")
    return w


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_play(r, d):
    w = prepare(r)
    r.mark("play"); play_file(track(1)); time.sleep(60); st = status(); r.shot("playing"); mpris("Pause")
    r.mark("idle", status=st); time.sleep(30)


def s03_covers(r, d):
    w = prepare(r)
    r.mark("covers")
    waits = []
    for a in range(10):
        t0 = time.monotonic(); play_file(track(a)); waits.append(round(time.monotonic() - t0, 1)); time.sleep(6)
    r.shot("covers"); mpris("Pause")
    r.mark("idle", openuri_s=waits); time.sleep(30)


def s04_skip(r, d):
    w = prepare(r)
    r.mark("skip"); play_file(track(20)); time.sleep(2)
    for _ in range(20):
        mpris("Next"); time.sleep(1.5)
    r.shot("skipped"); mpris("Pause")
    r.mark("idle"); time.sleep(30)


def s05_scan(r, d):
    r.launch(LAUNCH)
    w = r.wait_window("Rhythmbox", timeout=60)
    r.mark("scan"); r.maximize(w); time.sleep(10)
    t0 = time.monotonic(); cpu_idle(r, cores=0.1, quiet_s=5, timeout=600); secs = round(time.monotonic() - t0 + 10, 1)
    settle(r, timeout=60); r.shot("scanned")
    r.mark("idle", scan_s=secs); time.sleep(30)


def s07_s10_background(r, d):
    w = prepare(r)
    r.mark("idle_fg"); play_file(track(1)); time.sleep(20); mpris("Pause"); time.sleep(10)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    mpris("Play"); time.sleep(30); r.shot("restored"); mpris("Pause")
    r.mark("idle"); time.sleep(30)


def s08_bg_play(r, d):
    w = prepare(r)
    r.mark("background"); play_file(track(2)); time.sleep(2)
    X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120); st = status()
    r.mark("restore", status=st); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(5)
    r.shot("restored"); mpris("Pause")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_play, s03_covers, s04_skip, s05_scan, s07_s10_background, s08_bg_play]


if __name__ == "__main__":
    os.environ["GDK_BACKEND"] = "x11"
    names = [a for a in sys.argv[2:] if not a.startswith("--")] or [f.__name__ for f in RUNS]

    def before_each(_order=iter(names)):
        _current["run"] = next(_order, None)
        restore_profile()
    run_trials("rhythmbox", RUNS, M, evict_list(), "^/usr/bin/rhythmbox", before_each=before_each)
