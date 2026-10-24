#!/bin/bash
# 2026-10-22: Douyin s03_comments w3 failed (no comment list found); rerun it once after all formal
# runs (Imgur and Evolution) are done, keeping the failed run under douyin_trial/_invalid/.
cd "$(dirname "$0")" || exit 1
RES=/home/boyi/HNUST/huawei_server/results/linux_app_mem_20261021
until grep -q "^all done" "$RES/formal_queue_20261022c.log" 2>/dev/null; do sleep 15; done
out=$RES/douyin_trial
mkdir -p "$out/_invalid"; mv "$out/s03_comments_w3" "$out/_invalid/" 2>/dev/null
echo "$(date '+%F %T') rerun s03_comments w3" >> "$out/run.log"
APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5 timeout 1800 python3 -I web_trial.py douyin "$out" --rep=w3 s03_comments >> "$out/rerun_w3.log" 2>&1
systemctl --user reset-failed "appmem-douyin-*" 2>/dev/null
python3 -I report.py "$out" --aggregate > "$out/table.md" 2>> "$out/run.log"
python3 -I report_wss.py "$out" > "$out/table_wss.md" 2>> "$out/run.log"
echo "$(date '+%F %T') rerun s03_comments w3 done" >> "$out/run.log"
echo "douyin rerun done $(date +%T)" >> "$RES/formal_queue_20261022c.log"
