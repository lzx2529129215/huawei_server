#!/usr/bin/env python3
"""Offline tools for the acceptance experiment (stage2_spec.md section 5).

qualify  From the qualification run (observe, the whole candidate sequence in order):
           N_120 = first position where the cumulative category total >= 120 and all
                   three types (popup, OOM kill, startup failure) have appeared;
           N_95  = smallest N with P(all three baseline runs of N cases >= 100) >= 0.95,
                   per-case counts ~ negative binomial with the observed per-case mean and
                   variance (Poisson when variance <= mean); Monte Carlo, fixed seed;
           N_dev = first position where the cumulative device-style total >= 110
                   (user decision, 实验设置.md section 0.1; --dev110 0 turns it off);
           N = max(N_120, N_95, N_dev), N' = ceil(1.3 N); writes the freeze file, and the
           computation to <out>.report.json (also when it fails).
         Exit 2 (no freeze) when N_120 or N_dev does not exist in the run or N' exceeds
         the sequence length; exit 3 when the run is not usable (invalid, unconfirmed
         terminal states, wrong mode, missing cases.csv).
compare  Checks every formal run against the freeze (same sequence hash and case ids,
         valid, expected mode/hint; baselines: no unconfirmed terminal states, >= 100
         events with all three types; in apply runs unconfirmed states count as startup
         failures and do not invalidate, 实验设置.md 7.3, so there the runner's valid flag
         decides) and gives the verdict: mean(optimized category_total) <= 0.7 x
         mean(baseline). Secondary: rate ratio with an exact conditional (binomial) 95%
         CI, a case-stratified exact permutation test (run labels swapped as wholes,
         one-sided), the same for device-style totals, totals without startup failures
         and unique; per type, case, app and big op. Writes report.json + report.md.
         Exit 0 when the comparison is valid, 2 when it is not (report written anyway).
extend   Freeze extended to N' (the first N' cases), for a baseline run that misses
         100 events or a type.
Exit 1 means bad input (unreadable freeze, unwritable output). Standard library only.

Usage:
  accept_tools.py qualify --run QDIR --out freeze.json [--n120 120] [--p 0.95] [--target 100] [--sims 20000]
  accept_tools.py compare --freeze F --baseline D1 D2 D3 --optimized D1 D2 D3 [--control D1 ...] --out DIR
  accept_tools.py extend --freeze F --out F2
"""
import argparse
import csv
import hashlib
import itertools
import json
import math
import os
import random
import sys
import time
from pathlib import Path

SEED = 20261024
COUNT_KEYS = ("popups", "kills", "policy_kills", "kernel_oom", "startup_failures", "category_total", "unique",
              "device_style", "shadow", "unconfirmed", "startup_timeouts")
TYPE_KEYS = ("popup", "oom_kill", "startup_failure")
TYPE_CN = {"popup": "低内存弹窗", "oom_kill": "OOM 查杀", "startup_failure": "启动失败"}
EVENT_TYPES = ("popup", "policy_kill", "kernel_oom", "startup_failure")
BIG_OPS = (("shotcut", "S06"), ("chrome", "S06"), ("edge", "S06"))
GROUPS = (("baseline", "基线（observe + 预告）", "observe", True),
          ("optimized", "优化版（apply-TMO + 预告）", "apply", True),
          ("control", "对照（apply-TMO，不发预告）", "apply", False))
METRICS = (("category_total", "分类合计"), ("device_style", "设备口径（启动失败 + 弹窗 + 内核 OOM）"),
           ("no_startup", "不含启动失败（查杀 + 弹窗）"), ("unique", "unique（每个事件只计一次）"))
RULE = ("N_120 = first position of the qualification run (observe, candidate sequence order) where the cumulative "
        "category total >= {n120} and all three types (popup, OOM kill, startup failure) have appeared; "
        "N_95 = smallest N with P(all three baseline runs of N cases >= {target}) >= {p}, per-case counts ~ negative "
        "binomial with the observed per-case mean and variance (Poisson when variance <= mean), Monte Carlo with "
        "{sims} draws and seed {seed}; N_dev = first position where the cumulative device-style total (startup "
        "failures + popups + kernel OOM kills) >= {dev110} (0 = not used); N = max(N_120, N_95, N_dev); "
        "N' = ceil(1.3 N) must not exceed the sequence length. "
        "Formal runs use the first N cases; extended to the first N' cases only when a baseline run misses {target} "
        "events or a type.")


# ---------------------------------------------------------------- reading run directories
def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def read_csv(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error, UnicodeDecodeError):
        return None


def to_int(x, default=0):
    if x is None or x == "":
        return default
    if isinstance(x, bool):
        return int(x)
    try:
        return int(round(float(x)))
    except (TypeError, ValueError):
        s = str(x).strip().lower()
        return 1 if s in ("true", "yes") else 0 if s in ("false", "no") else default


def to_bool(x):
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return x != 0
    return str(x).strip().lower() in ("1", "true", "yes", "on")


def sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()
    except OSError:
        return None


def norm_case(row):
    cid = next((row.get(k) for k in ("id", "case_id", "case") if row.get(k)), None)
    out = {"id": str(cid) if cid is not None else None}
    present = {k for k in COUNT_KEYS if row.get(k) not in (None, "")}
    for k in COUNT_KEYS:
        out[k] = to_int(row.get(k))
    if "kills" not in present and ({"policy_kills", "kernel_oom"} & present):
        out["kills"] = out["policy_kills"] + out["kernel_oom"]
    if "category_total" not in present:
        out["category_total"] = out["kills"] + out["popups"] + out["startup_failures"]
    if "device_style" not in present:
        out["device_style"] = (out["startup_failures"] + out["popups"] + out["kernel_oom"]
                               if "kernel_oom" in present else None)
    if "unique" not in present:
        out["unique"] = None
    out["no_startup"] = out["category_total"] - out["startup_failures"]
    return out


