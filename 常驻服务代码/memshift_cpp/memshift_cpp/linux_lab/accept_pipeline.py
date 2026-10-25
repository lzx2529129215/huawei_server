#!/usr/bin/env python3
"""Unattended driver of the acceptance experiment (stage2_spec.md section 6).

Phases (each appends a Chinese line to 进度.md and sends a Windows toast; failures of the toast are ignored):
  checks     build the lab targets, ctest (watermark|linux_scope|policy|smoke); a short observe run and a
             short apply+hint run on tuning cases 1-2 (summary fields present, every app attempt has a
             terminal state); wl16 observe round 1 (events_total recorded only; earlier rounds gave 10-12)
  calibrate  run_accept.py --calibrate; regenerate the scenario with it (make_accept_cases.py
             --calibration -> OUT/<name>_cal.json, used by every later phase); stop when a check fails
  qualify    observe + hint on the whole candidate sequence; accept_tools.py qualify -> OUT/freeze.json;
             stop when it fails
  smoke      apply+hint and apply (no hint) on tuning cases 3-6; memshiftd must submit jobs, no errors
  formal     O1 A1 T1 O2 A2 T2 O3 A3 T3 (O observe+hint, A apply+hint, T apply without hint) on the first
             N cases of the freeze. An invalid run is rerun at once (at most 2 extra). T runs are dropped
             first when the projected end passes --deadline. A baseline < 100 or missing a type: extend
             to N' (O and A rerun, T not) if time allows, else record the failure.
  report     accept_tools.py compare -> OUT/report/report.{md,json}
  reference  optional (实验设置.md 0.1): if time is left, 1 observe+hint and 1 apply+hint run on the 20
             unfiltered R20 cases (scenario OUT/acceptR20_cal.json), compared only as a reference
             (OUT/report_R20/); it never stops the pipeline (--no-reference skips it)
The N rule (accept_tools qualify) includes the device-style term of 实验设置.md 0.1; 进度.md gets table rows.
Run it detached (nohup/setsid); SIGHUP is ignored, SIGTERM stops it cleanly (exit 2, resumable).
Resume: OUT/pipeline_state.json. Without --from the pipeline continues with the first phase not done
(inside formal, run by run). --from PHASE reruns PHASE and the later phases; finished formal runs of the
same freeze and scenario are kept unless --fresh-formal. --dry-run prints the phases and commands only.
Before every run: MemFree guard (< 17 GiB -> a balloon process allocates and touches
min(MemAvailable - 1 GiB, 20 GiB) and exits, then re-check; still low -> run anyway with
--allow-low-memfree and log it).
Exit 0 finished, 2 stopped for a human (the reason is in pipeline.log, 进度.md and the state), 1 usage error.

Usage:
  accept_pipeline.py --scenario linux_lab/scenarios/acceptS130.json \
      --out results/linux_oom_lab_accept_20261024/runs --deadline 2026-10-25T19:00 [--from PHASE] [--dry-run]
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

LAB = Path(__file__).resolve().parent
CODE = LAB.parent
REPO = LAB.parents[3]
PY = sys.executable or "python3"
NOTIFY = REPO / "tools" / "linux_app_mem" / "notify_windows.py"
DATA = REPO / "results" / "linux_oom_lab_accept_20261024" / "data"
PHASES = ("checks", "calibrate", "qualify", "smoke", "formal", "report", "reference")
PHASE_CN = {"checks": "检查", "calibrate": "校准", "qualify": "资格轮", "smoke": "冒烟", "formal": "正式运行",
            "report": "汇总", "reference": "参考用例 R20", "pipeline": "流水线"}
VARIANTS = {"O": ("observe", "on", "obs"), "A": ("apply", "on", "tmoh"), "T": ("apply", "off", "tmo")}
VARIANT_CN = {"O": "observe+预告", "A": "apply-TMO+预告", "T": "apply-TMO（不发预告）"}
FORMAL_ORDER = ("O1", "A1", "T1", "O2", "A2", "T2", "O3", "A3", "T3")
BUILD_TARGETS = ("memshiftd", "memshiftctl", "memlab_app", "compare_runs", "test_watermark", "test_linux_scope",
                 "test_policy")
CTEST_REGEX = "watermark|linux_scope|policy|smoke"
GiB_KIB = 1024 * 1024
MEMFREE_MIN_KIB = 17 * GiB_KIB
BALLOON_MAX_MIB = 20 * 1024
RUN_OVERHEAD_S = 180          # assumption: daemon start, teardown and log parsing per run
DEFAULT_CASE_S = 90.0         # assumption until a run has been measured (1-s ramps, 15 s case gap)
CASE_TIMEOUT_S = 200          # per-case bound used for a run's timeout (worst case about 160 s)
STALL_S = 900                 # hung: progress.json (rewritten after every case) unchanged this long
REPORT_S = 120
MAX_EXTRA = 2                 # reruns of an invalid run
TIMEOUT_RC = -999
TARGET = 100
TERMINAL = {"success", "confirmed_memory_failure", "unconfirmed"}
NOT_TERMINAL = {"", "running", "pending", "unknown", "none", "null"}
SUMMARY_KEYS = ("scenario", "sequence", "sequence_sha256", "case_set", "case_ids", "mode", "hint", "label", "run_id",
                "n_cases", "totals", "types_present", "unconfirmed", "valid", "invalid_reasons", "validity",
                "memshiftd", "durations", "cases")
TOTAL_KEYS = ("category_total", "kills", "policy_kills", "kernel_oom", "popups", "startup_failures", "unique",
              "device_style", "shadow", "startup_timeouts")
TYPE_KEYS = ("startup_failure", "popup", "oom_kill")

# A separate process so its memory is returned when it exits. Stops early when MemAvailable would fall
# below the floor, so it never pushes the VM into an OOM.
BALLOON = r"""
import mmap, sys
target, floor = int(sys.argv[1]) << 20, int(sys.argv[2]) << 20
chunk, blk = 256 << 20, b"\x01" * (1 << 20)
def avail():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) << 10
    return 0
