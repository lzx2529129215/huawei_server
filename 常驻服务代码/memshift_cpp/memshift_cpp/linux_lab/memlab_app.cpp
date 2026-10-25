// memlab_app: one simulated application for the Linux OOM lab.
// Daily footprint = idle anonymous memory (touched once, then cold) + clean
// page cache + a working file that a writer thread keeps dirty.
// SIGUSR1 = the user returns to the app: every daily anonymous page is touched
// again (swapped-out pages fault back in). SIGUSR2 = a peak: allocate peak_mib
// more anonymous memory, keep it in use for hold_s, then release it.
// --peak-after S runs that peak by itself S seconds after DAILY_READY (the app's
// typical task during a launch). STARTUP_READY marks the end of the startup
// attempt: after the task's release, or right after DAILY_READY without one
// (with --await-start: after the start signal's task, see below).
// --exit-after-peak S closes the app S seconds after STARTUP_READY (user closes it).
// --locked-mib X adds X MiB of anonymous memory locked with mlock: it counts in
// the cgroup but can never be reclaimed (stands in for kernel memory).
// --peak-compress-ratio R fills peak pages like daily ones (about R times
// compressible) instead of random or one-word pages.
// --touch-file 1 makes SIGUSR1 also read the clean file back (dropped page
// cache comes back from disk).
// --hot-anon-mib A --hot-file-mib F --hot-period P: memory still in use while
// the app sits in the background. The first A MiB of daily anon and the first F
// MiB of the clean file are touched once per P seconds, a slice every second.
// --peak-curve FILE: the peak follows a measured curve instead of a constant rate. FILE has
// one "seconds MiB" pair per line: how much peak memory should be in use that long after
// PEAK_BEGIN (non-decreasing; the last value is the whole peak).
// --peak-hot-file-mib F --peak-hot-period P: while a peak runs (PEAK_BEGIN to
// PEAK_RELEASE) the app also reads the first F MiB of its clean file once per P
// seconds (code and data it uses for the task).
// --await-start 1: the launch waits for a start signal. After DAILY_READY the app
// logs AWAIT_START and keeps running (hot set, SIGUSR1) until the first SIGUSR2;
// one that arrived during loading is kept and served right after AWAIT_START.
// That signal runs the peak and STARTUP_READY follows PEAK_RELEASE (without
// --peak-mib it follows the signal). Later SIGUSR2 run peaks as usual. Cannot be
// combined with --peak-after.
// --peak-hot-anon-mib X --peak-hot-anon-period P (default 10): while a peak runs,
// a helper thread reads one byte per page of the first X MiB of daily anon (the
// working set the task itself needs; pages compressed earlier fault back): once
// at PEAK_BEGIN, logged as PEAK_HOT_ANON ms=<that pass> mib=<read>, then every P
// seconds (PEAK_HOT_ANON_SLOW for a later pass of >= 100 ms). It runs beside the
// ramp, so under pressure its line can come after PEAK_DONE.
// --wait-hot-anon-period W (default 2, 0 = off): with --await-start, the same X
// MiB are also read every W seconds while the app waits for its start signal
// (first pass W s after AWAIT_START; logged once as WAIT_HOT_ANON mib= period=,
// WAIT_HOT_ANON_SLOW for a pass of >= 100 ms). The app keeps using its S02
// working set, so reclaim does not take those pages first (design 0.1).
// --fill-threads N (1..16, default 1): a rate-based ramp (no --peak-curve) is
// split into N contiguous chunks, each filled by its own thread at rate/N.
// An unknown option, a missing value or a malformed number exits 2 (and is
// logged as ERROR args: when --log is given).
// ERROR lines of a failed system call end in errno=<n> (12 = ENOMEM; bad_alloc
// logs errno=12). A failed mlock says (raise LimitMEMLOCK) for ENOMEM/EPERM (the
// rlimit) and (out of memory while locking) for EAGAIN, which is how mlock
// reports an ENOMEM while populating the pages (mm/mlock.c).
// Every phase is appended to an event log with write(2) so lines survive an
// OOM kill.
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <map>
#include <new>
#include <set>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <system_error>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {
constexpr std::size_t MiB = 1024 * 1024;
std::atomic<bool> stopping{false};
volatile sig_atomic_t touchRequested = 0, peakRequested = 0;
void onSignal(int) { stopping = true; }
void onUser(int sig) { if (sig == SIGUSR1) touchRequested = 1; else peakRequested = 1; }