class Run:
    """One run_accept.py output directory; never raises on missing or partial files."""

    def __init__(self, d):
        self.dir = Path(d)
        self.problems = []
        self.summary = read_json(self.dir / "summary.json")
        if not isinstance(self.summary, dict):
            self.summary = None
            self.problems.append("summary.json missing or unreadable")
        s = self.summary or {}
        rows = read_csv(self.dir / "cases.csv")
        if rows is None and isinstance(s.get("cases"), list):
            rows = [r for r in s["cases"] if isinstance(r, dict)]
        self.cases = [norm_case(r) for r in rows or [] if isinstance(r, dict)]
        self.cases = [c for c in self.cases if c["id"]]
        if not self.cases:
            self.problems.append("cases.csv missing or empty")
        self.events = read_csv(self.dir / "events.csv")
        self.scenario = read_json(self.dir / "scenario.json")
        if not isinstance(self.scenario, dict):
            self.scenario = None
        self.mode = s.get("mode")
        self.hint = to_bool(s.get("hint")) if s.get("hint") is not None else None
        self.case_ids = [str(x) for x in s.get("case_ids") or []] or [c["id"] for c in self.cases]
        self.sequence_sha256 = s.get("sequence_sha256") or (self.scenario or {}).get("sequence_sha256")
        self.valid = s.get("valid")
        self.unconfirmed = to_int(s.get("unconfirmed"), default=sum(c["unconfirmed"] for c in self.cases))
        self.totals = self._totals(s.get("totals"))
        tp = s.get("types_present") if isinstance(s.get("types_present"), dict) else {}
        self.types_present = {
            "popup": to_bool(tp["popup"]) if "popup" in tp else (self.totals["popups"] or 0) > 0,
            "oom_kill": to_bool(tp["oom_kill"]) if "oom_kill" in tp else (self.totals["kills"] or 0) > 0,
            "startup_failure": to_bool(tp["startup_failure"]) if "startup_failure" in tp
            else (self.totals["startup_failures"] or 0) > 0}
        self.n_cases = to_int(s.get("n_cases"), default=len(self.cases))
        self.by_case = {c["id"]: c for c in self.cases}

    def _totals(self, t):
        t = t if isinstance(t, dict) else {}
        if not t and not self.cases:           # nothing to count: no value, not zero
            return {k: None for k in COUNT_KEYS + ("no_startup",)}
        out = {}
        for k in COUNT_KEYS:
            if t.get(k) not in (None, ""):
                out[k] = to_int(t[k])
            elif k == "unique" and any(c["unique"] is None for c in self.cases):
                out[k] = None
            else:
                out[k] = sum(c[k] or 0 for c in self.cases)
        if t.get("kills") in (None, "") and (t.get("policy_kills") not in (None, "") or t.get("kernel_oom") not in (None, "")):
            out["kills"] = out["policy_kills"] + out["kernel_oom"]
        out["no_startup"] = out["category_total"] - out["startup_failures"]
        return out

    def metric(self, key, case_ids=None):
        """A metric over the whole run (summary totals), or summed over the given cases."""
        if case_ids is None:
            return self.totals.get(key)
        vals = [self.by_case[c].get(key) for c in case_ids if c in self.by_case]
        if len(vals) != len(case_ids) or any(v is None for v in vals):
            return None
        return sum(vals)

    def label(self):
        return self.dir.name


# ---------------------------------------------------------------- statistics
def poisson(rng, lam):
    """Poisson draw: multiplication method for small lam, PTRS (Hoermann 1993) otherwise."""
    if lam <= 0:
        return 0
    if lam < 10:
        L, k, p = math.exp(-lam), 0, rng.random()
        while p > L:
            k += 1
            p *= rng.random()
        return k
    slam, loglam = math.sqrt(lam), math.log(lam)
    b = 0.931 + 2.53 * slam
    a = -0.059 + 0.02483 * b
    invalpha = 1.1239 + 1.1328 / (b - 3.4)
    vr = 0.9277 - 3.6224 / (b - 2)
    while True:
        u = rng.random() - 0.5
        v = rng.random()
        us = 0.5 - abs(u)
        if us <= 0 or v <= 0:           # measure-zero draws; avoid 1/0 and log(0)
            continue
        k = math.floor((2 * a / us + b) * u + lam + 0.43)
        if us >= 0.07 and v <= vr:
            return k
        if k < 0 or (us < 0.013 and v > us):
            continue
        if (math.log(v) + math.log(invalpha) - math.log(a / (us * us) + b)) <= (-lam + k * loglam - math.lgamma(k + 1)):
            return k


class CountModel:
    """Per-case counts ~ NB(mean, var) (gamma-Poisson), or Poisson when var <= mean.
    A run of N cases is the sum of N iid draws: NB(N r, p) or Poisson(N mean)."""

    def __init__(self, mean, var):
        self.mean, self.var = mean, var
        # A huge r is Poisson in practice (and would lose precision in lgamma differences).
        self.poisson = var <= mean or mean * mean / (var - mean) > 1e6
        self.r = None if self.poisson else mean * mean / (var - mean)

    @property
    def name(self):
        return "poisson" if self.poisson else "negative_binomial"

    def draw_run(self, rng, n):
        lam = n * self.mean if self.poisson else rng.gammavariate(n * self.r, self.mean / self.r)
        return poisson(rng, lam)

    def p_run_ge(self, n, target):
        """Exact P(run total of n cases >= target)."""
        if target <= 0:
            return 1.0
        lam = n * self.mean
        if lam <= 0:
            return 0.0
        cdf = 0.0
        if self.poisson:
            for k in range(target):
                cdf += math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))
        else:
            R, q = n * self.r, self.r / (self.r + self.mean)
            for k in range(target):
                cdf += math.exp(math.lgamma(k + R) - math.lgamma(R) - math.lgamma(k + 1) +
                                R * math.log(q) + k * math.log1p(-q))
        return max(0.0, 1.0 - cdf)


