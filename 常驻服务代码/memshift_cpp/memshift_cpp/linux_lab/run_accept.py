#!/usr/bin/env python3
"""Acceptance-experiment runner, structure B (results/linux_oom_lab_accept_20261024/stage2_spec.md §4).

One case = a group of 7-8 memlab_app units clicked click_gap_s apart inside
memlab.slice (the 16 GiB "machine"). The last-clicked app is in front
(oom_score_adj 100), the others are background (900). Every app loads its daily
memory, logs DAILY_READY and AWAIT_START and waits. first_ramp_after_last_click_s
after the last click (barrier: all alive apps DAILY_READY, at most
barrier_wait_max_s later) the runner starts the peaks so that all apps reach the
top at one moment T: with ramp_cap_s every app ramps within that time and all
start together, otherwise each app starts at T - its measured ramp. Before an app
starts it is protected (memshiftctl --protect, memory.high=max, 50 ms, re-check).
Apps hold, release, log STARTUP_READY and exit; the runner deletes their data
files (page cache), checks that the slice is empty and runs the next case.
The oracle hint (observe and apply+hint) goes out hint_lead_s before the first
click: the case's same-moment total, per-app targets and the planned T.

Events: popups (run_round.PopupMonitor), policy kills (run_round.PolicyKiller,
70 ms / 700 ms levels), kernel OOM kills (kernel log), startup failures (a group
app killed, or an ENOMEM-like ERROR, between START and STARTUP_READY). The
attribution lives in pure functions (analyze() and its helpers), see --self-test.

Usage:
  run_accept.py --scenario S.json --mode observe|apply [--hint on|off] --label L --run-id R --out DIR
                [--case-set cases|tuning|reference] [--first K] [--cases N] [--set key=value ...]
  run_accept.py --scenario S.json --calibrate --out cal.json
  add --dry-run to print the schedule and every command without running anything
  run_accept.py --self-test
  run_accept.py --check-memfree
--first K is the 1-based position of the first case in the case set (default 1);
--cases N runs N cases from there (default: all the rest).
Run directory: DIR/<label>_<tag>_<run-id>, tag obs (observe), tmoh (apply + hint),
tmo (apply, no hint), obsnh (observe, no hint). It holds scenario.json, args.json,
memshiftd.ini/.stderr, memshiftd/ (daemon logs), apps/<case>/<app>.log,
pressure.csv (10 Hz), events.csv, cases.csv, attempts.csv, progress.json, summary.json.
Exit codes: 0 run finished (validity is in summary.json), 1 run error (summary.json
still written, valid=false), 2 usage, 3 calibrated acceptance checks fail,
4 MemFree < 17 GiB (override: --allow-low-memfree), 5 environment (disk space, or
another run holds the memlab.slice lock).
"""
import argparse
import csv
import ctypes
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
import zlib
from collections import Counter, defaultdict, deque
from pathlib import Path

LAB = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB))
import run_round as rr  # noqa: E402

MiB = rr.MiB
TICK_S = 0.1
MEMFREE_MIN_GIB = 17
DISK_MIN_GIB = 20
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_CHECKS, EXIT_MEMFREE, EXIT_ENV = 0, 1, 2, 3, 4, 5
SUCCESS, FAILURE, UNCONFIRMED, NOT_RUN = "success", "confirmed_memory_failure", "unconfirmed", "not_run"
LIFECYCLE = ("START", "DAILY_READY", "AWAIT_START", "PEAK_BEGIN", "PEAK_HOT_ANON", "PEAK_DONE",
             "PEAK_RELEASE", "STARTUP_READY", "EXIT")
TIMELINE_DEFAULTS = {
    "click_gap_s": 3, "first_ramp_after_last_click_s": 10, "barrier_wait_max_s": 20, "hold_s": 15,
    "exit_after_release_s": 2, "gap_before_next_case_s": 15, "hint_lead_s": 15, "ramp_cap_s": 1,
    "release_timeout_s": 30, "fill_threads": 4,
    "precase": {"current_max_mib": 128, "zswap_max_mib": 16, "swap_max_mib": 64, "wait_max_s": 30},
}
MEM_ERROR = re.compile(r"cannot allocate memory|enomem|bad_alloc|out of memory|errno=12\b", re.I)
OOM_MEMCG = re.compile(r"task_memcg=\S*/memlab-([^/,]+)\.service")   # same as run_round.kernel_log
OOM_CONSTRAINT = re.compile(r"constraint=(\w+)")


class UsageError(Exception):
    pass


class Terminated(BaseException):
    """SIGTERM: unwinds the run so memshiftd and the units are still cleaned up."""


def _raise_terminated(signum, frame):
    raise Terminated(f"signal {signum}")


def describe(e):
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


# ---------------------------------------------------------------- small helpers

def fnum(x):
    """Number for a command line: 5306.0 -> '5306', 573/14.8 -> '38.7162'."""
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:.6g}"


def fval(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def run_cmd(cmd, timeout=30.0):
    """subprocess.run with a timeout that never raises: (rc, stdout, stderr); 124 timeout, 127 not found."""
    cmd = [str(c) for c in cmd]
    try:
        r = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout:g} s: {shlex.join(cmd)}"
    except OSError as e:
        return 127, "", str(e)


