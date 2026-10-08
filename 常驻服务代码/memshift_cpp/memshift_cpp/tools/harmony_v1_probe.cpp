#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/vfs.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {
struct Target { std::string name, path; };

std::string readFile(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return {};
    return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}

std::string trim(std::string s) {
    const auto first = s.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    const auto last = s.find_last_not_of(" \t\r\n");
    return s.substr(first, last - first + 1);
}

bool safeRelative(const std::string& path) {
    if (path.empty() || path.front() == '/' || path.back() == '/') return false;
    std::istringstream parts(path);
    std::string part;
    while (std::getline(parts, part, '/')) {
        if (part.empty() || part == "." || part == "..") return false;
    }
    return true;
}

std::uint64_t number(const std::string& s) {
    if (s.empty() || s.front() == '-') throw std::runtime_error("invalid unsigned integer");
    std::size_t used = 0;
    const auto n = std::stoull(s, &used, 10);
    if (used != s.size()) throw std::runtime_error("invalid integer suffix");
    return n;
}

std::map<std::string, std::uint64_t> parseCounters(const std::string& text) {
    std::map<std::string, std::uint64_t> values;
    std::istringstream in(text);
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream row(line);
        std::string key, value, unit, extra;
        if (!(row >> key >> value >> unit) || (row >> extra)) continue;
        if (key == "Anon:" || key == "Zram:" || key == "Eswap:") {
            if (unit != "kB") continue;
            key.pop_back();
            try { values[key] = number(value); } catch (...) {}
        }
    }
    return values;
}

std::map<std::string, std::uint64_t> parseMeminfo(const std::string& text) {
    std::map<std::string, std::uint64_t> values;
    std::istringstream in(text);
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream row(line);
        std::string key, value, unit;
        if (!(row >> key >> value)) continue;
        row >> unit;
        if (!key.empty() && key.back() == ':') key.pop_back();
        try {
            auto n = number(value);
            if (unit == "kB" && n <= std::numeric_limits<std::uint64_t>::max() / 1024) n *= 1024;
            values[key] = n;
        } catch (...) {}
    }
    return values;
}

std::string quote(const std::string& s) {
    std::string out = "\"";
    for (const unsigned char c : s) {
        if (c == '\\' || c == '"') { out.push_back('\\'); out.push_back(static_cast<char>(c)); }
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else if (c >= 0x20) out.push_back(static_cast<char>(c));
    }
    out.push_back('"');
    return out;
}

void emitOptional(const char* key, const std::map<std::string, std::uint64_t>& values,
                  const std::string& sourceKey) {
    std::cout << ",\"" << key << "\":";
    const auto it = values.find(sourceKey);
    if (it == values.end()) std::cout << "null";
    else std::cout << it->second;
}

std::string psiRaw(const std::string& path) {
    auto s = readFile(path);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
    return s;
}

bool selfHasMemoryController(const std::string& proc) {
    std::istringstream in(readFile(proc + "/self/cgroup"));
    std::string line;
    while (std::getline(in, line)) {
        const auto first = line.find(':');
        const auto second = first == std::string::npos ? first : line.find(':', first + 1);
        if (first == std::string::npos || second == std::string::npos) continue;
        std::istringstream controllers(line.substr(first + 1, second - first - 1));
        std::string name;
        while (std::getline(controllers, name, ',')) if (name == "memory") return true;
    }
    return false;
}

bool isCgroupMountAt(const std::string& proc, const std::string& root) {
    std::istringstream in(readFile(proc + "/mounts"));
    std::string source, mountPoint, fsType, options;
    while (in >> source >> mountPoint >> fsType >> options) {
        std::string ignored;
        std::getline(in, ignored);
        while (mountPoint.size() > 1 && mountPoint.back() == '/') mountPoint.pop_back();
        if (mountPoint == root && fsType == "cgroup") return true;
    }
    return false;
}

std::uint64_t parsePositive(const char* arg, const char* name, std::uint64_t min, std::uint64_t max) {
    auto n = number(arg);
    if (n < min || n > max) throw std::runtime_error(std::string(name) + " out of range");
    return n;
}
} // namespace

