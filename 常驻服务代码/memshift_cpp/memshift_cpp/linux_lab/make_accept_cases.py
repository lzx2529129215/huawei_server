#!/usr/bin/env python3
"""Scenario JSON for the acceptance experiment, structure B (stage2_spec.md section 3).

Reads the stage-1 candidate and tuning sequences and the per-app run profiles
(results/linux_oom_lab_accept_20261024/data/), copies the cases (id, click
order, rotation, the stage-1 numbers) and the apps used by any case, and fills
each app's synthetic-run fields from its measured profile:
  anon/clean/locked   measured daily split
  peak_mib            the peak op's increment; ramp_s / peak_curve its measured ramp
  hot_anon/hot_file   background WSS (read every hot_period s)
  peak_hot_anon       S02 WSS anon (read while the peak runs)
The stage-1 hashes are kept (and re-verified against the case lists).

Acceptance checks (also used by run_accept.py, section 4), M = 16384 MiB:
  (1) every app's peak <= M
  (2) daily sum of all apps used <= M, and each case's daily sum <= M
  (3) each case's same-moment total (peaks aligned: sum of its apps' peaks) >= 1.2 M
With --calibration (run_accept.py --calibrate output: {app: {daily_mib, peak_mib,
startup_ms}}) they use cal_daily_mib / cal_peak_mib; otherwise nominal values
(synthetic footprint anon+clean+locked+4 MiB, peak = daily + increment).
One line per case is printed. The scenario is always written; exit 3 when any
check fails (or an app lacks calibration values), 1 on bad input.

Usage (from linux_lab/):
  python3 make_accept_cases.py --sequence DATA/sequence_S130.json --tuning DATA/sequence_S130T.json \
      --profiles DATA/profiles.json --out scenarios/acceptS130.json [--calibration cal.json]
  (--sequence DATA/sequence_R20.json makes the unfiltered reference scenario; --reference adds R20 as
  reference_cases instead.)
"""
import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent
REPO = LAB.parents[3]
DATA = REPO / "results" / "linux_oom_lab_accept_20261024" / "data"

M_MIB = 16384
SCOPE = {"memory_max_mib": M_MIB, "swap_max_mib": 25600, "zswap_max_mib": 2048}
# Case gap and hint lead are 15 s (user decision 2026-10-24). Lever: 1-s ramps (step 3).
TIMELINE = {"click_gap_s": 3, "first_ramp_after_last_click_s": 10, "barrier_wait_max_s": 20, "hold_s": 15,
            "exit_after_release_s": 2, "gap_before_next_case_s": 15, "hint_lead_s": 15, "ramp_cap_s": 1,
            "release_timeout_s": 30, "fill_threads": 4,
            "precase": {"current_max_mib": 128, "zswap_max_mib": 16, "swap_max_mib": 64, "wait_max_s": 30}}
# Stage-1 timeline keys that must match (they are covered by the stage-1 hash).
SAME_AS_STAGE1 = {"click_gap_s": "click_gap_s", "first_ramp_after_last_click_s": "first_ramp_after_last_click_s",
                  "barrier_wait_max_s": "barrier_wait_max_s", "hold_s": "hold_s",
                  "exit_after_release_s": "exit_after_release_s", "gap_before_next_case_s": "gap_before_next_case_s",
                  "hint_lead_s": "hint_lead_before_first_click_s"}
ADJ = {"background": 900, "front": 100}
POLICY_KILL = {"window_s": 1.0, "cooldown_s": 1.0,
               "levels": [{"some_ms": 70, "min_adj": 800}, {"full_ms": 700, "min_adj": 0}]}