double boottime() {
    timespec ts{};
    clock_gettime(CLOCK_BOOTTIME, &ts);
    return static_cast<double>(ts.tv_sec) + static_cast<double>(ts.tv_nsec) / 1e9;
}

struct Args {
    std::string name = "app", log, dataDir, peakCurve;
    double anonMiB = 0, cleanMiB = 0, dirtyMiB = 0, dirtyRate = 0;
    double peakMiB = 0, peakRate = 0, hold = 0, peakDeadline = 30, ttl = 600, peakPageOnly = 0, compressRatio = 1;
    double peakAfter = -1, exitAfterPeak = -1, lockedMiB = 0, peakCompressRatio = 0, touchFile = 0;
    double hotAnonMiB = 0, hotFileMiB = 0, hotPeriod = 120, peakHotFileMiB = 0, peakHotPeriod = 10;
    double awaitStart = 0, peakHotAnonMiB = 0, peakHotAnonPeriod = 10, fillThreads = 1, waitHotAnonPeriod = 2;
    std::uint64_t seed = 1;
};

int logFd = -1;
std::string appName;
void logEvent(const std::string& what) {
    char head[96];
    std::snprintf(head, sizeof(head), "%.3f %s ", boottime(), appName.c_str());
    const std::string line = head + what + "\n";
    if (logFd >= 0) { auto n = ::write(logFd, line.data(), line.size()); (void)n; }
}

// A failed system call; main logs it with errno=<n>. Callers copy errno before
// building the message, which may change errno.
[[noreturn]] void sysFail(int err, const std::string& what) {
    throw std::system_error(err, std::generic_category(), what);
}

// std::thread whose creation error names the helper (pthread_create fails with
// EAGAIN or ENOMEM when threads or memory run short).
template <class... T>
std::thread spawn(const char* what, T&&... args) {
    try {
        return std::thread(std::forward<T>(args)...);
    } catch (const std::system_error& e) {
        throw std::system_error(e.code(), std::string("thread ") + what);
    }
}

struct Rng {
    std::uint64_t s;
    std::uint64_t next() { s ^= s >> 12; s ^= s << 25; s ^= s >> 27; return s * 2685821657736338717ULL; }
};

// Random words make every page incompressible and private.
void fill(unsigned char* p, std::size_t bytes, Rng& rng) {
    auto* w = reinterpret_cast<std::uint64_t*>(p);
    for (std::size_t i = 0; i < bytes / 8; ++i) w[i] = rng.next();
}

// ratio > 1: each 4 KiB page gets 1/ratio random words and a constant fill,
// so a compressor such as lz4 shrinks it to roughly 1/ratio. The fill is never
// all zero, so the kernel's zero-page shortcut does not apply.
void fillCompressible(unsigned char* p, std::size_t bytes, Rng& rng, double ratio) {
    if (ratio <= 1.0) { fill(p, bytes, rng); return; }
    const std::size_t words = 4096 / 8;
    const auto randomWords = std::max<std::size_t>(1, static_cast<std::size_t>(static_cast<double>(words) / ratio));
    for (std::size_t page = 0; page < bytes / 4096; ++page) {
        auto* w = reinterpret_cast<std::uint64_t*>(p + page * 4096);
        for (std::size_t i = 0; i < randomWords; ++i) w[i] = rng.next();
        const std::uint64_t pattern = 0x0101010101010101ULL * ((page & 0x7f) + 1);
        for (std::size_t i = randomWords; i < words; ++i) w[i] = pattern;
    }
}

unsigned char* mapAnon(std::size_t bytes) {
    void* p = ::mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) sysFail(errno, "mmap");
    return static_cast<unsigned char*>(p);
}

