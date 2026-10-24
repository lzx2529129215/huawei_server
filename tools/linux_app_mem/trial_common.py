#!/usr/bin/env python3
"""Shared runner for per-app trials (one cold start per operation, on the Xvfb screen)."""
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import appmem  # noqa: E402

X = appmem.sh


def filler(n, seed=7):
    rng = random.Random(seed)
    words = "memory pressure reclaim anonymous file page cache document editing paragraph table figure".split()
    out = []
    while sum(len(w) + 1 for w in out) < n:
        out.append(rng.choice(words))
    return " ".join(out)


def settle(r, quiet_mib=4, window_s=5, timeout=90):
    """Wait until memory.current moves less than quiet_mib over window_s seconds."""
    hist, end = [], time.monotonic() + timeout
    while time.monotonic() < end:
        hist.append((time.monotonic(), int((r.cg / "memory.current").read_text())))
        hist = [h for h in hist if h[0] >= hist[-1][0] - window_s]
        if hist[-1][0] - hist[0][0] >= window_s - 0.3 and \
                (max(v for _, v in hist) - min(v for _, v in hist)) < quiet_mib * appmem.MiB:
            return True
        time.sleep(0.25)
    return False


def cpu_idle(r, cores=0.25, quiet_s=3, timeout=300):
    """Wait until the app's cgroup uses less than `cores` CPUs for quiet_s seconds (busy apps drop input)."""
    f = r.cg / "cpu.stat"
    use = lambda: int(next(l.split()[1] for l in f.read_text().splitlines() if l.startswith("usage_usec")))
    t0 = time.monotonic(); quiet = 0.0; last, lt = use(), time.monotonic()
    while quiet < quiet_s:
        if time.monotonic() - t0 > timeout:
            return False
        time.sleep(0.5)
        now, nt = use(), time.monotonic()
        rate = (now - last) / 1e6 / (nt - lt)
        quiet = quiet + (nt - lt) if rate < cores else 0.0
        last, lt = now, nt
    return True


def new_windows(r, before):
    return [x for x in r.windows() if x[0] not in before]


def open_main(r, argv, pattern, timeout=90):
    r.launch(argv)
    w = r.wait_window(pattern, timeout=timeout)
    r.maximize(w)
    return w


def click(w, x, y, button=1):
    X(["xdotool", "mousemove", "--window", str(w), str(x), str(y), "click", str(button)])


# Shared system files every GUI app maps, many of them only via dlopen (Qt/GTK plugins, Mesa/LLVM
# for software OpenGL, GStreamer, codecs) and therefore missing from `ldd`. Since 2026-10-22 06:00
# they are dropped before every run too, so the app is charged for all the files it reads.
SHARED = ["/usr/lib/x86_64-linux-gnu", "/usr/share/icons", "/usr/share/mime", "/usr/share/glib-2.0",
          "/usr/share/X11", "/usr/share/fonts"]
# Only these apps were measured that way (2026-10-22 05:50-09:15). By the user's decision all other
# apps keep the stage-3 method (app's own files, config, fonts), so each app is consistent in itself.
SHARED_EVICT_APPS = {"gnote", "rhythmbox", "thunderbird", "calendar"}


def run_trials(app, runs, materials, evict, running_pattern, argv=None, before_each=None, display=None):
    """argv: OUT_DIR [--rep=rN] [run ...]. Each run gets fresh copies of the materials in work/."""
    argv = argv or sys.argv[1:]
    out, args, rep = Path(argv[0]), argv[1:], "r1"
    if args and args[0].startswith("--rep="):
        rep, args = args[0][6:], args[1:]
    names = args or [f.__name__ for f in runs]
    table = {f.__name__: f for f in runs}
    # The WSLg PulseAudio server times out (since 2026-10-22); ~/.asoundrc makes the ALSA default a
    # null device, and a refused PulseAudio address makes apps fall back to it at once.
    os.environ["PULSE_SERVER"] = "unix:/nonexistent-pulse"
    # File dialogs must stay on the test screen: the desktop portal would show them on the user's
    # real (WSLg) desktop. GTK 3 and GTK 4 then use their own in-process dialogs.
    os.environ["GTK_USE_PORTAL"] = "0"
    os.environ["GDK_DEBUG"] = "no-portals"
    display = display or os.environ.get("APPMEM_DISPLAY")   # e.g. ":98" for a second Xvfb used in test runs
    if display:   # e.g. ":0" (WSLg desktop) for apps that cannot start on Xvfb
        os.environ["DISPLAY"] = display
        os.environ.pop("WAYLAND_DISPLAY", None)
    else:
        appmem.use_xvfb()
    for name in names:
        if subprocess.run(["pgrep", "-f", running_pattern], capture_output=True).returncode == 0:
            sys.exit(f"{app} is already running; close it before {name}")
        if before_each:
            before_each()
        r = appmem.Run(app, f"{name}_{rep}", out)
        work = r.out / "work"
        work.mkdir(exist_ok=True)
        for f in Path(materials).iterdir():
            if f.is_file() and f.name != "manifest.json":
                shutil.copyfile(f, work / f.name)
        # APPMEM_NO_EVICT: test runs next to a formal run must not drop the formal run's page cache
        shared = SHARED if app in SHARED_EVICT_APPS else []
        n = 0 if os.environ.get("APPMEM_NO_EVICT") else appmem.evict(list(evict) + shared + [str(work)])
        r.mark("evicted", files=n, display=os.environ.get("DISPLAY"))
        try:
            table[name](r, work)
        except Exception as e:  # keep the samples, record the failure
            r.mark("error", error=repr(e))
            print(f"{name}: ERROR {e!r}", flush=True)
        finally:
            try:  # which files the app's processes map (to split own files from shared system files)
                import residency
                mf = residency.mapped_files(r.pids())
                (r.out / "mapped_files.json").write_text(json.dumps(
                    {"split_mib": residency.split_mapped(mf), "files_kib": mf}, ensure_ascii=False))
            except Exception as e:  # never let this break a measurement
                print(f"{name}: mapped_files failed: {e!r}", flush=True)
            r.stop()
        shutil.rmtree(work)  # our own copies
        s = appmem.summarize(r.out)
        (r.out / "summary.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))
        print(f"{name}: " + ", ".join(f"{p}: peak {v['peak']['current']} (anon {v['peak']['anon']}, file {v['peak']['file']})"
                                      for p, v in s.items() if p not in ("start", "stop", "evicted")), flush=True)
        time.sleep(5)
