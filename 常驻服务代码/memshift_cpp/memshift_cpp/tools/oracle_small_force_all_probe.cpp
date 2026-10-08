#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <iomanip>
#include <map>
#include <csignal>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <vector>

// Isolated small-object force_shrink_all diagnostic. This executable is not a
// service and accepts only one 16 MiB self-owned cgroup per invocation.
namespace {
using Clock = std::chrono::steady_clock;
constexpr std::size_t kBytes = 16U * 1024U * 1024U;
constexpr const char* kGroupPrefix = "/dev/memcg/oracle_small_";
constexpr const char* kEndpoint = "memory.force_shrink_all";
constexpr const char* kPayload = "20"; // exactly one two-byte write, no retry

struct Identity {
    pid_t pid = -1;
    std::uint64_t uid = 0, startTicks = 0, rssAnonKiB = 0;
    std::optional<std::uint64_t> vmSwapKiB;
    std::uint64_t minflt = 0, majflt = 0;
    std::string exe;
};

struct WriterIdentity {
    pid_t pid = -1;
    std::uint64_t uid = 0, startTicks = 0;
    std::string exe, cgroup;
};

std::string trim(std::string s) {
    const auto first = s.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    const auto last = s.find_last_not_of(" \t\r\n");
    return s.substr(first, last - first + 1);
}

std::string readFile(const std::string& path, std::size_t limit = 1024U * 1024U) {
    const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) throw std::runtime_error("open " + path + ": " + std::strerror(errno));
    std::string out;
    std::array<char, 8192> buffer{};
    for (;;) {
        const ssize_t n = ::read(fd, buffer.data(), buffer.size());
        if (n == 0) break;
        if (n < 0) {
            const int e = errno; ::close(fd);
            if (e == EINTR) continue;
            throw std::runtime_error("read " + path + ": " + std::strerror(e));
        }
        out.append(buffer.data(), static_cast<std::size_t>(n));
        if (out.size() > limit) { ::close(fd); throw std::runtime_error("oversized file " + path); }
    }
    ::close(fd);
    return out;
}

std::uint64_t number(const std::string& s) {
    if (s.empty() || s.front() == '-') throw std::runtime_error("invalid unsigned value");
    std::size_t used = 0;
    const auto n = std::stoull(s, &used, 10);
    if (used != s.size()) throw std::runtime_error("invalid numeric suffix");
    return n;
}

double bootSeconds() {
    timespec ts{};
    if (::clock_gettime(CLOCK_BOOTTIME, &ts) != 0) throw std::runtime_error("CLOCK_BOOTTIME unavailable");
    return static_cast<double>(ts.tv_sec) + static_cast<double>(ts.tv_nsec) / 1.0e9;
}

std::string realText(double value) {
    std::ostringstream out;
    out << std::setprecision(17) << value;
    return out.str();
}

void sleepUntilBoot(double target) {
    for (;;) {
        const double left = target - bootSeconds();
        if (left <= 0) return;
        const auto nap = std::chrono::duration<double>(std::min(left, 0.050));
        std::this_thread::sleep_for(nap);
    }
}

std::uint64_t procField(const std::string& stat, pid_t expectedPid, int wanted,
                        std::uint64_t* minflt = nullptr, std::uint64_t* majflt = nullptr) {
    std::istringstream h(stat);
    std::string token;
    if (!(h >> token) || number(token) != static_cast<std::uint64_t>(expectedPid))
        throw std::runtime_error("/proc/PID/stat leading PID mismatch");
    const auto close = stat.rfind(')');
    if (close == std::string::npos) throw std::runtime_error("malformed /proc/PID/stat comm");
    std::istringstream fields(stat.substr(close + 1));
    for (int field = 3; field <= 22; ++field) {
        if (!(fields >> token)) throw std::runtime_error("short /proc/PID/stat");
        if (field == 10 && minflt) *minflt = number(token);
        if (field == 12 && majflt) *majflt = number(token);
        if (field == wanted) return number(token);
    }
    throw std::runtime_error("missing /proc/PID/stat field");
}

std::map<std::string, std::uint64_t> statusValues(const std::string& status) {
    std::map<std::string, std::uint64_t> out;
    std::istringstream lines(status);
    std::string line;
    while (std::getline(lines, line)) {
        const auto colon = line.find(':');
        if (colon == std::string::npos) continue;
        const std::string key = line.substr(0, colon);
        std::istringstream vals(line.substr(colon + 1));
        std::string v;
        if (key == "Uid") {
            std::array<std::uint64_t, 4> ids{};
            for (auto& id : ids) if (!(vals >> v)) throw std::runtime_error("malformed status Uid"); else id = number(v);
            if (!std::all_of(ids.begin(), ids.end(), [&](auto id) { return id == ids[0]; }))
                throw std::runtime_error("status Uid values differ");
            out["Uid"] = ids[0];
        } else if (key == "Pid" || key == "RssAnon" || key == "VmSwap") {
            if (!(vals >> v)) continue;
            out[key] = number(v);
        }
    }
    return out;
}

std::vector<pid_t> groupPids(const std::string& group) {
    const auto text = readFile(group + "/cgroup.procs", 4096);
    std::vector<pid_t> result;
    std::istringstream lines(text);
    std::string line;
    while (std::getline(lines, line)) if (!trim(line).empty()) result.push_back(static_cast<pid_t>(number(trim(line))));
    return result;
}

bool memberCgroup(pid_t pid, const std::string& groupName) {
    std::istringstream lines(readFile("/proc/" + std::to_string(pid) + "/cgroup", 16384));
    std::string line;
    bool found = false;
    while (std::getline(lines, line)) {
        const auto a = line.find(':'), b = a == std::string::npos ? a : line.find(':', a + 1);
        if (a == std::string::npos || b == std::string::npos) continue;
        std::istringstream controllers(line.substr(a + 1, b - a - 1));
        std::string c;
        while (std::getline(controllers, c, ',')) {
            if (c == "memory") {
                if (found || line.substr(b + 1) != "/" + groupName) return false;
                found = true;
            }
        }
    }
    return found;
}

std::string exeOf(pid_t pid) {
    std::array<char, 4096> buf{};
    const auto n = ::readlink(("/proc/" + std::to_string(pid) + "/exe").c_str(), buf.data(), buf.size() - 1);
    if (n < 0) throw std::runtime_error("readlink /proc/PID/exe failed");
    return std::string(buf.data(), static_cast<std::size_t>(n));
}