// Touch at a bounded rate (MiB/s, 0 = unbounded) so a peak can ramp.
// pageOnly writes one random word per page: allocation then runs at page-fault
// speed instead of fill speed, like an app that maps and touches a big buffer.
// exactLast paces a short last step by its own size (the threaded split, whose
// chunks are often under one step); the single-thread ramp keeps its old timing.
void touchRamp(unsigned char* p, std::size_t bytes, double rate, Rng& rng, bool pageOnly, double compressRatio,
               bool exactLast = false) {
    const std::size_t step = 16 * MiB;
    const double begin = boottime();
    for (std::size_t done = 0; done < bytes && !stopping; done += step) {
        const std::size_t n = std::min(step, bytes - done);
        if (compressRatio > 0) {
            fillCompressible(p + done, n, rng, compressRatio);
        } else if (pageOnly) {
            for (std::size_t off = 0; off < n; off += 4096)
                *reinterpret_cast<std::uint64_t*>(p + done + off) = rng.next();
        } else {
            fill(p + done, n, rng);
        }
        if (rate > 0) {
            const double due = begin + static_cast<double>(done + (exactLast ? n : step)) / MiB / rate;
            const double wait = due - boottime();
            if (wait > 0) std::this_thread::sleep_for(std::chrono::duration<double>(wait));
        }
    }
}

// touchRamp split into n contiguous page-aligned chunks, each filled by its own
// thread at rate / n (0 stays unbounded) with its own Rng drawn from rng.
// Returns when every chunk is done. n == 1 is plain touchRamp.
void touchRampThreads(unsigned char* p, std::size_t bytes, double rate, Rng& rng, bool pageOnly,
                      double compressRatio, std::size_t n) {
    if (n <= 1) { touchRamp(p, bytes, rate, rng, pageOnly, compressRatio); return; }
    const std::size_t chunk = (bytes / 4096 + n - 1) / n * 4096;
    const double each = rate / static_cast<double>(n);
    std::vector<std::thread> threads;
    threads.reserve(n);
    for (std::size_t off = 0; off < bytes; off += chunk) {
        const std::size_t len = std::min(chunk, bytes - off);
        Rng own{rng.next() | 1};  // never the all-zero xorshift state
        try {
            threads.emplace_back([=]() mutable { touchRamp(p + off, len, each, own, pageOnly, compressRatio, true); });
        } catch (const std::system_error& e) {
            // No thread available: fill this chunk here rather than fail the peak.
            logEvent(std::string("WARN fill thread: ") + e.what() + " errno=" + std::to_string(e.code().value()));
            touchRamp(p + off, len, each, own, pageOnly, compressRatio, true);
        }
    }
    for (auto& t : threads) t.join();
}

void writeFile(const std::string& path, std::size_t bytes, Rng& rng, bool sync) {
    int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) { const int err = errno; sysFail(err, "open " + path); }
    std::vector<unsigned char> buf(MiB);
    for (std::size_t done = 0; done < bytes && !stopping; done += MiB) {
        fill(buf.data(), buf.size(), rng);
        if (::write(fd, buf.data(), std::min(MiB, bytes - done)) < 0) {
            const int err = errno;
            ::close(fd);
            sysFail(err, "write " + path);
        }
    }
    if (sync && ::fdatasync(fd) != 0) {
        const int err = errno;
        ::close(fd);
        sysFail(err, "fdatasync " + path);
    }
    ::close(fd);
}

// Rewrites the working file in place without fsync so its pages stay dirty.
void dirtyWriter(std::string path, std::size_t bytes, double rate, std::uint64_t seed) {
    int fd = ::open(path.c_str(), O_WRONLY | O_CLOEXEC);
    if (fd < 0) return;
    Rng rng{seed};
    const std::size_t block = 256 * 1024;
    std::vector<unsigned char> buf(block);
    std::size_t offset = 0;
    const double begin = boottime();
    std::size_t written = 0;
    while (!stopping) {
        fill(buf.data(), block, rng);
        if (::pwrite(fd, buf.data(), block, static_cast<off_t>(offset)) < 0) break;
        offset = (offset + block) % bytes;
        written += block;
        const double due = begin + static_cast<double>(written) / MiB / rate;
        const double wait = due - boottime();
        if (wait > 0) std::this_thread::sleep_for(std::chrono::duration<double>(std::min(wait, 1.0)));
    }
    ::close(fd);
}

// The whole value must be a finite number ("15s" or "" is an error, not 15 or 0).
double numberOf(const std::string& k, const std::string& v) {
    std::size_t used = 0;
    double x = 0;
    try { x = std::stod(v, &used); } catch (const std::exception&) { used = 0; }
    if (used == 0 || used != v.size() || !std::isfinite(x))
        throw std::runtime_error("bad number for --" + k + ": '" + v + "'");
    return x;
}

