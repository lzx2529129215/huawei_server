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
    return a.get("anon_mib", 0) + a.get("clean_mib", 0) + a.get("dirty_mib", 0) + 4


def contract_checks(sc):
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
                  f"reclaim={'true' if a['role'] == 'background' else 'false'}",
                  f"min_keep_mb={a.get('min_keep_mb', 32)}"]
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

    def poll(self, now, lru, alive):
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
        if some_ms < self.cfg.get("some_ms", 1e9) and full_ms < self.cfg.get("full_ms", 1e9):
            return None
        victims = [n for n in lru if n in alive]
        if not victims:
            return None
        victim = victims[0]
        sh(["systemctl", "--user", "kill", "-s", "SIGKILL", unit(victim)], check=False)
        self.last_kill = now
        self.hist.clear()
        rec = {"t": now, "app": victim, "some_ms": round(some_ms, 1), "full_ms": round(full_ms, 1)}
        self.kills.append(rec)
        return victim


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
    ap.add_argument("--allow-contract-fail", action="store_true")
    args = ap.parse_args()

    sc = json.loads(Path(args.scenario).read_text())
    for item in args.set:
        k, _, v = item.partition("=")
        sc.setdefault("memshiftd", {})[k] = v
    policy = sc.get("policy_kill")
    if args.policy_kill is not None:
        policy = json.loads(args.policy_kill)
    checks = contract_checks(sc)
    if not all(v for k, v in checks.items() if k.endswith(("_M", "_1_2M"))) and not args.allow_contract_fail:
        sys.exit(f"contract checks failed: {checks}")
    apps = {a["name"]: a for a in sc["apps"]}
    names = [a["name"] for a in sc["apps"]]
    timeline = sorted(sc["timeline"], key=lambda e: e["t"])
    duration = sc["duration_s"]

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
    sh(["systemctl", "--user", "set-property", "--runtime", "memlab.slice",
        f"MemoryMax={m}M", f"MemorySwapMax={s}M"])
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
    if args.lead is not None and args.mode != "off":
        cap = int(sc.get("memshiftd", {}).get("max_boost_mb", 4096))
        for e in timeline:
            if e["op"] != "peak":
                continue
            members = [apps[n] for n in e["apps"]]
            need = sum(x["peak_mib"] for x in members) * (1 + args.error)
            h = {"at_s": max(0.0, e["t"] - args.lead), "peak_at_s": e["t"],
                 "boost_mib": min(cap, max(1, int(math.ceil(need)))),
                 "ttl_s": int(min(300, args.lead + max(x.get("hold_s", 0) for x in members) + 15))}
            hints.append(h)
            actions.append((h["at_s"], "hint", h))
    actions.sort(key=lambda x: (x[0], x[1] != "hint"))

    t0, t0_unix = boottime(), time.time()
    base_scope = {**kv(read(SCOPE / "memory.events")), **psi(read(SCOPE / "memory.pressure"))}
    base_stat = kv(read(SCOPE / "memory.stat"))

    def launch(a, gen):
        cmd = ["systemd-run", "--user", "-q", f"--unit={unit(a['name'])}", "--slice=memlab",
               "-p", f"OOMScoreAdjust={a.get('oom_adj', 200)}", str(BUILD / "memlab_app"),
               "--name", a["name"], "--log", str(rd / "apps" / f"{a['name']}.log"),
               "--data-dir", str(data), "--anon-mib", str(a.get("anon_mib", 0)),
               "--clean-mib", str(a.get("clean_mib", 0)), "--dirty-mib", str(a.get("dirty_mib", 0)),
               "--dirty-rate", str(a.get("dirty_rate_mib_s", 0)), "--ttl", str(duration + 5),
               "--seed", str(a.get("seed", zlib.crc32(a["name"].encode()) % 100000 + gen))]
        if a.get("peak_mib"):
            cmd += ["--peak-mib", str(a["peak_mib"]), "--peak-rate", str(a.get("peak_rate_mib_s", 0)),
                    "--hold", str(a.get("hold_s", 0)), "--peak-deadline", str(a.get("peak_deadline_s", 30))]
        # A killed unit stays failed until reset; clear it so the name can be reused.
        sh(["systemctl", "--user", "reset-failed", unit(a["name"])], check=False)
        return sh(cmd, check=False).returncode

    killer = PolicyKiller(policy) if policy else None
    samples = open(rd / "samples.csv", "w", newline="")
    sw = csv.writer(samples)
    sw.writerow(["t", "scope_current_mib", "scope_swap_mib", "file_dirty_mib", "file_writeback_mib",
                 "some_total_us", "full_total_us", "some_avg10", "full_avg10", "alive"] +
                [f"{n}_mib" for n in names])
    lru = []          # background apps, least recently used first
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
                r = sh([str(ctl), "--file", str(rd / "control.conf"), "--boost-mb", str(obj["boost_mib"]),
                        "--ttl", str(obj["ttl_s"])], check=False)
                hint_log.append({**obj, "sent_s": round(now, 2), "rc": r.returncode})
                continue
            e = obj
            if e["op"] == "launch":
                for n in e["apps"]:
                    launch(apps[n], 0)
                    launched.add(n)
                    if apps[n]["role"] == "background":
                        lru = [x for x in lru if x != n] + [n]
            elif e["op"] == "peak":
                for n in e["apps"]:
                    sh(["systemctl", "--user", "kill", "-s", "SIGUSR2", unit(n)], check=False)
            elif e["op"] == "return":
                for n in e["apps"]:
                    if n in alive:
                        sh(["systemctl", "--user", "kill", "-s", "SIGUSR1", unit(n)], check=False)
                    else:
                        generation[n] += 1
                        launch(apps[n], generation[n])
                        relaunches.append({"t_s": round(now, 2), "app": n})
                    lru = [x for x in lru if x != n] + [n]
            op_log.append({"t_s": round(now, 2), **e})
        if killer:
            killer.poll(boottime(), lru, alive_apps(launched))
        if now >= next_sample:
            alive = alive_apps(launched)
            st = kv(read(SCOPE / "memory.stat"))
            p = psi(read(SCOPE / "memory.pressure"))
            row = [round(now, 2), int(read(SCOPE / "memory.current", "0") or 0) // MiB,
                   int(read(SCOPE / "memory.swap.current", "0") or 0) // MiB,
                   st.get("file_dirty", 0) // MiB, st.get("file_writeback", 0) // MiB,
                   p.get("some_total", 0), p.get("full_total", 0), p.get("some_avg10", 0), p.get("full_avg10", 0),
                   len(alive)]
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
    drops_s = [round(d - t0_unix, 1) for d in drops]

    # Per-app outcome and compare_runs rows (one row per app per round).
    app_events = {n: parse_app_logs(rd / "apps" / f"{n}.log") for n in names}
    for evs in app_events.values():
        for ev in evs:
            ev["t_s"] = round(ev["t"] - t0, 2)
    apps_out, rows_kernel, rows_total = [], [], []
    for idx, n in enumerate(names, 1):
        evs = app_events[n]
        kk = sum(1 for k in kkills if k["app"] == n)
        pk = sum(1 for k in policy_kills if k["app"] == n)
        ready = [float(e["ms"]) for e in evs if e["event"] == "DAILY_READY"]
        peaks = [e for e in evs if e["event"] == "PEAK_DONE"]
        begins = sum(1 for e in evs if e["event"] == "PEAK_BEGIN")
        if apps[n]["role"] == "peak":
            fail = begins - sum(1 for e in peaks if e.get("timeout") == "0")
            lat = max((float(e["ms"]) for e in peaks), default=None)
        else:
            fail = 0 if ready else 1
            lat = ready[0] if ready else None
        touches = [float(e["ms"]) for e in evs if e["event"] == "TOUCH_DONE"]
        apps_out.append({"app": n, "role": apps[n]["role"], "kernel_oom_kills": kk, "policy_kills": pk,
                         "peak_fail": fail if apps[n]["role"] == "peak" else 0, "peak_ms": lat if apps[n]["role"] == "peak" else None,
                         "touch_ms": touches, "relaunches": sum(1 for r in relaunches if r["app"] == n)})
        lat_s = "" if (fail or lat is None) else f"{lat:.0f}"
        f = 1 if (fail or lat is None) else 0
        rows_kernel.append([args.round_id, idx, n, f, 0, kk, lat_s])
        rows_total.append([args.round_id, idx, n, f, 0, kk + pk, lat_s])
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
    peaks_ops = [e for e in timeline if e["op"] == "peak"]
    for j, e in enumerate(peaks_ops):
        hold = max(apps[n].get("hold_s", 0) for n in e["apps"])
        win_end = e["t"] + hold + 10
        nxt = [x["t"] for x in timeline if x["t"] > e["t"] and x["op"] != "peak"]
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
            "stall_some_ms": some, "stall_full_ms": full,
            "peak_ms_max": max(lats) if lats else None,
            "reclaimed_before_mib": round(-sum(float(r["current_change"] or 0) for r in pre) / MiB, 1),
            "drop_caches_in_window": any(e["t"] - 5 <= d <= win_end for d in drops_s),
        })

    touch_all = [t for a in apps_out for t in a["touch_ms"]]
    summary = {
        "scenario": sc["name"], "mode": args.mode, "round_id": args.round_id, "tag": args.tag,
        "lead_s": args.lead, "error": args.error, "policy_kill": policy, "duration_s": duration,
        "contract_checks": checks,
        "kills": {"kernel_oom": len(kkills), "policy": len(policy_kills), "total": len(kkills) + len(policy_kills)},
        "relaunches": len(relaunches),
        "peak_fail": sum(a["peak_fail"] for a in apps_out),
        "scope_events_delta": {k: end_scope.get(k, 0) - base_scope.get(k, 0) for k in ("oom", "oom_kill", "max")},
        "psi_ms": {"some": round((end_scope.get("some_total", 0) - base_scope.get("some_total", 0)) / 1000),
                   "full": round((end_scope.get("full_total", 0) - base_scope.get("full_total", 0)) / 1000)},
        "pswpout_mib": round((end_stat.get("pswpout", 0) - base_stat.get("pswpout", 0)) * 4096 / MiB),
        "pswpin_mib": round((end_stat.get("pswpin", 0) - base_stat.get("pswpin", 0)) * 4096 / MiB),
        "swap_peak_mib": swap_peak,
        "touch_ms": {"n": len(touch_all), "mean": round(sum(touch_all) / len(touch_all)) if touch_all else None,
                     "max": max(touch_all) if touch_all else None},
        "memshiftd_reclaim": reclaim_summary(rows),
        "events": events_out,
        "hints": hint_log, "ops": op_log, "relaunch_log": relaunches,
        "kernel_kills": kkills, "policy_kills": policy_kills,
        "wsl_drop_caches_s": drops_s, "apps": apps_out,
    }
    (rd / "summary.json").write_text(json.dumps(summary, indent=1))
    brief = {k: summary[k] for k in ("scenario", "mode", "round_id", "tag", "kills", "relaunches", "peak_fail",
                                     "psi_ms", "pswpout_mib", "touch_ms")}
    brief["events"] = [{k: e[k] for k in ("t", "policy_kills", "kernel_kills", "stall_some_ms", "peak_ms_max",
                                          "reclaimed_before_mib", "drop_caches_in_window")} for e in events_out]
    brief["reclaim_ok_mib"] = summary["memshiftd_reclaim"]["submitted_mib"]
    print(json.dumps(brief))


if __name__ == "__main__":
    main()
