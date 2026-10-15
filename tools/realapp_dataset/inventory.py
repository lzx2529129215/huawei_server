"""Inspect installed launchers without accepting terms or entering account data."""
from __future__ import annotations

import time
from pathlib import Path
from memscenes.device import ui_nodes, quote as q
from .core import save


def inspect_ui(dev, suite, out: Path):
    out.mkdir(parents=True,exist_ok=True)
    remote="/data/local/tmp/realapp_dataset_inventory_"+str(int(time.time()))
    dev.checked("mkdir "+q(remote))
    results=[]
    terms=("登录","登陆","同意","拒绝","继续","授权","注册","激活","试用","许可证","欢迎","隐私","协议","新建","打开","导入")
    for index,(name,app) in enumerate(suite["apps"].items()):
        if not all(app.get(k) for k in ("bundle","module","ability")):continue
        row={"app":name,"bundle":app["bundle"],"workload_ready":False}
        before=dev.checked("pidof "+q(app["bundle"])+" || true").strip()
        # Do not force-stop a preexisting group even if its main process is absent.
        groups=dev.checked("ls /dev/memcg/100/"+q(app["bundle"])+"_*/cgroup.procs 2>/dev/null || true").splitlines()
        has_members=any(dev.read(p).strip() for p in groups if p.startswith("/dev/memcg/"))
        try:
            dev.launch(app["bundle"],app["ability"],app["module"])
            time.sleep(2)
            tree=dev.dump_ui(remote+f"/app_{index}.json")
            nodes=[n for n in ui_nodes(tree) if n.get("__ownerBundleName")==app["bundle"]]
            # Retain only fixed readiness terms; do not archive mailbox/document text.
            texts=[str(n.get("text","")) for n in nodes]
            row["observed_terms"]=sorted({term for term in terms if any(term in text for text in texts)})
            row["node_count"]=len(nodes)
            row["launch_succeeded"]=True
            row["status"]=("no_owned_accessible_ui" if not nodes else "initialization_or_login_ui_observed"
                           if set(row["observed_terms"]) & {"登录","登陆","同意","拒绝","激活","许可证","隐私","协议"}
                           else "launched_needs_workload_calibration")
        except Exception as exc:
            row.update(status="inspection_failed",error=str(exc))
        finally:
            if not before and not has_members:
                dev.force_stop(app["bundle"])
                row["closed_owned_launch"]=True
        results.append(row);save(out/"readiness.json",results)
        print(name+": "+row["status"],flush=True)
    return results
