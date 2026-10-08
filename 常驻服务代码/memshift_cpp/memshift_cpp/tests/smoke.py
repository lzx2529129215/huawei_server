#!/usr/bin/env python3
"""Synthetic fixture/CLI tests only. No real reclaim or performance claim."""
import csv
import pathlib
import subprocess
import sys
import tempfile

build = pathlib.Path(sys.argv[1]).resolve()

def run(*args, ok=True):
    p = subprocess.run([str(x) for x in args], text=True, capture_output=True, timeout=20)
    if (p.returncode == 0) != ok:
        raise AssertionError(f"unexpected exit {p.returncode}: {args}\n{p.stdout}\n{p.stderr}")
    return p

with tempfile.TemporaryDirectory(prefix="memshift_test_") as tmp:
    base = pathlib.Path(tmp)
    cg, proc = base / "cgroup", base / "proc"
    cg.mkdir()
    (proc / "pressure").mkdir(parents=True)
    (proc / "meminfo").write_text(
        "MemTotal: 16777216 kB\nMemAvailable: 716800 kB\nMemFree: 524288 kB\n"
        "SwapTotal: 4194304 kB\nSwapFree: 4194304 kB\nZswap: 0 kB\nZswapped: 0 kB\n")
    (proc / "vmstat").write_text("pswpin 0\npswpout 0\n")
    psi = "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    for k in ("memory", "io"):
        (proc / "pressure" / k).write_text(psi)
    stat = {
        "anon": 2 * 1024**3, "file": 2 * 1024**3,
        "inactive_anon": 1024**3, "inactive_file": 1024**3,
        "file_dirty": 0, "file_writeback": 0, "unevictable": 0,
        "pgfault": 20000, "pgmajfault": 0, "workingset_refault_anon": 0,
        "workingset_refault_file": 0, "pgsteal": 0,
    }
    for name in ("background", "editor"):
        d = cg / name
        d.mkdir()
        files = {
            "memory.stat": "".join(f"{k} {v}\n" for k, v in stat.items()),
            "memory.current": str(4 * 1024**3), "memory.min": "0", "memory.low": "0",
            "memory.swap.current": "0", "memory.swap.max": "max", "memory.events": "oom_kill 0\n",
            "cgroup.type": "domain\n", "cgroup.events": "populated 1\n",
            "memory.pressure": psi, "memory.reclaim": "NEVER_WRITE_THIS_FIXTURE\n",
        }
        for k, v in files.items():
            (d / k).write_text(v)
    ctl = base / "control.conf"
    run(build / "memshiftctl", "--file", ctl, "--init", "--protect", "editor")
    run(build / "memshiftctl", "--file", ctl, "--boost-mb", "512", "--ttl", "30")
    assert "protected_apps=editor" in ctl.read_text(), "atomic merge must preserve foreground"

    def config(name):
        p = base / f"{name}.ini"
        p.write_text(
            f"[daemon]\ncgroup_root={cg}\nproc_root={proc}\nlog_dir={base/name}\n"
            f"lock_file={base/'test.lock'}\ncontrol_file={ctl}\ninterval_ms=200\n"
            "startup_grace_sec=0.2\ncooldown_sec=1\nbatch_mb=32\nready_free_mb=0\n"
            "[app background]\npath=background\nreclaim=true\n"
            "[app editor]\npath=editor\nreclaim=true\n"
        )
        return p

    cfg = config("observe")
    run(build / "memshiftd", "--config", cfg, "--fixture", "--ticks", "8")
    rows = list(csv.DictReader((base / "observe" / "reclaim.csv").open()))
    assert rows and all(r["app"] == "background" and r["status"] == "observe_only" for r in rows)
    assert all(r["submitted_bytes"] == "0" for r in rows)
    assert len(rows) <= 2, "cooldown limits plans"
    for name in ("background", "editor"):
        assert (cg / name / "memory.reclaim").read_text() == "NEVER_WRITE_THIS_FIXTURE\n"
    run(build / "memshiftd", "--config", cfg, "--fixture", "--apply", ok=False)
    run(build / "memshiftd", "--config", cfg, "--observe", "--apply", ok=False)
    run(build / "memshiftd", "--config", cfg, "--apply", ok=False)
    run(build / "memshiftctl", "--file", ctl, "--pause", "true")
    cfg = config("paused")
    run(build / "memshiftd", "--config", cfg, "--fixture", "--ticks", "3")
    assert not list(csv.DictReader((base / "paused" / "reclaim.csv").open()))
    ctl.write_text("paused=false\n")
    cfg = config("invalid_control")
    run(build / "memshiftd", "--config", cfg, "--fixture", "--ticks", "3")
    rows = list(csv.DictReader((base / "invalid_control" / "system.csv").open()))
    assert rows and all(r["decision"] == "control_invalid" for r in rows)

    # Arithmetic/format test with toy data, NOT measured performance.
    a, b = base / "a.csv", base / "b.csv"
    header = "run_id,sequence,case_id,launch_fail,lowmem_popup,oom_events,launch_ms\n"
    a.write_text(header + "1,1,TOY_TEST,10,0,0,\n")
    b.write_text(header + "1,1,TOY_TEST,9,0,0,\n")
    out = run(build / "compare_runs", a, b)
    assert "Measured event reduction: 10.00%" in out.stdout
    b.write_text(header + "1,1,DIFFERENT_CASE,9,0,0,\n")
    run(build / "compare_runs", a, b, ok=False)
    a.write_text(header + "1,1,TOY_TEST,0,0,0,100\n")
    b.write_text(header + "1,1,TOY_TEST,0,0,0,90\n")
    out = run(build / "compare_runs", a, b)
    assert "baseline zero" in out.stdout
print("PASS: synthetic daemon/control/CSV integration checks; no kernel reclaim was executed")
