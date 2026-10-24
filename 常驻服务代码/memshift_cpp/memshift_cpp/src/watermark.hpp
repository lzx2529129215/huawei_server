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
#include <map>
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
} // namespace ms