int main(int argc, char** argv) {
    try {
        std::string root = "/dev/memcg", proc = "/proc";
        std::uint64_t intervalMs = 1000, samples = 60;
        std::vector<Target> targets;
        for (int i = 1; i < argc; ++i) {
            const std::string arg = argv[i];
            auto value = [&](const char* option) -> const char* {
                if (++i >= argc) throw std::runtime_error(std::string("missing value for ") + option);
                return argv[i];
            };
            if (arg == "--root") root = value("--root");
            else if (arg == "--proc") proc = value("--proc");
            else if (arg == "--interval-ms") intervalMs = parsePositive(value("--interval-ms"), "interval-ms", 200, 10000);
            else if (arg == "--samples") samples = parsePositive(value("--samples"), "samples", 1, 86400);
            else if (arg == "--target") {
                std::string item = value("--target");
                const auto split = item.find('=');
                if (split == std::string::npos || split == 0 || split + 1 == item.size())
                    throw std::runtime_error("target must be NAME=RELATIVE_PATH");
                Target t{item.substr(0, split), item.substr(split + 1)};
                if (!safeRelative(t.path)) throw std::runtime_error("target path must be safe and relative");
                targets.push_back(std::move(t));
            } else if (arg == "--help") {
                std::cout << "Read-only HarmonyOS cgroup-v1 MemShift observation probe.\n"
                          << "Usage: harmony_v1_probe [--root /dev/memcg] [--proc /proc] "
                          << "[--target NAME=RELATIVE_PATH]... [--interval-ms 1000] [--samples 60]\n";
                return 0;
            } else throw std::runtime_error("unknown option: " + arg);
        }
        if (targets.empty()) throw std::runtime_error("provide at least one --target");
        std::string normalizedRoot = root;
        while (normalizedRoot.size() > 1 && normalizedRoot.back() == '/') normalizedRoot.pop_back();
        if (!selfHasMemoryController(proc))
            throw std::runtime_error("/proc/self/cgroup does not report the memory controller");
        if (!isCgroupMountAt(proc, normalizedRoot))
            throw std::runtime_error("the selected root is not listed as a cgroup mount in /proc/mounts");
        struct statfs fs{};
        if (::statfs(root.c_str(), &fs) != 0) throw std::runtime_error("statfs failed for cgroup root");
        std::cout << "{\"record\":\"profile\",\"profile\":\"harmony-v1-observe\","
                  << "\"mode\":\"read-only\",\"reclaim_supported\":false,"
                  << "\"root_validation\":\"/proc/self/cgroup has memory controller and /proc/mounts lists selected root as cgroup; statfs magic recorded but not used to reject HarmonyOS\","
                  << "\"statfs_magic_raw\":" << static_cast<unsigned long>(fs.f_type) << ","
                  << "\"anon_semantics\":\"Anon kB from HarmonyOS memmgr memory.stat; clamped to memory.current as an upper bound, not a cold-page estimate\","
                  << "\"psi_missing_policy\":\"unknown; no reclaim action\"}\n";
        for (std::uint64_t seq = 0; seq < samples; ++seq) {
            const auto meminfo = parseMeminfo(readFile(proc + "/meminfo"));
            const auto memoryPsi = psiRaw(proc + "/pressure/memory");
            const auto ioPsi = psiRaw(proc + "/pressure/io");
            std::cout << "{\"record\":\"system\",\"seq\":" << seq;
            emitOptional("MemTotal_bytes", meminfo, "MemTotal");
            emitOptional("MemAvailable_bytes", meminfo, "MemAvailable");
            emitOptional("MemFree_bytes", meminfo, "MemFree");
            emitOptional("SwapFree_bytes", meminfo, "SwapFree");
            std::cout << ",\"pressure_memory\":" << (memoryPsi.empty() ? "null" : quote(memoryPsi))
                      << ",\"pressure_io\":" << (ioPsi.empty() ? "null" : quote(ioPsi))
                      << ",\"pressure_io_status\":" << quote(ioPsi.empty() ? "unknown" : "present") << "}\n";
            for (const auto& target : targets) {
                const auto base = root + "/" + target.path;
                const auto currentText = readFile(base + "/memory.current");
                std::uint64_t current = 0;
                bool currentKnown = false;
                try { current = number(trim(currentText)); currentKnown = true; } catch (...) {}
                const auto counters = parseCounters(readFile(base + "/memory.stat"));
                const auto anon = counters.find("Anon");
                const bool anonKnown = anon != counters.end() && anon->second <= std::numeric_limits<std::uint64_t>::max() / 1024;
                const auto anonBytes = anonKnown ? anon->second * 1024 : 0;
                std::cout << "{\"record\":\"app\",\"seq\":" << seq << ",\"name\":" << quote(target.name)
                          << ",\"path\":" << quote(target.path)
                          << ",\"memory_current_raw\":" << (currentKnown ? std::to_string(current) : "null")
                          << ",\"memory_current_unit\":\"assumed_bytes_from_interface_name\""
                          << ",\"anon_accounted_bytes\":" << (anonKnown ? std::to_string(anonBytes) : "null")
                          << ",\"anon_upper_bound_bytes\":"
                          << (anonKnown && currentKnown ? std::to_string(std::min(current, anonBytes)) : "null");
                emitOptional("zram_accounted_kib", counters, "Zram");
                emitOptional("eswap_accounted_kib", counters, "Eswap");
                std::cout << ",\"file_candidate_bytes\":null,\"cold_anon_candidate_bytes\":null,"
                          << "\"reclaim_action\":\"none\",\"reason\":\"no byte-budgeted vendor reclaim interface confirmed\"}\n";
            }
            std::cout.flush();
            if (seq + 1 < samples) std::this_thread::sleep_for(std::chrono::milliseconds(intervalMs));
        }
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "harmony_v1_probe: " << e.what() << '\n';
        return 2;
    }
}
