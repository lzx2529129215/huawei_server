#!/usr/bin/env python3
"""Run one round of the Linux OOM lab.

memlab.slice (user systemd, no sudo) stands in for the machine: its MemoryMax is
the "physical memory" and MemorySwapMax its swap budget. Each simulated app is a
memlab_app process in its own memlab-<name>.service. memshiftd runs off, in
observe mode, or in apply mode with scope_cgroup pointing at the slice. The same
oracle boost hints are sent in observe and apply, so the only difference
between them is whether memory.reclaim is written.

Timeline operations (scenario "timeline"):
  launch  start an app (cold start)
  peak    SIGUSR2 to the listed apps: allocate peak_mib, hold, release
  return  the user comes back to the listed background apps: SIGUSR1 if alive
          (touch all daily memory; swapped pages fault back), relaunch if killed

Low-memory popups: Linux shows none, so a popup is recorded (not drawn) when
the slice's available memory (free + clean page cache + half of reclaimable
slab, as in linux_scope.hpp) drops below low_mib; it re-arms only after staying
at or above rearm_mib for rearm_s.

Whitelist scenarios (scenario "cases"): a launch_group op cold-starts a group
of apps together (the user clicks them all); each app runs its typical task
peak peak_after_s seconds after it is ready, holds it, then closes. The three
acceptance conditions are checked per case on the group's apps. The kill
policy can be two-level lmkd-like ("levels"), and startup failures follow the
Harmony framework: a startup attempt (START to STARTUP_READY) ended by a kill;
attempts that simply never got ready are startup_timeout and not counted.

Aligned-peak scenarios (scenario "peak_plan", make_cgroup_scenarios.py): app
profiles come from cgroup measurements; every peak app ramps at its own rate so
that all of them reach their peak at the same time T, hold, and return to daily
one by one; then the background apps are switched back one by one (return).
One oracle hint goes out `lead` seconds before the first ramp starts; it carries
the size every peak app will reach (memshiftctl --peak-targets). Peak apps can
be killed by the policy killer too (they have the lowest oom_adj, so last).
The main metric is oom_events = kills + peak allocation timeouts; popups are
reported separately.

Kills: kernel OOM kills come from the kernel log (oom-kill lines whose
task_memcg is an app unit). The optional policy killer is lmkd-like: when the
slice's memory stall within a sliding window exceeds a threshold it SIGKILLs the
least recently used live background app. Both are reported separately.
"""
import argparse
import csv
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import zlib
from pathlib import Path

LAB = Path(__file__).resolve().parent
BUILD = LAB.parent / "build"
UID = os.getuid()
USER_CG = f"user.slice/user-{UID}.slice/user@{UID}.service"
SCOPE_REL = f"{USER_CG}/memlab.slice"
SCOPE = Path("/sys/fs/cgroup") / SCOPE_REL
MiB = 1024 * 1024


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def sh(cmd, check=True):
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def read(path, default=""):
    try:
        return Path(path).read_text()
    except OSError:
        return default


def kv(text):
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def psi(text):
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        for item in parts[1:]:
            k, _, v = item.partition("=")
            if k == "total":
                out[parts[0] + "_total"] = int(v)
            elif k == "avg10":
                out[parts[0] + "_avg10"] = float(v)
    return out


def unit(app):
    return f"memlab-{app}.service"


def app_cg(app):
    return SCOPE / unit(app)


def daily_mib(a):
    return a.get("anon_mib", 0) + a.get("locked_mib", 0) + a.get("clean_mib", 0) + a.get("dirty_mib", 0) + 4


def app_daily(a):
    return a.get("cal_daily_mib", daily_mib(a))


def app_peak(a):
    return a.get("cal_peak_mib", daily_mib(a) + a.get("peak_mib", 0))


def case_checks(sc):
    """Acceptance conditions per case, on the apps launched together.
    (2) uses the strictest reading: every app that is open (residents plus the
    group) and also all apps of the scenario together stay within M."""
    m = sc["scope"]["memory_max_mib"]
    apps = {a["name"]: a for a in sc["apps"]}
    residents = [a for a in sc["apps"] if a["role"] == "resident"]
    all_daily = sum(app_daily(a) for a in sc["apps"])
    out = []
    for c in sc["cases"]:
        g = [apps[n] for n in c["apps"]]
        out.append({
            "case": c["id"], "apps": c["apps"],
            "max_single_peak_mib": max(app_peak(a) for a in g),
            "open_daily_sum_mib": sum(app_daily(a) for a in g) + sum(app_daily(a) for a in residents),
            "group_peak_sum_mib": sum(app_peak(a) for a in g),
            "group_peak_ratio": round(sum(app_peak(a) for a in g) / m, 3),
        })
    return {
        "memory_max_mib": m, "source": "calibrated" if all("cal_peak_mib" in a for a in sc["apps"]) else "nominal",
        "all_apps_daily_sum_mib": all_daily, "cases": out,
        "single_peak_le_M": all(c["max_single_peak_mib"] <= m for c in out),
        "daily_sum_le_M": all_daily <= m and all(c["open_daily_sum_mib"] <= m for c in out),
        "peak_sum_ge_1_2M": all(c["group_peak_sum_mib"] >= 1.2 * m for c in out),
    }


def aligned_checks(sc):
    """Experiment-plan conditions (实验设置.md §3) on calibrated values when present:
    daily sum 9.5-10.5 GiB, every app's peak <= M, all peaks at once >= 1.2 M."""
    m = sc["scope"]["memory_max_mib"]
    daily = sum(app_daily(a) for a in sc["apps"])
    at_peak = daily + sum(app_peak(a) - app_daily(a) for a in sc["apps"] if a["role"] == "peak")
    return {
        "memory_max_mib": m, "source": "calibrated" if all("cal_peak_mib" in a for a in sc["apps"]) else "nominal",
        "daily_sum_mib": daily, "aligned_peak_sum_mib": at_peak, "aligned_peak_ratio": round(at_peak / m, 3),
        "max_single_peak_mib": max(app_peak(a) for a in sc["apps"]),
        "daily_in_band_M": 9.5 * 1024 <= daily <= 10.5 * 1024,
        "single_peak_le_M": max(app_peak(a) for a in sc["apps"]) <= m,
        "daily_sum_le_M": daily <= m,
        "peak_sum_ge_1_2M": at_peak >= 1.2 * m,
    }


