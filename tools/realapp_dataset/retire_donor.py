"""Close only manifest-verified legacy test windows, preserving other tabs."""
from __future__ import annotations
import re
import time
from pathlib import Path
from memscenes.device import ui_nodes,quote as q
from .core import load,save


def retire(dev, manifest: Path, out: Path):
    plan=load(manifest);expected={"file://"+p["ui_uri"] for w in plan["windows"] for p in w["pages"]}
    root="/data/local/tmp/realapp_dataset_retire_"+str(int(time.time()))
    dev.checked("mkdir "+q(root));out.mkdir(parents=True,exist_ok=True)
    result=load(out/"retired.json") if (out/"retired.json").exists() else {"closed_windows":[],"expected_pages":len(expected),"complete":False}
    result.pop("error",None)
    result["boot_id"]=dev.read("/proc/sys/kernel/random/boot_id").strip()
    def nodes(window):return [n for n in ui_nodes(dev.dump_ui(root+"/layout.json")) if n.get("__ownerBundleName")==plan["bundle"] and str(n.get("hostWindowId"))==str(window)]
    def click(node):
        b=[int(x) for x in re.findall(r"-?\d+",node["bounds"])]
        if len(b)!=4:raise RuntimeError("bad observed bounds")
        dev.checked(f"uitest uiInput click {(b[0]+b[2])//2} {(b[1]+b[3])//2}")
    try:
        for index in range(len(result["closed_windows"]),plan["window_count"]):
            wms=dev.checked("hidumper -s WindowManagerService -a '-a'")
            windows=re.findall(r"(?m)^browser\d+\s+\d+\s+(\d+)\s+(\d+)\s+\d+\s+\d+\s+\d+\s+(-?\d+)",wms)
            if not windows:raise RuntimeError("no remaining browser window available")
            top=max(windows,key=lambda w:int(w[2]));window=top[1]
            current=nodes(window)
            tabs=[n for n in current if n.get("id")=="pc_panel_text_in_web"]
            if len(tabs)!=3 or any(n.get("text")!="Memshift local table" and n.get("text") not in expected for n in tabs):
                raise RuntimeError("current window is not exactly three legacy test tabs")
            verified=[]
            for tab in sorted(tabs,key=lambda n:int(re.findall(r"\d+",n["bounds"])[0])):
                click(tab);time.sleep(1)
                current=nodes(window)
                address=[n["text"] for n in current if n.get("type")=="TextInput" and n.get("hint")=="搜索或输入网址"]
                if len(address)!=1 or address[0] not in expected:
                    raise RuntimeError("tab URL is not in legacy manifest")
                verified.append(address[0])
            if len(set(verified))!=3:raise RuntimeError("tab selection did not verify three distinct pages")
            close=[n for n in current if n.get("id")=="EnhanceCloseBtn"]
            if len(close)!=1:raise RuntimeError("window close ownership ambiguous")
            click(close[0]);time.sleep(2)
            result["closed_windows"].append({"urls":verified,"window_id":close[0].get("hostWindowId")})
            save(out/"retired.json",result)
            print(f"closed legacy window {index+1}",flush=True)
        actual={url for window in result["closed_windows"] for url in window["urls"]}
        result["complete"]=actual==expected
    except Exception as exc:result["error"]=str(exc)
    save(out/"retired.json",result)
    return result
