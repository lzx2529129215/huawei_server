#!/usr/bin/env python3
"""Which pages of shared system files are in the page cache (mincore; never faults pages in).

Used to split an app's file pages into "its own files" and "shared system files" (libraries,
fonts, icons, ...). Before a run the cache of these dirs is dropped; right before launch and
again after the app has exited, every file is scanned; pages that became resident in between
were read by the app. Several apps' bitmaps can then be OR-ed to count shared pages only once.

  scan(dirs) -> {path: (n_pages, bitmap bytes)} for files with at least one resident page
  diff(before, after) -> pages resident after but not before
  save(path, pages) / load(path); mib(pages); by_category(pages)
"""
import base64
import ctypes
import gzip
import json
import mmap
import os
from pathlib import Path

PAGE = os.sysconf("SC_PAGE_SIZE")
_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.mmap.restype = ctypes.c_void_p
_libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
_libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
_FAIL = ctypes.c_void_p(-1).value

CATEGORIES = [("系统库", "/usr/lib/x86_64-linux-gnu/"), ("字体", "/usr/share/fonts/"),
              ("图标/MIME/GLib/X11", "/usr/share/")]


def _pack(raw):
    bits = bytearray((len(raw) + 7) // 8)
    for i, b in enumerate(raw):
        if b & 1:
            bits[i >> 3] |= 1 << (i & 7)
    return bytes(bits)


def scan(dirs):
    out = {}
    for root in dirs:
        root = Path(root)
        files = [root] if root.is_file() else (p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
        for p in files:
            try:
                size = p.stat().st_size
                if size == 0:
                    continue
                fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                addr = _libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_SHARED, fd, 0)
                if addr is None or addr == _FAIL:
                    continue
                try:
                    n = (size + PAGE - 1) // PAGE
                    vec = (ctypes.c_ubyte * n)()
                    if _libc.mincore(ctypes.c_void_p(addr), size, vec) == 0:
                        raw = bytes(vec)
                        if raw.count(0) != n:
                            out[str(p)] = (n, _pack(raw))
                finally:
                    _libc.munmap(ctypes.c_void_p(addr), size)
            finally:
                os.close(fd)
    return out


def diff(before, after):
    out = {}
    for path, (n, bits) in after.items():
        old = before.get(path, (n, b""))[1]
        new = bytes(b & ~(old[i] if i < len(old) else 0) & 0xFF for i, b in enumerate(bits))
        if any(new):
            out[path] = (n, new)
    return out


def union(*page_sets):
    out = {}
    for ps in page_sets:
        for path, (n, bits) in ps.items():
            if path in out:
                old = out[path][1]
                out[path] = (n, bytes(a | b for a, b in zip(old.ljust(len(bits), b"\0"), bits)))
            else:
                out[path] = (n, bits)
    return out


def count(bits):
    return sum(bin(b).count("1") for b in bits)


def mib(pages):
    return sum(count(bits) for _, bits in pages.values()) * PAGE / 2**20


def by_category(pages):
    out = {name: 0.0 for name, _ in CATEGORIES}
    for path, (_, bits) in pages.items():
        for name, prefix in CATEGORIES:
            if path.startswith(prefix):
                out[name] += count(bits) * PAGE / 2**20
                break
    return out


def save(path, pages):
    with gzip.open(path, "wt") as f:
        json.dump({p: [n, base64.b64encode(b).decode()] for p, (n, b) in pages.items()}, f)


def load(path):
    with gzip.open(path, "rt") as f:
        return {p: (n, base64.b64decode(b)) for p, (n, b) in json.load(f).items()}


if __name__ == "__main__":
    import sys
    import time
    t0 = time.monotonic()
    s = scan(sys.argv[1:] or ["/usr/lib/x86_64-linux-gnu"])
    print(f"{len(s)} files with resident pages, {mib(s):.1f} MiB resident, scan {time.monotonic() - t0:.1f} s")


# ---- mapped files (works for root-owned files too; mincore() reports root-owned files as fully
# resident since Linux 5.2, so scan() above is only meaningful for files we own) ----
SHARED_PREFIXES = ("/usr/lib/x86_64-linux-gnu/", "/usr/share/fonts/", "/usr/share/icons/", "/usr/share/mime/",
                   "/usr/share/glib-2.0/", "/usr/share/X11/", "/usr/lib/locale/", "/usr/lib/x86_64-linux-gnu")


def mapped_files(pids):
    """{path: resident KiB} of file-backed mappings, max over the given processes (a page of a
    library mapped by several processes of the app is one physical page)."""
    out = {}
    for pid in pids:
        try:
            text = open(f"/proc/{pid}/smaps").read()
        except OSError:
            continue
        per = {}
        path = None
        for line in text.splitlines():
            if line[:1].isdigit() or line[:1] in "abcdef" and "-" in line.split()[0]:
                parts = line.split(None, 5)
                path = parts[5] if len(parts) > 5 and parts[5].startswith("/") else None
            elif path and line.startswith("Rss:"):
                per[path] = per.get(path, 0) + int(line.split()[1])
        for p, kib in per.items():
            if p.endswith(" (deleted)"):
                continue
            out[p] = max(out.get(p, 0), kib)
    return out


def split_mapped(files):
    shared = sum(k for p, k in files.items() if p.startswith(SHARED_PREFIXES))
    return {"shared_mib": shared / 1024, "own_mib": (sum(files.values()) - shared) / 1024}
