// memlab_app: one simulated application for the Linux OOM lab.
// Daily footprint = idle anonymous memory (touched once, then cold) + clean
// page cache + a working file that a writer thread keeps dirty.
// SIGUSR1 = the user returns to the app: every daily anonymous page is touched
// again (swapped-out pages fault back in). SIGUSR2 = a peak: allocate peak_mib
// more anonymous memory, keep it in use for hold_s, then release it.
// --peak-after S runs that peak by itself S seconds after DAILY_READY (the app's
// typical task during a launch). STARTUP_READY marks the end of the startup
// attempt: after the task's release, or right after DAILY_READY without one.
// --exit-after-peak S closes the app S seconds after its task (user closes it).
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
// Every phase is appended to an event log with write(2) so lines survive an
// OOM kill.
#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <map>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <thread>
#include <time.h>
#include <unistd.h>
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
    if (p == MAP_FAILED) throw std::runtime_error(std::string("mmap: ") + std::strerror(errno));
    return static_cast<unsigned char*>(p);
}

// Touch at a bounded rate (MiB/s, 0 = unbounded) so a peak can ramp.
// pageOnly writes one random word per page: allocation then runs at page-fault
// speed instead of fill speed, like an app that maps and touches a big buffer.
void touchRamp(unsigned char* p, std::size_t bytes, double rate, Rng& rng, bool pageOnly, double compressRatio) {
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
            const double due = begin + static_cast<double>(done + step) / MiB / rate;
            const double wait = due - boottime();
            if (wait > 0) std::this_thread::sleep_for(std::chrono::duration<double>(wait));
        }
    }
}

void writeFile(const std::string& path, std::size_t bytes, Rng& rng, bool sync) {
    int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) throw std::runtime_error("open " + path + ": " + std::strerror(errno));
    std::vector<unsigned char> buf(MiB);
    for (std::size_t done = 0; done < bytes && !stopping; done += MiB) {
        fill(buf.data(), buf.size(), rng);
        if (::write(fd, buf.data(), std::min(MiB, bytes - done)) < 0)
            throw std::runtime_error("write " + path + ": " + std::strerror(errno));
    }
    if (sync && ::fdatasync(fd) != 0) throw std::runtime_error("fdatasync " + path);
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

Args parse(int argc, char** argv) {
    Args a; std::map<std::string, std::string> kv;
    for (int i = 1; i + 1 < argc; i += 2) {
        std::string k = argv[i];
        if (k.rfind("--", 0) != 0) throw std::runtime_error("expected --key value");
        kv[k.substr(2)] = argv[i + 1];
    }
    auto num = [&](const char* k, double& v) { if (kv.count(k)) v = std::stod(kv[k]); };
    if (kv.count("name")) a.name = kv["name"];
    if (kv.count("log")) a.log = kv["log"];
    if (kv.count("data-dir")) a.dataDir = kv["data-dir"];
    if (kv.count("peak-curve")) a.peakCurve = kv["peak-curve"];
    if (kv.count("seed")) a.seed = std::stoull(kv["seed"]);
    num("anon-mib", a.anonMiB); num("clean-mib", a.cleanMiB); num("dirty-mib", a.dirtyMiB);
    num("dirty-rate", a.dirtyRate); num("peak-mib", a.peakMiB); num("peak-page-only", a.peakPageOnly);
    num("compress-ratio", a.compressRatio);
    num("peak-after", a.peakAfter); num("exit-after-peak", a.exitAfterPeak);
    num("locked-mib", a.lockedMiB); num("peak-compress-ratio", a.peakCompressRatio); num("touch-file", a.touchFile);
    num("hot-anon-mib", a.hotAnonMiB); num("hot-file-mib", a.hotFileMiB); num("hot-period", a.hotPeriod);
    num("peak-hot-file-mib", a.peakHotFileMiB); num("peak-hot-period", a.peakHotPeriod);
    if (a.hotPeriod < 1 || a.peakHotPeriod < 1) throw std::runtime_error("hot periods must be >= 1");
    num("peak-rate", a.peakRate); num("hold", a.hold); num("peak-deadline", a.peakDeadline); num("ttl", a.ttl);
    if (a.ttl <= 0 || a.ttl > 3600) throw std::runtime_error("ttl must be in (0,3600]");
    if ((a.cleanMiB > 0 || a.dirtyMiB > 0) && a.dataDir.empty()) throw std::runtime_error("files need --data-dir");
    return a;
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
    if (!f) throw std::runtime_error("open peak curve " + path);
    double t = 0, m = 0;
    while (std::fscanf(f, "%lf %lf", &t, &m) == 2) c.emplace_back(t, m);
    std::fclose(f);
    if (c.empty()) throw std::runtime_error("empty peak curve " + path);
    return c;
}

