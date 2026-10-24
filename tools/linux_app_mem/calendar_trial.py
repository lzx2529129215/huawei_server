#!/usr/bin/env python3
"""GNOME Calendar 46 (Ubuntu package, GTK 4) as the same-category substitute for 日历 (Calendar).

Data: the local "Personal" calendar (On This Computer) holds 10,280 events (make_ics.py: 10,000
over two years + a dense week of 280 events, 2026-10-19..25). GNOME Calendar keeps no data itself:
the events live in evolution-data-server (EDS), which runs as separate systemd user services
(evolution-source-registry, evolution-calendar-factory, ...), outside the app's cgroup. Before
every run the EDS services are stopped and the calendar file and EDS caches restored, so EDS
starts cold when the app activates it; EDS memory is sampled into eds.csv and reported separately.
One cold start per operation on the Xvfb screen, window 3840x2160.

Operations follow the Excel list for 日历 (A15-S01..S10). S06 (sync 3 accounts), S08 (background
sync) and S09 (reminders, handled by the separate evolution-alarm-notify) are not measured:
  s01_startup        start; month view (October 2026)
  s02_event          create an event ("+", title, Save), open an existing event and close it
  s03_week           switch to the dense week view and scroll through it
  s04_search         search the 10,280 events for 3 words
  s05_import         open import.ics (5,000 events) and import it into the Personal calendar
  s07_s10_background hide 2 min; restore and go to today

Usage: APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 python3 calendar_trial.py OUT_DIR [--rep=r1] [run ...]
"""
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_common import X, click, cpu_idle, run_trials, settle  # noqa: E402

M = Path.home() / "linux_app_mem/materials/calendar_run"        # empty
DATA = Path.home() / "linux_app_mem/materials/calendar"
CAL = Path.home() / ".local/share/evolution/calendar/system/calendar.ics"
EDS_CACHE = Path.home() / ".cache/evolution"
EDS = ["evolution-source-registry", "evolution-calendar-factory", "evolution-addressbook-factory"]
LAUNCH = ["/usr/bin/gnome-calendar"]
PLUS, SAVE, WEEK, MONTH, SEARCH, TODAY = (3697, 27), (2227, 600), (2007, 27), (2133, 27), (3657, 27), (350, 27)
UCG = Path(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice")


class Abort(RuntimeError):
    pass


def evict_list():
    return ["/usr/bin/gnome-calendar", "/usr/share/gnome-calendar", "/usr/libexec/evolution-calendar-factory",
            "/usr/libexec/evolution-source-registry", "/usr/lib/x86_64-linux-gnu/evolution-data-server",
            "/usr/share/fonts", str(CAL.parent), str(EDS_CACHE), str(DATA)]


def restore_profile():
    M.mkdir(parents=True, exist_ok=True)
    subprocess.run(["systemctl", "--user", "stop"] + [f"{s}.service" for s in EDS], capture_output=True)
    CAL.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DATA / "calendar.ics", CAL)
    shutil.rmtree(EDS_CACHE / "calendar", ignore_errors=True)


class EdsSampler(threading.Thread):
    """Memory of the EDS services (outside the app's cgroup), every second, into eds.csv."""

    def __init__(self, r):
        super().__init__(daemon=True)
        self.r, self.stop = r, threading.Event()

    def run(self):
        with open(self.r.out / "eds.csv", "w") as f:
            f.write("t,phase," + ",".join(f"{s}_current,{s}_anon,{s}_file" for s in EDS) + "\n")
            while not self.stop.is_set():
                vals = []
                for s in EDS:
                    cg = UCG / f"{s}.service"
                    try:
                        cur = int((cg / "memory.current").read_text())
                        st = dict(l.split() for l in (cg / "memory.stat").read_text().splitlines())
                        vals += [cur, int(st["anon"]), int(st["file"])]
                    except (OSError, KeyError, ValueError):
                        vals += [0, 0, 0]
                f.write(f"{self.r.now():.1f},{self.r.phase}," + ",".join(map(str, vals)) + "\n"); f.flush()
                time.sleep(1)


