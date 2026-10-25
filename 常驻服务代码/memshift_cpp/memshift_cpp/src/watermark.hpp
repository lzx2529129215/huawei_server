#pragma once
// Linux-only "high" reclaim mode for the linux-v2 backend (scope mode only).
// Modeled on Harmony's zswapd: reclaim starts when free memory falls below a
// low watermark and continues until it reaches a high watermark; a known
// upcoming load raises the watermarks so reclaim starts early. Reclaim is done
// with memory.high pulses on background apps (memshiftd.cpp, runHigh), most
// expendable first, each keeping a floor. Optionally a per-app
// memory.zswap.max quota sends part of the reclaimed anonymous memory straight
// to disk swap when compressing everything into zswap would not free enough.
// Only pure arithmetic lives here so it can be tested without cgroups.
#include "core.hpp"
#include <deque>
#include <map>
#include <optional>
#include <vector>

namespace ms {
struct Watermarks { Bytes low = 0, high = 0; };

// Normal pair (wm_low, wm_high). With an active peak hint the high watermark
// becomes "peak memory still to come + hint reserve" and the low watermark sits
// hint_gap below it; neither goes below the normal pair.
inline Watermarks watermarks(const Options& o, bool hinted, Bytes remaining) {
    Watermarks w{o.wmLow, o.wmHigh};
    if (!hinted) return w;
    const Bytes high = add(remaining, o.hintReserve);
    if (high > w.high) { w.high = high; w.low = std::max(o.wmLow, sub(high, o.hintGap)); }
    return w;
}

// Peak memory still to come: for every hinted peak app that is alive, how far
// its memory.current is below the size it will reach at its peak. Apps without
// a sample (not started or killed) contribute nothing.
inline Bytes remainingPeak(const std::map<std::string,Bytes>& targets,
                           const std::map<std::string,Bytes>& currents) {
    Bytes r = 0;
    for (const auto& [name, target] : targets) {
        auto it = currents.find(name);
        if (it != currents.end()) r = add(r, sub(target, it->second));
    }
    return r;
}

// One background app as the planner sees it (live memory.stat).
struct DonorView {
    std::string name;
    int priority = 0;           // higher = more expendable = reclaimed and sent to disk first
    Bytes anon = 0;             // reclaimable anon: memory.stat anon - unevictable
    Bytes file = 0;             // memory.stat file
    Bytes reference = 0;        // largest anon + file seen for this app (its daily footprint)
};
// The floor keeps floor_ratio of the app's reclaimable memory (anon + file)
// resident. Pulses stop at the floor; the zswap pool and locked memory are
// not part of it.
inline Bytes residentFloor(const Options& o, const DonorView& d) {
    return amount(o.floorRatio * static_cast<double>(d.reference));
}
inline Bytes residentRoom(const Options& o, const DonorView& d) {
    return sub(add(d.anon, d.file), residentFloor(o, d));
}
// Largest net drop of memory.current one pulse may ask for without pushing the
// app below its floor: compressing anon into zswap frees only (1 - 1/ratio) of
// what it takes, so a net drop of N can consume up to N / (1 - 1/ratio).
inline Bytes pulseLimit(const Options& o, Bytes room, Bytes page) {
    const double r = o.assumedCompressRatio;
    Bytes n = std::min(o.pulse, amount(static_cast<double>(room) * (1.0 - 1.0 / r)));
    return page ? n - n % page : n;
}
inline std::vector<DonorView> byExpendability(std::vector<DonorView> v) {
    std::stable_sort(v.begin(), v.end(), [](const DonorView& a, const DonorView& b) { return a.priority > b.priority; });
    return v;
}

// zswap quota plan, same accounting as the experiment plan (实验设置.md §3):
// every donor gives (1 - floor_ratio) of its file pages (dropped, freed 1:1) and
// (1 - floor_ratio) of its anon pages. Compressed into zswap, anon frees only
// (1 - 1/ratio); sent to disk it frees 1:1, so each byte moved from zswap to
// disk frees 1/ratio more. If compressing everything is not enough, move
// deficit * ratio of anon to disk, most expendable donors first: a donor that
// sends all its reclaimed anon to disk gets quota 0, a partial one gets the
// compressed size of what stays in zswap, the others keep "max".
struct QuotaPlan {
    Bytes need = 0, zswapCapacity = 0, diskCapacity = 0, deficit = 0, diskAnon = 0, unplaced = 0;
    std::map<std::string,Bytes> quota;      // memory.zswap.max (compressed bytes); absent = max
    std::map<std::string,Bytes> diskShare;  // anon planned to go to disk
};
inline QuotaPlan planZswapQuotas(const Options& o, const std::vector<DonorView>& donors, Bytes need, Bytes page) {
    QuotaPlan p; p.need = need;
    const double keep = 1.0 - o.floorRatio, r = o.assumedCompressRatio;
    double z = 0, disk = 0;
    for (const auto& d : donors) {
        z += keep * static_cast<double>(d.file) + keep * static_cast<double>(d.anon) * (1.0 - 1.0 / r);
        disk += keep * static_cast<double>(d.file) + keep * static_cast<double>(d.anon);
    }
    p.zswapCapacity = amount(z); p.diskCapacity = amount(disk);
    if (static_cast<double>(need) <= z) return p;
    p.deficit = amount(static_cast<double>(need) - z);
    double left = (static_cast<double>(need) - z) * r;
    for (const auto& d : byExpendability(donors)) {
        if (left < static_cast<double>(MiB) / 2) break;
        const double reclaimed = keep * static_cast<double>(d.anon);
        if (reclaimed <= 0) continue;
        const double take = std::min(reclaimed, left);
        left -= take;
        Bytes q = take >= reclaimed - static_cast<double>(MiB) / 2 ? 0 : amount((reclaimed - take) / r);
        if (page) q -= q % page;
        p.quota[d.name] = q;
        p.diskShare[d.name] = amount(take);
        p.diskAnon = add(p.diskAnon, amount(take));
    }
    p.unplaced = amount(left);
    return p;
}

// ---- TMO loop, rate top-up and brake (tmo / hint_mode=rate / brake_some_ms) ----

// Stall accumulated by a cumulative PSI counter (total=, microseconds) over
// the last `span` seconds. The counter value at now - span is interpolated
// between the two samples around it; with less history the window starts at
// the oldest sample. A counter that goes backwards (cgroup recreated) restarts.
class PsiWindow {
    std::deque<std::pair<double,Bytes>> h_;
public:
    void reset() { h_.clear(); }
    // Adds a sample; keeps enough history for windows of up to `keep` seconds.
    void add(double t, Bytes total, double keep) {
        if (!h_.empty() && (total < h_.back().second || t < h_.back().first)) h_.clear();
        if (!h_.empty() && t == h_.back().first) h_.back().second = total;
        else h_.emplace_back(t, total);
        while (h_.size() >= 2 && h_[1].first <= t - keep) h_.pop_front();
    }
    // Stall (us) over the `span` seconds before the newest sample.
    Bytes over(double span) const {
        if (h_.size() < 2) return 0;
        const double cut = h_.back().first - span;
        std::size_t k = 0;   // last sample at or before the cut, else the oldest
        while (k + 1 < h_.size() && h_[k + 1].first <= cut) ++k;
        double base = static_cast<double>(h_[k].second);
        if (h_[k].first < cut && k + 1 < h_.size()) {
            const auto& a = h_[k]; const auto& b = h_[k + 1];
            base += (static_cast<double>(b.second) - base) * (cut - a.first) / (b.first - a.first);
        }
        const double d = static_cast<double>(h_.back().second) - base;
        return d > 0 ? amount(d + 0.5) : 0;
    }
};

// Brake: ON when the scope's PSI some stall over the last 1 s reaches onMs;
// OFF once it has stayed below resumeMs for resumeSec without a break.
class Brake {
    PsiWindow w_;
    double onMs_, resumeMs_, resumeSec_;
    bool on_ = false;
    double belowSince_ = -1, windowMs_ = 0;
public:
    Brake(double onMs, double resumeMs, double resumeSec) : onMs_(onMs), resumeMs_(resumeMs), resumeSec_(resumeSec) {}
    explicit Brake(const Options& o) : Brake(o.brakeSomeMs, o.brakeResumeMs, o.brakeResumeSec) {}
    // t: monotonic seconds; someTotalUs: memory.pressure some total. Returns on().
    bool update(double t, Bytes someTotalUs) {
        w_.add(t, someTotalUs, 1.0);
        windowMs_ = static_cast<double>(w_.over(1.0)) / 1000.0;
        if (!on_) {
            if (onMs_ > 0 && windowMs_ >= onMs_) { on_ = true; belowSince_ = -1; }
        } else if (windowMs_ < resumeMs_) {
            if (belowSince_ < 0) belowSince_ = t;
            if (t - belowSince_ >= resumeSec_) { on_ = false; belowSince_ = -1; }
        } else belowSince_ = -1;
        return on_;
    }
    bool on() const { return on_; }
    double windowMs() const { return windowMs_; }
};

// TMO step: net drop asked of one app for one interval. tmo_ratio of its room
// above the floor, scaled by how far its own PSI stall p is below the target;
// at least tmo_min_step. No upper cap here: the PulseWorker splits a job into
// pulses of at most pulse_mb and paces them. 0 once p has reached the target.
inline Bytes tmoStep(const Options& o, Bytes room, double psiUs, Bytes page) {
    if (psiUs >= o.tmoPsiTargetUs) return 0;
    const double scale = 1.0 - std::max(0.0, psiUs) / o.tmoPsiTargetUs;
    Bytes step = amount(o.tmoRatio * static_cast<double>(room) * scale);
    step = std::max(step, o.tmoMinStep);
    return page ? step - step % page : step;
}
// Room worth a TMO / rate pulse: one pulse can take at least tmo_min_step.
inline bool tmoHasRoom(const Options& o, Bytes room, Bytes page) { return pulseLimit(o, room, page) >= o.tmoMinStep; }

// Per-app TMO loop state.
struct TmoApp {
    PsiWindow psi;            // the app's memory.pressure some total, fed every tick
    double next = 0;          // next evaluation (monotonic s); 0 = first chance
    double backoffUntil = 0;  // paused: its stall reached the target
    double dueSince = -1;     // >= 0: due for one pulse since then
    Bytes step = 0;           // net drop for that pulse
    Bytes lastPsi = 0;        // stall (us) over the interval at the last evaluation
};
// One tick for one app (call after psi.add). While eligible, at most once per
// tmo_interval_ms: p = its stall over the last interval; p >= target pauses it
// for tmo_backoff_sec (re-evaluated when the pause ends), otherwise it becomes
// due with tmoStep() unless it is at its floor. Not eligible: not due; it is
// evaluated as soon as it is eligible again.
inline void tmoEvaluate(const Options& o, TmoApp& a, double now, bool eligible, Bytes room, Bytes page) {
    if (!eligible) { a.dueSince = -1; return; }
    if (now < a.next) return;
    const double interval = o.tmoIntervalMs / 1000.0;
    const Bytes p = a.psi.over(interval);
    a.lastPsi = p;
    if (static_cast<double>(p) >= o.tmoPsiTargetUs) {
        a.backoffUntil = now + o.tmoBackoffSec; a.next = a.backoffUntil; a.dueSince = -1; return;
    }
    a.next = now + interval;
    if (!tmoHasRoom(o, room, page)) { a.dueSince = -1; return; }
    a.step = tmoStep(o, room, static_cast<double>(p), page);
    if (a.dueSince < 0) a.dueSince = now;
}
// The job for a due app was accepted: not due, and its next pulse at least one
// interval later.
inline void tmoSubmitted(const Options& o, TmoApp& a, double now) {
    a.dueSince = -1;
    a.next = std::max(a.next, now + o.tmoIntervalMs / 1000.0);
}

// Rate top-up: memory still to free before the hinted peak, i.e. the hinted
// high watermark (remaining peak + hint reserve, at most `total`) minus free.
inline Bytes rateNeed(const Options& o, Bytes remaining, Bytes free, Bytes total) {
    return sub(std::min(add(remaining, o.hintReserve), total), free);
}
// Rate mode: peak memory still to come for the whole hint. A hinted app not seen
// during this hint counts with its full target (it has not started yet); an app
// seen alive counts target - the largest memory.current seen for it (what it
// still has to grow; reclaim does not raise it); an app seen and now gone counts 0.
struct HintApp { Bytes target = 0; bool seen = false, alive = false; Bytes maxCurrent = 0; };
inline Bytes hintRemaining(const std::vector<HintApp>& apps) {
    Bytes r = 0;
    for (const auto& a : apps) {
        if (!a.seen) r = add(r, a.target);
        else if (a.alive) r = add(r, sub(a.target, a.maxCurrent));
    }
    return r;
}
// Rate mode need: projected use at the peak (used + still to come) plus the hint
// reserve must fit in the scope, so need = remaining + reserve - free (no cap at
// the scope size: a whole case's peak can be larger than the scope).
inline Bytes rateNeedHint(const Options& o, Bytes remaining, Bytes free) {
    return sub(add(remaining, o.hintReserve), free);
}
// Seconds the top-up still has: until hint_stop_sec before the peak (<= 0: stop).
inline double rateLeft(const Options& o, double peakAtUnix, double nowUnix) {
    return peakAtUnix - nowUnix - o.hintStopSec;
}
// This job's share: need / left * elapsed, where elapsed is the time since the
// last accepted top-up job (at least one tick, so ticks lost to a busy worker
// are made up); at least tmo_min_step while need > 0, never more than the need.
// 0 when stopped.
inline Bytes rateWant(const Options& o, Bytes need, double left, double elapsedSec, Bytes page) {
    if (!need || !(left > 0)) return 0;
    Bytes w = std::min(amount(static_cast<double>(need) / left * elapsedSec), need);
    w = std::max(w, o.tmoMinStep);
    return page ? w - w % page : w;
}
struct RateDonor {
    std::size_t index = 0;
    int priority = 0;
    Bytes room = 0;           // resident - floor
    bool eligible = false;    // eligible() and its PSI is readable
    bool backoff = false;     // in TMO backoff
    Bytes psiUs = 0;          // its stall over the last TMO interval
};
// Most expendable donor with room (highest priority, ties: largest room),
// skipping apps in TMO backoff or at / above the PSI target.
inline std::optional<std::size_t> pickRateDonor(const Options& o, const std::vector<RateDonor>& v, Bytes page) {
    std::optional<std::size_t> best;
    const RateDonor* b = nullptr;
    for (const auto& d : v) {
        if (!d.eligible || d.backoff || static_cast<double>(d.psiUs) >= o.tmoPsiTargetUs || !tmoHasRoom(o, d.room, page)) continue;
        if (!b || d.priority > b->priority || (d.priority == b->priority && d.room > b->room)) { b = &d; best = d.index; }
    }
    return best;
}
} // namespace ms
