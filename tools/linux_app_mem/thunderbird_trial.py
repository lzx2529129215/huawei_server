#!/usr/bin/env python3
"""Thunderbird 158 (Mozilla tarball, zh-CN) as the same-category substitute for coremail.

No mail account and no network: a "Local Folders" profile whose Inbox holds 10,000 generated
messages (make_mail.py; 428 MB mbox, every 50th with a 1-2 MB JPEG, every 500th with a PDF,
newest 50 unread). The profile (already indexed, incl. the global search index) is reset from a
baseline before every run. One cold start per operation, Xvfb screen, window 3840x2160.

Operations follow the Excel list for coremail (A12-S01..S10). S04 (sync new mail), S08
(background receive) and S09 (background upload) need a mail server and are not measured.
  s01_startup        start; the Inbox (10,000 messages) is shown
  s02_read           read 20 messages in a row (Down arrow, 2 s each)
  s03_search         quick filter over the 10,000 messages: "budget", "meeting", "invoice", "Sender 42"
                     (the global search index is not built in the profile: Thunderbird only starts it
                     after a long idle time, so the global search is not used)
  s05_attachments    show 5 messages with large JPEG attachments (shown inline)
  s06_compose        write a mail with a 1.9 MB JPEG attachment and "Send Later" (goes to the Outbox)
  s07_s10_background hide 2 min; restore and open the next unread message

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 thunderbird_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, cpu_idle, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/thunderbird"    # empty
APP = Path.home() / "linux_app_mem/apps/thunderbird"
PROFILE = Path.home() / "linux_app_mem/browser/thunderbird"
BASE = Path.home() / "linux_app_mem/profiles/thunderbird_baseline"
ATTACH = Path.home() / "linux_app_mem/materials/mail/att_1.jpg"
LAUNCH = [str(APP / "thunderbird"), "--profile", str(PROFILE), "--no-remote"]
FIRST_ROW = (1000, 125)                        # first message in the list (window maximized)
FIRST_ROW_FILTERED = (1000, 215)               # first message while the quick filter bar is shown


class Abort(RuntimeError):
    pass


def evict_list():
    return [str(APP), str(PROFILE), "/usr/share/fonts"]


def restore_profile():
    M.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", "--delete", f"{BASE}/", f"{PROFILE}/"], check=True)


def visible(pattern):
    for x in X(["xdotool", "search", "--onlyvisible", "--name", pattern], check=False).stdout.split():
        return x
    return None


def wait_visible(pattern, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        x = visible(pattern)
        if x:
            return x
        time.sleep(0.3)
    raise Abort(f"no window matching {pattern!r}")


def prepare(r):
    r.launch(LAUNCH)
    w = r.wait_window("Mozilla Thunderbird", timeout=90)
    r.mark("settle"); settle(r, timeout=90)
    r.maximize(w); time.sleep(2); cpu_idle(r)
    if "收件箱" not in X(["xdotool", "getwindowname", w]).stdout:
        raise Abort(f"Inbox not shown: {X(['xdotool', 'getwindowname', w]).stdout.strip()}")
    settle(r, timeout=30); r.shot("loaded")
    return w


def select_first(r, w):
    click(w, *FIRST_ROW); time.sleep(1)


FILTER_BOX = (500, 112)                        # quick filter text box (bar shown)


def open_filter_bar(r, w):
    r.key(w, "ctrl+shift+k"); time.sleep(1.5)            # toggles the quick filter bar: press it once only


def quick_filter(r, w, text):
    click(w, *FILTER_BOX); time.sleep(0.4); r.key(w, "ctrl+a"); r.type(w, text, delay_ms=40)
    time.sleep(4); cpu_idle(r)


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_read(r, d):
    w = prepare(r)
    r.mark("read"); select_first(r, w)
    for _ in range(20):
        r.key(w, "Down"); time.sleep(2)
    r.shot("read")
    r.mark("idle"); time.sleep(30)


def s03_search(r, d):
    w = prepare(r)
    r.mark("search"); open_filter_bar(r, w)
    quick_filter(r, w, "budget"); r.shot("filter1")
    quick_filter(r, w, "meeting"); quick_filter(r, w, "invoice"); quick_filter(r, w, "Sender 42"); r.shot("filter2")
    r.key(w, "Escape"); time.sleep(1)
    r.mark("idle"); time.sleep(30)


def s05_attachments(r, d):
    w = prepare(r)
    r.mark("attachments"); open_filter_bar(r, w)
    for k in (9950, 9900, 9850, 9800, 9750):              # every 50th mail has a 1-2 MB JPEG
        quick_filter(r, w, f"#{k}"); click(w, *FIRST_ROW_FILTERED); time.sleep(3); cpu_idle(r)
    r.shot("attachment")
    r.mark("idle"); time.sleep(30)


def s06_compose(r, d):
    w = prepare(r)
    r.mark("compose"); r.key(w, "ctrl+n")
    c = wait_visible("撰写|写信|写邮件|Write|Compose", timeout=30); time.sleep(3); r.maximize(c); time.sleep(1)
    r.type(c, "test@example.com", delay_ms=30); r.key(c, "Tab"); time.sleep(0.5); r.key(c, "Tab"); time.sleep(0.5)
    r.type(c, "memory test with attachment", delay_ms=30); r.key(c, "Tab"); time.sleep(0.5)
    r.type(c, "This message is only saved to the local Outbox.", delay_ms=20); time.sleep(1)
    r.key(c, "ctrl+shift+a"); f = wait_visible("附加|附件|Attach", timeout=20); time.sleep(1)
    r.key(f, "ctrl+l"); time.sleep(0.5); r.type(f, str(ATTACH), delay_ms=15); time.sleep(0.5); r.key(f, "Return")
    time.sleep(3); cpu_idle(r); r.shot("composed")
    r.key(c, "ctrl+shift+Return"); time.sleep(5); cpu_idle(r)              # Send Later -> Outbox
    for _ in range(3):                                                      # possible notice dialogs
        x = next((x for x, n in r.windows() if x not in (w, c) and n and "Thunderbird" not in n), None)
        if not x:
            break
        r.key(x, "Return"); time.sleep(2)
    outbox = PROFILE / "Mail/Local Folders/Unsent Messages"
    r.shot("sent_later")
    r.mark("idle", outbox_bytes=outbox.stat().st_size if outbox.exists() else -1); time.sleep(30)


def s07_s10_background(r, d):
    w = prepare(r)
    r.mark("idle_fg"); select_first(r, w); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    r.key(w, "n"); time.sleep(3); r.shot("restored")                         # next unread message
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_read, s03_search, s05_attachments, s06_compose, s07_s10_background]

if __name__ == "__main__":
    run_trials("thunderbird", RUNS, M, evict_list(), "[l]inux_app_mem/apps/thunderbird/thunderbird", before_each=restore_profile)
