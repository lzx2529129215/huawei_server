#!/usr/bin/env bash
# Aligned-peak experiment of results/linux_oom_lab_rss_plan_20261022/实验设置.md (v2, 2026-10-22):
# scenarios cgA..cgE (16 GiB, 25 GiB swap, 2 GiB zswap), observe vs apply, where apply is
# memshiftd reclaim_interface=high (watermarks + memory.high pulses + zswap quotas).
# The oracle hint goes out 30 s before the first peak app starts ramping (--lead 30).
# Needs the zswap WSL kernel. Stages (run in this order; check results in between):
#   run_cg.sh calibrate OUT              single-app calibration of every scenario, then regenerate with it
#   run_cg.sh trial OUT [S]              one observe + one apply round of scenario S (default A)
#   run_cg.sh baseline OUT [SCENS] [N]   N (default 2) observe rounds per scenario: are there OOM events to remove?
#   run_cg.sh formal OUT [SCENS] [N]     N (default 10) observe/apply rounds per scenario, interleaved
set -u
cd "$(dirname "$0")"
STAGE=${1:?stage: calibrate|trial|baseline|formal}
OUT=${2:?output dir}
mkdir -p "$OUT"
NOTIFY=../../../../tools/linux_app_mem/notify_windows.py

# Load memlab_app once outside memlab.slice so its program pages are charged
# there, not to whichever app happens to start first inside the scope.
warm() { ../build/memlab_app --name warm --ttl 0.2 >/dev/null 2>&1 || true; }
notify() { python3 "$NOTIFY" "$1" "$2" >/dev/null 2>&1 || true; }
aggregate() {  # label dirs...
    local label=$1; shift
    python3 aggregate_aligned.py --out "$OUT" --label "$label" "$@" > /dev/null
}

case "$STAGE" in
calibrate)
    mkdir -p "$OUT/calibration"
    warm
    python3 make_cgroup_scenarios.py --out-dir scenarios
    for S in A B C D E; do
        python3 calibrate_apps.py --scenario scenarios/cg$S.json --hold 2 --out "$OUT/calibration/calibration_cg$S.json"
    done
    python3 make_cgroup_scenarios.py --out-dir scenarios --calibration-dir "$OUT/calibration"
    notify "memlab 校准完成" "cgA..cgE 已按校准值重新生成"
    ;;
trial)
    S=${3:-A}
    warm
    python3 run_series.py --scenario scenarios/cg$S.json --out "$OUT/trial" --label trial_cg$S --rounds 1 \
        --variant observe::30 --variant apply::30
    aggregate trial_cg$S "$OUT"/trial/cg${S}_*_1
    notify "memlab 试跑完成" "场景 $S observe + apply 各 1 轮"
    ;;
baseline)
    SCENS=${3:-ABCDE}; N=${4:-2}
    for S in $(echo "$SCENS" | grep -o .); do
        warm
        python3 run_series.py --scenario scenarios/cg$S.json --out "$OUT/baseline" --label baseline_cg$S --rounds "$N" \
            --variant observe::30
    done
    aggregate baseline "$OUT"/baseline/cg*_observe_*
    notify "memlab 基线完成" "observe 每个场景 $N 轮"
    ;;
formal)
    SCENS=${3:-ABCDE}; N=${4:-10}
    for S in $(echo "$SCENS" | grep -o .); do
        warm
        python3 run_series.py --scenario scenarios/cg$S.json --out "$OUT/formal" --label formal_cg$S --rounds "$N" \
            --variant observe::30 --variant apply::30
        aggregate formal "$OUT"/formal/cg*_observe_* "$OUT"/formal/cg*_apply_*
        notify "memlab 场景 $S 完成" "observe/apply 各 $N 轮"
    done
    notify "memlab 正式运行完成" "场景 $SCENS 全部跑完"
    ;;
*) echo "unknown stage $STAGE" >&2; exit 2 ;;
esac