Identity inspect(pid_t pid, const std::string& expectedExe, const std::string& group,
                 const std::string& groupName, bool requireSingleton) {
    const auto before = readFile("/proc/" + std::to_string(pid) + "/stat", 16384);
    const auto status = statusValues(readFile("/proc/" + std::to_string(pid) + "/status", 65536));
    const auto cg = readFile("/proc/" + std::to_string(pid) + "/cgroup", 16384);
    const auto after = readFile("/proc/" + std::to_string(pid) + "/stat", 16384);
    Identity id; id.pid = pid;
    id.startTicks = procField(before, pid, 22, &id.minflt, &id.majflt);
    const auto afterTicks = procField(after, pid, 22);
    if (afterTicks != id.startTicks) throw std::runtime_error("PID start_ticks changed during identity sample");
    (void)cg;
    const auto p = status.find("Pid"), u = status.find("Uid"), r = status.find("RssAnon"), s = status.find("VmSwap");
    if (p == status.end() || u == status.end() || r == status.end() || s == status.end())
        throw std::runtime_error("missing identity/RssAnon/VmSwap status field");
    if (p->second != static_cast<std::uint64_t>(pid)) throw std::runtime_error("status PID mismatch");
    id.uid = u->second; id.rssAnonKiB = r->second;
    id.vmSwapKiB = s->second;
    id.exe = exeOf(pid);
    if (id.exe != expectedExe) throw std::runtime_error("process executable identity mismatch");
    if (!memberCgroup(pid, groupName)) throw std::runtime_error("process memory cgroup mismatch");
    const auto members = groupPids(group);
    if (requireSingleton && (members.size() != 1 || members[0] != pid))
        throw std::runtime_error("cgroup.procs is not exactly the owned PID");
    return id;
}

WriterIdentity inspectWriter(const std::string& expectedExe) {
    const pid_t pid = ::getpid();
    const auto before = readFile("/proc/self/stat", 16384);
    const auto status = statusValues(readFile("/proc/self/status", 65536));
    const auto cgroupBefore = readFile("/proc/self/cgroup", 16384);
    const auto exeBefore = exeOf(pid);
    const auto after = readFile("/proc/self/stat", 16384);
    const auto cgroupAfter = readFile("/proc/self/cgroup", 16384);
    const auto p = status.find("Pid"), u = status.find("Uid");
    if (p == status.end() || u == status.end() || p->second != static_cast<std::uint64_t>(pid))
        throw std::runtime_error("writer PID/UID status unavailable");
    const auto ticks = procField(before, pid, 22);
    if (procField(after, pid, 22) != ticks || cgroupBefore != cgroupAfter || exeBefore != expectedExe)
        throw std::runtime_error("writer identity/cgroup changed during validation");
    bool memoryPathFound = false, writerMemberFound = false;
    std::istringstream cgLines(cgroupBefore);
    std::string cgLine;
    while (std::getline(cgLines, cgLine)) {
        const auto a = cgLine.find(':'), b = a == std::string::npos ? a : cgLine.find(':', a + 1);
        if (a == std::string::npos || b == std::string::npos) continue;
        std::istringstream controllers(cgLine.substr(a + 1, b - a - 1));
        std::string controller; bool hasMemory = false;
        while (std::getline(controllers, controller, ',')) hasMemory = hasMemory || controller == "memory";
        if (!hasMemory) continue;
        if (memoryPathFound) throw std::runtime_error("writer has duplicate memory cgroup membership");
        memoryPathFound = true;
        const auto path = cgLine.substr(b + 1);
        if (path.empty() || path.front() != '/' || path.find("/../") != std::string::npos || path == "/..")
            throw std::runtime_error("unsafe writer memory cgroup path");
        const auto members = groupPids(std::string("/dev/memcg") + path);
        writerMemberFound = std::find(members.begin(), members.end(), pid) != members.end();
    }
    if (!memoryPathFound || !writerMemberFound) throw std::runtime_error("writer is absent from its reported memory cgroup.procs");
    return {pid, u->second, ticks, exeBefore, cgroupBefore};
}

std::uint8_t byteAt(std::size_t i) {
    std::uint64_t x = static_cast<std::uint64_t>(i) ^ 0x6d656d7368696674ULL;
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return static_cast<std::uint8_t>((x ^ (x >> 31)) >> 24);
}

std::string jsonEscape(const std::string& s) {
    std::string out;
    for (const unsigned char c : s) {
        if (c == '"' || c == '\\') { out.push_back('\\'); out.push_back(static_cast<char>(c)); }
        else if (c < 0x20) out += "?";
        else out.push_back(static_cast<char>(c));
    }
    return out;
}

std::string conditionalBoolean(bool conditionalMode, bool value) {
    return !conditionalMode ? "null" : (value ? "true" : "false");
}

void appendLine(const std::string& path, const std::string& line, bool exclusive = false) {
    const int flags = O_WRONLY | O_CREAT | O_CLOEXEC | O_NOFOLLOW | (exclusive ? O_EXCL : O_APPEND);
    const int fd = ::open(path.c_str(), flags, 0600);
    if (fd < 0) throw std::runtime_error("open output " + path + ": " + std::strerror(errno));
    const std::string text = line + "\n";
    const ssize_t n = ::write(fd, text.data(), text.size());
    const int e = errno;
    if (n == static_cast<ssize_t>(text.size()) && ::fsync(fd) == 0) { ::close(fd); return; }
    ::close(fd);
    throw std::runtime_error("persist output failed: " + std::string(std::strerror(n < 0 ? e : EIO)));
}

void pipeWrite(int fd, const std::string& line) {
    const std::string text = line + "\n";
    std::size_t done = 0;
    while (done < text.size()) {
        const ssize_t n = ::write(fd, text.data() + done, text.size() - done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) throw std::runtime_error("control pipe write failed");
        done += static_cast<std::size_t>(n);
    }
}

std::string pipeReadLine(int fd) {
    std::string line;
    char c = '\0';
    while (line.size() < 4096) {
        const ssize_t n = ::read(fd, &c, 1);
        if (n < 0 && errno == EINTR) continue;
        if (n != 1) throw std::runtime_error("control pipe closed");
        if (c == '\n') return line;
        line.push_back(c);
    }
    throw std::runtime_error("oversized control message");
}

struct MappingSample { std::uint64_t resident = 0, pages = 0, minflt = 0, majflt = 0; double begin = 0, end = 0; };
struct FaultSample { std::uint64_t minflt = 0, majflt = 0; };
struct EmittedSample { Identity identity; MappingSample mapping; };
FaultSample processFaults() {
    rusage usage{};
    if (::getrusage(RUSAGE_SELF, &usage) != 0) throw std::runtime_error("getrusage failed");
    return {static_cast<std::uint64_t>(usage.ru_minflt), static_cast<std::uint64_t>(usage.ru_majflt)};
}
MappingSample sampleMapping(const volatile std::uint8_t* mem, std::size_t size) {
    MappingSample s; s.begin = bootSeconds();
    const long ps = ::sysconf(_SC_PAGESIZE);
    if (ps <= 0) throw std::runtime_error("page size unavailable");
    const auto page = static_cast<std::size_t>(ps);
    s.pages = (size + page - 1) / page;
    std::vector<unsigned char> vec(static_cast<std::size_t>(s.pages));
    if (::mincore(const_cast<std::uint8_t*>(mem), size, vec.data()) != 0) throw std::runtime_error("mincore failed");
    for (const auto v : vec) if ((v & 1U) != 0) ++s.resident;
    const auto faults = processFaults();
    s.minflt = faults.minflt; s.majflt = faults.majflt;
    s.end = bootSeconds();
    return s;
}

