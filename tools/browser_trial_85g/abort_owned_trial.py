"""Fail-closed local/device abort helper for an invalid 8.5 GiB baseline.

Default mode is a local-only dry run. Device mutation requires --execute and
--host-confirmed-stopped; no result/report file is modified.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
FROZEN=ROOT/"results"/"daily_lead90_20261012_1640"/"candidate_runner"
BUNDLES=tuple(f"io.memscene.scene4.workload.a{i}" for i in (14,15,16))
BACKUPS=ROOT/"results"/"browser_donor_integration_20261013"
GATE_BIN=FROZEN/"native_source"/"gate_window"
EXPECTED_ORIGINAL_EXE="/data/local/tmp/memshift_wps_gate_20261013/memshiftd"
EXPECTED_ORIGINAL_INI="/data/local/tmp/memshift_wps_gate_20261013/wps_window_gate.ini"
EXPECTED_ORIGINAL_HASHES={
    "daemon":"60b871e051e7fb005fc7c4882ef1641374f9cb4bbb56b69574c216aa0904881f",
    "ini":"a081620837715a385ff5c4bd681c4a4a28443fbca609bd55b89d4f75b223d722",
    "control":"5062cae48b51ed17573558e265b80bed7905001e6cb2024aee3b03290becb80d",
}
CASE_RE=re.compile(r"s4_[0-9a-f]{12}_c0001\Z")
BOOT_RE=re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
NATIVE_ROOT_RE=re.compile(r"/data/local/tmp/memscenes_s4_[0-9a-f]{16}\Z")
SERVICE_ROOT_RE=re.compile(r"/data/local/tmp/browser_trial_[0-9a-f]{16}\Z")


def sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""):h.update(block)
    return h.hexdigest()


def read_json(path:Path)->dict:
    value=json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value,dict):raise ValueError(f"expected JSON object: {path}")
    return value


def _load_module(name:str,path:Path):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None:raise RuntimeError(f"cannot load helper {path}")
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


def _process_list()->list[dict]:
    """Use the Windows process table; do not infer host completion from a flag."""
    command=("$p=@(Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine); "
             "ConvertTo-Json -InputObject $p -Compress")
    result=subprocess.run(["powershell.exe","-NoLogo","-NoProfile","-NonInteractive","-Command",command],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=False)
    if result.returncode:
        raise RuntimeError("cannot inspect Win32 process table: "+result.stderr.decode("utf-8","replace")[:500])
    raw=result.stdout.decode("utf-8-sig","replace").strip()
    if not raw:return []
    value=json.loads(raw)
    if isinstance(value,dict):value=[value]
    return [x for x in value if isinstance(x,dict)]


def host_audit(result_dir:Path,expected_pid:int)->dict:
    processes=_process_list()
    expected=[p for p in processes if str(p.get("ProcessId"))==str(expected_pid)]
    script=str((HERE/"run_trial.py").resolve()).lower().replace("/","\\")
    script_suffix="tools\\browser_trial_85g\\run_trial.py"
    output=str(result_dir.resolve()).lower().replace("/","\\")
    try:relative_output=str(result_dir.resolve().relative_to(ROOT)).lower().replace("/","\\")
    except ValueError:relative_output=output
    matches=[]
    for p in processes:
        cmd=str(p.get("CommandLine") or "")
        norm=cmd.lower().replace("/","\\")
        if ((script in norm or script_suffix in norm) and "--output" in norm and
                (output in norm or relative_output in norm)):
            matches.append({"pid":p.get("ProcessId"),"command_line":cmd})
    return {"expected_host_pid":expected_pid,"expected_pid_absent":not bool(expected),
        "expected_pid_entries":[{"pid":p.get("ProcessId"),"command_line":p.get("CommandLine")} for p in expected],
        "matching_run_trial_processes":matches,"no_matching_run_trial_process":not matches,
        "stopped_verified":not expected and not matches}


def parse_kv_pipe(line:str)->dict[str,str]:
    fields=line.rstrip("\r\n").split("|")
    out={}
    for item in fields[2:]:
        if "=" in item:
            key,value=item.split("=",1);out[key]=value
    return out


def local_preflight(result_dir:Path,expected_host_pid:int)->dict:
    result_dir=result_dir.resolve(strict=True)
    required=("native_registration.json","service_swap.json","target_daily_ready.json",
        "native_retry_plan.tsv","browser_event_plan.tsv","service.generated.ini",
        "control.generated.conf","interference_target_cleanup.json")
    missing=[name for name in required if not (result_dir/name).is_file()]
    if missing:raise ValueError("required local abort receipts missing: "+", ".join(missing))
    reg=read_json(result_dir/"native_registration.json")
    swap=read_json(result_dir/"service_swap.json")
    ready=read_json(result_dir/"target_daily_ready.json")
    cleanup=read_json(result_dir/"interference_target_cleanup.json")
    if (ready.get("schema")!="browser_trial_three_target_daily_ready_v1" or ready.get("validated") is not True or
            set(ready.get("targets",{}))!=set(BUNDLES)):
        raise ValueError("target daily-ready receipt is not the exact validated a14-a16 set")
    case_id=reg.get("case_id")
    if not isinstance(case_id,str) or not CASE_RE.fullmatch(case_id) or ready.get("case_id")!=case_id:
        raise ValueError("case ID does not match registration and daily-ready proof")
    boot_id=reg.get("boot_id")
    if not isinstance(boot_id,str) or not BOOT_RE.fullmatch(boot_id):raise ValueError("registration boot ID is invalid")
    if reg.get("clock_domain")!="CLOCK_BOOTTIME":raise ValueError("registration clock domain is not CLOCK_BOOTTIME")
    T=float(reg.get("T",0))
    if T<=0:raise ValueError("registered fixed T is invalid")
    native_root=reg.get("remote_root","")
    if not NATIVE_ROOT_RE.fullmatch(native_root):raise ValueError("native remote root is not a randomized memscenes_s4 root")
    gate=reg.get("gate_window") or {}
    if (abs(float(gate.get("close",0))-(T-1))>0.01 or abs(float(gate.get("reopen",0))-(T+930))>0.01 or
            abs(float(gate.get("watchdog",0))-(T+935))>0.01 or gate.get("disk_swapout")!="forbidden" or
            gate.get("zram")!="allowed"):
        raise ValueError("registered gate window differs from this fixed 85g run")
    plan_path=result_dir/"native_retry_plan.tsv"
    plan_rows=plan_path.read_text(encoding="ascii").splitlines()
    if len(plan_rows)!=5 or plan_rows[0]!="device_retry_plan_browser_donor_3target_85g_v1":
        raise ValueError("native retry plan is not the closed 85g 3-target plan")
    meta=plan_rows[1].split("|")
    if len(meta)!=16 or meta[0]!=case_id or meta[1]!=boot_id or abs(float(meta[2])-T)>0.01:
        raise ValueError("registered case/boot/T do not match the 85g retry plan")
    native_plan=_load_module("abort_85g_native_plan",HERE/"native_plan.py")
    native_plan.validate_serialized(plan_path.read_text(encoding="ascii"))
    if sha(plan_path).lower()!=str(reg.get("plan_sha256","")).lower():raise ValueError("native retry plan hash differs from device registration")
    if reg.get("case_id")!=case_id:raise ValueError("case mismatch")
    for name in ("a14_peak_final.jsonl","a15_peak_final.jsonl","a16_peak_final.jsonl"):
        if (result_dir/name).exists():raise ValueError(f"peak journal exists; refusing abort classification: {name}")
    for n in (14,15,16):
        journal=result_dir/f"a{n}_daily_journal.jsonl"
        if journal.is_file():
            for line in journal.open("r",encoding="utf-8",errors="replace"):
                if '"event": "peak_begin"' in line or '"event": "peak_reached"' in line:
                    raise ValueError(f"peak event is present in local journal {journal.name}")
    if set(cleanup)!=set(BUNDLES) or any(row.get("verified_absent") is not True for row in cleanup.values()):
        raise ValueError("prior exact target cleanup receipt is absent or incomplete")
    service_root=swap.get("remote_root","")
    if not SERVICE_ROOT_RE.fullmatch(service_root):raise ValueError("service remote root is not randomized browser_trial root")
    original=swap.get("original") or {};candidate=swap.get("candidate") or {}
    if (original.get("exe")!=EXPECTED_ORIGINAL_EXE or original.get("config")!=EXPECTED_ORIGINAL_INI or
            original.get("hashes")!=EXPECTED_ORIGINAL_HASHES):
        raise ValueError("service_swap original identity/hashes differ from trusted original service")
    if candidate.get("exe")!=service_root+"/memshiftd" or candidate.get("config")!=service_root+"/service.ini" or candidate.get("control")!=service_root+"/control.conf":
        raise ValueError("candidate service exe/config/control do not bind the recorded randomized service root")
    if candidate.get("paused") is not True:raise ValueError("A01 candidate control was not recorded as paused baseline")
    if candidate.get("plan_sha256")!=sha(result_dir/"browser_event_plan.tsv"):
        raise ValueError("candidate browser plan hash does not match the local scenario plan")
    config=configparser.ConfigParser(interpolation=None,strict=True)
    config.read(result_dir/"service.generated.ini",encoding="utf-8")
    daemon=config["daemon"]
    if (daemon.get("control_file")!=candidate.get("control") or
            daemon.get("daily_known_plan_file")!=service_root+"/browser_event_plan.tsv" or
            daemon.get("daily_known_evidence_ledger")!=service_root+"/browser_evidence.tsv"):
        raise ValueError("generated candidate INI paths do not match its recorded service root")
    candidate_ini_sha=sha(result_dir/"service.generated.ini")
    if candidate.get("config_sha256")!=candidate_ini_sha:raise ValueError("candidate INI hash differs from service_swap")
    backups=BACKUPS
    expected_paths={"daemon":backups/"original_memshiftd","ini":backups/"original_service.ini",
                    "control":backups/"original_control.conf"}
    backup_hashes={key:sha(path) for key,path in expected_paths.items() if path.is_file()}
    if backup_hashes!=EXPECTED_ORIGINAL_HASHES:raise ValueError("trusted original service backup set is missing or hash-mismatched")
    host=host_audit(result_dir,expected_host_pid)
    local_gate_hash=sha(GATE_BIN) if GATE_BIN.is_file() else None
    if local_gate_hash!=reg.get("gate_sha256"):
        raise ValueError("local frozen gate_window hash differs from native registration")
    return {"result_dir":str(result_dir),"case_id":case_id,"boot_id":boot_id,"T":T,
        "native_remote_root":native_root,"service_remote_root":service_root,
        "registration_sha256":sha(result_dir/"native_registration.json"),
        "service_swap_sha256":sha(result_dir/"service_swap.json"),"plan_sha256":sha(plan_path),
        "target_ready_sha256":sha(result_dir/"target_daily_ready.json"),
        "gate_binary_local_sha256":local_gate_hash,
        "phase":"baseline","peak_journals_absent":True,"prior_target_cleanup_verified":True,
        "trusted_original_backup_hashes":backup_hashes,"host_process_audit":host,
        "execute_requirements":["--execute","--host-confirmed-stopped","no matching Win32 run_trial.py process",
            "same-boot gate/controller/collector identities verified before signals"]}


def _device_shell(device,command:str,timeout:int=10,check:bool=True)->str:
    return device.shell(command,timeout=timeout,check=check)


def _kv_from_pipe(line:str)->dict[str,str]:
    fields=line.strip().split("|")
    out={}
    for item in fields[1:]:
        if "=" in item:
            k,v=item.split("=",1);out[k]=v
    return out


def _validate_gate_logs(events:str,watchdog:str,reg:dict)->dict:
    event_lines=events.splitlines(); watchdog_lines=watchdog.splitlines()
    pre=[line for line in event_lines if line.startswith("PREFLIGHT|")]
    helper=[line for line in event_lines if line.startswith("GATE_HELPER|")]
    armed=[line for line in watchdog_lines if line.startswith("WATCHDOG_ARMED|")]
    if len(pre)!=1 or len(helper)!=1 or len(armed)!=1:
        raise ValueError("gate logs must contain exactly one PREFLIGHT, GATE_HELPER, WATCHDOG_ARMED")
    f=pre[0].split("|")
    if len(f)<4 or abs(float(f[1])-float(reg["T"]))>0.01:raise ValueError("gate PREFLIGHT T mismatch")
    pf=_kv_from_pipe(pre[0])
    if pf.get("boot_match")!="1" or pf.get("proc")!="enable" or pf.get("reclaim_in")!="enable":
        raise ValueError("gate PREFLIGHT does not prove same-boot initial enable/reclaim-in enable")
    if abs(float(pf.get("close_at","nan"))-(float(reg["T"])-1))>0.01 or abs(float(pf.get("open_at","nan"))-(float(reg["T"])+930))>0.01 or abs(float(pf.get("watchdog_at","nan"))-(float(reg["T"])+935))>0.01:
        raise ValueError("gate PREFLIGHT deadlines differ from the registered window")
    hf=_kv_from_pipe(helper[0]); wf=_kv_from_pipe(armed[0])
    helper_pid=int(hf.get("pid",0)); helper_start=int(hf.get("start_ticks",0))
    watch_pid=int(hf.get("watchdog_pid",0)); watch_start=int(hf.get("watchdog_start_ticks",0))
    if (helper_pid!=int(reg.get("gate_wrapper_pid",0)) or helper_start!=int(reg.get("gate_wrapper_pid_start_ticks",0)) or
            watch_pid<=0 or watch_start<=0 or int(wf.get("pid",0))!=watch_pid or int(wf.get("start_ticks",0))!=watch_start):
        raise ValueError("gate helper/watchdog IDs and start ticks do not match their registration records")
    deadline=float(reg["gate_window"]["watchdog"])
    if abs(float(hf.get("deadline","nan"))-deadline)>0.01 or abs(float(wf.get("deadline","nan"))-deadline)>0.01:
        raise ValueError("gate helper/watchdog deadline differs from registration")
    if any(line.startswith(("FINAL|","GATE_RESULT|","WATCHDOG_EXIT|")) for line in event_lines):
        raise ValueError("gate already has terminal records; this active-owner abort path is not applicable")
    return {"preflight":pre[0],"helper":hf,"helper_record":helper[0],"watchdog_armed":wf,
        "watchdog_record":armed[0],"poll_rows":sum(line.startswith("POLL|") for line in event_lines),
        "initial_enable_verified":True,"same_registered_gate_identity":True}


def _snapshot_script(pid:int)->str:
    return (f"p={pid}; s=$(cat /proc/$p/stat 2>/dev/null) || {{ echo GONE; exit; }}; "
        "r=${s##*)}; set -- $r; [ $# -ge 20 ] || { echo GONE; exit; }; st=${20}; state=$1; "
        "x=$(readlink /proc/$p/exe 2>/dev/null); c=$(tr '\\000' ' ' < /proc/$p/cmdline 2>/dev/null); c=${c% }; "
        "printf 'SNAP|%s|%s|%s|%s|%s\\n' $p $st $state \"$x\" \"$c\"; "
        "s=$(cat /proc/$p/stat 2>/dev/null) || { echo GONE; exit; }; "
        "r=${s##*)}; set -- $r; [ $# -ge 20 ] || { echo GONE; exit; }; st=${20}; state=$1; "
        "x=$(readlink /proc/$p/exe 2>/dev/null); c=$(tr '\\000' ' ' < /proc/$p/cmdline 2>/dev/null); c=${c% }; "
        "printf 'SNAP|%s|%s|%s|%s|%s\\n' $p $st $state \"$x\" \"$c\"")


def _parse_snapshots(raw:str,pid:int)->list[dict]:
    rows=[]
    for line in raw.splitlines():
        if not line.startswith("SNAP|"):continue
        f=line.split("|",5)
        if len(f)==6 and f[1].isdecimal() and f[2].isdecimal():
            rows.append({"pid":int(f[1]),"start_ticks":int(f[2]),"state":f[3],"exe":f[4],"cmdline":f[5]})
    if "GONE" in raw:return []
    if len(rows)!=2 or rows[0]!=rows[1] or rows[0]["pid"]!=pid:
        raise RuntimeError(f"PID {pid} failed the two-snapshot identity sandwich: {raw[:1200]}")
    return rows


def _owned_snapshot(device,pid:int,expected_start:int,expected_exe:str,expected_argv:list[str])->dict:
    raw=_device_shell(device,_snapshot_script(pid),timeout=8,check=False)
    rows=_parse_snapshots(raw,pid)
    if not rows:raise RuntimeError(f"owned PID {pid} disappeared before the identity sandwich")
    row=rows[-1]
    try:argv=shlex.split(row["cmdline"])
    except ValueError as exc:raise RuntimeError(f"cannot parse PID {pid} cmdline") from exc
    if row["start_ticks"]!=expected_start or row["exe"]!=expected_exe or argv!=expected_argv:
        raise RuntimeError(f"PID {pid} is not the exact registered owner: {row}")
    return row


def _signal_term_exact(device,row:dict,expected_argv:list[str])->str:
    pid=row["pid"]; ticks=row["start_ticks"]; exe=shlex.quote(row["exe"])
    cmd=shlex.quote(" ".join(expected_argv))
    script=(f"p={pid}; s=$(cat /proc/$p/stat 2>/dev/null) || {{ echo ABSENT; exit 0; }}; "
        "r=${s##*)}; set -- $r; [ $# -ge 20 ] || { echo ABSENT; exit 0; }; t=${20}; "
        "x=$(readlink /proc/$p/exe 2>/dev/null); c=$(tr '\\000' ' ' < /proc/$p/cmdline 2>/dev/null); c=${c% }; "
        f"if [ \"$t\" = '{ticks}' ] && [ \"$x\" = {exe} ] && [ \"$c\" = {cmd} ]; "
        f"then kill -TERM {pid} && echo TERM_SENT; else echo IDENTITY_CHANGED; fi")
    return _device_shell(device,script,timeout=8,check=False).strip()


def _wait_pid_gone(device,pid:int,start_ticks:int,timeout_s:float=5.0)->dict:
    deadline=time.monotonic()+timeout_s; last=None
    while time.monotonic()<deadline:
        raw=_device_shell(device,_snapshot_script(pid),timeout=6,check=False)
        rows=_parse_snapshots(raw,pid)
        if not rows:return {"gone_or_generation_changed":True,"last_snapshot":raw.strip()}
        last=rows[-1]
        if last["start_ticks"]!=start_ticks:return {"gone_or_generation_changed":True,"last_snapshot":last}
        time.sleep(0.2)
    return {"gone_or_generation_changed":False,"last_snapshot":last}


def _device_preflight(device,local:dict,result_dir:Path,evidence_dir:Path)->dict:
    reg=read_json(result_dir/"native_registration.json")
    swap=read_json(result_dir/"service_swap.json")
    boot=_device_shell(device,"cat /proc/sys/kernel/random/boot_id").strip()
    selinux=_device_shell(device,"getenforce").strip()
    if boot!=reg["boot_id"]:raise RuntimeError("device boot ID changed since native registration")
    if selinux!="Enforcing":raise RuntimeError(f"SELinux is not Enforcing: {selinux}")
    remote_root=reg["remote_root"]
    events_path=remote_root+"/gate.events.tsv"; watchdog_path=remote_root+"/gate.watchdog.tsv"
    events=_device_shell(device,"cat "+shlex.quote(events_path),timeout=12)
    watchdog=_device_shell(device,"cat "+shlex.quote(watchdog_path),timeout=12)
    gate_evidence=_validate_gate_logs(events,watchdog,reg)
    hashes_raw=_device_shell(device,"sha256sum "+" ".join(map(shlex.quote,(remote_root+"/gate_window",remote_root+"/controller",remote_root+"/plan.tsv"))),timeout=10)
    remote_hashes={}
    for line in hashes_raw.splitlines():
        fields=line.split()
        if len(fields)>=2:remote_hashes[fields[-1]]=fields[0].lower()
    if remote_hashes.get(remote_root+"/gate_window")!=reg.get("gate_sha256"):raise RuntimeError("remote gate binary hash differs from registration")
    if remote_hashes.get(remote_root+"/controller")!=reg.get("controller_sha256"):raise RuntimeError("remote controller hash differs from registration")
    if remote_hashes.get(remote_root+"/plan.tsv")!=reg.get("plan_sha256"):raise RuntimeError("remote controller plan hash differs from registration")
    native_rc=_device_shell(device,"cat "+shlex.quote(remote_root+"/native.rc"),timeout=8).strip()
    if native_rc!="7":raise RuntimeError(f"abort requires exact native.rc=7, observed {native_rc!r}")
    controller_stderr=_device_shell(device,"cat "+shlex.quote(remote_root+"/stderr.log"),timeout=8)
    if "CONTROLLER_INVALID journal_bounds" not in controller_stderr:
        raise RuntimeError("controller stderr does not prove the expected journal_bounds abort")
    identity_raw=_device_shell(device,"cat "+shlex.quote(remote_root+"/identity.tsv"),timeout=8).strip().split("\t")
    if (len(identity_raw)!=5 or identity_raw[0]!=boot or identity_raw[3]!=remote_root+"/controller" or
            identity_raw[4]!=reg.get("plan_sha256") or not identity_raw[1].isdecimal() or not identity_raw[2].isdecimal()):
        raise RuntimeError("native controller identity receipt is missing or mismatched")
    controller_pid=int(identity_raw[1]); controller_start=int(identity_raw[2])
    controller_rows=_parse_snapshots(_device_shell(device,_snapshot_script(controller_pid),timeout=8,check=False),controller_pid)
    controller_alive=bool(controller_rows and controller_rows[-1]["start_ticks"]==controller_start and
        controller_rows[-1]["exe"]==remote_root+"/controller")
    if controller_alive:raise RuntimeError("registered native controller is still alive; refusing abort")
    requests_remote=remote_root+"/requests.tsv"
    requests=_device_shell(device,"cat "+shlex.quote(requests_remote),timeout=10)
    request_rows=[line for line in requests.splitlines() if line.strip() and not line.startswith("case_id\t")]
    if len(request_rows)!=3 or any(not line.startswith("initial_pending\t") for line in request_rows):
        raise RuntimeError("remote request ledger is not exactly three initial_pending rows; refusing abort")
    evidence_dir.mkdir(parents=True,exist_ok=True)
    (evidence_dir/"gate.events.pre_abort.tsv").write_text(events,encoding="utf-8",newline="")
    (evidence_dir/"gate.watchdog.pre_abort.tsv").write_text(watchdog,encoding="utf-8",newline="")
    (evidence_dir/"requests.pre_abort.tsv").write_text(requests,encoding="utf-8",newline="")
    (evidence_dir/"requests.tsv").write_text(requests,encoding="utf-8",newline="")
    (evidence_dir/"controller.stderr.pre_abort.log").write_text(controller_stderr,encoding="utf-8",newline="")
    gate_pid=int(gate_evidence["helper"]["pid"]); gate_start=int(gate_evidence["helper"]["start_ticks"])
    watchdog_pid=int(gate_evidence["watchdog_armed"]["pid"]); watchdog_start=int(gate_evidence["watchdog_armed"]["start_ticks"])
    executable=remote_root+"/gate_window"
    argv=[executable,"--T",f"{float(reg['T']):.9f}","--boot",boot,"--out",remote_root+"/gate",
        "--open-offset","930","--watchdog-offset","935"]
    gate_process=_owned_snapshot(device,gate_pid,gate_start,executable,argv)
    watchdog_process=_owned_snapshot(device,watchdog_pid,watchdog_start,executable,argv)
    gate_state=_device_shell(device,"cat /proc/sys/kernel/hyperhold/enable; cat /sys/block/zram0/hyperhold_enable 2>/dev/null",timeout=8).splitlines()
    if not gate_state or gate_state[0].strip() not in ("enable","disable"):
        raise RuntimeError("current hyperhold gate state is unreadable/unknown")
    # Read service instance without changing it. __exit__ later owns exact teardown/restore.
    sys.path.insert(0,str(FROZEN))
    from memscenes.device import Device  # already selected HDC target
    lifecycle_mod=_load_module("abort_85g_service_lifecycle",HERE/"service_lifecycle.py")
    swap=read_json(result_dir/"service_swap.json")
    plan_path=result_dir/"browser_event_plan.tsv"
    donor=read_json(result_dir/"browser_manifest.json")
    package_uid=int(donor["uid"])
    lifecycle=lifecycle_mod.BrowserServiceLifecycle(device,"baseline",HERE/"browser_profile_template.ini",
        plan_path,package_uid,BACKUPS,evidence_dir/"service_lifecycle")
    lifecycle.remote_root=swap["remote_root"]
    lifecycle.original_pid=int(swap["original"]["pid"]); lifecycle.original_start=int(swap["original"]["start_ticks"])
    lifecycle.original_exe_path=swap["original"]["exe"]
    lifecycle.original_ini_path=swap["original"]["config"]
    lifecycle.original_control_path=swap["original"]["control"]
    lifecycle.original_argv=list(swap["original"]["argv"])
    lifecycle.candidate_pid=int(swap["candidate"]["pid"]); lifecycle.candidate_start=int(swap["candidate"]["start_ticks"])
    lifecycle.candidate_daemon_path=swap["candidate"]["exe"]
    lifecycle.candidate_ini_path=swap["candidate"]["config"]
    lifecycle.candidate_control_path=swap["candidate"]["control"]
    lifecycle.candidate_plan_path=swap["remote_root"]+"/browser_event_plan.tsv"
    lifecycle.candidate_ledger_path=swap["remote_root"]+"/browser_evidence.tsv"
    lifecycle.candidate_log_path=swap["remote_root"]+"/memshiftd.log"
    lifecycle.original_stopped=True; lifecycle.original_stop_attempted=True; lifecycle.switched=True
    pids=lifecycle._pids()
    if len(pids)>1:raise RuntimeError(f"multiple memshiftd instances; refusing lifecycle restore: {pids}")
    service_state="no_resident_memshiftd"
    if pids:
        pid=pids[0]; ticks=lifecycle._stat_start(pid); exe=lifecycle._exe(pid)
        config_path=lifecycle._config_arg(lifecycle._cmdline(pid))
        if pid==lifecycle.candidate_pid and ticks==lifecycle.candidate_start and exe==lifecycle.candidate_daemon_path and config_path==lifecycle.candidate_ini_path:
            service_state="exact_candidate_live"
        elif pid==lifecycle.original_pid and ticks==lifecycle.original_start and exe==lifecycle.original_exe_path and config_path==lifecycle.original_ini_path:
            service_state="exact_original_already_live"
        else:raise RuntimeError("resident service is neither recorded candidate nor original; refusing abort")
    return {"boot_id":boot,"selinux":selinux,"gate_preflight":gate_evidence,
        "native_rc":int(native_rc),"native_controller_identity":{"pid":controller_pid,"start_ticks":controller_start,"observed_alive":controller_alive},
        "request_rows":len(request_rows),"request_states":[line.split("\t",1)[0] for line in request_rows],
        "gate_current_state":gate_state[0].strip(),"gate_current_reclaim_in":gate_state[1].strip() if len(gate_state)>1 else None,
        "gate_helper_live_identity":gate_process,"watchdog_live_identity":watchdog_process,
        "remote_hashes":remote_hashes,"service_state":service_state,
        "service_lifecycle":lifecycle,"saved_raw_gate_evidence":True}


def _write_receipt(path:Path,receipt:dict)->None:
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    os.replace(temp,path)


def execute_abort(result_dir:Path,hdc:Path,target:str,expected_host_pid:int,host_confirmed:bool)->dict:
    if not host_confirmed:raise ValueError("--execute requires --host-confirmed-stopped")
    local=local_preflight(result_dir,expected_host_pid)
    if not local["host_process_audit"]["stopped_verified"]:
        raise RuntimeError("host runner is not stopped: "+json.dumps(local["host_process_audit"],ensure_ascii=False))
    if not hdc.is_file():raise ValueError(f"HDC executable not found: {hdc}")
    receipt_path=result_dir/"abort_receipt.json"
    if receipt_path.exists():raise ValueError("abort_receipt.json already exists; refusing to overwrite prior abort evidence")
    evidence_dir=result_dir/"abort_evidence"
    sys.path.insert(0,str(FROZEN))
    from memscenes.device import Device
    device=Device(str(hdc.resolve()),target)
    receipt={"schema":"browser_trial_85g_owned_abort_v1","trial_status":"invalid_aborted_never_valid_baseline",
        "valid_baseline":False,"result_dir":str(result_dir.resolve()),"target":target,
        "expected_host_pid":expected_host_pid,"local_preflight":local,"operations":[],"errors":[]}
    lifecycle=None; gate_owners_stopped=False; same_boot=False
    try:
        preflight=_device_preflight(device,local,result_dir,evidence_dir)
        lifecycle=preflight.pop("service_lifecycle")
        receipt["device_preflight"]=preflight
        same_boot=True
        remote_root=read_json(result_dir/"native_registration.json")["remote_root"]
        gate_info=preflight["gate_preflight"]
        helper=preflight["gate_helper_live_identity"]; watchdog=preflight["watchdog_live_identity"]
        helper_argv=shlex.split(helper["cmdline"]); watchdog_argv=shlex.split(watchdog["cmdline"])
        outcomes={}
        for key,row,argv in (("gate_helper",helper,helper_argv),("watchdog",watchdog,watchdog_argv)):
            signal_result=_signal_term_exact(device,row,argv)
            gone=_wait_pid_gone(device,row["pid"],row["start_ticks"],timeout_s=5)
            outcomes[key]={"identity":row,"signal_result":signal_result,"exit_check":gone}
        gate_owners_stopped=all(item["exit_check"]["gone_or_generation_changed"] for item in outcomes.values())
        receipt["operations"].append({"step":"term_exact_gate_and_watchdog","owners":outcomes,
            "both_gone":gate_owners_stopped,"no_wide_kill_used":True})
        try:
            events_after=_device_shell(device,"cat "+shlex.quote(remote_root+"/gate.events.tsv"),timeout=10,check=False)
            watchdog_after=_device_shell(device,"cat "+shlex.quote(remote_root+"/gate.watchdog.tsv"),timeout=10,check=False)
            (evidence_dir/"gate.events.after_abort.tsv").write_text(events_after,encoding="utf-8",newline="")
            (evidence_dir/"gate.watchdog.after_abort.tsv").write_text(watchdog_after,encoding="utf-8",newline="")
            receipt["operations"].append({"step":"preserve_gate_log_after_abort","events_sha256":sha(evidence_dir/"gate.events.after_abort.tsv"),
                "watchdog_sha256":sha(evidence_dir/"gate.watchdog.after_abort.tsv"),"synthetic_final_record_added":False})
        except Exception as exc:receipt["errors"].append("post-abort gate log collection: "+str(exc))
    except Exception as exc:
        receipt["errors"].append("device preflight/gate abort: "+f"{type(exc).__name__}: {exc}")
    finally:
        if same_boot and gate_owners_stopped:
            try:
                before=_device_shell(device,"cat /proc/sys/kernel/random/boot_id; cat /proc/sys/kernel/hyperhold/enable; cat /sys/block/zram0/hyperhold_enable 2>/dev/null",timeout=8).splitlines()
                if not before or before[0].strip()!=read_json(result_dir/"native_registration.json")["boot_id"]:
                    raise RuntimeError("boot changed before gate restore")
                prior=before[1].strip() if len(before)>1 else ""
                command="if [ \"$(cat /proc/sys/kernel/random/boot_id)\" = "+shlex.quote(before[0].strip())+" ]; then printf 'enable\\n' > /proc/sys/kernel/hyperhold/enable; cat /proc/sys/kernel/hyperhold/enable; else echo BOOT_CHANGED; fi"
                write_readback=_device_shell(device,command,timeout=8,check=False).strip()
                after=_device_shell(device,"cat /proc/sys/kernel/random/boot_id; cat /proc/sys/kernel/hyperhold/enable; cat /sys/block/zram0/hyperhold_enable 2>/dev/null",timeout=8).splitlines()
                verified=len(after)>=2 and after[0].strip()==before[0].strip() and after[1].strip()=="enable"
                receipt["operations"].append({"step":"restore_only_hyperhold_proc_enable","before":prior,
                    "write_readback":write_readback,"after":after,"verified":verified,
                    "no_zram_sysfs_write":True,"same_boot":bool(after and after[0].strip()==before[0].strip())})
                if not verified:receipt["errors"].append("hyperhold enable restore/readback did not verify")
            except Exception as exc:receipt["errors"].append("hyperhold gate restore: "+f"{type(exc).__name__}: {exc}")
        elif same_boot:
            receipt["errors"].append("gate helper/watchdog disappearance was not verified; no sysctl write attempted")
        if same_boot:
            try:
                reg=read_json(result_dir/"native_registration.json")
                bridge=_load_module("abort_85g_native_bridge",HERE/"native_bridge.py")
                collector=bridge.stop_reader(None,device,reg["kernel_collector_remote_root"],result_dir/"abort_evidence")
                receipt["operations"].append({"step":"stop_exact_owned_kernel_collector","result":collector})
                if collector.get("terminated") is not True:receipt["errors"].append("owned collector was not confirmed terminated")
            except Exception as exc:receipt["errors"].append("collector cleanup: "+f"{type(exc).__name__}: {exc}")
            try:
                target_prepare=_load_module("abort_85g_target_prepare",HERE/"target_prepare.py")
                runner=_load_module("abort_85g_run_trial_helpers",HERE/"run_trial.py")
                ready=read_json(result_dir/"target_daily_ready.json")
                (evidence_dir/"native_retry_plan.tsv").write_bytes((result_dir/"native_retry_plan.tsv").read_bytes())
                if not (evidence_dir/"requests.tsv").is_file():
                    # The exact request ledger was downloaded during the validated preflight.
                    remote=read_json(result_dir/"native_registration.json")["remote_root"]+"/requests.tsv"
                    device.receive(remote,evidence_dir/"requests.tsv")
                owned=runner._owned_case_ids(evidence_dir,ready["case_id"])
                cleanup=target_prepare.cleanup_targets(device,ready,ready["case_id"],owned)
                receipt["operations"].append({"step":"cleanup_exact_owned_targets","result":cleanup})
                if set(cleanup)!=set(BUNDLES) or any(v.get("verified_absent") is not True for v in cleanup.values()):
                    receipt["errors"].append("one or more exact target cleanup checks failed")
            except Exception as exc:receipt["errors"].append("target cleanup: "+f"{type(exc).__name__}: {exc}")
        if lifecycle is not None:
            try:
                lifecycle.__exit__(None,None,None)
                receipt["operations"].append({"step":"restore_original_service_via_lifecycle_exit","result":lifecycle.restore_result})
                if lifecycle.restore_result.get("verified") is not True:receipt["errors"].append("original service restore not verified")
            except Exception as exc:
                receipt["operations"].append({"step":"restore_original_service_via_lifecycle_exit","result":lifecycle.restore_result})
                receipt["errors"].append("original service restore: "+f"{type(exc).__name__}: {exc}")
    try:
        receipt["final_readback"]={"boot_id":_device_shell(device,"cat /proc/sys/kernel/random/boot_id",timeout=8).strip(),
            "selinux":_device_shell(device,"getenforce",timeout=8).strip(),
            "hyperhold_enable":_device_shell(device,"cat /proc/sys/kernel/hyperhold/enable",timeout=8).strip(),
            "memshiftd_pids":_device_shell(device,"pidof memshiftd",timeout=8,check=False).split()}
    except Exception as exc:receipt["errors"].append("final readback: "+f"{type(exc).__name__}: {exc}")
    receipt["abort_completed"]=(gate_owners_stopped and not receipt["errors"] and
        receipt.get("final_readback",{}).get("boot_id")==read_json(result_dir/"native_registration.json")["boot_id"] and
        receipt.get("final_readback",{}).get("selinux")=="Enforcing" and
        receipt.get("final_readback",{}).get("hyperhold_enable")=="enable" and
        len(receipt.get("final_readback",{}).get("memshiftd_pids",[]))==1)
    receipt["evidence_files"]={str(path.relative_to(result_dir)):sha(path) for path in (result_dir/"abort_evidence").rglob("*") if path.is_file()}
    _write_receipt(receipt_path,receipt)
    return receipt


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir",type=Path,required=True)
    parser.add_argument("--hdc",type=Path,required=True,help="HDC executable (not used in default dry-run)")
    parser.add_argument("--target",required=True,help="exact HDC device serial")
    parser.add_argument("--expected-host-pid",type=int,required=True)
    parser.add_argument("--execute",action="store_true",help="perform narrowly scoped device recovery")
    parser.add_argument("--host-confirmed-stopped",action="store_true",
        help="explicit operator assertion; tool independently verifies the PID and exact run_trial command are absent")
    args=parser.parse_args()
    local=local_preflight(args.result_dir,args.expected_host_pid)
    if not args.execute:
        print(json.dumps({"mode":"dry_run_no_HDC","local_preflight":local,
            "host_confirmed_stopped_flag":args.host_confirmed_stopped,
            "device_mutations":"none"},ensure_ascii=False,indent=2))
        return 0
    receipt=execute_abort(args.result_dir.resolve(strict=True),args.hdc.resolve(strict=True),args.target,
        args.expected_host_pid,args.host_confirmed_stopped)
    print(json.dumps({"abort_receipt":str((args.result_dir.resolve()/"abort_receipt.json")),
        "abort_completed":receipt["abort_completed"],"valid_baseline":False,
        "errors":receipt["errors"]},ensure_ascii=False,indent=2))
    return 0 if receipt["abort_completed"] else 2


if __name__=="__main__":
    try:raise SystemExit(main())
    except (ValueError,OSError,RuntimeError,KeyError,json.JSONDecodeError,subprocess.SubprocessError) as exc:
        print(f"abort refused/incomplete: {type(exc).__name__}: {exc}",file=sys.stderr)
        raise SystemExit(2)
