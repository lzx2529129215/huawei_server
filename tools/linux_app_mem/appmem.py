#!/usr/bin/env python3
"""Measure one GUI app's memory inside a simulated 16 GiB machine (appmem.slice).

Each run: evict the app's file cache, start the app in its own systemd scope under
appmem.slice (MemoryMax=16G, MemorySwapMax=25G, MemoryZSwapMax=2G), sample the scope's
memory.stat every 100 ms, mark operation phases, take window screenshots, and stop.
Results are WSL measurements of the Linux build, not HarmonyOS data.
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

SLICE = os.environ.get("APPMEM_SLICE", "appmem.slice")   # test runs use another slice so they never share the 16G limit
CG_ROOT = Path(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/{SLICE}")
STAT_KEYS = ["anon", "file", "kernel", "shmem", "file_mapped", "file_dirty", "file_writeback",
             "zswap", "zswapped", "sock", "slab", "pagetables",
             "active_anon", "inactive_anon", "active_file", "inactive_file"]
MiB = 1 << 20


def sh(cmd, check=True, **kw):
    return subprocess.run(cmd, check=check, text=True, capture_output=True, **kw)


XVFB_DISPLAY = ":99"


def use_xvfb():
    """Run GUI apps on a private virtual screen so the user's desktop cannot steal input."""
    if sh(["systemctl", "--user", "is-active", "appmem-xvfb.service"], check=False).stdout.strip() != "active":
        sh(["systemd-run", "--user", "--unit=appmem-xvfb", "-q", "--", "/usr/bin/Xvfb", XVFB_DISPLAY,
            "-screen", "0", "3840x2160x24", "-dpi", "96", "-nolisten", "tcp"])
        time.sleep(2)
    os.environ["DISPLAY"] = XVFB_DISPLAY
    os.environ.pop("WAYLAND_DISPLAY", None)


def setup_slice():
    sh(["systemctl", "--user", "set-property", "--runtime", SLICE,
        "MemoryMax=16G", "MemorySwapMax=25G", "MemoryZSwapMax=2G"])