void runPeak(const Args& a, Rng& rng) {
    const auto bytes = bytesOf(a.peakMiB);
    const double t = boottime();
    logEvent("PEAK_BEGIN mib=" + std::to_string(static_cast<long>(a.peakMiB)));
    std::atomic<bool> hotDone{false};
    std::thread hot;
    if (a.peakHotFileMiB > 0 && a.cleanMiB > 0) hot = std::thread(peakHotReader, std::cref(a), std::cref(hotDone));
    auto* p = mapAnon(bytes);
    if (!a.peakCurve.empty()) touchCurve(p, bytes, readCurve(a.peakCurve), rng, a.peakCompressRatio);
    else touchRamp(p, bytes, a.peakRate, rng, a.peakPageOnly > 0, a.peakCompressRatio);
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
    hotDone = true;
    if (hot.joinable()) hot.join();
    ::munmap(p, bytes);
    logEvent("PEAK_RELEASE");
}

int main(int argc, char** argv) {
    std::signal(SIGTERM, onSignal); std::signal(SIGINT, onSignal);
    std::signal(SIGUSR1, onUser); std::signal(SIGUSR2, onUser);
    Args a;
    try { a = parse(argc, argv); } catch (const std::exception& e) { std::fprintf(stderr, "%s\n", e.what()); return 2; }
    appName = a.name;
    if (!a.log.empty()) logFd = ::open(a.log.c_str(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    const double start = boottime(), deadline = start + a.ttl;
    logEvent("START pid=" + std::to_string(::getpid()));
    Rng rng{a.seed * 0x9e3779b97f4a7c15ULL + 1};
    std::thread writer;
    unsigned char* anon = nullptr;
    const std::size_t anonBytes = bytesOf(a.anonMiB), lockedBytes = bytesOf(a.lockedMiB);
    try {
        if (anonBytes > 0) {
            anon = mapAnon(anonBytes);
            fillCompressible(anon, anonBytes, rng, a.compressRatio); // touched once, then left cold
        }
        if (lockedBytes > 0) {
            auto* locked = mapAnon(lockedBytes);
            fillCompressible(locked, lockedBytes, rng, a.compressRatio);
            if (::mlock(locked, lockedBytes) != 0)
                throw std::runtime_error(std::string("mlock (raise LimitMEMLOCK): ") + std::strerror(errno));
        }
        if (a.cleanMiB > 0) writeFile(a.dataDir + "/" + a.name + ".clean", bytesOf(a.cleanMiB), rng, true);
        if (a.dirtyMiB > 0) {
            const auto path = a.dataDir + "/" + a.name + ".dirty";
            writeFile(path, bytesOf(a.dirtyMiB), rng, false);
            if (a.dirtyRate > 0) writer = std::thread(dirtyWriter, path, bytesOf(a.dirtyMiB), a.dirtyRate, a.seed + 7);
        }
        char buf[160];
        std::snprintf(buf, sizeof(buf), "DAILY_READY ms=%.0f anon_mib=%.0f clean_mib=%.0f dirty_mib=%.0f locked_mib=%.0f",
                      (boottime() - start) * 1000, a.anonMiB, a.cleanMiB, a.dirtyMiB, a.lockedMiB);
        logEvent(buf);
        if (a.peakMiB > 0 && a.peakAfter >= 0) {
            const double at = boottime() + a.peakAfter;
            while (!stopping && boottime() < at) std::this_thread::sleep_for(std::chrono::milliseconds(50));
            if (!stopping) runPeak(a, rng);
        }
        if (!stopping) logEvent("STARTUP_READY ms=" + std::to_string(static_cast<long>((boottime() - start) * 1000)));
        const double closeAt = a.exitAfterPeak >= 0 ? boottime() + a.exitAfterPeak : deadline;
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
            }
            if (peakRequested) {
                peakRequested = 0;
                if (a.peakMiB > 0) runPeak(a, rng);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        if (hotFd >= 0) ::close(hotFd);
    } catch (const std::exception& e) {
        logEvent(std::string("ERROR ") + e.what());
        stopping = true;
        if (writer.joinable()) writer.join();
        return 1;
    }
    stopping = true;
    if (writer.joinable()) writer.join();
    logEvent("EXIT");
    return 0;
}