def bounded_rr_sh(cmd, check=True, timeout=60.0):
    """run_round.sh with a timeout; installed for real runs (PolicyKiller, cleanup_units use rr.sh)."""
    try:
        return subprocess.run(cmd, check=check, text=True, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        if check:
            raise
        return subprocess.CompletedProcess(cmd, 124 if isinstance(e, subprocess.TimeoutExpired) else 127, "", str(e))


def wait_for(cond, timeout, step=0.05):
    end = time.monotonic() + timeout
    while True:
        if cond():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(step)


def write_json(path, obj):
    """Atomic write, so a reader never sees half a file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=str))
    os.replace(tmp, path)


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", restval="")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else int(v) if isinstance(v, bool) else v) for k, v in r.items()})


def safe_rows(path):
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def memfree_mib(text=None):
    text = rr.read("/proc/meminfo") if text is None else text
    for line in text.splitlines():
        if line.startswith("MemFree:"):
            try:
                return int(line.split()[1]) / 1024
            except (IndexError, ValueError):
                return None
    return None


def disk_problems():
    out = []
    for mount in ("/", "/mnt/c"):
        try:
            if os.path.isdir(mount) and shutil.disk_usage(mount).free < DISK_MIN_GIB * 1024 * MiB:
                out.append(f"less than {DISK_MIN_GIB} GiB free on {mount}")
        except OSError:
            pass
    return out


def acquire_lock():
    """One memlab.slice user at a time: (fd, path); fd None when another run holds it, -1 if no lock possible."""
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "memlab_slice.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    except OSError:
        return -1, path
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None, path
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd, path


def anchor_cmd():
    # Keeps memlab.slice alive between cases (memshiftd stops if its scope disappears).
    return ["systemd-run", "--user", "-q", f"--unit={rr.unit('anchor')}", "--slice=memlab", "sleep", "infinity"]


def cleanup_cmds():
    """What rr.cleanup_units() runs (for --dry-run)."""
    return [["systemctl", "--user", "stop", "memlab-*.service"], ["systemctl", "--user", "reset-failed", "memlab-*.service"],
            ["systemctl", "--user", "stop", "memlab.slice"]]


def log_path(rd, cid, app):
    return Path(rd) / "apps" / cid / f"{app}.log"


def data_path(rd, cid):
    return Path(rd) / "data" / cid


def curve_path(rd, app):
    return Path(rd) / "curves" / f"{app}.txt"


def curve_text(a):
    return "".join(f"{t} {m}\n" for t, m in a["peak_curve"])


# ---------------------------------------------------------------- scenario (pure)

def timeline(sc):
    raw = sc.get("timeline") or {}
    tl = {k: v for k, v in TIMELINE_DEFAULTS.items() if k != "precase"}
    tl.update({k: v for k, v in raw.items() if k != "precase"})
    if "hint_lead_s" not in raw and "hint_lead_before_first_click_s" in raw:
        tl["hint_lead_s"] = raw["hint_lead_before_first_click_s"]
    tl["precase"] = {**TIMELINE_DEFAULTS["precase"], **(raw.get("precase") or {})}
    return tl


def app_table(sc):
    apps = sc.get("apps") or {}
    return {a["name"]: a for a in apps} if isinstance(apps, list) else dict(apps)


def adj_values(sc):
    a = sc.get("adj") or {}
    return int(a.get("background", 900)), int(a.get("front", 100))


def apps_used(cases):
    return list(dict.fromkeys(n for c in cases for n in c["order"]))


def select_cases(sc, case_set="cases", first=1, n=None):
    """[(position in the case set, case)] for --case-set/--first/--cases."""
    key = {"tuning": "tuning_cases", "reference": "reference_cases"}.get(case_set, "cases")
    allc = sc.get(key) or []
    if not allc:
        raise UsageError(f"scenario has no {key}")
    if first < 1 or first > len(allc):
        raise UsageError(f"--first {first} is outside 1..{len(allc)} ({key})")
    end = len(allc) if n is None else first - 1 + n
    if n is not None and (n < 1 or end > len(allc)):
        raise UsageError(f"--cases {n} from position {first} needs {end} cases, {key} has {len(allc)}")
    for c in allc[first - 1:end]:
        if not c.get("id") or not c.get("order"):
            raise UsageError(f"case without id/order in {key}: {c}")
    return [(first + i, c) for i, c in enumerate(allc[first - 1:end])]


def daily_of(a):
    v = a.get("cal_daily_mib")
    return float(v) if v is not None else float(rr.daily_mib(a))


def peak_of(a):
    v = a.get("cal_peak_mib")
    return float(v) if v is not None else float(rr.daily_mib(a) + (a.get("peak_mib") or 0))


def acceptance_checks(sc, cases):
    """The three acceptance conditions on the selected cases, calibrated values when present.
    refuse = calibration values exist and a condition fails (exit 3)."""
    m = sc["scope"]["memory_max_mib"]
    apps = app_table(sc)
    used = apps_used(cases)
    missing = [n for n in used if n not in apps]
    if missing:
        raise UsageError(f"cases use apps that the scenario does not define: {missing}")
    n_cal = sum(1 for n in used if apps[n].get("cal_daily_mib") is not None and apps[n].get("cal_peak_mib") is not None)
    source = "calibrated" if n_cal == len(used) else ("partial" if n_cal else "nominal")
    daily = {n: daily_of(apps[n]) for n in used}
    peak = {n: peak_of(apps[n]) for n in used}
    rows = []
    for c in cases:
        d, p = sum(daily[n] for n in c["order"]), sum(peak[n] for n in c["order"])
        rows.append({"case": c["id"], "daily_sum_mib": round(d), "same_moment_mib": round(p),
                     "same_moment_ratio": round(p / m, 3), "daily_ok": d <= m, "same_moment_ok": p >= 1.2 * m})
    top = max(used, key=lambda n: peak[n])
    res = {"source": source, "memory_max_mib": m, "apps_used": len(used), "calibrated_apps": n_cal,
           "max_single_peak_mib": round(peak[top]), "max_single_peak_app": top,
           "all_daily_sum_mib": round(sum(daily.values())),
           "single_peak_le_M": all(peak[n] <= m for n in used), "all_daily_le_M": sum(daily.values()) <= m,
           "case_daily_le_M": all(r["daily_ok"] for r in rows),
           "same_moment_ge_1_2M": all(r["same_moment_ok"] for r in rows),
           "failing_cases": [r["case"] for r in rows if not (r["daily_ok"] and r["same_moment_ok"])], "cases": rows}
    res["ok"] = res["single_peak_le_M"] and res["all_daily_le_M"] and res["case_daily_le_M"] and res["same_moment_ge_1_2M"]
    res["refuse"] = source != "nominal" and not res["ok"]
    return res


def variant_tag(mode, hint):
    if mode == "observe":
        return "obs" if hint else "obsnh"
    return "tmoh" if hint else "tmo"


def app_ramp(a, tl):
    """Seconds an app's peak takes: ramp_cap_s, else its measured ramp (or curve length)."""
    cap = tl.get("ramp_cap_s")
    if cap:
        return float(cap)
    r = fval(a.get("ramp_s"))
    if r <= 0 and a.get("peak_curve"):
        r = float(a["peak_curve"][-1][0])
    return r


def plan_times(tl, order, ramps, t_hint):
    """Planned case times (runner clock) when the hint lead starts at t_hint."""
    first = t_hint + tl["hint_lead_s"]
    clicks = [first + i * tl["click_gap_s"] for i in range(len(order))]
    barrier = clicks[-1] + tl["first_ramp_after_last_click_s"]
    rmax = max(ramps.values()) if ramps else 0.0
    T = barrier + rmax
    return {"hint": t_hint, "first_click": first, "clicks": clicks, "barrier": barrier, "ramp_max": rmax, "T": T,
            "starts": {n: T - ramps[n] for n in order},
            "deadline": T + tl["hold_s"] + tl["exit_after_release_s"] + tl["release_timeout_s"]}


def case_length_s(tl, n, rmax):
    return (n - 1) * tl["click_gap_s"] + tl["first_ramp_after_last_click_s"] + rmax + tl["hold_s"] + tl["exit_after_release_s"]


def app_ttl(tl, n, rmax):
    return int(min(3600, math.ceil(case_length_s(tl, n, rmax) + 120)))


def case_bound_s(tl, n, rmax):
    """Longest a case can take with every bounded wait at its maximum (plus slack for commands)."""
    return (tl["precase"]["wait_max_s"] + tl["hint_lead_s"] + case_length_s(tl, n, rmax) + tl["barrier_wait_max_s"]
            + tl["release_timeout_s"] + 15 + max(0, tl["gap_before_next_case_s"] - tl["hint_lead_s"]) + 60)


def hint_plan(apps, order, tl, t_hint, T_planned, peak_unix):
    targets = {n: int(round(peak_of(apps[n]))) for n in order}
    return {"boost_mib": int(sum(targets.values())), "targets_mib": targets,
            "ttl_s": int(min(300, max(1, math.ceil(T_planned + tl["hold_s"] + 10 - t_hint)))),
            "peak_at_unix": int(round(peak_unix)), "T_planned_s": round(T_planned, 3)}


def hint_args(h):
    return ["--boost-mb", str(h["boost_mib"]), "--ttl", str(h["ttl_s"]),
            "--peak-targets", ",".join(f"{k}:{v}" for k, v in h["targets_mib"].items()),
            "--peak-at-unix", str(h["peak_at_unix"])]


def launch_cmd(a, name, tl, log, data_dir, seed, adj, ttl, build, curve_file=None):
    """systemd-run command of one click (run_round-style unit in memlab.slice)."""
    cmd = ["systemd-run", "--user", "-q", f"--unit={rr.unit(name)}", "--slice=memlab", "-p", f"OOMScoreAdjust={adj}",
           str(Path(build) / "memlab_app"), "--name", name, "--log", str(log), "--data-dir", str(data_dir),
           "--anon-mib", fnum(a.get("anon_mib", 0)), "--clean-mib", fnum(a.get("clean_mib", 0)),
           "--locked-mib", fnum(a.get("locked_mib", 0)), "--compress-ratio", fnum(a.get("compress_ratio", 1)),
           "--ttl", str(int(ttl)), "--seed", str(int(seed))]
    if a.get("dirty_mib"):
        cmd += ["--dirty-mib", fnum(a["dirty_mib"]), "--dirty-rate", fnum(a.get("dirty_rate_mib_s", 0))]
    for key, flag in (("hot_anon_mib", "--hot-anon-mib"), ("hot_file_mib", "--hot-file-mib"), ("hot_period", "--hot-period")):
        if a.get(key) is not None:
            cmd += [flag, fnum(a[key])]
    peak = fval(a.get("peak_mib"))
    if peak > 0:
        ramp, cap = app_ramp(a, tl), tl.get("ramp_cap_s")
        if cap:
            cmd += ["--peak-mib", fnum(peak), "--peak-rate", fnum(peak / float(cap))]
        elif a.get("peak_curve") and curve_file is not None:
            cmd += ["--peak-mib", fnum(peak), "--peak-rate", "0", "--peak-curve", str(curve_file)]
        else:
            cmd += ["--peak-mib", fnum(peak), "--peak-rate", fnum(peak / ramp) if ramp > 0 else "0"]
        if a.get("peak_compress_ratio"):
            cmd += ["--peak-compress-ratio", fnum(a["peak_compress_ratio"])]
        cmd += ["--hold", fnum(tl["hold_s"]), "--peak-deadline", fnum(round(ramp + max(5.0, 0.2 * ramp), 3))]
    cmd += ["--await-start", "1", "--exit-after-peak", fnum(tl["exit_after_release_s"])]
    if a.get("peak_hot_anon_mib"):
        cmd += ["--peak-hot-anon-mib", fnum(a["peak_hot_anon_mib"]),
                "--peak-hot-anon-period", fnum(a.get("peak_hot_anon_period") or 10)]
    if tl.get("fill_threads"):
        cmd += ["--fill-threads", str(int(tl["fill_threads"]))]
    return cmd


def cfg_value(v):
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def memshiftd_ini(sc, rd, names):
    o = {"log_dir": Path(rd) / "memshiftd", "lock_file": Path(rd) / "memshiftd.lock",
         "control_file": Path(rd) / "control.conf", "scope_cgroup": rr.SCOPE_REL}
    o.update(sc.get("memshiftd") or {})
    apps = app_table(sc)
    lines = ["[daemon]"] + [f"{k}={cfg_value(v)}" for k, v in o.items() if v is not None]
    for n in names:
        a = apps.get(n, {})
        lines += [f"[app {n}]", f"path={rr.SCOPE_REL}/{rr.unit(n)}", "reclaim=true",
                  f"min_keep_mb={a.get('min_keep_mb', 32)}", f"priority={a.get('priority', 900)}"]
    return "\n".join(lines) + "\n"


def precase_ok(st, pre):
    vals = (st.get("current_mib"), st.get("zswap_mib"), st.get("swap_mib"))
    if any(v is None for v in vals):
        return False
    return (not st.get("active") and not st.get("prev_data_exists") and st["current_mib"] <= pre["current_max_mib"]
            and st["zswap_mib"] <= pre["zswap_max_mib"] and st["swap_mib"] <= pre["swap_max_mib"])


# ---------------------------------------------------------------- logs and attribution (pure)

def parse_log_text(text, t0=0.0):
    """Records like run_round.parse_app_logs (t, event, key=value fields) plus t_s = t - t0,
    msg (text after the event) and the line; malformed lines are skipped, missing text = no events."""
    out = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            continue
        rec = {"t": t, "t_s": round(t - t0, 3), "event": parts[2], "msg": " ".join(parts[3:]), "line": line.strip()}
        for item in parts[3:]:
            k, sep, v = item.partition("=")
            if sep and k not in rec:
                rec[k] = v
        out.append(rec)
    return out


def first_events(evs):
    f = {}
    for e in evs:
        if e["event"] in LIFECYCLE and e["event"] not in f:
            f[e["event"]] = e
    return f


def kill_phase(f):
    """Phase of an attempt that ended here, from the last lifecycle event it logged
    (a SIGKILLed app logs nothing afterwards, so this is robust to clock offsets)."""
    for ev, phase in (("STARTUP_READY", "after_ready"), ("PEAK_RELEASE", "release"), ("PEAK_DONE", "hold"),
                      ("PEAK_BEGIN", "ramp"), ("DAILY_READY", "waiting"), ("START", "loading")):
        if ev in f:
            return phase
    return "unknown"


def is_memory_error(msg):
    """ERROR lines that are memory failures; an mlock rlimit error is a setup problem."""
    return bool(MEM_ERROR.search(msg or "")) and "LimitMEMLOCK" not in (msg or "")


def attempt_outcome(evs, kills, launched=True, stopped=False):
    """Terminal state of one app attempt (one case): success (STARTUP_READY),
    confirmed_memory_failure (a kill or an ENOMEM-like ERROR before STARTUP_READY) or
    unconfirmed. A kill only counts if the app did not log EXIT (SIGKILL leaves no EXIT);
    at most one kill counts per attempt, the earliest."""
    starts = [i for i, e in enumerate(evs) if e["event"] == "START"]
    if len(starts) > 1:
        evs = evs[starts[-1]:]
    f = first_events(evs)
    errors = [e for e in evs if e["event"] == "ERROR"]
    kills = sorted(kills, key=lambda k: k["t_s"])
    if "EXIT" in f:
        counted, dropped = None, [dict(k, drop_reason="app_logged_exit") for k in kills]
    else:
        counted = kills[0] if kills else None
        dropped = [dict(k, drop_reason="duplicate") for k in kills[1:]]
    mem_err = next((e for e in errors if is_memory_error(e["msg"])), None)
    if not launched and "START" not in f:
        outcome, cause = UNCONFIRMED, "launch_failed"
    elif "STARTUP_READY" in f:
        outcome, cause = SUCCESS, None
    elif counted:
        outcome, cause = FAILURE, counted["source"]
    elif mem_err:
        outcome, cause = FAILURE, "enomem"
    elif errors:
        outcome, cause = UNCONFIRMED, "error"
    elif "START" not in f:
        outcome, cause = UNCONFIRMED, "no_log"
    elif stopped:
        outcome, cause = UNCONFIRMED, "timeout"
    elif "EXIT" in f:
        outcome, cause = UNCONFIRMED, "exited_without_ready"
    else:
        outcome, cause = UNCONFIRMED, "died_unrecorded"

    def ms(ev, key="ms"):
        return fval(f[ev].get(key), None) if ev in f else None
    t_fail = None
    if outcome == FAILURE:
        t_fail = counted["t_s"] if counted else mem_err["t_s"]
    return {"outcome": outcome, "cause": cause, "kill": counted, "kill_phase": kill_phase(f) if counted else None,
            "fail_phase": kill_phase(f) if outcome == FAILURE else None, "t_fail_s": t_fail,
            "dropped_kills": dropped, "error": errors[0]["msg"] if errors else "", "stopped": stopped,
            "times": {ev: f[ev]["t_s"] for ev in f},
            "fields": {"daily_ready_ms": ms("DAILY_READY"), "peak_hot_anon_ms": ms("PEAK_HOT_ANON"),
                       "peak_done_ms": ms("PEAK_DONE"), "startup_ready_ms": ms("STARTUP_READY"),
                       "peak_timeout": f["PEAK_DONE"].get("timeout") if "PEAK_DONE" in f else None}}


def parse_kernel_text(text, t0_unix):
    """oom-kill and drop_caches lines of `journalctl -k -o short-unix` (matching as run_round.kernel_log),
    keeping each source line."""
    kills, drops = [], []
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            continue
        if "oom-kill:" in line and "memlab.slice" in line:
            m, c = OOM_MEMCG.search(line), OOM_CONSTRAINT.search(line)
            kills.append({"t_unix": t, "t_s": round(t - t0_unix, 3), "app": m.group(1) if m else "?",
                          "constraint": c.group(1) if c else "?", "source": "kernel_oom", "line": line.strip()})
        elif "drop_caches" in line:
            drops.append({"t_unix": t, "t_s": round(t - t0_unix, 3), "line": line.strip()})
    return kills, drops


def assign_kernel_kills(kills, recs):
    """Case of each kernel kill: the case whose window holds it if the app is in that case, else the
    nearest case with that app (kernel timestamps are realtime and can be off by a little)."""
    bounds = [(r["id"], r.get("begin_s", 0.0), recs[i + 1].get("begin_s", math.inf) if i + 1 < len(recs) else math.inf, r)
              for i, r in enumerate(recs)]
    out = []
    for k in kills:
        t = k["t_s"]
        win = next((b for b in bounds if b[1] <= t < b[2]), None)
        cands = [b for b in bounds if k.get("app") in b[3]["order"]]
        if win is not None and win in cands:
            cid = win[0]
        elif cands:
            cid = min(cands, key=lambda b: min(abs(t - b[1]), abs(t - b[2])))[0]
        else:
            cid = win[0] if win else None
        out.append(dict(k, case=cid))
    return out


def case_phase(rec, t):
    """Where a case was at time t (phase column of popups and shadow counts)."""
    def reached(key):
        v = rec.get(key)
        return v is not None and t >= v
    if not reached("hint_s"):
        return "precase"
    if not reached("first_click_s"):
        return "lead"
    if not reached("last_click_s"):
        return "clicks"
    if not reached("first_start_s"):
        return "waiting"
    T = rec.get("T_s")
    if T is None or t < T:
        return "ramp"
    return "hold" if t < T + rec.get("hold_s", 0) else "tail"


COUNT_KEYS = ("category_total", "kills", "policy_kills", "kernel_oom", "popups", "startup_failures", "unique",
              "device_style", "shadow", "startup_timeouts")


def analyze(recs, logs, policy_kills, kernel_kills, popups, shadows):
    """Pure attribution: per-attempt outcomes, per-case counts, totals and event rows.
    recs: case records (id, order, front, adj, launch, starts, stopped, times); logs: {(case, app): events};
    policy_kills/popups/shadows carry their case; kernel kills are assigned here."""
    by_id = {r["id"]: r for r in recs}
    per, unmatched = defaultdict(list), []
    for k in list(policy_kills) + assign_kernel_kills(kernel_kills, recs):
        r = by_id.get(k.get("case"))
        if r is not None and k.get("app") in r["order"]:
            per[(r["id"], k["app"])].append(k)
        else:
            unmatched.append(k)    # e.g. the anchor unit: not an app attempt
    attempts, rows, events, dropped = [], [], [], []
    for r in recs:
        cid, atts = r["id"], []
        for pos, app in enumerate(r["order"], 1):
            launch = (r.get("launch") or {}).get(app)
            if launch is None:
                att = {"outcome": NOT_RUN, "cause": "not_clicked", "kill": None, "kill_phase": None, "fail_phase": None,
                       "t_fail_s": None, "dropped_kills": [], "error": "", "stopped": False, "times": {}, "fields": {}}
            else:
                att = attempt_outcome(logs.get((cid, app), []), per.get((cid, app), []),
                                      launched=launch.get("rc", 0) == 0, stopped=app in (r.get("stopped") or []))
            start = (r.get("starts") or {}).get(app) or {}
            done = att["times"].get("PEAK_DONE")
            att.update(case=cid, app=app, position=pos, adj=(r.get("adj") or {}).get(app), front=app == r.get("front"),
                       launch_s=(launch or {}).get("t_s"), launch_rc=(launch or {}).get("rc"),
                       planned_start_s=start.get("planned_s"), signal_s=start.get("signal_s"),
                       high_fixup=start.get("high_fixup"),
                       late_ms=round((done - r["T_s"]) * 1000) if done is not None and r.get("T_s") is not None else None)
            atts.append(att)
            dropped += [dict(k, case=cid) for k in att["dropped_kills"]]
        counted = [a["kill"] for a in atts if a["kill"]]
        pol = sum(1 for k in counted if k["source"] == "policy_kill")
        ker = len(counted) - pol
        sf = sum(1 for a in atts if a["outcome"] == FAILURE)
        enomem = sum(1 for a in atts if a["outcome"] == FAILURE and a["cause"] == "enomem")
        pops = [p for p in popups if p.get("case") == cid]
        shs = [s for s in shadows if s.get("case") == cid]
        lates = [a["late_ms"] for a in atts if a["late_ms"] is not None]
        rows.append({
            "id": cid, "popups": len(pops), "kills": pol + ker, "startup_failures": sf,
            "category_total": pol + ker + len(pops) + sf, "unique": pol + ker + len(pops) + enomem,
            "device_style": sf + len(pops) + ker, "shadow": len(shs),
            "max_some_1s_ms": None if r.get("max_some_1s_ms") is None else round(r["max_some_1s_ms"], 1),
            "precase_ok": r.get("precase_ok"), "barrier_wait_s": r.get("barrier_wait_s"),
            "unconfirmed": sum(1 for a in atts if a["outcome"] == UNCONFIRMED),
            "position": r.get("pos"), "policy_kills": pol, "kernel_oom": ker,
            "startup_timeouts": sum(1 for a in atts if a["outcome"] == UNCONFIRMED and a["cause"] in ("timeout", "exited_without_ready")),
            "not_run": sum(1 for a in atts if a["outcome"] == NOT_RUN), "n_apps": len(r["order"]),
            "front": r.get("front"), "order": " ".join(r["order"]), "rotation": r.get("rotation"),
            "begin_s": r.get("begin_s"), "hint_s": r.get("hint_s"), "first_click_s": r.get("first_click_s"),
            "barrier_s": r.get("barrier_s"), "first_start_s": r.get("first_start_s"),
            "T_planned_s": r.get("T_planned_s"), "T_s": r.get("T_s"), "end_s": r.get("end_s"),
            "duration_s": round(r["end_s"] - r["begin_s"], 2) if r.get("end_s") is not None else None,
            "precase_wait_s": r.get("precase_wait_s"), "hint_rc": (r.get("hint") or {}).get("rc"),
            "max_late_ms": max(lates) if lates else None, "stopped": " ".join(r.get("stopped") or []),
            "kill_phases": " ".join(a["kill_phase"] for a in atts if a["kill_phase"]),
        })
        for p in pops:
            events.append({"t_s": p["t_s"], "case": cid, "app": p.get("foreground") or "", "type": "popup", "level": "",
                           "phase": case_phase(r, p["t_s"]), "some_ms": "", "full_ms": "",
                           "source": f"available_mib={p.get('available_mib')} foreground={p.get('foreground')}"})
        for a in atts:
            k = a["kill"]
            if k:
                pk = k["source"] == "policy_kill"
                events.append({"t_s": k["t_s"], "case": cid, "app": a["app"], "type": k["source"],
                               "level": k.get("level") if pk else k.get("constraint", ""), "phase": a["kill_phase"],
                               "some_ms": k.get("some_ms", ""), "full_ms": k.get("full_ms", ""),
                               "source": (f"policy level {k.get('level')}: some_ms={k.get('some_ms')} full_ms={k.get('full_ms')} "
                                          f"adj={k.get('adj')}") if pk else k.get("line", "")})
            if a["outcome"] == FAILURE:
                events.append({"t_s": a["t_fail_s"], "case": cid, "app": a["app"], "type": "startup_failure",
                               "level": "" if not k else (k.get("level") if k["source"] == "policy_kill" else k.get("constraint", "")),
                               "phase": a["fail_phase"], "some_ms": (k or {}).get("some_ms", ""),
                               "full_ms": (k or {}).get("full_ms", ""),
                               "source": a["cause"] + (f": {a['error']}" if a["error"] and not k else "")})
        for s in shs:
            events.append({"t_s": s["t_s"], "case": cid, "app": "", "type": "shadow", "level": "",
                           "phase": case_phase(r, s["t_s"]), "some_ms": s.get("some_ms", ""), "full_ms": "",
                           "source": "shadow 1-s window"})
        attempts += atts
    events.sort(key=lambda e: (e["t_s"] if e["t_s"] is not None else 0.0))
    totals = {k: sum(row[k] for row in rows) for k in COUNT_KEYS}
    return {"attempts": attempts, "cases": rows, "events": events, "totals": totals,
            "types_present": types_present(totals),
            "unconfirmed": sum(row["unconfirmed"] for row in rows), "not_run": sum(row["not_run"] for row in rows),
            "dropped_kills": dropped, "unmatched_kills": unmatched}


# Design 0.1 (2026-10-24): an unconfirmed terminal state that apply may have caused (the app timed out or exited
# without STARTUP_READY) is apply's own problem: in an apply run it counts as a startup failure (against apply)
# and does not void the run. Launch failures, non-memory ERROR lines and missing logs are program or environment
# errors and void any run; in observe every unconfirmed state voids the run.
APPLY_COUNTED_CAUSES = ("timeout", "exited_without_ready", "died_unrecorded")


def apply_unconfirmed_policy(mode, res):
    """Mutates res (from analyze) for the run's mode; returns the number of unconfirmed states that still void it."""
    if mode != "apply":
        return res["unconfirmed"]
    left = 0
    for row in res["cases"]:
        mine = [a for a in res["attempts"] if a["case"] == row["id"] and a["outcome"] == UNCONFIRMED]
        counted = sum(1 for a in mine if a["cause"] in APPLY_COUNTED_CAUSES)
        left += len(mine) - counted
        row["unconfirmed_counted_as_failure"] = counted
        if counted:
            for k in ("startup_failures", "category_total", "unique", "device_style"):
                row[k] += counted
    res["totals"] = {k: sum(row[k] for row in res["cases"]) for k in COUNT_KEYS}
    res["types_present"] = types_present(res["totals"])
    res["unconfirmed_counted_as_failure"] = sum(row.get("unconfirmed_counted_as_failure", 0) for row in res["cases"])
    return left


def types_present(totals):
    return {"startup_failure": totals["startup_failures"] > 0, "popup": totals["popups"] > 0, "oom_kill": totals["kills"] > 0}


def validity_reasons(mode, totals, unconfirmed, v, error=None, extra=()):
    """Invalid run (spec §4): VM-level reclaim, a drop_caches line, observe without slice max events
    while there were popups/kills, a crash, unconfirmed terminal states (plus extra reasons)."""
    r = []
    if error:
        r.append(f"crash: {error}")
    if v.get("pgscan_kswapd_delta") is None or v.get("pgscan_direct_delta") is None:
        if not error:   # a run that crashed before its baseline is already invalid
            r.append("vmstat unreadable: VM-level reclaim cannot be ruled out")
    else:
        if v["pgscan_kswapd_delta"] > 0:
            r.append(f"vm_reclaim: pgscan_kswapd +{v['pgscan_kswapd_delta']}")
        if v["pgscan_direct_delta"] > 0:
            r.append(f"vm_reclaim: pgscan_direct +{v['pgscan_direct_delta']}")
    if v.get("drop_caches_lines"):
        r.append(f"drop_caches: {v['drop_caches_lines']} kernel log line(s)")
    if v.get("kernel_log_ok") is False:
        r.append("kernel log unreadable: kernel OOM kills and drop_caches unknown")
    if mode == "observe" and v.get("slice_max_events_delta") == 0 and totals.get("popups", 0) + totals.get("kills", 0) > 0:
        r.append("observe: slice memory.events max did not grow although cases had popups or kills")
    if unconfirmed:
        r.append(f"unconfirmed terminal states: {unconfirmed}")
    r.extend(extra)
    return r


def brake_seconds(rows, end_unix_ms=None):
    """Total brake-on time and number of on transitions from memshiftd's brake.csv."""
    on_since, total, n = None, 0.0, 0
    for row in sorted(rows, key=lambda x: fval(x.get("unix_ms"))):
        t = fval(row.get("unix_ms"), None)
        if t is None:
            continue
        on = str(row.get("state", "")).strip().lower() in ("on", "1", "true")
        if on and on_since is None:
            on_since, n = t, n + 1
        elif not on and on_since is not None:
            total, on_since = total + t - on_since, None
    if on_since is not None and end_unix_ms:
        total += max(0.0, end_unix_ms - on_since)
    return total / 1000, n


def memshiftd_summary(logdir, end_unix_ms=None):
    logdir = Path(logdir)
    rows = [r for r in safe_rows(logdir / "reclaim.csv") if r.get("status")]   # a cut last line has no status
    ok = [r for r in rows if r.get("status") == "ok"]
    obs = [r for r in rows if r.get("status") == "observe_only"]
    ev = safe_rows(logdir / "events.csv")
    errs = [r for r in ev if (r.get("level") or "") == "ERROR"]
    on_s, n_on = brake_seconds(safe_rows(logdir / "brake.csv"), end_unix_ms)
    return {"logs_present": (logdir / "events.csv").exists(), "rows": len(rows), "jobs": len(rows) - len(obs),
            "ok_jobs": len(ok), "observe_only": len(obs),
            "by_status": dict(Counter(r.get("status") or "" for r in rows)),
            "by_trigger": dict(Counter(r.get("trigger") or "" for r in rows)),
            "reclaimed_mib": round(-sum(fval(r.get("current_change")) for r in ok) / MiB, 1),
            "submitted_mib": round(sum(fval(r.get("submitted_bytes")) for r in ok) / MiB, 1),
            "planned_observe_mib": round(sum(fval(r.get("planned_bytes")) for r in obs) / MiB, 1),
            "pulses": len(safe_rows(logdir / "pulses.csv")), "errors": len(errs),
            "error_messages": [f"{r.get('app')}: {r.get('message')}" for r in errs[:10]],
            "warnings": sum(1 for r in ev if (r.get("level") or "") == "WARN"),
            "brake_on_s": round(on_s, 1), "brake_on_count": n_on}


def case_reclaim(rows, recs, t0_unix):
    """memshiftd jobs that finished ok between a case's start and its T (apply's preparation)."""
    out = {}
    if t0_unix is None:
        return out
    pts = [(fval(x.get("unix_ms"), None), x) for x in rows if x.get("status") == "ok"]
    pts = [(t / 1000 - t0_unix, x) for t, x in pts if t is not None]
    for r in recs:
        a, b = r.get("begin_s"), r.get("T_s")
        if a is None or b is None:
            continue
        sel = [x for t, x in pts if a <= t < b]
        out[r["id"]] = {"ok_jobs_before_T": len(sel),
                        "reclaimed_before_T_mib": round(-sum(fval(x.get("current_change")) for x in sel) / MiB, 1)}
    return out


# ---------------------------------------------------------------- monitors

class SlidingDelta:
    """Growth of a cumulative counter over the last window_s (PSI some total -> stall in the last second)."""

    def __init__(self, window_s=1.0):
        self.w, self.h = window_s, deque()

    def update(self, t, total):
        self.h.append((t, total))
        while self.h and t - self.h[0][0] > self.w:
            self.h.popleft()
        return max(0, self.h[-1][1] - self.h[0][1]) if len(self.h) > 1 else 0


class ShadowCounter:
    """Shadow rule (no kill): a 1-s window with some >= some_ms counts, at most once per cooldown_s."""

    def __init__(self, some_ms, window_s=1.0, cooldown_s=1.0):
        self.ms, self.cool, self.last, self.win = float(some_ms), cooldown_s, -1e18, SlidingDelta(window_s)

    def update(self, t, some_total_us):
        d = self.win.update(t, some_total_us) / 1000
        if d >= self.ms and t - self.last >= self.cool:
            self.last = t
            return d
        return None


PRESSURE_FIELDS = ["t_s", "case", "current_mib", "swap_mib", "some_total_us", "full_total_us", "zswap_mib",
                   "zswapped_mib", "avail_mib", "some_1s_ms", "alive"]


class Monitors:
    """Per-tick work of a real run: 10 Hz slice samples (pressure.csv), the shadow counter and the
    worst 1-s stall per case, run_round.PolicyKiller and run_round.PopupMonitor."""

    def __init__(self, ctx, path):
        sc = ctx.sc
        self.ctx = ctx
        self.killer = rr.PolicyKiller(sc["policy_kill"]) if sc.get("policy_kill") else None
        self.popup = rr.PopupMonitor(sc["popup"]) if sc.get("popup") else None
        self.shadow = ShadowCounter(sc["shadow_some_ms"]) if sc.get("shadow_some_ms") else None
        self.win = SlidingDelta(1.0)
        self.f = open(path, "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(PRESSURE_FIELDS)
        self.n = self.errors = 0

    def poll(self):
        try:
            self._poll()
        except Exception as e:  # noqa: BLE001 - a monitor glitch must not end a 2-h run
            self.errors += 1
            if self.errors <= 5 or self.errors % 100 == 0:
                self.ctx.warn(f"monitor error #{self.errors}: {type(e).__name__}: {e}")
            if self.errors > 300:
                raise RuntimeError(f"monitors failed {self.errors} times; last: {e}") from e

    def _poll(self):
        ctx = self.ctx
        b = rr.boottime()
        t = b - ctx.t0_boot
        rec = ctx.current
        cid = rec["id"] if rec else ""
        p = rr.psi(rr.read(rr.SCOPE / "memory.pressure"))
        st = rr.kv(rr.read(rr.SCOPE / "memory.stat"))
        some, full = p.get("some_total"), p.get("full_total")
        w1 = None
        if some is not None:
            w1 = self.win.update(t, some) / 1000
            if rec is not None:
                rec["max_some_1s_ms"] = max(rec.get("max_some_1s_ms") or 0.0, w1)
            if self.shadow:
                ms = self.shadow.update(t, some)
                if ms is not None:
                    ctx.shadows.append({"case": cid, "t_s": round(t, 3), "some_ms": round(ms, 1)})
        alive = rr.alive_apps(ctx.lru) if ctx.lru else set()
        avail = rr.scope_available()

        def mib(name):
            v = rr.read(rr.SCOPE / name).strip()
            return round(int(v) / MiB, 1) if v.isdigit() else ""
        self.w.writerow([round(t, 2), cid, mib("memory.current"), mib("memory.swap.current"),
                         "" if some is None else some, "" if full is None else full,
                         round(st.get("zswap", 0) / MiB, 1), round(st.get("zswapped", 0) / MiB, 1),
                         "" if avail is None else round(avail / MiB, 1), "" if w1 is None else round(w1, 1), len(alive)])
        self.n += 1
        if self.n % 10 == 0:
            self.f.flush()
        if self.killer:
            victim = self.killer.poll(b, list(ctx.lru), alive, dict(ctx.adj))
            if victim:
                k = self.killer.kills[-1]
                ctx.policy_kills.append({**k, "case": cid, "t_s": round(k["t"] - ctx.t0_boot, 3), "source": "policy_kill"})
        # Without the slice, rr.scope_available() reads memory.max as 0 and would report a popup.
        if self.popup and rr.SCOPE.exists():
            n = len(self.popup.popups)
            self.popup.poll(b, ctx.foreground)
            for pp in self.popup.popups[n:]:
                ctx.popups.append({**pp, "case": cid, "t_s": round(pp["t"] - ctx.t0_boot, 3)})
        if self.n % 50 == 0 and not rr.read(rr.app_cg("anchor") / "cgroup.procs").strip():
            ctx.warn("anchor unit is gone; restarting it")
            run_cmd(["systemctl", "--user", "reset-failed", rr.unit("anchor")], 15)
            run_cmd(anchor_cmd(), 30)

    def close(self):
        if not self.f.closed:
            self.f.close()


# ---------------------------------------------------------------- side effects: real and simulated

class RealOps:
    """Side effects of a real run: systemd user units, cgroup files, memshiftctl. Every command is bounded."""

    def __init__(self, build, control):
        self.build, self.control = Path(build), Path(control)
        self.start_clock()

    def start_clock(self):
        self.t0, self.t0_unix = rr.boottime(), time.time()

    def now(self):
        return rr.boottime() - self.t0

    def unix(self):
        return time.time()

    def sleep(self, s):
        if s > 0:
            time.sleep(s)

    def sh(self, cmd, timeout=30.0):
        return run_cmd(cmd, timeout)

    def ctl(self, args):
        rc, _, err = run_cmd([self.build / "memshiftctl", "--file", self.control] + list(args), 15)
        return rc, err

    def launch(self, app, cmd, info=None):
        u = rr.unit(app)
        run_cmd(["systemctl", "--user", "reset-failed", u], 15)
        rc, _, err = run_cmd(cmd, 30)
        if rc != 0:
            state = run_cmd(["systemctl", "--user", "is-active", u], 15)[1].strip()
            if state in ("active", "activating"):
                return 0, f"systemd-run rc {rc} but the unit is {state}: {err}"
            run_cmd(["systemctl", "--user", "reset-failed", u], 15)
            rc, _, err2 = run_cmd(cmd, 30)
            err += err2
        return rc, err

    def signal(self, apps, sig):
        # One call: systemctl kill signals every unit and reports the ones that failed.
        rc, _, err = run_cmd(["systemctl", "--user", "kill", "-s", sig] + [rr.unit(a) for a in apps], 15)
        return rc, err

    def stop(self, apps, block=False):
        cmd = ["systemctl", "--user", "stop"] + ([] if block else ["--no-block"]) + [rr.unit(a) for a in apps]
        return run_cmd(cmd, 60)[0]

    def kill(self, apps, sig="SIGKILL"):
        return self.signal(apps, sig)[0]

    def reset_failed(self, apps):
        run_cmd(["systemctl", "--user", "reset-failed"] + [rr.unit(a) for a in apps], 15)

    def alive(self, apps):
        return rr.alive_apps(apps)

    def read_log(self, path):
        return rr.read(path)

    def write_high(self, app, value):
        try:
            (rr.app_cg(app) / "memory.high").write_text(f"{value}\n")
            return True
        except OSError:
            return False

    def read_high(self, app):
        return rr.read(rr.app_cg(app) / "memory.high").strip()

    def precase_state(self, prev_data):
        def mib(name):
            v = rr.read(rr.SCOPE / name).strip()
            return round(int(v) / MiB, 1) if v.isdigit() else None
        text = rr.read(rr.SCOPE / "memory.stat")
        st = rr.kv(text)
        active = []
        try:
            for p in sorted(rr.SCOPE.glob("memlab-*.service")):
                if p.name != rr.unit("anchor") and rr.read(p / "cgroup.procs").strip():
                    active.append(p.name[len("memlab-"):-len(".service")])
        except OSError:
            pass
        return {"current_mib": mib("memory.current"), "swap_mib": mib("memory.swap.current"),
                "zswap_mib": round(st.get("zswap", 0) / MiB, 1) if text else None, "active": active,
                "prev_data_exists": bool(prev_data is not None and Path(prev_data).exists())}

    def exists(self, p):
        return Path(p).exists()

    def rmtree(self, p):
        shutil.rmtree(p, ignore_errors=True)

    def mkdir(self, p):
        Path(p).mkdir(parents=True, exist_ok=True)


class DryOps:
    """--dry-run and --self-test: prints (or records) every command and simulates the apps on a
    virtual clock (DAILY_READY 1 s after the click, peak on the start signal, exit after hold + exit).
    It never runs a command or touches a file."""

    def __init__(self, build, control, quiet=False):
        self.build, self.control, self.quiet = Path(build), Path(control), quiet
        self.t, self.unix0, self.lines, self.state, self.by_log = 0.0, time.time(), [], {}, {}

    def emit(self, text):
        line = f"[{self.t:9.2f} s] {text}"
        self.lines.append(line)
        if not self.quiet:
            print(line)

    def cmd(self, cmd):
        self.emit(shlex.join(str(c) for c in cmd))

    def start_clock(self):
        self.t0, self.t0_unix = 0.0, self.unix0

    def now(self):
        return self.t

    def unix(self):
        return self.unix0 + self.t

    def sleep(self, s):
        self.t += max(0.0, s)

    def sh(self, cmd, timeout=30.0):
        self.cmd(cmd)
        return 0, "", ""

    def ctl(self, args):
        self.cmd([self.build / "memshiftctl", "--file", self.control] + list(args))
        return 0, ""

    def launch(self, app, cmd, info=None):
        self.cmd(["systemctl", "--user", "reset-failed", rr.unit(app)])
        self.cmd(cmd)
        info = info or {}
        self.state[app] = {"log": str(info.get("log", "")), "launch": self.t, "ready": self.t + 1.0, "signal": None,
                           "stop": None, "ramp": info.get("ramp", 1.0), "hold": info.get("hold", 0.0),
                           "exit_after": info.get("exit_after", 0.0)}
        self.by_log[self.state[app]["log"]] = (app, self.state[app])   # logs of earlier cases stay readable
        return 0, ""

    def _times(self, s):
        """Simulated log of one app: [(t, event, fields)]."""
        ev = [(s["launch"], "START", "pid=1"), (s["ready"], "DAILY_READY", "ms=1000"), (s["ready"], "AWAIT_START", "")]
        end = s["stop"]
        if s["signal"] is not None:
            b = max(s["signal"], s["ready"])
            ev += [(b, "PEAK_BEGIN", ""), (b + 0.01, "PEAK_HOT_ANON", "ms=10"),
                   (b + s["ramp"], "PEAK_DONE", f"ms={s['ramp'] * 1000:.0f} timeout=0"),
                   (b + s["ramp"] + s["hold"], "PEAK_RELEASE", ""),
                   (b + s["ramp"] + s["hold"], "STARTUP_READY", f"ms={(b + s['ramp'] + s['hold'] - s['launch']) * 1000:.0f}")]
            natural = b + s["ramp"] + s["hold"] + s["exit_after"]
            end = natural if end is None else min(end, natural)
        if end is not None:
            ev = [e for e in ev if e[0] < end] + [(end, "EXIT", "")]
        return ev, end

    def alive(self, apps):
        out = set()
        for a in apps:
            s = self.state.get(a)
            if s is not None and self.t >= s["launch"]:
                end = self._times(s)[1]
                if end is None or self.t < end:
                    out.add(a)
        return out

    def signal(self, apps, sig):
        self.cmd(["systemctl", "--user", "kill", "-s", sig] + [rr.unit(a) for a in apps])
        for a in apps:
            s = self.state.get(a)
            if sig == "SIGUSR2" and s is not None and s["signal"] is None:
                s["signal"] = self.t
            elif sig == "SIGKILL" and s is not None:
                s["stop"] = self.t
        return 0, ""

    def stop(self, apps, block=False):
        self.cmd(["systemctl", "--user", "stop"] + ([] if block else ["--no-block"]) + [rr.unit(a) for a in apps])
        for a in apps:
            if a in self.state and a in self.alive([a]):
                self.state[a]["stop"] = self.t
        return 0

    def kill(self, apps, sig="SIGKILL"):
        return self.signal(apps, sig)[0]

    def reset_failed(self, apps):
        self.cmd(["systemctl", "--user", "reset-failed"] + [rr.unit(a) for a in apps])

    def read_log(self, path):
        if str(path) not in self.by_log:
            return ""
        a, s = self.by_log[str(path)]
        return "".join(f"{t:.3f} {a} {e} {x}".rstrip() + "\n" for t, e, x in self._times(s)[0] if t <= self.t)

    def write_high(self, app, value):
        self.emit(f"echo {value} > {rr.app_cg(app) / 'memory.high'}")
        return True

    def read_high(self, app):
        return "max"

    def precase_state(self, prev_data):
        return {"current_mib": 0.0, "swap_mib": 0.0, "zswap_mib": 0.0, "active": sorted(self.alive(list(self.state))),
                "prev_data_exists": False}

    def exists(self, p):
        return False

    def rmtree(self, p):
        self.cmd(["rm", "-rf", p])

    def mkdir(self, p):
        self.cmd(["mkdir", "-p", p])


# ---------------------------------------------------------------- the case state machine

class Ctx:
    """State shared by the case state machine, the monitors and the final analysis."""

    def __init__(self, sc, ops, rd, build, mode, hint, quiet=False):
        self.sc, self.ops, self.rd, self.build = sc, ops, Path(rd), Path(build)
        self.tl, self.apps = timeline(sc), app_table(sc)
        self.adj_bg, self.adj_front = adj_values(sc)
        self.mode, self.hint, self.quiet = mode, hint, quiet
        self.use_curves = not self.tl.get("ramp_cap_s")
        self.case_recs, self.current, self.prev_data = [], None, None
        self.lru, self.adj, self.foreground = [], {}, None
        self.policy_kills, self.popups, self.shadows, self.hints = [], [], [], []
        self.ctl_failures, self.warnings = [], []
        self.daemon, self.dlog = None, None
        self.t0_boot = self.t0_unix = None
        self.n_cases = self.n_done = 0
        self.progress_path, self.label = None, ""

    def ctl(self, args, what):
        rc, err = self.ops.ctl([str(a) for a in args])
        if rc != 0:
            self.ctl_failures.append({"t_s": round(self.ops.now(), 2), "what": what, "args": [str(a) for a in args],
                                      "rc": rc, "stderr": (err or "")[-300:]})
            self.warn(f"memshiftctl {what} failed (rc {rc}): {(err or '').strip()[-200:]}")
        return rc

    def warn(self, msg):
        if len(self.warnings) < 500:
            self.warnings.append({"t_s": round(self.ops.now(), 2), "msg": msg})
        if not self.quiet:
            print(f"WARN {msg}", file=sys.stderr, flush=True)

    def case_done(self, rec):
        self.n_done += 1
        pk = sum(1 for k in self.policy_kills if k.get("case") == rec["id"])
        pp = sum(1 for p in self.popups if p.get("case") == rec["id"])
        line = (f"{self.label}case {self.n_done}/{self.n_cases} {rec['id']}: hint {rec['hint_s']:.1f} s, clicks "
                f"{rec['first_click_s']:.1f}-{rec['last_click_s']:.1f} s, barrier +{rec['barrier_wait_s']:.1f} s, "
                f"T {rec['T_s']:.1f} s (planned {rec['T_planned_s']:.1f}), end {rec['end_s']:.1f} s "
                f"({rec['end_s'] - rec['begin_s']:.1f} s); live: popups {pp}, policy kills {pk}"
                + ("" if rec["precase_ok"] else ", PRECASE NOT OK") + (f", stopped {rec['stopped']}" if rec["stopped"] else ""))
        if not self.quiet:
            print(line, flush=True)
        self.write_progress(rec["id"])

    def write_progress(self, last_case):
        """progress.json after setup and after every case (the pipeline watches it for stalls)."""
        if self.progress_path is None:
            return
        try:
            write_json(self.progress_path, {"cases_done": self.n_done, "n_cases": self.n_cases, "last_case": last_case,
                                            "elapsed_s": round(self.ops.now(), 1), "live_popups": len(self.popups),
                                            "live_policy_kills": len(self.policy_kills), "updated_unix": time.time()})
        except OSError as e:
            self.warn(f"progress.json: {e}")


def wait_until(ops, t):
    """Yield ticks until t; the last fraction is slept so clicks and starts land on time."""
    while ops.now() < t - TICK_S:
        yield
    d = t - ops.now()
    if d > 0:
        ops.sleep(d)


def case_steps(ctx, pos, case, last):
    """One case as a generator: every yield hands one 0.1 s tick back to the main loop (which keeps
    the monitors running); every wait is bounded."""
    ops, tl = ctx.ops, ctx.tl
    cid, order = case["id"], list(case["order"])
    front = order[-1]
    ramps = {a: app_ramp(ctx.apps[a], tl) for a in order}
    rmax = max(ramps.values())
    rec = {"id": cid, "pos": pos, "order": order, "front": front, "rotation": case.get("rotation"),
           "adj": {a: (ctx.adj_front if a == front else ctx.adj_bg) for a in order}, "ramps": ramps,
           "hold_s": tl["hold_s"], "begin_s": round(ops.now(), 3), "launch": {}, "starts": {}, "stopped": [],
           "first_start_s": None, "max_some_1s_ms": None}
    ctx.case_recs.append(rec)
    ctx.current, ctx.lru, ctx.adj, ctx.foreground = rec, [], {}, None
    pre = tl["precase"]

    # 1. Pre-case check: no app unit left, previous data removed, slice drained (bounded wait).
    t_end = ops.now() + float(pre["wait_max_s"])
    while True:
        if ctx.prev_data is not None and ops.exists(ctx.prev_data):
            ops.rmtree(ctx.prev_data)
        st = ops.precase_state(ctx.prev_data)
        ok = precase_ok(st, pre)
        if ok or ops.now() >= t_end:
            break
        yield
    rec.update(precase_ok=ok, precase_wait_s=round(ops.now() - rec["begin_s"], 2), precase_state=st)
    if not ok and st.get("active"):
        # Leftover units would block this case's launches.
        ctx.warn(f"{cid}: stopping leftover units before the case: {st['active']}")
        ops.stop(st["active"], block=True)
        ops.reset_failed(st["active"])
        rec["precase_stopped"] = st["active"]

    # 2. Hint, hint_lead_s before the first click (the same timing without a hint).
    t_hint = ops.now()
    times = plan_times(tl, order, ramps, t_hint)
    rec.update(hint_s=round(t_hint, 3), first_click_s=round(times["first_click"], 3),
               last_click_s=round(times["clicks"][-1], 3), barrier_s=round(times["barrier"], 3),
               T_planned_s=round(times["T"], 3))
    if ctx.hint:
        h = hint_plan(ctx.apps, order, tl, t_hint, times["T"], ops.unix() + times["T"] - ops.now())
        rc = ctx.ctl(hint_args(h), "hint")
        rec["hint"] = dict(h, rc=rc)
        ctx.hints.append(dict(h, case=cid, t_s=round(t_hint, 3), rc=rc))
    yield from wait_until(ops, times["first_click"])

    # 3. Protect the front app; 4. click one app every click_gap_s.
    ctx.ctl(["--protect", front], "protect front")
    ops.mkdir(data_path(ctx.rd, cid))
    ops.mkdir(log_path(ctx.rd, cid, front).parent)
    ttl = app_ttl(tl, len(order), rmax)
    for i, app in enumerate(order):
        yield from wait_until(ops, times["clicks"][i])
        a = ctx.apps[app]
        log = log_path(ctx.rd, cid, app)
        cmd = launch_cmd(a, app, tl, log, data_path(ctx.rd, cid), zlib.crc32(app.encode()) % 100000 + pos,
                         rec["adj"][app], ttl, ctx.build,
                         curve_path(ctx.rd, app) if ctx.use_curves and a.get("peak_curve") else None)
        t = ops.now()
        rc, err = ops.launch(app, cmd, {"log": log, "ramp": ramps[app], "hold": tl["hold_s"],
                                        "exit_after": tl["exit_after_release_s"]})
        rec["launch"][app] = {"t_s": round(t, 3), "rc": rc, "adj": rec["adj"][app]}
        if rc != 0:
            ctx.warn(f"{cid}: launch of {app} failed (rc {rc}): {(err or '').strip()[-200:]}")
        elif err:
            ctx.warn(f"{cid}: launch of {app}: {err.strip()[-200:]}")
        ctx.lru.append(app)
        ctx.adj[app] = rec["adj"][app]
        ctx.foreground = app

    # 5. Barrier: every alive app has logged DAILY_READY (at most barrier_wait_max_s more).
    yield from wait_until(ops, times["barrier"])
    t_b = ops.now()
    while True:
        alive = ops.alive(order)
        waiting = [a for a in order if a in alive and "DAILY_READY" not in ops.read_log(log_path(ctx.rd, cid, a))]
        if not waiting or ops.now() - t_b >= float(tl["barrier_wait_max_s"]):
            break
        yield
    rec.update(barrier_release_s=round(ops.now(), 3), barrier_wait_s=round(ops.now() - t_b, 2), barrier_not_ready=waiting)

    # 6. Starts: every app reaches its peak at T (1-s ramp: all at once); protect, then signal.
    T = ops.now() + rmax
    rec["T_s"] = round(T, 3)
    groups = defaultdict(list)
    for a in order:
        groups[round(T - ramps[a], 3)].append(a)
    started = []
    for t_start in sorted(groups):
        names = groups[t_start]
        yield from wait_until(ops, t_start)
        alive = ops.alive(names)
        go = [a for a in names if a in alive]
        t_sig, rc, fixed = None, None, []
        if go:
            ctx.ctl(["--protect", ",".join(dict.fromkeys([front] + started + go))], "protect start")
            for a in go:
                ops.write_high(a, "max")
            ops.sleep(0.05)
            for a in go:
                v = ops.read_high(a)
                if v and v != "max":   # memshiftd lowered it in between: undo before the start
                    ops.write_high(a, "max")
                    fixed.append(a)
            t_sig = ops.now()
            rc, err = ops.signal(go, "SIGUSR2")
            if rc != 0:
                ctx.warn(f"{cid}: SIGUSR2 to {go} returned {rc}: {(err or '').strip()[-200:]}")
            started += go
            if rec["first_start_s"] is None:
                rec["first_start_s"] = round(t_sig, 3)
        for a in names:
            rec["starts"][a] = {"planned_s": t_start, "signal_s": round(t_sig, 3) if a in go else None,
                                "rc": rc if a in go else None, "high_fixup": a in fixed}

    # 7. Wait until every app exited or was killed (bounded); stop the rest (unconfirmed).
    deadline = T + tl["hold_s"] + tl["exit_after_release_s"] + tl["release_timeout_s"]
    rec["deadline_s"] = round(deadline, 3)
    while ops.alive(order) and ops.now() < deadline:
        yield
    left = sorted(ops.alive(order))
    if left:
        rec["stopped"] = left
        ctx.warn(f"{cid}: still running at the deadline, stopping {left}")
        ops.stop(left)
        t_stop = ops.now()
        while ops.alive(left) and ops.now() - t_stop < 15:
            yield
        still = sorted(ops.alive(left))
        if still:
            ops.kill(still, "SIGKILL")
            rec["stop_failed"] = still
    rec["done_s"] = round(ops.now(), 3)

    # 8. Clean up: unit state, the data files (their page cache), protection, hint.
    ops.reset_failed(order)
    ops.rmtree(data_path(ctx.rd, cid))
    ctx.prev_data = data_path(ctx.rd, cid)
    ctx.lru, ctx.adj = [], {}
    ctx.ctl(["--protect", "none"], "unprotect")
    if ctx.hint:
        ctx.ctl(["--boost-mb", "0"], "clear hint")
    rec["end_s"] = round(ops.now(), 3)
    ctx.case_done(rec)

    # 9. Gap: the next hint goes out gap - lead after this end.
    if not last:
        yield from wait_until(ops, ops.now() + max(0.0, tl["gap_before_next_case_s"] - tl["hint_lead_s"]))


def all_cases(ctx, selected):
    for i, (pos, case) in enumerate(selected):
        yield from case_steps(ctx, pos, case, last=i == len(selected) - 1)


def drive(ctx, gen, monitors=None, bound_s=None):
    """Main loop: one tick every 0.1 s runs the monitors and one step of the case state machine."""
    ops = ctx.ops
    next_tick = ops.now()
    while True:
        if monitors is not None:
            monitors.poll()
        try:
            next(gen)
        except StopIteration:
            return
        if ctx.daemon is not None and ctx.daemon.poll() is not None:
            raise RuntimeError(f"memshiftd exited during the run (code {ctx.daemon.returncode}); see memshiftd.stderr")
        if bound_s is not None and ops.now() > bound_s:
            raise RuntimeError(f"run exceeded its time bound of {bound_s:.0f} s")
        next_tick += TICK_S
        d = next_tick - ops.now()
        if d > 0:
            ops.sleep(d)
        else:
            next_tick = ops.now()


# ---------------------------------------------------------------- real run

def preflight_memlab(build, work):
    """The memlab_app binary must have the await-start contract: an old binary ignores unknown flags
    silently and would run a different experiment. Runs a tiny standalone process (no cgroup)."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    log = work / "preflight.log"
    log.unlink(missing_ok=True)
    cmd = [str(Path(build) / "memlab_app"), "--name", "preflight", "--log", str(log), "--anon-mib", "2",
           "--peak-mib", "4", "--peak-rate", "0", "--hold", "1", "--await-start", "1", "--exit-after-peak", "0",
           "--peak-hot-anon-mib", "1", "--peak-hot-anon-period", "1", "--fill-threads", "2", "--ttl", "30"]
    try:
        with open(work / "preflight.stderr", "w") as err:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
    except OSError as e:
        raise RuntimeError(f"cannot start {cmd[0]}: {e}") from e
    try:
        wait_for(lambda: "AWAIT_START" in rr.read(log) or p.poll() is not None, 10)
        if "AWAIT_START" not in rr.read(log):
            raise RuntimeError(f"memlab_app preflight: no AWAIT_START (binary without --await-start?); log: "
                               f"{rr.read(log)[-300:]!r}; stderr: {rr.read(work / 'preflight.stderr')[-200:]!r}")
        os.kill(p.pid, signal.SIGUSR2)
        p.wait(timeout=20)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("memlab_app preflight did not exit after its start signal") from e
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)
    names = [e["event"] for e in parse_log_text(rr.read(log))]
    need = ["START", "DAILY_READY", "AWAIT_START", "PEAK_BEGIN", "PEAK_DONE", "PEAK_RELEASE", "STARTUP_READY", "EXIT"]
    idx = [names.index(n) if n in names else -1 for n in need]
    if -1 in idx or idx != sorted(idx) or "PEAK_HOT_ANON" not in names or p.returncode != 0:
        raise RuntimeError(f"memlab_app preflight: unexpected log order {names} (rc {p.returncode}); rebuild memlab_app")


def snapshot():
    return {"vmstat": rr.kv(rr.read("/proc/vmstat")), "events": rr.kv(rr.read(rr.SCOPE / "memory.events")),
            "unix": time.time()}


def vm_pgscan(vm):
    def pick(name):
        if name in vm:
            return vm[name]
        parts = [v for k, v in vm.items() if k.startswith(name + "_") and not k.endswith("_throttle")]
        return sum(parts) if parts else None
    return pick("pgscan_kswapd"), pick("pgscan_direct")


def stop_daemon(p, timeout=60.0):
    if p is None:
        return None
    if p.poll() is None:
        try:
            p.send_signal(signal.SIGINT)
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=10)
    return p.returncode


def die_with_parent():
    """preexec_fn giving the child SIGTERM when the runner dies (PR_SET_PDEATHSIG): an orphaned
    memshiftd would otherwise keep pulsing memory.high during later runs."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return None
    return lambda: libc.prctl(1, int(signal.SIGTERM), 0, 0, 0)


def must(res, what):
    rc, err = res[0], res[-1]
    if rc != 0:
        raise RuntimeError(f"{what} failed (rc {rc}): {(err or '').strip()[-300:]}")


def setup_run(ctx, args, used):
    """Slice, anchor, memshiftd (rr.main-style). Raises on any failure."""
    ops, rd, sc = ctx.ops, ctx.rd, ctx.sc
    if not args.skip_preflight:
        preflight_memlab(ctx.build, rd / "preflight")
    rr.cleanup_units()
    scope = sc["scope"]
    props = [f"MemoryMax={scope['memory_max_mib']}M", f"MemorySwapMax={scope['swap_max_mib']}M"]
    if "zswap_max_mib" in scope:
        props.append(f"MemoryZSwapMax={scope['zswap_max_mib']}M")
    must(ops.sh(["systemctl", "--user", "set-property", "--runtime", "memlab.slice"] + props), "set memlab.slice properties")
    must(ops.sh(anchor_cmd()), "start the anchor unit")
    if not wait_for(lambda: rr.SCOPE.exists(), 10):
        raise RuntimeError(f"{rr.SCOPE} did not appear")
    if ctx.use_curves:
        for n in used:
            if ctx.apps[n].get("peak_curve"):
                curve_path(rd, n).parent.mkdir(parents=True, exist_ok=True)
                curve_path(rd, n).write_text(curve_text(ctx.apps[n]))
    (rd / "memshiftd.ini").write_text(memshiftd_ini(sc, rd, used))
    must(ops.ctl(["--init", "--protect", "none"]), "memshiftctl --init")
    if ctx.hint:
        # The hint needs --peak-at-unix; fail now rather than send dozens of broken hints.
        must(ops.ctl(["--boost-mb", "1", "--ttl", "1", "--peak-at-unix", str(int(time.time()) + 60)]),
             "memshiftctl --peak-at-unix preflight")
        must(ops.ctl(["--boost-mb", "0"]), "memshiftctl clear")
    ctx.dlog = open(rd / "memshiftd.stderr", "w")
    ctx.daemon = subprocess.Popen([str(ctx.build / "memshiftd"), "--config", str(rd / "memshiftd.ini"),
                                   "--apply" if ctx.mode == "apply" else "--observe"],
                                  stdout=ctx.dlog, stderr=subprocess.STDOUT, preexec_fn=die_with_parent())
    time.sleep(1.5)
    if ctx.daemon.poll() is not None:
        raise RuntimeError(f"memshiftd exited early (code {ctx.daemon.returncode}): {rr.read(rd / 'memshiftd.stderr')[-400:]}")


def finalize(ctx, args, sc_raw, selected, checks, base, monitors, error, tb):
    """Always runs: stop memshiftd, clean up, attribute events, write the outputs. Never raises."""
    rd, notes = ctx.rd, []

    def guard(what, fn, default=None):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            notes.append(f"{what}: {type(e).__name__}: {e}")
            return default
    ended = time.time()
    end = guard("end snapshot", snapshot, {}) or {}
    daemon_rc = guard("stop memshiftd", lambda: stop_daemon(ctx.daemon))
    if ctx.dlog is not None:
        guard("close memshiftd.stderr", ctx.dlog.close)
    if monitors is not None:
        guard("close pressure.csv", monitors.close)
    guard("clean up units", rr.cleanup_units)
    krc, kout, kerr = guard("kernel log", lambda: run_cmd(
        ["journalctl", "-k", f"--since=@{int(ctx.t0_unix) - 1}", "-o", "short-unix", "--no-pager"], 120),
        (1, "", "")) or (1, "", "")
    kkills, drops = parse_kernel_text(kout, ctx.t0_unix)
    guard("remove data", lambda: shutil.rmtree(rd / "data", ignore_errors=True))
    tag = variant_tag(args.mode, ctx.hint)
    summary = {"scenario": ctx.sc.get("name"), "sequence": ctx.sc.get("sequence"),
               "sequence_sha256": ctx.sc.get("sequence_sha256"), "tuning_sha256": ctx.sc.get("tuning_sha256"),
               "scenario_sha256": hashlib.sha256(sc_raw).hexdigest(), "scenario_path": str(Path(args.scenario).resolve()),
               "case_set": args.case_set, "case_ids": [c["id"] for _, c in selected], "first": args.first,
               "mode": args.mode, "hint": ctx.hint, "tag": tag, "label": args.label, "run_id": args.run_id,
               "run_dir": str(rd), "n_cases": len(selected), "checks": checks["source"], "acceptance_checks": checks,
               "valid": False, "invalid_reasons": [], "error": error}
    try:
        logs = {(r["id"], a): parse_log_text(rr.read(log_path(rd, r["id"], a)), ctx.t0_boot)
                for r in ctx.case_recs for a in r["order"]}
        res = analyze(ctx.case_recs, logs, ctx.policy_kills, kkills, ctx.popups, ctx.shadows)
        msd = memshiftd_summary(rd / "memshiftd", ended * 1000)
        msd.update(exit_code=daemon_rc, stderr_tail=rr.read(rd / "memshiftd.stderr")[-600:])
        per_case = case_reclaim(safe_rows(rd / "memshiftd" / "reclaim.csv"), ctx.case_recs, ctx.t0_unix)
        for row in res["cases"]:
            row.update(per_case.get(row["id"], {}))
        k0, d0 = vm_pgscan((base or {}).get("vmstat", {}))
        k1, d1 = vm_pgscan(end.get("vmstat", {}))
        ev0, ev1 = (base or {}).get("events", {}), end.get("events", {})
        validity = {"pgscan_kswapd_delta": None if None in (k0, k1) else k1 - k0,
                    "pgscan_direct_delta": None if None in (d0, d1) else d1 - d0,
                    "drop_caches_lines": len(drops), "drop_caches_t_s": [d["t_s"] for d in drops],
                    "slice_max_events_delta": ev1["max"] - ev0["max"] if "max" in ev0 and "max" in ev1 else None,
                    "slice_events_delta": {k: ev1[k] - ev0[k] for k in ev0 if k in ev1},
                    "kernel_log_ok": krc == 0, "precase_not_ok": sum(1 for r in ctx.case_recs if r.get("precase_ok") is False),
                    "memfree_mib_at_start": getattr(ctx, "memfree_start", None)}
        completed = [r["id"] for r in ctx.case_recs if r.get("end_s") is not None]
        extra = []
        if len(completed) < len(selected):
            extra.append(f"incomplete: {len(completed)}/{len(selected)} cases finished")
        if ctx.ctl_failures:
            extra.append(f"memshiftctl failed {len(ctx.ctl_failures)} time(s)")
        voiding = apply_unconfirmed_policy(args.mode, res)
        reasons = validity_reasons(args.mode, res["totals"], voiding, validity, error, extra)
        run_s = (max((r["end_s"] for r in ctx.case_recs if r.get("end_s") is not None), default=0.0))
        per_case_s = {r["id"]: round(r["end_s"] - r["begin_s"], 1) for r in ctx.case_recs if r.get("end_s") is not None}
        summary.update({
            "case_ids_completed": completed, "n_cases_completed": len(completed),
            "totals": res["totals"], "types_present": res["types_present"], "unconfirmed": res["unconfirmed"],
            "unconfirmed_counted_as_failure": res.get("unconfirmed_counted_as_failure", 0),
            "valid": not reasons, "invalid_reasons": reasons, "validity": validity, "memshiftd": msd,
            "durations": {"run_s": round(run_s, 1), "wall_s": round(ended - ctx.t0_unix, 1), "per_case_s": per_case_s,
                          "mean_case_s": round(sum(per_case_s.values()) / len(per_case_s), 1) if per_case_s else None},
            "traceback": (tb or "")[-3000:] or None, "warnings": ctx.warnings, "finalize_notes": notes,
            "hints": ctx.hints, "ctl_failures": ctx.ctl_failures, "dropped_kills": res["dropped_kills"],
            "unmatched_kills": res["unmatched_kills"], "timeline": ctx.tl, "policy_kill": ctx.sc.get("policy_kill"),
            "popup": ctx.sc.get("popup"), "shadow_some_ms": ctx.sc.get("shadow_some_ms"),
            "started_unix": ctx.t0_unix, "ended_unix": ended,
            "cases": [dict(row, apps=[{"app": a["app"], "outcome": a["outcome"], "cause": a["cause"],
                                       "kill_source": (a["kill"] or {}).get("source"), "kill_phase": a["kill_phase"],
                                       "late_ms": a["late_ms"]} for a in res["attempts"] if a["case"] == row["id"]])
                      for row in res["cases"]],
        })
        write_csv(rd / "events.csv", ["t_s", "case", "app", "type", "level", "phase", "some_ms", "full_ms", "source"],
                  res["events"])
        write_csv(rd / "cases.csv", CASE_FIELDS, res["cases"])
        write_csv(rd / "attempts.csv", ATTEMPT_FIELDS, [attempt_row(a) for a in res["attempts"]])
    except Exception as e:  # noqa: BLE001 - still leave a summary behind
        summary.update(valid=False, invalid_reasons=summary.get("invalid_reasons", []) + [f"analysis failed: {type(e).__name__}: {e}"],
                       analysis_traceback=traceback.format_exc()[-3000:], finalize_notes=notes, traceback=(tb or "")[-3000:] or None)
    try:
        write_json(rd / "summary.json", summary)
    except OSError as e:
        print(f"ERROR: cannot write {rd / 'summary.json'}: {e}", file=sys.stderr)
    return summary


CASE_FIELDS = ["id", "popups", "kills", "startup_failures", "category_total", "unique", "device_style", "shadow",
               "max_some_1s_ms", "precase_ok", "barrier_wait_s", "unconfirmed", "position", "policy_kills", "kernel_oom",
               "startup_timeouts", "not_run", "n_apps", "front", "order", "rotation", "begin_s", "hint_s", "first_click_s",
               "barrier_s", "first_start_s", "T_planned_s", "T_s", "end_s", "duration_s", "precase_wait_s", "hint_rc",
               "max_late_ms", "kill_phases", "stopped", "ok_jobs_before_T", "reclaimed_before_T_mib",
               "unconfirmed_counted_as_failure"]
ATTEMPT_FIELDS = ["case", "position", "app", "adj", "front", "outcome", "cause", "kill_source", "kill_level", "kill_phase",
                  "kill_s", "fail_phase", "t_fail_s", "error", "launch_s", "launch_rc", "planned_start_s", "signal_s",
                  "start_s", "daily_ready_s", "daily_ready_ms", "await_start_s", "peak_begin_s", "peak_hot_anon_s",
                  "peak_hot_anon_ms", "peak_done_s", "peak_done_ms", "peak_timeout", "peak_release_s", "startup_ready_s",
                  "startup_ready_ms", "exit_s", "late_ms", "stopped_by_runner", "high_fixup", "dropped_kills"]


def attempt_row(a):
    t, f, k = a.get("times", {}), a.get("fields", {}), a.get("kill") or {}
    return {"case": a["case"], "position": a["position"], "app": a["app"], "adj": a["adj"], "front": a["front"],
            "outcome": a["outcome"], "cause": a.get("cause"), "kill_source": k.get("source"),
            "kill_level": k.get("level") if k.get("source") == "policy_kill" else k.get("constraint"),
            "kill_phase": a.get("kill_phase"), "kill_s": k.get("t_s"), "fail_phase": a.get("fail_phase"),
            "t_fail_s": a.get("t_fail_s"), "error": a.get("error"), "launch_s": a.get("launch_s"),
            "launch_rc": a.get("launch_rc"), "planned_start_s": a.get("planned_start_s"), "signal_s": a.get("signal_s"),
            "start_s": t.get("START"), "daily_ready_s": t.get("DAILY_READY"), "daily_ready_ms": f.get("daily_ready_ms"),
            "await_start_s": t.get("AWAIT_START"), "peak_begin_s": t.get("PEAK_BEGIN"),
            "peak_hot_anon_s": t.get("PEAK_HOT_ANON"), "peak_hot_anon_ms": f.get("peak_hot_anon_ms"),
            "peak_done_s": t.get("PEAK_DONE"), "peak_done_ms": f.get("peak_done_ms"), "peak_timeout": f.get("peak_timeout"),
            "peak_release_s": t.get("PEAK_RELEASE"), "startup_ready_s": t.get("STARTUP_READY"),
            "startup_ready_ms": f.get("startup_ready_ms"), "exit_s": t.get("EXIT"), "late_ms": a.get("late_ms"),
            "stopped_by_runner": a.get("stopped", False), "high_fixup": a.get("high_fixup"),
            "dropped_kills": len(a.get("dropped_kills", []))}


def guards_ok(args, checks, dry=False):
    """Pre-run guards; returns an exit code or None. In a dry run they only print."""
    if checks["refuse"]:
        print(f"REFUSE (exit {EXIT_CHECKS}): calibrated acceptance checks fail: "
              f"{ {k: checks[k] for k in ('single_peak_le_M', 'all_daily_le_M', 'case_daily_le_M', 'same_moment_ge_1_2M')} }, "
              f"failing cases {checks['failing_cases']}", file=sys.stderr)
        if not dry:
            return EXIT_CHECKS
    mf = memfree_mib()
    if mf is not None and mf < MEMFREE_MIN_GIB * 1024 and not args.allow_low_memfree:
        print(f"{'WOULD REFUSE' if dry else 'REFUSE'} (exit {EXIT_MEMFREE}): MemFree {mf / 1024:.1f} GiB < "
              f"{MEMFREE_MIN_GIB} GiB (use --allow-low-memfree to run anyway)", file=sys.stderr)
        if not dry:
            return EXIT_MEMFREE
    for problem in disk_problems():
        print(f"{'WOULD REFUSE' if dry else 'REFUSE'} (exit {EXIT_ENV}): {problem}", file=sys.stderr)
        if not dry:
            return EXIT_ENV
    return None


def move_aside(path):
    path = Path(path)
    if path.exists():
        aside = path.with_name(f"{path.name}.old{int(time.time())}")
        path.rename(aside)
        print(f"moved the existing {path} to {aside}", file=sys.stderr)


def cmd_run(args, sc, sc_raw, selected, checks, build):
    hint = args.hint == "on"
    tag = variant_tag(args.mode, hint)
    code = guards_ok(args, checks)
    if code is not None:
        return code
    lock, lock_path = acquire_lock()
    if lock is None:
        print(f"REFUSE (exit {EXIT_ENV}): another run holds {lock_path}", file=sys.stderr)
        return EXIT_ENV
    try:
        rd = Path(args.out) / f"{args.label}_{tag}_{args.run_id}"
        try:
            move_aside(rd)
            (rd / "apps").mkdir(parents=True)
            (rd / "data").mkdir()
            write_json(rd / "scenario.json", sc)
            write_json(rd / "args.json", {**vars(args), "tag": tag, "build": str(build),
                                          "case_ids": [c["id"] for _, c in selected]})
        except OSError as e:
            print(f"REFUSE (exit {EXIT_ENV}): cannot create the run directory {rd}: {e}", file=sys.stderr)
            return EXIT_ENV
        rr.sh = bounded_rr_sh
        ops = RealOps(build, rd / "control.conf")
        ctx = Ctx(sc, ops, rd, build, args.mode, hint)
        ctx.n_cases, ctx.progress_path = len(selected), rd / "progress.json"
        ctx.label = f"[{rd.name}] "
        ctx.memfree_start = memfree_mib()
        ctx.t0_boot, ctx.t0_unix = ops.t0, ops.t0_unix
        used = apps_used([c for _, c in selected])
        bound = sum(case_bound_s(ctx.tl, len(c["order"]), max(app_ramp(ctx.apps[a], ctx.tl) for a in c["order"]))
                    for _, c in selected) + 300
        base, monitors, error, tb = None, None, None, None
        old_term = signal.signal(signal.SIGTERM, _raise_terminated)
        try:
            setup_run(ctx, args, used)
            base = snapshot()
            ops.start_clock()
            ctx.t0_boot, ctx.t0_unix = ops.t0, ops.t0_unix
            monitors = Monitors(ctx, rd / "pressure.csv")
            ctx.write_progress(None)
            print(f"{ctx.label}{len(selected)} cases, mode {args.mode}, hint {args.hint}, bound {bound / 3600:.2f} h",
                  flush=True)
            drive(ctx, all_cases(ctx, selected), monitors, bound)
        except BaseException as e:  # noqa: BLE001 - includes SIGTERM/Ctrl-C: always clean up
            error, tb = describe(e), traceback.format_exc()
            print(f"ERROR during the run: {error}", file=sys.stderr, flush=True)
        finally:
            # Clean-up and outputs are bounded; a second SIGINT/SIGTERM must not cut them short.
            old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            summary = finalize(ctx, args, sc_raw, selected, checks, base, monitors, error, tb)
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
        print(json.dumps({"run_dir": str(rd), "valid": summary.get("valid"), "totals": summary.get("totals"),
                          "invalid_reasons": summary.get("invalid_reasons")}, ensure_ascii=False), flush=True)
        return EXIT_OK if error is None else EXIT_ERROR
    finally:
        if lock not in (None, -1):
            os.close(lock)


def cmd_dry_run(args, sc, selected, checks, build):
    """Prints the guards, the setup, every command of every case on a simulated clock, and the schedule."""
    hint = args.hint == "on"
    tag = variant_tag(args.mode, hint)
    rd = Path(args.out or "OUT") / f"{args.label or 'LABEL'}_{tag}_{args.run_id or 'RUN'}"
    tl = timeline(sc)
    print(f"# DRY RUN: nothing is executed. scenario {sc.get('name')} sequence {sc.get('sequence')} "
          f"({sc.get('sequence_sha256')}), {args.case_set} {selected[0][0]}..{selected[-1][0]} ({len(selected)} cases), "
          f"mode {args.mode}, hint {args.hint}, tag {tag}")
    print(f"# run dir {rd}")
    print(f"# acceptance checks ({checks['source']}): ok={checks['ok']} refuse={checks['refuse']} "
          f"max single peak {checks['max_single_peak_mib']} MiB ({checks['max_single_peak_app']}), "
          f"daily of all apps used {checks['all_daily_sum_mib']} MiB, failing cases {checks['failing_cases']}")
    guards_ok(args, checks, dry=True)
    mf = memfree_mib()
    print(f"# MemFree {mf / 1024:.1f} GiB" if mf is not None else "# MemFree unknown")
    ops = DryOps(build, rd / "control.conf")
    ctx = Ctx(sc, ops, rd, build, args.mode, hint)
    ctx.n_cases = len(selected)
    used = apps_used([c for _, c in selected])
    print("# --- setup")
    if not args.skip_preflight:
        ops.emit(f"preflight: {build / 'memlab_app'} --await-start 1 ... (standalone, checks the log order)")
    for c in cleanup_cmds():
        ops.cmd(c)
    scope = sc["scope"]
    props = [f"MemoryMax={scope['memory_max_mib']}M", f"MemorySwapMax={scope['swap_max_mib']}M"] + \
        ([f"MemoryZSwapMax={scope['zswap_max_mib']}M"] if "zswap_max_mib" in scope else [])
    ops.cmd(["systemctl", "--user", "set-property", "--runtime", "memlab.slice"] + props)
    ops.cmd(anchor_cmd())
    if ctx.use_curves:
        for n in used:
            if ctx.apps[n].get("peak_curve"):
                ops.emit(f"write {curve_path(rd, n)} ({len(ctx.apps[n]['peak_curve'])} points)")
    ops.emit(f"write {rd / 'memshiftd.ini'}:")
    for line in memshiftd_ini(sc, rd, used).splitlines():
        print(f"    {line}")
    ops.ctl(["--init", "--protect", "none"])
    if hint:
        ops.ctl(["--boost-mb", "1", "--ttl", "1", "--peak-at-unix", "<now+60>"])
        ops.ctl(["--boost-mb", "0"])
    ops.cmd([build / "memshiftd", "--config", rd / "memshiftd.ini", "--apply" if args.mode == "apply" else "--observe"])
    ops.emit("main loop every 0.1 s: pressure.csv sample, shadow counter, PolicyKiller, PopupMonitor")
    print("# --- cases (simulated: DAILY_READY 1 s after the click, every app on time)")
    ops.start_clock()
    ctx.t0_boot, ctx.t0_unix = 0.0, ops.unix0
    drive(ctx, all_cases(ctx, selected))
    print("# --- teardown")
    ops.emit("kill -INT memshiftd; wait up to 60 s")
    for c in cleanup_cmds():
        ops.cmd(c)
    ops.cmd(["journalctl", "-k", "--since=@<t0-1>", "-o", "short-unix", "--no-pager"])
    ops.cmd(["rm", "-rf", rd / "data"])
    ops.emit("write events.csv cases.csv attempts.csv summary.json")
    print("# --- schedule (s from the run start)")
    print(f"{'pos':>4} {'case':<10} {'n':>2} {'front':<11} {'hint':>8} {'click1':>8} {'barrier':>8} {'T':>8} {'end':>8} {'len':>6}")
    for r in ctx.case_recs:
        print(f"{r['pos']:>4} {r['id']:<10} {len(r['order']):>2} {r['front']:<11} {r['hint_s']:>8.1f} {r['first_click_s']:>8.1f} "
              f"{r['barrier_s']:>8.1f} {r['T_s']:>8.1f} {r['end_s']:>8.1f} {r['end_s'] - r['begin_s']:>6.1f}")
    total = ctx.case_recs[-1]["end_s"] if ctx.case_recs else 0
    bound = sum(case_bound_s(tl, len(c["order"]), max(app_ramp(ctx.apps[a], tl) for a in c["order"])) for _, c in selected)
    print(f"# simulated run length {total:.0f} s = {total / 3600:.2f} h (+ setup ~5 s); worst case with every wait "
          f"at its bound {bound / 3600:.2f} h")
    return EXIT_CHECKS if checks["refuse"] else EXIT_OK


# ---------------------------------------------------------------- calibration

def calibrate_app(ops, name, a, tl, work, build, adj):
    """One app alone in memlab.slice with the run's launch args: daily = slice memory.current growth
    0.5 s after DAILY_READY; peak = the app cgroup's memory.peak (fallback: max memory.current, 10 Hz)."""
    work = Path(work)
    log, data = log_path(work, "cal", name), work / "data" / name
    data.mkdir(parents=True, exist_ok=True)
    curve = None
    if not tl.get("ramp_cap_s") and a.get("peak_curve"):
        curve = curve_path(work, name)
        curve.parent.mkdir(parents=True, exist_ok=True)
        curve.write_text(curve_text(a))
    ramp = app_ramp(a, tl)
    ttl = int(min(3600, math.ceil(ramp + tl["hold_s"] + tl["exit_after_release_s"] + 360)))
    cmd = launch_cmd(a, name, tl, log, data, zlib.crc32(name.encode()) % 100000, adj, ttl, build, curve)
    cg = rr.app_cg(name)

    def num(path):
        v = rr.read(path).strip()
        return int(v) if v.isdigit() else None
    base = num(rr.SCOPE / "memory.current") or 0
    res = {"daily_mib": None, "peak_mib": None, "startup_ms": None, "app_daily_mib": None, "slice_daily_mib": None,
           "peak_done_ms": None, "peak_timeout": None, "slice_peak_delta_mib": None,
           "nominal_daily_mib": rr.daily_mib(a), "nominal_peak_mib": rr.daily_mib(a) + (a.get("peak_mib") or 0),
           "ok": False, "error": None}
    peak = {"memory_peak": 0, "current": 0, "slice": 0}

    def sample():
        cur, pk, sl = num(cg / "memory.current"), num(cg / "memory.peak"), num(rr.SCOPE / "memory.current")
        peak["current"] = max(peak["current"], cur or 0)
        peak["memory_peak"] = max(peak["memory_peak"], pk or 0)
        if sl is not None:
            peak["slice"] = max(peak["slice"], sl - base)
        return cur, sl

    def alive():
        return bool(rr.read(cg / "cgroup.procs").strip())

    def wait(cond, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            sample()
            if cond():
                return True
            time.sleep(0.1)
        return cond()
    rc, err = ops.launch(name, cmd)
    if rc != 0:
        res["error"] = f"launch failed (rc {rc}): {(err or '').strip()[-200:]}"
        return res
    try:
        wait(lambda: "DAILY_READY" in rr.read(log) or not alive(), 120)
        if "DAILY_READY" not in rr.read(log):
            res["error"] = "no DAILY_READY"
            return res
        t_ready = time.monotonic()
        wait(lambda: time.monotonic() - t_ready >= 0.5, 2)
        cur, sl = sample()
        res["app_daily_mib"] = round(cur / MiB) if cur else None
        res["slice_daily_mib"] = round((sl - base) / MiB) if sl is not None else None
        ops.signal([name], "SIGUSR2")
        wait(lambda: "STARTUP_READY" in rr.read(log) or not alive(), ramp + tl["hold_s"] + 120)
        wait(lambda: not alive(), tl["exit_after_release_s"] + 20)
    finally:
        if alive():
            ops.stop([name], block=True)
            wait(lambda: not alive(), 15)
        ops.reset_failed([name])
        shutil.rmtree(data, ignore_errors=True)
    evs = parse_log_text(rr.read(log))
    f = first_events(evs)
    errors = [e["msg"] for e in evs if e["event"] == "ERROR"]
    res["startup_ms"] = fval(f["STARTUP_READY"].get("ms"), None) if "STARTUP_READY" in f else None
    res["peak_done_ms"] = fval(f["PEAK_DONE"].get("ms"), None) if "PEAK_DONE" in f else None
    res["peak_timeout"] = f["PEAK_DONE"].get("timeout") if "PEAK_DONE" in f else None
    res["slice_peak_delta_mib"] = round(peak["slice"] / MiB)
    res["ok"] = "STARTUP_READY" in f and not errors and ("PEAK_DONE" in f or not a.get("peak_mib"))
    if not res["ok"]:
        res["error"] = "; ".join(errors) or f"incomplete log: {[e['event'] for e in evs]}"
        return res
    daily = res["slice_daily_mib"] if res["slice_daily_mib"] is not None else res["app_daily_mib"]
    res["daily_mib"] = daily
    res["peak_mib"] = round(max(peak["memory_peak"], peak["current"]) / MiB)
    return res


def cmd_calibrate(args, sc, build, dry=False):
    tl, apps = timeline(sc), app_table(sc)
    adj = adj_values(sc)[0]
    out = Path(args.out)
    work = out.with_suffix(".work")
    if dry:
        ops = DryOps(build, work / "control.conf")
        print(f"# DRY RUN calibration of {len(apps)} apps -> {out} (work dir {work}); nothing is executed")
        guards_ok(args, {"refuse": False}, dry=True)
        if not args.skip_preflight:
            ops.emit(f"preflight: {build / 'memlab_app'} --await-start 1 ... (standalone, checks the log order)")
        for c in cleanup_cmds():
            ops.cmd(c)
        scope = sc["scope"]
        ops.cmd(["systemctl", "--user", "set-property", "--runtime", "memlab.slice",
                 f"MemoryMax={scope['memory_max_mib']}M", f"MemorySwapMax={scope['swap_max_mib']}M"] +
                ([f"MemoryZSwapMax={scope['zswap_max_mib']}M"] if "zswap_max_mib" in scope else []))
        ops.cmd(anchor_cmd())
        for name, a in apps.items():
            ramp = app_ramp(a, tl)
            ops.cmd(launch_cmd(a, name, tl, log_path(work, "cal", name), work / "data" / name,
                               zlib.crc32(name.encode()) % 100000, adj,
                               int(min(3600, math.ceil(ramp + tl["hold_s"] + tl["exit_after_release_s"] + 360))), build,
                               curve_path(work, name) if not tl.get("ramp_cap_s") and a.get("peak_curve") else None))
            ops.emit(f"wait DAILY_READY (<= 120 s), +0.5 s: daily = slice memory.current growth; then:")
            ops.signal([name], "SIGUSR2")
            ops.emit(f"wait STARTUP_READY (<= {ramp + tl['hold_s'] + 120:.0f} s) and exit; peak = {rr.app_cg(name)}/memory.peak")
            ops.reset_failed([name])
            ops.cmd(["rm", "-rf", work / "data" / name])
            ops.sleep(ramp + tl["hold_s"] + tl["exit_after_release_s"] + 2)
        for c in cleanup_cmds():
            ops.cmd(c)
        ops.emit(f"write {out} and {work / 'calibration_meta.json'}")
        return EXIT_OK
    mf = memfree_mib()
    if mf is not None and mf < MEMFREE_MIN_GIB * 1024 and not args.allow_low_memfree:
        print(f"REFUSE (exit {EXIT_MEMFREE}): MemFree {mf / 1024:.1f} GiB < {MEMFREE_MIN_GIB} GiB", file=sys.stderr)
        return EXIT_MEMFREE
    for problem in disk_problems():
        print(f"REFUSE (exit {EXIT_ENV}): {problem}", file=sys.stderr)
        return EXIT_ENV
    lock, lock_path = acquire_lock()
    if lock is None:
        print(f"REFUSE (exit {EXIT_ENV}): another run holds {lock_path}", file=sys.stderr)
        return EXIT_ENV
    results, error = {}, None
    old_term = signal.signal(signal.SIGTERM, _raise_terminated)
    try:
        move_aside(work)
        (work / "apps" / "cal").mkdir(parents=True)
        rr.sh = bounded_rr_sh
        ops = RealOps(build, work / "control.conf")
        if not args.skip_preflight:
            preflight_memlab(build, work / "preflight")
        rr.cleanup_units()
        scope = sc["scope"]
        props = [f"MemoryMax={scope['memory_max_mib']}M", f"MemorySwapMax={scope['swap_max_mib']}M"]
        if "zswap_max_mib" in scope:
            props.append(f"MemoryZSwapMax={scope['zswap_max_mib']}M")
        must(ops.sh(["systemctl", "--user", "set-property", "--runtime", "memlab.slice"] + props), "set memlab.slice properties")
        must(ops.sh(anchor_cmd()), "start the anchor unit")
        if not wait_for(lambda: rr.SCOPE.exists(), 10):
            raise RuntimeError(f"{rr.SCOPE} did not appear")
        for name, a in apps.items():
            results[name] = calibrate_app(ops, name, a, tl, work, build, adj)
            print(json.dumps({name: results[name]}, ensure_ascii=False), flush=True)
    except BaseException as e:  # noqa: BLE001
        error = describe(e)
        print(f"ERROR during calibration: {error}", file=sys.stderr, flush=True)
    finally:
        old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            rr.cleanup_units()
        except Exception as e:  # noqa: BLE001
            print(f"cleanup failed: {e}", file=sys.stderr)
        shutil.rmtree(work / "data", ignore_errors=True)
        try:
            write_json(out, results)
            write_json(work / "calibration_meta.json", {"error": error, "scenario": sc.get("name"), "build": str(build),
                                                         "timeline": tl, "apps": list(apps), "finished_unix": time.time()})
        except OSError as e:
            print(f"ERROR: cannot write {out}: {e}", file=sys.stderr)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        if lock not in (None, -1):
            os.close(lock)
    ok = error is None and len(results) == len(apps) and all(r.get("ok") for r in results.values())
    if not ok:
        print(f"calibration incomplete: error={error}, failed={[n for n, r in results.items() if not r.get('ok')]}",
              file=sys.stderr)
    return EXIT_OK if ok else EXIT_ERROR


# ---------------------------------------------------------------- self-test (pure, no commands, no files)

def self_test():
    count = [0]

    def check(cond, msg):
        count[0] += 1
        if not cond:
            raise AssertionError(msg)

    def L(t, app, ev, extra=""):
        return f"{t:.3f} {app} {ev} {extra}".rstrip()

    # Parsing: malformed lines skipped, fields, a missing file = no events.
    evs = parse_log_text("\n".join(["10.000 a START pid=1", "garbage", "12.x a DAILY_READY ms=1", "",
                                    "11.000 a DAILY_READY ms=1000 anon_mib=100",
                                    "11.001 a ERROR mmap: Cannot allocate memory"]), t0=10.0)
    check([e["event"] for e in evs] == ["START", "DAILY_READY", "ERROR"], f"tolerant parser: {[e['event'] for e in evs]}")
    check(evs[1]["t_s"] == 1.0 and evs[1]["ms"] == "1000", "parser fields and relative time")
    check(is_memory_error(evs[2]["msg"]), "ENOMEM text is a memory failure")
    check(not is_memory_error("mlock (raise LimitMEMLOCK): Cannot allocate memory"), "memlock rlimit is a setup error")
    check(parse_log_text(rr.read("/nonexistent/memlab/x.log"), 0) == [], "missing log file = no events")

    # The spec scenario: case T-01 has a policy kill during a ramp and a popup; in T-02 one app never
    # writes STARTUP_READY and is not killed (stopped by the runner at the deadline).
    def rec(cid, pos, order, begin, stopped=()):
        first = begin + 15
        clicks = {a: first + 3 * i for i, a in enumerate(order)}
        T = clicks[order[-1]] + 10 + 1
        return {"id": cid, "pos": pos, "order": order, "front": order[-1],
                "adj": {a: 100 if a == order[-1] else 900 for a in order}, "begin_s": begin, "hint_s": begin,
                "first_click_s": first, "last_click_s": clicks[order[-1]], "first_start_s": T - 1, "T_s": T,
                "T_planned_s": T, "hold_s": 15.0, "barrier_wait_s": 0.0, "precase_ok": True, "max_some_1s_ms": 120.0,
                "launch": {a: {"t_s": clicks[a], "rc": 0} for a in order},
                "starts": {a: {"planned_s": T - 1, "signal_s": T - 0.95, "high_fixup": False} for a in order},
                "stopped": list(stopped), "end_s": T + 20}

    def full(app, t_launch, T, exit_too=True):
        out = [L(t_launch, app, "START", "pid=7"), L(t_launch + 1, app, "DAILY_READY", "ms=1000"),
               L(t_launch + 1, app, "AWAIT_START"), L(T - 1, app, "PEAK_BEGIN", "mib=300"),
               L(T - 0.9, app, "PEAK_HOT_ANON", "ms=40 mib=20"), L(T, app, "PEAK_DONE", "ms=1000 timeout=0"),
               L(T + 15, app, "PEAK_RELEASE"), L(T + 15, app, "STARTUP_READY", "ms=30000")]
        return out + ([L(T + 17, app, "EXIT")] if exit_too else [])
    r1 = rec("T-01", 1, ["a1", "a2", "a3"], 0.0)
    r2 = rec("T-02", 2, ["b1", "b2"], r1["end_s"], stopped=["b1"])
    T1, T2 = r1["T_s"], r2["T_s"]
    texts = {("T-01", "a1"): [L(15, "a1", "START"), L(16, "a1", "DAILY_READY", "ms=1000"), L(16, "a1", "AWAIT_START"),
                              L(T1 - 1, "a1", "PEAK_BEGIN", "mib=300")],
             ("T-01", "a2"): full("a2", 18, T1), ("T-01", "a3"): full("a3", 21, T1),
             ("T-02", "b1"): [L(r2["launch"]["b1"]["t_s"], "b1", "START"), L(r2["launch"]["b1"]["t_s"] + 1, "b1", "DAILY_READY", "ms=900"),
                              L(r2["launch"]["b1"]["t_s"] + 1, "b1", "AWAIT_START"), L(T2 - 1, "b1", "PEAK_BEGIN"),
                              L(T2 + 3, "b1", "PEAK_DONE", "ms=4000 timeout=0"), L(T2 + 47, "b1", "EXIT")],
             ("T-02", "b2"): full("b2", r2["launch"]["b2"]["t_s"], T2)}
    logs = {k: parse_log_text("\n".join(v)) for k, v in texts.items()}
    pkills = [{"case": "T-01", "app": "a1", "t_s": T1 - 0.6, "level": 0, "some_ms": 85.0, "full_ms": 10.0, "adj": 900,
               "source": "policy_kill"}]
    pops = [{"case": "T-01", "t_s": T1 - 0.5, "available_mib": 42.0, "foreground": "a3"}]
    shadows = [{"case": "T-01", "t_s": T1 - 0.7, "some_ms": 210.0}]
    res = analyze([r1, r2], logs, pkills, [], pops, shadows)
    out = {(a["case"], a["app"]): a for a in res["attempts"]}
    check(out[("T-01", "a1")]["outcome"] == FAILURE and out[("T-01", "a1")]["cause"] == "policy_kill", "a1 policy kill = failure")
    check(out[("T-01", "a1")]["kill_phase"] == "ramp", f"a1 killed in its ramp: {out[('T-01', 'a1')]['kill_phase']}")
    check(all(out[k]["outcome"] == SUCCESS for k in (("T-01", "a2"), ("T-01", "a3"), ("T-02", "b2"))), "others succeed")
    check(out[("T-02", "b1")]["outcome"] == UNCONFIRMED and out[("T-02", "b1")]["cause"] == "timeout", "b1 unconfirmed (timeout)")
    c1, c2 = res["cases"]
    check((c1["popups"], c1["kills"], c1["policy_kills"], c1["kernel_oom"], c1["startup_failures"], c1["category_total"],
           c1["unique"], c1["device_style"], c1["shadow"], c1["unconfirmed"]) == (1, 1, 1, 0, 1, 3, 2, 2, 1, 0),
          f"case T-01 counts: {c1}")
    check((c2["category_total"], c2["unconfirmed"], c2["startup_timeouts"]) == (0, 1, 1), f"case T-02 counts: {c2}")
    t = res["totals"]
    check((t["category_total"], t["kills"], t["popups"], t["startup_failures"], t["unique"], t["device_style"], t["shadow"],
           t["startup_timeouts"]) == (3, 1, 1, 1, 2, 2, 1, 1), f"totals: {t}")
    check(res["types_present"] == {"startup_failure": True, "popup": True, "oom_kill": True}, "types present")
    types = [e["type"] for e in res["events"]]
    check(sorted(types) == ["policy_kill", "popup", "shadow", "startup_failure"], f"event rows: {types}")
    check(all(e["phase"] == "ramp" for e in res["events"]), f"event phases: {[e['phase'] for e in res['events']]}")
    v0 = {"pgscan_kswapd_delta": 0, "pgscan_direct_delta": 0, "drop_caches_lines": 0, "slice_max_events_delta": 5,
          "kernel_log_ok": True}
    reasons = validity_reasons("observe", t, res["unconfirmed"], v0)
    check(len(reasons) == 1 and reasons[0].startswith("unconfirmed"), f"unconfirmed invalidates: {reasons}")
    check(validity_reasons("observe", t, 0, v0) == [], "clean run is valid")
    check(any("max did not grow" in x for x in validity_reasons("observe", t, 0, dict(v0, slice_max_events_delta=0))),
          "observe without slice max events is invalid")
    check(validity_reasons("apply", t, 0, dict(v0, slice_max_events_delta=0)) == [], "that rule is observe-only")
    check(any("pgscan_direct" in x for x in validity_reasons("apply", t, 0, dict(v0, pgscan_direct_delta=3))), "VM reclaim")
    check(any("drop_caches" in x for x in validity_reasons("apply", t, 0, dict(v0, drop_caches_lines=1))), "drop_caches")

    # More rules: kernel OOM in a hold, ENOMEM while loading, missing log, a kill of an app that then
    # logged EXIT (no effect), a kill after STARTUP_READY, a memlock setup error, kernel log parsing.
    r3 = rec("T-03", 3, ["c1", "c2", "c3", "c4", "c5", "c6"], 0.0)
    T3 = r3["T_s"]
    kline = (f"{1000.0 + T3 + 3:.6f} host kernel: oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,"
             f"mems_allowed=0,oom_memcg=/{rr.SCOPE_REL},task_memcg=/{rr.SCOPE_REL}/memlab-c1.service,task=memlab_app,pid=9,uid=1000")
    ktext = "\n".join(["-- No entries --", kline, f"{1000.0 + 5:.6f} host kernel: sh (1): drop_caches: 1", "junk"])
    kk, dd = parse_kernel_text(ktext, 1000.0)
    check(len(kk) == 1 and kk[0]["app"] == "c1" and kk[0]["constraint"] == "CONSTRAINT_MEMCG" and abs(kk[0]["t_s"] - (T3 + 3)) < 1e-6,
          f"kernel oom-kill line: {kk}")
    check(len(dd) == 1 and dd[0]["t_s"] == 5.0, "drop_caches line")
    texts3 = {("T-03", "c1"): full("c1", 15, T3)[:6],     # PEAK_DONE, then killed in the hold
              ("T-03", "c2"): [L(18, "c2", "START"), L(18.5, "c2", "ERROR", "mmap: Cannot allocate memory")],
              ("T-03", "c4"): full("c4", 24, T3), ("T-03", "c5"): full("c5", 27, T3, exit_too=False),
              ("T-03", "c6"): [L(30, "c6", "START"), L(30.1, "c6", "ERROR", "mlock (raise LimitMEMLOCK): Cannot allocate memory")]}
    logs3 = {k: parse_log_text("\n".join(v)) for k, v in texts3.items()}
    pk3 = [{"case": "T-03", "app": "c4", "t_s": T3 + 1, "level": 0, "some_ms": 90, "full_ms": 0, "adj": 900, "source": "policy_kill"},
           {"case": "T-03", "app": "c5", "t_s": T3 + 16, "level": 1, "some_ms": 900, "full_ms": 750, "adj": 900, "source": "policy_kill"}]
    res3 = analyze([r3], logs3, pk3, kk, [], [])
    o3 = {a["app"]: a for a in res3["attempts"]}
    check(o3["c1"]["outcome"] == FAILURE and o3["c1"]["cause"] == "kernel_oom" and o3["c1"]["kill_phase"] == "hold", f"c1: {o3['c1']}")
    check(o3["c2"]["outcome"] == FAILURE and o3["c2"]["cause"] == "enomem" and o3["c2"]["fail_phase"] == "loading", f"c2: {o3['c2']}")
    check(o3["c3"]["outcome"] == UNCONFIRMED and o3["c3"]["cause"] == "no_log", "c3: missing log = unconfirmed")
    check(o3["c4"]["outcome"] == SUCCESS and o3["c4"]["kill"] is None and len(o3["c4"]["dropped_kills"]) == 1, "c4: kill w/o effect dropped")
    check(o3["c5"]["outcome"] == SUCCESS and o3["c5"]["kill_phase"] == "after_ready", "c5: kill after ready is a kill only")
    check(o3["c6"]["outcome"] == UNCONFIRMED and o3["c6"]["cause"] == "error", "c6: setup error = unconfirmed")
    t3 = res3["totals"]
    check((t3["kills"], t3["policy_kills"], t3["kernel_oom"], t3["startup_failures"], t3["category_total"], t3["unique"],
           t3["device_style"]) == (2, 1, 1, 2, 4, 3, 3), f"extra totals: {t3}")
    check(res3["unconfirmed"] == 2 and len(res3["dropped_kills"]) == 1, "extra unconfirmed / dropped")
    check(brake_seconds([{"unix_ms": "1000", "state": "on"}, {"unix_ms": "3500", "state": "off"},
                         {"unix_ms": "5000", "state": "on"}], 6000) == (3.5, 2), "brake seconds")
    check(precase_ok({"current_mib": 10, "zswap_mib": 0, "swap_mib": 0, "active": [], "prev_data_exists": False},
                     TIMELINE_DEFAULTS["precase"]), "precase ok")
    check(not precase_ok({"current_mib": None, "zswap_mib": 0, "swap_mib": 0, "active": []}, TIMELINE_DEFAULTS["precase"]),
          "precase: unreadable slice is not ok")

    # Planning, hint and launch arguments.
    tl = timeline({"timeline": {}})
    order8 = [f"x{i}" for i in range(8)]
    times = plan_times(tl, order8, {a: 1.0 for a in order8}, 100.0)
    check(times["first_click"] == 115 and times["clicks"][-1] == 136 and times["T"] == 147, f"plan times: {times}")
    apps8 = {a: {"anon_mib": 100, "clean_mib": 200, "locked_mib": 10, "peak_mib": 2000} for a in order8}
    h = hint_plan(apps8, order8, tl, 100.0, times["T"], 1e9 + 47)
    check(h["ttl_s"] == 72 and h["boost_mib"] == 8 * (314 + 2000) and h["peak_at_unix"] == 1000000047, f"hint: {h}")
    check(hint_args(h)[-2:] == ["--peak-at-unix", "1000000047"] and "x0:2314" in hint_args(h)[5], "hint args")
    a = {"anon_mib": 754, "clean_mib": 295, "locked_mib": 77, "compress_ratio": 3, "peak_mib": 5306.0, "ramp_s": 61.8,
         "peak_curve": [[0, 0], [61.8, 5306]], "hot_anon_mib": 8, "hot_file_mib": 8, "hot_period": 120,
         "peak_hot_anon_mib": 452, "peak_hot_anon_period": 10, "peak_compress_ratio": 3}
    cmd = launch_cmd(a, "shotcut", tl, "/l/s.log", "/d/S-01", zlib.crc32(b"shotcut") % 100000 + 3, 100, 169, "/b")
    kv_ = dict(zip(cmd[cmd.index("--name"):][::2], cmd[cmd.index("--name"):][1::2]))
    check(kv_["--peak-rate"] == "5306" and kv_["--await-start"] == "1" and kv_["--fill-threads"] == "4"
          and kv_["--peak-deadline"] == "6" and kv_["--exit-after-peak"] == "2" and kv_["--hold"] == "15"
          and kv_["--peak-hot-anon-mib"] == "452" and "--peak-curve" not in kv_ and "-p" in cmd
          and "OOMScoreAdjust=100" in cmd and kv_["--seed"] == str(zlib.crc32(b"shotcut") % 100000 + 3), f"launch: {kv_}")
    tl_m = timeline({"timeline": {"ramp_cap_s": None}})
    cmd = launch_cmd(a, "shotcut", tl_m, "/l", "/d", 1, 900, 300, "/b", Path("/c/shotcut.txt"))
    kv_ = dict(zip(cmd[cmd.index("--name"):][::2], cmd[cmd.index("--name"):][1::2]))
    check(kv_["--peak-rate"] == "0" and kv_["--peak-curve"] == "/c/shotcut.txt" and kv_["--peak-deadline"] == "74.16", f"curve launch: {kv_}")
    sc_chk = {"scope": {"memory_max_mib": 16384}, "apps": {n: {"anon_mib": 1000, "clean_mib": 500, "peak_mib": 1000} for n in "pqr"}}
    cases_chk = [{"id": "K", "order": ["p", "q", "r"]}]
    chk = acceptance_checks(sc_chk, cases_chk)
    check(chk["source"] == "nominal" and not chk["ok"] and not chk["refuse"], "nominal checks run but never refuse")
    for n in "pqr":
        sc_chk["apps"][n].update(cal_daily_mib=1500, cal_peak_mib=2500)
    check(acceptance_checks(sc_chk, cases_chk)["refuse"], "calibrated failing checks refuse")
    for n in "pqr":
        sc_chk["apps"][n].update(cal_peak_mib=7000)
    chk = acceptance_checks(sc_chk, cases_chk)
    check(chk["ok"] and not chk["refuse"] and chk["source"] == "calibrated", f"calibrated passing checks: {chk}")

    # The case state machine on the simulated clock (what --dry-run prints), 1-s ramps and measured ramps.
    for cap in (1, None):
        apps = {"a1": {"anon_mib": 100, "clean_mib": 50, "locked_mib": 10, "compress_ratio": 3, "peak_mib": 300, "ramp_s": 5.0,
                       "peak_curve": None, "peak_hot_anon_mib": 20, "peak_hot_anon_period": 10},
                "a2": {"anon_mib": 200, "clean_mib": 50, "locked_mib": 0, "compress_ratio": 3, "peak_mib": 400, "ramp_s": 12.0,
                       "peak_curve": [[0, 0], [12.0, 400]]},
                "a3": {"anon_mib": 50, "clean_mib": 20, "locked_mib": 0, "compress_ratio": 3, "peak_mib": 100, "ramp_s": 3.0,
                       "peak_curve": None}}
        sc = {"name": "selftest", "scope": {"memory_max_mib": 16384, "swap_max_mib": 25600},
              "timeline": {"ramp_cap_s": cap}, "adj": {"background": 900, "front": 100}, "apps": apps,
              "cases": [{"id": "X-01", "order": ["a1", "a2", "a3"]}, {"id": "X-02", "order": ["a2", "a3", "a1"]}]}
        ops = DryOps("/b", "/r/control.conf", quiet=True)
        ctx = Ctx(sc, ops, "/r", "/b", "apply", True, quiet=True)
        sel = select_cases(sc)
        ctx.n_cases = len(sel)
        ops.start_clock()
        ctx.t0_boot, ctx.t0_unix = 0.0, ops.unix0
        drive(ctx, all_cases(ctx, sel))
        x1, x2 = ctx.case_recs
        rmax = 1.0 if cap else 12.0
        check(abs(x1["first_click_s"] - (x1["hint_s"] + 15)) < 1e-6 and abs(x1["last_click_s"] - x1["first_click_s"] - 6) < 1e-6,
              f"clicks every 3 s after the 15-s lead: {x1}")
        check(abs(x1["T_s"] - (x1["barrier_release_s"] + rmax)) < 1e-6 and x1["barrier_wait_s"] == 0, f"T = barrier + ramp: {x1}")
        for r in (x1, x2):
            for app in r["order"]:
                want = r["T_s"] - (1.0 if cap else apps[app]["ramp_s"]) + 0.05
                check(abs(r["starts"][app]["signal_s"] - want) < 0.02, f"{r['id']} {app} start {r['starts'][app]} vs {want}")
            check(r["end_s"] >= r["T_s"] + 17 and not r["stopped"], f"{r['id']} ends after the exits")
        check(0 <= x2["hint_s"] - x1["end_s"] < 0.2, "gap 15 - lead 15: next hint right after the end")
        prot = [ln.split("--protect ")[1] for ln in ops.lines if "--protect " in ln]
        # Measured ramps: one protection sequence per start time (the front's own start repeats the list).
        want = ["a3", "a3,a1,a2", "none", "a1", "a1,a2,a3", "none"] if cap else \
            ["a3", "a3,a2", "a3,a2,a1", "a3,a2,a1", "none", "a1", "a1,a2", "a1,a2", "a1,a2,a3", "none"]
        check(prot == want, f"protection lists ({cap}): {prot}")
        check(sum("--boost-mb 0" in ln for ln in ops.lines) == 2 and sum("--peak-at-unix" in ln for ln in ops.lines) == 2,
              "one hint and one clear per case")
        if not cap:
            check(any("--peak-curve /r/curves/a2.txt" in ln for ln in ops.lines) and any("--peak-rate 60 " in ln for ln in ops.lines),
                  "measured ramps: curve file for a2, 300/5 MiB/s for a1")
        slog = {(r["id"], app): parse_log_text(ops.read_log(log_path(ctx.rd, r["id"], app))) for r in ctx.case_recs for app in r["order"]}
        sres = analyze(ctx.case_recs, slog, [], [], [], [])
        check(all(x["outcome"] == SUCCESS for x in sres["attempts"]) and sres["totals"]["category_total"] == 0
              and sres["unconfirmed"] == 0, "simulated apps all succeed")
    print(f"self-test passed: {count[0]} checks")
    return EXIT_OK


# ---------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 epilog="See the module docstring (head -50 run_accept.py) for outputs and exit codes.")
    ap.add_argument("--scenario")
    ap.add_argument("--mode", choices=["observe", "apply"])
    ap.add_argument("--hint", choices=["on", "off"], default="on", help="oracle hint (default on; apply + off = TMO control)")
    ap.add_argument("--label")
    ap.add_argument("--run-id")
    ap.add_argument("--out", help="output directory (run), or the calibration JSON file (--calibrate)")
    ap.add_argument("--case-set", choices=["cases", "tuning", "reference"], default="cases")
    ap.add_argument("--first", type=int, default=1, help="1-based position of the first case in the case set")
    ap.add_argument("--cases", type=int, default=None, help="number of cases from --first (default: all the rest)")
    ap.add_argument("--set", action="append", default=[], help="memshiftd key=value override (repeatable)")
    ap.add_argument("--calibrate", action="store_true", help="run every scenario app alone; write {app: {daily_mib, peak_mib, startup_ms}}")
    ap.add_argument("--dry-run", action="store_true", help="print the schedule and every command; execute nothing")
    ap.add_argument("--self-test", action="store_true", help="check the attribution and the state machine (no commands)")
    ap.add_argument("--allow-low-memfree", action="store_true", help=f"run even if MemFree < {MEMFREE_MIN_GIB} GiB")
    ap.add_argument("--check-memfree", action="store_true", help=f"print MemFree; exit 0, or {EXIT_MEMFREE} if < {MEMFREE_MIN_GIB} GiB")
    ap.add_argument("--build", default=None, help="directory with memlab_app, memshiftd, memshiftctl (default CODE/build)")
    ap.add_argument("--skip-preflight", action="store_true", help="skip the memlab_app await-start preflight")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.self_test:
        try:
            return self_test()
        except AssertionError as e:
            print(f"SELF-TEST FAILED: {e}", file=sys.stderr)
            return EXIT_ERROR
    if args.check_memfree:
        mf = memfree_mib()
        ok = mf is not None and mf >= MEMFREE_MIN_GIB * 1024
        print(json.dumps({"memfree_mib": None if mf is None else round(mf), "min_mib": MEMFREE_MIN_GIB * 1024, "ok": ok}))
        return EXIT_OK if ok else EXIT_MEMFREE
    if not args.scenario:
        ap.error("--scenario is required")
    try:
        sc_raw = Path(args.scenario).read_bytes()
        sc = json.loads(sc_raw)
        if not isinstance(sc.get("scope"), dict) or "memory_max_mib" not in sc["scope"] or not sc.get("apps"):
            raise ValueError("needs scope.memory_max_mib and apps")
    except (OSError, ValueError) as e:
        print(f"cannot use scenario {args.scenario}: {e}", file=sys.stderr)
        return EXIT_USAGE
    for item in args.set:
        k, sep, v = item.partition("=")
        if not sep or not k.strip():
            ap.error(f"--set needs key=value, got {item!r}")
        sc.setdefault("memshiftd", {})[k.strip()] = v.strip()
    build = Path(args.build).resolve() if args.build else rr.BUILD
    try:
        if args.calibrate:
            if not args.out:
                ap.error("--calibrate needs --out cal.json")
            return cmd_calibrate(args, sc, build, dry=args.dry_run)
        if not args.mode:
            ap.error("--mode observe|apply is required")
        if not args.dry_run and not (args.label and args.run_id and args.out):
            ap.error("--label, --run-id and --out are required")
        selected = select_cases(sc, args.case_set, args.first, args.cases)
        checks = acceptance_checks(sc, [c for _, c in selected])
        if args.dry_run:
            return cmd_dry_run(args, sc, selected, checks, build)
        return cmd_run(args, sc, sc_raw, selected, checks, build)
    except UsageError as e:
        print(f"usage error: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