def contract_checks(sc):
    if sc.get("cases"):
        return case_checks(sc)
    if sc.get("peak_plan"):
        return aligned_checks(sc)
    m = sc["scope"]["memory_max_mib"]
    daily = [daily_mib(a) for a in sc["apps"]]
    peak = [daily_mib(a) + a.get("peak_mib", 0) for a in sc["apps"]]
    return {
        "memory_max_mib": m,
        "max_single_peak_mib": max(peak),
        "daily_sum_mib": sum(daily),
        "peak_sum_mib": sum(peak),
        "peak_sum_ratio": round(sum(peak) / m, 3),
        "single_peak_le_M": max(peak) <= m,
        "daily_sum_le_M": sum(daily) <= m,
        "peak_sum_ge_1_2M": sum(peak) >= 1.2 * m,
    }


def cleanup_units():
    sh(["systemctl", "--user", "stop", "memlab-*.service"], check=False)
    sh(["systemctl", "--user", "reset-failed", "memlab-*.service"], check=False)
    sh(["systemctl", "--user", "stop", "memlab.slice"], check=False)
    for _ in range(100):
        if not SCOPE.exists():
            return
        time.sleep(0.1)


def memshiftd_config(sc, rd):
    o = {
        "log_dir": rd / "memshiftd",
        "lock_file": rd / "memshiftd.lock",
        "control_file": rd / "control.conf",
        "scope_cgroup": SCOPE_REL,
    }
    o.update(sc.get("memshiftd", {}))
    lines = ["[daemon]"] + [f"{k}={v}" for k, v in o.items()]
    for a in sc["apps"]:
        lines += [f"[app {a['name']}]", f"path={SCOPE_REL}/{unit(a['name'])}",
                  f"reclaim={'false' if a['role'] == 'peak' else 'true'}",
                  f"min_keep_mb={a.get('min_keep_mb', 32)}"]
        if sc.get("peak_plan"):
            # high mode reclaims and sends to disk the most expendable apps first.
            lines.append(f"priority={a.get('oom_adj', 0)}")
    return "\n".join(lines) + "\n"


def alive_apps(names):
    out = set()
    for n in names:
        if read(app_cg(n) / "cgroup.procs").strip():
            out.add(n)
    return out


