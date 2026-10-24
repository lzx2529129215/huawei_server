#!/usr/bin/env python3
"""Evolution 3.52 (Ubuntu package) as the same-category substitute for 华为邮箱 (A13).

No mail account and no network: the local "On This Computer" Inbox (Maildir) holds the same
10,000 generated messages as the Thunderbird test (make_mail.py; every 50th with a 1-2 MB JPEG,
every 500th with a PDF). The baseline (profiles/evolution_baseline: ~/.local/share/evolution/mail
already indexed, ~/.config/evolution, ~/.cache/evolution/mail) was made on 2026-10-22: first start,
"Welcome" assistant closed with "Do not show this wizard again", Inbox opened once so the summary
is built. Settings changed for the tests (GSettings, reset after the tests): show-startup-wizard
false, show-to-do-bar false (the To Do bar would list the 10,000 test events of the GNOME Calendar
test), show-attachment-bar false (attachments then sit at the end of the message with an inline
"show" toggle). The local calendar, contacts and the EDS services (evolution-source-registry,
-calendar-factory, -addressbook-factory) run outside the app's cgroup and are not counted.
One cold start per operation, Xvfb screen, window 3840x2160, stage-3 cache eviction (the app's own
files, the libraries it links, fonts, its data).

Operations follow the Excel list for 华为邮箱 (A13-S01..S10). S03 (switch between 3 accounts),
S06 (sync), S08 (background receive) and S09 (background attachment download) need mail accounts
or a server and are not measured.
  s01_startup        start; the Inbox (10,000 messages) is shown
  s02_read           read 20 messages in a row (Down arrow, 2 s each)
  s04_search         search the 10,000 messages: "budget", "meeting", "invoice", "Sender 42"
  s05_attachments    show the JPEG attachment (1-2 MB) of 5 messages inline
  s07_s10_background hide 2 min; restore and open the next unread message ("." key)

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 evolution_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, cpu_idle, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/evolution"      # empty
BASE = Path.home() / "linux_app_mem/profiles/evolution_baseline"
SHARE = Path.home() / ".local/share/evolution/mail"
CONF = Path.home() / ".config/evolution"
CACHE = Path.home() / ".cache/evolution/mail"
# GDK_BACKEND=x11 is required: without it Evolution 3.52 connects to the WSLg Wayland socket
# ($XDG_RUNTIME_DIR/wayland-0) even when WAYLAND_DISPLAY is unset, i.e. it opens on the user's real
# desktop (happened in the first trial, 2026-10-22 15:01-15:08). Checked with WAYLAND_DEBUG=1.
LAUNCH = ["env", "GDK_BACKEND=x11", "/usr/bin/evolution"]
SEARCH_BOX = (1200, 112)                       # "Subject or Addresses contain" (window 3840x2160)
SEARCH_CLEAR = (3620, 112)
LIST_ROW = (1000, 700)                         # a row in the message list
FIRST_ROW = (1000, 168)                        # first message of a search result
PREVIEW = (1200, 1900)                         # message preview (bottom pane)
ATTACH_TOGGLE = (245, 2090)                    # inline toggle of the attachment, preview scrolled to the end


class Abort(RuntimeError):
    pass


def evict_list():
    libs = set()
    for line in subprocess.run(["ldd", LAUNCH[-1]], capture_output=True, text=True).stdout.splitlines():
        m = re.search(r"=> (/\S+)", line)
        if m:
            libs.add(m.group(1))
    return sorted(libs) + [LAUNCH[-1], "/usr/lib/evolution", "/usr/share/evolution", "/usr/share/fonts",
                           str(SHARE), str(CONF), str(CACHE)]


def restore_profile():
    M.mkdir(parents=True, exist_ok=True)
    for src, dst in ((BASE / "share_mail", SHARE), (BASE / "config", CONF), (BASE / "cache_mail", CACHE)):
        dst.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-a", "--delete", f"{src}/", f"{dst}/"], check=True)


def prepare(r):
    r.launch(LAUNCH)
    w = r.wait_window("Inbox", timeout=90)
    r.mark("settle"); r.maximize(w); time.sleep(2); settle(r, timeout=90); cpu_idle(r)
    if "unread" not in X(["xdotool", "getwindowname", w]).stdout:
        raise Abort(f"Inbox not shown: {X(['xdotool', 'getwindowname', w]).stdout.strip()}")
    settle(r, timeout=30); r.shot("loaded")
    return w


def wheel_down(w, x, y, n):
    for _ in range(n):
        click(w, x, y, button=5)
        time.sleep(0.05)


def search(r, w, text):
    click(w, *SEARCH_BOX); time.sleep(0.4); r.key(w, "ctrl+a"); r.type(w, text, delay_ms=40)
    r.key(w, "Return"); time.sleep(3); cpu_idle(r)


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_read(r, d):
    w = prepare(r)
    r.mark("read"); click(w, *LIST_ROW); time.sleep(1)
    for _ in range(20):
        r.key(w, "Down"); time.sleep(2)
    r.shot("read")
    r.mark("idle"); time.sleep(30)


def s04_search(r, d):
    w = prepare(r)
    r.mark("search")
    for i, q in enumerate(("budget", "meeting", "invoice", "Sender 42")):
        search(r, w, q)
        if i == 0:
            r.shot("search1")
    r.shot("search2"); click(w, *SEARCH_CLEAR); time.sleep(3); cpu_idle(r)
    r.mark("idle"); time.sleep(30)


def s05_attachments(r, d):
    w = prepare(r)
    r.mark("attachments")
    for k in (9950, 9900, 9850, 9800, 9750):              # every 50th mail has a 1-2 MB JPEG
        search(r, w, f"#{k}"); click(w, *FIRST_ROW); time.sleep(2)
        click(w, *PREVIEW); wheel_down(w, *PREVIEW, 30); time.sleep(1)
        click(w, *ATTACH_TOGGLE); time.sleep(3); wheel_down(w, *PREVIEW, 15); time.sleep(2); cpu_idle(r)
    r.shot("attachment"); click(w, *SEARCH_CLEAR); time.sleep(3)
    r.mark("idle"); time.sleep(30)


def s07_s10_background(r, d):
    w = prepare(r)
    r.mark("idle_fg"); click(w, *LIST_ROW); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    r.key(w, "period"); time.sleep(3); r.shot("restored")                    # next unread message
    r.mark("idle", title=X(["xdotool", "getwindowname", w]).stdout.strip()); time.sleep(30)


RUNS = [s01_startup, s02_read, s04_search, s05_attachments, s07_s10_background]

if __name__ == "__main__":
    run_trials("evolution", RUNS, M, evict_list(), "[/]usr/bin/evolution", before_each=restore_profile)
