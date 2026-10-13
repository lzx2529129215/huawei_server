"""Owned device-local kernel capture; no kernel log settings are changed."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from .device import quote
from .model import parse_stat


class DeviceDmesgReader:
    """Small Popen-compatible handle, tied to PID/start_ticks, never to pidof."""

    def __init__(self, device, remote_root: str, index: int):
        if not re.fullmatch(r"/data/local/tmp/memscenes_s4_[a-f0-9]+", remote_root):
            raise ValueError("device kernel capture requires an owned Scene4 directory")
        self.device = device
        self.index = index
        self.raw_path = f"{remote_root}/kernel_{index}.raw.log"
        self.stderr_path = f"{remote_root}/kernel_{index}.stderr.log"
        self.returncode = None
        # All descriptors are detached from HDC before it returns. exec preserves
        # the recorded PID when nohup starts dmesg; no watchdog restarts it.
        command = (f"nohup dmesg -W > {quote(self.raw_path)} "
                   f"2> {quote(self.stderr_path)} < /dev/null & echo $!")
        response = device.shell(command, timeout=10).strip()
        if not response.isdigit():
            raise RuntimeError("device kernel reader PID was not returned")
        self.pid = int(response)
        time.sleep(0.2)
        identity = self._identity()
        if identity is None or identity["comm"] not in ("dmesg", "nohup"):
            raise RuntimeError("device kernel reader identity was not established")
        self.start_ticks = identity["start_ticks"]
        self.comm = identity["comm"]
        # HDC text transport truncates at NUL, so convert argv separators remotely.
        cmdline = device.shell(f"tr '\\000' '\\n' < /proc/{self.pid}/cmdline", timeout=10).strip()
        if cmdline.splitlines() not in (["nohup", "dmesg", "-W"], ["dmesg", "-W"]):
            raise RuntimeError("device kernel reader command does not match")

    def _identity(self):
        raw = self.device.shell(f"cat /proc/{self.pid}/stat 2>/dev/null", timeout=10,
                                check=False).strip()
        if not raw:
            return None
        try:
            return {**parse_stat(raw), "comm": raw.partition("(")[2].rpartition(")")[0],
                    "state": raw.rpartition(") ")[2].split()[0]}
        except (ValueError, IndexError) as exc:
            raise RuntimeError("device kernel reader identity is unreadable") from exc

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        identity = self._identity()
        if (identity is None or identity["start_ticks"] != self.start_ticks or
                identity["comm"] != self.comm or identity.get("state") == "Z"):
            # The actual device exit status is unavailable; never report success.
            self.returncode = -1
        return self.returncode

    def _signal(self, number: int):
        if self.poll() is not None:
            return
        # Verify start ticks and command in the same device shell as the signal.
        # The two known toybox applet comm values contain no special characters.
        command = (f"set -- $(cat /proc/{self.pid}/stat 2>/dev/null); "
                   f"if [ \"$2\" = '({self.comm})' ] && [ \"${{22}}\" = '{self.start_ticks}' ]; "
                   f"then kill -{number} {self.pid}; fi")
        self.device.shell(command, timeout=10, check=False)

    def terminate(self):
        self._signal(15)

    def kill(self):
        self._signal(9)

    def wait(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("owned device dmesg", timeout)
            time.sleep(0.1)
        return self.returncode

    def collect(self, local_raw: Path, local_stderr: Path):
        # The remote source remains available after collection or reader death.
        # The complete file is collected, not a ring-buffer tail presented as coverage.
        segment = local_raw.parent / f"kernel_segment_{self.index:04d}.raw.log"
        incoming = segment.with_suffix(".incoming")
        incoming.unlink(missing_ok=True)
        self.device.receive(self.raw_path, incoming)
        incoming.replace(segment)
        with local_raw.open("wb") as output:
            for path in sorted(local_raw.parent.glob("kernel_segment_*.raw.log")):
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
        self.device.receive(self.stderr_path, local_stderr)
