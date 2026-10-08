#!/usr/bin/env python3
"""Reconcile a saved Scene 4 result after SHA-bound frame review is complete.

This is a thin CLI over the runner's canonical reconciliation logic. It never
edits the source result, manifests, reviews, or captured images.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path,
                        help="original Scene 4 result JSON containing frame manifest paths")
    args = parser.parse_args(argv)
    try:
        from memscenes.runner import reconcile_scene4_frame_reviews

        output = reconcile_scene4_frame_reviews(args.result)
    except Exception as exc:
        print(f"review reconciliation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
