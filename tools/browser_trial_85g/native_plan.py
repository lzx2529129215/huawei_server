"""Serializer for the local 3-target retry controller plan (not the service donor plan)."""
from __future__ import annotations

import math
import re

BUNDLES = tuple(f"io.memscene.scene4.workload.a{i}" for i in (14, 15, 16))
FINGERPRINT = ("20261013", "3", "21504", "1536", "8704", "256", "90", "10",
               "30", "330", "420", "1200", "4")


def serialize(case_id: str, boot_id: str, fixed_T: float,
              identities: dict[str, dict]) -> str:
    if not re.fullmatch(r"s4_[0-9a-f]{12}_c0001", case_id):
        raise ValueError("case id must be s4_<12 lowercase hex>_c0001")
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", boot_id):
        raise ValueError("invalid device boot id")
    if isinstance(fixed_T, bool) or not isinstance(fixed_T, (int, float)) or not math.isfinite(fixed_T) or fixed_T <= 0:
        raise ValueError("fixed device T must be positive and finite")
    if set(identities) != set(BUNDLES):
        raise ValueError("the pressure plan must bind exactly targets a14-a16")
    meta = "|".join((case_id, boot_id, f"{fixed_T:.9f}", *FINGERPRINT))
    rows = ["device_retry_plan_browser_donor_3target_85g_v1", meta]
    for bundle in BUNDLES:
        identity = identities[bundle]
        values = (identity.get("uid"), identity.get("pid"), identity.get("start_ticks"))
        if any(isinstance(v, bool) or not str(v).isdecimal() or int(v) <= 0 for v in values):
            raise ValueError(f"target identity invalid: {bundle}")
        rows.append("|".join((bundle, *(str(int(v)) for v in values), "peak_target")))
    return "\n".join(rows) + "\n"


def validate_serialized(raw: str) -> dict:
    """Reject cross-workload or malformed controller plans before device upload."""
    rows = raw.splitlines()
    expected_header = "device_retry_plan_browser_donor_3target_85g_v1"
    if len(rows) != 5 or rows[0] != expected_header:
        raise ValueError("8.5GiB controller plan schema/row count mismatch")
    fields = rows[1].split("|")
    if len(fields) != 16 or tuple(fields[3:]) != FINGERPRINT:
        raise ValueError("8.5GiB controller workload fingerprint mismatch")
    for index, bundle in enumerate(BUNDLES):
        values = rows[index + 2].split("|")
        if (len(values) != 5 or values[0] != bundle or values[4] != "peak_target" or
                any(not item.isdecimal() or int(item) <= 0 for item in values[1:4])):
            raise ValueError("8.5GiB controller target identity/schema mismatch")
    return {"case_id": fields[0], "boot_id": fields[1], "T": float(fields[2]),
            "fingerprint": tuple(fields[3:])}
