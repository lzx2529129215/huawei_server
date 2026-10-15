from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.realapp_dataset.catalog import suite
from tools.realapp_dataset.core import load, save
from tools.realapp_dataset.device_ops import Target,recover
from tools.realapp_dataset.pipeline import build_sampler, calibrate, collect, export_dataset, preflight, smoke
from tools.realapp_dataset.inventory import inspect_ui
from tools.realapp_dataset.fixtures import generate
from tools.realapp_dataset.audit import audit_partial


def main():
    parser=argparse.ArgumentParser(description="真实应用 dd 占位数据集：采集与训练导出")
    parser.add_argument("--hdc",default=r"D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe")
    parser.add_argument("--target",default="3QC0124C03000514")
    sub=parser.add_subparsers(dest="command",required=True)
    p=sub.add_parser("init");p.add_argument("--output",type=Path,required=True)
    p=sub.add_parser("fixtures");p.add_argument("--output",type=Path,required=True)
    for command in ("preflight","calibrate","inventory-ui"):
        p=sub.add_parser(command);p.add_argument("--suite",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    p=sub.add_parser("build");p.add_argument("--output",type=Path,default=ROOT/"build/realapp_dataset/sampler")
    p=sub.add_parser("smoke");p.add_argument("--output",type=Path,required=True);p.add_argument("--watchdog-test",action="store_true")
    p=sub.add_parser("calibrate-fill");p.add_argument("--output",type=Path,required=True)
    p=sub.add_parser("collect");p.add_argument("--frozen",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    p=sub.add_parser("recover");p.add_argument("--run",type=Path,required=True);p.add_argument("--after-reboot",action="store_true")
    p=sub.add_parser("audit");p.add_argument("--run",type=Path,required=True)
    p=sub.add_parser("export");p.add_argument("--runs",type=Path,nargs="+",required=True);p.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.command=="init":
        if args.output.exists(): raise FileExistsError(args.output)
        save(args.output,suite());result={"suite":str(args.output)}
    elif args.command=="fixtures": result=generate(args.output)
    elif args.command=="audit":result=audit_partial(args.run)
    elif args.command=="build": result={"binary":str(build_sampler(args.output))}
    elif args.command=="export": result=export_dataset(args.runs,args.output)
    else:
        dev=Target(args.hdc,args.target)
        if args.command=="recover":result=recover(dev,args.run,after_reboot=args.after_reboot)
        elif args.command=="preflight": result=preflight(dev,load(args.suite),args.output)
        elif args.command=="calibrate": result=calibrate(dev,load(args.suite),args.suite.parent,args.output)
        elif args.command=="inventory-ui": result={"apps":inspect_ui(dev,load(args.suite),args.output)}
        else:
            binary=build_sampler(ROOT/"build/realapp_dataset/sampler")
            if args.command=="smoke": result=smoke(dev,args.output,binary,exercise_watchdog=args.watchdog_test)
            elif args.command=="calibrate-fill":result=smoke(dev,args.output,binary,full_fill=True)
            else: result=collect(dev,args.frozen,args.output,binary)
    print(json.dumps({k:v for k,v in result.items() if k in ("status","passed","completed","error","windows","runs","suite","binary","blockers")},ensure_ascii=False,indent=2))
    return 2 if result.get("status")=="blocked" or result.get("passed") is False else 0


if __name__=="__main__":
    raise SystemExit(main())
