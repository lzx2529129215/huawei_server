from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

MIB = 1024 ** 2
GIB = 1024 ** 3
SCHEMA = "realapp-dd-dataset-v1"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(MIB), b""):
            h.update(block)
    return h.hexdigest()


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def memory(raw: str) -> dict[str, int]:
    return {m[1]: int(m[2]) * (1024 if m[3] else 1)
            for m in re.finditer(r"(?m)^(\w+):\s+(\d+)(?:\s+(kB))?", raw)}


def fill_mib(total: int) -> int:
    result = (total - 8 * GIB) // MIB
    if result <= 0 or result * MIB >= 32 * GIB:
        raise ValueError("MemTotal cannot support this 8 GiB reference / 32 GiB tmpfs profile")
    return result


def active_mount(fdinfo: str, mountinfo: str) -> dict:
    match = re.search(r"(?m)^mnt_id:\s*(\d+)$", fdinfo)
    if not match:
        raise ValueError("actual /dev/shm mount ID unavailable")
    matches = [line for line in mountinfo.splitlines() if line.split()[0] == match[1]]
    if len(matches) != 1:
        raise ValueError("ambiguous active mount")
    left, right = matches[0].split(" - ", 1)
    a, b = left.split(), right.split()
    if a[4] != "/dev/shm" or b[0] != "tmpfs":
        raise ValueError("/dev/shm does not resolve to the expected tmpfs mount")
    size = next((x.split("=", 1)[1] for x in b[2].split(",") if x.startswith("size=")), None)
    if size is None or not re.fullmatch(r"\d+[kKmMgG]?", size):
        raise ValueError("original tmpfs size is unknown")
    return {"mount_id": int(a[0]), "device": a[2], "mount_options": a[5],
            "super_options": b[2], "size": size, "raw": matches[0]}


def schedule(suite: dict) -> list[dict]:
    result = []
    for index, scene in enumerate(suite["scenes"]):
        phases = ("observe", "apply") if index % 2 == 0 else ("apply", "observe")
        for phase in phases:
            result.append({"scene": scene["id"], "pair_id": scene["id"] + "_pair01",
                           "phase": phase, "order": len(result) + 1})
    return result


class Gate:
    """Reference policy mirrored by the device sampler; one attempt per run."""
    def __init__(self):
        self.consecutive = 0
        self.attempted = False
        self.last_time = None

    def update(self, stamp: float, available: int | None, armed: bool) -> bool:
        contiguous = self.last_time is not None and 0.20 <= stamp - self.last_time <= 0.50
        self.last_time = stamp
        if not armed or available is None or available * 10 >= 8 * GIB:
            self.consecutive = 0
            return False
        self.consecutive = self.consecutive + 1 if contiguous else 1
        if self.consecutive >= 2 and not self.attempted:
            self.attempted = True
            return True
        return False


def segment_states(samples: list[dict]) -> list[dict]:
    segments = []
    for s in samples:
        state = s.get("gate", "").strip()
        state = state if state in ("enable", "readonly", "disable") else "unknown"
        if not segments or segments[-1]["state"] != state or s["time"] - segments[-1]["end"] > 0.75:
            segments.append({"start": s["time"], "end": s["time"], "state": state})
        else:
            segments[-1]["end"] = s["time"]
    return segments


def validate_recipe(recipe: dict, app: dict, root: Path) -> list[str]:
    errors = []
    if not app.get("bundle") or not app.get("ability") or not app.get("module"):
        errors.append("unresolved_application_entry")
    if app.get("form") not in ("native", "container", "web"):
        errors.append("unverified_application_form")
    for field in ("setup", "actions", "postcondition"):
        if not recipe.get(field):
            errors.append("missing_" + field)
    # At least one actual non-launch operation, not a blank home-page launch.
    if not any(a.get("type") in ("click", "text", "key", "swipe") for a in recipe.get("actions", [])):
        errors.append("no_real_operation")
    for material in recipe.get("materials", []):
        path = (root / material["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            errors.append("material_missing:" + material["path"])
        elif digest(path) != material.get("sha256"):
            errors.append("material_hash_mismatch:" + material["path"])
    if not recipe.get("materials") and not recipe.get("online_fixture"):
        errors.append("missing_test_material_or_online_fixture")
    return errors


def training_windows(samples: list[dict], events: list[dict], *, event_coverage: bool,
                     history=30, horizons=(5, 10, 30), max_gap=1.5) -> list[dict]:
    """Only actual complete histories; future values live exclusively under labels."""
    samples = sorted(samples, key=lambda r: r["time"])
    output = []
    for current in samples:
        t = current["time"]
        past = [r for r in samples if t - history <= r["time"] <= t]
        if not past or past[0]["time"] > t - history + 0.5:
            continue
        if any(b["time"] - a["time"] > max_gap for a, b in zip(past, past[1:])):
            continue
        if any(r.get("available") is None for r in past):
            continue
        record = {"time": t, "history": past, "labels": {}}
        for horizon in horizons:
            future = [r for r in samples if t < r["time"] <= t + horizon]
            complete = (bool(future) and samples[-1]["time"]>=t+horizon and abs(future[-1]["time"] - (t + horizon)) <= 0.5
                        and all(b["time"] - a["time"] <= max_gap
                                for a, b in zip([current] + future, future))
                        and all(r.get("available") is not None for r in future))
            if not complete:
                record["labels"][str(horizon)] = {"valid": False, "reason": "future_incomplete"}
                continue
            kills = [e for e in events if e["category"] == "oom_kill" and t < e["time"] <= t + horizon]
            record["labels"][str(horizon)] = {
                "valid": True, "available": future[-1]["available"],
                "endpoint_sample_time":future[-1]["time"],
                "minimum_available": min(r["available"] for r in future),
                "oom": bool(kills) if event_coverage or kills else None,
                "oom_coverage_complete": event_coverage,
                "time_to_oom": min((e["time"] - t for e in kills), default=None),
                "contains_post_oom_release": bool(kills),
                "gate_changed": len({r.get("gate") for r in [current] + future}) > 1}
        output.append(record)
    return output
