from __future__ import annotations

import json
import csv
import hashlib
import re
import subprocess
import time
from pathlib import Path

from memscenes.device import ui_nodes, focused_bundles, quote as q
from .core import SCHEMA, digest, fill_mib, load, memory, rows, save, schedule, segment_states, training_windows, validate_recipe
from .device_ops import Target, Session
from .events import classify_kernel, read_kernel_capture
from .events import classify_system
from .checkpoints import Checkpoints
from .incidents import pressure_blockers
from memscenes.model import json_lines

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SDK = Path(r"D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\native")


def build_sampler(out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [str(SDK / "llvm/bin/clang++.exe"), "--target=aarch64-linux-ohos", "--sysroot=" + str(SDK / "sysroot"),
               "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", str(HERE / "sampler.cpp"), "-o", str(out)]
    subprocess.run(command, check=True, capture_output=True)
    save(out.with_suffix(".manifest.json"), {"source_sha256": digest(HERE / "sampler.cpp"),
                                            "binary_sha256": digest(out), "argv": command})
    return out


def package(dev, bundle):
    text = dev.checked("bm dump -n " + q(bundle))
    start = text.find("{")
    if start < 0:
        raise ValueError("bundle metadata unavailable")
    return json.loads(text[start:])


def preflight(dev: Target, suite: dict, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    installed_raw = dev.checked("bm dump -a")
    installed = {line.strip() for line in installed_raw.splitlines() if "." in line and " " not in line.strip()}
    result = {"schema": SCHEMA, "device": dev.target, "boot_id": dev.read("/proc/sys/kernel/random/boot_id").strip(),
              "kernel": dev.checked("uname -r").strip(), "mount": dev.mount(),
              "df": dev.checked("df -k /dev/shm"), "meminfo": memory(dev.read("/proc/meminfo")),
              "gate": dev.read("/proc/sys/kernel/hyperhold/enable").strip(),
              "gate_detail": dev.read("/sys/block/zram0/hyperhold_enable").strip(),
              "apps": {}, "blockers": [], "installed_bundles": sorted(installed)}
    result["planned_fill_mib"] = fill_mib(result["meminfo"]["MemTotal"])
    result["pressure_incidents"] = pressure_blockers(dev.target, result["planned_fill_mib"])
    try:
        service = dev.service()
        save(out / "service_snapshot.json", service)
        result["service"] = {k: service[k] for k in ("pid", "start_ticks", "exe", "config_path", "control_path")}
    except Exception as exc:
        result["blockers"].append("service:" + str(exc))
    for name, app in suite["apps"].items():
        bundle = app.get("bundle")
        info = {"bundle": bundle, "form": app.get("form"), "installed": bundle in installed,
                "entry_verified": False, "workload_ready": False}
        if bundle in installed:
            try:
                meta = package(dev, bundle)
                abilities = []
                for module in meta.get("hapModuleInfos", []):
                    for ability in module.get("abilityInfos", []):
                        abilities.append({"module": module.get("moduleName"), "ability": ability.get("name"),
                                          "type": ability.get("type"), "exported": ability.get("exported")})
                info.update(uid=meta.get("uid"), version_code=meta.get("versionCode"),
                            version_name=meta.get("versionName"), abilities=abilities)
                info["entry_verified"] = any(a["module"] == app.get("module") and a["ability"] == app.get("ability") for a in abilities)
                # A package is not evidence that its real workload/login is ready.
            except Exception as exc:
                info["error"] = str(exc)
        else:
            result["blockers"].append("application_entry_unresolved:" + name)
        result["apps"][name] = info
    result["synthetic_processes"] = dev.checked("ps -A -o PID,ARGS | grep -E 'io.memscene|browser_donor|/pressure |gate_window' | grep -v grep || true").strip()
    if result["synthetic_processes"]:
        result["blockers"].append("synthetic_workload_residuals")
    save(out / "preflight.json", result)
    report = ["# 真实应用覆盖检查", "", f"设备：{dev.target}；计划占位 {result['planned_fill_mib']} MiB。", "",
              "| 应用 | 原生包/宿主 | 安装 | 操作校准 |", "|---|---|---|---|"]
    for name, app in result["apps"].items():
        report.append(f"| {name} | {app['bundle'] or '待核查容器/网页入口'} | {'已发现' if app['installed'] else '未确认'} | 尚未完成 |")
    (out / "coverage.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return result


class UI:
    """Observed-node selectors only; no unverified hard-coded click coordinates."""
    def __init__(self, dev, remote, out):
        self.dev, self.remote, self.out = dev, remote, out
        self.index = 0
        self.expected_bundle = None
        self.materials = {}

    def tree(self):
        self.index += 1
        tree=self.dev.dump_ui(self.remote + f"/layout_{self.index}.json")
        observed=self.dev.uptime()
        with (self.out/"ui_observations.jsonl").open("a",encoding="utf-8") as stream:
            stream.write(json.dumps({"time":observed,"focused_bundles":sorted(focused_bundles(tree))})+"\n")
            for node in ui_nodes(tree):
                text=str(node.get("text",""))
                if node.get("visible")=="true" and node.get("__ownerBundleName") and re.search(r"内存不足|低内存|not enough memory|insufficient memory",text,re.I):
                    stream.write(json.dumps({"category":"low_memory_popup","time":observed,
                                             "bundle":node["__ownerBundleName"],"source":"visible_ui_text", "text":text[:250]},ensure_ascii=False)+"\n")
        return tree

    def match(self, selector):
        if not isinstance(selector, dict) or not selector:
            raise ValueError("nonempty UI selector required")
        found = [n for n in ui_nodes(self.tree())
                 if (self.expected_bundle is None or n.get("__ownerBundleName")==self.expected_bundle)
                 and all(str(n.get(k, "")) == str(v) for k, v in selector.items())]
        if len(found) != 1:
            raise RuntimeError(f"UI selector matched {len(found)} nodes: {selector}")
        return found[0]

    def condition(self, condition):
        if not condition.get("selector"):
            raise ValueError("postcondition must identify a visible result node")
        self.match(condition["selector"])

    def action(self, action):
        kind = action["type"]
        if kind == "assert":
            self.condition(action)
        elif kind == "click":
            node = self.match(action["selector"])
            bounds = [int(n) for n in re.findall(r"-?\d+", str(node.get("bounds", "")))]
            if len(bounds) != 4:
                raise RuntimeError("node bounds invalid")
            x, y = (bounds[0]+bounds[2])//2, (bounds[1]+bounds[3])//2
            self.dev.checked(f"uitest uiInput click {x} {y}")
        elif kind == "text":
            self.action({"type":"click","selector":action["selector"]})
            value=self.materials[action["material"]] if "material" in action else action["value"]
            self.dev.checked("uitest uiInput text " + q(value))
        elif kind == "key":
            keys = action["codes"]
            if not keys or any(not isinstance(k, int) or k < 0 for k in keys):
                raise ValueError("invalid key codes")
            self.dev.checked("uitest uiInput keyEvent " + " ".join(map(str, keys)))
        elif kind == "swipe":
            node = self.match(action["selector"])
            b = [int(n) for n in re.findall(r"-?\d+", str(node.get("bounds", "")))]
            if len(b) != 4:
                raise ValueError("invalid swipe node")
            x=(b[0]+b[2])//2; height=b[3]-b[1]
            self.dev.checked(f"uitest uiInput swipe {x} {b[1]+height*3//4} {x} {b[1]+height//4} 350")
        elif kind == "wait":
            seconds = float(action["seconds"])
            if not 0 <= seconds <= 30:
                raise ValueError("wait exceeds 30 seconds")
            time.sleep(seconds)
        else:
            raise ValueError("unsupported UI action: " + kind)


def stage_materials(dev,suite,local_root):
    files={}
    for scene in suite["scenes"]:
        for step in scene["steps"]:
            for m in step["recipe"]["materials"]:
                path=(local_root/m["path"]).resolve()
                if not path.is_relative_to(local_root.resolve()) or digest(path)!=m["sha256"]:
                    raise RuntimeError("material changed or escaped root")
                if not re.fullmatch(r"[A-Za-z0-9_./-]+",m["path"]) or ".." in Path(m["path"]).parts:
                    raise RuntimeError("material device path must use safe relative names")
                files[m["path"]]=m["sha256"]
    key=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()[:16]
    root="/storage/media/100/local/files/Docs/realapp_dataset_"+key
    mapping={}
    for path,expected in files.items():
        remote=root+"/"+path
        dev.checked("mkdir -p "+q(remote.rsplit("/",1)[0]))
        dev.push(local_root/path,remote)
        if dev.checked("sha256sum "+q(remote)).split()[0]!=expected:raise RuntimeError("device material checksum mismatch")
        mapping[path]="/storage/Users/currentUser/realapp_dataset_"+key+"/"+path
    return mapping


def calibrate(dev, suite, suite_root, out):
    """Freeze only after executing real recipes; missing recipes yield a blocking report."""
    suite_root=Path(suite.get("material_root",suite_root)).resolve()
    pf = preflight(dev, suite, out / "preflight")
    errors = list(pf["blockers"])
    if not suite.get("reset_steps"):
        errors.append("initial_state_reset_recipe_missing")
    for scene in suite["scenes"]:
        for step in scene["steps"]:
            errors.extend(step["id"] + ":" + e for e in validate_recipe(step["recipe"], suite["apps"][step["app"]], suite_root))
    if errors:
        result = {"schema": SCHEMA, "status": "blocked", "reasons": errors, "frozen": False}
        save(out / "calibration.json", result)
        return result
    remote = "/data/local/tmp/realapp_dataset_calibration_" + str(int(time.time()))
    dev.checked("mkdir " + q(remote))
    ui = UI(dev, remote, out)
    ui.materials=stage_materials(dev,suite,suite_root)
    run_reset(dev,suite,ui)
    evidence = []
    for scene in suite["scenes"]:
        for step in scene["steps"]:
            spec = suite["apps"][step["app"]]; recipe=step["recipe"]
            ui.expected_bundle=spec["bundle"]
            start = dev.uptime()
            dev.launch(spec["bundle"], spec["ability"], spec["module"])
            for action in recipe["setup"] + recipe["actions"]:
                if dev.uptime() - start > recipe.get("timeout_seconds", 120):
                    raise RuntimeError("calibration step timeout: " + step["id"])
                ui.action(action)
            ui.condition(recipe["postcondition"])
            evidence.append({"step": step["id"], "start": start, "end": dev.uptime(), "verified": True})
    result = {"schema": SCHEMA, "status": "calibrated", "frozen": True, "steps": evidence,
              "suite": suite, "preflight": pf,"material_root":str(suite_root),"material_uris":ui.materials}
    save(out / "frozen.json", result)
    save(out / "calibration.json", {"status": "calibrated", "sha256": digest(out / "frozen.json")})
    return result


def run_reset(dev,suite,ui):
    if not suite.get("reset_steps"):raise RuntimeError("verified initial-state reset steps required")
    for step in suite["reset_steps"]:
        app=suite["apps"][step["app"]];ui.expected_bundle=app["bundle"]
        dev.launch(app["bundle"],app["ability"],app["module"])
        for action in step["actions"]:ui.action(action)
        ui.condition(step["postcondition"])


def start_sampler(session, binary, applications):
    session.ensure_active()
    dev=session.dev
    local=session.out / "apps.tsv"
    unique={v["uid"]: v["bundle"] for v in applications.values() if v.get("uid") is not None and v.get("bundle")}
    local.write_text("".join(f"{uid}\t{bundle}\n" for uid,bundle in sorted(unique.items())), encoding="utf-8")
    dev.push(local, session.remote+"/apps.tsv")
    dev.push(binary, session.remote+"/sampler")
    binary_hash=digest(binary)
    if dev.checked("sha256sum "+q(session.remote+"/sampler")).split()[0]!=binary_hash:
        raise RuntimeError("device sampler checksum mismatch")
    save(session.out/"sampler_build.json",{"binary_sha256":binary_hash,"source_sha256":digest(HERE/"sampler.cpp")})
    dev.checked("chmod 700 " + q(session.remote+"/sampler"))
    pid=dev.checked(f"nohup {q(session.remote+'/sampler')} {q(session.remote)} {session.ttl} > {q(session.remote+'/sampler.log')} 2>&1 < /dev/null & echo $!").strip()
    save(session.out / "sampler_identity.json", dev.identity(int(pid)))
    time.sleep(0.5)
    ready=dev.read(session.remote+"/gate.jsonl")
    if '"event":"ready"' not in ready:
        raise RuntimeError("sampler not ready")
    return pid


def retrieve(session):
    for name in ("system.jsonl", "processes.jsonl", "kernel.jsonl", "gate.jsonl", "sampler.log", "dd.log", "restore.log"):
        try:
            session.dev.receive(session.remote+"/"+name, session.out/name)
        except Exception as exc:
            save(session.out/(name+".missing.json"), {"error": str(exc)})


def smoke(dev, out, binary, *, exercise_watchdog=False, full_fill=False):
    if full_fill:
        blockers = pressure_blockers(dev.target, fill_mib(memory(dev.read("/proc/meminfo"))["MemTotal"]))
        if blockers:
            result = {"schema": SCHEMA, "profile": "fill_calibration", "status": "blocked",
                      "valid_for_training": False, "completed": False, "blockers": blockers,
                      "device_mutated": False}
            save(out / "result.json", result)
            return result
    session=Session(dev,out,"observe",ttl=1800 if full_fill else 20 if exercise_watchdog else 90)
    result={"schema": SCHEMA, "profile": "fill_calibration" if full_fill else "smoke", "valid_for_training": False}
    checkpoints=Checkpoints(session)
    try:
        session.prepare()
        start_sampler(session,binary,{})
        checkpoints.start()
        mib=fill_mib(memory(session.snapshot["meminfo"])["MemTotal"]) if full_fill else 8
        session.allocate(mib)
        deadline=time.monotonic()+(300 if full_fill else 20)
        while time.monotonic()<deadline:
            allocated=session.allocation_result(mib)
            if allocated is not None:
                if not allocated["valid"]: raise RuntimeError("fill invalid")
                result["allocation"]=allocated
                break
            time.sleep(0.5)
        else: raise RuntimeError("fill timeout")
        dev.text(session.remote+"/stage","baseline")
        if full_fill:
            print("Full dd write complete; observing 60-second baseline",flush=True)
            wait_stage(session,60,"baseline")
        else:time.sleep(2)
        if exercise_watchdog:
            time.sleep(22)
            result["watchdog_receipt"] = dev.read(session.remote+"/restored")
        result["completed"]=True
    except Exception as exc:
        result.update(completed=False,error=str(exc))
    finally:
        result["restore"]=session.restore()
        result["checkpoints_stopped"]=checkpoints.finish()
        if session.started:
            time.sleep(0.5)
            retrieve(session)
            try:
                evidence=event_summary(out)
                result["capture_complete"]=evidence["stream_complete"]
            except Exception as exc:
                result["capture_complete"]=False;result["measurement_error"]=str(exc)
        result["passed"] = result.get("completed",False) and result["restore"]["verified"] and result.get("capture_complete",False)
        save(out / "result.json", result)
    return result


def event_summary(out):
    result=classify_kernel(read_kernel_capture(out/"kernel.jsonl"),rows(out/"processes.jsonl"),rows(out/"gate.jsonl"))
    save(out/"events.json",result)
    return result


def additional_events(session, kernel_result):
    out=session.out;gate=rows(out/"gate.jsonl")
    status={"complete":False}
    try:
        if not gate or "wall_ms" not in gate[-1]:raise RuntimeError("missing completed sampler clock anchors")
        raw=session.dev.checked(f"hisysevent -l -s {gate[0]['wall_ms']} -e {gate[-1]['wall_ms']} -m 100000",timeout=30)
        (out/"hisysevent.raw").write_text(raw,encoding="utf-8")
        status=classify_system(json_lines(raw),rows(out/"processes.jsonl"),gate)
        kernel_result["events"].extend(status["events"])
    except Exception as exc:status["error"]=str(exc)
    save(out/"system_event_status.json",status)
    kernel_result["events"].extend(r for r in rows(out/"ui_observations.jsonl") if r.get("category"))
    kernel_result["system_event_status"]={k:v for k,v in status.items() if k!="events"}
    save(out/"events.json",kernel_result)
    return kernel_result


def export_dataset(inputs, out):
    out.mkdir(parents=True,exist_ok=True)
    count=0; runs=[]
    with (out/"windows.jsonl").open("w",encoding="utf-8") as f:
        for folder in inputs:
            result=load(folder/"result.json")
            if not result.get("valid_for_training"):
                runs.append({"run":str(folder),"exported":False,"reason":"run_not_qualified"}); continue
            event_doc=load(folder/"events.json")
            raw=rows(folder/"system.jsonl")
            process_rows=[p for p in rows(folder/"processes.jsonl") if "pid" in p]
            samples=[]; last=-1
            for r in raw:
                if r["time"]-last<0.9 or r.get("stage") not in ("baseline","actions","post"): continue
                last=r["time"]; m=memory(r["meminfo"])
                latest={}
                for p in process_rows:
                    if 0<=r["time"]-p["time"]<=1.5:
                        latest[(p["pid"],p["start_ticks"])]=p
                apps={}
                for p in latest.values():
                    metrics=memory(p["status"])
                    app=apps.setdefault(p["bundle"],{"rss_sum":0,"anon_sum":0,"swap_sum":0,"members":[]})
                    if not all(k in metrics for k in ("VmRSS","RssAnon","VmSwap")):
                        app["incomplete"]=True;continue
                    app["rss_sum"]+=metrics["VmRSS"];app["anon_sum"]+=metrics["RssAnon"];app["swap_sum"]+=metrics["VmSwap"]
                    app["members"].append({"pid":p["pid"],"start_ticks":p["start_ticks"],"observed_at":p["time"]})
                samples.append({"time":r["time"],"available":m.get("MemAvailable"),"free":m.get("MemFree"),
                                "shmem":m.get("Shmem"),"swap_free":m.get("SwapFree"),"gate":r.get("gate"),
                                "phase":result["phase"],"apps":apps,"service_control":r.get("service_control")})
            windows=training_windows(samples,event_doc["events"],event_coverage=event_doc["oom_label_coverage_complete"])
            for window in windows:
                window.update(run_id=folder.name,pair_id=result["pair_id"],split_group=result["scene"])
                f.write(json.dumps(window,ensure_ascii=False)+"\n")
            count+=len(windows); runs.append({"run":str(folder),"exported":True,"windows":len(windows)})
    save(out/"export.json",{"schema":SCHEMA,"windows":count,"runs":runs,
                            "split_rule":"all repeats and paired phases of a scene belong to the same split"})
    with (out/"labels.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        columns=["run_id","pair_id","split_group","time","horizon_seconds","valid","available","minimum_available","oom","time_to_oom","oom_coverage_complete","gate_changed"]
        writer=csv.DictWriter(stream,fieldnames=columns,extrasaction="ignore");writer.writeheader()
        for window in rows(out/"windows.jsonl"):
            for horizon,label in window["labels"].items():
                writer.writerow({**{k:window[k] for k in ("run_id","pair_id","split_group","time")},"horizon_seconds":horizon,**label})
    return {"windows":count,"runs":len(runs)}


def service_logs(session, suffix):
    directory=session.snapshot["service"]["log_dir"]
    dest=session.out/("service_"+suffix);dest.mkdir(exist_ok=True)
    for filename in ("daily_actions.csv","events.csv","events.csv.1","system.csv","system.csv.1","apps.csv","apps.csv.1"):
        try: session.dev.receive(directory+"/"+filename,dest/filename)
        except Exception as exc: save(dest/(filename+".missing.json"),{"error":str(exc)})


def service_interventions(out, phase):
    before=out/"service_before/daily_actions.csv";after=out/"service_after/daily_actions.csv"
    result={"complete":False,"actions":[],"observe_no_writes":None}
    if before.is_file() and after.is_file():
        old=before.read_bytes();new=after.read_bytes()
        if new.startswith(old):
            parsed=list(csv.DictReader(new.decode("utf-8").splitlines()))
            n=len(list(csv.DictReader(old.decode("utf-8").splitlines())))
            result["actions"]=parsed[n:];result["complete"]=True
            result["observe_no_writes"]=not any(str(a.get("write_attempted","")).lower() in ("1","true") for a in parsed[n:]) if phase=="observe" else None
        else:result["reason"]="ledger_rotated_or_rewritten"
    else:result["reason"]="missing_ledger"
    save(out/"interventions.json",result)
    return result


def wait_stage(session, seconds, stage):
    session.ensure_active()
    session.dev.text(session.remote+"/stage",stage)
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        time.sleep(min(5,max(0,end-time.monotonic())))
        if session.dev.read("/proc/sys/kernel/random/boot_id").strip()!=session.snapshot["boot_id"]:
            raise RuntimeError("device rebooted")
        if session.dev.identity(session.snapshot["service"]["pid"])["start_ticks"]!=session.snapshot["service"]["start_ticks"]:
            raise RuntimeError("service identity changed")


def collect(dev, frozen_path, out, binary):
    out.mkdir(parents=True,exist_ok=True)
    frozen=load(frozen_path)
    if frozen.get("frozen") is not True or frozen.get("status")!="calibrated":
        result={"schema":SCHEMA,"status":"blocked","reason":"real UI operations have not been calibrated; no pressure started"}
        save(out/"collection.json",result);return result
    suite=frozen["suite"];pf=preflight(dev,suite,out/"preflight")
    errors=list(pf["blockers"])
    errors.extend("unresolved_pressure_incident:"+b["incident"] for b in pf["pressure_incidents"])
    for name,app in pf["apps"].items():
        prior=frozen["preflight"]["apps"][name]
        if (app.get("uid"),app.get("version_code"))!=(prior.get("uid"),prior.get("version_code")):
            errors.append("application_version_or_uid_changed:"+name)
    if errors:
        result={"status":"blocked","reasons":errors};save(out/"collection.json",result);return result
    materials=stage_materials(dev,suite,Path(frozen["material_root"]))
    plan=schedule(suite);save(out/"schedule.json",plan)
    completed=[]
    for item in plan:
        scene=next(s for s in suite["scenes"] if s["id"]==item["scene"])
        run=out/f"{item['order']:02d}_{item['scene']}_{item['phase']}"
        if run.exists(): raise FileExistsError("refusing to overwrite run: "+str(run))
        session=Session(dev,run,item["phase"])
        checkpoints=Checkpoints(session)
        result={**item,"schema":SCHEMA,"profile":"pilot","frozen_sha256":digest(frozen_path),
                "valid_for_training":False,"completed":False}
        try:
            session.prepare()
            reset_ui=UI(dev,session.remote,run);reset_ui.materials=materials
            run_reset(dev,suite,reset_ui)
            start_sampler(session,binary,pf["apps"])
            checkpoints.start()
            service_logs(session,"before")
            mib=fill_mib(memory(session.snapshot["meminfo"])["MemTotal"])
            session.allocate(mib)
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                allocation=session.allocation_result(mib)
                if allocation is not None:
                    if not allocation["valid"]: raise RuntimeError("allocation failed or partial")
                    break
                time.sleep(2)
            else: raise RuntimeError("allocation exceeded 300 seconds")
            wait_stage(session,60,"baseline")
            raw=dev.read(session.remote+"/system.jsonl")
            samples=[json.loads(line) for line in raw.splitlines() if line.strip()]
            baseline=[s for s in samples if s["stage"]=="baseline"]
            if not baseline or any(memory(s["meminfo"]).get("MemAvailable",0)*10 < 8*1024**3 for s in baseline):
                raise RuntimeError("baseline below reference threshold or incomplete")
            # Do not begin actions after a preparation-stage kernel OOM candidate.
            kernel=dev.read(session.remote+"/kernel.jsonl")
            if re.search(r"out of [Mm]emory|\(OOM\)|Killed process",kernel):
                raise RuntimeError("kernel memory failure during preparation")
            ui=UI(dev,session.remote,run)
            ui.materials=materials
            action_end=dev.uptime()+1200
            dev.text(session.remote+"/stage","actions")
            result["actions_begin"]=dev.uptime()
            with (run/"actions.jsonl").open("w",encoding="utf-8") as log:
                for step in scene["steps"]:
                    spec=suite["apps"][step["app"]];recipe=step["recipe"]
                    ui.expected_bundle=spec["bundle"]
                    row={"step":step["id"],"app":step["app"],"start":dev.uptime(),"verified":False}
                    try:
                        dev.launch(spec["bundle"],spec["ability"],spec["module"])
                        for action in recipe["setup"]+recipe["actions"]:
                            session.ensure_active()
                            if dev.uptime()>min(action_end,row["start"]+recipe.get("timeout_seconds",120)):
                                raise RuntimeError("operation timeout")
                            ui.action(action)
                        ui.condition(recipe["postcondition"])
                        row["verified"]=True
                        time.sleep(min(float(recipe.get("dwell_seconds",15)),30))
                    except Exception as exc:
                        row["error"]=str(exc)
                        raise
                    finally:
                        row["end"]=dev.uptime();log.write(json.dumps(row,ensure_ascii=False)+"\n");log.flush()
            wait_stage(session,60,"post")
            result["completed"]=True
        except Exception as exc:
            result["error"]=str(exc)
            if result.get("actions_begin") is not None:
                try:wait_stage(session,60,"post")
                except Exception as post_exc:result["post_observation_error"]=str(post_exc)
        finally:
            if session.snapshot and session.started:
                try: service_logs(session,"after")
                except Exception as exc: result["service_log_error"]=str(exc)
            result["restore"]=session.restore()
            result["checkpoints_stopped"]=checkpoints.finish()
            if session.started:
                time.sleep(.5);retrieve(session)
            try:
                event_result=event_summary(run)
                if session.started:event_result=additional_events(session,event_result)
                samples=rows(run/"system.jsonl");save(run/"gate_segments.json",segment_states(samples))
            except Exception as exc:
                result["measurement_error"]=str(exc)
                event_result={"events":[],"oom_label_coverage_complete":False,"error":str(exc)}
                save(run/"events.json",event_result)
            interventions=service_interventions(run,item["phase"])
            confirmed=any(e["category"] in ("oom_kill","allocation_failure","memory_policy_kill") and e.get("time") is not None
                          and e["time"]>=result.get("actions_begin",float("inf")) for e in event_result["events"])
            result["outcome"]="completed" if result["completed"] else "confirmed_memory_failure" if confirmed else "unconfirmed"
            result["valid_for_training"]=bool((result["completed"] or confirmed) and result["restore"]["verified"]
                and interventions["complete"] and interventions["observe_no_writes"] is not False
                and not result.get("measurement_error") and not result.get("service_log_error"))
            save(run/"result.json",result)
        completed.append(result)
        save(out/"collection.json",{"schema":SCHEMA,"status":"running","runs":completed})
        if not result["restore"]["verified"] or not result["valid_for_training"]:
            result={"schema":SCHEMA,"status":"blocked","runs":completed,"remaining":len(plan)-len(completed),
                    "reason":"restore or workload failure; inspect evidence before retry"}
            save(out/"collection.json",result);return result
    result={"schema":SCHEMA,"status":"complete","runs":completed}
    save(out/"collection.json",result);return result
