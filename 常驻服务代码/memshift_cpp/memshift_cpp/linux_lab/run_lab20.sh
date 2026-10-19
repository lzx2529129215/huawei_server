#!/usr/bin/env bash
# 5 GiB formal comparison and ablations (first run 2026-10-18/19).
# Usage: run_lab20.sh OUTPUT_DIR
set -u
cd "$(dirname "$0")"
OUT=${1:?usage: run_lab20.sh OUTPUT_DIR}

# lab20 = calibration set cal04 (see results/.../calibration/校准记录.md).
python3 make_scenario.py --name lab20 --swap-max 1792 --peak-mib 1500 --hold 15 --events 5 --event-gap 80 \
    --return-before 60 --return-all 1 --kill-ms 200 --out scenarios/lab20.json

# Formal comparison: interleaved observe/apply, oracle hint 30 s ahead.
python3 run_series.py --scenario scenarios/lab20.json --out "$OUT/formal" --label formal --rounds 3 \
    --variant observe::30 --variant apply::30

# Ablations (apply only; paired with the formal observe rounds by round id).
python3 run_series.py --scenario scenarios/lab20.json --out "$OUT/ablation" --label ablation_lead_error --rounds 2 \
    --variant apply:_L5:5 --variant apply:_L10:10 --variant apply:_L60:60 \
    --variant apply:_em20:30:-0.2 --variant apply:_em50:30:-0.5

# memshiftd defaults for the rate/budget knobs (scale settings kept).
python3 run_series.py --scenario scenarios/lab20.json --out "$OUT/ablation" --label ablation_default_params --rounds 2 \
    --variant apply:_defaults:30 --set alpha=0.2 --set anon_weight=0.5 --set keep_ratio=0.7 \
    --set episode_fraction=0.2 --set batch_mb=64 --set max_per_minute_mb=1024 --set app_per_minute_mb=256 \
    --set cooldown_sec=3

# One table with the formal baseline against every variant.
python3 aggregate.py --out "$OUT" --label all_variants --baseline observe \
    "$OUT"/formal/lab20_observe_[0-9] "$OUT"/formal/lab20_apply_[0-9] "$OUT"/ablation/lab20_apply_*_[0-9]
