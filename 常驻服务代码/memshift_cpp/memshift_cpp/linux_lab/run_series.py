#!/usr/bin/env python3
"""Run interleaved rounds (e.g. observe 1, apply 1, observe 2, ...) so slow
drift on the host affects every mode alike, then aggregate them.

Each --variant is mode[:tag[:lead[:error]]], e.g. "observe::30", "apply::30",
"apply:_L10:10", "apply:_e+20:30:0.2". An empty lead means no oracle hints.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--first-round", type=int, default=1)
    ap.add_argument("--variant", action="append", required=True)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--policy-kill", default=None)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    name = json.loads(Path(a.scenario).read_text())["name"]
    dirs = []
    for r in range(a.first_round, a.first_round + a.rounds):
        for v in a.variant:
            parts = (v.split(":") + ["", "", ""])[:4]
            mode, tag, lead, err = parts
            cmd = [sys.executable, str(LAB / "run_round.py"), "--scenario", a.scenario, "--mode", mode,
                   "--round-id", str(r), "--out", str(out), "--tag", tag]
            if lead:
                cmd += ["--lead", lead]
            if err:
                cmd += ["--error", err]
            if a.policy_kill is not None:
                cmd += ["--policy-kill", a.policy_kill]
            for s in a.set:
                cmd += ["--set", s]
            res = subprocess.run(cmd, text=True, capture_output=True)
            line = (res.stdout.strip().splitlines() or [""])[-1]
            print(f"[{a.label}] round {r} {mode}{tag}: {line}", flush=True)
            if res.returncode != 0:
                print(res.stderr, file=sys.stderr, flush=True)
                continue
            dirs.append(out / f"{name}_{mode}{tag}_{r}")
    subprocess.run([sys.executable, str(LAB / "aggregate.py"), "--out", str(out), "--label", a.label] +
                   [str(d) for d in dirs])


if __name__ == "__main__":
    main()
