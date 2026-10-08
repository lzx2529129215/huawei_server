#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/types.h>
#include <unistd.h>
#include <vector>

namespace {
constexpr const char* AllowedGroupPrefix = "/dev/memcg/memshift_probe_";
constexpr std::uint64_t MaxPayload = 4096;
constexpr std::uint64_t MaxCurrent = 128ULL * 1024ULL * 1024ULL;
constexpr const char* WorkloadName = "harmony_v1_isolated_workload";

std::string trim(std::string s) {
    const auto first = s.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    const auto last = s.find_last_not_of(" \t\r\n");
    return s.substr(first, last - first + 1);
}

std::string readFile(const std::string& path) {
    const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) throw std::runtime_error("open failed for " + path + ": " + std::strerror(errno));
    std::string text;
    char buffer[4096];
    for (;;) {
        const ssize_t n = ::read(fd, buffer, sizeof(buffer));
        if (n == 0) break;
        if (n < 0) {
            if (errno == EINTR) continue;
            const int saved = errno;
            ::close(fd);
            throw std::runtime_error("read failed for " + path + ": " + std::strerror(saved));
        }
        text.append(buffer, static_cast<std::size_t>(n));
        if (text.size() > 1024 * 1024) {
            ::close(fd);
            throw std::runtime_error("file too large: " + path);
        }
    }
    ::close(fd);
    return text;
}

std::uint64_t uintValue(const std::string& text) {
    if (text.empty() || text.front() == '-') throw std::runtime_error("expected unsigned integer");
    std::size_t used = 0;
    const auto value = std::stoull(text, &used, 10);
    if (used != text.size()) throw std::runtime_error("invalid integer suffix");
    return value;
}