SHADOW_SOME_MS = 200
POPUP = {"low_mib": 50, "rearm_mib": 150, "rearm_s": 3.0}
# tmo_ratio 0.25: the user's decision (实验设置.md section 0.1, after stage2_spec.md listed 0.02): 25% of the
# reclaimable part every 2 s for both apply variants (the 1-s-ramp case leaves about 15 s per app).
# pulse_mb 16, brake_some_ms 20, brake_pulse_ms 5 (bug fix 2026-10-24 20:05, 进度.md): in the first apply check
# 64-MiB anon pulses took 56-195 ms, throttled the donor app while it touched its memory and stalled the slice
# 72-95 ms in a 1-s window before the peak, so the 70 ms rule killed waiting apps; the 30 ms brake reacted only
# after the window was over it. Smaller pulses plus a stall check before every pulse keep apply's own stall
# below the kill rule, as the brake was meant to.
MEMSHIFTD = {"reclaim_interface": "high", "interval_ms": 250, "startup_grace_sec": 5, "wm_low_mb": 512,
             "wm_high_mb": 1024, "hint_reserve_mb": 512, "hint_gap_mb": 256, "floor_ratio": 0.2,
             "assumed_compress_ratio": 3.0, "pulse_mb": 16, "pulse_rate_mb_s": 256, "pulse_timeout_ms": 5000,
             "backoff_sec": 10, "max_boost_mb": 24576, "min_batch_mb": 2, "tmo": "true", "tmo_interval_ms": 2000,
             "tmo_ratio": 0.25, "tmo_min_step_mb": 2, "tmo_psi_target_us": 2000, "tmo_backoff_sec": 10,
             "hint_mode": "rate", "hint_stop_sec": 5, "brake_some_ms": 20, "brake_pulse_ms": 5, "brake_resume_ms": 15,
             "brake_resume_sec": 2}
COMPRESS_RATIO = 3
HOT_PERIOD_S = 120
PEAK_HOT_ANON_PERIOD_S = 10
BINARY_MIB = 4            # memlab_app's own footprint, as run_round.daily_mib
CASE_NUMBERS = ("same_moment_mib", "literal_peak_sum_mib", "D_mib", "margin_min_mib")


class InputError(Exception):
    pass


def canon_hash(obj):
    """Same canonical-JSON sha256 as the stage-1 analysis/acc_model.py."""
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def hash_view(seq):
    """What the stage-1 sha256 covers (analysis/sequence.py hash_view): the case list and the fixed timeline,
    not the rule wording or model estimates."""
    keep = ("app", "click_s", "op_code", "increment_mib", "ramp_s", "adj")
    return {"name": seq["name"], "timeline": seq["timeline"],
            "cases": [{"id": c["id"], "group": c["group"], "rotation": c["rotation"], "order": c["order"],
                       "apps": [{k: a[k] for k in keep} for a in c["apps"]]} for c in seq["cases"]]}


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InputError(f"cannot read {what} {path}: {e}")


def as_int(x, what):
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise InputError(f"{what}: not a number: {x!r}")
    if not math.isfinite(v):
        raise InputError(f"{what}: not finite: {x!r}")
    return int(round(v))


def load_sequence(path, what):
    seq = load_json(path, what)
    if not isinstance(seq, dict) or not isinstance(seq.get("cases"), list) or not seq["cases"]:
        raise InputError(f"{what} {path}: no cases")
    want = seq.get("sha256")
    if not want:
        raise InputError(f"{what} {path}: no sha256")
    try:
        got = canon_hash(hash_view(seq))
    except (KeyError, TypeError) as e:
        raise InputError(f"{what} {path}: case list incomplete for the hash ({e})")
    # Files written before the hash_view change hashed the whole object.
    if got != want and canon_hash({k: v for k, v in seq.items() if k != "sha256"}) != want:
        raise InputError(f"{what} {path}: sha256 mismatch (file says {want}, the case list gives {got})")
    return seq


def nominal_daily(a):
    return a["anon_mib"] + a["clean_mib"] + a["locked_mib"] + BINARY_MIB


def nominal_peak(a):
    return nominal_daily(a) + a["peak_mib"]