def events_in_file():
    try:
        return CAL.read_text(errors="replace").count("BEGIN:VEVENT")
    except OSError:
        return -1


_eds = {"t": None}


def prepare(r):
    r.launch(LAUNCH)
    if _eds["t"]:
        _eds["t"].stop.set()
    _eds["t"] = EdsSampler(r); _eds["t"].start()
    w = r.wait_window("Calendar", timeout=60)
    r.mark("settle"); settle(r, timeout=90)
    r.maximize(w); time.sleep(3); cpu_idle(r)
    settle(r, timeout=60); r.shot("loaded")
    return w


# ---------------------------------------------------------------- runs ---
def s01_startup(r, d):
    prepare(r)
    r.mark("steady"); time.sleep(30); r.shot("steady")


def s02_event(r, d):
    w = prepare(r)
    r.mark("event"); click(w, *PLUS); time.sleep(2)
    r.type(w, "memory test event", delay_ms=40); time.sleep(1); r.shot("new_event"); click(w, *SAVE); time.sleep(3)
    click(w, 600, 220); time.sleep(2); r.shot("opened_event"); r.key(w, "Escape"); time.sleep(2)   # an event in the grid
    cpu_idle(r)
    r.mark("idle", events=events_in_file()); time.sleep(30)


def s03_week(r, d):
    w = prepare(r)
    r.mark("week"); click(w, *WEEK); time.sleep(4); cpu_idle(r)
    X(["xdotool", "mousemove", "1900", "1200"])
    for b in ["5"] * 15 + ["4"] * 15:                      # scroll the day columns down and up again
        X(["xdotool", "click", b]); time.sleep(0.3)
    r.shot("week"); click(w, *MONTH); time.sleep(3); cpu_idle(r)
    r.mark("idle"); time.sleep(30)


def s04_search(r, d):
    w = prepare(r)
    r.mark("search")
    for q in ("review", "dense", "launch"):
        click(w, *SEARCH); time.sleep(1); r.key(w, "ctrl+a"); r.type(w, q, delay_ms=60); time.sleep(4); cpu_idle(r)
        r.key(w, "Escape"); time.sleep(1)
    r.shot("searched")
    r.mark("idle"); time.sleep(30)


def s05_import(r, d):
    w = prepare(r)
    r.mark("import")
    subprocess.run(["timeout", "30", "gnome-calendar", str(DATA / "import.ics")],
                   env=dict(os.environ, GDK_BACKEND="x11"), capture_output=True)     # hands the file to the running app
    end = time.monotonic() + 300
    dlg = None
    while time.monotonic() < end:                          # the title becomes "Import 5000 events" when parsed
        dlg = next((x for x, n in r.windows() if n.startswith("Import") and "5000" in n), None)
        if dlg:
            break
        time.sleep(1)
    if not dlg:
        raise Abort("import dialog did not finish parsing the file")
    time.sleep(2); click(dlg, 496, 27)
    t0 = time.monotonic()
    while events_in_file() < 15280 and time.monotonic() - t0 < 600:
        time.sleep(2)
    secs = round(time.monotonic() - t0, 1); cpu_idle(r, timeout=300); r.shot("imported")
    r.mark("idle", events=events_in_file(), import_s=secs); time.sleep(30)


def s07_s10_background(r, d):
    w = prepare(r)
    r.mark("idle_fg"); time.sleep(30)
    r.mark("background"); X(["xdotool", "windowunmap", "--sync", str(w)], check=False)
    time.sleep(120)
    r.mark("restore"); X(["xdotool", "windowmap", "--sync", str(w)], check=False); time.sleep(1)
    click(w, *WEEK); time.sleep(2); click(w, *TODAY); time.sleep(3); cpu_idle(r); r.shot("restored")
    r.mark("idle"); time.sleep(30)


RUNS = [s01_startup, s02_event, s03_week, s04_search, s05_import, s07_s10_background]

if __name__ == "__main__":
    os.environ["GDK_BACKEND"] = "x11"
    run_trials("calendar", RUNS, M, evict_list(), "^/usr/bin/gnome-calendar", before_each=restore_profile)