held, total = [], 0
while total + chunk <= target and avail() - chunk > floor:
    m = mmap.mmap(-1, chunk, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    for _ in range(chunk // len(blk)):
        m.write(blk)
    held.append(m)
    total += chunk
print(total >> 20)
"""


class Stop(Exception):
    """Stop and wait for a human; the message is Chinese and goes to 进度.md."""


class Interrupted(Exception):
    pass


def now_s():
    return time.time()


def stamp(t=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t or now_s()))


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, TypeError):
        return None


def write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                parts = v.split()
                if parts and parts[0].isdigit():
                    out[k] = int(parts[0])
    except OSError:
        pass
    return out


def to_int(x, default=0):
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        return default


def hours(s):
    return f"{s / 3600:.1f} h"


def resolve_input(p):
    """An input path as given, else relative to the repo / code / lab directories."""
    p = Path(p)
    for c in ([p] if p.is_absolute() else [p, REPO / p, CODE / p, LAB / p]):
        if c.exists():
            return c.resolve()
    return p.resolve()


def resolve_out(p):
    """--out as given; a relative path whose parent exists only under the repo root goes there
    (the spec's example results/... is repo-relative)."""
    p = Path(p)
    if p.is_absolute() or p.resolve().parent.is_dir() or not (REPO / p).parent.is_dir():
        return p.resolve()
    return (REPO / p).resolve()


def sha256_file(path):
    import hashlib
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


class Pipeline:
    def __init__(self, a):
        self.a = a
        self.dry = a.dry_run
        self.out = resolve_out(a.out)
        self.scenario = resolve_input(a.scenario)
        self.build = Path(a.build_dir).resolve() if a.build_dir else CODE / "build"
        self.progress_path = Path(a.progress).resolve() if a.progress else self.out.parent / "进度.md"
        self.state_path = self.out / "pipeline_state.json"
        self.deadline = self.parse_deadline(a.deadline)
        self.runner = Path(a.runner).resolve() if a.runner else LAB / "run_accept.py"
        self.round_runner = Path(a.round_runner).resolve() if a.round_runner else LAB / "run_round.py"
        self.ctest = a.ctest or "ctest"
        # Substitute runners are for testing the pipeline: never touch systemd units then.
        self.real = not (a.runner or a.round_runner)
        self.child = None
        self.crashes_in_row = 0
        self.crashed_attempts = []
        self.state = self.load_state()

    # ------------------------------------------------------------ basics
    @staticmethod
    def parse_deadline(s):
        try:
            d = dt.datetime.fromisoformat(s)
        except ValueError:
            raise SystemExit(f"bad --deadline {s!r}; use e.g. 2026-10-25T19:00 (local time)")
        return d.timestamp()          # naive = local time

    def load_state(self):
        st = read_json(self.state_path) if not self.a.fresh else None
        if not isinstance(st, dict) or st.get("version") != 1:
            st = {"version": 1, "created": stamp(), "phases": {}, "runs": [], "formal": None,
                  "checks": {}, "stop_reason": None}
        st.setdefault("phases", {})
        st.setdefault("runs", [])
        st.setdefault("checks", {})
        return st

    def save(self):
        if self.dry:
            return
        self.state["updated"] = stamp()
        self.state["scenario"] = str(self.scenario)
        self.state["deadline"] = self.a.deadline
        write_atomic(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=1) + "\n")

    def log(self, msg):
        line = f"[{stamp()}] {msg}"
        try:
            print(line, flush=True)
        except (OSError, ValueError):     # the terminal went away; the log file still gets it
            pass
        if not self.dry:
            try:
                with open(self.out / "pipeline.log", "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def progress(self, phase, msg, toast=True):
        """One Chinese row in the 进度.md table (时间 | 做了什么 | 结果 / 下一步) plus a toast (both best effort)."""
        self.log(f"progress [{phase}] {msg}")
        if self.dry:
            return
        head = "| 时间 | 做了什么 | 结果 / 下一步 |\n|---|---|---|\n"
        cell = " ".join(str(msg).split()).replace("|", "／")
        row = f"| {time.strftime('%m-%d %H:%M')} | {PHASE_CN.get(phase, phase)}（流水线自动） | {cell} |\n"
        try:
            text = self.progress_path.read_text(encoding="utf-8") if self.progress_path.exists() else None
            with open(self.progress_path, "a", encoding="utf-8") as f:
                if text is None:
                    f.write("# 验收实验进度（accept_pipeline.py 自动记录）\n\n" + head)
                else:
                    lines = [x for x in text.splitlines() if x.strip()]
                    if not lines or not lines[-1].lstrip().startswith("|"):
                        f.write(("" if text.endswith("\n") else "\n") + "\n" + head)   # start a table
                    elif not text.endswith("\n"):
                        f.write("\n")
                f.write(row)
        except OSError as e:
            self.log(f"cannot write {self.progress_path}: {e}")
        if toast and not self.a.no_notify:
            try:
                subprocess.run([PY, str(NOTIFY), f"验收实验：{PHASE_CN.get(phase, phase)}", msg[:240]],
                               capture_output=True, timeout=90)
            except (OSError, subprocess.SubprocessError):
                pass

    def sh(self, cmd, timeout):
        """Short command: (rc, stdout + stderr). Never raises for a failing command."""
        try:
            r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return TIMEOUT_RC, f"timeout after {timeout} s"
        except OSError as e:
            return 127, str(e)

    def run_logged(self, name, cmd, timeout, cleanup=False, watch=None, stall_s=None):
        """Long command with output to OUT/logs/<name>.log, in its own process group so a timeout kills
        everything it started (memshiftd included). With watch/stall_s the run also counts as hung when the
        watch file (run_accept.py rewrites progress.json after every case) has not changed for stall_s.
        Returns the exit code or TIMEOUT_RC."""
        cmd = [str(c) for c in cmd]
        logf = self.out / "logs" / f"{name}.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        self.log(f"run {name} (timeout {timeout} s): {shlex.join(cmd)}")
        with open(logf, "a", encoding="utf-8") as f:
            f.write(f"\n# {stamp()} {shlex.join(cmd)}\n")
            f.flush()
            try:
                self.child = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                              start_new_session=True, cwd=str(LAB))
            except OSError as e:
                f.write(f"cannot start: {e}\n")
                self.log(f"cannot start {name}: {e}")
                return 127
            t_start = now_s()
            t_end = t_start + timeout
            try:
                while self.child.poll() is None:
                    t = now_s()
                    why = f"timeout after {timeout} s" if t > t_end else None
                    if why is None and watch is not None and stall_s:
                        try:
                            last = max(t_start, Path(watch).stat().st_mtime)
                        except OSError:
                            last = t_start
                        if t - last > stall_s:
                            why = f"no progress for {stall_s} s ({Path(watch).name})"
                    if why:
                        self.log(f"{name}: {why}, stopping it")
                        self.kill_child()
                        if cleanup:
                            self.cleanup_units()
                        f.write(f"# killed: {why}\n")
                        return TIMEOUT_RC
                    time.sleep(0.5)
            except (KeyboardInterrupt, Interrupted):
                self.kill_child()
                if cleanup:
                    self.cleanup_units()
                raise
            rc = self.child.returncode
            self.child = None
            f.write(f"# exit {rc}\n")
            return rc

    def kill_child(self):
        p = self.child
        if p is None or p.poll() is not None:
            self.child = None
            return
        for sig, wait in ((signal.SIGINT, 60), (signal.SIGTERM, 20), (signal.SIGKILL, 10)):
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                p.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        self.child = None

    def cleanup_units(self):
        """After a killed run: stop the app units it may have left (same commands as run_round.cleanup_units)."""
        if not self.real:
            self.log("cleanup skipped (substitute runner)")
            return
        for cmd in (["systemctl", "--user", "stop", "memlab-*.service"],
                    ["systemctl", "--user", "reset-failed", "memlab-*.service"],
                    ["systemctl", "--user", "stop", "memlab.slice"]):
            rc, out = self.sh(cmd, timeout=120)
            self.log(f"cleanup {' '.join(cmd[2:])}: rc {rc} {out.strip()[:200]}")

    # ------------------------------------------------------------ memory guard
    def memfree_guard(self):
        """True when the run must be started with --allow-low-memfree."""
        mi = meminfo()
        free = mi.get("MemFree", 0)
        if free >= MEMFREE_MIN_KIB:
            return False
        avail = mi.get("MemAvailable", 0)
        target = min((avail - GiB_KIB) // 1024, BALLOON_MAX_MIB, self.a.balloon_max_mib)
        self.log(f"MemFree {free / GiB_KIB:.1f} GiB < 17 GiB (MemAvailable {avail / GiB_KIB:.1f} GiB): "
                 f"balloon of {target} MiB")
        if target >= 256:
            rc, out = self.sh([PY, "-c", BALLOON, str(int(target)), "1024"], timeout=600)
            self.log(f"balloon rc {rc}, allocated {out.strip()[-20:]} MiB")
            time.sleep(2)
        free2 = meminfo().get("MemFree", 0)
        if free2 >= MEMFREE_MIN_KIB:
            self.log(f"MemFree now {free2 / GiB_KIB:.1f} GiB")
            return False
        self.log(f"MemFree still {free2 / GiB_KIB:.1f} GiB after the balloon: running anyway (--allow-low-memfree)")
        self.state.setdefault("low_memfree_runs", 0)
        self.state["low_memfree_runs"] += 1
        return True

    # ------------------------------------------------------------ scenario and commands
    def scenario_for(self, phase):
        if phase in ("checks", "calibrate"):
            return self.scenario
        p = self.state.get("scenario_run")
        if p and Path(p).is_file():
            return Path(p)
        guess = self.cal_scenario_path()
        if guess.is_file():
            return guess
        if self.dry:
            return guess
        raise Stop(f"找不到校准后的场景文件 {guess}，要先跑 calibrate 阶段")

    def cal_scenario_path(self):
        return self.out / f"{self.scenario.stem}_cal.json"

    @staticmethod
    def case_count(scenario_path, case_set, first=None, cases=None):
        sc = read_json(scenario_path) or {}
        total = len(sc.get("tuning_cases" if case_set == "tuning" else "cases") or []) or (24 if case_set == "tuning" else 72)
        start = (first or 1) - 1
        n = max(0, total - start)
        return min(n, cases) if cases else n

    def cmd_accept(self, scenario, variant, label, run_id, out, case_set="cases", first=None, cases=None,
                   allow_low=False):
        mode, hint, _tag = VARIANTS[variant]
        cmd = [PY, self.runner, "--scenario", scenario, "--mode", mode, "--hint", hint, "--label", label,
               "--run-id", run_id, "--out", out, "--case-set", case_set]
        # --first K is the 1-based index of the first case of the set, --cases N the number of cases.
        if first is not None:
            cmd += ["--first", first]
        if cases is not None:
            cmd += ["--cases", cases]
        if allow_low:
            cmd.append("--allow-low-memfree")
        return [str(c) for c in cmd]

    @staticmethod
    def other_runs():
        """PIDs of other live run_accept.py / run_round.py processes (not dry runs)."""
        out = []
        for p in Path("/proc").iterdir():
            if not p.name.isdigit() or int(p.name) == os.getpid():
                continue
            try:
                args = (p / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            text = b" ".join(args)
            if (b"run_accept.py" in text or b"run_round.py" in text) and not any(
                    x in text for x in (b"--dry-run", b"--self-test", b"--check-memfree", b"--help")):
                if args and b"python" in args[0]:
                    out.append(int(p.name))
        return out

    def wait_other_runs(self, max_s=4 * 3600):
        """After exit 5: if another run holds the slice, wait (bounded) until it is gone. True = retry."""
        pids = self.other_runs()
        if not pids:
            return False
        self.log(f"another run is still going (pids {pids}); waiting up to {hours(max_s)} for it to finish")
        self.progress("pipeline", f"memlab.slice 还被之前的运行占着（pid {pids}），等它跑完再接着跑", toast=False)
        t_end = now_s() + max_s
        while now_s() < t_end:
            time.sleep(30)
            if not self.other_runs():
                self.log("the other run is gone; retrying")
                return True
        return False

    @staticmethod
    def run_dir(out, label, variant, run_id):
        return Path(out) / f"{label}_{VARIANTS[variant][2]}_{run_id}"

    def find_run_dir(self, out, expected, variant, label, run_id, t0):
        """The spec's <label>_<tag>_<run-id>; otherwise a fresh dir whose summary has this label, run id and mode."""
        if (expected / "summary.json").is_file():
            return expected
        mode, hint, _tag = VARIANTS[variant]
        best = None
        for sp in Path(out).glob("*/summary.json"):
            try:
                if sp.stat().st_mtime < t0 - 1:
                    continue
            except OSError:
                continue
            s = read_json(sp) or {}
            h = s.get("hint")
            h = ("on" if h else "off") if isinstance(h, bool) else str(h).lower()
            if (str(s.get("label")) == str(label) and str(s.get("run_id")) == str(run_id)
                    and s.get("mode") == mode and h in (hint, "none")):
                best = sp.parent
        if best is not None:
            self.log(f"note: run dir is {best.name}, expected {expected.name}")
            return best
        return expected

    def accept_run(self, scenario, variant, label, run_id, out, case_set="cases", first=None, cases=None):
        """One run_accept.py run (MemFree guard first). Returns a record with status done / crashed /
        timeout / refused and the summary fields the pipeline uses."""
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        n = self.case_count(scenario, case_set, first, cases)
        d = self.run_dir(out, label, variant, run_id)
        allow_low = self.memfree_guard()
        name = d.name
        t0 = now_s()
        kw = {"timeout": 900 + CASE_TIMEOUT_S * max(1, n), "cleanup": True, "watch": d / "progress.json",
              "stall_s": self.a.stall_s}
        rc = self.run_logged(name, self.cmd_accept(scenario, variant, label, run_id, out, case_set, first, cases,
                                                   allow_low), **kw)
        if rc == 4 and not allow_low:
            self.log(f"{name}: refused for low MemFree (exit 4); once more with --allow-low-memfree")
            allow_low = True
            rc = self.run_logged(name, self.cmd_accept(scenario, variant, label, run_id, out, case_set, first, cases,
                                                       True), **kw)
        if rc == 5 and self.wait_other_runs():
            # The slice lock was held by an earlier run (e.g. left over from a killed pipeline): try once more.
            rc = self.run_logged(name, self.cmd_accept(scenario, variant, label, run_id, out, case_set, first, cases,
                                                       allow_low), **kw)
        if rc == 2:
            raise Stop(f"run_accept.py 用法错误（exit 2），流水线和运行器的参数对不上，见 logs/{name}.log")
        if rc == 5:
            raise Stop(f"run_accept.py 报环境问题（exit 5：磁盘空间不够，或 memlab.slice 被别的运行占着），见 logs/{name}.log")
        dur = now_s() - t0
        d = self.find_run_dir(out, d, variant, label, run_id, t0)
        summ = None
        sp = d / "summary.json"
        if sp.is_file() and sp.stat().st_mtime >= t0 - 1:
            summ = read_json(sp)
        if isinstance(summ, dict):
            status = "done"
        elif rc == 3:
            status = "refused"
        elif rc == TIMEOUT_RC:
            status = "timeout"
        else:
            status = "crashed"
        totals = (summ or {}).get("totals") if isinstance((summ or {}).get("totals"), dict) else {}
        tp = (summ or {}).get("types_present") if isinstance((summ or {}).get("types_present"), dict) else {}
        rec = {"name": name, "dir": str(d), "variant": variant, "label": label, "run_id": str(run_id), "rc": rc,
               "status": status, "n_cases": to_int((summ or {}).get("n_cases"), n), "duration_s": round(dur, 1),
               "started": stamp(t0), "ended": stamp(),
               "valid": bool((summ or {}).get("valid")) if summ else False,
               "invalid_reasons": (summ or {}).get("invalid_reasons") or ([] if summ else [f"exit {rc}, no summary"]),
               "unconfirmed": to_int((summ or {}).get("unconfirmed"), 0),
               "category_total": to_int(totals.get("category_total"), 0),
               "popups": to_int(totals.get("popups"), 0), "kills": to_int(totals.get("kills"), 0),
               "startup_failures": to_int(totals.get("startup_failures"), 0),
               "types_ok": all(bool(tp.get(k)) for k in TYPE_KEYS), "low_memfree": allow_low,
               "memshiftd": (summ or {}).get("memshiftd")}
        # Exit 1 = the runner hit an error but still wrote summary.json (valid=false): counts like a crash.
        rec["run_error"] = bool(status == "done" and rc not in (0, None))
        self.state["runs"].append(rec)
        self.save()
        self.log(f"{name}: {status} rc {rc}, {hours(dur)}, valid {rec['valid']}, category_total {rec['category_total']}"
                 + (f", error {str((summ or {}).get('error'))[:200]}" if rec["run_error"] else ""))
        if status == "done" and not rec["run_error"]:
            self.crashes_in_row = 0
            self.crashed_attempts = []
        elif status in ("crashed", "timeout") or rec["run_error"]:
            self.crashes_in_row += 1
        return rec

    # ------------------------------------------------------------ checks on run outputs
    def check_outputs(self, rec, scenario, apply_run=False):
        """Problems in a short check run: summary fields and a terminal state for every app attempt.
        Unconfirmed states fail an observe check; in an apply run they are apply's own result (实验设置.md 7.3)."""
        if rec["status"] != "done" or rec.get("run_error"):
            return [f"{rec['name']}: {rec['status']} (exit {rec['rc']})"]
        d = Path(rec["dir"])
        s = read_json(d / "summary.json") or {}
        p = [f"summary.json lacks {k}" for k in SUMMARY_KEYS if k not in s]
        tot = s.get("totals") if isinstance(s.get("totals"), dict) else {}
        p += [f"totals lacks {k}" for k in TOTAL_KEYS if k not in tot]
        tp = s.get("types_present") if isinstance(s.get("types_present"), dict) else {}
        p += [f"types_present lacks {k}" for k in TYPE_KEYS if k not in tp]
        for f in ("cases.csv", "events.csv", "attempts.csv", "pressure.csv"):
            if not (d / f).is_file():
                p.append(f"{f} missing")
        sc = read_json(scenario) or {}
        orders = {c["id"]: c.get("order", []) for c in (sc.get("cases") or []) + (sc.get("tuning_cases") or [])
                  if isinstance(c, dict) and c.get("id")}
        import csv
        try:
            with open(d / "attempts.csv", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except (OSError, csv.Error, UnicodeDecodeError):
            rows = []
        seen = {}
        for r in rows:
            cid = r.get("case") or r.get("case_id") or r.get("id") or ""
            out = (r.get("outcome") or "").strip()
            seen.setdefault((cid, r.get("app") or ""), []).append(out)
            if out.lower() in NOT_TERMINAL:
                p.append(f"attempt {cid}/{r.get('app')} has no terminal state ({out or 'empty'})")
            elif out == "not_run":
                p.append(f"attempt {cid}/{r.get('app')} was not run")
            elif out not in TERMINAL:
                self.log(f"note: unexpected outcome {out!r} for {cid}/{r.get('app')}")
        for cid in s.get("case_ids") or []:
            for app in orders.get(cid, []):
                if (cid, app) not in seen:
                    p.append(f"no attempt row for {cid}/{app}")
        if to_int(s.get("unconfirmed"), 0) > 0:
            if apply_run:
                self.log(f"note: {rec['name']}: {s.get('unconfirmed')} unconfirmed terminal states (apply: not a failure)")
            else:
                p.append(f"{s.get('unconfirmed')} unconfirmed terminal states")
        if not s.get("case_ids"):
            p.append("no case_ids")
        return p

    def memshiftd_problems(self, rec):
        """Smoke: memshiftd must have submitted jobs and logged no ERROR."""
        if rec["status"] != "done" or rec.get("run_error"):
            return [f"{rec['name']}: {rec['status']} (exit {rec['rc']})"]
        d = Path(rec["dir"])
        s = read_json(d / "summary.json") or {}
        ms = s.get("memshiftd") if isinstance(s.get("memshiftd"), dict) else {}
        jobs = to_int(ms.get("jobs"), -1)
        p = []
        if jobs < 0:
            import csv
            try:
                with open(d / "memshiftd" / "reclaim.csv", newline="", encoding="utf-8") as f:
                    jobs = sum(1 for _ in csv.DictReader(f))
            except (OSError, csv.Error):
                jobs = 0
        if jobs <= 0:
            p.append(f"{rec['name']}: memshiftd submitted no jobs")
        if to_int(ms.get("errors"), 0) > 0:
            p.append(f"{rec['name']}: memshiftd errors {ms.get('errors')}")
        errs = []
        for ev in list(d.glob("memshiftd*/events.csv")):
            try:
                for line in ev.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
                    parts = line.split(",", 3)
                    if len(parts) >= 2 and parts[1].strip() == "ERROR":
                        errs.append(line[:160])
            except OSError:
                pass
        if errs:
            p.append(f"{rec['name']}: memshiftd ERROR lines: " + " | ".join(errs[:3]))
        if not list(d.glob("memshiftd*/events.csv")):
            self.log(f"note: {rec['name']}: no memshiftd events.csv found")
        return p

    # ------------------------------------------------------------ time projection
    def per_case_s(self):
        done = [r for r in self.state["runs"] if r.get("status") == "done" and to_int(r.get("n_cases")) >= 4]
        formal = [r for r in done if r.get("label") in ("formal", "formalx")]
        use = formal or [r for r in done if r.get("label") == "qual"] or done
        vals = [max(20.0, (float(r["duration_s"]) - RUN_OVERHEAD_S) / to_int(r["n_cases"])) for r in use]
        return 1.05 * sum(vals) / len(vals) if vals else DEFAULT_CASE_S

    def est_run_s(self, n):
        return RUN_OVERHEAD_S + n * self.per_case_s()

    # ------------------------------------------------------------ phases
    def phase_checks(self):
        ck = self.state["checks"]
        if not self.a.no_build:
            rc = self.run_logged("build", ["cmake", "--build", self.build, "--target", *BUILD_TARGETS, "-j4"],
                                 timeout=1800)
            if rc != 0:
                raise Stop(f"编译失败（exit {rc}），见 {self.out / 'logs' / 'build.log'}")
        rc = self.run_logged("ctest", [self.ctest, "--test-dir", self.build, "-R", CTEST_REGEX, "--output-on-failure"],
                             timeout=900)
        ck["ctest_rc"] = rc
        self.save()
        if rc != 0:
            raise Stop(f"ctest（{CTEST_REGEX}）没过（exit {rc}），见 {self.out / 'logs' / 'ctest.log'}")
        lines = ["ctest 通过"]
        sc = self.scenario_for("checks")
        for variant in ("O", "A"):
            rec = self.accept_run(sc, variant, "chk", "1", self.out / "checks", "tuning", first=1, cases=2)
            probs = self.check_outputs(rec, sc, apply_run=variant != "O")
            if variant == "A":
                probs += [x for x in self.memshiftd_problems(rec) if "no jobs" not in x]
            ck[f"short_{variant}"] = {"dir": rec["dir"], "problems": probs, "category_total": rec["category_total"],
                                      "valid": rec["valid"]}
            self.save()
            if probs:
                raise Stop(f"短运行 {VARIANT_CN[variant]} 检查没过：" + "；".join(probs[:5]))
            lines.append(f"短运行 {VARIANT_CN[variant]}（调参用例 1–2）字段和终态齐全，分类合计 {rec['category_total']}"
                         f"{'' if rec['valid'] else '（运行标为无效：' + '；'.join(map(str, rec['invalid_reasons']))[:80] + '）'}")
        # wl16 observe round 1, same arguments as run_series --variant observe::30 (record only).
        allow = self.memfree_guard()
        if allow:
            self.log("wl16: run_round.py has no MemFree guard of its own")
        wl = self.out / "checks" / "wl16"
        wl.mkdir(parents=True, exist_ok=True)
        t0 = now_s()
        rc = self.run_logged("wl16_observe_1", self.cmd_wl16(wl), timeout=450 + 900, cleanup=True)
        s = read_json(wl / "wl16_observe_1" / "summary.json") if rc == 0 else None
        ev = (s or {}).get("events_total")
        ck["wl16"] = {"rc": rc, "events_total": ev, "duration_s": round(now_s() - t0, 1)}
        self.save()
        lines.append(f"wl16 observe 第 1 轮 events_total = {ev if ev is not None else '没有结果（exit ' + str(rc) + '）'}"
                     "（以前 10–12，只记录）")
        self.progress("checks", "；".join(lines))

    def cmd_wl16(self, wl):
        return [PY, self.round_runner, "--scenario", LAB / "scenarios" / "wl16.json", "--mode", "observe",
                "--round-id", "1", "--out", wl, "--lead", "30"]

    def phase_calibrate(self):
        cal = self.out / "calibration.json"
        allow = self.memfree_guard()
        cmd = [PY, self.runner, "--scenario", self.scenario, "--calibrate", "--out", cal]
        if allow:
            cmd.append("--allow-low-memfree")
        t0 = now_s()
        rc = self.run_logged("calibrate", cmd, timeout=3600, cleanup=True)
        if rc == 4 and not allow:
            self.log("calibrate: refused for low MemFree (exit 4); once more with --allow-low-memfree")
            rc = self.run_logged("calibrate", cmd + ["--allow-low-memfree"], timeout=3600, cleanup=True)
        fresh = cal.is_file() and cal.stat().st_mtime >= t0 - 1
        if rc != 0 or not fresh or not isinstance(read_json(cal), dict):
            raise Stop(f"校准失败（run_accept.py --calibrate exit {rc}），见 {self.out / 'logs' / 'calibrate.log'}")
        out = self.cal_scenario_path()
        rc, text = self.sh(self.make_cases_cmd(out, cal), timeout=300)
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        (self.out / "logs" / "make_accept_cases_cal.log").write_text(text, encoding="utf-8")
        if rc == 3:
            raise Stop(f"按校准读数的验收检查没过，场景照写在 {out}，见 logs/make_accept_cases_cal.log")
        if rc != 0 or not out.is_file():
            raise Stop(f"用校准结果重新生成场景失败（exit {rc}）：{text.strip()[-300:]}")
        self.state["scenario_run"] = str(out)
        self.state["calibration"] = str(cal)
        self.save()
        sc = read_json(out) or {}
        chk = sc.get("acceptance_checks") or {}
        sm = [c.get("cal_same_moment_mib") for c in sc.get("cases", []) if c.get("cal_same_moment_mib") is not None]
        self.progress("calibrate", f"{len(sc.get('apps', {}))} 个应用校准完；按校准读数三个条件都满足："
                                   f"所有应用日常合计 {chk.get('all_apps_daily_sum_mib')} MiB，候选用例同一时刻合计 "
                                   f"{min(sm) if sm else '?'}–{max(sm) if sm else '?'} MiB（≥ 19,661）；场景 {out.name}")

    def make_cases_cmd(self, out, cal, sequence=None, name=None):
        """make_accept_cases.py with the stage-1 inputs the nominal scenario was made from (its "sources");
        sequence/name override the candidate sequence (the R20 reference scenario)."""
        sc = read_json(self.scenario) or {}
        src = sc.get("sources") or {}

        def pick(opt, key, default):
            return resolve_input(opt or src.get(key) or default)
        cmd = [PY, LAB / "make_accept_cases.py",
               "--sequence", sequence or pick(self.a.sequence, "sequence", DATA / "sequence_S130.json"),
               "--tuning", pick(self.a.tuning, "tuning", DATA / "sequence_S130T.json"),
               "--profiles", pick(self.a.profiles, "profiles", DATA / "profiles.json"),
               "--out", out, "--name", name or sc.get("name") or self.scenario.stem, "--quiet"]
        rc_cap = (sc.get("timeline") or {}).get("ramp_cap_s", 1)
        if rc_cap != 1:                                   # keep the nominal scenario's lever
            cmd += ["--ramp-cap", rc_cap or 0]
        if cal is not None:
            cmd += ["--calibration", cal]
        return [str(c) for c in cmd]

    def run_with_reruns(self, key, scenario, variant, label, base_id, out, case_set, cases, holder):
        """Run until valid, at most MAX_EXTRA reruns. holder['attempts'] keeps every attempt across restarts."""
        holder.setdefault("attempts", [])
        for att in holder["attempts"]:          # cut off by a crash or a restart of the pipeline: does not count
            if att.get("status") == "running":
                att["status"] = "interrupted"
        while True:
            counted = [x for x in holder["attempts"] if x.get("status") != "interrupted"]
            if counted and counted[-1].get("status") == "done" and counted[-1].get("valid"):
                return counted[-1]
            if len(counted) >= 1 + MAX_EXTRA:
                return None
            k = len(holder["attempts"])
            run_id = str(base_id) if k == 0 else f"{base_id}r{k}"
            holder["attempts"].append({"run_id": run_id, "status": "running", "started": stamp()})
            self.save()
            rec = self.accept_run(scenario, variant, label, run_id, out, case_set, cases=cases)
            att = {k2: v for k2, v in rec.items() if k2 != "memshiftd"}
            holder["attempts"][-1] = att
            if rec["status"] in ("crashed", "timeout") or rec.get("run_error"):
                self.crashed_attempts.append(att)
            self.save()
            if rec["status"] == "refused":
                raise Stop(f"{rec['name']} 被 run_accept.py 拒绝（exit 3：校准后的验收检查没过）")
            if self.crashes_in_row >= 2:
                # Most likely a code problem: these attempts do not use up the reruns once a human fixed it.
                for x in self.crashed_attempts:
                    x["was"], x["status"] = x["status"], "interrupted"
                self.save()
                how = "运行出错" if rec.get("run_error") else {"timeout": "超时或卡住", "crashed": "崩溃，没有 summary"}.get(
                    rec["status"], rec["status"])
                raise Stop(f"run_accept.py 连续 {self.crashes_in_row} 次没跑完（最后一次 {rec['name']}：{how}，"
                           f"exit {rec['rc']}），见 logs/{rec['name']}.log")
            if rec["status"] == "done" and rec["valid"]:
                return rec
            left = 1 + MAX_EXTRA - len([x for x in holder["attempts"] if x.get("status") != "interrupted"])
            why = "；".join(map(str, rec.get("invalid_reasons") or [])) or rec["status"]
            self.progress(key if key in PHASES else "formal",
                          f"{rec['name']} 作废（{why[:120]}）" + (f"，按原顺序补跑（还能补 {left} 次）" if left > 0 else "，已补满 2 次"),
                          toast=False)

    def phase_qualify(self):
        sc = self.scenario_for("qualify")
        q = self.state.setdefault("qualify", {})
        # A changed scenario or an explicit --from qualify starts a new qualification run.
        if q.get("scenario_sha") != sha256_file(sc) or self.a.from_phase == "qualify":
            if q.get("attempts"):
                self.state.setdefault("qualify_discarded", []).append(dict(q))
            q.clear()
            q["scenario_sha"] = sha256_file(sc)
            self.a.from_phase = None if self.a.from_phase == "qualify" else self.a.from_phase
        n = self.case_count(sc, "cases")
        rec = self.run_with_reruns("qualify", sc, "O", "qual", "1", self.out / "qual", "cases", None, q)
        if rec is None:
            raise Stop("资格轮连续作废 3 次，没有能用的资格轮")
        freeze = self.out / "freeze.json"
        rc, text = self.sh([PY, LAB / "accept_tools.py", "qualify", "--run", rec["dir"], "--out", freeze,
                            "--scenario", sc], timeout=900)
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        (self.out / "logs" / "qualify_tool.log").write_text(text, encoding="utf-8")
        if rc != 0 or not freeze.is_file():
            raise Stop(f"资格轮没过（accept_tools qualify exit {rc}）：{text.strip()[-300:]}")
        fr = read_json(freeze) or {}
        self.state["freeze"] = self.state["freeze_active"] = str(freeze)
        self.state["formal"] = None            # a new freeze starts the formal runs afresh
        self.save()
        self.progress("qualify", f"资格轮 {n} 个用例：分类合计 {rec['category_total']}（弹窗 {rec['popups']}、查杀 {rec['kills']}、"
                                 f"启动失败 {rec['startup_failures']}）；N_120 = {fr.get('N_120')}，N_95 = {fr.get('N_95')}，"
                                 f"设备口径 ≥ 110 的位置 = {fr.get('N_dev')}，N = {fr.get('N')}，N′ = {fr.get('N_prime')}；"
                                 f"冻结前 {fr.get('N')} 个用例（{freeze.name}）")

    def phase_smoke(self):
        sc = self.scenario_for("smoke")
        lines, probs = [], []
        for variant in ("A", "T"):
            rec = self.accept_run(sc, variant, "smoke", "1", self.out / "smoke", "tuning", first=3, cases=4)
            p = self.memshiftd_problems(rec)
            probs += p
            ms = rec.get("memshiftd") or {}
            lines.append(f"{VARIANT_CN[variant]}（调参用例 3–6）：memshiftd 任务 {ms.get('jobs', '?')}（成功 {ms.get('ok_jobs', '?')}），"
                         f"回收 {ms.get('reclaimed_mib', '?')} MiB，分类合计 {rec['category_total']}")
        self.state["checks"]["smoke"] = {"lines": lines, "problems": probs}
        self.save()
        if probs:
            raise Stop("冒烟没过：" + "；".join(probs[:4]))
        self.progress("smoke", "；".join(lines) + "；memshiftd 没报错")

    # ------------------------------------------------------------ formal
    def active_freeze(self):
        p = self.state.get("freeze_active") or self.state.get("freeze") or str(self.out / "freeze.json")
        fr = read_json(p)
        if not isinstance(fr, dict) or not fr.get("N"):
            raise Stop(f"找不到冻结文件 {p}，要先跑 qualify 阶段")
        return Path(p), fr

    def new_formal(self, fpath, fr, sc):
        return {"freeze": str(fpath), "freeze_sha": sha256_file(fpath), "scenario_sha": sha256_file(sc),
                "extended": bool(fr.get("extended")), "superseded": [], "notes": [],
                "slots": {s: {"slot": s, "variant": s[0], "index": int(s[1]), "n_cases": fr["N"],
                              "label": "formalx" if fr.get("extended") else "formal", "attempts": [],
                              "status": "pending"} for s in FORMAL_ORDER}}

    def ensure_formal(self):
        fpath, fr = self.active_freeze()
        sc = self.scenario_for("formal")
        f = self.state.get("formal")
        if (not isinstance(f, dict) or self.a.fresh_formal or f.get("freeze_sha") != sha256_file(fpath)
                or f.get("scenario_sha") != sha256_file(sc)):
            if isinstance(f, dict) and any(s["attempts"] for s in f["slots"].values()):
                self.log("formal: freeze/scenario changed or --fresh-formal: earlier formal runs are not reused")
                self.state.setdefault("formal_discarded", []).append(f)
            f = self.state["formal"] = self.new_formal(fpath, fr, sc)
            self.save()
        for s in f["slots"].values():          # an attempt cut off by a crash or a restart does not count
            for att in s["attempts"]:
                if att.get("status") == "running":
                    att["status"] = "interrupted"
        self.a.fresh_formal = False
        return f, fr, sc

    def pending_oa(self, f, exclude=None):
        return [s for k, s in f["slots"].items() if s["status"] == "pending" and s["variant"] in "OA" and k != exclude]

    def formal_fits(self, f, slot=None):
        """Projected end (s since epoch) of the remaining required runs (+ the given slot)."""
        t = now_s() + REPORT_S
        if slot is not None:
            t += self.est_run_s(slot["n_cases"])
        for s in self.pending_oa(f, exclude=slot["slot"] if slot else None):
            t += self.est_run_s(s["n_cases"])
        return t

    def phase_formal(self):
        f, fr, sc = self.ensure_formal()
        warned = False
        while True:
            slot = next((f["slots"][k] for k in FORMAL_ORDER if f["slots"][k]["status"] == "pending"), None)
            if slot is None:
                break
            end = self.formal_fits(f, slot)
            if slot["variant"] == "T" and end > self.deadline:
                slot["status"] = "dropped"
                f["notes"].append(f"{slot['slot']} dropped at {stamp()}: projected end {stamp(end)} > deadline")
                self.save()
                self.progress("formal", f"按截止时间砍掉 {slot['slot']}（{VARIANT_CN['T']}）：再跑它要到 {stamp(end)[5:16]} "
                                        f"才能跑完必须的运行，超过截止 {stamp(self.deadline)[5:16]}")
                continue
            if end > self.deadline and not warned:
                warned = True
                self.progress("formal", f"预计 {stamp(end)[5:16]} 才能跑完必须的基线和优化版，超过截止 "
                                        f"{stamp(self.deadline)[5:16]}；基线和优化版照跑，没跑的对照先砍")
            rec = self.run_with_reruns("formal", sc, slot["variant"], slot["label"], slot["index"], self.out / "formal",
                                       "cases", slot["n_cases"], slot)
            if rec is None:
                slot["status"] = "invalid"
                last = slot["attempts"][-1] if slot["attempts"] else {}
                slot["final_dir"] = last.get("dir")
                self.save()
                self.progress("formal", f"{slot['slot']}（{VARIANT_CN[slot['variant']]}）补跑 2 次后仍作废，照记，对比会不成立")
                continue
            slot["status"] = "done"
            slot["final_dir"] = rec["dir"]
            self.save()
            msg = (f"{slot['slot']}（{VARIANT_CN[slot['variant']]}，{rec['n_cases']} 个用例）：分类合计 {rec['category_total']}"
                   f"（弹窗 {rec['popups']}、查杀 {rec['kills']}、启动失败 {rec['startup_failures']}），用时 {hours(rec['duration_s'])}")
            self.progress("formal", msg, toast=slot["slot"] in ("O1", "A3", "T3"))
            if slot["variant"] == "O":
                self.check_baseline(f, fr, slot, rec)
                fpath, fr = self.active_freeze()
        done = {k: s["status"] for k, s in f["slots"].items()}
        self.progress("formal", "正式运行结束：" + "，".join(f"{k} {v}" for k, v in done.items()))

    def check_baseline(self, f, fr, slot, rec):
        if rec["category_total"] >= TARGET and rec["types_ok"]:
            return
        why = []
        if rec["category_total"] < TARGET:
            why.append(f"分类合计只有 {rec['category_total']}（< {TARGET}）")
        if not rec["types_ok"]:
            why.append("三类事件没有都出现")
        why = "，".join(why)
        if f.get("extended") or fr.get("extended"):
            f["notes"].append(f"baseline {slot['slot']} failed after the extension: {why}")
            self.save()
            self.progress("formal", f"基线 {slot['slot']} {why}；已经加长到 N′，不能再加，照记失败（对比不成立）")
            return
        np_ = to_int(fr.get("N_prime"))
        need = 6 * self.est_run_s(np_) + REPORT_S
        if now_s() + need > self.deadline:
            f["notes"].append(f"baseline {slot['slot']} failed ({why}); no time to extend (needs {hours(need)})")
            self.save()
            self.progress("formal", f"基线 {slot['slot']} {why}；加长到 N′ = {np_} 要再跑约 {hours(need)}，"
                                    f"截止前来不及，照记失败（对比不成立），按 N 继续跑完")
            return
        src, dst = Path(f["freeze"]), self.out / "freeze_Nprime.json"
        rc, text = self.sh([PY, LAB / "accept_tools.py", "extend", "--freeze", src, "--out", dst,
                            "--reason", f"baseline {slot['slot']}: {why}"], timeout=120)
        if rc != 0:
            raise Stop(f"加长冻结序列失败（accept_tools extend exit {rc}）：{text.strip()[-200:]}")
        nf = read_json(dst) or {}
        self.state["freeze_active"] = str(dst)
        f["superseded"] += [dict(f["slots"][k]) for k in FORMAL_ORDER
                            if f["slots"][k]["variant"] in "OA" and f["slots"][k]["attempts"]]
        for k in FORMAL_ORDER:
            s = f["slots"][k]
            if s["variant"] in "OA" or s["status"] == "pending":
                f["slots"][k] = {"slot": k, "variant": s["variant"], "index": s["index"], "n_cases": nf.get("N"),
                                 "label": "formalx", "attempts": [], "status": "pending"}
        f["extended"] = True
        f["freeze"] = str(dst)
        f["freeze_sha"] = sha256_file(dst)
        f["notes"].append(f"extended to N' = {nf.get('N')} at {stamp()} after baseline {slot['slot']}: {why}")
        self.save()
        self.progress("formal", f"基线 {slot['slot']} {why}：按规则把冻结序列加长到 N′ = {nf.get('N')}（{dst.name}），"
                                f"基线和优化版全部按 N′ 重跑，已跑完的对照不重跑；预计还要约 {hours(need)}")

    # ------------------------------------------------------------ report
    def phase_report(self):
        fpath, fr = self.active_freeze()
        f = self.state.get("formal") or {"slots": {}}
        dirs = {"O": [], "A": [], "T": []}
        for k in FORMAL_ORDER:
            s = f["slots"].get(k)
            if s and s.get("final_dir") and s["status"] in ("done", "invalid"):
                dirs[s["variant"]].append(s["final_dir"])
        out = self.out / "report"
        cmd = [PY, LAB / "accept_tools.py", "compare", "--freeze", fpath, "--out", out]
        if dirs["O"]:
            cmd += ["--baseline", *dirs["O"]]
        if dirs["A"]:
            cmd += ["--optimized", *dirs["A"]]
        if dirs["T"]:
            cmd += ["--control", *dirs["T"]]
        rc, text = self.sh(cmd, timeout=900)
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        (self.out / "logs" / "compare.log").write_text(text, encoding="utf-8")
        rep = read_json(out / "report.json")
        if rc not in (0, 2) or not isinstance(rep, dict):
            raise Stop(f"对比工具出错（accept_tools compare exit {rc}）：{text.strip()[-300:]}")
        ct = (rep.get("main") or {}).get("category_total") or {}
        fmt = lambda x, nd=1: "—" if x is None else f"{x:.{nd}f}"
        red = ct.get("reduction")
        msg = (f"判定：{rep.get('verdict')}。基线 {len(dirs['O'])} 次均值 {fmt(ct.get('baseline_mean'))}，优化版 "
               f"{len(dirs['A'])} 次均值 {fmt(ct.get('other_mean'))}，比值 {fmt(ct.get('ratio'), 3)}"
               f"（降幅 {'—' if red is None else f'{red * 100:.1f}%'}）；对照 {len(dirs['T'])} 次；报告 {out / 'report.md'}")
        if rep.get("invalid_reasons"):
            msg += "；不成立的原因：" + "；".join(rep["invalid_reasons"][:3])
        self.state["report"] = {"dir": str(out), "verdict": rep.get("verdict"), "valid": rep.get("valid")}
        self.save()
        self.progress("report", msg)

    # ------------------------------------------------------------ reference (optional)
    def reference_seq(self):
        return resolve_input(self.a.reference_seq or DATA / "sequence_R20.json")

    def phase_reference(self):
        """实验设置.md 0.1: when time is left, 1 baseline + 1 optimized run on the 20 unfiltered R20 cases,
        only as a reference. It never turns a finished main line into a stop."""
        try:
            self.reference_runs()
        except Stop as e:
            self.state.setdefault("reference", {})["skipped"] = str(e)
            self.save()
            self.progress("reference", f"参考用例 R20 没跑或没跑完：{e}（主线结果不受影响）")

    def reference_runs(self):
        if self.a.no_reference:
            raise Stop("--no-reference")
        seq = self.reference_seq()
        r = read_json(seq)
        if not isinstance(r, dict) or not r.get("cases"):
            raise Stop(f"找不到参考序列 {seq}")
        cal = Path(self.state.get("calibration") or self.out / "calibration.json")
        if not cal.is_file():
            raise Stop("没有校准结果 calibration.json")
        n = len(r["cases"])
        need = 2 * self.est_run_s(n) + REPORT_S
        if now_s() + need > self.deadline:
            raise Stop(f"截止前来不及（基线 + 优化版约 {hours(need)}）")
        sc_path = self.out / "acceptR20_cal.json"
        rc, text = self.sh(self.make_cases_cmd(sc_path, cal, sequence=seq, name="acceptR20"), timeout=300)
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        (self.out / "logs" / "make_accept_cases_R20.log").write_text(text, encoding="utf-8")
        if rc != 0 or not sc_path.is_file():
            raise Stop(f"生成参考场景失败（make_accept_cases exit {rc}"
                       f"{'：按校准读数的验收检查没过' if rc == 3 else ''}）")
        sc = read_json(sc_path) or {}
        ids = [c["id"] for c in sc.get("cases", [])]
        fz = self.out / "freeze_R20.json"
        write_atomic(fz, json.dumps({"sequence": sc.get("sequence"), "sequence_sha256": sc.get("sequence_sha256"),
                                     "N": len(ids), "case_ids": ids, "reference": True, "extended": False,
                                     "rule": "reference only: all cases of the unfiltered R20 sequence, 1 run each",
                                     "scenario": str(sc_path), "scenario_sha256": sha256_file(sc_path),
                                     "created_unix": int(now_s())}, ensure_ascii=False, indent=1) + "\n")
        ref = self.state.setdefault("reference", {})
        if ref.get("scenario_sha") != sha256_file(sc_path):
            ref.clear()
            ref["scenario_sha"] = sha256_file(sc_path)
        dirs = {}
        for variant in ("O", "A"):
            rec = self.run_with_reruns("reference", sc_path, variant, "ref", "1", self.out / "reference", "cases",
                                       None, ref.setdefault(variant, {}))
            if rec is None:
                raise Stop(f"参考运行 {VARIANT_CN[variant]} 补跑 2 次后仍作废")
            dirs[variant] = rec["dir"]
        out = self.out / "report_R20"
        rc, text = self.sh([PY, LAB / "accept_tools.py", "compare", "--freeze", fz, "--baseline", dirs["O"],
                            "--optimized", dirs["A"], "--runs", "1", "--target", "0", "--reference", "--out", out], timeout=900)
        (self.out / "logs" / "compare_R20.log").write_text(text, encoding="utf-8")
        rep = read_json(out / "report.json")
        if rc not in (0, 2) or not isinstance(rep, dict):
            raise Stop(f"参考对比出错（accept_tools compare exit {rc}）")
        ct = (rep.get("main") or {}).get("category_total") or {}
        fmt = lambda x, nd=1: "—" if x is None else f"{x:.{nd}f}"
        ref["report"] = str(out)
        self.save()
        self.progress("reference", f"R20 参考（{n} 个不过滤的用例，只作参考）：基线分类合计 {fmt(ct.get('baseline_mean'), 0)}，"
                                   f"优化版 {fmt(ct.get('other_mean'), 0)}，比值 {fmt(ct.get('ratio'), 3)}；报告 {out / 'report.md'}")

    # ------------------------------------------------------------ driver
    def first_phase(self):
        if self.a.from_phase:
            return PHASES.index(self.a.from_phase)
        for i, p in enumerate(PHASES):
            if (self.state["phases"].get(p) or {}).get("status") != "done":
                return i
        return len(PHASES)

    def run(self):
        i0 = self.first_phase()
        if i0 >= len(PHASES):
            self.log("all phases are done (use --from PHASE to rerun one)")
            return 0
        if self.a.from_phase:
            for p in PHASES[i0:]:
                self.state["phases"].pop(p, None)
        self.state["stop_reason"] = None
        self.save()
        left = self.deadline - now_s()
        self.log(f"start at {PHASES[i0]}; out {self.out}; scenario {self.scenario}; deadline {stamp(self.deadline)} "
                 f"({hours(left)} left)")
        if left <= 0:
            self.log("warning: the deadline has already passed; T runs will be dropped")
        for p in PHASES[i0:]:
            ph = self.state["phases"].setdefault(p, {})
            ph.update({"status": "running", "started": stamp()})
            self.save()
            getattr(self, f"phase_{p}")()
            ph.update({"status": "done", "ended": stamp()})
            self.save()
        return 0

    # ------------------------------------------------------------ dry run
    def dry_run(self):
        a = self.a
        q = lambda c: shlex.join([str(x) for x in c])
        print("accept_pipeline.py dry run: nothing is executed, written or notified")
        print(f"  out       {self.out}")
        print(f"  scenario  {self.scenario}{'' if self.scenario.is_file() else '  (MISSING)'}")
        print(f"  deadline  {stamp(self.deadline)} local ({hours(self.deadline - now_s())} from now)")
        print(f"  state     {self.state_path} ({'exists' if self.state_path.is_file() else 'new'}; done: "
              f"{', '.join(p for p in PHASES if (self.state['phases'].get(p) or {}).get('status') == 'done') or 'none'})")
        print(f"  progress  {self.progress_path} (+ toast via {NOTIFY.name})")
        i0 = self.first_phase()
        print(f"  start at  {PHASES[i0] if i0 < len(PHASES) else '(all done)'}")
        mi = meminfo()
        free, avail = mi.get("MemFree", 0), mi.get("MemAvailable", 0)
        guard = (f"MemFree {free / GiB_KIB:.1f} GiB >= 17 GiB: no balloon" if free >= MEMFREE_MIN_KIB else
                 f"MemFree {free / GiB_KIB:.1f} GiB < 17 GiB now: balloon of "
                 f"{min((avail - GiB_KIB) // 1024, BALLOON_MAX_MIB, a.balloon_max_mib)} MiB, re-check, "
                 "else --allow-low-memfree")
        print(f"  before every run: MemFree guard ({guard})")
        sc_nom = self.scenario
        sc_cal = self.scenario_for("qualify")
        fr = read_json(self.state.get("freeze_active") or self.out / "freeze.json") or {}
        N = fr.get("N")
        n_txt = str(N) if N else "N (from freeze.json; stage-1 estimate 41)"
        for p in PHASES[i0:]:
            print(f"\n[{p}] {PHASE_CN[p]}")
            if p == "checks":
                if not a.no_build:
                    print("  $ " + q(["cmake", "--build", self.build, "--target", *BUILD_TARGETS, "-j4"]))
                print("  $ " + q([self.ctest, "--test-dir", self.build, "-R", CTEST_REGEX, "--output-on-failure"]))
                for v in ("O", "A"):
                    print("  $ " + q(self.cmd_accept(sc_nom, v, "chk", "1", self.out / "checks", "tuning", 1, 2)))
                    print(f"    check: summary fields, a terminal state per app attempt"
                          f"{', memshiftd ERROR lines' if v == 'A' else ''}")
                print("  $ " + q(self.cmd_wl16(self.out / "checks" / "wl16")))
                print("    record events_total only (earlier rounds 10-12)")
            elif p == "calibrate":
                cal = self.out / "calibration.json"
                print("  $ " + q([PY, self.runner, "--scenario", sc_nom, "--calibrate", "--out", cal]))
                print("  $ " + q(self.make_cases_cmd(self.cal_scenario_path(), cal)))
                print("    exit 3 (a check fails) -> stop for a human; later phases use the calibrated scenario")
            elif p == "qualify":
                print("  $ " + q(self.cmd_accept(sc_cal, "O", "qual", "1", self.out / "qual", "cases")))
                print("    invalid -> rerun as 1r1, 1r2")
                print("  $ " + q([PY, LAB / "accept_tools.py", "qualify", "--run", self.out / "qual" / "qual_obs_1",
                                  "--out", self.out / "freeze.json", "--scenario", sc_cal]))
                print("    exit 2 (no N_120 / N' too long) -> stop for a human")
            elif p == "smoke":
                for v in ("A", "T"):
                    print("  $ " + q(self.cmd_accept(sc_cal, v, "smoke", "1", self.out / "smoke", "tuning", 3, 4)))
                print("    check: memshiftd jobs > 0 and no ERROR lines")
            elif p == "formal":
                per = self.per_case_s()
                n_est = N or 41
                for k in FORMAL_ORDER:
                    print(f"  {k}: $ " + q(self.cmd_accept(sc_cal, k[0], "formal", k[1], self.out / "formal", "cases",
                                                         cases=n_txt if not N else N)))
                total = 9 * self.est_run_s(n_est)
                print(f"    per case ~{per:.0f} s ({'measured' if self.state['runs'] else 'assumed'}), one run of "
                      f"{n_est} cases ~{hours(self.est_run_s(n_est))}, all 9 ~{hours(total)}; ends ~"
                      f"{stamp(now_s() + total)[5:16]} if started now (deadline {stamp(self.deadline)[5:16]})")
                print("    invalid -> rerun (<= 2 extra, run ids 1r1, 1r2); T runs dropped first when the projected end "
                      "passes the deadline;")
                print("    baseline < 100 or a type missing -> accept_tools.py extend -> freeze_Nprime.json, O and A "
                      "rerun with label formalx at N' (if time allows)")
            elif p == "report":
                print("  $ " + q([PY, LAB / "accept_tools.py", "compare", "--freeze", self.out / "freeze.json",
                                  "--baseline", "<O1 O2 O3 dirs>", "--optimized", "<A1 A2 A3 dirs>", "--control",
                                  "<T dirs>", "--out", self.out / "report"]))
            elif p == "reference":
                if a.no_reference:
                    print("  skipped (--no-reference)")
                    continue
                seq = self.reference_seq()
                n = len((read_json(seq) or {}).get("cases") or []) or 20
                print(f"  only if the 2 runs (~{hours(2 * self.est_run_s(n))}) still fit before the deadline; "
                      "a failure here never stops the pipeline")
                sc_r = self.out / "acceptR20_cal.json"
                print("  $ " + q(self.make_cases_cmd(sc_r, self.out / "calibration.json", sequence=seq, name="acceptR20")))
                for v in ("O", "A"):
                    print("  $ " + q(self.cmd_accept(sc_r, v, "ref", "1", self.out / "reference", "cases")))
                print("  $ " + q([PY, LAB / "accept_tools.py", "compare", "--freeze", self.out / "freeze_R20.json",
                                  "--baseline", self.out / "reference" / "ref_obs_1", "--optimized",
                                  self.out / "reference" / "ref_tmoh_1", "--runs", "1", "--target", "0", "--reference",
                                  "--out", self.out / "report_R20"]))
        print("\nexit codes: 0 finished, 2 stopped for a human")
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="unattended driver of the acceptance experiment (see the module doc)")
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--deadline", required=True, help="local time, e.g. 2026-10-25T19:00")
    ap.add_argument("--from", dest="from_phase", choices=PHASES, default=None)
    ap.add_argument("--dry-run", action="store_true", help="print the phases and commands, run nothing")
    ap.add_argument("--fresh", action="store_true", help="ignore the state file (start a new state)")
    ap.add_argument("--fresh-formal", action="store_true", help="do not reuse finished formal runs")
    ap.add_argument("--no-build", action="store_true", help="skip the cmake build in the checks phase")
    ap.add_argument("--no-notify", action="store_true", help="no Windows toasts")
    ap.add_argument("--build-dir", default=None, help="CMake build dir (default CODE/build)")
    ap.add_argument("--progress", default=None, help="进度.md path (default: next to the out dir)")
    ap.add_argument("--sequence", default=None, help="stage-1 sequence (default: the scenario's sources)")
    ap.add_argument("--tuning", default=None)
    ap.add_argument("--profiles", default=None)
    ap.add_argument("--balloon-max-mib", type=int, default=BALLOON_MAX_MIB, help="cap of the MemFree balloon")
    ap.add_argument("--balloon-only", action="store_true", help="run the MemFree guard once and exit")
    ap.add_argument("--runner", default=None, help="testing: substitute for run_accept.py (no systemd cleanup)")
    ap.add_argument("--round-runner", default=None, help="testing: substitute for run_round.py (no systemd cleanup)")
    ap.add_argument("--ctest", default=None, help="ctest executable (default ctest)")
    ap.add_argument("--stall-s", type=float, default=STALL_S,
                    help="a run counts as hung when its progress.json is unchanged this long (default 900)")
    ap.add_argument("--no-reference", action="store_true", help="skip the optional R20 reference phase")
    ap.add_argument("--reference-seq", default=None, help="R20 sequence (default DATA/sequence_R20.json)")
    a = ap.parse_args(argv)

    pl = Pipeline(a)
    if a.dry_run:
        return pl.dry_run()
    if not pl.scenario.is_file():
        print(f"accept_pipeline: scenario {pl.scenario} not found", file=sys.stderr)
        return 1
    pl.out.mkdir(parents=True, exist_ok=True)
    lock = open(pl.out / ".pipeline.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"accept_pipeline: another pipeline holds {pl.out / '.pipeline.lock'}", file=sys.stderr)
        return 1
    if a.fresh and pl.state_path.is_file():
        keep = pl.state_path.with_name(f"pipeline_state.{time.strftime('%Y%m%d_%H%M%S')}.json")
        os.replace(pl.state_path, keep)
        pl.log(f"--fresh: the old state is kept as {keep.name}")
    if a.balloon_only:
        allow = pl.memfree_guard()
        pl.log(f"balloon-only: run would {'need' if allow else 'not need'} --allow-low-memfree")
        return 0

    def on_term(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)    # one shutdown is enough; let the cleanup finish
        raise Interrupted(f"signal {signum}")
    signal.signal(signal.SIGTERM, on_term)
    # A closed terminal must not end an overnight run (the runs inherit this too).
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    phase = "pipeline"
    try:
        return pl.run()
    except Stop as e:
        reason = str(e)
    except (KeyboardInterrupt, Interrupted) as e:
        reason = f"被中断（{e or 'Ctrl-C'}）"
    except Exception as e:  # noqa: BLE001 - unattended: log every failure and stop cleanly
        reason = f"程序出错：{type(e).__name__}: {e}"
        pl.log(traceback.format_exc())
    phase = next((p for p in PHASES if (pl.state["phases"].get(p) or {}).get("status") == "running"), phase)
    if phase in pl.state["phases"]:
        pl.state["phases"][phase].update({"status": "stopped", "ended": stamp(), "note": reason})
    pl.state["stop_reason"] = reason
    pl.save()
    pl.progress(phase, f"停下来等人：{reason}（修好后用 --from {phase if phase in PHASES else 'checks'} 接着跑）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