// "--key value" pairs. Every key must be one that is read below: a misspelt
// option would otherwise drop a lever silently in an unattended run.
Args parse(int argc, char** argv) {
    Args a; std::map<std::string, std::string> kv;
    if (argc % 2 == 0)
        throw std::runtime_error(std::string("unpaired argument '") + argv[argc - 1] + "' (expected --key value)");
    for (int i = 1; i + 1 < argc; i += 2) {
        std::string k = argv[i], v = argv[i + 1];
        if (k.rfind("--", 0) != 0) throw std::runtime_error("expected --key value, got '" + k + "'");
        if (v.rfind("--", 0) == 0) throw std::runtime_error("missing value for " + k);
        kv[k.substr(2)] = v;
    }
    std::set<std::string> known;
    auto has = [&](const char* k) { known.insert(k); return kv.count(k) > 0; };
    auto str = [&](const char* k, std::string& v) { if (has(k)) v = kv[k]; };
    auto num = [&](const char* k, double& v) { if (has(k)) v = numberOf(k, kv[k]); };
    str("name", a.name); str("log", a.log); str("data-dir", a.dataDir); str("peak-curve", a.peakCurve);
    if (has("seed")) {
        const std::string& s = kv["seed"];
        std::size_t used = 0;
        try { a.seed = std::stoull(s, &used); } catch (const std::exception&) { used = 0; }
        if (used == 0 || used != s.size()) throw std::runtime_error("bad number for --seed: '" + s + "'");
    }
    num("anon-mib", a.anonMiB); num("clean-mib", a.cleanMiB); num("dirty-mib", a.dirtyMiB);
    num("dirty-rate", a.dirtyRate); num("peak-mib", a.peakMiB); num("peak-page-only", a.peakPageOnly);
    num("compress-ratio", a.compressRatio);
    num("peak-after", a.peakAfter); num("exit-after-peak", a.exitAfterPeak);
    num("locked-mib", a.lockedMiB); num("peak-compress-ratio", a.peakCompressRatio); num("touch-file", a.touchFile);
    num("hot-anon-mib", a.hotAnonMiB); num("hot-file-mib", a.hotFileMiB); num("hot-period", a.hotPeriod);
    num("peak-hot-file-mib", a.peakHotFileMiB); num("peak-hot-period", a.peakHotPeriod);
    num("await-start", a.awaitStart); num("peak-hot-anon-mib", a.peakHotAnonMiB);
    num("peak-hot-anon-period", a.peakHotAnonPeriod); num("fill-threads", a.fillThreads);
    num("wait-hot-anon-period", a.waitHotAnonPeriod);
    num("peak-rate", a.peakRate); num("hold", a.hold); num("peak-deadline", a.peakDeadline); num("ttl", a.ttl);
    for (const auto& item : kv)
        if (!known.count(item.first)) throw std::runtime_error("unknown option --" + item.first);
    if (a.hotPeriod < 1 || a.peakHotPeriod < 1 || a.peakHotAnonPeriod < 1)
        throw std::runtime_error("hot periods must be >= 1");
    if (a.waitHotAnonPeriod < 0 || (a.waitHotAnonPeriod > 0 && a.waitHotAnonPeriod < 1))
        throw std::runtime_error("wait-hot-anon-period must be 0 (off) or >= 1");
    if (a.peakHotAnonMiB < 0) throw std::runtime_error("peak-hot-anon-mib must be >= 0");
    if (!(a.fillThreads >= 1 && a.fillThreads <= 16) || a.fillThreads != std::floor(a.fillThreads))
        throw std::runtime_error("fill-threads must be an integer in [1,16]");
    if (a.awaitStart > 0 && a.peakAfter >= 0) throw std::runtime_error("--await-start excludes --peak-after");
    if (a.ttl <= 0 || a.ttl > 3600) throw std::runtime_error("ttl must be in (0,3600]");
    if ((a.cleanMiB > 0 || a.dirtyMiB > 0) && a.dataDir.empty()) throw std::runtime_error("files need --data-dir");
    return a;
}

