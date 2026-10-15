"""Action journal for supervised, observed UI operations during a manual trial."""
import json
import time
from pathlib import Path

from memscenes.device import quote as q, ui_nodes
from .device_ops import Target
from .core import load, memory, save

OUT = Path("results/realapp_dataset_20261015/manual20_apply01")


class Live:
    def __init__(self, out=OUT):
        self.out = Path(out)
        self.dev = Target(r"D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe", "3QC0124C03000514")
        self.receipt = load(self.out / "session.json")
        self.remote = self.receipt["remote"]
        self.suite = load(Path("config/realapp_dataset/suite.json"))

    def act(self, name, command, wait=1):
        if not (self.out / "ready.json").exists():
            raise RuntimeError("baseline not complete")
        if self.dev.read(self.remote + "/stage").strip() != "actions":
            raise RuntimeError("trial no longer in actions phase")
        if self.dev.read('/data/local/tmp/realapp_dataset.lock/owner').strip() != self.receipt['token']:
            raise RuntimeError('trial lease no longer owned')
        row={"action":name,"command":command,"start":self.dev.uptime(),"execution_ok":False,"workload_verified":False}
        with (self.out/'action_requests.jsonl').open('a',encoding='utf8') as f:
            f.write(json.dumps(row,ensure_ascii=False)+'\n')
        try:
            result=self.dev.checked(command,timeout=30)
            row["execution_ok"]="failed to start ability" not in result.lower()
            row["response"]=result
            if wait:time.sleep(wait)
            return result
        except Exception as exc:
            row["error"]=str(exc)
            raise
        finally:
            try:
                row["end"]=self.dev.uptime()
                row["available"]=memory(self.dev.read('/proc/meminfo')).get('MemAvailable')
            except Exception as exc:
                row["end"]=None
                row["final_observation_error"]=str(exc)
            with (self.out/'actions.jsonl').open('a',encoding='utf8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')

    def launch(self,name):
        a=self.suite['apps'][name]
        return self.act('launch:'+name,f"aa start -b {q(a['bundle'])} -a {q(a['ability'])} -m {q(a['module'])}",wait=4)

    def snapshot(self,name):
        t=self.dev.dump_ui(self.remote+'/live_ui.json')
        nodes=list(ui_nodes(t))
        self.dev.screenshot(self.remote+'/live.png',self.out/(name+'.png'))
        print('available_GiB',round(memory(self.dev.read('/proc/meminfo'))['MemAvailable']/1024**3,3))
        for n in nodes:
            if n.get('text') and n.get('__ownerBundleName')!='com.ohos.sceneboard':
                print({k:n.get(k) for k in ['text','id','bounds','__ownerBundleName']})
        return t

    def verify(self,name,evidence):
        with (self.out/'verified_operations.jsonl').open('a',encoding='utf8') as f:
            f.write(json.dumps({'time':self.dev.uptime(),'operation':name,'evidence':evidence},ensure_ascii=False)+'\n')
