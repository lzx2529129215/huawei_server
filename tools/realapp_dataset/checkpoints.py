"""Periodic host copies preserve partial evidence when device logs disappear."""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path

from .core import save


class Checkpoints:
    FILES=("system.jsonl","processes.jsonl","kernel.jsonl","gate.jsonl","dd.log")

    def __init__(self,session):
        self.session=session
        self.stop=threading.Event()
        self.thread=None

    def start(self):
        self.thread=threading.Thread(target=self.run,name="realapp-host-checkpoints",daemon=True)
        self.thread.start()

    def run(self):
        s=self.session;out=s.out/"checkpoints";out.mkdir(exist_ok=True)
        while not self.stop.is_set():
            state={"host_monotonic":time.monotonic(),"files":{}}
            try:
                boot=s.dev.read("/proc/sys/kernel/random/boot_id").strip()
                if boot!=s.snapshot["boot_id"]:
                    state["boot_changed"]=True;save(out/"status.json",state);return
            except Exception as exc:
                state["connection_error"]=str(exc);save(out/"status.json",state)
                if self.stop.wait(5):return
                continue
            for name in self.FILES:
                if self.stop.is_set():break
                target=out/name;incoming=out/(name+".incoming")
                try:
                    # Each HDC process is independent; the device sampler does
                    # not depend on transfer completion or host timing.
                    response=s.dev.call("file","recv",s.remote+"/"+name,str(incoming),timeout=8)
                    if not incoming.is_file() or "fail" in response.lower():raise RuntimeError(response[-300:])
                    if target.exists() and incoming.stat().st_size<target.stat().st_size:
                        raise RuntimeError("device evidence unexpectedly shrank")
                    incoming.replace(target)
                    state["files"][name]={"bytes":target.stat().st_size}
                except Exception as exc:state["files"][name]={"error":str(exc)}
            save(out/"status.json",state)
            self.stop.wait(5)

    def finish(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=12)
        return self.thread is None or not self.thread.is_alive()
