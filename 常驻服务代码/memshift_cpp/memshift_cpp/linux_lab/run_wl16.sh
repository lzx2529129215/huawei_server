#!/usr/bin/env bash
# Whitelist (20 apps) concurrent-launch formal comparison (first run 2026-10-19).
# Needs the zswap WSL kernel. Usage: run_wl16.sh OUTPUT_DIR
set -u
cd "$(dirname "$0")"
OUT=${1:?usage: run_wl16.sh OUTPUT_DIR}

# Single-app calibration: every case is checked on measured daily/peak values.
[ -f "$OUT/calibration_apps.json" ] || python3 make_whitelist.py --out scenarios/wl16_nominal.json
[ -f "$OUT/calibration_apps.json" ] || python3 calibrate_apps.py --scenario scenarios/wl16_nominal.json \
    --out "$OUT/calibration_apps.json"

# wl16 = calibration set w3: 6 apps clicked 5.5 s apart per case, 4 cases per round.
python3 make_whitelist.py --name wl16 --click-gap 5.5 --calibration "$OUT/calibration_apps.json" \
    --out scenarios/wl16.json

# Baseline gives ~12 events per round, so 9 rounds reach the 100-event threshold.
python3 run_series.py --scenario scenarios/wl16.json --out "$OUT/formal" --label formal --rounds 9 \
    --variant observe::30 --variant apply::30
