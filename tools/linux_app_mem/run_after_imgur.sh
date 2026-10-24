#!/bin/bash
# 2026-10-22: after the Douyin + Imgur formal queue, run Evolution's formal runs (3 reps), then stop
# the evolution-alarm-notify copies that D-Bus starts on the WSLg desktop, and send a toast.
cd "$(dirname "$0")" || exit 1
RES=/home/boyi/HNUST/huawei_server/results/linux_app_mem_20261021
until grep -q " done imgur" "$RES/imgur_trial/run.log" 2>/dev/null; do sleep 15; done
sleep 20
bash run_formal.sh evolution
# exact process name (comm is cut to 15 chars), so this never matches a shell
for p in $(pgrep -x evolution-alarm); do
  tr '\0' '\n' < /proc/"$p"/environ | grep -qx 'DISPLAY=:0' && kill "$p"
done
python3 -I notify_windows.py "全部测完" "抖音、Imgur、Evolution 正式测量结束" > /dev/null 2>&1
echo "all done $(date +%T)" >> "$RES/formal_queue_20261022c.log"
