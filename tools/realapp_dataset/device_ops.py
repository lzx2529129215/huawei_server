from __future__ import annotations

import configparser
import json
import re
import secrets
import shlex
import time
from pathlib import Path

from memscenes.device import Device, quote as q
from memscenes.model import parse_stat
from .core import active_mount, memory, save
from .incidents import require_pressure_ready


class Target(Device):
    def checked(self, command: str, timeout=45) -> str:
        marker = "REALAPP_RC_" + secrets.token_hex(8)
        raw = self.shell("( " + command + "\n); rc=$?; printf '\\n" + marker + "=%s\\n' \"$rc\"", timeout=timeout)
        match = re.search(r"\n" + marker + r"=(\d+)\s*$", raw)
        if not match or int(match[1]):
            raise RuntimeError("device command failed: " + raw[-1500:])
        return raw[:match.start()]

    def read(self, path: str):
        return self.checked("cat " + q(path))

    def push(self, local: Path, remote: str):
        result = self.call("file", "send", str(local), remote, timeout=180)
        if "error" in result.lower() or "fail" in result.lower():
            raise RuntimeError(result)

    def text(self, remote: str, value: str):
        self.checked("printf %s " + q(value) + " > " + q(remote))

    def uptime(self):
        return float(self.read("/proc/uptime").split()[0])

    def mount(self):
        raw = self.checked("exec 9</dev/shm; cat /proc/self/fdinfo/9; printf '\\n--MOUNTS--\\n'; cat /proc/self/mountinfo")
        a, b = raw.split("\n--MOUNTS--\n", 1)
        return active_mount(a, b)

    def identity(self, pid):
        return parse_stat(self.read(f"/proc/{int(pid)}/stat").strip())

    def service(self):
        pids = self.checked("pidof memshiftd").split()
        if len(pids) != 1 or not pids[0].isdigit():
            raise RuntimeError("exactly one existing memshiftd is required")
        pid = int(pids[0]); ident = self.identity(pid)
        argv = self.checked(f"tr '\\000' '\\n' < /proc/{pid}/cmdline").splitlines()
        if "--config" not in argv or "--apply" not in argv:
            raise RuntimeError("expected current apply service")
        cfg_path = argv[argv.index("--config") + 1]
        cfg = self.read(cfg_path)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(cfg)
        ctl_path = parser["daemon"]["control_file"]
        ctl = self.read(ctl_path)
        if not re.search(r"(?m)^paused=false$", ctl):
            raise RuntimeError("original service is not active; preserve it and resolve before collecting")
        return {"pid": pid, "start_ticks": ident["start_ticks"], "argv": argv,
                "exe": self.checked(f"readlink /proc/{pid}/exe").strip(),
                "binary_sha256": self.checked(f"sha256sum /proc/{pid}/exe").split()[0],
                "config_path": cfg_path, "config": cfg, "control_path": ctl_path,
                "control": ctl, "log_dir": parser["daemon"]["log_dir"].rstrip("/") + "/harmony-v1-ratio"}