void childMain(int readFd, int writeFd, const std::string& group, const std::string& groupName,
               const std::string& expectedExe, bool extraRead) {
    void* mapping = MAP_FAILED;
    try {
        if (pipeReadLine(readFd) != "START") throw std::runtime_error("parent did not confirm cgroup placement");
        mapping = ::mmap(nullptr, kBytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (mapping == MAP_FAILED) throw std::runtime_error("mmap failed");
        auto* mem = static_cast<volatile std::uint8_t*>(mapping);
        const long psLong = ::sysconf(_SC_PAGESIZE);
        if (psLong <= 0) throw std::runtime_error("page size unavailable");
        const auto ps = static_cast<std::size_t>(psLong);
        std::uint64_t expectedHash = 1469598103934665603ULL;
        for (std::size_t i = 0; i < kBytes; i += ps) {
            const auto end = std::min(kBytes, i + ps);
            for (std::size_t j = i; j < end; ++j) {
                const auto b = byteAt(j); mem[j] = b;
                expectedHash = (expectedHash ^ b) * 1099511628211ULL;
            }
        }
        std::uint64_t extraHash = 0;
        if (extraRead) {
            extraHash = 1469598103934665603ULL;
            for (std::size_t i = 0; i < kBytes; ++i) extraHash = (extraHash ^ mem[i]) * 1099511628211ULL;
        }
        const auto id = inspect(::getpid(), expectedExe, group, groupName, true);
        pipeWrite(writeFd, "READY " + std::to_string(id.pid) + " " + std::to_string(id.uid) + " " +
                  std::to_string(id.startTicks) + " " + std::to_string(id.rssAnonKiB) + " " +
                  std::to_string(expectedHash) + " " + std::to_string(extraHash));
        for (;;) {
            const auto cmd = pipeReadLine(readFd);
            if (cmd.rfind("SAMPLE ", 0) == 0) {
                const auto label = cmd.substr(7);
                const auto s = sampleMapping(mem, kBytes);
                pipeWrite(writeFd, "MAP " + label + " " + realText(s.begin) + " " +
                    realText(s.end) + " " + std::to_string(s.resident) + " " +
                    std::to_string(s.pages) + " " + std::to_string(s.minflt) + " " + std::to_string(s.majflt));
            } else if (cmd == "SELF_ACTION") {
                (void)inspect(::getpid(), expectedExe, group, groupName, true);
                const int fd = ::open((group + "/" + kEndpoint).c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
                if (fd < 0) { pipeWrite(writeFd, "ACTION_ERROR open " + std::to_string(errno)); continue; }
                (void)inspect(::getpid(), expectedExe, group, groupName, true);
                errno = 0;
                const double writeBeginBoot = bootSeconds();
                const ssize_t n = ::write(fd, kPayload, 2); // one call; EINTR is not retried
                const int e = errno;
                const double writeReturnBoot = bootSeconds();
                ::close(fd);
                try {
                    (void)inspect(::getpid(), expectedExe, group, groupName, true);
                    pipeWrite(writeFd, "ACTION_RESULT " + std::to_string(n) + " " + std::to_string(n == 2 ? 0 : e) + " " +
                        realText(writeBeginBoot) + " " + realText(writeReturnBoot));
                } catch (...) {
                    pipeWrite(writeFd, "ACTION_POST_IDENTITY_UNKNOWN " + std::to_string(n) + " " + std::to_string(e) + " " +
                        realText(writeBeginBoot) + " " + realText(writeReturnBoot));
                }
            } else if (cmd == "VERIFY_EXIT") {
                std::uint64_t actualHash = 1469598103934665603ULL, bad = 0;
                const auto faultsBefore = processFaults();
                const auto begin = bootSeconds();
                for (std::size_t i = 0; i < kBytes; ++i) {
                    const auto b = mem[i]; if (b != byteAt(i)) ++bad;
                    actualHash = (actualHash ^ b) * 1099511628211ULL;
                }
                const auto end = bootSeconds();
                const auto faultsAfter = processFaults();
                const auto postCrc = sampleMapping(mem, kBytes);
                pipeWrite(writeFd, "CRC " + realText(begin) + " " + realText(end) + " " +
                    std::to_string(expectedHash) + " " + std::to_string(actualHash) + " " + std::to_string(bad) + " " +
                    std::to_string(faultsBefore.minflt) + " " + std::to_string(faultsAfter.minflt) + " " +
                    std::to_string(faultsBefore.majflt) + " " + std::to_string(faultsAfter.majflt) + " " +
                    realText(postCrc.begin) + " " + realText(postCrc.end) + " " +
                    std::to_string(postCrc.resident) + " " + std::to_string(postCrc.pages) + " " +
                    std::to_string(postCrc.minflt) + " " + std::to_string(postCrc.majflt));
                if (pipeReadLine(readFd) != "CRC_SAVED") throw std::runtime_error("parent did not persist CRC evidence");
                ::munmap(mapping, kBytes); mapping = MAP_FAILED;
                _exit(bad == 0 && actualHash == expectedHash ? 0 : 4);
            } else throw std::runtime_error("unknown parent command");
        }
    } catch (const std::exception& e) {
        if (mapping != MAP_FAILED) ::munmap(mapping, kBytes);
        pipeWrite(writeFd, "CHILD_ERROR " + jsonEscape(e.what()));
        _exit(5);
    }
}

std::string groupNameFor(const std::string& runId, const std::string& cell) {
    if (runId.empty() || runId.size() > 32 || cell.empty()) throw std::runtime_error("invalid run id/cell");
    const auto safe = [](char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
        (c >= '0' && c <= '9') || c == '_' || c == '-'; };
    if (!std::all_of(runId.begin(), runId.end(), safe)) throw std::runtime_error("unsafe run id");
    const std::string groupName = "oracle_small_" + runId + "_" + cell;
    if (!std::all_of(groupName.begin(), groupName.end(), safe)) throw std::runtime_error("unsafe group name");
    return groupName;
}

std::string memcgSnapshot(const std::string& group) {
    std::ostringstream out;
    out << "memory.current=" << jsonEscape(trim(readFile(group + "/memory.current"))) << ";";
    out << "memory.stat=" << jsonEscape(trim(readFile(group + "/memory.stat"))) << ";";
    try { out << "memory.events=" << jsonEscape(trim(readFile(group + "/memory.events"))) << ";"; }
    catch (...) { out << "memory.events=unknown;"; }
    return out.str();
}

std::string systemSnapshot() {
    std::ostringstream out;
    bool totalKnown = false, availableKnown = false, swapTotalKnown = false, swapKnown = false;
    try {
        std::istringstream lines(readFile("/proc/meminfo", 65536));
        std::string line;
        while (std::getline(lines, line)) {
            if (line.rfind("MemTotal:", 0) == 0) { out << trim(line) << ";"; totalKnown = true; }
            if (line.rfind("MemAvailable:", 0) == 0) { out << trim(line) << ";"; availableKnown = true; }
            if (line.rfind("SwapTotal:", 0) == 0) { out << trim(line) << ";"; swapTotalKnown = true; }
            if (line.rfind("SwapFree:", 0) == 0) { out << trim(line) << ";"; swapKnown = true; }
        }
    } catch (...) { out << "meminfo=unknown;"; }
    if (!totalKnown || !availableKnown || !swapTotalKnown || !swapKnown)
        throw std::runtime_error("system MemTotal/MemAvailable/SwapTotal/SwapFree sample incomplete");
    try { out << "zram_mm_stat=" << jsonEscape(trim(readFile("/sys/block/zram0/mm_stat", 4096))) << ";"; }
    catch (...) { out << "zram_mm_stat=unknown;"; }
    return out.str();
}

EmittedSample emitSample(const std::string& outPath, const std::string& group, const std::string& groupName,
                pid_t pid, const std::string& expectedExe, int toChild, int fromChild,
                const std::string& runId, const std::string& cell, const std::string& label) {
    const double begin = bootSeconds();
    const auto id = inspect(pid, expectedExe, group, groupName, true);
    pipeWrite(toChild, "SAMPLE " + label);
    const auto m = pipeReadLine(fromChild);
    if (m.rfind("MAP " + label + " ", 0) != 0) throw std::runtime_error("mapping sample missing");
    std::istringstream mapFields(m);
    std::string mapTag, mapLabel; double mapBegin = 0, mapEnd = 0;
    std::uint64_t resident = 0, pages = 0, minflt = 0, majflt = 0;
    if (!(mapFields >> mapTag >> mapLabel >> mapBegin >> mapEnd >> resident >> pages >> minflt >> majflt) ||
        mapTag != "MAP" || mapLabel != label || pages == 0 || resident > pages)
        throw std::runtime_error("incomplete mapping/mincore/fault sample");
    const auto cg = memcgSnapshot(group);
    const auto sys = systemSnapshot();
    const double end = bootSeconds();
    std::ostringstream row;
    row << std::setprecision(17) << "{\"kind\":\"sample\",\"run_id\":\"" << runId << "\",\"cell\":\"" << cell
        << "\",\"label\":\"" << label << "\",\"host_sample_begin_boottime_s\":" << begin
        << ",\"host_sample_end_boottime_s\":" << end << ",\"pid\":" << pid
        << ",\"uid\":" << id.uid << ",\"start_ticks\":" << id.startTicks
        << ",\"rss_anon_kib\":" << id.rssAnonKiB << ",\"vmswap_kib\":"
        << (id.vmSwapKiB ? std::to_string(*id.vmSwapKiB) : "null")
        << ",\"vmswap_status\":\"" << (id.vmSwapKiB ? "observed" : "unknown") << "\""
        << ",\"minflt\":" << id.minflt << ",\"majflt\":" << id.majflt
        << ",\"mapping_sample\":{\"begin_boottime_s\":" << mapBegin << ",\"end_boottime_s\":" << mapEnd
        << ",\"resident_pages\":" << resident << ",\"total_pages\":" << pages << ",\"minflt\":" << minflt
        << ",\"majflt\":" << majflt << "},\"memcg\":\"" << cg << "\",\"system\":\"" << sys << "\"}";
    appendLine(outPath, row.str());
    return {id, MappingSample{resident, pages, minflt, majflt, mapBegin, mapEnd}};
}

bool sameTargetIdentity(const Identity& a, const Identity& b) {
    return a.pid == b.pid && a.uid == b.uid && a.startTicks == b.startTicks && a.exe == b.exe;
}

bool positiveResidentFeedback(const EmittedSample& before, const std::vector<EmittedSample>& after) {
    if (after.empty() || !before.identity.vmSwapKiB || before.mapping.pages == 0 ||
        before.mapping.resident == 0)
        return false;
    for (const auto& sample : after) {
        if (!sameTargetIdentity(before.identity, sample.identity) || !sample.identity.vmSwapKiB ||
            sample.mapping.pages != before.mapping.pages || sample.mapping.resident >= before.mapping.resident)
            return false;
    }
    const auto& final = after.back();
    return final.identity.rssAnonKiB < before.identity.rssAnonKiB &&
        final.mapping.resident < before.mapping.resident &&
        final.identity.minflt >= before.identity.minflt && final.identity.majflt >= before.identity.majflt;
}

bool completeFeedback(const EmittedSample& baseline, const std::vector<EmittedSample>& samples) {
    if (samples.empty() || !baseline.identity.vmSwapKiB || baseline.mapping.pages == 0 ||
        baseline.mapping.end < baseline.mapping.begin)
        return false;
    double lastEnd = baseline.mapping.end;
    for (const auto& sample : samples) {
        if (!sameTargetIdentity(baseline.identity, sample.identity) || !sample.identity.vmSwapKiB ||
            sample.mapping.pages != baseline.mapping.pages || sample.mapping.resident > sample.mapping.pages ||
            sample.mapping.begin < lastEnd || sample.mapping.end < sample.mapping.begin)
            return false;
        lastEnd = sample.mapping.end;
    }
    return true;
}

int feedbackSelfTest() {
    if (conditionalBoolean(false,true)!="null" || conditionalBoolean(true,true)!="true" ||
        conditionalBoolean(true,false)!="false") return 7;
    const Identity baseId{101, 0, 5001, 17100, 0, 100, 3, "/probe"};
    const EmittedSample baseline{baseId, MappingSample{4096, 4096, 101, 3, 10.0, 10.1}};
    std::vector<EmittedSample> positive;
    for (int i = 0; i < 4; ++i) {
        Identity id{101, 0, 5001, 13916, 3184, static_cast<std::uint64_t>(102 + i), 4, "/probe"};
        const double begin = 11.0 + i;
        positive.push_back({id, MappingSample{3300, 4096, static_cast<std::uint64_t>(102 + i), 4, begin, begin + 0.1}});
    }
    if (!completeFeedback(baseline, positive) || !positiveResidentFeedback(baseline, positive)) return 1;
    auto noRss = positive; noRss.back().identity.rssAnonKiB = baseline.identity.rssAnonKiB;
    if (positiveResidentFeedback(baseline, noRss)) return 2;
    auto noMincore = positive; noMincore.back().mapping.resident = baseline.mapping.resident;
    if (positiveResidentFeedback(baseline, noMincore)) return 3;
    auto missingSwap = positive; missingSwap.back().identity.vmSwapKiB.reset();
    if (completeFeedback(baseline, missingSwap) || positiveResidentFeedback(baseline, missingSwap)) return 4;
    auto wrongPid = positive; wrongPid.back().identity.pid += 1;
    if (completeFeedback(baseline, wrongPid) || positiveResidentFeedback(baseline, wrongPid)) return 5;
    auto wrongTicks = positive; wrongTicks.back().identity.startTicks += 1;
    if (completeFeedback(baseline, wrongTicks) || positiveResidentFeedback(baseline, wrongTicks)) return 6;
    std::cout << "feedback gate checks passed: positive, zero-RSS, zero-mincore, missing-swap, PID-change, start-ticks-change\n";
    return 0;
}

bool writeAction(const std::string& group, const std::string& groupName, pid_t target,
                 const std::string& expectedExe, bool self, int toChild, int fromChild,
                 const std::string& actionId, const std::string& ledgerPath,
                 const std::string& runId, const std::string& cell,
                 double& syscallBeginBoot, double& syscallReturnBoot,
                 const std::string& payload = kPayload) {
    auto id = inspect(target, expectedExe, group, groupName, true);
    const WriterIdentity writerBefore = self
        ? WriterIdentity{target, id.uid, id.startTicks, expectedExe,
            readFile("/proc/" + std::to_string(target) + "/cgroup", 16384)}
        : inspectWriter(expectedExe);
    const double trigger = bootSeconds();
    std::ostringstream reserve;
    reserve << std::setprecision(17) << "{\"action_id\":\"" << actionId << "\",\"row\":\"reserved\",\"run_id\":\""
        << runId << "\",\"cell\":\"" << cell << "\",\"writer\":\"" << (self ? "target_self" : "external_controller")
        << "\",\"pid\":" << target << ",\"uid\":" << id.uid << ",\"start_ticks\":" << id.startTicks
        << ",\"writer_pid\":" << writerBefore.pid << ",\"writer_uid\":" << writerBefore.uid
        << ",\"writer_start_ticks\":" << writerBefore.startTicks << ",\"writer_exe\":\""
        << jsonEscape(writerBefore.exe) << "\",\"writer_cgroup\":\"" << jsonEscape(writerBefore.cgroup) << "\""
        << ",\"trigger_boottime_s\":" << trigger << ",\"payload\":\"" << jsonEscape(payload) << "\",\"write_calls\":1}";
    appendLine(ledgerPath, reserve.str()); // durable reservation before one write call
    const auto start = Clock::now();
    ssize_t n = -1; int err = 0; bool submitted = false, postIdentityVerified = true;
    std::optional<WriterIdentity> writerAfter;
    int actionFd = -1;
    std::string stage = "write";
    try {
        // Open, then repeat all target identity and singleton checks directly before write.
        actionFd = ::open((group + "/" + kEndpoint).c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
        if (actionFd < 0) throw std::runtime_error("open endpoint errno=" + std::to_string(errno));
        const auto latest = inspect(target, expectedExe, group, groupName, true);
        if (latest.startTicks != id.startTicks || latest.uid != id.uid) {
            throw std::runtime_error("target identity changed prewrite");
        }
        if (self) {
            stage = "self_write";
            pipeWrite(toChild, "SELF_ACTION");
            const auto reply = pipeReadLine(fromChild);
            const bool postUnknown = reply.rfind("ACTION_POST_IDENTITY_UNKNOWN ", 0) == 0;
            if (reply.rfind("ACTION_RESULT ", 0) != 0 && !postUnknown)
                throw std::runtime_error("self writer action result unknown: " + reply);
            const auto prefix = postUnknown ? std::string("ACTION_POST_IDENTITY_UNKNOWN ").size() : std::string("ACTION_RESULT ").size();
            std::istringstream vals(reply.substr(prefix)); vals >> n >> err >> syscallBeginBoot >> syscallReturnBoot;
            if (!vals) throw std::runtime_error("malformed self write result");
            submitted = true; postIdentityVerified = !postUnknown;
            if (postIdentityVerified) {
                try {
                    const auto targetAfter = inspect(target, expectedExe, group, groupName, true);
                    writerAfter = WriterIdentity{targetAfter.pid, targetAfter.uid, targetAfter.startTicks,
                        targetAfter.exe, readFile("/proc/" + std::to_string(target) + "/cgroup", 16384)};
                    postIdentityVerified = targetAfter.pid == writerBefore.pid && targetAfter.uid == writerBefore.uid &&
                        targetAfter.startTicks == writerBefore.startTicks && targetAfter.exe == writerBefore.exe &&
                        writerAfter->cgroup == writerBefore.cgroup;
                } catch (...) { postIdentityVerified = false; }
            }
        } else {
            const auto writerLatest = inspectWriter(expectedExe);
            if (writerLatest.pid != writerBefore.pid || writerLatest.uid != writerBefore.uid ||
                writerLatest.startTicks != writerBefore.startTicks || writerLatest.exe != writerBefore.exe ||
                writerLatest.cgroup != writerBefore.cgroup)
                throw std::runtime_error("external writer identity/cgroup changed prewrite");
            errno = 0;
            syscallBeginBoot = bootSeconds();
            n = ::write(actionFd, payload.data(), payload.size()); // one call only; never retry EINTR
            const int writeErrno = errno;
            syscallReturnBoot = bootSeconds();
            err = n == 2 ? 0 : writeErrno;
            submitted = true;
            try {
                const auto writerCheck = inspectWriter(expectedExe);
                const auto targetAfter = inspect(target, expectedExe, group, groupName, true);
                writerAfter = writerCheck;
                postIdentityVerified = writerCheck.pid == writerBefore.pid && writerCheck.uid == writerBefore.uid &&
                    writerCheck.startTicks == writerBefore.startTicks && writerCheck.exe == writerBefore.exe &&
                    writerCheck.cgroup == writerBefore.cgroup && targetAfter.uid == id.uid && targetAfter.startTicks == id.startTicks;
            } catch (...) { postIdentityVerified = false; }
        }
        ::close(actionFd); actionFd = -1;
    } catch (const std::exception& e) {
        if (actionFd >= 0) ::close(actionFd);
        const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() - start).count();
        std::ostringstream result;
        result << std::setprecision(17) << "{\"action_id\":\"" << actionId << "\",\"row\":\"result\",\"status\":\""
            << (submitted ? "write_submitted_feedback_unknown" : "unknown_or_failed") << "\",\"stage\":\""
            << stage << "\",\"error\":\"" << jsonEscape(e.what()) << "\",\"write_length\":"
        << (submitted ? std::to_string(n) : "null") << ",\"errno\":"
            << (submitted ? std::to_string(err) : "null") << ",\"syscall_begin_boottime_s\":"
            << (submitted ? std::to_string(syscallBeginBoot) : "null") << ",\"syscall_return_boottime_s\":"
            << (submitted ? std::to_string(syscallReturnBoot) : "null") << ",\"elapsed_us\":" << elapsed << "}";
        appendLine(ledgerPath, result.str());
        return false;
    }
    const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() - start).count();
    const bool accepted = n == 2 && err == 0 && postIdentityVerified;
    std::ostringstream result;
    result << std::setprecision(17) << "{\"action_id\":\"" << actionId << "\",\"row\":\"result\",\"status\":\""
        << (n != 2 || err != 0 ? "write_failed_or_partial_no_retry" :
            (postIdentityVerified ? "write_accepted_effect_unknown" : "write_accepted_identity_unknown"))
        << "\",\"write_length\":" << n << ",\"errno\":" << err
        << ",\"post_identity_verified\":" << (postIdentityVerified ? "true" : "false")
        << ",\"writer_after_pid\":" << (writerAfter ? std::to_string(writerAfter->pid) : "null")
        << ",\"writer_after_uid\":" << (writerAfter ? std::to_string(writerAfter->uid) : "null")
        << ",\"writer_after_start_ticks\":" << (writerAfter ? std::to_string(writerAfter->startTicks) : "null")
        << ",\"writer_after_exe\":\"" << (writerAfter ? jsonEscape(writerAfter->exe) : "unknown")
        << "\",\"writer_after_cgroup\":\"" << (writerAfter ? jsonEscape(writerAfter->cgroup) : "unknown") << "\""
        << ",\"syscall_begin_boottime_s\":" << syscallBeginBoot << ",\"syscall_return_boottime_s\":" << syscallReturnBoot
        << ",\"elapsed_us\":" << elapsed << "}";
    appendLine(ledgerPath, result.str());
    return accepted;
}

int runCell(const std::string& runId, const std::string& cell, const std::string& outDir,
            bool twoStep = false, bool stepUp = false) {
    const bool self = cell.rfind("self_", 0) == 0;
    const bool extraRead = cell.size() >= 10 && cell.substr(cell.size() - 10) == "extra_read";
    const bool validCell = cell == "self_fill" || cell == "self_extra_read" ||
        cell == "external_fill" || cell == "external_extra_read" ||
        ((twoStep || stepUp) && cell == "external_two_step") ||
        (stepUp && cell == "external_step_up");
    if (!validCell) throw std::runtime_error("cell must be one of the four fixed matrix names or an authorized small-object mode");
    if ((twoStep || stepUp) && (self || extraRead ||
        (twoStep && cell != "external_two_step") || (stepUp && cell != "external_step_up")))
        throw std::runtime_error("conditional two-action modes are restricted to a fresh external-writer fill-only object");
    const std::string outPrefix = "/data/local/tmp/";
    if (outDir.rfind(outPrefix, 0) != 0 || outDir.find("..") != std::string::npos ||
        outDir.find(' ') != std::string::npos || outDir.find('\\') != std::string::npos)
        throw std::runtime_error("output directory is outside the safe /data/local/tmp path policy");
    struct stat st{};
    if (::lstat(outDir.c_str(), &st) != 0 || !S_ISDIR(st.st_mode) || S_ISLNK(st.st_mode))
        throw std::runtime_error("output directory must be a real directory");
    const auto name = groupNameFor(runId, cell);
    const std::string group = std::string(kGroupPrefix) + runId + "_" + cell;
    if (group != "/dev/memcg/" + name) throw std::runtime_error("unsafe group construction");
    struct stat existing{};
    if (::lstat(group.c_str(), &existing) == 0 || errno != ENOENT) throw std::runtime_error("cgroup path exists or cannot be checked; refusing reuse");
    const auto executable = exeOf(::getpid());
    if (::mkdir(group.c_str(), 0755) != 0) throw std::runtime_error("mkdir new cgroup failed: " + std::string(std::strerror(errno)));
    const std::string outPath = outDir + "/" + runId + "_" + cell + ".jsonl";
    const std::string ledgerPath = outDir + "/" + runId + "_" + cell + "_actions.jsonl";
    bool childStarted = false, cleaned = false;
    int fds[2]{}; pid_t child = -1;
    try {
        appendLine(outPath, "{\"kind\":\"begin\",\"run_id\":\"" + runId + "\",\"cell\":\"" + cell +
            "\",\"bytes\":16777216,\"fill\":\"deterministic_private_anon\",\"extra_read\":" + (extraRead ? "true" : "false") +
            ",\"mode\":\"" + (stepUp ? "conditional_20_then_30_external" : (twoStep ? "conditional_two_step_external" : "single_action_matrix")) + "\"" +
            ",\"group\":\"" + group + "\",\"exe\":\"" + jsonEscape(executable) + "\",\"action_payload\":\"" +
            (stepUp ? "20_then_30" : "20") + "\"}", true);
        if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) throw std::runtime_error("socketpair failed");
        child = ::fork();
        if (child < 0) throw std::runtime_error("fork failed");
        if (child == 0) {
            ::close(fds[0]);
            childMain(fds[1], fds[1], group, name, executable, extraRead);
            _exit(5);
        }
        childStarted = true; ::close(fds[1]); fds[1] = -1;
        const auto pidText = std::to_string(child);
        const int cgfd = ::open((group + "/cgroup.procs").c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
        if (cgfd < 0) throw std::runtime_error("open new cgroup.procs failed");
        const ssize_t moved = ::write(cgfd, pidText.data(), pidText.size());
        const int moveErr = errno; ::close(cgfd);
        if (moved != static_cast<ssize_t>(pidText.size())) throw std::runtime_error("move sole child to cgroup failed errno=" + std::to_string(moveErr));
        pipeWrite(fds[0], "START");
        const auto ready = pipeReadLine(fds[0]);
        if (ready.rfind("READY ", 0) != 0) throw std::runtime_error("owned object did not become ready: " + ready);
        pid_t readyPid = -1; std::uint64_t readyUid = 0, readyTicks = 0, readyRss = 0, expectedHash = 0, extraHash = 0;
        {
            std::istringstream vals(ready.substr(6));
            if (!(vals >> readyPid >> readyUid >> readyTicks >> readyRss >> expectedHash >> extraHash) || readyPid != child)
                throw std::runtime_error("malformed child readiness/identity checksum record");
        }
        if (extraRead && extraHash != expectedHash) throw std::runtime_error("extra-read payload checksum mismatch");
        const auto identity = inspect(child, executable, group, name, true);
        if (static_cast<pid_t>(readyPid) != identity.pid || readyUid != identity.uid || readyTicks != identity.startTicks ||
            readyRss != identity.rssAnonKiB)
            throw std::runtime_error("child readiness identity/RSS changed before initial sample");
        const double readyAt = bootSeconds();
        appendLine(outPath, "{\"kind\":\"ready\",\"boottime_s\":" + realText(readyAt) +
            ",\"pid\":" + std::to_string(identity.pid) + ",\"uid\":" + std::to_string(identity.uid) +
            ",\"start_ticks\":" + std::to_string(identity.startTicks) + ",\"rss_anon_kib\":" +
            std::to_string(identity.rssAnonKiB) + ",\"vmswap_kib\":" +
            (identity.vmSwapKiB ? std::to_string(*identity.vmSwapKiB) : "null") +
            ",\"vmswap_status\":\"" + (identity.vmSwapKiB ? "observed" : "unknown") + "\"" +
            ",\"expected_fnv64\":" + std::to_string(expectedHash) + ",\"extra_read_fnv64\":" +
            (extraRead ? std::to_string(extraHash) : "null") +
            ",\"extra_read\":" + (extraRead ? "true" : "false") + "}");

        // Same fixed post-fill wait. The optional extra-read is completed before
        // READY; the separate two-step mode keeps its one object throughout.
        std::this_thread::sleep_for(std::chrono::seconds(5));
        const auto prewrite = emitSample(outPath, group, name, child, executable, fds[0], fds[0], runId, cell, "prewrite");
        std::vector<std::string> actionIds;
        std::vector<EmittedSample> lastActionSamples;
        bool firstFeedbackPositive = false;
        bool secondFeedbackPositive = false;
        std::string secondDecision = "not_applicable_single_action";
        auto runOneAction = [&](int ordinal, const std::string& labelPrefix,
                                const EmittedSample& baseline, bool requireCompleteFeedback,
                                const std::string& payload) -> std::vector<EmittedSample> {
            const std::string actionId = runId + "_" + cell + "_a" + std::to_string(ordinal);
            double syscallBeginBoot = 0, syscallReturnBoot = 0;
            if (!writeAction(group, name, child, executable, self, fds[0], fds[0], actionId, ledgerPath,
                             runId, cell, syscallBeginBoot, syscallReturnBoot, payload))
                throw std::runtime_error("external write was partial/unknown or identity changed; stopping this object");
            actionIds.push_back(actionId);
            appendLine(outPath, "{\"kind\":\"action_return\",\"ordinal\":" + std::to_string(ordinal) +
                ",\"syscall_begin_boottime_s\":" + realText(syscallBeginBoot) +
                ",\"syscall_return_boottime_s\":" + realText(syscallReturnBoot) +
                ",\"action_id\":\"" + actionId + "\",\"meaning\":\"write accepted only; measured sample follows\"}");
            std::vector<EmittedSample> observed;
            try {
                observed.push_back(emitSample(outPath, group, name, child, executable, fds[0], fds[0], runId, cell, labelPrefix + "immediate"));
                sleepUntilBoot(syscallReturnBoot + 0.250);
                observed.push_back(emitSample(outPath, group, name, child, executable, fds[0], fds[0], runId, cell, labelPrefix + "250ms"));
                sleepUntilBoot(syscallReturnBoot + 1.0);
                observed.push_back(emitSample(outPath, group, name, child, executable, fds[0], fds[0], runId, cell, labelPrefix + "1s"));
                sleepUntilBoot(syscallReturnBoot + 5.0);
                observed.push_back(emitSample(outPath, group, name, child, executable, fds[0], fds[0], runId, cell, labelPrefix + "5s"));
            } catch (const std::exception& e) {
                if (requireCompleteFeedback) throw;
                appendLine(outPath, "{\"kind\":\"feedback_error\",\"action_id\":\"" + actionId +
                    "\",\"error\":\"" + jsonEscape(e.what()) + "\",\"further_actions\":false}");
                return observed;
            }
            if (requireCompleteFeedback && !completeFeedback(baseline, observed))
                throw std::runtime_error("post-write feedback incomplete or target identity changed; no further action allowed");
            return observed;
        };
        lastActionSamples = runOneAction(1, (twoStep || stepUp) ? "a1_" : "", prewrite, true, kPayload);
        std::size_t sampleCount = 1U + lastActionSamples.size();
        if (twoStep || stepUp) {
            firstFeedbackPositive = positiveResidentFeedback(prewrite, lastActionSamples);
            // Keep an owning copy: runOneAction(2, ...) replaces the sample
            // vector, so a reference into it would dangle during later review.
            const auto firstEnd = lastActionSamples.back();
            appendLine(outPath, "{\"kind\":\"feedback_gate\",\"action_id\":\"" + actionIds.back() +
                "\",\"complete\":true,\"eligible_for_second_action\":" + (firstFeedbackPositive ? "true" : "false") +
                ",\"baseline_rss_anon_kib\":" + std::to_string(prewrite.identity.rssAnonKiB) +
                ",\"observed_rss_anon_kib\":" + std::to_string(firstEnd.identity.rssAnonKiB) +
                ",\"baseline_vmswap_kib\":" + std::to_string(*prewrite.identity.vmSwapKiB) +
                ",\"observed_vmswap_kib\":" + std::to_string(*firstEnd.identity.vmSwapKiB) +
                ",\"baseline_mapping_resident_pages\":" + std::to_string(prewrite.mapping.resident) +
                ",\"observed_mapping_resident_pages\":" + std::to_string(firstEnd.mapping.resident) +
                ",\"baseline_minflt\":" + std::to_string(prewrite.identity.minflt) +
                ",\"observed_minflt\":" + std::to_string(firstEnd.identity.minflt) +
                ",\"baseline_majflt\":" + std::to_string(prewrite.identity.majflt) +
                ",\"observed_majflt\":" + std::to_string(firstEnd.identity.majflt) + "}");
            if (firstFeedbackPositive) {
                secondDecision = "executed_after_positive_complete_feedback";
                lastActionSamples = runOneAction(2, "a2_", firstEnd, false, stepUp ? "30" : kPayload);
                sampleCount += lastActionSamples.size();
                const bool secondFeedbackComplete = lastActionSamples.size() == 4 && completeFeedback(firstEnd, lastActionSamples);
                secondFeedbackPositive = secondFeedbackComplete && positiveResidentFeedback(firstEnd, lastActionSamples);
                if (!secondFeedbackComplete) secondDecision = "second_action_feedback_unknown_no_further_actions";
                const EmittedSample* secondEnd = lastActionSamples.empty() ? nullptr : &lastActionSamples.back();
                appendLine(outPath, "{\"kind\":\"feedback_gate\",\"action_id\":\"" + actionIds.back() +
                    "\",\"complete\":" + (secondFeedbackComplete ? "true" : "false") +
                    ",\"eligible_for_further_action\":false,\"positive_resident_feedback\":" +
                    (secondFeedbackPositive ? "true" : "false") + ",\"baseline_rss_anon_kib\":" +
                    std::to_string(firstEnd.identity.rssAnonKiB) + ",\"observed_rss_anon_kib\":" +
                    (secondEnd ? std::to_string(secondEnd->identity.rssAnonKiB) : "null") + ",\"baseline_mapping_resident_pages\":" +
                    std::to_string(firstEnd.mapping.resident) + ",\"observed_mapping_resident_pages\":" +
                    (secondEnd ? std::to_string(secondEnd->mapping.resident) : "null") + "}");
            } else {
                secondDecision = "skipped_first_action_no_positive_rss_and_mapping_feedback";
                appendLine(outPath, "{\"kind\":\"action_skipped\",\"reason\":\"first_action_no_positive_rss_and_mapping_feedback\",\"no_retry\":true}");
            }
        }
        pipeWrite(fds[0], "VERIFY_EXIT");
        const auto crc = pipeReadLine(fds[0]);
        if (crc.rfind("CRC ", 0) != 0) throw std::runtime_error("final CRC unavailable: " + crc);
        std::istringstream crcFields(crc);
        std::string crcTag;
        double crcBegin = 0, crcEnd = 0, mincoreBegin = 0, mincoreEnd = 0;
        std::uint64_t crcExpected = 0, crcActual = 0, crcBad = 0, minBefore = 0, minAfter = 0;
        std::uint64_t majBefore = 0, majAfter = 0, resident = 0, pages = 0, minPost = 0, majPost = 0;
        if (!(crcFields >> crcTag >> crcBegin >> crcEnd >> crcExpected >> crcActual >> crcBad >> minBefore >> minAfter >>
              majBefore >> majAfter >> mincoreBegin >> mincoreEnd >> resident >> pages >> minPost >> majPost) ||
            crcTag != "CRC" || crcExpected != expectedHash || crcActual != expectedHash || crcBad != 0 ||
            pages == 0 || resident > pages)
            throw std::runtime_error("final CRC/fault/post-mincore evidence invalid");
        appendLine(outPath, "{\"kind\":\"final_crc\",\"begin_boottime_s\":" + realText(crcBegin) +
            ",\"end_boottime_s\":" + realText(crcEnd) + ",\"expected_fnv64\":" + std::to_string(crcExpected) +
            ",\"actual_fnv64\":" + std::to_string(crcActual) + ",\"mismatched_bytes\":" + std::to_string(crcBad) +
            ",\"minflt_before\":" + std::to_string(minBefore) + ",\"minflt_after\":" + std::to_string(minAfter) +
            ",\"majflt_before\":" + std::to_string(majBefore) + ",\"majflt_after\":" + std::to_string(majAfter) +
            ",\"post_crc_mincore\":{\"begin_boottime_s\":" + realText(mincoreBegin) +
            ",\"end_boottime_s\":" + realText(mincoreEnd) + ",\"resident_pages\":" + std::to_string(resident) +
            ",\"total_pages\":" + std::to_string(pages) + ",\"minflt\":" + std::to_string(minPost) +
            ",\"majflt\":" + std::to_string(majPost) + "}}");
        pipeWrite(fds[0], "CRC_SAVED");
        int status = 0;
        if (::waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
            throw std::runtime_error("owned object exited abnormally after CRC");
        childStarted = false;
        const auto members = groupPids(group);
        if (!members.empty()) throw std::runtime_error("owned group not empty after child exit");
        if (::rmdir(group.c_str()) != 0) throw std::runtime_error("owned group removal failed: " + std::string(std::strerror(errno)));
        cleaned = true;
        appendLine(outPath, "{\"kind\":\"cleanup\",\"group_removed\":true,\"member_count\":0}");
        std::ostringstream complete;
        complete << "{\"kind\":\"complete\",\"run_id\":\"" << runId << "\",\"cell\":\"" << cell
            << "\",\"action_ids\":[";
        for (std::size_t i = 0; i < actionIds.size(); ++i) {
            if (i != 0) complete << ",";
            complete << "\"" << actionIds[i] << "\"";
        }
        complete << "],\"action_count\":" << actionIds.size() << ",\"sample_count\":" << sampleCount
            << ",\"two_step\":" << ((twoStep || stepUp) ? "true" : "false")
            << ",\"step_up_20_to_30\":" << (stepUp ? "true" : "false")
            << ",\"first_feedback_positive\":" << conditionalBoolean(twoStep || stepUp,firstFeedbackPositive)
            << ",\"second_action_decision\":\"" << secondDecision << "\""
            << ",\"second_feedback_positive\":" << conditionalBoolean((twoStep || stepUp) && actionIds.size() == 2,secondFeedbackPositive)
            << ",\"crc_verified\":true,\"group_removed\":true}";
        appendLine(outPath, complete.str());
        std::cout << "__ORACLE_SMALL_COMPLETE_" << runId << "_" << cell << "__=1\n";
        ::close(fds[0]);
        return 0;
    } catch (...) {
        if (childStarted && child > 0) {
            // Best-effort graceful stop only; do not signal another identity.
            try {
                const auto ids = groupPids(group);
                if (ids.size() == 1 && ids[0] == child) ::kill(child, SIGTERM);
            } catch (...) {}
            int status = 0; (void)::waitpid(child, &status, 0);
        }
        if (fds[0] >= 0) ::close(fds[0]); if (fds[1] >= 0) ::close(fds[1]);
        if (!cleaned) {
            try { const auto ids = groupPids(group); if (ids.empty()) (void)::rmdir(group.c_str()); } catch (...) {}
        }
        throw;
    }
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (argc == 2 && std::string(argv[1]) == "--feedback-self-test") return feedbackSelfTest();
        if (argc == 2 && std::string(argv[1]) == "--clock-probe") {
            timespec mono{}, realtime{};
            const double bootBefore = bootSeconds();
            if (::clock_gettime(CLOCK_MONOTONIC, &mono) != 0 || ::clock_gettime(CLOCK_REALTIME, &realtime) != 0)
                throw std::runtime_error("clock domain read failed");
            const double bootAfter = bootSeconds();
            std::cout << std::setprecision(17)
                << "{\"kind\":\"clock_probe\",\"boot_begin_s\":" << bootBefore
                << ",\"monotonic_s\":" << static_cast<double>(mono.tv_sec) + static_cast<double>(mono.tv_nsec) / 1.0e9
                << ",\"realtime_s\":" << static_cast<double>(realtime.tv_sec) + static_cast<double>(realtime.tv_nsec) / 1.0e9
                << ",\"boot_end_s\":" << bootAfter
                << ",\"proc_uptime\":\"" << jsonEscape(trim(readFile("/proc/uptime", 4096)))
                << "\",\"boot_id\":\"" << jsonEscape(trim(readFile("/proc/sys/kernel/random/boot_id", 4096))) << "\"}\n";
            return 0;
        }
        if (argc == 4 && std::string(argv[1]) == "--two-step-external") {
            return runCell(argv[2], "external_two_step", argv[3], true);
        }
        if (argc == 4 && std::string(argv[1]) == "--step-up-external") {
            return runCell(argv[2], "external_step_up", argv[3], false, true);
        }
        if (argc != 4) {
            std::cerr << "Usage: oracle_small_force_all_probe RUN_ID CELL OUTPUT_DIR\n"
                         "CELL: self_fill | self_extra_read | external_fill | external_extra_read\n"
                         "or: oracle_small_force_all_probe --two-step-external RUN_ID OUTPUT_DIR\n"
                         "or: oracle_small_force_all_probe --step-up-external RUN_ID OUTPUT_DIR\n";
            return 2;
        }
        return runCell(argv[1], argv[2], argv[3]);
    } catch (const std::exception& e) {
        std::cerr << "oracle_small_force_all_probe: " << e.what() << "\n";
        return 1;
    }
}
