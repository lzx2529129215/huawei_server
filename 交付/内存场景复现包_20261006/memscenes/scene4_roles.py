"""Explicit fixed background roles for the opt-in Scene4 peak experiment."""
import re


def memory_roles(cfg: dict, bundles: list[str], peak_mib: int, file_mib: int) -> dict:
    donors = list(cfg.get("fixed_background_donors", []))
    if donors:
        if (cfg.get("execution_profile") != "legacy_peak_transition" or
                cfg.get("oracle_profile") != "peak_transition_os_snapshot"):
            raise ValueError("fixed background roles require the explicit Scene4 peak profile")
        if (len(bundles) != 8 or len(set(bundles)) != 8 or len(donors) != 2 or
                len(set(donors)) != 2 or not set(donors).issubset(bundles) or
                any(not re.fullmatch(r"io\.memscene\.scene4\.workload\.a[1-8]", b)
                    for b in bundles)):
            raise ValueError("fixed roles require two owned donors and six distinct owned peak targets")
    targets = [b for b in bundles if b not in donors]
    per_app = {}
    for bundle in bundles:
        is_donor = bundle in donors
        daily = int(cfg["fixed_background_donor_mib"] if is_donor else cfg["daily_mib"])
        peak = daily if is_donor else int(peak_mib)
        if not 0 <= int(file_mib) <= daily <= peak:
            raise ValueError("each role requires file <= daily <= peak")
        per_app[bundle] = {"role": "background_donor" if is_donor else "peak_target",
                           "daily_mib": daily, "peak_mib": peak, "file_mib": int(file_mib)}
    return {"enabled": bool(donors), "donors": donors, "peak_targets": targets,
            "reclaim_candidates": donors if donors else list(bundles), "per_app": per_app,
            "daily_total_mib": sum(x["daily_mib"] for x in per_app.values()),
            "peak_total_mib": sum(x["peak_mib"] for x in per_app.values()),
            "incremental_bytes": sum(x["peak_mib"] - x["daily_mib"]
                                     for x in per_app.values()) * 1024**2}
