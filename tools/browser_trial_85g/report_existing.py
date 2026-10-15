from __future__ import annotations

"""Offline audit for a completed browser-trial result directory.

This tool has no device/HDC integration. It requires an execution result to be
completed by default; --allow-incomplete permits diagnostic review only.
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FROZEN_NATIVE = ROOT / "results" / "daily_lead90_20261012_1640" / "candidate_runner" / "native_source"
MIB = 1024 * 1024
STAMP = re.compile(r"^\[\s*(\d+\.\d+)\]")
SEQ = re.compile(r"\[kmsg_seq=(\d+)\]")
CASE_MARKER = re.compile(r"MEMSCENES_CASE_(BEGIN|END)\s+session=\S+\s+case_id=([^\s]+)")


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load existing audit helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OOM = _module("browser_trial_existing_oom_signatures", FROZEN_NATIVE / "oom_signatures.py")
COLLECTOR = _module("browser_trial_existing_collector_protocol", FROZEN_NATIVE / "collector_protocol.py")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def streamed_file_info(path: Path) -> dict:
    """Hash/count lines incrementally; in particular never load reads.tsv whole."""
    digest = hashlib.sha256()
    size = lines = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
            lines += block.count(b"\n")
    return {"bytes": size, "newline_count": lines, "sha256": digest.hexdigest(), "streamed": True}


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _optional_json(path: Path) -> dict | None:
    if not path.is_file(): return None
    try: return _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError): return None


def _sidecar_provenance(result_dir: Path, result: dict) -> dict:
    """Summarize emergency sidecars without promoting them into result proof."""
    reg_path=result_dir/"native_registration.json"
    final_path=result_dir/"native_emergency_finalization.json"
    reg=_optional_json(reg_path) or {}
    final=_optional_json(final_path) or {}
    plan=result.get("plan") or {}
    preflight=result.get("device_preflight") or {}
    arm=result.get("kernel_collector_armed_before_service_stop") or {}
    start=reg.get("kernel_capture_start_boottime")
    ready=reg.get("kernel_collector_ready_boottime")
    try:
        start=float(start); ready=float(ready)
        arm_start=float(arm.get("capture_start_boottime")); arm_ready=float(arm.get("collector_ready_boottime"))
        clock_match=(reg.get("clock_domain")=="CLOCK_BOOTTIME" and
            abs(start-arm_start)<=0.01 and abs(ready-arm_ready)<=0.01 and
            start<=ready<=float(plan.get("T",0)))
    except (TypeError,ValueError):
        clock_match=False; start=ready=None
    rc_path=result_dir/"emergency_native.rc"
    native_rc=None
    if rc_path.is_file():
        try: native_rc=int(rc_path.read_text(encoding="ascii").strip())
        except (OSError,ValueError): pass
    errors=final.get("collection_errors") or []
    gate_path=result_dir/"emergency_gate.events.tsv"
    gate_events=[]; poll_count=0
    if gate_path.is_file():
        with gate_path.open("r",encoding="utf-8",errors="replace") as stream:
            for line in stream:
                if line.startswith("POLL|"): poll_count+=1
                elif line.startswith(("GATE_INVALID|","GATE_IO_COVERAGE|","GATE_RESULT|","WATCHDOG_EXIT|","FINAL|")):
                    gate_events.append(line.rstrip()[:1500])
    return {"supplemental_only_not_promoted_to_native_execution":True,
        "native_registration":{"present":bool(reg),"path":str(reg_path) if reg else None,
            "sha256":sha256_file(reg_path) if reg else None,"case_id":reg.get("case_id"),
            "case_matches_result":bool(reg and reg.get("case_id")==plan.get("case_id")),
            "T_boottime_s":reg.get("T"),"T_matches_result":bool(reg and isinstance(reg.get("T"),(int,float)) and abs(reg["T"]-float(plan.get("T",0)))<=0.01),
            "boot_id":reg.get("boot_id"),"boot_matches_preflight":bool(reg and reg.get("boot_id")==preflight.get("boot_id")),
            "clock_domain":reg.get("clock_domain"),"capture_start_boottime_s":start,
            "collector_ready_boottime_s":ready,"capture_times_match_arm_receipt":clock_match,
            "controller_wrapper_pid":reg.get("controller_wrapper_pid"),"gate_wrapper_pid":reg.get("gate_wrapper_pid"),
            "gate_window":reg.get("gate_window")},
        "emergency_finalization":{"present":bool(final),"path":str(final_path) if final else None,
            "sha256":sha256_file(final_path) if final else None,"error":final.get("error"),
            "launch_attempted":final.get("launch_attempted"),
            "native_completion_wait":final.get("native_completion_wait"),
            "native_rc_file":str(rc_path) if rc_path.is_file() else None,"native_rc_file_value":native_rc,
            "native_poll_timeout_count":sum("native poll: TimeoutExpired" in str(x) for x in errors),
            "collector_cleanup":final.get("collector_cleanup"),
            "collection_error_count":len(errors),"collection_errors":errors,
            "native_completion_clock":final.get("native_completion_clock"),
            "gate_auto_restore_wait":final.get("gate_auto_restore_wait"),
            "final_gate_readback":final.get("final_gate_readback")},
        "emergency_gate":{"path":str(gate_path) if gate_path.is_file() else None,
            "sha256":sha256_file(gate_path) if gate_path.is_file() else None,
            "poll_rows_observed":poll_count,"event_records":gate_events}}


def _service_logs_audit(result_dir: Path) -> dict:
    """Stream candidate service CSVs and report observed actions/memory ranges."""
    artifacts=result_dir/"service_artifacts"
    paths={key:next(iter(artifacts.glob(pattern)),None) for key,pattern in {
        "daily_actions":"*__logs__harmony-v1-ratio__daily_actions.csv",
        "system":"*__logs__harmony-v1-ratio__system.csv",
        "apps":"*__logs__harmony-v1-ratio__apps.csv"}.items()}
    out={"csv_streamed":True,"files":{}}
    for key,path in paths.items():
        if path is None:
            out["files"][key]={"present":False}; continue
        info={"present":True,"path":str(path),"sha256":sha256_file(path)}
        with path.open("r",encoding="utf-8-sig",newline="") as stream:
            reader=csv.DictReader(stream)
            if key=="daily_actions":
                rows=0; attempted=0; bytes_attempted=0; successes=0
                for row in reader:
                    rows+=1
                    attempted+=row.get("write_attempted","").strip().lower() in ("1","true","yes")
                    try: bytes_attempted+=int(row.get("write_length") or 0)
                    except ValueError: pass
                    successes+=row.get("status","").strip().lower() in ("success","applied","written")
                info.update({"data_rows":rows,"write_attempt_rows":attempted,"successful_status_rows":successes,
                    "sum_write_length_bytes":bytes_attempted,"no_daily_actions_recorded":rows==0})
            elif key=="system":
                count=0; first=None; last=None; min_avail=None; max_avail=None; min_free=None; min_avail_row=None
                for row in reader:
                    count+=1; first=first or row; last=row
                    try: avail=int(row.get("mem_available_bytes") or 0)
                    except ValueError: continue
                    if min_avail is None or avail<min_avail:
                        min_avail=avail; min_avail_row={"unix_ms":row.get("unix_ms"),"mem_available_bytes":avail,
                            "mem_free_bytes":row.get("mem_free_bytes"),"decision":row.get("decision"),"reclaim_action":row.get("reclaim_action")}
                    max_avail=avail if max_avail is None else max(max_avail,avail)
                    try:
                        free=int(row.get("mem_free_bytes") or 0)
                        min_free=free if min_free is None else min(min_free,free)
                    except ValueError: pass
                info.update({"data_rows":count,"first_unix_ms":None if first is None else first.get("unix_ms"),
                    "last_unix_ms":None if last is None else last.get("unix_ms"),
                    "mem_available_min_bytes":min_avail,"mem_available_min_mib":None if min_avail is None else round(min_avail/MIB,2),
                    "mem_available_max_bytes":max_avail,"mem_free_min_bytes":min_free,"row_at_min_available":min_avail_row,
                    "last_mem_available_bytes":None if last is None else last.get("mem_available_bytes"),
                    "last_mem_free_bytes":None if last is None else last.get("mem_free_bytes")})
            else:
                count=0; first=None; last=None; min_anon=None; max_anon=None; min_row=None; max_row=None
                for row in reader:
                    if row.get("app")!="browser": continue
                    count+=1; first=first or row; last=row
                    try: anon=int(row.get("anon_kib") or 0)
                    except ValueError: continue
                    snap={"unix_ms":row.get("unix_ms"),"anon_kib":anon,"zram_kib":row.get("zram_kib"),
                        "eswap_kib":row.get("eswap_kib"),"proc_count":row.get("proc_count"),
                        "memory_current_raw":row.get("memory_current_raw"),"memory_current_unit":row.get("memory_current_unit"),
                        "reclaim_action":row.get("reclaim_action"),"reason":row.get("reason")}
                    if min_anon is None or anon<min_anon: min_anon=anon; min_row=snap
                    if max_anon is None or anon>max_anon: max_anon=anon; max_row=snap
                info.update({"browser_rows":count,"first_browser_sample":None if first is None else {
                        k:first.get(k) for k in ("unix_ms","anon_kib","zram_kib","eswap_kib","proc_count","reclaim_action","reason")},
                    "last_browser_sample":None if last is None else {
                        k:last.get(k) for k in ("unix_ms","anon_kib","zram_kib","eswap_kib","proc_count","reclaim_action","reason")},
                    "minimum_anon_sample":min_row,"maximum_anon_sample":max_row})
        out["files"][key]=info
    return out


def _proof_completion_errors(result: dict) -> list[str]:
    native = result.get("native_execution") or {}
    errors = []
    if result.get("device_execution") != "completed": errors.append("outer device_execution is not completed")
    if native.get("device_execution") != "completed": errors.append("native device_execution is not completed")
    if native.get("native_returncode") != 0: errors.append("native controller return code is not zero")
    if native.get("coverage_through_t_plus_930") is not True: errors.append("native T+930 coverage flag is false/missing")
    if native.get("gate_audit", {}).get("valid") is not True: errors.append("gate audit is invalid/incomplete")
    if result.get("service_restore", {}).get("verified") is not True: errors.append("original service restoration is not verified")
    cleanup = result.get("target_cleanup", {})
    if not isinstance(cleanup, dict) or not cleanup or not all(
            isinstance(row, dict) and row.get("verified_absent") is True for row in cleanup.values()):
        errors.append("owned target cleanup is not fully verified")
    return errors


def _collector_audit(path: Path, T: float, stderr: str, native: dict) -> dict:
    heartbeats = []
    starts = []
    terminals = []
    epipe = []
    async_invalid = []
    confirmed = []
    for line in stderr.splitlines():
        if line.startswith("HEARTBEAT "):
            try: heartbeats.append(COLLECTOR.fields(line))
            except ValueError: pass
        elif line.startswith("START "):
            try: starts.append(COLLECTOR.fields(line))
            except ValueError: pass
        elif line.startswith("TERMINAL "):
            terminals.append(line)
        elif line.startswith("EPIPE "):
            epipe.append(line)
        elif line.startswith("ASYNC_INVALID "):
            async_invalid.append(line)
        elif line.startswith("CONFIRMED_MARKER "):
            try: confirmed.append(COLLECTOR.fields(line))
            except ValueError: pass
    uptime_values=[]
    for row in heartbeats:
        try: uptime_values.append(float(row["uptime"]))
        except (KeyError, ValueError, TypeError): pass
    intervals=[b-a for a,b in zip(uptime_values,uptime_values[1:]) if b>=a]
    last=heartbeats[-1] if heartbeats else {}
    last_uptime=uptime_values[-1] if uptime_values else None
    end=T+930.0
    horizon=last_uptime is not None and last_uptime >= end
    # Reader nominal cadence is 5 seconds. A gap above 15 seconds is flagged as
    # an interruption needing review, not silently counted as continuous capture.
    max_interval=max(intervals,default=None)
    long_gap=max_interval is not None and max_interval > 15.0
    protocol=COLLECTOR.inspect_collector(stderr, interval_verified=horizon)
    last_flags={key:last.get(key) for key in ("invalid","gaps","reorders","unconfirmed_records","sequence","high","records","uptime")}
    last_clean=bool(last) and all(last.get(key)=="0" for key in ("invalid","gaps","unconfirmed_records"))
    continuous=bool(horizon and protocol.get("verified") and last_clean and not epipe and
                    not async_invalid and not long_gap)
    return {"stderr_path":str(path),"stderr_sha256":sha256_file(path) if path.is_file() else None,
        "start_record_count":len(starts),"heartbeat_count":len(heartbeats),
        "last_heartbeat":last_flags,"last_heartbeat_uptime":last_uptime,
        "required_last_uptime":end,"horizon_reached":horizon,
        "max_heartbeat_interval_s":max_interval,"heartbeat_interval_over_15s":long_gap,
        "epipe_count":len(epipe),"epipe_records":epipe[:8],
        "async_invalid_count":len(async_invalid),"async_invalid_records":async_invalid[:8],
        "terminal_records":terminals,"confirmed_marker_count":len(confirmed),
        "protocol_review":protocol,"last_heartbeat_clean":last_clean,
        "continuous_capture_proven":continuous,
        "reader_returncode":(native.get("kernel_collector") or {}).get("reader_returncode"),
        "reorders_are_diagnostic_not_gap_count":True}


def _load_raw_kernel(path: Path) -> dict:
    # Stream the possibly large capture. oom_signatures.parse_kernel_oom_records
    # accepts an iterable despite its list[str] annotation, so parsing can make
    # a separate streaming pass without retaining raw lines in memory.
    seq_first=None; seq_last=None; seq_reorders=0; seq_jumps=0; seq_jump_ranges=[]
    counts=Counter(); last_stamp=None; time_reorders=0; line_count=0
    case_markers=[]
    with path.open("r",encoding="utf-8",errors="replace") as stream:
        for n,line in enumerate(stream,1):
            line_count=n
            stamp=STAMP.match(line)
            seq=SEQ.search(line)
            if stamp:
                current=float(stamp.group(1))
                if last_stamp is not None and current<last_stamp: time_reorders+=1
                last_stamp=current
            else: current=None
            if seq:
                value=int(seq.group(1)); counts[value]+=1
                if seq_first is None: seq_first=value
                if seq_last is not None and value<seq_last: seq_reorders+=1
                elif seq_last is not None and value>seq_last+1:
                    seq_jumps += 1
                    if len(seq_jump_ranges)<64:
                        seq_jump_ranges.append([seq_last,value])
                seq_last=value
            match=CASE_MARKER.search(line)
            if match:
                case_markers.append({"line":n,"kind":match.group(1),"case_id":match.group(2),
                    "boottime":current,"sequence":int(seq.group(1)) if seq else None,"raw":line.rstrip()[:1000]})
    duplicates=sum(c-1 for c in counts.values() if c>1)
    return {"path":str(path),"sha256":sha256_file(path),"line_count":line_count,
        "first_sequence":seq_first,"last_sequence":seq_last,"duplicate_sequence_rows":duplicates,
        "observed_filtered_sequence_jumps":seq_jumps,
        "observed_filtered_sequence_jump_ranges_first_64":seq_jump_ranges,
        "sequence_jumps_do_not_prove_missing_logs":True,
        "sequence_order_regressions_in_filtered_raw":seq_reorders,
        "timestamp_order_regressions_in_filtered_raw":time_reorders,
        "case_markers":case_markers}


def _identity_index(result: dict) -> dict[int,list[dict]]:
    index: dict[int,list[dict]]={}
    for row in (result.get("plan") or {}).get("member_identities",[]):
        try:
            pid,uid,ticks=map(int,row[:3])
            index.setdefault(pid,[]).append({"role":"browser_donor","uid":uid,"start_ticks":ticks})
        except (TypeError,ValueError): pass
    target_ready=result.get("target_daily_ready") or {}
    for bundle,item in (target_ready.get("targets") or {}).items():
        try:
            pid=int(item["pid"]); uid=int(item["uid"]); ticks=int(item["start_ticks"])
            index.setdefault(pid,[]).append({"role":bundle,"uid":uid,"start_ticks":ticks})
        except (KeyError,TypeError,ValueError): pass
    return index


def _oom_audit(path: Path, result: dict, capture_start: float | None, capture_start_verified: bool, T: float) -> dict:
    with path.open("r",encoding="utf-8",errors="replace") as stream:
        parsed=OOM.parse_kernel_oom_records(stream)
    stop=T+930.0
    pre_T=[]; post_T=[]; pre_T_unverified=[]; before_capture=0; out_of_window=0; untimeable=0
    for row in parsed["victims"]:
        at=row.get("t")
        if at is None:
            untimeable+=1; continue
        if capture_start_verified and capture_start is not None and capture_start <= at < T: pre_T.append(row)
        elif not capture_start_verified and at < T: pre_T_unverified.append(row)
        elif T <= at <= stop: post_T.append(row)
        elif capture_start_verified and capture_start is not None and at < capture_start: before_capture+=1
        else: out_of_window+=1
    contexts=[]
    for row in parsed["contexts"]:
        at=row.get("t")
        if at is not None and capture_start_verified and capture_start is not None and capture_start <= at <= stop: contexts.append(row)
        elif at is not None and T <= at <= stop: contexts.append(row)
    unresolved=[r for r in contexts if r.get("paired_victim_line") is None]
    # A kernel process generation is not present in every OOM signature. Dedup
    # explicit victims by PID+comm, retain all contributing sequences/lines.
    victim_map={}
    identities=_identity_index(result)
    for row in pre_T + pre_T_unverified + post_T:
        key=(int(row["pid"]),str(row["comm"]))
        item=victim_map.setdefault(key,{"pid":key[0],"comm":key[1],"signatures":set(),
            "records":[],"pre_run_identity_matches":identities.get(key[0],[])})
        item["signatures"].add(row.get("signature"))
        item["records"].append({"line":row.get("line"),"boottime":row.get("t"),"kmsg_seq":row.get("seq"),"signature":row.get("signature")})
    victims=[]
    for item in victim_map.values():
        item["signatures"]=sorted(x for x in item["signatures"] if x)
        item["record_count_before_pid_comm_dedup"]=len(item["records"])
        item["records"].sort(key=lambda x:(x["boottime"] or 0,x["line"] or 0))
        victims.append(item)
    victims.sort(key=lambda x:(x["records"][0]["boottime"] if x["records"] else 0,x["pid"]))
    signature_counts=Counter(row.get("signature","unknown") for row in pre_T + pre_T_unverified + post_T)
    victims_before_T=sum(1 for item in victims if any(r["boottime"] < T for r in item["records"]))
    victims_at_or_after_T=sum(1 for item in victims if any(r["boottime"] >= T for r in item["records"]))
    return {"parser":"frozen native_source/oom_signatures.py:parse_kernel_oom_records",
        "primary_oom_window_boottime":[capture_start,stop] if capture_start_verified and capture_start is not None else None,
        "fixed_T_boottime":T,"collector_started_at_boottime":capture_start,
        "capture_start_verified":capture_start_verified,
        "explicit_victim_rows_in_primary_window":len(pre_T)+len(post_T) if capture_start_verified else None,
        "explicit_victim_rows_before_T_within_capture_window":len(pre_T),
        "explicit_victim_rows_before_T_capture_start_unverified":len(pre_T_unverified),
        "explicit_victim_rows_at_or_after_T_through_T_plus_930":len(post_T),
        "unique_victim_processes_before_T":victims_before_T,
        "unique_victim_processes_at_or_after_T":victims_at_or_after_T,
        "explicit_victim_rows_before_capture_start":before_capture,
        "explicit_victim_rows_outside_window":out_of_window,
        "explicit_victim_rows_without_timestamp":untimeable,
        "unique_victim_processes_pid_comm":len(victims),
        "duplicate_rows_suppressed_by_pid_comm":len(pre_T)+len(pre_T_unverified)+len(post_T)-len(victims),
        "victim_signature_counts":dict(signature_counts),"victims":victims,
        "oom_context_rows_in_window":len(contexts),
        "unresolved_oom_context_count":len(unresolved),
        "unresolved_oom_contexts":[{k:r.get(k) for k in ("line","t","seq","kind","pid","raw")} for r in unresolved],
        "all_parser_contexts_in_window":[{k:r.get(k) for k in ("line","t","seq","kind","pid","paired_victim_line","raw")} for r in contexts],
        "renderer_exit_is_oom":False,
        "count_interpretation":"Explicit kernel victim rows are parsed separately from OOM context rows; victims are deduplicated by (pid,comm). PID generation at kill-time is not asserted unless an exact pre-run PID/UID/start identity is available.",
        "zero_oom_conclusion_allowed":False}


def _gate_and_boot(result: dict) -> dict:
    native=result.get("native_execution") or {}
    gate=native.get("gate_audit") or {}
    native_boot=(native.get("registration") or {}).get("boot_id")
    preflight_boot=(result.get("device_preflight") or {}).get("boot_id")
    restore_text=native.get("gate_restore_readback") or ""
    restore_boot=restore_text.splitlines()[0].strip() if restore_text else None
    boots=[x for x in (preflight_boot,native_boot,restore_boot) if x]
    boot_consistent=len(set(boots))<=1 and bool(boots)
    return {"gate_audit_valid":gate.get("valid") is True,
        "gate_coverage_through_t_plus_930":native.get("coverage_through_t_plus_930") is True,
        "gate_interpretation":gate.get("interpretation"),
        "gate_result":gate.get("result"),"gate_io_coverage":gate.get("io_coverage"),
        "boot_ids":{"device_preflight":preflight_boot,"native_registration":native_boot,"gate_restore_readback":restore_boot},
        "boot_consistent":boot_consistent,"boot_change_detected":len(set(boots))>1,
        "service_restore_verified":result.get("service_restore",{}).get("verified") is True,
        "target_cleanup_verified":all(isinstance(x,dict) and x.get("verified_absent") is True
             for x in (result.get("target_cleanup") or {}).values()) and bool(result.get("target_cleanup"))}


def _observed_oom_count_status(oom: dict, capture_valid: bool) -> dict:
    """Confirm raw-capture OOM counts independently of gate-protocol validity."""
    count=oom.get("unique_victim_processes_pid_comm")
    confirmed=bool(capture_valid and oom.get("capture_start_verified") is True and
        oom.get("explicit_victim_rows_in_primary_window") is not None and
        oom.get("unresolved_oom_context_count")==0 and
        oom.get("explicit_victim_rows_without_timestamp")==0)
    return {"observed_kernel_oom_count":count,
        "observed_kernel_oom_count_confirmed":confirmed,
        "observed_complete_zero":bool(confirmed and count==0)}


def _target_journal_audit(result_dir: Path, expected_peak_mib: int, expected_case_id: str) -> dict:
    output={}; source_hashes={}
    for suffix in ("a14","a15","a16"):
        path=result_dir/f"{suffix}_peak_final.jsonl"
        if not path.is_file():
            output[suffix]={"present":False,"peak_reached":False,"reason":"journal missing"}; continue
        rows=[]; malformed=0; foreign_case_rows=0; counts=Counter(); maxrow=None; reached=[]; terminal=None
        with path.open("r",encoding="utf-8-sig") as stream:
            for line_no,line in enumerate(stream,1):
                if not line.strip(): continue
                try: row=json.loads(line)
                except json.JSONDecodeError: malformed+=1; continue
                if row.get("case_id") != expected_case_id:
                    foreign_case_rows+=1
                    continue
                counts[row.get("event","unknown")]+=1
                if row.get("event")=="allocation_progress" and (maxrow is None or int(row.get("touched_bytes",0))>int(maxrow.get("touched_bytes",0))):
                    maxrow={**row,"_line":line_no}
                if row.get("event")=="peak_reached": reached.append({**row,"_line":line_no})
                terminal={**row,"_line":line_no}
                rows.append(row)
        source_hashes[path.name]=sha256_file(path)
        peak_record=reached[-1] if reached else None
        peak_bytes=int((peak_record or {}).get("peak_bytes",0))
        output[suffix]={"present":True,"path":str(path),"sha256":source_hashes[path.name],
            "case_id_filter":expected_case_id,"rows":len(rows),"foreign_case_rows_ignored":foreign_case_rows,
            "malformed_rows":malformed,"event_counts":dict(counts),
            "peak_reached":bool(peak_record and peak_bytes>=expected_peak_mib*MIB),
            "peak_reached_records":[{"line":r["_line"],"boottime_s":r.get("boottime_ns",0)/1e9,
                "phase":r.get("phase"),"anon_bytes":r.get("anon_bytes"),"file_bytes":r.get("file_bytes"),
                "touched_bytes":r.get("touched_bytes"),"peak_bytes":r.get("peak_bytes")} for r in reached],
            "max_allocation_progress":None if maxrow is None else {"line":maxrow["_line"],
                "boottime_s":maxrow.get("boottime_ns",0)/1e9,"phase":maxrow.get("phase"),
                "anon_bytes":maxrow.get("anon_bytes"),"file_bytes":maxrow.get("file_bytes"),
                "touched_bytes":maxrow.get("touched_bytes"),"touched_mib":int(maxrow.get("touched_bytes",0))/MIB,
                "peak_bytes":maxrow.get("peak_bytes")},
            "terminal_record":None if terminal is None else {"line":terminal["_line"],
                "event":terminal.get("event"),"phase":terminal.get("phase"),
                "boottime_s":terminal.get("boottime_ns",0)/1e9,
                "touched_bytes":terminal.get("touched_bytes"),"peak_bytes":terminal.get("peak_bytes")}}
    return {"targets":output,"source_hashes":source_hashes,
        "targets_at_expected_peak":[k for k,v in output.items() if v.get("peak_reached")],
        "all_targets_reached_peak":all(output.get(k,{}).get("peak_reached") is True for k in ("a14","a15","a16"))}


def audit(result_dir: Path, *, allow_incomplete: bool=False) -> tuple[dict,str]:
    result_dir=result_dir.resolve(strict=True)
    result_path=result_dir/"result.json"
    if not result_path.is_file():
        raise ValueError(f"result.json is missing: {result_path}")
    result=_read_json(result_path)
    if result.get("schema")!="browser_donor_trial_85g_preflight_v1":
        raise ValueError("result.json is not a browser_trial_85g result")
    completion_errors=_proof_completion_errors(result)
    if completion_errors and not allow_incomplete:
        raise ValueError("refusing incomplete result.json by default: " + "; ".join(completion_errors) + "; use --allow-incomplete only for diagnostics")
    native=result.get("native_execution") or {}
    plan=result.get("plan") or {}
    T=float(plan.get("T",native.get("registration",{}).get("T",0)))
    if T<=0: raise ValueError("result has no valid fixed-device T")
    arm=result.get("kernel_collector_armed_before_service_stop") or {}
    sidecars=_sidecar_provenance(result_dir,result)
    registration=native.get("registration") or sidecars.get("native_registration") or {}
    proof_start=arm.get("capture_start_boottime")
    registration_start=registration.get("kernel_capture_start_boottime",registration.get("capture_start_boottime_s"))
    collector_ready=arm.get("collector_ready_boottime",registration.get("kernel_collector_ready_boottime"))
    try:
        capture_start=float(proof_start) if proof_start is not None else None
        registration_start=float(registration_start) if registration_start is not None else None
        collector_ready=float(collector_ready) if collector_ready is not None else None
    except (TypeError,ValueError):
        capture_start=registration_start=collector_ready=None
    gate_boot=_gate_and_boot(result)
    identity_ok=bool(registration.get("case_matches_result",registration.get("case_id")==plan.get("case_id")) and
        registration.get("T_matches_result",isinstance(registration.get("T"),(int,float)) and abs(registration.get("T",0)-T)<=0.01) and
        registration.get("boot_matches_preflight",registration.get("boot_id")==((result.get("device_preflight") or {}).get("boot_id"))))
    capture_start_verified=bool(capture_start is not None and registration_start is not None and identity_ok and
        collector_ready is not None and abs(capture_start-registration_start)<=0.01 and
        capture_start<=collector_ready<=T and registration.get("clock_domain")=="CLOCK_BOOTTIME" and
        gate_boot["boot_consistent"])
    raw_path=result_dir/"kernel_dmesg.raw.log"
    stderr_path=result_dir/"kernel_reader.stderr.log"
    if not raw_path.is_file() or not stderr_path.is_file():
        raise ValueError("kernel_dmesg.raw.log or kernel_reader.stderr.log is missing")
    raw_meta=_load_raw_kernel(raw_path)
    oom=_oom_audit(raw_path,result,capture_start,capture_start_verified,T)
    stderr=stderr_path.read_text(encoding="utf-8",errors="replace")
    collector=_collector_audit(stderr_path,T,stderr,native)
    capture_started_before_T=capture_start_verified and capture_start is not None and capture_start <= T
    capture_valid=collector["continuous_capture_proven"] and gate_boot["boot_consistent"] and capture_started_before_T
    gate_boot["capture_started_before_T"]=capture_started_before_T
    oom["zero_oom_conclusion_allowed"]=bool(capture_valid and oom["capture_start_verified"] and
        oom["unresolved_oom_context_count"]==0 and oom["explicit_victim_rows_without_timestamp"]==0 and
        oom["explicit_victim_rows_in_primary_window"]==0)
    oom["kernel_capture_valid_for_zero_conclusion"]=capture_valid
    gate_boot["raw_kernel_capture_valid"]=capture_valid
    observed_oom=_observed_oom_count_status(oom,capture_valid)
    oom.update(observed_oom)
    gate_boot["native_returncode_zero"]=native.get("native_returncode")==0
    gate_boot["native_execution_completed"]=native.get("device_execution")=="completed"
    reads_path=next((p for p in (result_dir/"reads.tsv",result_dir/"emergency_reads.tsv") if p.is_file()),None)
    reads_info=streamed_file_info(reads_path) if reads_path else None
    service_logs=_service_logs_audit(result_dir)
    target_profile=result.get("profile",{}).get("load",{})
    expected_peak=int(target_profile.get("target_peak_mib",0))
    target_journals=_target_journal_audit(result_dir,expected_peak,plan.get("case_id","")) if expected_peak else {"targets":{},"error":"peak size missing"}
    files={"result.json":sha256_file(result_path),"kernel_dmesg.raw.log":raw_meta["sha256"],
        "kernel_reader.stderr.log":collector["stderr_sha256"]}
    if reads_path and reads_info: files[reads_path.name]=reads_info["sha256"]
    files.update(target_journals.get("source_hashes",{}))
    for entry in service_logs.get("files",{}).values():
        if entry.get("present"):
            files[Path(entry["path"]).name]=entry["sha256"]
    for key in ("native_registration","emergency_finalization","emergency_gate"):
        entry=sidecars.get(key,{})
        if entry.get("sha256"):
            files[Path(entry["path"]).name]=entry["sha256"]
    summary={"schema":"browser_trial_85g_existing_review_v1","case_id":plan.get("case_id"),
        "phase":result.get("phase"),"trial_device_execution":result.get("device_execution"),
        "completion_gate_errors":completion_errors,"allow_incomplete_override":allow_incomplete,
        "T_boottime_s":T,"capture_start_boottime_s":capture_start,
        "capture_end_required_boottime_s":T+930,"gate_and_boot":gate_boot,
        "kernel_raw":raw_meta,"collector":collector,"oom":oom,
        "target_peak_journals":target_journals,
        "supplemental_sidecar_provenance":sidecars,
        "service_log_observations":service_logs,
        "reads_tsv":({"path":str(reads_path),**reads_info} if reads_path and reads_info else {"present":False,"streamed":True}),
        "evidence_sha256":files}
    coverage_all=(capture_valid and gate_boot["gate_audit_valid"] and gate_boot["gate_coverage_through_t_plus_930"] and
        gate_boot["service_restore_verified"] and gate_boot["target_cleanup_verified"] and
        gate_boot["native_returncode_zero"] and gate_boot["native_execution_completed"])
    summary["valid_for_oom_comparison"]=bool(coverage_all and not completion_errors)
    summary.update({key:observed_oom[key] for key in
        ("observed_kernel_oom_count","observed_kernel_oom_count_confirmed","observed_complete_zero")})
    summary["oom"]["zero_oom_conclusion_allowed"]=bool(summary["valid_for_oom_comparison"] and
        summary["oom"]["capture_start_verified"] and
        summary["oom"]["unresolved_oom_context_count"]==0 and
        summary["oom"]["explicit_victim_rows_without_timestamp"]==0 and
        summary["oom"]["explicit_victim_rows_in_primary_window"]==0)
    lines=[f"Browser 8.5 GiB existing-run review: {summary['case_id']}",
        f"Device execution: {summary['trial_device_execution']}; valid for OOM comparison: {summary['valid_for_oom_comparison']}.",
        f"Fixed T={T:.6f}; required kernel horizon ends at T+930={T+930:.6f}.",
        f"Kernel capture continuous/proven: {collector['continuous_capture_proven']}; started before T={capture_started_before_T}; last heartbeat={collector['last_heartbeat_uptime']}; max heartbeat interval={collector['max_heartbeat_interval_s']}; gaps/invalid/reorders/unconfirmed(last)={collector['last_heartbeat'].get('gaps')}/{collector['last_heartbeat'].get('invalid')}/{collector['last_heartbeat'].get('reorders')}/{collector['last_heartbeat'].get('unconfirmed_records')}.",
        f"Gate valid={gate_boot['gate_audit_valid']}; gate T+930={gate_boot['gate_coverage_through_t_plus_930']}; boot consistent={gate_boot['boot_consistent']}; service restore={gate_boot['service_restore_verified']}; target cleanup={gate_boot['target_cleanup_verified']}.",
        f"Explicit OOM victim rows in capture window={oom['explicit_victim_rows_in_primary_window']} (before T={oom['explicit_victim_rows_before_T_within_capture_window']}, T through T+930={oom['explicit_victim_rows_at_or_after_T_through_T_plus_930']}); de-duplicated PID+comm victims={oom['unique_victim_processes_pid_comm']}; unresolved OOM contexts={oom['unresolved_oom_context_count']}; full-capture OOM count confirmed={oom['observed_kernel_oom_count_confirmed']}; complete observed zero={oom['observed_complete_zero']}; protocol-valid zero-OOM conclusion allowed={oom['zero_oom_conclusion_allowed']}.",
        f"Target peaks reached: {', '.join(target_journals.get('targets_at_expected_peak',[])) or 'none proven'} of 3."]
    if completion_errors: lines.append("Incomplete-result reasons: " + "; ".join(completion_errors))
    reg=sidecars.get("native_registration",{})
    final=sidecars.get("emergency_finalization",{})
    gate_sidecar=sidecars.get("emergency_gate",{})
    if reg.get("present"):
        lines.append(f"Supplemental native_registration.json (not promoted into result.json validity): case/T/boot match={reg.get('case_matches_result')}/{reg.get('T_matches_result')}/{reg.get('boot_matches_preflight')}; CLOCK_BOOTTIME capture start/collector-ready={reg.get('capture_start_boottime_s')}/{reg.get('collector_ready_boottime_s')}; controller/gate PID={reg.get('controller_wrapper_pid')}/{reg.get('gate_wrapper_pid')}.")
    if final.get("present"):
        lines.append(f"Supplemental emergency finalization: native rc sidecar={final.get('native_rc_file_value')}; HDC native poll timeouts={final.get('native_poll_timeout_count')}; collector cleanup={final.get('collector_cleanup')}; this supplements but does not replace missing nested native_execution/gate evidence.")
    for event in gate_sidecar.get("event_records",[]):
        if event.startswith(("GATE_INVALID|","GATE_IO_COVERAGE|","GATE_RESULT|","WATCHDOG_EXIT|")):
            lines.append("Supplemental gate event: " + event)
    actions=service_logs.get("files",{}).get("daily_actions",{})
    if actions.get("present"):
        lines.append(f"Candidate service daily_actions.csv recorded {actions.get('data_rows')} rows, {actions.get('write_attempt_rows')} write-attempt rows, {actions.get('sum_write_length_bytes')} bytes. This is the captured service action journal count, not an inference from kernel/disk telemetry.")
    system=service_logs.get("files",{}).get("system",{})
    if system.get("present"):
        free=system.get("mem_free_min_bytes")
        lines.append(f"Streamed service system.csv: {system.get('data_rows')} rows; minimum mem_available/free={system.get('mem_available_min_mib')}/{None if free is None else round(free/MIB,2)} MiB; last available/free bytes={system.get('last_mem_available_bytes')}/{system.get('last_mem_free_bytes')}.")
    apps=service_logs.get("files",{}).get("apps",{})
    if apps.get("present"):
        lo=apps.get("minimum_anon_sample") or {}; hi=apps.get("maximum_anon_sample") or {}; last=apps.get("last_browser_sample") or {}
        lines.append(f"Streamed browser app samples={apps.get('browser_rows')}; anon range={lo.get('anon_kib')}–{hi.get('anon_kib')} KiB; at maximum-anon sample: zram/Eswap/proc_count={hi.get('zram_kib')}/{hi.get('eswap_kib')}/{hi.get('proc_count')}; last sample anon/zram/Eswap/proc_count={last.get('anon_kib')}/{last.get('zram_kib')}/{last.get('eswap_kib')}/{last.get('proc_count')}.")
    for name,item in target_journals.get("targets",{}).items():
        if not item.get("present"): lines.append(f"{name}: peak journal missing."); continue
        peak=item.get("peak_reached_records",[])
        max_progress=item.get("max_allocation_progress") or {}
        lines.append(f"{name}: peak_reached={item.get('peak_reached')}; peak_reached_rows={len(peak)}; max touched={max_progress.get('touched_mib')} MiB; terminal phase={(item.get('terminal_record') or {}).get('phase')}; journal sha256={item.get('sha256')}.")
    lines.append("OOM counts come from frozen oom_signatures.parse_kernel_oom_records; native_collection wide-regex sequence counts are not treated as OOM decisions. Renderer exits are diagnostics only. Filtered-raw kmsg sequence jumps are observations, not evidence of missing logs; capture gaps are assessed from whole-stream collector heartbeat/protocol errors. reads.tsv was streamed for size/line-count/hash, never loaded whole.")
    return summary,"\n".join(lines)+"\n"


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir",type=Path,help="completed A01/B01 directory containing result.json")
    parser.add_argument("--allow-incomplete",action="store_true",help="produce a diagnostic report despite incomplete result flags; it remains invalid")
    parser.add_argument("--output-dir",type=Path,help="defaults to result_dir")
    args=parser.parse_args()
    summary,report=audit(args.result_dir,allow_incomplete=args.allow_incomplete)
    output=(args.output_dir or args.result_dir).resolve()
    output.mkdir(parents=True,exist_ok=True)
    (output/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (output/"report.txt").write_text(report,encoding="utf-8")
    print(json.dumps({"summary":str(output/"summary.json"),"report":str(output/"report.txt"),
        "valid_for_oom_comparison":summary["valid_for_oom_comparison"],
        "oom_victims":summary["oom"]["unique_victim_processes_pid_comm"]},ensure_ascii=False,indent=2))
    return 0 if summary["valid_for_oom_comparison"] else 2


if __name__=="__main__":
    try: raise SystemExit(main())
    except (ValueError,OSError,json.JSONDecodeError) as exc:
        print(str(exc),file=sys.stderr)
        raise SystemExit(2)
