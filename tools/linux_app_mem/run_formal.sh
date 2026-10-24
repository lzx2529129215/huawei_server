#!/bin/bash
# Formal runs, one app after another: 3 repeats each (w1..w3) on the Xvfb screen :99, appmem.slice,
# cache eviction on, RSS/WSS on. Nothing else may run meanwhile (shared files would be charged to
# whichever cgroup reads them first). After each app: summary tables + a Windows toast.
# Usage: run_formal.sh APP [APP ...]   (shotcut gimp vlc librecad chrome edge bilibili douyin ...)
cd "$(dirname "$0")" || exit 1
RES=/home/boyi/HNUST/huawei_server/results/linux_app_mem_20261021
export APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5
unset APPMEM_DISPLAY APPMEM_SLICE APPMEM_NO_EVICT
for app in "$@"; do
  case $app in
    chrome|edge) cmd=(python3 -I browser_trial.py "$app") ;;
    bilibili|douyin|imgur) cmd=(python3 -I web_trial.py "$app") ;;
    *) cmd=(python3 -I "${app}_trial.py") ;;
  esac
  out=$RES/${app}_trial
  mkdir -p "$out"
  echo "$(date '+%F %T') start $app" >> "$out/run.log"
  for rep in w1 w2 w3; do
    timeout 5400 "${cmd[@]}" "$out" --rep=$rep >> "$out/run_$rep.log" 2>&1
    systemctl --user reset-failed "appmem-$app-*" 2>/dev/null
  done
  echo "$(date '+%F %T') done $app" >> "$out/run.log"
  python3 -I report.py "$out" --aggregate > "$out/table.md" 2>> "$out/run.log"
  python3 -I report_wss.py "$out" > "$out/table_wss.md" 2>> "$out/run.log"
  errs=$(grep -c "ERROR" "$out"/run_w*.log | awk -F: '{s+=$2} END {print s+0}')
  python3 -I notify_windows.py "$app 正式测量完成" "3 轮已跑完，错误 $errs 处" > /dev/null 2>&1
done