bool safeGroup(const std::string& group) {
    if (group.rfind(AllowedGroupPrefix, 0) != 0 || group.size() == std::strlen(AllowedGroupPrefix)) return false;
    for (std::size_t i = std::strlen(AllowedGroupPrefix); i < group.size(); ++i) {
        const char c = group[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
    }
    return true;
}

std::string basename(const std::string& path) {
    const auto pos = path.find_last_of('/');
    return pos == std::string::npos ? path : path.substr(pos + 1);
}

std::string executable(std::uint64_t pid) {
    const std::string path = "/proc/" + std::to_string(pid) + "/exe";
    char buffer[4096];
    const auto n = ::readlink(path.c_str(), buffer, sizeof(buffer) - 1);
    if (n < 0) throw std::runtime_error("readlink exe failed: " + std::string(std::strerror(errno)));
    buffer[n] = '\0';
    return buffer;
}

std::uint64_t startTicks(std::uint64_t pid) {
    const auto stat = readFile("/proc/" + std::to_string(pid) + "/stat");
    const auto close = stat.rfind(')');
    if (close == std::string::npos) throw std::runtime_error("malformed proc stat");
    std::istringstream fields(stat.substr(close + 1));
    std::string token;
    for (int field = 3; field <= 22; ++field) {
        if (!(fields >> token)) throw std::runtime_error("short proc stat");
        if (field == 22) return uintValue(token);
    }
    throw std::runtime_error("missing proc starttime");
}

bool memberOf(std::uint64_t pid, const std::string& groupName) {
    std::istringstream lines(readFile("/proc/" + std::to_string(pid) + "/cgroup"));
    std::string line;
    while (std::getline(lines, line)) {
        const auto first = line.find(':');
        const auto second = first == std::string::npos ? first : line.find(':', first + 1);
        if (first == std::string::npos || second == std::string::npos) continue;
        std::istringstream names(line.substr(first + 1, second - first - 1));
        std::string name;
        bool hasMemory = false;
        while (std::getline(names, name, ',')) hasMemory = hasMemory || name == "memory";
        if (hasMemory) return line.substr(second + 1) == "/" + groupName;
    }
    return false;
}

std::vector<std::uint64_t> groupPids(const std::string& group) {
    std::istringstream lines(readFile(group + "/cgroup.procs"));
    std::vector<std::uint64_t> pids;
    std::string line;
    while (std::getline(lines, line)) {
        line = trim(line);
        if (!line.empty()) pids.push_back(uintValue(line));
    }
    return pids;
}

std::uint64_t currentBytes(const std::string& group) {
    return uintValue(trim(readFile(group + "/memory.current")));
}

std::uint64_t parseLimited(const char* s, const char* name) {
    const auto value = uintValue(s ? s : "");
    if (value == 0 || value > MaxPayload) throw std::runtime_error(std::string(name) + " must be in 1..4096 (units unknown)");
    return value;
}

void validate(const std::string& group, const std::string& name, std::uint64_t pid,
              std::uint64_t expectedStart, const std::string& expectedExe) {
    if (!safeGroup(group)) throw std::runtime_error("group path is outside the dedicated /dev/memcg/memshift_probe_ prefix");
    if (basename(executable(pid)) != WorkloadName || executable(pid) != expectedExe)
        throw std::runtime_error("target PID executable is not the isolated MemShift workload");
    if (!memberOf(pid, name)) throw std::runtime_error("target PID is not in the selected memory cgroup");
    const auto pids = groupPids(group);
    if (pids.size() != 1 || pids.front() != pid) throw std::runtime_error("selected test group must contain exactly the target PID");
    if (startTicks(pid) != expectedStart) throw std::runtime_error("PID starttime changed");
    const auto current = currentBytes(group);
    if (current == 0 || current > MaxCurrent) throw std::runtime_error("group current must be in 1..128 MiB");
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (argc != 5) {
            std::cerr << "Usage: harmony_v1_test_reclaim /dev/memcg/memshift_probe_NAME PID "
                         "memory.force_shrink_anon|memory.force_shrink_anon_bycount|proc-reclaim PAYLOAD\n";
            return 2;
        }
        const std::string group = argv[1];
        if (!safeGroup(group)) throw std::runtime_error("group must start with /dev/memcg/memshift_probe_ and contain only safe name characters");
        const auto pid = uintValue(argv[2]);
        if (pid == 0) throw std::runtime_error("PID must be positive");
        const std::string endpoint = argv[3];
        const bool processReclaim = endpoint == "proc-reclaim";
        if (!processReclaim && endpoint != "memory.force_shrink_anon" && endpoint != "memory.force_shrink_anon_bycount")
            throw std::runtime_error("endpoint is not on the isolated-test allowlist");
        std::string payload;
        if (processReclaim) {
            if (std::string(argv[4]) != "anon") throw std::runtime_error("proc-reclaim only accepts the literal payload anon");
            payload = "anon\n";
        } else {
            payload = std::to_string(parseLimited(argv[4], "payload")) + "\n";
        }
        const std::string groupName = basename(group);
        const auto initialStart = startTicks(pid);
        const auto initialExe = executable(pid);
        validate(group, groupName, pid, initialStart, initialExe);
        const std::string control = processReclaim ? "/proc/" + std::to_string(pid) + "/reclaim" : group + "/" + endpoint;
        const int fd = ::open(control.c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
        if (fd < 0) throw std::runtime_error("open endpoint failed: " + std::string(std::strerror(errno)));
        // Recheck identity and exclusive test-group membership immediately before the write.
        validate(group, groupName, pid, initialStart, initialExe);
        const auto currentBefore = currentBytes(group);
        const auto started = std::chrono::steady_clock::now();
        errno = 0;
        const ssize_t n = ::write(fd, payload.data(), payload.size());
        const int saved = errno;
        const auto elapsedUs = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - started).count();
        ::close(fd);
        const bool accepted = n == static_cast<ssize_t>(payload.size());
        std::cout << "event=vendor_reclaim_write status=" << (accepted ? "accepted" : "failed")
                  << " group=" << group << " pid=" << pid << " start_ticks=" << initialStart
                  << " exe=" << initialExe << " endpoint=" << endpoint
                  << " payload=" << (processReclaim ? "anon" : payload.substr(0, payload.size() - 1))
                  << " payload_units=unknown bytes_written=" << n << " errno=" << (accepted ? 0 : saved)
                  << " elapsed_us=" << elapsedUs << " current_before_bytes=" << currentBefore
                  << " current_after_bytes=" << currentBytes(group)
                  << "\n";
        return accepted ? 0 : 1;
    } catch (const std::exception& e) {
        std::cerr << "harmony_v1_test_reclaim: " << e.what() << '\n';
        return 2;
    }
}