def app_entry(name, p):
    if not isinstance(p, dict):
        raise InputError(f"profile of {name} missing")
    anon = as_int(p.get("anon_mib"), f"{name}.anon_mib")
    clean = as_int(p.get("clean_mib"), f"{name}.clean_mib")
    locked = as_int(p.get("locked_mib"), f"{name}.locked_mib")
    peak = as_int(p.get("peak_mib"), f"{name}.peak_mib")
    if min(anon, clean, locked) < 0 or peak <= 0:
        raise InputError(f"{name}: bad daily split or increment ({anon}, {clean}, {locked}, {peak})")
    ramp = float(p.get("ramp_s") or 0)
    if ramp <= 0:
        raise InputError(f"{name}: no ramp_s")
    curve = p.get("peak_curve") or None
    if curve is not None:
        curve = [[float(t), float(v)] for t, v in curve]
    return {
        "anon_mib": anon, "clean_mib": clean, "locked_mib": locked, "compress_ratio": COMPRESS_RATIO,
        "peak_mib": peak, "peak_compress_ratio": COMPRESS_RATIO, "ramp_s": ramp, "peak_curve": curve,
        # Hot sets cannot exceed the region they read.
        "hot_anon_mib": min(anon, as_int(p.get("hot_anon_mib") or 0, f"{name}.hot_anon_mib")),
        "hot_file_mib": min(clean, as_int(p.get("hot_file_mib") or 0, f"{name}.hot_file_mib")),
        "hot_period": HOT_PERIOD_S,
        "peak_hot_anon_mib": min(anon, as_int(p.get("peak_hot_anon_mib") or 0, f"{name}.peak_hot_anon_mib")),
        "peak_hot_anon_period": PEAK_HOT_ANON_PERIOD_S,
        "op": str(p.get("op") or ""), "display": str(p.get("display") or name),
        "op_code": p.get("op_code"), "ramp_source": p.get("ramp_source"),
        "daily_total_mib": p.get("daily_total_mib"),
        "cal_daily_mib": None, "cal_peak_mib": None,
    }


def copy_case(c, profiles, what):
    cid = c.get("id")
    order = c.get("order")
    if not cid or not isinstance(order, list) or not order:
        raise InputError(f"{what}: case without id or order: {str(c)[:120]}")
    if len(set(order)) != len(order):
        raise InputError(f"{what} {cid}: an app is clicked twice")
    for n in order:
        if n not in profiles:
            raise InputError(f"{what} {cid}: app {n} has no profile")
    out = {"id": cid, "order": list(order), "rotation": c.get("rotation", 0),
           "group": sorted(c.get("group") or order)}
    for k in CASE_NUMBERS:
        if k in c:
            out[k] = c[k]
    # Stage-1 per-app numbers must match the profiles (same measured data).
    for a in c.get("apps", []):
        p = profiles.get(a.get("app"), {})
        dm = a.get("daily_mib")
        if dm and dm != [p.get("daily_total_mib"), p.get("anon_mib"), p.get("clean_mib"), p.get("locked_mib")]:
            raise InputError(f"{what} {cid}: {a.get('app')} daily split differs from profiles.json")
        if a.get("increment_mib") is not None and float(a["increment_mib"]) != float(p.get("peak_mib", -1)):
            raise InputError(f"{what} {cid}: {a.get('app')} increment differs from profiles.json")
    return out


def load_calibration(path):
    cal = load_json(path, "calibration")
    if isinstance(cal, dict) and isinstance(cal.get("apps"), dict):
        cal = cal["apps"]
    if not isinstance(cal, dict):
        raise InputError(f"calibration {path}: expected {{app: {{daily_mib, peak_mib}}}}")
    out = {}
    for n, v in cal.items():
        if not isinstance(v, dict):
            continue
        d, p = v.get("daily_mib"), v.get("peak_mib")
        try:
            d = None if d is None else int(round(float(d)))
            p = None if p is None else int(round(float(p)))
        except (TypeError, ValueError):
            d = p = None
        out[n] = (d, p)
    return out


