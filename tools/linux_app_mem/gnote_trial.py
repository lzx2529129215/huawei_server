#!/usr/bin/env python3
"""Gnote 46 (Ubuntu package, GTK 4) as the same-category substitute for 备忘录 (Notes).

A notebook of 1,000 notes (make_notes.py: 999 ordinary notes + one ~3,000-line "长笔记 Long
note"), restored into ~/.local/share/gnote before every run (Backup dir cleared). One cold start
per operation on the Xvfb screen, window 3840x2160.

Operations follow the Excel list for 备忘录 (A11-S01..S10). Gnote notes cannot hold pictures, so
S03 (note with 20 pictures) and S05 (paste a large picture) are not measured; S08 (cloud sync)
and S09 (attachment download) need an account.
  s01_startup        start; the list of 1,000 notes
  s02_edit           open the long note, type 5 lines at the end, undo x3, page up/down x30
  s04_search         full-text search of the 1,000 notes for 4 words
  s07_s10_background long note open; hide 2 min; restore and scroll

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 gnote_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, cpu_idle, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/gnote_run"        # empty
NOTES = Path.home() / "linux_app_mem/materials/gnote/notes"
DATA = Path.home() / ".local/share/gnote"
LAUNCH = ["/usr/bin/gnote"]
FIRST_RESULT = (1400, 128)                     # first row of the note list


class Abort(RuntimeError):
    pass


def evict_list():
    return ["/usr/bin/gnote", "/usr/lib/x86_64-linux-gnu/gnote", "/usr/share/gnote", "/usr/share/fonts", str(DATA)]


def restore_profile():
    M.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", "--delete", "--exclude", "manifest.xml", "--exclude", "addins",
                    f"{NOTES}/", f"{DATA}/"], check=True)
    shutil.rmtree(DATA / "Backup", ignore_errors=True)


def prepare(r):
    r.launch(LAUNCH)
    w = r.wait_window("Gnote", timeout=60)
    r.mark("settle"); settle(r, timeout=90)
    r.maximize(w); time.sleep(2); cpu_idle(r)
    settle(r, timeout=30); r.shot("loaded")
    return w


def search(r, w, text):
    r.key(w, "ctrl+f"); time.sleep(0.8); r.key(w, "ctrl+a"); r.type(w, text, delay_ms=60)
    time.sleep(3); cpu_idle(r)


def open_long(r, w):
    search(r, w, "Long note")
    X(["xdotool", "mousemove", str(FIRST_RESULT[0]), str(FIRST_RESULT[1]), "click", "--repeat", "2", "--delay", "80", "1"])
    time.sleep(3); cpu_idle(r)


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_edit(r, d):
    w = prepare(r)
    r.mark("edit"); open_long(r, w)
    X(["xdotool", "mousemove", "1900", "1200", "click", "1"]); time.sleep(0.5); r.key(w, "ctrl+End")
    for i in range(5):
        r.key(w, "Return"); r.type(w, f"memory test line {i}: the quick brown fox jumps over the lazy dog", delay_ms=25)
    time.sleep(1); r.shot("typed")
    for _ in range(3):
        r.key(w, "ctrl+z"); time.sleep(0.5)
    for k in ["Page_Up"] * 30 + ["Page_Down"] * 30:
        r.key(w, k, delay_ms=40); time.sleep(0.2)
    settle(r, timeout=30); r.shot("edited")
    r.mark("idle"); time.sleep(30)


def s04_search(r, d):
    w = prepare(r)
    r.mark("search")
    for q in ("budget", "weekend", "recipe 00", "meeting"):
        search(r, w, q); time.sleep(2)
    r.shot("searched")
    r.mark("idle"); time.sleep(30)


def s07_s10_background(r, d):
    w = prepare(r)
    r.mark("open"); open_long(r, w)
    r.mark("idle_fg"); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    X(["xdotool", "mousemove", "1900", "1200", "click", "1"]); time.sleep(0.5)
    for _ in range(10):
        r.key(w, "Page_Down", delay_ms=40); time.sleep(0.3)
    r.shot("restored")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_edit, s04_search, s07_s10_background]

if __name__ == "__main__":
    os.environ["GDK_BACKEND"] = "x11"
    run_trials("gnote", RUNS, M, evict_list(), "^/usr/bin/gnote", before_each=restore_profile)