def p_all3_mc(model, n, target, sims, seed=SEED):
    rng = random.Random(seed)       # same seed for every n: smooth in n
    ok = 0
    for _ in range(sims):
        if all(model.draw_run(rng, n) >= target for _ in range(3)):
            ok += 1
    return ok / sims


def find_n95(model, target, p, sims, n_max):
    """Smallest N with the Monte Carlo P(all three runs >= target) >= p; the exact value only picks where to start."""
    if model.mean <= 0:
        return None, {}
    exact = None
    for n in range(1, n_max + 1):
        if model.p_run_ge(n, target) ** 3 >= p:
            exact = n
            break
    if exact is None:
        return None, {"n_exact": None}
    cache = {}

    def mc(n):
        if n not in cache:
            cache[n] = p_all3_mc(model, n, target, sims)
        return cache[n]

    n = max(1, exact - 3)
    if mc(n) >= p:
        while n > 1 and mc(n - 1) >= p:
            n -= 1
    else:
        while n < n_max and mc(n) < p:
            n += 1
        if mc(n) < p:
            return None, {"n_exact": exact, "mc": cache}
    return n, {"n_exact": exact, "p_exact_at_n": model.p_run_ge(n, target) ** 3,
               "p_exact_at_n_exact": model.p_run_ge(exact, target) ** 3, "mc": {str(k): v for k, v in sorted(cache.items())}}


def log_binom_pmf(k, n, p):
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) +
            (k * math.log(p) if k else 0.0) + ((n - k) * math.log1p(-p) if n - k else 0.0))


def binom_cdf(x, n, p):
    return min(1.0, sum(math.exp(log_binom_pmf(k, n, p)) for k in range(0, x + 1)))


def binom_sf(x, n, p):
    """P(X >= x)."""
    return min(1.0, sum(math.exp(log_binom_pmf(k, n, p)) for k in range(x, n + 1)))