class PolicyKiller:
    """lmkd-like: SIGKILL the least recently used live background app when the
    slice's memory stall within a sliding window exceeds a threshold."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.hist = []
        self.last_kill = -1e9
        self.kills = []

    def poll(self, now, lru, alive, adj=None):
        p = psi(read(SCOPE / "memory.pressure"))
        if "some_total" not in p:
            return None
        self.hist.append((now, p["some_total"], p.get("full_total", 0)))
        window = self.cfg.get("window_s", 1.0)
        while self.hist and now - self.hist[0][0] > window:
            self.hist.pop(0)
        if len(self.hist) < 2 or now - self.last_kill < self.cfg.get("cooldown_s", 1.0):
            return None
        some_ms = (self.hist[-1][1] - self.hist[0][1]) / 1000
        full_ms = (self.hist[-1][2] - self.hist[0][2]) / 1000
        level = None
        if "levels" in self.cfg:
            # lmkd-like: each level makes processes with oom_adj >= min_adj eligible;
            # the most severe triggered level wins. Highest adj first, then LRU.
            for i, lv in enumerate(self.cfg["levels"]):
                if some_ms >= lv.get("some_ms", 1e9) or full_ms >= lv.get("full_ms", 1e9):
                    level = i
            if level is None:
                return None
            min_adj = self.cfg["levels"][level]["min_adj"]
            order = {n: i for i, n in enumerate(lru)}
            victims = sorted((n for n in lru if n in alive and adj.get(n, 0) >= min_adj),
                             key=lambda n: (-adj.get(n, 0), order[n]))
        else:
            if some_ms < self.cfg.get("some_ms", 1e9) and full_ms < self.cfg.get("full_ms", 1e9):
                return None
            victims = [n for n in lru if n in alive]
        if not victims:
            return None
        victim = victims[0]
        sh(["systemctl", "--user", "kill", "-s", "SIGKILL", unit(victim)], check=False)
        self.last_kill = now
        self.hist.clear()
        rec = {"t": now, "app": victim, "some_ms": round(some_ms, 1), "full_ms": round(full_ms, 1),
               "level": level, "adj": adj.get(victim) if adj else None}
        self.kills.append(rec)
        return victim


def scope_available():
    """Same formula as memshiftd's scope mode (src/linux_scope.hpp)."""
    try:
        limit = int(read(SCOPE / "memory.max", "0").strip())
        current = int(read(SCOPE / "memory.current", "0").strip())
    except ValueError:
        return None
    st = kv(read(SCOPE / "memory.stat"))
    clean = max(0, st.get("file", 0) - st.get("shmem", 0) - st.get("file_dirty", 0) - st.get("file_writeback", 0))
    return min(limit, max(0, limit - current) + clean + st.get("slab_reclaimable", 0) // 2)


class PopupMonitor:
    """Low-memory popup stand-in: one popup per low-memory episode."""

    def __init__(self, cfg):
        self.low = cfg.get("low_mib", 50) * MiB
        self.rearm = cfg.get("rearm_mib", 150) * MiB
        self.rearm_s = cfg.get("rearm_s", 3.0)
        self.armed, self.above_since, self.popups = True, None, []

    def poll(self, now, foreground):
        avail = scope_available()
        if avail is None:
            return
        if self.armed:
            if avail < self.low:
                self.armed, self.above_since = False, None
                self.popups.append({"t": now, "available_mib": round(avail / MiB, 1), "foreground": foreground})
        elif avail >= self.rearm:
            if self.above_since is None:
                self.above_since = now
            elif now - self.above_since >= self.rearm_s:
                self.armed = True
        else:
            self.above_since = None


def parse_app_logs(path):
    events = []
    for line in read(path).splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        rec = {"t": float(parts[0]), "event": parts[2]}
        for item in parts[3:]:
            k, _, v = item.partition("=")
            rec[k] = v
        events.append(rec)
    return events


def startup_attempts(evs, app, kills):
    """Split an app log into startup attempts (one per START). An attempt that
    never reached STARTUP_READY is a startup failure when a kill of this app
    falls inside it, otherwise a startup timeout."""
    starts = [e for e in evs if e["event"] == "START"]
    out = []
    for j, st in enumerate(starts):
        end = starts[j + 1]["t_s"] if j + 1 < len(starts) else 1e12
        within = [e for e in evs if st["t_s"] <= e["t_s"] < end]
        ready = next((e for e in within if e["event"] == "STARTUP_READY"), None)
        if ready:
            out.append({"t_s": st["t_s"], "outcome": "ready", "ms": float(ready["ms"])})
            continue
        # Any kill of this app between this START and the next one ended the attempt.
        kill = next((k for k in kills if k["app"] == app and st["t_s"] - 0.5 <= k["t_s"] < end), None)
        enomem = any(e["event"] == "ERROR" for e in within)
        if kill or enomem:
            out.append({"t_s": st["t_s"], "outcome": "startup_failure",
                        "cause": "enomem" if enomem and not kill else kill["source"]})
        else:
            out.append({"t_s": st["t_s"], "outcome": "startup_timeout"})
    return out


def oracle_need(group, error):
    """Largest concurrent extra demand of a launch group over time: each app's
    daily footprint from launch, plus its task increment during the peak."""
    edges = []
    for a, click in group:
        edges.append((click, daily_mib(a)))
        if a.get("peak_mib"):
            start = click + 1.0 + a.get("peak_after_s", 0)
            edges.append((start, a["peak_mib"]))
            edges.append((start + 1.0 + a.get("hold_s", 0), -a["peak_mib"]))
        if a.get("exit_after_s", -1) >= 0:
            edges.append((click + 2.0 + a.get("peak_after_s", 0) + a.get("hold_s", 0) + a["exit_after_s"], -daily_mib(a)))
    level = peak = 0
    for _, d in sorted(edges, key=lambda x: (x[0], x[1])):
        level += d
        peak = max(peak, level)
    return peak * (1 + error)


def kernel_log(since_unix):
    out = sh(["journalctl", "-k", f"--since=@{int(since_unix) - 1}", "-o", "short-unix", "--no-pager"],
             check=False).stdout
    kills, drops = [], []
    for line in out.splitlines():
        if "oom-kill:" in line and "memlab.slice" in line:
            m = re.search(r"task_memcg=\S*/memlab-([^/,]+)\.service", line)
            c = re.search(r"constraint=(\w+)", line)
            kills.append({"t_unix": float(line.split()[0]), "app": m.group(1) if m else "?",
                          "constraint": c.group(1) if c else "?"})
        elif "drop_caches" in line:
            drops.append(float(line.split()[0]))
    return kills, drops


def reclaim_rows(log_dir):
    path = log_dir / "reclaim.csv"
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def reclaim_summary(rows):
    out = {"rows": len(rows), "ok": 0, "observe_only": 0, "other": 0, "submitted_mib": 0.0,
           "planned_mib": 0.0, "current_change_mib": 0.0, "swap_change_mib": 0.0, "write_ms": 0.0}
    for r in rows:
        st = r["status"]
        if st == "ok":
            out["ok"] += 1
            out["submitted_mib"] += int(r["submitted_bytes"] or 0) / MiB
            out["current_change_mib"] += float(r["current_change"] or 0) / MiB
            out["swap_change_mib"] += float(r["swap_change"] or 0) / MiB
            out["write_ms"] += float(r["write_ms"] or 0)
        elif st == "observe_only":
            out["observe_only"] += 1
            out["planned_mib"] += int(r["planned_bytes"] or 0) / MiB
        else:
            out["other"] += 1
    return {k: round(v, 1) if isinstance(v, float) else v for k, v in out.items()}


def csv_rows(path):
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def aligned_summary(sc, hints, kkills, policy_kills, popups, app_events, apps_out, relaunches,
                    reclaim, sample_rows, drops_s, rd, duration, start_unix):
    """Per-phase view of an aligned-peak round, plus peak latencies, switch-back
    costs and what memshiftd's memory.high pulses did."""
    plan, apps = sc["peak_plan"], {a["name"]: a for a in sc["apps"]}
    m = sc["scope"]["memory_max_mib"]
    t_peak, hold = plan["T"], plan["hold_s"]
    hint_at = hints[0]["at_s"] if hints else plan["first_ramp_s"] - 30
    bounds = [("daily", 0, hint_at), ("prep", hint_at, plan["first_ramp_s"]), ("ramp", plan["first_ramp_s"], t_peak),
              ("hold", t_peak, t_peak + hold), ("release", t_peak + hold, plan["return_start_s"]),
              ("return", plan["return_start_s"], duration)]
    pts = [(float(r["t"]), int(r["some_total_us"]), int(r["full_total_us"]), int(r["scope_current_mib"])) for r in sample_rows]

    def at_or_before(x):
        before = [p for p in pts if p[0] <= x]
        return before[-1] if before else (pts[0] if pts else None)

    def stall(a, b):
        # Phases tile: stall accrued between the samples around a boundary is not lost.
        p0, p1 = at_or_before(a), at_or_before(b)
        if p0 is None or p1 is None or p1[0] <= p0[0]:
            return 0, 0
        return round((p1[1] - p0[1]) / 1000), round((p1[2] - p0[2]) / 1000)

    def min_free(a, b):
        w = [m - p[3] for p in pts if a <= p[0] < b]
        return min(w) if w else None

    def job_t(r):
        return int(r["unix_ms"]) / 1000 - start_unix if start_unix and r.get("unix_ms") else None

    peak_done = {n: [e for e in app_events[n] if e["event"] == "PEAK_DONE"] for n in apps}
    phases = []
    for name, a0, b0 in bounds:
        some, full = stall(a0, b0)
        within = lambda x: a0 <= x["t_s"] < b0
        jobs = [r for r in reclaim if r["status"] == "ok" and job_t(r) is not None and a0 <= job_t(r) < b0]
        lats = [float(e["ms"]) for n in apps for e in peak_done[n] if a0 <= e["t_s"] < b0]
        phases.append({
            "phase": name, "t": round(a0, 1), "end": round(b0, 1), "apps": [],
            "kernel_kills": sum(1 for k in kkills if within(k)), "policy_kills": sum(1 for k in policy_kills if within(k)),
            "killed": [k["app"] for k in sorted(kkills + policy_kills, key=lambda k: k["t_s"]) if within(k)],
            "popups": sum(1 for pp in popups if within(pp)), "startup_failures": 0, "case": None,
            "stall_some_ms": some, "stall_full_ms": full, "min_free_mib": min_free(a0, b0),
            "peak_ms_max": max(lats) if lats else None,
            "reclaimed_before_mib": round(-sum(float(r["current_change"] or 0) for r in jobs) / MiB, 1),
            "drop_caches_in_window": any(a0 - 5 <= d < b0 for d in drops_s),
        })
    killed = {k["app"] for k in kkills + policy_kills}
    peaks = []
    for n, a in apps.items():
        if a["role"] != "peak":
            continue
        done = peak_done[n][0] if peak_done[n] else None
        ms = float(done["ms"]) if done else None
        peaks.append({"app": n, "ramp_s": a.get("ramp_s"), "deadline_s": a.get("peak_deadline_s"),
                      "done_ms": ms, "late_ms": round(ms - 1000 * a.get("ramp_s", 0)) if ms is not None else None,
                      "timeout": bool(done and done.get("timeout") == "1"), "done_t_s": done["t_s"] if done else None,
                      "killed": n in killed})
    returns = []
    for e in sc["timeline"]:
        if e["op"] != "return":
            continue
        for n in e["apps"]:
            td = next((x for x in app_events[n] if x["event"] == "TOUCH_DONE" and x["t_s"] >= e["t"] - 0.5), None)
            returns.append({"app": n, "t": e["t"], "relaunched": any(r["app"] == n and abs(r["t_s"] - e["t"]) < 2 for r in relaunches),
                            "touch_ms": float(td["ms"]) if td else None,
                            "anon_ms": float(td["anon_ms"]) if td and "anon_ms" in td else None,
                            "file_ms": float(td["file_ms"]) if td and "file_ms" in td else None})
    pulses = csv_rows(rd / "memshiftd" / "pulses.csv")
    pages = lambda key: round(sum(int(p.get(key) or 0) for p in pulses) * 4096 / MiB, 1)
    quota = csv_rows(rd / "memshiftd" / "quota.csv")
    near_t = min(pts, key=lambda p: abs(p[0] - t_peak)) if pts else None
    at_t = [m - near_t[3]] if near_t and abs(near_t[0] - t_peak) <= 3 else []
    return {
        "phases": phases, "hint_at_s": hint_at, "peaks": peaks, "returns": returns,
        "free_at_T_mib": at_t[0] if at_t else None,
        "pulses": {"n": len(pulses), "jobs": sum(1 for r in reclaim if r["status"] == "ok"),
                   "net_freed_mib": round(sum(float(p["net_drop"] or 0) for p in pulses) / MiB, 1),
                   "write_ms": round(sum(float(p["write_ms"] or 0) for p in pulses)),
                   "max_write_ms": max((float(p["write_ms"] or 0) for p in pulses), default=0),
                   "not_restored": sum(1 for p in pulses if p.get("restored") == "0"),
                   "zswpout_mib": pages("zswpout_delta"), "zswpwb_mib": pages("zswpwb_delta"),
                   "pswpout_mib": pages("pswpout_delta")},
        "quota_plan": [{k: q[k] for k in ("app", "action", "quota_bytes", "disk_share_bytes", "need_bytes",
                                         "zswap_capacity_bytes", "deficit_bytes", "disk_anon_bytes", "status")} for q in quota],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--mode", choices=["off", "observe", "apply"], required=True)
    ap.add_argument("--round-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", default="", help="suffix for the round directory name")
    ap.add_argument("--lead", type=float, default=None, help="oracle hint lead seconds; omit for no hints")
    ap.add_argument("--error", type=float, default=0.0, help="oracle size error, e.g. 0.2 or -0.2")
    ap.add_argument("--policy-kill", default=None, help="JSON overriding scenario policy_kill ('null' disables)")
    ap.add_argument("--set", action="append", default=[], help="memshiftd key=value override")
    ap.add_argument("--popup", default=None, help="JSON overriding scenario popup ('null' disables)")
    ap.add_argument("--allow-contract-fail", action="store_true")
    args = ap.parse_args()

    sc = json.loads(Path(args.scenario).read_text())
    for item in args.set:
        k, _, v = item.partition("=")
        sc.setdefault("memshiftd", {})[k] = v
    policy = sc.get("policy_kill")
    if args.policy_kill is not None:
        policy = json.loads(args.policy_kill)
    popup_cfg = sc.get("popup", {"low_mib": 50, "rearm_mib": 150, "rearm_s": 3.0})
    if args.popup is not None:
        popup_cfg = json.loads(args.popup)
    checks = contract_checks(sc)
    if not all(v for k, v in checks.items() if k.endswith(("_M", "_1_2M"))) and not args.allow_contract_fail:
        sys.exit(f"contract checks failed: {checks}")
    apps = {a["name"]: a for a in sc["apps"]}
    names = [a["name"] for a in sc["apps"]]
    timeline = sorted(sc["timeline"], key=lambda e: e["t"])
    duration = sc["duration_s"]

    # Each round writes a few GiB of temporary files into the WSL disk image (on C:)
    # and swap lives on C:; refuse to start when either is close to full.
    for mount in ("/", "/mnt/c"):
        if os.path.isdir(mount) and shutil.disk_usage(mount).free < 20 * 1024 * MiB:
            sys.exit(f"less than 20 GiB free on {mount}; not starting the round")

    rd = Path(args.out) / f"{sc['name']}_{args.mode}{args.tag}_{args.round_id}"
    if rd.exists():
        shutil.rmtree(rd)
    (rd / "apps").mkdir(parents=True)
    data = rd / "data"
    data.mkdir()
    (rd / "scenario.json").write_text(json.dumps(sc, indent=1))
    (rd / "args.json").write_text(json.dumps(vars(args), indent=1))

    cleanup_units()
    m, s = sc["scope"]["memory_max_mib"], sc["scope"]["swap_max_mib"]
    props = [f"MemoryMax={m}M", f"MemorySwapMax={s}M"]
    if "zswap_max_mib" in sc["scope"]:
        props.append(f"MemoryZSwapMax={sc['scope']['zswap_max_mib']}M")
    sh(["systemctl", "--user", "set-property", "--runtime", "memlab.slice"] + props)
    sh(["systemd-run", "--user", "-q", f"--unit={unit('anchor')}", "--slice=memlab",
        "sleep", str(duration + 120)])
    for _ in range(50):
        if SCOPE.exists():
            break
        time.sleep(0.1)

    daemon, dlog = None, None
    ctl = BUILD / "memshiftctl"
    peak_names = [a["name"] for a in sc["apps"] if a["role"] == "peak"]
    if args.mode != "off":
        (rd / "memshiftd.ini").write_text(memshiftd_config(sc, rd))
        sh([str(ctl), "--file", str(rd / "control.conf"), "--init", "--protect", ",".join(peak_names) or "none"])
        dlog = open(rd / "memshiftd.stderr", "w")
        daemon = subprocess.Popen([str(BUILD / "memshiftd"), "--config", str(rd / "memshiftd.ini"),
                                   "--apply" if args.mode == "apply" else "--observe"],
                                  stdout=dlog, stderr=subprocess.STDOUT)
        time.sleep(1.5)
        if daemon.poll() is not None:
            cleanup_units()
            sys.exit(f"memshiftd exited early; see {rd / 'memshiftd.stderr'}")

    # Oracle hints: the fixed timeline is known, so every peak's size and time
    # are looked up instead of predicted.
    actions = [(e["t"], "op", e) for e in timeline]
    hints = []
    if args.lead is not None and args.mode != "off" and sc.get("peak_plan"):
        # One hint before the first ramp: the size each peak app will reach.
        plan = sc["peak_plan"]
        peak_apps = [a for a in sc["apps"] if a["role"] == "peak"]
        at = max(0.0, plan["first_ramp_s"] - args.lead)
        until = plan["T"] + plan["hold_s"]
        h = {"at_s": at, "peak_at_s": plan["T"], "first_ramp_s": plan["first_ramp_s"],
             "boost_mib": int(math.ceil(sum(a["peak_mib"] for a in peak_apps) * (1 + args.error))),
             "targets_mib": {a["name"]: int(round(app_daily(a) + a["peak_mib"] * (1 + args.error))) for a in peak_apps},
             "ttl_s": int(min(300, math.ceil(until - at)))}
        h["need_mib"] = h["boost_mib"]
        hints.append(h)
        actions.append((at, "hint", h))
    elif args.lead is not None and args.mode != "off":
        cap = int(sc.get("memshiftd", {}).get("max_boost_mb", 4096))
        for e in timeline:
            if e["op"] == "launch_group":
                gap = e.get("click_gap_s", 0)
                members = [apps[n] for n in e["apps"]]
                need = oracle_need([(a, k * gap) for k, a in enumerate(members)], args.error)
                span = max(k * gap + a.get("peak_after_s", 0) + a.get("hold_s", 0) for k, a in enumerate(members))
            elif e["op"] == "peak":
                members = [apps[n] for n in e["apps"]]
                need = sum(x["peak_mib"] for x in members) * (1 + args.error)
                span = max(x.get("hold_s", 0) for x in members)
            else:
                continue
            h = {"at_s": max(0.0, e["t"] - args.lead), "peak_at_s": e["t"],
                 "boost_mib": min(cap, max(1, int(math.ceil(need)))), "need_mib": int(math.ceil(need)),
                 "ttl_s": int(min(300, args.lead + span + 15))}
            hints.append(h)
            actions.append((h["at_s"], "hint", h))
    actions.sort(key=lambda x: (x[0], x[1] != "hint"))

    t0, t0_unix = boottime(), time.time()
    base_scope = {**kv(read(SCOPE / "memory.events")), **psi(read(SCOPE / "memory.pressure"))}
    base_stat = kv(read(SCOPE / "memory.stat"))

    def launch(a, gen, adj_value=None):
        adj_value = a.get("oom_adj", 200) if adj_value is None else adj_value
        cur_adj[a["name"]] = adj_value
        cmd = ["systemd-run", "--user", "-q", f"--unit={unit(a['name'])}", "--slice=memlab",
               "-p", f"OOMScoreAdjust={adj_value}", str(BUILD / "memlab_app"),
               "--name", a["name"], "--log", str(rd / "apps" / f"{a['name']}.log"),
               "--data-dir", str(data), "--anon-mib", str(a.get("anon_mib", 0)),
               "--clean-mib", str(a.get("clean_mib", 0)), "--dirty-mib", str(a.get("dirty_mib", 0)),
               "--dirty-rate", str(a.get("dirty_rate_mib_s", 0)), "--ttl", str(duration + 5),
               "--compress-ratio", str(a.get("compress_ratio", 1)),
               "--seed", str(a.get("seed", zlib.crc32(a["name"].encode()) % 100000 + gen))]
        for key, flag in (("locked_mib", "--locked-mib"), ("touch_file", "--touch-file"),
                          ("peak_compress_ratio", "--peak-compress-ratio"), ("hot_anon_mib", "--hot-anon-mib"),
                          ("hot_file_mib", "--hot-file-mib"), ("hot_period", "--hot-period"),
                          ("peak_hot_file_mib", "--peak-hot-file-mib"), ("peak_hot_period", "--peak-hot-period")):
            if a.get(key):
                cmd += [flag, str(a[key])]
        if a.get("peak_curve"):
            # Measured peak curve (make_cgroup_scenarios.py --peak-curve): one "seconds MiB" per line.
            cf = rd / "curves" / f"{a['name']}.txt"
            cf.parent.mkdir(exist_ok=True)
            cf.write_text("".join(f"{t_} {m_}\n" for t_, m_ in a["peak_curve"]))
            cmd += ["--peak-curve", str(cf)]
        if a.get("peak_mib"):
            cmd += ["--peak-mib", str(a["peak_mib"]), "--peak-rate", str(a.get("peak_rate_mib_s", 0)),
                    "--peak-page-only", str(a.get("peak_page_only", 0)),
                    "--hold", str(a.get("hold_s", 0)), "--peak-deadline", str(a.get("peak_deadline_s", 30))]
            if "peak_after_s" in a:
                cmd += ["--peak-after", str(a["peak_after_s"])]
        if a.get("exit_after_s", -1) >= 0:
            cmd += ["--exit-after-peak", str(a["exit_after_s"])]
        # A killed unit stays failed until reset; clear it so the name can be reused.
        sh(["systemctl", "--user", "reset-failed", unit(a["name"])], check=False)
        return sh(cmd, check=False).returncode

    cur_adj = {}
    killer = PolicyKiller(policy) if policy else None
    popup = PopupMonitor(popup_cfg) if popup_cfg else None
    foreground = None
    samples = open(rd / "samples.csv", "w", newline="")
    sw = csv.writer(samples)
    sw.writerow(["t", "scope_current_mib", "scope_swap_mib", "file_dirty_mib", "file_writeback_mib",
                 "some_total_us", "full_total_us", "some_avg10", "full_avg10", "alive",
                 "zswap_pool_mib", "zswapped_mib"] +
                [f"{n}_mib" for n in names])
    lru = []          # killable apps, least recently used first
    killable_roles = ("background", "resident", "launch") + (("peak",) if sc.get("peak_plan") else ())
    pending_clicks = []   # (time, app, adj) for staggered clicks inside a launch group
    launched, op_log, hint_log, relaunches = set(), [], [], []
    generation = {n: 0 for n in names}
    next_sample = 0.0
    i = 0
    while True:
        now = boottime() - t0
        if now >= duration:
            break
        if i < len(actions) and actions[i][0] <= now:
            alive = alive_apps(launched)
        while i < len(actions) and actions[i][0] <= now:
            _, kind, obj = actions[i]
            i += 1
            if kind == "hint":
                cmd = [str(ctl), "--file", str(rd / "control.conf"), "--boost-mb", str(obj["boost_mib"]),
                       "--ttl", str(obj["ttl_s"])]
                if "targets_mib" in obj:
                    cmd += ["--peak-targets", ",".join(f"{k}:{v}" for k, v in obj["targets_mib"].items())]
                r = sh(cmd, check=False)
                hint_log.append({**obj, "sent_s": round(now, 2), "rc": r.returncode})
                continue
            e = obj
            foreground = e["apps"][-1]
            if e["op"] == "launch":
                for n in e["apps"]:
                    launch(apps[n], 0)
                    launched.add(n)
                    if apps[n]["role"] in killable_roles:
                        lru = [x for x in lru if x != n] + [n]
            elif e["op"] == "launch_group":
                # The user clicks every app of the group; the last one ends up in front.
                if args.mode != "off":
                    sh([str(ctl), "--file", str(rd / "control.conf"), "--protect",
                        ",".join(peak_names + e["apps"])], check=False)
                gap = e.get("click_gap_s", 0)
                for k, n in enumerate(e["apps"]):
                    fg = k == len(e["apps"]) - 1
                    adj_value = apps[n].get("fg_adj", 100) if fg else apps[n].get("oom_adj", 700)
                    pending_clicks.append((now + k * gap, n, adj_value))
            elif e["op"] == "peak":
                for n in e["apps"]:
                    sh(["systemctl", "--user", "kill", "-s", "SIGUSR2", unit(n)], check=False)
            elif e["op"] == "return":
                if args.mode != "off" and sc.get("cases"):
                    sh([str(ctl), "--file", str(rd / "control.conf"), "--protect",
                        ",".join(peak_names) or "none"], check=False)
                for n in e["apps"]:
                    if n in alive:
                        sh(["systemctl", "--user", "kill", "-s", "SIGUSR1", unit(n)], check=False)
                    else:
                        generation[n] += 1
                        launch(apps[n], generation[n])
                        relaunches.append({"t_s": round(now, 2), "app": n})
                    lru = [x for x in lru if x != n] + [n]
            op_log.append({"t_s": round(now, 2), **e})
        while pending_clicks and pending_clicks[0][0] <= now:
            _, n, adj_value = pending_clicks.pop(0)
            generation[n] += 1
            launch(apps[n], generation[n], adj_value)
            launched.add(n)
            lru = [x for x in lru if x != n] + [n]
            foreground = n
        if killer:
            killer.poll(boottime(), lru, alive_apps(launched), cur_adj)
        if popup:
            popup.poll(boottime(), foreground)
        if now >= next_sample:
            alive = alive_apps(launched)
            st = kv(read(SCOPE / "memory.stat"))
            p = psi(read(SCOPE / "memory.pressure"))
            row = [round(now, 2), int(read(SCOPE / "memory.current", "0") or 0) // MiB,
                   int(read(SCOPE / "memory.swap.current", "0") or 0) // MiB,
                   st.get("file_dirty", 0) // MiB, st.get("file_writeback", 0) // MiB,
                   p.get("some_total", 0), p.get("full_total", 0), p.get("some_avg10", 0), p.get("full_avg10", 0),
                   len(alive), st.get("zswap", 0) // MiB, st.get("zswapped", 0) // MiB]
            row += [int(read(app_cg(n) / "memory.current", "0") or 0) // MiB if n in alive else "" for n in names]
            sw.writerow(row)
            samples.flush()
            next_sample = now + 1.0
        time.sleep(0.1)

    end_scope = {**kv(read(SCOPE / "memory.events")), **psi(read(SCOPE / "memory.pressure"))}
    end_stat = kv(read(SCOPE / "memory.stat"))
    swap_peak = int(read(SCOPE / "memory.swap.peak", "0") or 0) // MiB
    if daemon:
        daemon.send_signal(signal.SIGINT)
        try:
            daemon.wait(timeout=60)
        except subprocess.TimeoutExpired:
            daemon.kill()
        dlog.close()
    samples.close()
    kkills, drops = kernel_log(t0_unix)
    cleanup_units()
    shutil.rmtree(data, ignore_errors=True)

    for k in kkills:
        k["t_s"] = round(k["t_unix"] - t0_unix, 2)
    policy_kills = killer.kills if killer else []
    for k in policy_kills:
        k["t_s"] = round(k.pop("t") - t0, 2)
        k["source"] = "policy_kill"
    for k in kkills:
        k["source"] = "kernel_oom"
    drops_s = [round(d - t0_unix, 1) for d in drops]
    popups = popup.popups if popup else []
    for pp in popups:
        pp["t_s"] = round(pp.pop("t") - t0, 2)

    # Per-app outcome and compare_runs rows (one row per app per round).
    app_events = {n: parse_app_logs(rd / "apps" / f"{n}.log") for n in names}
    for evs in app_events.values():
        for ev in evs:
            ev["t_s"] = round(ev["t"] - t0, 2)
    apps_out, rows_kernel, rows_total = [], [], []
    for idx, n in enumerate(names, 1):
        evs = app_events[n]
        kk = sum(1 for k in kkills if k["app"] == n)
        pops = sum(1 for pp in popups if pp["foreground"] == n)
        pk = sum(1 for k in policy_kills if k["app"] == n)
        ready = [float(e["ms"]) for e in evs if e["event"] == "DAILY_READY"]
        peaks = [e for e in evs if e["event"] == "PEAK_DONE"]
        begins = sum(1 for e in evs if e["event"] == "PEAK_BEGIN")
        attempts = startup_attempts(evs, n, kkills + policy_kills) if sc.get("cases") else []
        if sc.get("cases"):
            fail = sum(1 for x in attempts if x["outcome"] == "startup_failure")
            oks = [x["ms"] for x in attempts if x["outcome"] == "ready"]
            lat = max(oks) if oks else (ready[0] if ready else 0.0)
        elif apps[n]["role"] == "peak" and sc.get("peak_plan"):
            # Allocation timeout: the peak finished late, or never finished although the app
            # was not killed (a kill is already counted as a kill).
            killed_n = any(k["app"] == n for k in kkills + policy_kills)
            fail = sum(1 for e in peaks if e.get("timeout") == "1") + \
                (0 if killed_n else max(0, begins - len(peaks)))
            lat = max((float(e["ms"]) for e in peaks), default=None)
        elif apps[n]["role"] == "peak":
            fail = begins - sum(1 for e in peaks if e.get("timeout") == "0")
            lat = max((float(e["ms"]) for e in peaks), default=None)
        else:
            fail = 0 if ready else 1
            lat = ready[0] if ready else None
        touches = [float(e["ms"]) for e in evs if e["event"] == "TOUCH_DONE"]
        harmony = bool(sc.get("cases"))
        apps_out.append({"app": n, "role": apps[n]["role"], "kernel_oom_kills": kk, "policy_kills": pk, "popups": pops,
                         "peak_fail": fail if (apps[n]["role"] == "peak" or harmony) else 0,
                         "peak_ms": lat if apps[n]["role"] in ("peak", "launch") else None,
                         "startup_attempts": attempts,
                         "touch_ms": touches, "relaunches": sum(1 for r in relaunches if r["app"] == n)})
        if harmony:
            f = fail
            lat_s = f"{lat:.0f}" if not fail or lat else ""
            lat_s = lat_s or "0"
        else:
            lat_s = "" if (fail or lat is None) else f"{lat:.0f}"
            f = 1 if (fail or lat is None) else 0
        rows_kernel.append([args.round_id, idx, n, f, pops, kk, lat_s])
        rows_total.append([args.round_id, idx, n, f, pops, kk + pk, lat_s])
    for fname, rows in (("compare_kernel.csv", rows_kernel), ("compare_total.csv", rows_total)):
        with open(rd / fname, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["run_id", "sequence", "case_id", "launch_fail", "lowmem_popup", "oom_events", "launch_ms"])
            w.writerows(rows)

    # Per-peak-event view: kills, stall and latency attributable to each peak.
    rows = reclaim_rows(rd / "memshiftd")
    sample_rows = list(csv.DictReader(open(rd / "samples.csv")))

    def stall_between(a, b):
        pts = [(float(r["t"]), int(r["some_total_us"]), int(r["full_total_us"])) for r in sample_rows
               if a <= float(r["t"]) <= b]
        if len(pts) < 2:
            return 0, 0
        return round((pts[-1][1] - pts[0][1]) / 1000), round((pts[-1][2] - pts[0][2]) / 1000)

    events_out = []
    # Aligned-peak scenarios are summarized by phase instead of per peak op.
    peaks_ops = [] if sc.get("peak_plan") else [e for e in timeline if e["op"] in ("peak", "launch_group")]
    all_attempts = [dict(x, app=a["app"]) for a in apps_out for x in a.get("startup_attempts", [])]
    for j, e in enumerate(peaks_ops):
        if e["op"] == "launch_group":
            gap = e.get("click_gap_s", 0)
            hold = max(k * gap + apps[n].get("peak_after_s", 0) + apps[n].get("hold_s", 0)
                       for k, n in enumerate(e["apps"])) + 3
        else:
            hold = max(apps[n].get("hold_s", 0) for n in e["apps"])
        win_end = e["t"] + hold + 10
        nxt = [x["t"] for x in timeline if x["t"] > e["t"] and x["op"] not in ("peak",)]
        win_end = min(win_end, nxt[0]) if nxt else win_end
        some, full = stall_between(e["t"] - 1, win_end)
        lats = [float(ev["ms"]) for n in e["apps"] for ev in app_events[n]
                if ev["event"] == "PEAK_DONE" and e["t"] - 1 <= ev["t_s"] <= win_end]
        prev_t = peaks_ops[j - 1]["t"] if j else 0
        pre = [r for r in rows if r["status"] == "ok" and r.get("unix_ms") and
               prev_t + 1 <= (int(r["unix_ms"]) / 1000 - t0_unix) < e["t"]]
        events_out.append({
            "t": e["t"], "apps": e["apps"],
            "kernel_kills": sum(1 for k in kkills if e["t"] - 1 <= k["t_s"] <= win_end),
            "policy_kills": sum(1 for k in policy_kills if e["t"] - 1 <= k["t_s"] <= win_end),
            "popups": sum(1 for pp in popups if e["t"] - 1 <= pp["t_s"] <= win_end),
            "startup_failures": sum(1 for x in all_attempts if x["outcome"] == "startup_failure"
                                    and e["t"] - 1 <= x["t_s"] <= win_end),
            "case": e.get("case"),
            "stall_some_ms": some, "stall_full_ms": full,
            "peak_ms_max": max(lats) if lats else None,
            "reclaimed_before_mib": round(-sum(float(r["current_change"] or 0) for r in pre) / MiB, 1),
            "drop_caches_in_window": any(e["t"] - 5 <= d <= win_end for d in drops_s),
        })

    aligned = aligned_summary(sc, hints, kkills, policy_kills, popups, app_events, apps_out, relaunches,
                              rows, sample_rows, drops_s, rd, duration, t0_unix) if sc.get("peak_plan") else None
    if aligned:
        events_out = aligned["phases"]
    touch_all = [t for a in apps_out for t in a["touch_ms"]]
    summary = {
        "scenario": sc["name"], "mode": args.mode, "round_id": args.round_id, "tag": args.tag,
        "lead_s": args.lead, "error": args.error, "policy_kill": policy, "duration_s": duration,
        "contract_checks": checks,
        "kills": {"kernel_oom": len(kkills), "policy": len(policy_kills), "total": len(kkills) + len(policy_kills)},
        "popups": len(popups), "popup_cfg": popup_cfg,
        "events_total": len(kkills) + len(policy_kills) + len(popups) + sum(a["peak_fail"] for a in apps_out),
        # Main metric of the aligned-peak experiment: kills + peak allocation timeouts (no popups).
        "oom_events": len(kkills) + len(policy_kills) + sum(a["peak_fail"] for a in apps_out),
        "startup_failures": sum(1 for x in all_attempts if x["outcome"] == "startup_failure"),
        "startup_timeouts": sum(1 for x in all_attempts if x["outcome"] == "startup_timeout"),
        "unique_incidents": len(kkills) + len(policy_kills) + len(popups) +
                            sum(1 for x in all_attempts if x["outcome"] == "startup_failure" and x.get("cause") == "enomem"),
        "relaunches": len(relaunches),
        "peak_fail": sum(a["peak_fail"] for a in apps_out),
        "scope_events_delta": {k: end_scope.get(k, 0) - base_scope.get(k, 0) for k in ("oom", "oom_kill", "max")},
        "psi_ms": {"some": round((end_scope.get("some_total", 0) - base_scope.get("some_total", 0)) / 1000),
                   "full": round((end_scope.get("full_total", 0) - base_scope.get("full_total", 0)) / 1000)},
        "pswpout_mib": round((end_stat.get("pswpout", 0) - base_stat.get("pswpout", 0)) * 4096 / MiB),
        "pswpin_mib": round((end_stat.get("pswpin", 0) - base_stat.get("pswpin", 0)) * 4096 / MiB),
        "zswap": {k: end_stat.get(k, 0) - base_stat.get(k, 0) for k in ("zswpout", "zswpin", "zswpwb")},
        "zswap_pool_peak_mib": max((int(r["zswap_pool_mib"]) for r in sample_rows), default=0),
        "zswapped_peak_mib": max((int(r["zswapped_mib"]) for r in sample_rows), default=0),
        "swap_peak_mib": swap_peak,
        "touch_ms": {"n": len(touch_all), "mean": round(sum(touch_all) / len(touch_all)) if touch_all else None,
                     "max": max(touch_all) if touch_all else None},
        "memshiftd_reclaim": reclaim_summary(rows),
        "events": events_out,
        "hints": hint_log, "ops": op_log, "relaunch_log": relaunches,
        "kernel_kills": kkills, "policy_kills": policy_kills, "popup_log": popups,
        "wsl_drop_caches_s": drops_s, "apps": apps_out,
    }
    if aligned:
        summary["aligned"] = {k: v for k, v in aligned.items() if k != "phases"}
    (rd / "summary.json").write_text(json.dumps(summary, indent=1))
    brief = {k: summary[k] for k in ("scenario", "mode", "round_id", "tag", "kills", "popups", "events_total", "oom_events",
                                     "startup_failures", "startup_timeouts", "relaunches", "peak_fail",
                                     "psi_ms", "pswpout_mib", "touch_ms")}
    brief["events"] = [{k: e[k] for k in ("t", "policy_kills", "kernel_kills", "popups", "startup_failures",
                                          "stall_some_ms", "peak_ms_max",
                                          "reclaimed_before_mib", "drop_caches_in_window")} for e in events_out]
    brief["reclaim_ok_mib"] = summary["memshiftd_reclaim"]["submitted_mib"]
    print(json.dumps(brief))


if __name__ == "__main__":
    main()
