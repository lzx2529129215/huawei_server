#!/bin/bash
# 2026-10-22 evening: re-measure the working set with a fixed 10 s window (Denning's w(t, 10 s),
# clear_refs + Referenced like Brendan Gregg's wss tool), same operations, 3 repeats each.
# Results: results/linux_app_mem_20261021/wss10/<app>_trial/ (table.md cgroup, table_wss10.md WSS).
# Usage: run_wss10.sh APP [APP ...]
cd "$(dirname "$0")" || exit 1
RES=/home/boyi/HNUST/huawei_server/results/linux_app_mem_20261021/wss10
mkdir -p "$RES"
export APPMEM_WSS=1 APPMEM_WSS_WINDOW=10 APPMEM_STOP_TIMEOUT=5
unset APPMEM_DISPLAY APPMEM_SLICE APPMEM_NO_EVICT
for app in "$@"; do
  case $app in
    chrome|edge) cmd=(python3 -I browser_trial.py "$app") ;;
    bilibili|douyin|imgur) cmd=(python3 -I web_trial.py "$app") ;;
    *) cmd=(python3 -I "${app}_trial.py") ;;
  esac
  if [ "$app" = evolution ]; then           # the test settings (see evolution_trial.py)
    gsettings set org.gnome.evolution.mail show-startup-wizard false
    gsettings set org.gnome.evolution.mail show-to-do-bar false
    gsettings set org.gnome.evolution.mail show-attachment-bar false
  fi
  out=$RES/${app}_trial
  mkdir -p "$out"
  echo "$(date '+%F %T') start $app" >> "$out/run.log"
  for rep in w1 w2 w3; do
    timeout 5400 "${cmd[@]}" "$out" --rep=$rep >> "$out/run_$rep.log" 2>&1
    systemctl --user reset-failed "appmem-$app-*" 2>/dev/null
  done
  echo "$(date '+%F %T') done $app" >> "$out/run.log"
  python3 -I report.py "$out" --aggregate > "$out/table.md" 2>> "$out/run.log"
  python3 -I report_wss10.py "$out" > "$out/table_wss10.md" 2>> "$out/run.log"
  errs=$(cat "$out"/run_w*.log 2>/dev/null | grep -c ERROR)
  echo "$(date '+%F %T') $app done, $errs ERROR lines" >> "$RES/queue.log"
done
