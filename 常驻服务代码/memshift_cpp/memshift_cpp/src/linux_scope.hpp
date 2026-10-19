#pragma once
// Linux-only scope mode for the linux-v2 backend.
// One cgroup v2 subtree stands in for the machine: its memory.max is the
// "physical memory", and the system sample (total/free/available/swap/PSI) is
// derived from that cgroup instead of host /proc/meminfo. Without
// scope_cgroup the backend keeps its host-wide behavior. Harmony backends
// never read this file.
#include "io.hpp"

namespace ms {
struct ScopeText {
    std::string max, current, stat, swapMax, swapCurrent, memPressure, ioPressure;
};

// Pure conversion so the arithmetic can be tested without a cgroup mount.
inline System scopeSystemFromText(const ScopeText& t, const System& host) {
    System s; s.time = host.time;
    const auto maxText = trim(t.max);
    if (maxText == "max") throw std::runtime_error("scope_cgroup needs a finite memory.max");
    s.total = uintValue(maxText);
    if (!s.total) throw std::runtime_error("scope_cgroup memory.max is zero");
    const Bytes current = uintValue(trim(t.current));
    const auto v = counters(t.stat);
    s.free = sub(s.total, current);
    const Bytes file = required(v,"file"), shmem = get(v,"shmem");
    const Bytes writeback = required(v,"file_writeback");
    s.dirty = required(v,"file_dirty");
    s.cached = file;
    s.writebackBytes = writeback;
    s.anonPagesBytes = required(v,"anon");
    s.shmemBytes = shmem;
    // Same spirit as MemAvailable: free memory plus clean page cache and half
    // of reclaimable slab. Dirty/writeback pages are not instantly available.
    const Bytes clean = sub(file, add(add(shmem, s.dirty), writeback));
    s.available = std::min(s.total, add(add(s.free, clean), get(v,"slab_reclaimable")/2));
    // Swap room is the tighter of the host swap and the scope's own budget.
    s.swapTotal = host.swapTotal; s.swapFree = host.swapFree;
    const auto swapMax = trim(t.swapMax);
    if (!swapMax.empty() && swapMax != "max") {
        const Bytes cap = uintValue(swapMax);
        const auto used = trim(t.swapCurrent);
        s.swapTotal = std::min(s.swapTotal, cap);
        s.swapFree = std::min(s.swapFree, sub(cap, used.empty() ? 0 : uintValue(used)));
    }
    s.zswap = get(v,"zswap"); s.zswapped = get(v,"zswapped");
    s.pswpin = v.count("pswpin") ? v.at("pswpin") : host.pswpin;
    s.pswpout = v.count("pswpout") ? v.at("pswpout") : host.pswpout;
    if (!t.memPressure.empty()) s.mem = parsePSI(t.memPressure);
    if (!t.ioPressure.empty()) s.io = parsePSI(t.ioPressure);
    return s;
}

inline System scopeSample(int root, const std::string& scope, const std::string& proc) {
    const System host = systemSample(proc);
    FD dir = openBeneath(root, scope);
    ScopeText t;
    t.max = readAt(dir.get(),"memory.max");
    t.current = readAt(dir.get(),"memory.current");
    t.stat = readAt(dir.get(),"memory.stat");
    t.swapMax = maybeAt(dir.get(),"memory.swap.max").value_or("");
    t.swapCurrent = maybeAt(dir.get(),"memory.swap.current").value_or("");
    t.memPressure = maybeAt(dir.get(),"memory.pressure").value_or("");
    t.ioPressure = maybeAt(dir.get(),"io.pressure").value_or("");
    return scopeSystemFromText(t, host);
}

// Single entry point for every linux-v2 system sample.
inline System machineSample(const Options& o, int root) {
    return o.scope.empty() ? systemSample(o.proc) : scopeSample(root, o.scope, o.proc);
}

// Every managed app must live strictly inside the scope.
inline void validateScopeTargets(const Options& o, const std::vector<App>& apps) {
    if (o.scope.empty()) return;
    for (const auto& app : apps)
        if (app.path == o.scope || !ancestor(o.scope, app.path))
            throw std::runtime_error("app outside scope_cgroup: " + app.name);
}
} // namespace ms
