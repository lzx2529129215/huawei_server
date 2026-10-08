#include <algorithm>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <unistd.h>

namespace {
volatile sig_atomic_t stopRequested = 0;
volatile sig_atomic_t verifyRequested = 0;

void onSignal(int sig) {
    if (sig == SIGUSR1) verifyRequested = 1;
    else stopRequested = 1;
}

std::uint64_t parse(const char* text, const char* label, std::uint64_t min, std::uint64_t max) {
    if (!text || !*text || *text == '-') throw std::runtime_error(std::string("invalid ") + label);
    char* end = nullptr;
    errno = 0;
    const auto value = std::strtoull(text, &end, 10);
    if (errno || !end || *end || value < min || value > max)
        throw std::runtime_error(std::string("invalid ") + label);
    return value;
}

void joinCgroup(const std::string& controlFile) {
    const int fd = ::open(controlFile.c_str(), O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) throw std::runtime_error("open cgroup.procs failed: " + std::string(std::strerror(errno)));
    const std::string pid = std::to_string(::getpid());
    ssize_t n;
    do { n = ::write(fd, pid.data(), pid.size()); } while (n < 0 && errno == EINTR);
    const int saved = errno;
    ::close(fd);
    if (n != static_cast<ssize_t>(pid.size()))
        throw std::runtime_error("write cgroup.procs failed: " + std::string(std::strerror(n < 0 ? saved : EIO)));
}

std::uint64_t mix(std::uint64_t x) {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}

std::uint8_t expected(std::size_t index) {
    return static_cast<std::uint8_t>(mix(static_cast<std::uint64_t>(index) ^ 0x6d656d7368696674ULL) >> 24);
}

struct Usage { long minor = 0, major = 0; };
Usage usage() {
    rusage value{};
    if (::getrusage(RUSAGE_SELF, &value) != 0) throw std::runtime_error("getrusage failed");
    return {value.ru_minflt, value.ru_majflt};
}

void verifyPages(const volatile std::uint8_t* memory, std::size_t size) {
    const auto before = usage();
    const auto started = std::chrono::steady_clock::now();
    std::uint64_t mismatches = 0;
    std::size_t firstMismatch = size;
    for (std::size_t i = 0; i < size; ++i) {
        if (memory[i] != expected(i)) {
            if (firstMismatch == size) firstMismatch = i;
            ++mismatches;
        }
    }
    const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now() - started).count();
    const auto after = usage();
    std::cout << "event=verify status=" << (mismatches == 0 ? "pass" : "fail")
              << " bytes=" << size << " mismatches=" << mismatches
              << " first_mismatch=" << (firstMismatch == size ? -1LL : static_cast<long long>(firstMismatch))
              << " elapsed_us=" << elapsed
              << " minor_before=" << before.minor << " minor_after=" << after.minor
              << " minor_delta=" << (after.minor - before.minor)
              << " major_before=" << before.major << " major_after=" << after.major
              << " major_delta=" << (after.major - before.major) << std::endl;
}
} // namespace

int main(int argc, char** argv) {
    void* mapping = MAP_FAILED;
    std::size_t size = 128ULL * 1024 * 1024;
    try {
        if (argc < 2 || argc > 4) {
            std::cerr << "Usage: harmony_v1_isolated_workload /dev/memcg/GROUP/cgroup.procs [MiB=128] [ttl_sec=120]\n";
            return 2;
        }
        if (argc >= 3) size = static_cast<std::size_t>(parse(argv[2], "MiB", 1, 2048)) * 1024ULL * 1024ULL;
        const std::uint64_t ttlSec = argc >= 4 ? parse(argv[3], "ttl_sec", 1, 3600) : 120;
        struct sigaction action{};
        action.sa_handler = onSignal;
        sigemptyset(&action.sa_mask);
        if (::sigaction(SIGUSR1, &action, nullptr) != 0 || ::sigaction(SIGTERM, &action, nullptr) != 0 ||
            ::sigaction(SIGINT, &action, nullptr) != 0) throw std::runtime_error("sigaction failed");

        joinCgroup(argv[1]);
        const long pageLong = ::sysconf(_SC_PAGESIZE);
        if (pageLong <= 0) throw std::runtime_error("sysconf(_SC_PAGESIZE) failed");
        const auto pageSize = static_cast<std::size_t>(pageLong);
        mapping = ::mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (mapping == MAP_FAILED) throw std::runtime_error("mmap failed: " + std::string(std::strerror(errno)));
        auto* memory = static_cast<volatile std::uint8_t*>(mapping);
        for (std::size_t page = 0; page < size; page += pageSize) {
            const auto end = std::min(size, page + pageSize);
            for (std::size_t i = page; i < end; ++i) memory[i] = expected(i);
        }
        const auto touched = usage();
        std::cout << "event=ready pid=" << ::getpid() << " bytes=" << size << " page_size=" << pageSize
                  << " cgroup_procs=" << argv[1] << " minor=" << touched.minor << " major=" << touched.major
                  << " ttl_sec=" << ttlSec << std::endl;

        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(ttlSec);
        while (!stopRequested && std::chrono::steady_clock::now() < deadline) {
            if (verifyRequested) {
                verifyRequested = 0;
                verifyPages(memory, size);
            }
            ::usleep(100000);
        }
        if (verifyRequested) verifyPages(memory, size);
        std::cout << "event=exit reason=" << (stopRequested ? "signal" : "ttl") << std::endl;
        ::munmap(mapping, size);
        mapping = MAP_FAILED;
        return 0;
    } catch (const std::exception& e) {
        if (mapping != MAP_FAILED) ::munmap(mapping, size);
        std::cerr << "workload_error=" << e.what() << std::endl;
        return 1;
    }
}