def acceptance_checks(apps, case_sets, source):
    """Section-4 checks per case. case_sets: [(set name, [case, ...])]."""
    def daily(n):
        a = apps[n]
        return a["cal_daily_mib"] if source == "calibrated" else nominal_daily(a)

    def peak(n):
        a = apps[n]
        return a["cal_peak_mib"] if source == "calibrated" else nominal_peak(a)

    missing = sorted(n for n in apps if daily(n) is None or peak(n) is None)
    used = sorted(apps)
    known = [n for n in used if n not in missing]
    all_daily = sum(daily(n) for n in known)
    rows, failed = [], []
    for set_name, cases in case_sets:
        for c in cases:
            names = c["order"]
            gap = [n for n in names if n in missing]
            if gap:
                row = {"set": set_name, "id": c["id"], "n_apps": len(names), "missing": gap,
                       "c1": False, "c2": False, "c3": False}
            else:
                d = sum(daily(n) for n in names)
                s = sum(peak(n) for n in names)
                row = {"set": set_name, "id": c["id"], "n_apps": len(names), "daily_sum_mib": d,
                       "max_single_peak_mib": max(peak(n) for n in names), "same_moment_mib": s,
                       "same_moment_ratio": round(s / M_MIB, 4),
                       "c1": max(peak(n) for n in names) <= M_MIB,
                       "c2": d <= M_MIB,                       # the global reading is checked below
                       "c3": s >= 1.2 * M_MIB and len(names) >= 3}
            row["ok"] = row["c1"] and row["c2"] and row["c3"]
            rows.append(row)
            if not row["ok"]:
                failed.append(c["id"])
    single = all(peak(n) <= M_MIB for n in known) and not missing
    daily_ok = all_daily <= M_MIB and not missing and all(r["c2"] for r in rows)
    same_ok = all(r["c3"] for r in rows)
    summary = {
        "source": source, "memory_max_mib": M_MIB, "threshold_same_moment_mib": math.ceil(1.2 * M_MIB),
        "apps_used": len(used), "all_apps_daily_sum_mib": all_daily, "missing_calibration": missing,
        "single_peak_le_M": single, "daily_sum_le_M": daily_ok, "same_moment_ge_1_2M": same_ok,
        "all_pass": not failed and single and daily_ok and same_ok,
        "failed_cases": failed,
    }
    return summary, rows


def print_checks(summary, rows, apps):
    src = summary["source"]
    how = ("calibrated: cal_daily_mib / cal_peak_mib" if src == "calibrated" else
           f"nominal: daily = anon+clean+locked+{BINARY_MIB} MiB, peak = daily + increment")
    print(f"acceptance checks ({how}; M = {M_MIB} MiB, condition 3 needs >= {summary['threshold_same_moment_mib']} MiB)")
    if summary["missing_calibration"]:
        print(f"  MISSING calibration values for: {', '.join(summary['missing_calibration'])}")
    print(f"  all {summary['apps_used']} apps used: daily sum {summary['all_apps_daily_sum_mib']} MiB "
          f"<= {M_MIB}: {'ok' if summary['all_apps_daily_sum_mib'] <= M_MIB else 'FAIL'}")
    print(f"  {'case':<10} {'apps':>4} {'daily_sum':>9} {'max_peak':>8} {'same_moment':>11} {'ratio':>7}  (1)  (2)  (3)")
    for r in rows:
        if "missing" in r:
            print(f"  {r['id']:<10} {r['n_apps']:>4}  no calibration for {', '.join(r['missing'])}  FAIL")
            continue
        flag = lambda b: " ok " if b else "FAIL"
        print(f"  {r['id']:<10} {r['n_apps']:>4} {r['daily_sum_mib']:>9} {r['max_single_peak_mib']:>8} "
              f"{r['same_moment_mib']:>11} {r['same_moment_ratio']:>7.1%} {flag(r['c1'])} {flag(r['c2'])} {flag(r['c3'])}")
    by_set = {}
    for r in rows:
        t = by_set.setdefault(r["set"], [0, 0, None, None])
        t[0] += 1
        t[1] += r["ok"]
        if "same_moment_mib" in r:
            t[2] = r["same_moment_mib"] if t[2] is None else min(t[2], r["same_moment_mib"])
            t[3] = r["same_moment_mib"] if t[3] is None else max(t[3], r["same_moment_mib"])
    for s, (n, ok, lo, hi) in by_set.items():
        print(f"  {s}: {ok}/{n} cases pass; same-moment total {lo}-{hi} MiB")
    big = max(apps, key=lambda n: nominal_peak(apps[n]) if src != "calibrated" else (apps[n]["cal_peak_mib"] or 0))
    print(f"  largest single peak: {big}; condition 1 {'ok' if summary['single_peak_le_M'] else 'FAIL'}, "
          f"2 {'ok' if summary['daily_sum_le_M'] else 'FAIL'}, 3 {'ok' if summary['same_moment_ge_1_2M'] else 'FAIL'}"
          f" -> {'ALL PASS' if summary['all_pass'] else 'CHECKS FAILED'}")