// A bad command line also goes to the --log file when there is one, so a runner
// that only reads app logs sees why the app never started.
void logArgsError(int argc, char** argv, const std::string& msg) {
    std::string path, name = "app";
    for (int i = 1; i + 1 < argc; i += 2) {
        if (std::strcmp(argv[i], "--log") == 0) path = argv[i + 1];
        if (std::strcmp(argv[i], "--name") == 0) name = argv[i + 1];
    }
    if (path.empty()) return;
    logFd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    appName = name;
    logEvent("ERROR args: " + msg);
    if (logFd >= 0) ::close(logFd);
    logFd = -1;
}

std::size_t bytesOf(double mib) { return static_cast<std::size_t>(mib * MiB) / 4096 * 4096; }
} // namespace

// Reads slices of the clean file until done is set: the file pages a task uses.
void peakHotReader(const Args& a, const std::atomic<bool>& done) {
    const std::size_t total = std::min(bytesOf(a.cleanMiB), bytesOf(a.peakHotFileMiB));
    int fd = ::open((a.dataDir + "/" + a.name + ".clean").c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0 || !total) { if (fd >= 0) ::close(fd); return; }
    const std::size_t slices = static_cast<std::size_t>(a.peakHotPeriod);
    const std::size_t step = (total / slices + 4095) / 4096 * 4096;
    std::vector<unsigned char> buf(256 * 1024);
    std::size_t slice = 0;
    double next = boottime();
    while (!done && !stopping) {
        const double t = boottime();
        const std::size_t f0 = std::min(total, slice * step), f1 = std::min(total, (slice + 1) * step);
        for (std::size_t off = f0; off < f1 && !done && !stopping; off += buf.size())
            if (::pread(fd, buf.data(), std::min(buf.size(), f1 - off), static_cast<off_t>(off)) <= 0) break;
        const double ms = (boottime() - t) * 1000;
        if (ms >= 100) { char b[64]; std::snprintf(b, sizeof(b), "PEAK_HOT_SLOW ms=%.0f", ms); logEvent(b); }
        slice = (slice + 1) % slices;
        next += 1.0;
        while (!done && !stopping && boottime() < next) std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    ::close(fd);
}

// Reads one byte per page of anon[0, bytes) every period seconds until done:
// the daily anon working set the task itself uses. Peak mode: the first pass
// runs right away and is always logged (partial=1 when the release cut it
// short). Waiting mode (before the start signal): the first pass one period
// later, as the pages were just loaded; slow passes are WAIT_HOT_ANON_SLOW.
void peakHotAnon(const unsigned char* anon, std::size_t bytes, double period, const std::atomic<bool>& done,
                 bool waiting) {
    double next = boottime() + (waiting ? period : 0.0);
    while (waiting && !done && !stopping && boottime() < next) std::this_thread::sleep_for(std::chrono::milliseconds(20));
    for (int pass = waiting ? 1 : 0; pass == 0 || (!done && !stopping); ++pass) {
        const double t = boottime();
        volatile unsigned char sink = 0;
        std::size_t off = 0;
        for (; off < bytes && !done && !stopping; off += 4096) sink = sink + anon[off];
        const double ms = (boottime() - t) * 1000;
        char b[96];
        b[0] = '\0';
        if (pass == 0)
            std::snprintf(b, sizeof(b), "PEAK_HOT_ANON ms=%.0f mib=%.0f%s", ms,
                          static_cast<double>(bytes) / MiB, off < bytes ? " partial=1" : "");
        else if (ms >= 100)
            std::snprintf(b, sizeof(b), "%s ms=%.0f", waiting ? "WAIT_HOT_ANON_SLOW" : "PEAK_HOT_ANON_SLOW", ms);
        if (b[0]) logEvent(b);
        next = std::max(next + period, boottime());  // a pass longer than period: next one at once
        while (!done && !stopping && boottime() < next) std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
}

// Allocates [0, bytes) following a measured curve: (seconds, MiB) points, linear in between.
void touchCurve(unsigned char* p, std::size_t bytes, const std::vector<std::pair<double, double>>& curve,
                Rng& rng, double compressRatio) {
    const double begin = boottime();
    const std::size_t step = MiB;
    std::size_t done = 0;
    std::size_t k = 0;
    while (done < bytes && !stopping) {
        const double t = boottime() - begin;
        while (k + 1 < curve.size() && curve[k + 1].first <= t) ++k;
        double mib = curve.back().second;
        if (k + 1 < curve.size()) {
            const auto [ta, va] = curve[k];
            const auto [tb, vb] = curve[k + 1];
            mib = tb > ta ? va + (vb - va) * std::max(0.0, t - ta) / (tb - ta) : vb;
        }
        // Past the last point the whole peak is due, even if the curve ends slightly short.
        const std::size_t target = t >= curve.back().first ? bytes : std::min(bytes, bytesOf(mib));
        if (done + step <= target || (target == bytes && done < bytes)) {
            const std::size_t n = std::min(step, bytes - done);
            if (compressRatio > 0) fillCompressible(p + done, n, rng, compressRatio);
            else fill(p + done, n, rng);
            done += n;
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }
}

std::vector<std::pair<double, double>> readCurve(const std::string& path) {
    std::vector<std::pair<double, double>> c;
    FILE* f = std::fopen(path.c_str(), "r");
    if (!f) { const int err = errno; sysFail(err, "open peak curve " + path); }
    double t = 0, m = 0;
    while (std::fscanf(f, "%lf %lf", &t, &m) == 2) c.emplace_back(t, m);
    std::fclose(f);
    if (c.empty()) throw std::runtime_error("empty peak curve " + path);
    return c;
}

// The peak's helper threads. They are stopped and joined on every way out of
// runPeak, also when the ramp throws (destroying a joinable std::thread aborts).
struct PeakHelpers {
    std::atomic<bool> done{false};
    std::thread fileReader, anonReader;
    void stop() {
        done = true;
        if (fileReader.joinable()) fileReader.join();
        if (anonReader.joinable()) anonReader.join();
    }
    ~PeakHelpers() { stop(); }
};

void runPeak(const Args& a, Rng& rng, const unsigned char* anon, std::size_t anonBytes) {
    const auto bytes = bytesOf(a.peakMiB);
    const double t = boottime();
    logEvent("PEAK_BEGIN mib=" + std::to_string(static_cast<long>(a.peakMiB)));
    PeakHelpers hot;
    if (a.peakHotFileMiB > 0 && a.cleanMiB > 0)
        hot.fileReader = spawn("peak hot file", peakHotReader, std::cref(a), std::cref(hot.done));
    const std::size_t hotAnon = anon ? std::min(anonBytes, bytesOf(a.peakHotAnonMiB)) : 0;
    if (hotAnon > 0)
        hot.anonReader = spawn("peak hot anon", peakHotAnon, anon, hotAnon, a.peakHotAnonPeriod, std::cref(hot.done),
                               false);
    auto* p = mapAnon(bytes);
    if (!a.peakCurve.empty()) touchCurve(p, bytes, readCurve(a.peakCurve), rng, a.peakCompressRatio);
    else touchRampThreads(p, bytes, a.peakRate, rng, a.peakPageOnly > 0, a.peakCompressRatio,
                          static_cast<std::size_t>(a.fillThreads));
    const double ms = (boottime() - t) * 1000;
    char buf[96];
    std::snprintf(buf, sizeof(buf), "PEAK_DONE ms=%.0f timeout=%d", ms, ms > a.peakDeadline * 1000 ? 1 : 0);
    logEvent(buf);
    // Active use during the hold keeps the peak memory hot.
    const double until = boottime() + a.hold;
    while (!stopping && boottime() < until) {
        for (std::size_t off = 0; off < bytes; off += 4096) p[off] ^= 1;
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    hot.stop();
    ::munmap(p, bytes);
    logEvent("PEAK_RELEASE");
}

int main(int argc, char** argv) {
    std::signal(SIGTERM, onSignal); std::signal(SIGINT, onSignal);
    std::signal(SIGUSR1, onUser); std::signal(SIGUSR2, onUser);
    Args a;
    try {
        a = parse(argc, argv);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "%s\n", e.what());
        logArgsError(argc, argv, e.what());
        return 2;
    }
    appName = a.name;
    if (!a.log.empty()) logFd = ::open(a.log.c_str(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    const double start = boottime(), deadline = start + a.ttl;
    logEvent("START pid=" + std::to_string(::getpid()));
    Rng rng{a.seed * 0x9e3779b97f4a7c15ULL + 1};
    std::thread writer;
    unsigned char* anon = nullptr;
    const std::size_t anonBytes = bytesOf(a.anonMiB), lockedBytes = bytesOf(a.lockedMiB);
    const auto fail = [&](const std::string& line) {
        logEvent(line);
        stopping = true;
        if (writer.joinable()) writer.join();
        return 1;
    };
    try {
        if (anonBytes > 0) {
            anon = mapAnon(anonBytes);
            fillCompressible(anon, anonBytes, rng, a.compressRatio); // touched once, then left cold
        }
        if (lockedBytes > 0) {
            auto* locked = mapAnon(lockedBytes);
            fillCompressible(locked, lockedBytes, rng, a.compressRatio);
            if (::mlock(locked, lockedBytes) != 0) {
                // ENOMEM/EPERM: over RLIMIT_MEMLOCK (setup). EAGAIN: no memory to
                // populate the pages, which mlock reports as EAGAIN.
                const int err = errno;
                sysFail(err, err == ENOMEM || err == EPERM ? "mlock (raise LimitMEMLOCK)"
                             : err == EAGAIN               ? "mlock (out of memory while locking)"
                                                           : "mlock");
            }
        }
        if (a.cleanMiB > 0) writeFile(a.dataDir + "/" + a.name + ".clean", bytesOf(a.cleanMiB), rng, true);
        if (a.dirtyMiB > 0) {
            const auto path = a.dataDir + "/" + a.name + ".dirty";
            writeFile(path, bytesOf(a.dirtyMiB), rng, false);
            if (a.dirtyRate > 0)
                writer = spawn("dirty writer", dirtyWriter, path, bytesOf(a.dirtyMiB), a.dirtyRate, a.seed + 7);
        }
        char buf[160];
        std::snprintf(buf, sizeof(buf), "DAILY_READY ms=%.0f anon_mib=%.0f clean_mib=%.0f dirty_mib=%.0f locked_mib=%.0f",
                      (boottime() - start) * 1000, a.anonMiB, a.cleanMiB, a.dirtyMiB, a.lockedMiB);
        logEvent(buf);
        // --await-start: the startup attempt stays open until the start signal's
        // peak is done (main loop below). A SIGUSR2 from loading is still pending.
        bool awaiting = a.awaitStart > 0;
        // While waiting, a helper reads the S02 working set every few seconds
        // (design 0.1): a real app keeps using it, so it must not look coldest.
        PeakHelpers waitHot;
        const std::size_t waitHotBytes =
            awaiting && anon && a.waitHotAnonPeriod > 0 ? std::min(anonBytes, bytesOf(a.peakHotAnonMiB)) : 0;
        const auto startWaitHot = [&]() {
            waitHot.done = false;
            try {
                waitHot.anonReader = std::thread(peakHotAnon, anon, waitHotBytes, a.waitHotAnonPeriod,
                                                 std::cref(waitHot.done), true);
            } catch (const std::system_error& e) {
                // Go on without it: observe does not reclaim before the peak; in
                // apply those pages may be compressed and fault back at the peak.
                logEvent(std::string("WARN wait hot anon thread: ") + e.what() + " errno=" +
                         std::to_string(e.code().value()));
            }
        };
        if (awaiting) {
            logEvent("AWAIT_START");
            if (waitHotBytes > 0) {
                std::snprintf(buf, sizeof(buf), "WAIT_HOT_ANON mib=%.0f period=%g",
                              static_cast<double>(waitHotBytes) / MiB, a.waitHotAnonPeriod);
                logEvent(buf);
                startWaitHot();
            }
        } else {
            if (a.peakMiB > 0 && a.peakAfter >= 0) {
                const double at = boottime() + a.peakAfter;
                while (!stopping && boottime() < at) std::this_thread::sleep_for(std::chrono::milliseconds(50));
                if (!stopping) runPeak(a, rng, anon, anonBytes);
            }
            if (!stopping) logEvent("STARTUP_READY ms=" + std::to_string(static_cast<long>((boottime() - start) * 1000)));
        }
        double closeAt = !awaiting && a.exitAfterPeak >= 0 ? boottime() + a.exitAfterPeak : deadline;
        // Hot set: slices of anon pages and of the clean file, one slice per second.
        const std::size_t hotAnon = std::min(anonBytes, bytesOf(a.hotAnonMiB));
        const std::size_t hotFile = std::min(bytesOf(a.cleanMiB), bytesOf(a.hotFileMiB));
        const std::size_t slices = static_cast<std::size_t>(a.hotPeriod);
        std::size_t slice = 0;
        double nextHot = boottime() + 1.0;
        int hotFd = hotFile ? ::open((a.dataDir + "/" + a.name + ".clean").c_str(), O_RDONLY | O_CLOEXEC) : -1;
        std::vector<unsigned char> hotBuf(hotFd >= 0 ? 256 * 1024 : 0);
        while (!stopping && boottime() < std::min(deadline, closeAt)) {
            if ((hotAnon || hotFd >= 0) && boottime() >= nextHot) {
                nextHot += 1.0;
                const double t = boottime();
                const auto part = [&](std::size_t total) {
                    const std::size_t step = (total / slices + 4095) / 4096 * 4096;
                    return std::make_pair(std::min(total, slice * step), std::min(total, (slice + 1) * step));
                };
                volatile unsigned char sink = 0;
                const auto [a0, a1] = part(hotAnon);
                for (std::size_t off = a0; anon && off < a1 && !stopping; off += 4096) sink = sink + anon[off];
                const auto [f0, f1] = part(hotFile);
                for (std::size_t off = f0; hotFd >= 0 && off < f1 && !stopping; off += hotBuf.size())
                    if (::pread(hotFd, hotBuf.data(), std::min(hotBuf.size(), f1 - off), static_cast<off_t>(off)) <= 0) break;
                slice = (slice + 1) % slices;
                const double ms = (boottime() - t) * 1000;
                if (ms >= 100) { std::snprintf(buf, sizeof(buf), "HOT_SLOW ms=%.0f", ms); logEvent(buf); }
            }
            if (touchRequested) {
                touchRequested = 0;
                // The touch writes every anon page: pause the waiting reader meanwhile.
                const bool rearm = waitHot.anonReader.joinable();
                if (rearm) waitHot.stop();
                const double t = boottime();
                logEvent("TOUCH_BEGIN");
                for (std::size_t off = 0; anon && off < anonBytes && !stopping; off += 4096) anon[off] ^= 1;
                const double anonDone = boottime();
                if (a.touchFile > 0 && a.cleanMiB > 0) {
                    // Read the clean file back; pages dropped by reclaim come from disk.
                    int fd = ::open((a.dataDir + "/" + a.name + ".clean").c_str(), O_RDONLY | O_CLOEXEC);
                    if (fd >= 0) {
                        std::vector<unsigned char> rb(MiB);
                        while (!stopping && ::read(fd, rb.data(), rb.size()) > 0) {}
                        ::close(fd);
                    }
                }
                const double end = boottime();
                std::snprintf(buf, sizeof(buf), "TOUCH_DONE ms=%.0f anon_ms=%.0f file_ms=%.0f",
                              (end - t) * 1000, (anonDone - t) * 1000, (end - anonDone) * 1000);
                logEvent(buf);
                if (rearm && awaiting && !stopping) startWaitHot();
            }
            if (peakRequested) {
                peakRequested = 0;
                // The wait is over; the reader stops within a page. It is joined
                // after the peak: a join here could delay PEAK_BEGIN by its 20 ms sleep.
                waitHot.done = true;
                if (a.peakMiB > 0) runPeak(a, rng, anon, anonBytes);
                waitHot.stop();
                if (awaiting && !stopping) {
                    // The start signal's task is done: this ends the startup attempt.
                    awaiting = false;
                    logEvent("STARTUP_READY ms=" + std::to_string(static_cast<long>((boottime() - start) * 1000)));
                    if (a.exitAfterPeak >= 0) closeAt = boottime() + a.exitAfterPeak;
                }
            }
            // Poll faster while waiting so the start signal is served within ~10 ms.
            std::this_thread::sleep_for(std::chrono::milliseconds(awaiting ? 10 : 50));
        }
        if (hotFd >= 0) ::close(hotFd);
    } catch (const std::system_error& e) {
        return fail(std::string("ERROR ") + e.what() + " errno=" + std::to_string(e.code().value()));
    } catch (const std::bad_alloc&) {
        return fail("ERROR bad_alloc errno=" + std::to_string(ENOMEM));
    } catch (const std::exception& e) {
        return fail(std::string("ERROR ") + e.what());
    }
    stopping = true;
    if (writer.joinable()) writer.join();
    logEvent("EXIT");
    return 0;
}
