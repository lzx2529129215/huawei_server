#!/usr/bin/env python3
"""Exit 0 if a python process runs SCRIPT (with all extra ARGs among its arguments), else 1.
Looks at argv of python processes only, so it never matches the shell that calls it.
Usage: running.py SCRIPT [ARG ...]      e.g. running.py browser_trial.py chrome
"""
import os
import sys

want = [a.encode() for a in sys.argv[1:]]
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) == os.getpid():
        continue
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        continue
    if exe.startswith("python") and all(w in argv for w in want):
        sys.exit(0)
sys.exit(1)