def rel_to_repo(p):
    p = Path(p).resolve()
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)


def build(args):
    seq = load_sequence(args.sequence, "sequence")
    tun = load_sequence(args.tuning, "tuning sequence")
    ref = load_sequence(args.reference, "reference sequence") if args.reference else None
    profiles = load_json(args.profiles, "profiles")
    if not isinstance(profiles, dict):
        raise InputError(f"profiles {args.profiles}: expected an object keyed by app")
    # The scenario timeline must be the one the stage-1 hash covers.
    for what, s in (("sequence", seq), ("tuning", tun)) + ((("reference", ref),) if ref else ()):
        st1 = s.get("timeline") or {}
        for ours, theirs in SAME_AS_STAGE1.items():
            if theirs in st1 and float(st1[theirs]) != float(TIMELINE[ours]):
                raise InputError(f"{what} timeline {theirs}={st1[theirs]} differs from the scenario's {ours}={TIMELINE[ours]}")
        for k, ours in (("adj_background", ADJ["background"]), ("adj_front", ADJ["front"])):
            if k in st1 and int(st1[k]) != ours:
                raise InputError(f"{what} timeline {k}={st1[k]} differs from {ours}")
    cases = [copy_case(c, profiles, "sequence") for c in seq["cases"]]
    tcases = [copy_case(c, profiles, "tuning") for c in tun["cases"]]
    rcases = [copy_case(c, profiles, "reference") for c in ref["cases"]] if ref else []
    ids = [c["id"] for c in cases + tcases + rcases]
    if len(set(ids)) != len(ids):
        raise InputError("duplicate case ids across the sequences")
    shared = {tuple(c["order"]) for c in cases} & {tuple(c["order"]) for c in tcases}
    if shared:
        raise InputError(f"tuning cases repeat candidate cases: {sorted(shared)[:3]}")
    used = sorted({n for c in cases + tcases + rcases for n in c["order"]})
    apps = {n: app_entry(n, profiles[n]) for n in used}
    source = "nominal"
    if args.calibration:
        cal = load_calibration(args.calibration)
        for n in used:
            d, p = cal.get(n, (None, None))
            apps[n]["cal_daily_mib"], apps[n]["cal_peak_mib"] = d, p
        source = "calibrated"
    timeline = json.loads(json.dumps(TIMELINE))
    if args.ramp_cap is not None:
        rc = args.ramp_cap
        timeline["ramp_cap_s"] = (int(rc) if rc == int(rc) else rc) if rc > 0 else None
    lever = "1-s ramp" if timeline["ramp_cap_s"] == 1 else (
        "measured ramps" if not timeline["ramp_cap_s"] else f"{timeline['ramp_cap_s']}-s ramp")
    sets = [("candidate", cases), ("tuning", tcases)] + ([("reference", rcases)] if rcases else [])
    summary, rows = acceptance_checks(apps, sets, source)
    if source == "calibrated":
        for c in cases + tcases + rcases:
            names = c["order"]
            ok = all(apps[n]["cal_peak_mib"] is not None and apps[n]["cal_daily_mib"] is not None for n in names)
            c["cal_same_moment_mib"] = sum(apps[n]["cal_peak_mib"] for n in names) if ok else None
            c["cal_daily_sum_mib"] = sum(apps[n]["cal_daily_mib"] for n in names) if ok else None
    sc = {
        "name": args.name or f"accept{seq.get('name', 'S')}",
        "sequence": seq.get("name"), "sequence_sha256": seq["sha256"],
        "tuning": tun.get("name"), "tuning_sha256": tun["sha256"],
        "cap": seq.get("cap"), "lever": lever,
        "scope": dict(SCOPE), "timeline": timeline, "adj": dict(ADJ),
        "policy_kill": json.loads(json.dumps(POLICY_KILL)), "shadow_some_ms": SHADOW_SOME_MS,
        "popup": dict(POPUP), "memshiftd": dict(MEMSHIFTD),
        "apps": apps, "cases": cases, "tuning_cases": tcases,
        "acceptance_checks": summary,
        "sources": {"sequence": rel_to_repo(args.sequence), "tuning": rel_to_repo(args.tuning),
                    "profiles": rel_to_repo(args.profiles),
                    "calibration": rel_to_repo(args.calibration) if args.calibration else None},
        "notes": ["gap_before_next_case_s and hint_lead_s are 15 s (user decision 2026-10-24; first stage-1 draft had 30 s)",
                  "lever: 1-s ramps (step 3) chosen by the user before any data; ramp_s / peak_curve kept for reference",
                  "memshiftd tmo_ratio 0.25 for both apply variants (user decision, 实验设置.md section 0.1)",
                  "app numbers are measured (WSL Linux apps); cal_* are synthetic-program calibration readings"],
        "generated_by": "linux_lab/make_accept_cases.py",
    }
    if ref:
        # Optional unfiltered reference cases (stage-1 rule 6, R20); only used when a runner selects them.
        sc["reference"], sc["reference_sha256"], sc["reference_cases"] = ref.get("name"), ref["sha256"], rcases
        sc["sources"]["reference"] = rel_to_repo(args.reference)
    return sc, summary, rows, apps


