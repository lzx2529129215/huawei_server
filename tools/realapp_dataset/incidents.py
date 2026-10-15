"""Persistent evidence-based blocks survive process exit and device reboot."""
from pathlib import Path

from .core import load

REGISTRY = Path(__file__).resolve().parents[2] / "config/realapp_dataset/incidents.json"


def pressure_blockers(target, mib, registry=REGISTRY):
    document = load(registry)
    return [
        {"incident": item["id"], "reason": item["reason"], "evidence": item["evidence"]}
        for item in document["incidents"]
        if item["device"] == target and item["status"] == "unresolved"
        and mib > item["maximum_diagnostic_mib"]
    ]


def require_pressure_ready(target, mib):
    blockers = pressure_blockers(target, mib)
    if blockers:
        raise RuntimeError("unresolved pressure incident: " + "; ".join(b["incident"] for b in blockers))