class Session:
    """Owned tmpfs file + exact control restoration; device watchdog precedes mutations."""
    def __init__(self, dev: Target, out: Path, phase: str, ttl=1800, *, manage_fill=True):
        self.dev, self.out, self.phase, self.ttl = dev, out, phase, ttl
        self.manage_fill = manage_fill
        self.token = secrets.token_hex(8)
        self.remote = "/data/local/tmp/realapp_dataset_" + self.token
        self.filldir = "/dev/shm/realapp_dataset_" + self.token
        self.fill = self.filldir + "/fill"
        self.started = False
        self.watchdog_armed = False
        self.deadline = None
        self.snapshot = None

    def prepare(self):
        self.out.mkdir(parents=True, exist_ok=True)
        d = self.dev
        save(self.out / "session.json", {"token":self.token,"remote":self.remote,"filldir":self.filldir,"phase":self.phase,"ttl":self.ttl,"manage_fill":self.manage_fill})
        self.snapshot = {"boot_id": d.read("/proc/sys/kernel/random/boot_id").strip(),
                         "mount": d.mount(), "service": d.service(),
                         "gate": d.read("/proc/sys/kernel/hyperhold/enable").strip(),
                         "gate_detail": d.read("/sys/block/zram0/hyperhold_enable").strip(),
                         "meminfo": d.read("/proc/meminfo"), "df": d.checked("df -k /dev/shm")}
        if self.snapshot["gate"] != "enable" or "reclaim_in enable" not in self.snapshot["gate_detail"]:
            raise RuntimeError("profile requires initially enabled disk and zram reclaim")
        save(self.out / "original_state.json", self.snapshot)
        d.checked("mkdir /data/local/tmp/realapp_dataset.lock")
        self.started = True
        d.text("/data/local/tmp/realapp_dataset.lock/owner", self.token)
        d.checked("mkdir " + q(self.remote) + (" " + q(self.filldir) if self.manage_fill else ""))
        d.text(self.remote + "/original_control", self.snapshot["service"]["control"])
        d.text(self.remote + "/control_path", self.snapshot["service"]["control_path"])
        script = self.cleanup_script()
        local = self.out / "restore.sh"
        local.write_text(script, encoding="utf-8", newline="\n")
        d.push(local, self.remote + "/restore.sh")
        watch = self.out / "watchdog.sh"
        watch.write_text(f"#!/bin/sh\nsleep {self.ttl}\nsh {q(self.remote + '/restore.sh')} watchdog\n", encoding="utf-8", newline="\n")
        d.push(watch, self.remote + "/watchdog.sh")
        self.deadline=d.uptime()+self.ttl
        pid = d.checked(f"nohup sh {q(self.remote + '/watchdog.sh')} > {q(self.remote + '/watchdog.log')} 2>&1 < /dev/null & echo $!").strip()
        if not pid.isdigit():
            raise RuntimeError("watchdog PID missing")
        save(self.out / "watchdog.json", d.identity(int(pid)))
        self.watchdog_armed = True
        self._check_mount()
        if self.manage_fill:
            d.checked("mount -o remount,size=32G /dev/shm")
            self._check_mount()
            if d.mount()["size"].lower() not in ("33554432k", "32g", "34359738368"):
                raise RuntimeError("32 GiB remount readback mismatch")
        self.set_phase()

    def _check_mount(self):
        if self.dev.mount()["mount_id"] != self.snapshot["mount"]["mount_id"]:
            raise RuntimeError("active mount changed")

    def set_phase(self):
        self.ensure_active()
        service = self.snapshot["service"]
        if self.dev.identity(service["pid"])["start_ticks"] != service["start_ticks"]:
            raise RuntimeError("service identity changed")
        control = service["control"]
        if self.phase == "observe":
            control = re.sub(r"(?m)^paused=false$", "paused=true", control)
        self.dev.text(self.remote + "/phase_control", control)
        self.dev.checked("cat " + q(self.remote + "/phase_control") + " > " + q(service["control_path"]))
        if self.dev.read(service["control_path"]) != control:
            raise RuntimeError("service control readback mismatch")
        time.sleep(2)  # drain the existing asynchronous action; verify ledger separately

    def allocate(self, mib: int):
        if not self.manage_fill:
            raise RuntimeError("externally managed occupancy: allocation is forbidden")
        self.ensure_active()
        if not 0 < mib < 32768:
            raise ValueError("invalid allocation")
        require_pressure_ready(self.dev.target, mib)
        d = self.dev
        d.text(self.remote + "/stage", "prepare")
        script = ("#!/bin/sh\n" + f"dd if=/dev/zero of={q(self.fill)} bs=1M count={mib} > {q(self.remote + '/dd.log')} 2>&1 &\n"
                  + "p=$!\n" + f"printf '%s\\n' \"$p\" > {q(self.remote + '/dd.pid')}\n"
                  + f"cat /proc/$p/stat > {q(self.remote + '/dd.stat')}\nwait $p\nrc=$?\n"
                  + f"printf '%s\\n' \"$rc\" > {q(self.remote + '/dd.rc')}\n")
        local = self.out / "allocate.sh"; local.write_text(script, encoding="utf-8", newline="\n")
        d.push(local, self.remote + "/allocate.sh")
        d.checked(f"nohup sh {q(self.remote + '/allocate.sh')} > {q(self.remote + '/allocate.log')} 2>&1 < /dev/null &")

    def allocation_result(self, mib):
        rc = self.dev.checked(f"if test -f {q(self.remote + '/dd.rc')}; then cat {q(self.remote + '/dd.rc')}; else printf pending; fi").strip()
        if rc == "pending":
            return None
        size = int(self.dev.checked("stat -c %s " + q(self.fill)).strip())
        result = {"rc": int(rc), "file_bytes": size, "expected_bytes": mib * 1024**2,
                  "df": self.dev.checked("df -k /dev/shm"),
                  "blocks": self.dev.checked("stat -c %b " + q(self.fill)).strip()}
        result["valid"] = result["rc"] == 0 and size == result["expected_bytes"] and int(result["blocks"])*512 >= size
        save(self.out / "allocation.json", result)
        return result

    def ensure_active(self):
        if self.deadline is None or self.dev.uptime()>=self.deadline-5:
            raise RuntimeError("device watchdog lease expired or too close to expiry")
        if self.dev.read("/data/local/tmp/realapp_dataset.lock/owner").strip()!=self.token:
            raise RuntimeError("session no longer owns device lease")
        if self.dev.read("/proc/sys/kernel/random/boot_id").strip()!=self.snapshot["boot_id"]:
            raise RuntimeError("device boot changed")

    def cleanup_script(self):
        s = self.snapshot; svc = s["service"]; r = self.remote
        fill_cleanup = f'''rm -f {q(self.fill)} || ok=0
rmdir {q(self.filldir)} || ok=0
test ! -e {q(self.fill)} || ok=0
mount -o remount,size={s['mount']['size']} /dev/shm || ok=0''' if self.manage_fill else "# External occupancy: no unlink, truncation, allocation or remount."
        # The boot, mount, service, ownership and PID start checks prevent stale restoration.
        return f'''#!/bin/sh
R={q(r)}
BOOT={q(s['boot_id'])}
test "$(cat /proc/sys/kernel/random/boot_id)" = "$BOOT" || exit 91
test "$(cat /data/local/tmp/realapp_dataset.lock/owner 2>/dev/null)" = {q(self.token)} || exit 92
mkdir "$R/restoring" 2>/dev/null || exit 93
exec > "$R/restore.log" 2>&1
printf '%s\\n' "$1"
touch "$R/stop"
ok=1
if test -f "$R/dd.pid" && test ! -f "$R/dd.rc"; then
 p=$(cat "$R/dd.pid")
 old=$(sed 's/.*) //' "$R/dd.stat" | awk '{{print $20}}')
 cur=$(sed 's/.*) //' /proc/$p/stat 2>/dev/null | awk '{{print $20}}')
 if test -n "$old" && test "$old" = "$cur"; then kill -TERM "$p"; sleep 1; fi
 cur=$(sed 's/.*) //' /proc/$p/stat 2>/dev/null | awk '{{print $20}}')
 if test -n "$old" && test "$old" = "$cur"; then kill -KILL "$p"; sleep 1; fi
fi
exec 9</dev/shm
mountid=$(awk '/^mnt_id:/ {{print $2}}' /proc/$$/fdinfo/9)
if test "$mountid" = {q(s['mount']['mount_id'])}; then
 {fill_cleanup}
 restored_mount=$(awk '$1 == {s['mount']['mount_id']} {{print}}' /proc/self/mountinfo)
 test "$restored_mount" = {q(s['mount']['raw'])} || ok=0
else
 printf 'mount_changed\\n'; ok=0
fi
printf '%s\\n' {q(s['gate'])} > /proc/sys/kernel/hyperhold/enable || ok=0
test "$(cat /proc/sys/kernel/hyperhold/enable)" = {q(s['gate'])} || ok=0
test "$(cat /sys/block/zram0/hyperhold_enable)" = {q(s['gate_detail'])} || ok=0
start=$(sed 's/.*) //' /proc/{svc['pid']}/stat 2>/dev/null | awk '{{print $20}}')
if test "$start" = {q(svc['start_ticks'])}; then
 cat "$R/original_control" > {q(svc['control_path'])} || ok=0
 cmp "$R/original_control" {q(svc['control_path'])} || ok=0
else
 printf 'service_identity_changed\\n'; ok=0
fi
if test "$ok" = 1; then
 printf verified > "$R/restored"
 rm -f /data/local/tmp/realapp_dataset.lock/owner
 rmdir /data/local/tmp/realapp_dataset.lock
else
 printf failed > "$R/restored"
fi
'''

    def restore(self):
        if not self.started:
            return {"attempted": False, "verified": True}
        result = {"attempted": True, "verified": False, "remote": self.remote}
        try:
            if not self.watchdog_armed:
                # No mount/control/allocation mutation occurs before arming.
                if self.manage_fill:
                    self.dev.checked("rmdir " + q(self.filldir) + " 2>/dev/null || true")
                self.dev.checked("if test \"$(cat /data/local/tmp/realapp_dataset.lock/owner 2>/dev/null)\" = " + q(self.token)
                                 + "; then rm /data/local/tmp/realapp_dataset.lock/owner && rmdir /data/local/tmp/realapp_dataset.lock; fi")
                result["verified"] = self.dev.mount() == self.snapshot["mount"]
                result["no_mutation_before_watchdog"] = True
                save(self.out / "restore.json",result)
                return result
            existing = self.dev.checked("if test -f " + q(self.remote + "/restored") + "; then cat " + q(self.remote + "/restored") + "; fi").strip()
            if existing != "verified":
                self.dev.checked("sh " + q(self.remote + "/restore.sh") + " host", timeout=30)
            result["receipt"] = self.dev.read(self.remote + "/restored")
            result["mount"] = self.dev.mount()
            result["verified"] = (result["receipt"] == "verified" and result["mount"] == self.snapshot["mount"]
                                  and self.dev.read(self.snapshot["service"]["control_path"]) == self.snapshot["service"]["control"]
                                  and self.dev.identity(self.snapshot["service"]["pid"])["start_ticks"]==self.snapshot["service"]["start_ticks"]
                                  and self.dev.read("/proc/sys/kernel/hyperhold/enable").strip()==self.snapshot["gate"]
                                  and self.dev.read("/sys/block/zram0/hyperhold_enable").strip()==self.snapshot["gate_detail"])
            result["log"] = self.dev.read(self.remote + "/restore.log")
        except Exception as exc:
            result["error"] = str(exc)
        save(self.out / "restore.json", result)
        return result


