"""Diagnose crash-truncated JSONL without rewriting or qualifying raw evidence."""
import hashlib
import json
from pathlib import Path
from .core import save,memory,GIB


def audit_partial(folder: Path):
    result={"valid_for_training":False,"files":{}}
    recovered={}
    for name in ("system.jsonl","processes.jsonl","kernel.jsonl","gate.jsonl"):
        source=folder/name
        if not source.is_file():
            result["files"][name]={"missing":True};continue
        raw=source.read_bytes();valid=[];position=0;error=None
        for line in raw.splitlines(keepends=True):
            try:
                if not line.endswith(b"\n"):raise ValueError("unterminated final record")
                valid.append(json.loads(line))
            except (ValueError,UnicodeError) as exc:
                error={"first_bad_byte":position,"reason":str(exc)};break
            position+=len(line)
        recovered[name]=valid
        result["files"][name]={"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),
                                "complete_prefix_records":len(valid),"valid_prefix_bytes":position,"corruption":error}
        target=folder/"diagnostic_prefix"/name;target.parent.mkdir(exist_ok=True)
        target.write_bytes(raw[:position])
    baseline=[memory(r["meminfo"]) for r in recovered.get("system.jsonl",[]) if r.get("stage")=="baseline"]
    if baseline:
        result["partial_baseline"]={"samples":len(baseline),"min_available_gib":min(m["MemAvailable"] for m in baseline)/GIB,
                                    "max_available_gib":max(m["MemAvailable"] for m in baseline)/GIB,
                                    "last_shmem_gib":baseline[-1].get("Shmem",0)/GIB,
                                    "last_swap_free_gib":baseline[-1].get("SwapFree",0)/GIB}
    result["note"]="Diagnostic prefixes are incomplete and must not be exported as qualified training runs."
    save(folder/"partial_audit.json",result)
    return result