def bisect(f, lo, hi, iters=200):
    """Root of a monotone f on (lo, hi), f(lo) and f(hi) of opposite sign."""
    flo = f(lo)
    for _ in range(iters):
        mid = (lo + hi) / 2
        fm = f(mid)
        if (fm < 0) == (flo < 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2


def rate_ratio(x_opt, e_opt, y_base, e_base, alpha=0.05):
    """Rate ratio (optimized / baseline) with the exact conditional CI: given n = x + y, x ~ Bin(n, pi),
    pi = RR e_opt / (RR e_opt + e_base); Clopper-Pearson bounds on pi mapped to RR."""
    out = {"x_optimized": x_opt, "exposure_optimized": e_opt, "y_baseline": y_base, "exposure_baseline": e_base,
           "point": None, "ci95": [None, None]}
    if e_opt <= 0 or e_base <= 0:
        return out
    n = x_opt + y_base
    if y_base > 0:
        out["point"] = (x_opt / e_opt) / (y_base / e_base)
    if n == 0:
        return out
    eps = 1e-12
    pl = 0.0 if x_opt == 0 else bisect(lambda p: binom_sf(x_opt, n, p) - alpha / 2, eps, 1 - eps)
    pu = 1.0 if x_opt == n else bisect(lambda p: binom_cdf(x_opt, n, p) - alpha / 2, eps, 1 - eps)
    k = e_base / e_opt
    out["ci95"] = [pl / (1 - pl) * k, (pu / (1 - pu) * k) if pu < 1 else None]   # None = unbounded
    return out


def perm_test(base, opt):
    """One-sided exact permutation test of mean(base) - mean(opt) > 0, run labels swapped as wholes."""
    vals = list(base) + list(opt)
    nb, no = len(base), len(opt)
    if nb == 0 or no == 0 or any(v is None for v in vals):
        return {"p_one_sided": None, "assignments": 0, "observed_diff": None}
    obs = sum(base) / nb - sum(opt) / no
    hits = total = 0
    for idx in itertools.combinations(range(nb + no), no):
        o = [vals[i] for i in idx]
        b = [vals[i] for i in range(nb + no) if i not in idx]
        total += 1
        if sum(b) / nb - sum(o) / no >= obs - 1e-9:
            hits += 1
    return {"p_one_sided": hits / total, "assignments": total, "observed_diff": obs}


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def sample_var(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def lever_of(scenario):
    rc = ((scenario or {}).get("timeline") or {}).get("ramp_cap_s")
    if scenario is None:
        return "1-s ramp"
    return "1-s ramp" if rc == 1 else ("measured ramps" if not rc else f"{rc}-s ramp")


# ---------------------------------------------------------------- qualify
def cmd_qualify(a):
    run = Run(a.run)
    s = run.summary or {}
    scen_path = Path(a.scenario) if a.scenario else None
    if scen_path is None:
        args_json = read_json(run.dir / "args.json") or {}
        cand = args_json.get("scenario") if isinstance(args_json, dict) else None
        if cand and Path(cand).is_file():
            scen_path = Path(cand)
    if scen_path is None and (run.dir / "scenario.json").is_file():
        scen_path = run.dir / "scenario.json"
    scenario = read_json(scen_path) if scen_path else None
    scenario = scenario if isinstance(scenario, dict) else run.scenario
    seq_ids = [c["id"] for c in (scenario or {}).get("cases", []) if isinstance(c, dict) and c.get("id")]

    problems = list(run.problems)
    if run.mode not in (None, "observe"):
        problems.append(f"mode is {run.mode}, not observe")
    if s.get("case_set") not in (None, "cases"):
        problems.append(f"case_set is {s.get('case_set')}, not the candidate sequence")
    if run.valid is False:
        problems.append("run marked invalid: " + "; ".join(str(x) for x in s.get("invalid_reasons") or []))
    if run.unconfirmed > 0:
        problems.append(f"{run.unconfirmed} unconfirmed terminal states")
    ids = [c["id"] for c in run.cases]
    if seq_ids and ids != seq_ids[:len(ids)]:
        problems.append("cases.csv is not the candidate sequence in order")
    if not seq_ids:
        seq_ids = ids
        problems_note = "scenario not found: the sequence length is the run's case count"
    else:
        problems_note = None
    report = {"run": str(run.dir), "problems": problems, "note": problems_note}
    report_path = Path(a.out).with_suffix(".report.json")     # written on success and on failure
    if problems and not a.allow_invalid:
        print("qualify: the qualification run is not usable:\n  " + "\n  ".join(problems), file=sys.stderr)
        write_atomic(report_path, json.dumps({**report, "result": "unusable"}, ensure_ascii=False, indent=1) + "\n")
        return 3

    L = len(seq_ids)
    totals = [c["category_total"] for c in run.cases]
    cum, cum_dev, n120, ndev, seen, cum_rows = 0, 0, None, None, {k: False for k in TYPE_KEYS}, []
    dev_missing = any(c["device_style"] is None for c in run.cases)
    for j, c in enumerate(run.cases, 1):
        cum += c["category_total"]
        cum_dev += c["device_style"] or 0
        seen["popup"] |= c["popups"] > 0
        seen["oom_kill"] |= c["kills"] > 0
        seen["startup_failure"] |= c["startup_failures"] > 0
        cum_rows.append(cum)
        if n120 is None and cum >= a.n120 and all(seen.values()):
            n120 = j
        # Third term (实验设置.md section 0.1): the device-style total must also reach dev110.
        if ndev is None and cum_dev >= a.dev110:
            ndev = j
    if a.dev110 <= 0:
        ndev = 0
    m = sum(totals) / len(totals) if totals else 0.0
    v = sample_var(totals)
    model = CountModel(m, v)
    n95, detail = find_n95(model, a.target, a.p, a.sims, n_max=max(4 * L, 400))
    parts = (n120, n95, ndev)
    N = max(parts) if all(x is not None for x in parts) else None
    Np = (13 * N + 9) // 10 if N is not None else None           # ceil(1.3 N) in integers
    inputs = {"mean": m, "variance": v, "variance_ddof": 1, "model": model.name, "nb_r": model.r,
              "n_cases": len(totals), "category_total": sum(totals), "device_style_total": cum_dev,
              "types_present": seen, "n120": a.n120, "dev110": a.dev110, "p": a.p, "target": a.target,
              "sims": a.sims, "seed": SEED, "n95_detail": detail}
    result = {**report, "N_120": n120, "N_95": n95, "N_dev": ndev, "N": N, "N_prime": Np, "sequence_length": L,
              "inputs": inputs, "cumulative": cum_rows}
    fail = None
    if n120 is None:
        fail = (f"N_120 does not exist: cumulative total {cum} (need >= {a.n120}) with types "
                f"{', '.join(k for k, x in seen.items() if x) or 'none'} over {len(totals)} cases")
    elif n95 is None:
        fail = f"N_95 not reached up to {max(4 * L, 400)} cases (mean {m:.2f}, variance {v:.2f})"
    elif ndev is None:
        fail = (f"N_dev does not exist: cumulative device-style total {cum_dev} (need >= {a.dev110}) over "
                f"{len(totals)} cases" + (" (cases.csv has no device_style column)" if dev_missing else ""))
    elif Np > L:
        fail = f"N' = {Np} exceeds the sequence length {L} (N_120 {n120}, N_95 {n95}, N_dev {ndev})"
    if fail:
        result["result"] = "failed"
        result["reason"] = fail
        write_atomic(report_path, json.dumps(result, ensure_ascii=False, indent=1) + "\n")
        print(f"qualify: FAILED, no freeze: {fail}", file=sys.stderr)
        return 2
    freeze = {
        "sequence": s.get("sequence") or (scenario or {}).get("sequence"),
        "sequence_sha256": run.sequence_sha256 or (scenario or {}).get("sequence_sha256"),
        "scenario": str(scen_path) if scen_path else None,
        "scenario_sha256": sha256_file(scen_path) if scen_path else None,
        "run_scenario_sha256": sha256_file(run.dir / "scenario.json"),
        "qualification_run": str(run.dir.resolve()),
        "N": N, "N_prime": Np, "N_120": n120, "N_95": n95, "N_dev": ndev, "sequence_length": L,
        "case_ids": seq_ids[:N], "case_ids_prime": seq_ids[:Np],
        "rule": RULE.format(n120=a.n120, target=a.target, p=a.p, sims=a.sims, seed=SEED, dev110=a.dev110),
        "inputs": {k: inputs[k] for k in ("mean", "variance", "variance_ddof", "model", "nb_r", "n_cases",
                                          "category_total", "device_style_total", "types_present", "n120", "dev110",
                                          "p", "target", "sims", "seed")},
        "p_all3_mc_at_N": p_all3_mc(model, N, a.target, a.sims),
        "p_all3_exact_at_N": model.p_run_ge(N, a.target) ** 3,
        "lever": lever_of(scenario),
        "extended": False,
        "created_unix": int(time.time()),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if problems:
        freeze["qualification_problems_overridden"] = problems
    try:
        write_atomic(a.out, json.dumps(freeze, ensure_ascii=False, indent=1) + "\n")
        write_atomic(report_path, json.dumps({**result, "result": "frozen", "freeze": str(a.out)},
                                             ensure_ascii=False, indent=1) + "\n")
    except OSError as e:
        print(f"qualify: cannot write {a.out}: {e}", file=sys.stderr)
        return 1
    print(f"qualify: N_120 {n120}, N_95 {n95} ({model.name}, mean {m:.2f}, variance {v:.2f}), N_dev {ndev}, "
          f"N {N}, N' {Np} of {L}; froze the first {N} cases -> {a.out}")
    return 0


# ---------------------------------------------------------------- extend
def cmd_extend(a):
    fr = read_json(a.freeze)
    if not isinstance(fr, dict) or not fr.get("case_ids"):
        print(f"extend: cannot read freeze {a.freeze}", file=sys.stderr)
        return 1
    if fr.get("extended"):
        print("extend: the freeze is already extended to N'", file=sys.stderr)
        return 2
    ids = fr.get("case_ids_prime")
    if not ids or len(ids) != fr.get("N_prime"):
        print("extend: the freeze has no case_ids_prime list of length N'", file=sys.stderr)
        return 1
    new = dict(fr)
    new.update({"N": fr["N_prime"], "case_ids": list(ids), "extended": True, "N_original": fr["N"],
                "case_ids_original": fr["case_ids"], "extended_unix": int(time.time()),
                "extended_reason": a.reason or "a baseline run missed the event target or a type"})
    try:
        write_atomic(a.out, json.dumps(new, ensure_ascii=False, indent=1) + "\n")
    except OSError as e:
        print(f"extend: cannot write {a.out}: {e}", file=sys.stderr)
        return 1
    print(f"extend: N {fr['N']} -> {new['N']} cases -> {a.out}")
    return 0


# ---------------------------------------------------------------- compare
def run_checks(run, freeze, group, mode, hint, target):
    """Problems that make a run unusable for the comparison (empty list = ok)."""
    p = list(run.problems)
    if run.summary is None:
        return p
    if run.sequence_sha256 != freeze.get("sequence_sha256"):
        p.append(f"sequence_sha256 {str(run.sequence_sha256)[:12]} differs from the freeze")
    if run.case_ids != list(freeze.get("case_ids") or []):
        p.append(f"case ids differ from the freeze ({len(run.case_ids)} vs {len(freeze.get('case_ids') or [])} cases)")
    if run.valid is not True:
        reasons = "; ".join(str(x) for x in (run.summary.get("invalid_reasons") or [])) or "valid flag missing"
        p.append(f"run invalid: {reasons}")
    # Unconfirmed states invalidate observe runs; in apply runs they count as startup failures (实验设置.md 7.3).
    if run.unconfirmed > 0 and mode == "observe":
        p.append(f"{run.unconfirmed} unconfirmed terminal states")
    if run.mode is not None and run.mode != mode:
        p.append(f"mode {run.mode}, expected {mode}")
    if run.hint is not None and run.hint != hint:
        p.append(f"hint {'on' if run.hint else 'off'}, expected {'on' if hint else 'off'}")
    if group == "baseline":
        if (run.totals["category_total"] or 0) < target:
            p.append(f"baseline category total {run.totals['category_total']} < {target}")
        miss = [TYPE_CN[k] for k in TYPE_KEYS if not run.types_present.get(k)]
        if miss:
            p.append("baseline misses event types: " + "、".join(miss))
    return p


def compare_metric(base_runs, opt_runs, key, case_ids=None):
    b = [r.metric(key, case_ids) for r in base_runs]
    o = [r.metric(key, case_ids) for r in opt_runs]
    mb, mo = mean(b), mean(o)
    ratio = mo / mb if (mb not in (None, 0) and mo is not None) else None
    n_cases = len(case_ids) if case_ids is not None else None
    e_b = sum(n_cases if n_cases is not None else r.n_cases for r in base_runs)
    e_o = sum(n_cases if n_cases is not None else r.n_cases for r in opt_runs)
    ok = all(x is not None for x in b + o) and b and o
    rr = rate_ratio(sum(o), e_o, sum(b), e_b) if ok else None
    return {"baseline": b, "other": o, "baseline_mean": mb, "other_mean": mo, "ratio": ratio,
            "reduction": (1 - ratio) if ratio is not None else None, "rate_ratio": rr,
            "permutation": perm_test(b, o) if ok else None}


def events_by_app(run):
    """{app: {type: count}} from events.csv (shadow rows ignored); None when the file is missing."""
    if run.events is None:
        return None
    out = {}
    for e in run.events:
        t = (e.get("type") or "").strip()
        if t not in EVENT_TYPES:
            continue
        app = (e.get("app") or "").strip() or "?"
        out.setdefault(app, {k: 0 for k in EVENT_TYPES})[t] += 1
    return out


def fmt(x, nd=1, pct=False):
    if x is None:
        return "—"
    if pct:
        return f"{x * 100:.{nd}f}%"
    if isinstance(x, int):
        return f"{x:,}"
    return f"{x:,.{nd}f}"


def fmt_ci(rr):
    if not rr or rr.get("point") is None and rr.get("ci95", [None])[0] is None:
        return "—"
    lo, hi = rr["ci95"]
    return f"{fmt(rr['point'], 3)}（{fmt(lo, 3)}–{fmt(hi, 3) if hi is not None else '∞'}）"


def md_table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def cmd_compare(a):
    freeze = read_json(a.freeze)
    if not isinstance(freeze, dict) or not freeze.get("case_ids"):
        print(f"compare: cannot read freeze {a.freeze}", file=sys.stderr)
        return 1
    ids = [str(x) for x in freeze["case_ids"]]
    dirs = {"baseline": a.baseline or [], "optimized": a.optimized or [], "control": a.control or []}
    runs = {g: [Run(d) for d in dirs[g]] for g in dirs}
    checks = {}
    for g, _cn, mode, hint in GROUPS:
        checks[g] = [run_checks(r, freeze, g, mode, hint, a.target) for r in runs[g]]
    reasons = []
    for g in ("baseline", "optimized"):
        if len(runs[g]) != a.runs:
            reasons.append(f"{g}: {len(runs[g])} runs given, {a.runs} required")
        for r, p in zip(runs[g], checks[g]):
            reasons += [f"{g} {r.label()}: {x}" for x in p]
    valid = not reasons
    main = {key: compare_metric(runs["baseline"], runs["optimized"], key) for key, _ in METRICS}
    ct = main["category_total"]
    success = bool(valid and ct["baseline_mean"] and ct["other_mean"] is not None
                   and ct["other_mean"] <= a.threshold * ct["baseline_mean"])
    verdict = ("成立" if success else "不成立") if valid else "对比不成立（检查没过）"
    if a.reference:                  # e.g. the unfiltered R20 cases: numbers only, no acceptance verdict
        verdict = "只作参考，不作验收判定" + ("" if valid else "（检查没过，见第 1 节）")
        success = False

    # Control: compared with the baseline on the cases both ran (the freeze may have been extended after it ran).
    control = None
    usable_ctrl = [r for r, p in zip(runs["control"], checks["control"])
                   if not [x for x in p if not x.startswith("case ids differ")]]
    if runs["control"]:
        common = [c for c in ids if all(c in r.by_case for r in usable_ctrl)] if usable_ctrl else []
        same = bool(usable_ctrl) and all(r.case_ids == ids for r in usable_ctrl)
        control = {"runs": [r.label() for r in runs["control"]], "usable": [r.label() for r in usable_ctrl],
                   "case_ids": "freeze" if same else common, "n_cases": len(ids) if same else len(common),
                   "metrics": {key: compare_metric(runs["baseline"], usable_ctrl, key, None if same else common)
                               for key, _ in METRICS} if usable_ctrl and (same or common) else None}

    per_type = {}
    for g in dirs:
        per_type[g] = {k: mean([r.totals.get(k) for r in runs[g]]) for k in COUNT_KEYS}
    per_case = []
    for cid in ids:
        row = {"id": cid}
        for g in dirs:
            vals = [r.by_case[cid]["category_total"] for r in runs[g] if cid in r.by_case]
            row[g] = mean(vals) if vals else None
        per_case.append(row)
    per_app, apps_seen = {}, set()
    for g in dirs:
        ev = [events_by_app(r) for r in runs[g]]
        got = [e for e in ev if e is not None]
        acc = {}
        for e in got:
            for app, d in e.items():
                apps_seen.add(app)
                t = acc.setdefault(app, {k: 0 for k in EVENT_TYPES})
                for k in EVENT_TYPES:
                    t[k] += d[k]
        per_app[g] = {"runs_with_events": len(got), "runs": len(ev),
                      "mean": {app: {k: v / len(got) for k, v in d.items()} for app, d in acc.items()} if got else {}}
    scen = next((r.scenario for g in dirs for r in runs[g] if r.scenario), None) or {}
    orders = {c["id"]: c.get("order", []) for c in scen.get("cases", []) if isinstance(c, dict) and c.get("id")}
    big_ops = []
    for app, code in BIG_OPS:
        info = (scen.get("apps") or {}).get(app, {}) if isinstance(scen.get("apps"), dict) else {}
        cases_with = [c for c in ids if app in orders.get(c, [])]
        row = {"app": app, "op_code": info.get("op_code", code), "op": info.get("op"), "display": info.get("display", app),
               "cases": len(cases_with)}
        for g in dirs:
            m_app = per_app[g]["mean"].get(app, {k: 0.0 for k in EVENT_TYPES}) if per_app[g]["runs_with_events"] else None
            tot = [r.metric("category_total", cases_with) for r in runs[g]] if cases_with else []
            row[g] = {"app_events": m_app, "cases_category_total_mean": mean(tot) if tot else None}
        big_ops.append(row)

    report = {
        "freeze": str(a.freeze), "sequence": freeze.get("sequence"), "sequence_sha256": freeze.get("sequence_sha256"),
        "N": freeze.get("N"), "extended": freeze.get("extended", False), "target": a.target, "threshold": a.threshold,
        "valid": valid, "invalid_reasons": reasons, "success": success, "verdict": verdict,
        "reference": bool(a.reference),
        "runs": {g: [{"dir": str(r.dir), "label": r.label(), "problems": p, "totals": r.totals,
                      "types_present": r.types_present, "n_cases": r.n_cases, "valid": r.valid,
                      "unconfirmed": r.unconfirmed}
                     for r, p in zip(runs[g], checks[g])] for g in dirs},
        "main": main, "control": control, "per_type": per_type, "per_case": per_case, "per_app": per_app,
        "big_ops": big_ops, "created_unix": int(time.time()),
    }
    md = render_md(report, runs, checks, ids, orders, scen, apps_seen)
    out = Path(a.out)
    if out.suffix in (".json", ".md"):
        jpath, mpath = out.with_suffix(".json"), out.with_suffix(".md")
    else:
        jpath, mpath = out / "report.json", out / "report.md"
    try:
        write_atomic(jpath, json.dumps(report, ensure_ascii=False, indent=1) + "\n")
        write_atomic(mpath, md)
    except OSError as e:
        print(f"compare: cannot write the report: {e}", file=sys.stderr)
        return 1
    print(f"compare: {verdict}; baseline mean {fmt(ct['baseline_mean'])}, optimized mean {fmt(ct['other_mean'])}, "
          f"ratio {fmt(ct['ratio'], 3)} -> {jpath}, {mpath}")
    for r in reasons:
        print(f"  not valid: {r}")
    return 0 if valid else 2


def render_md(rep, runs, checks, ids, orders, scen, apps_seen):
    ct = rep["main"]["category_total"]
    L = []
    if rep.get("reference"):
        L.append("# 参考用例对比：内存错峰调度（结构 B，只作参考）")
        L.append("")
        L.append(f"- **{rep['verdict']}**：这组用例不按条件 4′ 过滤，基线、优化版各 {len(runs['baseline'])} / "
                 f"{len(runs['optimized'])} 次，只报数字。")
    else:
        L.append("# 验收对比报告：内存错峰调度（结构 B）")
        L.append("")
        L.append(f"- **判定：{rep['verdict']}**。规则：优化版 {len(runs['optimized'])} 次的分类合计均值 ≤ "
                 f"{rep['threshold']} × 基线 {len(runs['baseline'])} 次的均值；基线每次 ≥ {rep['target']} 次且三类都出现；"
                 "每次运行的序列 hash、用例和冻结文件一致，运行有效；基线没有未确认终态。")
    L.append(f"- 冻结文件：`{rep['freeze']}`；序列 {rep['sequence']}（sha256 `{str(rep['sequence_sha256'])[:16]}…`），"
             f"每次 {rep['N']} 个用例{'（已加长到 N′）' if rep['extended'] else ''}。")
    L.append(f"- 基线均值 {fmt(ct['baseline_mean'])}，优化版均值 {fmt(ct['other_mean'])}，比值 {fmt(ct['ratio'], 3)}，"
             f"降幅 {fmt(ct['reduction'], 1, pct=True)}。")
    L.append("- 数字都是这几次运行的实验读数（Linux 上 memlab.slice 的模拟，不是 16 GB 真机）。")
    if rep["invalid_reasons"]:
        L.append("- **对比不成立的原因**：")
        L += [f"  - {x}" for x in rep["invalid_reasons"]]
    L.append("")
    L.append("## 1. 每次运行的检查")
    L.append("")
    rows = []
    for g, cn, _m, _h in GROUPS:
        for r, p in zip(runs[g], checks[g]):
            t = r.totals
            rows.append([cn.split("（")[0], r.label(), r.n_cases, "是" if r.valid is True else "否",
                         r.unconfirmed, fmt(t["category_total"]),
                         "/".join("有" if r.types_present.get(k) else "无" for k in TYPE_KEYS),
                         "通过" if not p else "；".join(p)])
    L.append(md_table(["组", "运行", "用例数", "有效", "未确认", "分类合计", "弹窗/查杀/启动失败", "检查"], rows))
    L.append("")
    unc = [(r.label(), r.unconfirmed) for g in ("optimized", "control") for r in runs[g] if r.unconfirmed > 0]
    if unc:
        L.append("- apply 运行里的未确认终态（" + "，".join(f"{n} {k} 个" for n, k in unc) +
                 "）按实验设置第 0.1 节记成启动失败、不作废；是否已计入分类合计以运行器的 summary 为准。")
        L.append("")
    L.append("## 2. 主结果和次要结果（优化版 ÷ 基线）")
    L.append("")
    L.append(md_table(["指标", "基线每次", "优化版每次", "基线均值", "优化版均值", "比值", "降幅", "率比（95% CI，条件精确法）",
                       "置换检验 p（单侧）"],
                      [[cn, " / ".join(fmt(x, 0) for x in m["baseline"]), " / ".join(fmt(x, 0) for x in m["other"]),
                        fmt(m["baseline_mean"]), fmt(m["other_mean"]), fmt(m["ratio"], 3),
                        fmt(m["reduction"], 1, pct=True), fmt_ci(m["rate_ratio"]),
                        fmt((m["permutation"] or {}).get("p_one_sided"), 3)]
                       for (key, cn), m in ((k, rep["main"][k[0]]) for k in METRICS)]))
    L.append("")
    L.append("- 置换检验：各次运行跑的是同一份冻结用例（按用例分层），把运行标签整体交换，"
             f"{(ct['permutation'] or {}).get('assignments', 0)} 种分法里统计量 ≥ 观察值的比例；3 对 3 次时最小 p = 0.05。")
    L.append("- 率比：两组的事件总数按条件二项分布（总数给定）求 Clopper–Pearson 区间，再换成率比。")
    L.append("")
    L.append("## 3. 对照（apply-TMO，不发预告）对基线")
    L.append("")
    c = rep["control"]
    if not c:
        L.append("没有对照运行。")
    elif not c["metrics"]:
        L.append(f"对照运行 {', '.join(c['runs'])} 都不能用（见第 1 节）。")
    else:
        scope = "冻结序列的全部用例" if c["case_ids"] == "freeze" else f"和基线共有的前 {c['n_cases']} 个用例"
        L.append(f"用到的对照运行：{', '.join(c['usable'])}；比较范围：{scope}。")
        L.append("")
        L.append(md_table(["指标", "基线均值", "对照均值", "比值", "降幅", "率比（95% CI）", "置换检验 p（单侧）"],
                          [[cn, fmt(m["baseline_mean"]), fmt(m["other_mean"]), fmt(m["ratio"], 3),
                            fmt(m["reduction"], 1, pct=True), fmt_ci(m["rate_ratio"]),
                            fmt((m["permutation"] or {}).get("p_one_sided"), 3)]
                           for (key, cn), m in ((k, c["metrics"][k[0]]) for k in METRICS)]))
    L.append("")
    L.append("## 4. 按类型（每次运行的均值）")
    L.append("")
    names = [("popups", "低内存弹窗"), ("kills", "OOM 查杀（合计）"), ("policy_kills", "其中规则杀进程"),
             ("kernel_oom", "其中内核 OOM"), ("startup_failures", "启动失败"), ("category_total", "分类合计"),
             ("device_style", "设备口径"), ("unique", "unique"), ("shadow", "影子计数（200 ms，不算事件）"),
             ("startup_timeouts", "启动超时（不计入）"), ("unconfirmed", "未确认终态")]
    L.append(md_table(["类型", "基线", "优化版", "对照"],
                      [[cn] + [fmt(rep["per_type"][g].get(k)) if runs[g] else "—" for g in ("baseline", "optimized", "control")]
                       for k, cn in names]))
    L.append("")
    L.append("## 5. 按大操作（每次运行的均值）")
    L.append("")
    rows = []
    for b in rep["big_ops"]:
        cells = [f"{b['display']} {b['op_code']}", b["cases"]]
        for g in ("baseline", "optimized", "control"):
            d = b[g]
            ev = d["app_events"]
            own = "—" if ev is None or not runs[g] else (
                f"{fmt(ev['policy_kill'] + ev['kernel_oom'])} / {fmt(ev['startup_failure'])}")
            cells.append(f"{own}；{fmt(d['cases_category_total_mean'])}" if runs[g] else "—")
        rows.append(cells)
    L.append(md_table(["大操作", "含它的用例数", "基线（它被杀 / 它启动失败；含它的用例分类合计）", "优化版（同左）",
                       "对照（同左）"], rows))
    L.append("")
    L.append("## 6. 按应用（events.csv，每次运行的均值；弹窗记在当时的前台应用上）")
    L.append("")
    rows = []
    for app in sorted(apps_seen):
        cells = [app]
        for g in ("baseline", "optimized", "control"):
            pa = rep["per_app"][g]
            if not pa["runs_with_events"]:
                cells.append("—")
                continue
            d = pa["mean"].get(app, {k: 0.0 for k in EVENT_TYPES})
            cells.append(f"{fmt(d['popup'])} / {fmt(d['policy_kill'] + d['kernel_oom'])} / {fmt(d['startup_failure'])}")
        rows.append(cells)
    L.append(md_table(["应用", "基线（弹窗 / 查杀 / 启动失败）", "优化版（同左）", "对照（同左）"], rows) if rows else "没有事件。")
    L.append("")
    L.append("## 7. 按用例（分类合计，每次运行的均值）")
    L.append("")
    rows = []
    for pc in rep["per_case"]:
        order = orders.get(pc["id"], [])
        rows.append([pc["id"], len(order) or "—", order[-1] if order else "—",
                     fmt(pc["baseline"]), fmt(pc["optimized"]), fmt(pc["control"])])
    L.append(md_table(["用例", "应用数", "前台应用", "基线", "优化版", "对照"], rows))
    L.append("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="acceptance experiment: qualify / compare / extend")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("qualify", help="N from the qualification run -> freeze.json")
    q.add_argument("--run", required=True)
    q.add_argument("--out", required=True)
    q.add_argument("--n120", type=int, default=120)
    q.add_argument("--dev110", type=int, default=110,
                   help="device-style cumulative threshold for the third N term (实验设置.md 0.1; 0 = off)")
    q.add_argument("--p", type=float, default=0.95)
    q.add_argument("--target", type=int, default=100)
    q.add_argument("--sims", type=int, default=20000)
    q.add_argument("--scenario", default=None, help="scenario file to hash (default: the run's args.json scenario)")
    q.add_argument("--allow-invalid", action="store_true", help="freeze even if the run is invalid (not for formal use)")
    c = sub.add_parser("compare", help="formal runs -> report.json / report.md")
    c.add_argument("--freeze", required=True)
    c.add_argument("--baseline", nargs="+", default=[])
    c.add_argument("--optimized", nargs="+", default=[])
    c.add_argument("--control", nargs="*", default=[])
    c.add_argument("--out", required=True, help="directory (or a .json/.md path prefix)")
    c.add_argument("--target", type=int, default=100)
    c.add_argument("--threshold", type=float, default=0.7)
    c.add_argument("--runs", type=int, default=3, help="required baseline and optimized runs")
    c.add_argument("--reference", action="store_true", help="reference set (R20): report numbers, no verdict")
    e = sub.add_parser("extend", help="freeze extended to N'")
    e.add_argument("--freeze", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--reason", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "qualify":
        if a.sims < 100 or not (0 < a.p < 1) or a.target < 1:
            print("qualify: bad --sims/--p/--target", file=sys.stderr)
            return 1
        return cmd_qualify(a)
    if a.cmd == "compare":
        return cmd_compare(a)
    return cmd_extend(a)


if __name__ == "__main__":
    sys.exit(main())
