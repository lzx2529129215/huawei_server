#!/usr/bin/env bash
# 16 GiB formal comparison and ablations (first run 2026-10-19).
# Needs the zswap WSL kernel (see results/linux_oom_lab_16g_20261019/calibration/校准记录.md).
# Usage: run_lab16.sh OUTPUT_DIR
set -u
cd "$(dirname "$0")"
OUT=${1:?usage: run_lab16.sh OUTPUT_DIR}

# lab16 = calibration set c16c: 16 GiB scope, 25 GiB swap budget, 4 GiB zswap,
# background memory ~3x compressible, five 2x3600 MiB peaks.
python3 make_scenario.py --name lab16 --memory-max 16384 --swap-max 25600 --zswap-max 4096 --compress-ratio 3 \
    --scale 3.2 --peak-mib 1125 --hold 15 --events 5 --event-gap 80 --return-before 60 --return-all 1 \
    --kill-ms 200 --out scenarios/lab16.json

# Formal comparison: 7 interleaved rounds each (baseline >= 100 events), hint 30 s ahead.
python3 run_series.py --scenario scenarios/lab16.json --out "$OUT/formal" --label formal --rounds 7 \
    --variant observe::30 --variant apply::30

# Ablations (apply only, 2 rounds each; paired with formal observe rounds 1-2).
python3 run_series.py --scenario scenarios/lab16.json --out "$OUT/ablation" --label ablation_lead_error --rounds 2 \
    --variant apply:_L5:5 --variant apply:_L10:10 \
    --variant apply:_em20:30:-0.2 --variant apply:_em50:30:-0.5 --variant apply:_ep20:30:0.2

python3 run_series.py --scenario scenarios/lab16.json --out "$OUT/ablation" --label ablation_default_params --rounds 2 \
    --variant apply:_defaults:30 --set alpha=0.2 --set anon_weight=0.5 --set keep_ratio=0.7 \
    --set episode_fraction=0.2 --set batch_mb=64 --set max_per_minute_mb=1024 --set app_per_minute_mb=256 \
    --set cooldown_sec=3

python3 aggregate.py --out "$OUT" --label all_variants --baseline observe \
    "$OUT"/formal/lab16_observe_[0-9] "$OUT"/formal/lab16_apply_[0-9] "$OUT"/ablation/lab16_apply_*_[0-9]
