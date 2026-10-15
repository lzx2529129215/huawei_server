"""Create the 90-second fixed-T browser event after donor sampling."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from tools.browser_donor.make_plan import create_plan
PROFILE = json.loads((HERE / "profile.json").read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    samples = manifest.get("samples", [])
    if not samples:
        parser.error("browser manifest has no verified device samples")
    newest = float(samples[-1]["device_boottime_seconds"])
    # The plan writer binds T to the freshest sample; do not use host wall time.
    result = create_plan(args.manifest.resolve(), args.case_id, newest + 90.0,
                         int(PROFILE["load"]["future_increment_mib"]), args.output.resolve())
    result["lead_seconds"] = 90
    result["clock_domain"] = "CLOCK_BOOTTIME"
    result["fixed_device_T"] = newest + 90.0
    args.output.with_suffix(args.output.suffix + ".receipt.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
