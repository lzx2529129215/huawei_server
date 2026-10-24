#!/usr/bin/env bash
# Whitelist (wl16) ablations, mirroring lab16: apply only, 2 rounds each, paired
# with formal observe rounds 1-2 (same scenario file and binaries).
# Needs the zswap WSL kernel. Usage: run_wl16_ablation.sh RESULT_DIR (the wl16 folder holding formal/)
set -u
cd "$(dirname "$0")"
OUT=${1:?usage: run_wl16_ablation.sh RESULT_DIR}
SC=scenarios/wl16.json

# Prediction quality: lead time, under/over-predicted size, and no hints at all.
python3 run_series.py --scenario $SC --out "$OUT/ablation" --label ablation_prediction --rounds 2 \
    --variant apply:_L5:5 --variant apply:_L10:10 \
    --variant apply:_em20:30:-0.2 --variant apply:_em50:30:-0.5 --variant apply:_ep20:30:0.2 \
    --variant apply:_nohint

# Reclaim parameters: memshiftd defaults instead of the relaxed lab values.
python3 run_series.py --scenario $SC --out "$OUT/ablation" --label ablation_default_params --rounds 2 \
    --variant apply:_defaults:30 --set alpha=0.2 --set anon_weight=0.5 --set keep_ratio=0.7 \
    --set episode_fraction=0.2 --set batch_mb=64 --set max_per_minute_mb=1024 --set app_per_minute_mb=256 \
    --set cooldown_sec=3

# Hint cap: the memshiftd default boost_cap_fraction=0.5 instead of 1.0.
python3 run_series.py --scenario $SC --out "$OUT/ablation" --label ablation_cap05 --rounds 2 \
    --variant apply:_cap05:30 --set boost_cap_fraction=0.5

python3 aggregate.py --out "$OUT" --label ablation_all --baseline observe \
    "$OUT"/formal/wl16_observe_[12] "$OUT"/formal/wl16_apply_[12] "$OUT"/ablation/wl16_apply_*_[12]