def evict(paths):
    """Drop clean page cache of every regular file under paths (no root needed)."""
    n = 0
    for root in paths:
        root = Path(root).expanduser()
        files = [root] if root.is_file() else (p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
        for p in files:
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                n += 1
            finally:
                os.close(fd)
    return n


VMA_RE = re.compile(r"^[0-9a-f]+-[0-9a-f]+ ")


def wss_kib(pids):
    """Pages referenced since the last clear_refs, summed over pids: (total, anon, file) KiB.
    Per VMA: anonymous VMAs count as anon; in file-backed VMAs the COW-anonymous part counts as
    anon (min(Referenced, Anonymous)) and the rest as file. Only mapped pages are visible here."""
    tot = anon = filek = 0
    for pid in pids:
        try:
            text = Path(f"/proc/{pid}/smaps").read_text()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        vmas, cur = [], None
        for line in text.splitlines():
            if VMA_RE.match(line):
                parts = line.split(None, 5)
                cur = {"file": len(parts) > 5 and parts[5].startswith("/"), "ref": 0, "anon": 0}
                vmas.append(cur)
            elif cur is not None and line.startswith("Referenced:"):
                cur["ref"] = int(line.split()[1])
            elif cur is not None and line.startswith("Anonymous:"):
                cur["anon"] = int(line.split()[1])
        for v in vmas:
            tot += v["ref"]
            if v["file"]:
                a = min(v["ref"], v["anon"]); anon += a; filek += v["ref"] - a
            else:
                anon += v["ref"]
    return tot, anon, filek


def wss_read_clear(pids):
    """Fixed-window working set: for every process read the pages referenced since its last
    clear_refs, then clear them right away. Returns (total, anon, file) KiB.
    Anonymous (private) pages are summed over processes. File-backed pages (incl. shared memory
    such as memfd, /dev/shm, /dev/zero) are counted once per file: the largest count any process
    has for that path (a page of libxul is one page however many processes map it; a lower bound
    when processes touch different pages of the same file)."""
    anon, files = 0, {}
    for pid in pids:
        try:
            text = Path(f"/proc/{pid}/smaps").read_text()
            with open(f"/proc/{pid}/clear_refs", "w") as f:
                f.write("1")
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        per, cur = {}, None
        for line in text.splitlines():
            if VMA_RE.match(line):
                parts = line.split(None, 5)
                path = parts[5] if len(parts) > 5 and parts[5].startswith("/") else None
                cur = {"path": path, "ref": 0, "anon": 0}
                per.setdefault(id(cur), cur)
            elif cur is not None and line.startswith("Referenced:"):
                cur["ref"] = int(line.split()[1])
            elif cur is not None and line.startswith("Anonymous:"):
                cur["anon"] = int(line.split()[1])
        mine = {}
        for v in per.values():
            if v["path"]:
                a = min(v["ref"], v["anon"]); anon += a
                mine[v["path"]] = mine.get(v["path"], 0) + v["ref"] - a
            else:
                anon += v["ref"]
        for k, n in mine.items():
            files[k] = max(files.get(k, 0), n)
    fl = sum(files.values())
    return anon + fl, anon, fl


def clear_refs(pids):
    for pid in pids:
        try:
            with open(f"/proc/{pid}/clear_refs", "w") as f:
                f.write("1")
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            pass


def read_stat(cg):
    d = {}
    for line in (cg / "memory.stat").read_text().splitlines():
        k, v = line.split()
        d[k] = int(v)
    return d


class Run:
    def __init__(self, app, run_id, out, period=0.1):
        self.app, self.run_id, self.period = app, run_id, period
        self.unit = f"appmem-{app}-{run_id}"
        self.cg = CG_ROOT / f"{self.unit}.scope"
        self.out = Path(out) / run_id
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "shots").mkdir(exist_ok=True)
        self.phase = "start"
        self.t0 = time.monotonic()
        # APPMEM_WSS=1: also sample per-process RSS/PSS every second and measure the working
        # set (pages referenced) of every phase with clear_refs.
        self.wss = os.environ.get("APPMEM_WSS") == "1"
        self._wss_t = None
        # APPMEM_WSS_WINDOW=10: instead, a fixed-window working set (Denning's w(t, tau)): the
        # referenced bits are cleared at every phase mark and then every tau seconds; each full
        # window gives one WSS(tau) sample ("wssw" events). A window cut short by a phase mark is
        # recorded too, with full=false.
        self.wss_win = float(os.environ["APPMEM_WSS_WINDOW"]) if os.environ.get("APPMEM_WSS_WINDOW") else None
        self._wss_lock = threading.Lock()
        self._stop = threading.Event()
        self.events = open(self.out / "events.jsonl", "w")

    # --- lifecycle -------------------------------------------------------
    def launch(self, argv, env=None):
        setup_slice()
        self.proc = subprocess.Popen(
            ["systemd-run", "--user", "--scope", "-q", f"--slice={SLICE}", f"--unit={self.unit}"]
            + (["-p", f"TimeoutStopSec={os.environ['APPMEM_STOP_TIMEOUT']}"] if os.environ.get("APPMEM_STOP_TIMEOUT") else [])
            + ["--"] + argv,
            stdout=open(self.out / "app.stdout", "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env)
        for _ in range(100):
            if (self.cg / "memory.stat").exists():
                break
            time.sleep(0.02)
        self.sampler = threading.Thread(target=self._sample, daemon=True)
        self.sampler.start()
        if self.wss:
            self.rss_sampler = threading.Thread(target=self._sample_rss, daemon=True)
            self.rss_sampler.start()
        if self.wss and self.wss_win:
            self.win_sampler = threading.Thread(target=self._sample_wss_window, daemon=True)
            self.win_sampler.start()
        self.mark("launch", argv=argv)

    def stop(self):
        self.mark("stop")
        self.smaps("before_stop")
        time.sleep(0.3)
        self._stop.set()
        self.sampler.join()
        if self.wss:
            self.rss_sampler.join()
        if self.wss and self.wss_win:
            self.win_sampler.join()
        sh(["systemctl", "--user", "stop", f"{self.unit}.scope"], check=False)
        for _ in range(100):
            if not self.cg.exists():
                break
            time.sleep(0.1)
        self.events.close()

    # --- recording -------------------------------------------------------
    def now(self):
        return time.monotonic() - self.t0

    def _window_event(self, full):
        """Close the current window (caller holds the lock) and open the next one."""
        pids, now = self.pids(), self.now()
        if self._wss_t is not None and pids:
            t, a, fl = wss_read_clear(pids)
            self.events.write(json.dumps({"t": round(self.now(), 3), "wssw": self.phase, "t0": round(self._wss_t, 3),
                                          "secs": round(now - self._wss_t, 2), "full": full,
                                          "total_kib": t, "anon_kib": a, "file_kib": fl}) + "\n")
            self.events.flush()
        elif pids:
            clear_refs(pids)
        self._wss_t = self.now()

    def _sample_wss_window(self):
        while not self._stop.is_set():
            time.sleep(0.05)
            with self._wss_lock:
                if self._wss_t is not None and self.now() - self._wss_t >= self.wss_win:
                    self._window_event(full=True)

    def mark(self, phase, **info):
        if self.wss and self.wss_win:
            with self._wss_lock:
                if self.phase not in ("start", "evicted"):
                    self._window_event(full=False)
                elif phase not in ("start", "evicted"):
                    self._wss_t = self.now()
                self.phase = phase
                self.events.write(json.dumps({"t": round(self.now(), 3), "phase": phase, **info}, ensure_ascii=False) + "\n")
                self.events.flush()
            return
        if self.wss and self.phase not in ("start", "evicted"):
            pids = self.pids()
            if self._wss_t is not None and pids:
                t, a, fl = wss_kib(pids)
                self.events.write(json.dumps({"t": round(self.now(), 3), "wss": self.phase,
                                              "secs": round(self.now() - self._wss_t, 1),
                                              "total_kib": t, "anon_kib": a, "file_kib": fl}) + "\n")
            clear_refs(pids)
        if self.wss and phase not in ("start", "evicted"):
            self._wss_t = self.now()
        self.phase = phase
        self.events.write(json.dumps({"t": round(self.now(), 3), "phase": phase, **info}, ensure_ascii=False) + "\n")
        self.events.flush()

    def _sample(self):
        with open(self.out / "samples.csv", "w") as f:
            f.write("t,phase,current," + ",".join(STAT_KEYS) + ",swap_current,nprocs\n")
            nxt = time.monotonic()
            while not self._stop.is_set():
                try:
                    st = read_stat(self.cg)
                    cur = int((self.cg / "memory.current").read_text())
                    sw = int((self.cg / "memory.swap.current").read_text())
                    npr = len((self.cg / "cgroup.procs").read_text().split())
                except (FileNotFoundError, ValueError):
                    break
                f.write(f"{self.now():.3f},{self.phase},{cur}," + ",".join(str(st.get(k, 0)) for k in STAT_KEYS)
                        + f",{sw},{npr}\n")
                nxt += self.period
                time.sleep(max(0.0, nxt - time.monotonic()))

    def _sample_rss(self, period=1.0):
        keys = ("Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem", "Referenced", "Anonymous")  # Anonymous = anon RSS
        with open(self.out / "rss.csv", "w") as f:
            f.write("t,phase,nprocs," + ",".join(k.lower() + "_kib" for k in keys) + "\n")
            nxt = time.monotonic()
            while not self._stop.is_set():
                pids, tot = self.pids(), dict.fromkeys(keys, 0)
                for pid in pids:
                    try:
                        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()[1:]:
                            k, v = line.split(":")
                            if k in tot:
                                tot[k] += int(v.split()[0])
                    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
                        continue
                f.write(f"{self.now():.3f},{self.phase},{len(pids)}," + ",".join(str(tot[k]) for k in keys) + "\n")
                f.flush()
                nxt += period
                time.sleep(max(0.0, nxt - time.monotonic()))

    def pids(self):
        try:
            return [int(p) for p in (self.cg / "cgroup.procs").read_text().split()]
        except FileNotFoundError:
            return []

    def smaps(self, label):
        rows = []
        for pid in self.pids():
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
                kv = {}
                for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()[1:]:
                    k, v = line.split(":")
                    kv[k.strip()] = int(v.split()[0])  # KiB
                rows.append({"pid": pid, "comm": comm, **{k: kv.get(k, 0) for k in
                             ("Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem", "Anonymous", "Swap")}})
            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
                continue
        self.events.write(json.dumps({"t": round(self.now(), 3), "smaps": label, "procs": rows}, ensure_ascii=False) + "\n")
        self.events.flush()

    # --- GUI -------------------------------------------------------------
    def windows(self):
        """Visible X windows owned by this run's processes, largest first."""
        found = []
        for pid in self.pids():
            r = sh(["xdotool", "search", "--onlyvisible", "--pid", str(pid)], check=False)
            for w in r.stdout.split():
                g = sh(["xdotool", "getwindowgeometry", "--shell", w], check=False).stdout
                kv = dict(line.split("=") for line in g.split() if "=" in line)
                name = sh(["xdotool", "getwindowname", w], check=False).stdout.strip()
                found.append((int(kv.get("WIDTH", 0)) * int(kv.get("HEIGHT", 0)), w, name))
        return [(w, name) for _, w, name in sorted(found, reverse=True)]

    def wait_window(self, pattern="", timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for w, name in self.windows():
                if pattern in name:
                    return w
            time.sleep(0.5)
        raise TimeoutError(f"no window matching {pattern!r}")

    def shot(self, label, window="root"):
        """Screenshot (default: the whole virtual screen, so popups and dialogs are included)."""
        p = self.out / "shots" / f"{len(list((self.out / 'shots').glob('*.png'))):02d}_{label}.png"
        sh(["import", "-window", str(window), str(p)], check=False)
        small = p.with_name(p.stem + "_small.png")
        sh(["convert", str(p), "-resize", "40%", str(small)], check=False)
        p.unlink(missing_ok=True)
        self.events.write(json.dumps({"t": round(self.now(), 3), "shot": small.name}) + "\n")
        return small

    def maximize(self, window, w=3840, h=2160):
        """No window manager on Xvfb: place the window over the whole screen ourselves."""
        sh(["xdotool", "windowmove", str(window), "0", "0"], check=False)
        sh(["xdotool", "windowsize", str(window), str(w), str(h)], check=False)

    def key(self, window, *keys, delay_ms=60):
        sh(["xdotool", "windowfocus", "--sync", str(window)], check=False)
        sh(["xdotool", "key", "--delay", str(delay_ms)] + list(keys), check=False)

    def type(self, window, text, delay_ms=30):
        sh(["xdotool", "windowfocus", "--sync", str(window)], check=False)
        sh(["xdotool", "type", "--delay", str(delay_ms), text], check=False)


def summarize(run_dir):
    """Per phase: mean over the phase and the sample where the total peaks (with its split)."""
    import csv
    rows = list(csv.DictReader(open(Path(run_dir) / "samples.csv")))
    out = {}
    for r in rows:
        out.setdefault(r["phase"], []).append(r)
    res = {}
    for phase, rs in out.items():
        pk = max(rs, key=lambda r: int(r["current"]))
        mib = lambda r, k: round(int(r[k]) / MiB, 1)
        res[phase] = {
            "seconds": round(float(rs[-1]["t"]) - float(rs[0]["t"]), 1),
            "mean_total": round(sum(int(r["current"]) for r in rs) / len(rs) / MiB, 1),
            "peak_t": float(pk["t"]),
            "peak": {k: mib(pk, k) for k in ("current", "anon", "file", "shmem", "kernel", "file_dirty", "zswapped", "swap_current")},
            "max_swap_or_zswap_mib": round(max(int(r["zswapped"]) + int(r["swap_current"]) for r in rs) / MiB, 1),
        }
    return res
