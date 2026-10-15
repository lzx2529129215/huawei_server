"""Kernel evidence only. Missing coverage is never a negative OOM label."""
from __future__ import annotations

import re
import json
from memscenes.model import EventBook


def read_kernel_capture(path):
    """Read kmsg's arbitrary bytes without dropping records or changing capture.

    Legacy sampler strings can contain non-UTF-8 bytes from kernel diagnostics.
    Latin-1 maps each such byte reversibly; ASCII headers/events remain exact.
    Malformed JSON still raises, so a truncated capture cannot become negative.
    """
    records=[]
    with path.open('rb') as stream:
        for line in stream:
            try:
                text=line.decode('utf-8')
                encoding='utf-8'
            except UnicodeDecodeError:
                text=line.decode('latin-1')
                encoding='latin-1-byte-map'
            record=json.loads(text)
            if encoding!='utf-8':record['_raw_encoding']=encoding
            records.append(record)
    return records


def lifetimes(processes):
    result={}
    frames=[f for f in processes if f.get("event")=="process_frame"]
    for row in processes:
        if "pid" not in row:continue
        existing=result.setdefault(row["pid"],[])
        item=next((x for x in existing if x["start_ticks"]==row["start_ticks"]),None)
        if item is None:
            item={k:row[k] for k in ("bundle","pid","uid","start_ticks")}
            item.update(first_uptime=row["time"],last_uptime=row["time"],first_absent_uptime=None)
            existing.append(item)
        item["last_uptime"]=max(item["last_uptime"],row["time"])
    for items in result.values():
        for item in items:
            key=f"{item['pid']}:{item['start_ticks']}"
            item["first_absent_uptime"]=min((f["time"] for f in frames if f["time"]>item["last_uptime"] and key not in f["members"]),default=None)
    return result


def classify_system(records, processes, gate):
    if not gate or "wall_ms" not in gate[0] or "wall_ms" not in gate[-1]:return {"events":[],"complete":False,"reason":"missing_wall_clock_anchor"}
    a,b=gate[0],gate[-1]
    if abs((b["wall_ms"]-a["wall_ms"])/1000-(b["time"]-a["time"]))>.1:
        return {"events":[],"complete":False,"reason":"wall_clock_changed"}
    observed=lifetimes(processes);targets={p["bundle"]:p["uid"] for p in processes if "pid" in p}
    book=EventBook()
    book.system_kills(records,"realapp",targets,a["wall_ms"],b["wall_ms"],"hisysevent.raw",observed,
                      a["time"],b["time"],a["wall_ms"],a["time"])
    result=[]
    for e in book.events:
        e={k:v for k,v in e.items() if k!="_key"};e["time"]=a["time"]+(e["timestamp_ms"]-a["wall_ms"])/1000;result.append(e)
    for record in records:
        if record.get("domain_")=="KERNEL_VENDOR" and record.get("name_")=="LOWMEM" and record.get("REASON")=="OOM_KILLER":
            try:
                stamp=int(record.get("TIMESTAMP",record.get("time_",0)))
                pid=int(record["PID"]);uid=int(record["UID"])
            except (ValueError,TypeError,KeyError):continue
            if not a["wall_ms"]<=stamp<=b["wall_ms"]:continue
            t=a["time"]+(stamp-a["wall_ms"])/1000
            matches=[p for p in observed.get(pid,[]) if p["uid"]==uid
                     and p["first_uptime"]<=t<=(p["first_absent_uptime"] or b["time"])
                     and record.get("PROCESS_NAME")==p["bundle"]]
            identity=matches[0] if len(matches)==1 else None
            exited=bool(identity and identity["first_absent_uptime"] is not None
                        and t<=identity["first_absent_uptime"]<=b["time"])
            # Vendor OOM selection and observed disappearance are separate
            # evidence. A delayed exit is retained, not moved to selection time.
            result.append({"category":"oom_kill" if exited else "vendor_oom_candidate",
                           "time":t,"pid":pid,"identity":identity,
                           "source":"hisysevent:KERNEL_VENDOR/LOWMEM",
                           "timestamp_kind":"vendor_event_report",
                           "observed_exit_time":identity["first_absent_uptime"] if identity else None,
                           "evidence":record})
            continue
        if record.get("name_") not in ("APP_CRASH","CPP_CRASH","JS_ERROR"):continue
        try:stamp=int(record.get("TIMESTAMP",record.get("time_",0)));pid=int(record.get("PID",0));uid=int(record.get("UID",-1))
        except (ValueError,TypeError):continue
        t=a["time"]+(stamp-a["wall_ms"])/1000
        matches=[p for p in observed.get(pid,[]) if p["uid"]==uid and p["first_uptime"]<=t<= (p["first_absent_uptime"] or b["time"])]
        if not matches:continue
        reason=str(record.get("REASON",""))+" "+str(record.get("SUMMARY",""))
        category="allocation_failure" if re.search(r"\bENOMEM\b|std::bad_alloc",reason) else "application_crash"
        result.append({"category":category,"time":t,"pid":pid,"identity":matches[0],"source":"hisysevent","evidence":record})
    return {"events":result,"complete":len(records)<100000,"query_result_count":len(records)}


