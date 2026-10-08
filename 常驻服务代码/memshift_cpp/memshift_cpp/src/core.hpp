#pragma once
// memshift: a conservative, replaceable policy layer; no kernel writes here.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <deque>
#include <limits>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace ms {
using Bytes = std::uint64_t;
constexpr Bytes MiB = 1024ULL * 1024ULL;
constexpr Bytes Unlimited = std::numeric_limits<Bytes>::max();
inline Bytes sub(Bytes a, Bytes b) { return a > b ? a - b : 0; }
inline Bytes add(Bytes a, Bytes b) { return a > Unlimited - b ? Unlimited : a + b; }
inline Bytes delta(Bytes now, Bytes old) { return now >= old ? now - old : 0; }
inline Bytes amount(double x) {
    if (!std::isfinite(x) || x <= 0) return 0;
    if (x >= static_cast<double>(Unlimited)) return Unlimited;
    return static_cast<Bytes>(x);
}
inline std::string trim(const std::string& s) {
    const auto b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return {};
    return s.substr(b, s.find_last_not_of(" \t\r\n") - b + 1);
}
inline Bytes uintValue(const std::string& s) {
    if (s.empty() || s.find_first_not_of("0123456789") != std::string::npos)
        throw std::runtime_error("expected unsigned integer: " + s);
    std::size_t n = 0;
    auto v = std::stoull(s, &n);
    if (n != s.size()) throw std::runtime_error("invalid integer: " + s);
    return v;
}
inline double realValue(const std::string& s) {
    std::size_t n = 0;
    auto v = std::stod(s, &n);
    if (n != s.size() || !std::isfinite(v)) throw std::runtime_error("invalid number: " + s);
    return v;
}
inline bool boolValue(const std::string& s) {
    if (s == "true") return true;
    if (s == "false") return false;
    throw std::runtime_error("expected true or false: " + s);
}
inline Bytes mbValue(const std::string& s) {
    auto v = uintValue(s);
    if (v > Unlimited / MiB) throw std::runtime_error("MiB value overflow");
    return v * MiB;
}
inline bool safeName(const std::string& s) {
    return !s.empty() && s.size() <= 100 &&
           s.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. ") == std::string::npos &&
           s.find(' ') == std::string::npos && s != "." && s != "..";
}
inline bool safeRelative(const std::string& s) {
    if (s.empty() || s.size() > 4096 || s.front() == '/' || s.back() == '/') return false;
    for (char ch : s) {
        const auto c = static_cast<unsigned char>(ch);
        if (c < 32 || c == 127) return false;
    }
    std::istringstream in(s); std::string p;
    while (std::getline(in, p, '/'))
        if (p.empty() || p == "." || p == ".." || p.find('\n') != std::string::npos || p.find('\r') != std::string::npos) return false;
    return true;
}
inline bool ancestor(const std::string& a, const std::string& b) {
    return a == b || (b.size() > a.size() && b.compare(0, a.size(), a) == 0 && b[a.size()] == '/');
}
using KV = std::map<std::string, Bytes>;
inline KV counters(const std::string& text) {
    KV out; std::istringstream in(text); std::string line;
    while (std::getline(in, line)) {
        std::istringstream row(line); std::string k, v;
        if (row >> k >> v) out[k] = uintValue(v);
    }
    return out;
}
inline Bytes get(const KV& m, const std::string& key, Bytes fallback = 0) {
    auto it = m.find(key); return it == m.end() ? fallback : it->second;
}
inline Bytes required(const KV& m, const std::string& key) {
    auto it = m.find(key);
    if (it == m.end()) throw std::runtime_error("missing counter: " + key);
    return it->second;
}
struct PSI {
    bool valid = false;
    double some10 = 0, full10 = 0;
    Bytes someTotal = 0, fullTotal = 0;
};
inline PSI parsePSI(const std::string& text) {
    PSI p; bool some = false, full = false;
    std::istringstream in(text); std::string line;
    while (std::getline(in, line)) {
        std::istringstream row(line); std::string kind, word;
        row >> kind; bool hasAvg = false, hasTotal = false;
        double avg = 0; Bytes total = 0;
        while (row >> word) {
            auto pos = word.find('='); if (pos == std::string::npos) continue;
            if (word.substr(0, pos) == "avg10") { avg = realValue(word.substr(pos+1)); hasAvg = true; }
            if (word.substr(0, pos) == "total") { total = uintValue(word.substr(pos+1)); hasTotal = true; }
        }
        if (kind == "some" && hasAvg && hasTotal) { p.some10 = avg; p.someTotal = total; some = true; }
        if (kind == "full" && hasAvg && hasTotal) { p.full10 = avg; p.fullTotal = total; full = true; }
    }
    p.valid = some && full;
    return p;
}
inline double stallPercent(Bytes a, Bytes b, double dt) {
    if (dt <= 0) return 0;
    return std::min(100.0, static_cast<double>(delta(a,b)) / (dt * 10000.0));
}
struct Options {
    std::string root = "/sys/fs/cgroup", proc = "/proc";
    std::string logs = "./logs", lock = "./memshiftd.lock", control = "";
    bool oracleMode = false;
    std::string oracleSchedule = "", oracleLedger = "";
    Bytes oracleBatch = 512*MiB, oracleGlobalCap = 4096*MiB, oracleCaseCap = 4096*MiB, oracleAppCap = 2048*MiB;
    unsigned oracleMaxRatio = 30;
    // Experimental oracle-only interpretation. The default preserves the
    // established resident-relative request behavior.
    std::string oracleRatioSemantics = "resident_incremental";
    unsigned oracleFeedbackDelayMs = 0;
    int intervalMs = 1000, swappiness = -1;
    bool allowAnon = true, requirePSI = true;
    double horizonSec = 5, warmupSec = 30, cooldownSec = 3, backoffSec = 30;
    double alpha = 0.20, anonWeight = 0.50, keepRatio = 0.70, episodeFraction = 0.20;
    double maxDeclineMBps = 256, growthGuardMBps = 8, refaultGuardMBps = 1, majorGuardPS = 64;
    double ioSomeLimit = 5, memoryFullLimit = 2, appSomeLimit = 2;
    Bytes reserve = 1024*MiB, readyFree = 256*MiB, hysteresis = 128*MiB;
    Bytes minBatch = 8*MiB, batch = 64*MiB, perMinute = 1024*MiB;
    Bytes appPerMinute = 256*MiB, swapReserve = 256*MiB, logMax = 32*MiB;
    Bytes maxBoost = 4096*MiB;
};
struct App {
    std::string name, path;
    // Opt-in: a missing/false reclaim flag never makes this app a donor.
    bool reclaim = false;
    Bytes keep = 256*MiB;
};
struct System {
    double time = 0;
    Bytes total = 0, available = 0, free = 0, swapTotal = 0, swapFree = 0;
    Bytes zswap = 0, zswapped = 0, pswpin = 0, pswpout = 0;
    PSI mem, io;
};
struct Sample {
    bool valid = false;
    std::uint64_t device = 0, inode = 0;
    Bytes current = 0, anon = 0, file = 0, inactiveAnon = 0, inactiveFile = 0;
    Bytes dirty = 0, writeback = 0, unevictable = 0, swap = 0, swapRoom = 0;
    Bytes low = 0, min = 0, pgfault = 0, major = 0, refault = 0;
    Bytes pgsteal = 0, pgstealProactive = 0, oomKills = 0;
    bool hasProactive = false, populated = false;
    PSI psi;
};
struct Forecast {
    Bytes predictedAvailable = 0;
    double declineBytesPS = 0;
};
// Replace this interface when introducing a trained predictor. No WSS labels
// or page-level reuse guarantees are implied by this baseline.
class Predictor {
public:
    virtual ~Predictor() = default;
    virtual Forecast update(const System& s, double horizon, double maxDeclineBytesPS) = 0;
};
class EWMAPredictor final : public Predictor {
    bool have_ = false;
    System last_;
    double rate_ = 0;
public:
    Forecast update(const System& s, double horizon, double maxRate) override {
        if (have_ && s.time > last_.time) {
            const double dt = s.time-last_.time;
            // Signed derivative avoids adding a positive bias for every noisy sample.
            const double instant = std::clamp((static_cast<double>(last_.available)-
                                               static_cast<double>(s.available))/dt, -maxRate, maxRate);
            const double beta = 1-std::exp(-dt/2.0);
            rate_ = (1-beta)*rate_ + beta*instant;
        }
        last_ = s; have_ = true;
        double decline = std::clamp(rate_, 0.0, maxRate);
        // Bound extrapolation: a trend estimate is not an unseen-launch oracle.
        Bytes futureDrop = std::min(amount(decline*horizon), s.total/4);
        return {sub(s.available, futureDrop), decline};
    }
};
class RollingLimit {
    std::deque<std::pair<double,Bytes>> history_;
public:
    Bytes used(double now) {
        while (!history_.empty() && now-history_.front().first >= 60) history_.pop_front();
        Bytes total = 0; for (auto& e: history_) total = add(total,e.second);
        return total;
    }
    Bytes room(double now, Bytes cap) { return sub(cap,used(now)); }
    void charge(double now, Bytes v) { history_.push_back({now,v}); }
};
struct Features {
    Sample now, prev;
    double age = 0, growthBytesPS = 0, refaultBytesPS = 0, majorPS = 0;
    bool protectedNow = true;
    Bytes episodeRoom = 0, minuteRoom = 0;
    double adaptive = 1;
};
struct Budget { Bytes bytes = 0, coldHint = 0; std::string reason; };
inline Budget donorBudget(const Options& o, const App& app, const Features& f,
                          const System& s, Bytes deficit, Bytes globalRoom, Bytes pageSize) {
    auto no = [](const std::string& why) { return Budget{0,0,why}; };
    if (!app.reclaim || f.protectedNow) return no("protected");
    if (!f.now.valid || !f.prev.valid) return no("need_two_samples");
    if (!f.now.populated) return no("empty_cgroup");
    if (f.age < o.warmupSec) return no("startup_grace");
    if (f.growthBytesPS > o.growthGuardMBps*MiB) return no("growing_app");
    if (f.refaultBytesPS > o.refaultGuardMBps*MiB || f.majorPS > o.majorGuardPS)
        return no("refault_guard");
    if (f.now.psi.valid && f.now.psi.some10 >= o.appSomeLimit) return no("app_psi_guard");
    // LRU list membership is a weak coldness hint, NOT WSS or exact future reuse.
    const Bytes fileHint = sub(std::min(f.now.inactiveFile,f.prev.inactiveFile),
                               add(f.now.dirty,f.now.writeback));
    Bytes anonHint = 0;
    if (o.allowAnon && o.swappiness != 0) {
        anonHint = std::min({f.now.inactiveAnon,f.prev.inactiveAnon,f.now.swapRoom,
                             sub(s.swapFree,o.swapReserve)});
    }
    const Bytes cold = add(fileHint,amount(o.anonWeight*static_cast<double>(anonHint)));
    // These are controller-side budgeting floors. Kernel reclaim can overshoot;
    // they are not promised hard physical-residency guarantees.
    const Bytes keep = std::max({app.keep,f.now.min,f.now.low,
                                 amount(o.keepRatio*static_cast<double>(f.now.current))});
    Bytes q = std::min({amount(o.alpha*f.adaptive*static_cast<double>(cold)),
                        sub(f.now.current,keep),f.episodeRoom,f.minuteRoom,
                        globalRoom,deficit,o.batch});
    if (pageSize == 0) return no("invalid_page_size");
    q -= q%pageSize;
    if (q < o.minBatch) return {0,cold,"budget_too_small"};
    return {q,cold,"eligible"};
}
inline Bytes deficit(const Options& o, const System& s, const Forecast& f, Bytes boost) {
    return std::max(sub(add(o.reserve,boost),f.predictedAvailable),sub(o.readyFree,s.free));
}
inline std::string pressureBlock(const Options& o, const System& s,
                                 double recentIO, double recentMemFull) {
    if (o.requirePSI && (!s.mem.valid || !s.io.valid)) return "psi_unavailable";
    if (std::max(s.io.some10,recentIO) >= o.ioSomeLimit) return "io_pressure";
    if (std::max(s.mem.full10,recentMemFull) >= o.memoryFullLimit) return "memory_full_pressure";
    return {};
}
} // namespace ms
