#!/bin/bash
# After the formal queue has been stopped (stop_queue_after.py prints "done"), re-measure two
# operations that were not measured correctly (user request 2026-10-22):
#   VLC s07_s10_background (resume used a lagging state check)   - stage-3 method like the rest of VLC
#   Rhythmbox s03_covers (MPRIS call timed out after 20 s)        - full-clear method like the rest of Rhythmbox
# The first results are kept under <app>_trial/_replaced/. Then tables are rebuilt and a toast is sent.
cd "$(dirname "$0")" || exit 1
RES=/home/boyi/HNUST/huawei_server/results/linux_app_mem_20261021
until grep -q "^done" "$RES/stop_queue_after.log" 2>/dev/null; do sleep 10; done
export APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5
unset APPMEM_DISPLAY APPMEM_SLICE APPMEM_NO_EVICT
for spec in "vlc s07_s10_background" "rhythmbox s03_covers"; do
  set -- $spec; app=$1; op=$2; out=$RES/${app}_trial
  mkdir -p "$out/_replaced"
  for d in "$out"/${op}_w*; do [ -d "$d" ] && mv "$d" "$out/_replaced/"; done
  echo "$(date '+%F %T') rerun $op" >> "$out/run.log"
  for rep in w1 w2 w3; do
    timeout 1800 python3 -I "${app}_trial.py" "$out" --rep=$rep "$op" >> "$out/rerun_$rep.log" 2>&1
    systemctl --user reset-failed "appmem-$app-*" 2>/dev/null
  done
  python3 -I report.py "$out" --aggregate > "$out/table.md" 2>> "$out/run.log"
  python3 -I report_wss.py "$out" > "$out/table_wss.md" 2>> "$out/run.log"
  echo "$(date '+%F %T') rerun $op done" >> "$out/run.log"
done
errs=$(cat "$RES"/vlc_trial/rerun_w*.log "$RES"/rhythmbox_trial/rerun_w*.log 2>/dev/null | grep -c ERROR)
python3 -I notify_windows.py "补测完成" "VLC S07/S10 和 Rhythmbox S03 已重测 3 次，错误 $errs 处" > /dev/null 2>&1
echo "rerun done $(date +%T)" >> "$RES/stop_queue_after.log"