def write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sequence", default=str(DATA / "sequence_S130.json"))
    ap.add_argument("--tuning", default=str(DATA / "sequence_S130T.json"))
    ap.add_argument("--profiles", default=str(DATA / "profiles.json"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--calibration", default=None, help="run_accept.py --calibrate output")
    ap.add_argument("--reference", default=None,
                    help="optional unfiltered reference sequence (DATA/sequence_R20.json) -> reference_cases")
    ap.add_argument("--name", default=None, help="scenario name (default accept<sequence name>)")
    ap.add_argument("--ramp-cap", type=float, default=None,
                    help="override timeline.ramp_cap_s (default 1; 0 = measured ramps)")
    ap.add_argument("--quiet", action="store_true", help="print only the summary line of the checks")
    args = ap.parse_args(argv)
    try:
        sc, summary, rows, apps = build(args)
    except InputError as e:
        print(f"make_accept_cases: {e}", file=sys.stderr)
        return 1
    try:
        write_atomic(args.out, json.dumps(sc, ensure_ascii=False, indent=1) + "\n")
    except OSError as e:
        print(f"make_accept_cases: cannot write {args.out}: {e}", file=sys.stderr)
        return 1
    if args.quiet:
        print(f"acceptance checks ({summary['source']}): {'ALL PASS' if summary['all_pass'] else 'CHECKS FAILED'}"
              f"; failed cases: {summary['failed_cases'] or 'none'}")
    else:
        print_checks(summary, rows, apps)
    print(f"wrote {args.out}: {len(sc['cases'])} cases + {len(sc['tuning_cases'])} tuning cases, {len(apps)} apps, "
          f"sequence {sc['sequence']} sha256 {sc['sequence_sha256'][:12]}..., lever {sc['lever']}")
    return 0 if summary["all_pass"] else 3


if __name__ == "__main__":
    sys.exit(main())