def classify_kernel(kernel: list[dict], processes: list[dict], gate: list[dict]) -> dict:
    records=[];markers=[];errors=[];previous=None;out_of_order=0
    expected_run=gate[0].get("run") if gate else None
    for row in kernel:
        if "read_error" in row:
            errors.append("kernel_read_error");continue
        raw=row.get("raw","");head,sep,message=raw.partition(";")
        try:
            fields=head.split(",");seq=int(fields[1]);stamp=int(fields[2])/1e6
            if not sep: raise ValueError()
        except (ValueError,IndexError):
            errors.append("invalid_kmsg_header");continue
        if previous is not None and seq<previous: out_of_order+=1
        previous=seq
        record={"seq":seq,"kernel_time":stamp,"message":message,"raw":raw}
        records.append(record)
        m=re.search(r"REALAPP_CLOCK run=(\S+) kind=(begin|tick|end) time=(\d+\.\d+)",message)
        if m and m[1]==expected_run:
            markers.append({"kind":m[2],"time":float(m[3]),"offset":float(m[3])-stamp,"seq":seq})
    # This kernel can deliver adjacent records out of sequence. Check actual
    # sequence coverage after ordering, while leaving the raw capture intact.
    records.sort(key=lambda r:r["seq"])
    for a,b in zip(records,records[1:]):
        if b["seq"]==a["seq"]: errors.append("kernel_duplicate_sequence")
        elif b["seq"]!=a["seq"]+1: errors.append("kernel_sequence_gap")
    begins=[m for m in markers if m["kind"]=="begin"]
    ends=[m for m in markers if m["kind"]=="end"]
    aligned=len(begins)==len(ends)==1 and begins[0]["seq"]<ends[0]["seq"]
    if aligned:
        drift=max(m["offset"] for m in markers)-min(m["offset"] for m in markers)
        aligned=drift<=0.1
        if not aligned: errors.append("clock_offset_drift")
    else:
        drift=None;errors.append("clock_markers_missing")
    offset=sum(m["offset"] for m in markers)/len(markers) if aligned else None
    events=[];dedup=set()
    frames=[r for r in processes if r.get("event")=="process_frame"]
    proc=[r for r in processes if "pid" in r]
    for index,r in enumerate(records):
        msg=r["message"]
        vendor=re.search(r"\[mem_oom:\d+\].*?out of Memory\s*\(OOM\),\s*start to kill process\s*<\s*([^>]+)>\s*\(pid:\s*(\d+),",msg,re.I)
        linux=re.search(r"(?:Out of memory|Memory cgroup out of memory): Killed process (\d+)\b",msg)
        if not vendor and not linux: continue
        pid=int(vendor[2] if vendor else linux[1])
        time=r["kernel_time"]+offset if aligned else None
        exit_record=None
        if vendor:
            exit_record=next((later for later in records[index+1:]
                              if 0<=later["kernel_time"]-r["kernel_time"]<=2
                              and re.search(r"process has exited, pid="+str(pid)+r"\b",later["message"])),None)
        confirmed=aligned and (bool(linux) or exit_record is not None)
        identity=None
        if aligned:
            candidates=[p for p in proc if p["pid"]==pid and 0<=time-p["time"]<=2]
            if candidates:
                p=candidates[-1];key=f"{pid}:{p['start_ticks']}"
                absent=any(f["time"]>p["time"] and f["time"]<time and key not in f["members"] for f in frames)
                replaced=any(other["pid"]==pid and other["start_ticks"]!=p["start_ticks"] and p["time"]<other["time"]<=time for other in proc)
                if not absent and not replaced: identity={k:p[k] for k in ("bundle","uid","start_ticks")}
        incident=(pid,identity and identity["start_ticks"],r["seq"])
        if incident in dedup: continue
        dedup.add(incident)
        events.append({"category":"oom_kill" if confirmed else "kernel_oom_candidate",
                       "time":time,"pid":pid,"identity":identity,"kernel_sequence":r["seq"],
                       "attribution":"observed_process" if identity else "system_only",
                       "evidence":[r["raw"]]+([exit_record["raw"]] if exit_record else [])})
    complete=bool(gate and gate[0].get("kernel_open") and gate[-1].get("event")=="stopped"
                  and gate[-1].get("kernel_read_ok") and aligned and not errors)
    unresolved=any(e["category"]=="kernel_oom_candidate" for e in events)
    return {"events":events,"stream_complete":complete,"clock_offset_seconds":offset,"clock_drift_seconds":drift,
            "oom_label_coverage_complete":complete and not unresolved,"coverage_errors":sorted(set(errors)),
            "out_of_order_records":out_of_order,
            "binary_text_records":sum(r.get('_raw_encoding')=='latin-1-byte-map' for r in kernel),
            "other_event_sources":"not_qualified_without_source_evidence"}