def recover(dev, folder: Path, after_reboot=False):
    from .core import load
    receipt=load(folder/"session.json")
    if not re.fullmatch(r"[a-f0-9]{16}",receipt["token"]):raise ValueError("invalid session token")
    session=Session(dev,folder,receipt["phase"],receipt["ttl"],manage_fill=receipt.get("manage_fill",True))
    session.token=receipt["token"]
    session.remote="/data/local/tmp/realapp_dataset_"+session.token
    session.filldir="/dev/shm/realapp_dataset_"+session.token;session.fill=session.filldir+"/fill"
    if session.remote!=receipt["remote"] or session.filldir!=receipt["filldir"]:raise ValueError("session path mismatch")
    session.snapshot=load(folder/"original_state.json");session.started=True;session.watchdog_armed=True
    boot=dev.read("/proc/sys/kernel/random/boot_id").strip()
    if boot!=session.snapshot["boot_id"]:
        if not after_reboot:raise RuntimeError("device rebooted; use --after-reboot for fresh-state verification")
        svc=session.snapshot["service"]
        if dev.read("/data/local/tmp/realapp_dataset.lock/owner").strip()!=session.token:
            raise RuntimeError("reboot recovery requires the original owned experiment lock")
        if dev.checked("sha256sum "+q(svc["exe"])).split()[0]!=svc["binary_sha256"] or dev.read(svc["config_path"])!=svc["config"]:
            raise RuntimeError("original service binary/config changed")
        original=svc["control"];paused=re.sub(r"(?m)^paused=false$","paused=true",original)
        if dev.read(svc["control_path"]) not in (original,paused):raise RuntimeError("control changed outside this experiment")
        if dev.mount()["size"]!=session.snapshot["mount"]["size"]:raise RuntimeError("new boot mount size differs; no stale remount")
        if dev.read("/proc/sys/kernel/hyperhold/enable").strip()!=session.snapshot["gate"]:raise RuntimeError("new boot gate differs")
        pids=dev.checked("pidof memshiftd || true").split()
        if pids:raise RuntimeError("a service already exists; preserve it and verify separately")
        dev.text(svc["control_path"],original)
        command=" ".join(q(x) for x in [svc["exe"]]+svc["argv"][1:])
        log="/data/local/tmp/realapp_recovery_"+session.token+".log"
        dev.checked(f"nohup {command} > {q(log)} 2>&1 < /dev/null &")
        time.sleep(2)
        current=dev.service()
        if current["binary_sha256"]!=svc["binary_sha256"] or current["config"]!=svc["config"] or current["control"]!=original:
            raise RuntimeError("restarted service verification failed")
        dev.checked("rm /data/local/tmp/realapp_dataset.lock/owner && rmdir /data/local/tmp/realapp_dataset.lock")
        result={"verified":True,"kind":"fresh_boot_recovery","old_boot_id":session.snapshot["boot_id"],"new_boot_id":boot,
                "new_service_pid":current["pid"],"new_service_start_ticks":current["start_ticks"],"training_run_valid":False}
        save(folder/"reboot_recovery.json",result);return result
    return session.restore()
