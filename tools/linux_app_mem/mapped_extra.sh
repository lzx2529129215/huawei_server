#!/bin/bash
# One daily-operation run each (S02, rep m1) only to record mapped_files.json -- which shared system
# files (libraries, fonts, icons ...) the app maps in daily use -- for apps measured before that record
# existed (2026-10-22). Used to count shared files once when adding apps (section 5 of the stage-4
# doc). Results go to <app>_trial_mapped/ so they never enter the 3-run tables.
cd "$(dirname "$0")" || exit 1
RES=/home/boyi/HNUST/huawei_server/results/linux_app_mem_20261021
until grep -q "rerun done" "$RES/stop_queue_after.log" 2>/dev/null; do sleep 10; done
export APPMEM_WSS=1 APPMEM_STOP_TIMEOUT=5
unset APPMEM_DISPLAY APPMEM_SLICE APPMEM_NO_EVICT
for spec in "shotcut s02_edit" "gimp s02_edit" "vlc s02_play" "librecad s02_edit" \
            "gnote s02_edit" "rhythmbox s02_play" "thunderbird s02_read"; do
  set -- $spec
  timeout 900 python3 -I "$1_trial.py" "$RES/$1_trial_mapped" --rep=m1 "$2" >> "$RES/mapped_extra.log" 2>&1
  systemctl --user reset-failed "appmem-$1-*" 2>/dev/null
done
python3 -I notify_windows.py "测量全部结束" "补测和共享库记录都已完成" > /dev/null 2>&1
echo "mapped extra done $(date +%T)" >> "$RES/stop_queue_after.log"
