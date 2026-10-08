#include <napi/native_api.h>
#include <hilog/log.h>
#include <hitrace/trace.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <fcntl.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <cerrno>
#include <cstring>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <algorithm>

static std::atomic<bool> running{false}, retouching{false}, promoteRequested{false};
static std::thread worker;
static std::mutex guard;
static std::string session = "manual", directory, phase = "idle";
static uint64_t anonBytes = 0, fileBytes = 0, readyBytes = 0, peakBytes = 0;
static int failure = 0;
static uint64_t workloadSeed = 1;
static std::string requestedPeakSession = "manual";
static const uint64_t MiB = 1024 * 1024;

static int64_t nowNs() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
static std::string escaped(const std::string &s) {
    std::string out;
    for (char c : s) {
        if (c == '"' || c == '\\') out += '\\';
        if (c == '\n') out += "\\n"; else out += c;
    }
    return out;
}
static std::string snapshot(const std::string &event, int level = -1) {
    rusage ru{};
    getrusage(RUSAGE_SELF, &ru);
    std::ostringstream o;
    o << "{\"event\":\"" << event << "\",\"case_id\":\"" << escaped(session)
      << "\",\"pid\":" << getpid() << ",\"uid\":" << getuid()
      << ",\"monotonic_ns\":" << nowNs()
      << ",\"time_ms\":" << std::chrono::duration_cast<std::chrono::milliseconds>(
          std::chrono::system_clock::now().time_since_epoch()).count()
      << ",\"phase\":\"" << phase << "\",\"anon_bytes\":" << anonBytes
      << ",\"file_bytes\":" << fileBytes << ",\"touched_bytes\":" << anonBytes + fileBytes
      << ",\"ready_bytes\":" << readyBytes << ",\"peak_bytes\":" << peakBytes
      << ",\"minor\":" << ru.ru_minflt << ",\"major\":" << ru.ru_majflt
      << ",\"errno\":" << failure << ",\"level\":" << level << "}";
    return o.str();
}
static void emit(const std::string &event, int level = -1) {
    std::lock_guard<std::mutex> l(guard);
    auto line = snapshot(event, level);
    OH_LOG_Print(LOG_APP, LOG_INFO, 0, "MemScene", "%{public}s", line.c_str());
    const std::string path = directory + "/memscene_" + session + ".jsonl";
    int fd = open(path.c_str(), O_CREAT | O_APPEND | O_WRONLY, 0600);
    if (fd >= 0) {
        line += '\n'; write(fd, line.data(), line.size()); fsync(fd); close(fd);
    }
    OH_HiTrace_CountTrace("memscene/touched_bytes", static_cast<int64_t>(anonBytes + fileBytes));
    rusage usage{}; getrusage(RUSAGE_SELF, &usage);
    OH_HiTrace_CountTrace("memscene/minor", usage.ru_minflt);
    OH_HiTrace_CountTrace("memscene/major", usage.ru_majflt);
}
struct Chunk { void *p; size_t size; bool file; };
static uint64_t randomWord(uint64_t &state) {
    state ^= state >> 12; state ^= state << 25; state ^= state >> 27;
    return state * 2685821657736338717ULL;
}
static void setPhase(const std::string &value, int err = 0) {
    std::lock_guard<std::mutex> l(guard); phase = value; failure = err;
}
static void setSession(const std::string &value) {
    std::lock_guard<std::mutex> l(guard); session = value;
}
static void release(std::vector<Chunk> &chunks, bool onlyTransient, uint64_t daily) {
    uint64_t kept = 0;
    std::vector<Chunk> retained;
    for (auto &c : chunks) {
        if (onlyTransient && kept + c.size <= daily) {
            retained.push_back(c); kept += c.size; continue;
        }
        munmap(c.p, c.size);
        std::lock_guard<std::mutex> l(guard);
        if (c.file) fileBytes -= c.size; else anonBytes -= c.size;
    }
    chunks = retained;
}
static void load(uint64_t daily, uint64_t peak, uint64_t fileTarget,
                 unsigned hold, unsigned lifetime, uint64_t seed,
                 std::string peakSession, std::string dailySessionId, bool awaitPeak) {
    std::vector<Chunk> chunks;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(lifetime);
    uint64_t total = 0, fileOffset = 0;
    uint64_t state = 0x9e3779b97f4a7c15ULL ^ seed ^ (static_cast<uint64_t>(getuid()) << 32);
    int fd = -1;
    std::string backing = directory + "/memscene_" + peakSession + ".pages";
    if (fileTarget) {
        fd = open(backing.c_str(), O_CREAT | O_TRUNC | O_RDWR, 0600);
        if (fd < 0 || ftruncate(fd, static_cast<off_t>(fileTarget))) {
            setPhase("failed", errno); emit("allocation_failed"); running = false;
            if (fd >= 0) close(fd);
            return;
        }
    }
    auto allocate_until = [&](uint64_t target, const std::string &phaseName) {
        while (running && total < target && std::chrono::steady_clock::now() < deadline) {
            size_t size = static_cast<size_t>(std::min<uint64_t>(64 * MiB, target - total));
            bool isFile = fileOffset < fileTarget;
            if (isFile) size = static_cast<size_t>(std::min<uint64_t>(size, fileTarget - fileOffset));
            void *p = mmap(nullptr, size, PROT_READ | PROT_WRITE,
                           isFile ? MAP_SHARED : MAP_PRIVATE | MAP_ANONYMOUS,
                           isFile ? fd : -1, static_cast<off_t>(isFile ? fileOffset : 0));
            if (p == MAP_FAILED) { setPhase(phaseName, errno); emit("allocation_failed"); break; }
            chunks.push_back({p, size, isFile});
            auto words = static_cast<uint64_t *>(p);
            size_t completed = 0;
            for (size_t i = 0; i < size / sizeof(uint64_t) && running; ++i) {
                words[i] = randomWord(state); completed += sizeof(uint64_t);
                if ((i & 8191) == 0 && std::chrono::steady_clock::now() >= deadline) running = false;
            }
            {
                std::lock_guard<std::mutex> l(guard);
                if (isFile) fileBytes += size; else anonBytes += size;
            }
            total += size; if (isFile) { fileOffset += size; msync(p, size, MS_SYNC); }
            if (completed != size) { setPhase("expired"); break; }
            emit("allocation_progress");
        }
        return running && total == target && failure == 0;
    };
    setSession(dailySessionId); setPhase("daily_allocating"); emit("startup_begin");
    bool dailyReady = allocate_until(daily, "daily_allocating");
    if (dailyReady) {
        { std::lock_guard<std::mutex> l(guard); readyBytes = anonBytes + fileBytes; phase = "daily_ready"; }
        emit("daily_ready");
        while (awaitPeak && running && !promoteRequested &&
               std::chrono::steady_clock::now() < deadline) std::this_thread::sleep_for(std::chrono::milliseconds(50));
        if (running && (!awaitPeak || promoteRequested)) {
            setSession(peakSession); setPhase("peak_allocating"); emit("peak_begin");
        } else if (running) {
            setPhase("daily_wait_timeout"); emit("peak_not_promoted");
            running = false;
        }
    }
    bool allocated = dailyReady && allocate_until(peak, "peak_allocating");
    if (allocated) {
        { std::lock_guard<std::mutex> l(guard); peakBytes = total; }
        setPhase("peak"); emit("peak_reached");
        auto until = std::chrono::steady_clock::now() + std::chrono::seconds(hold);
        while (running && std::chrono::steady_clock::now() < until &&
               std::chrono::steady_clock::now() < deadline) std::this_thread::sleep_for(std::chrono::milliseconds(100));
        if (running) {
            release(chunks, true, daily);
            { std::lock_guard<std::mutex> l(guard); readyBytes = anonBytes + fileBytes; }
            setPhase("ready"); emit("startup_ready");
        }
    }
    while (running && allocated && std::chrono::steady_clock::now() < deadline) {
        if (retouching.exchange(false)) {
            emit("retouch_begin");
            volatile uint64_t checksum = 0;
            for (auto &c : chunks) {
                auto p = static_cast<volatile uint8_t *>(c.p);
                for (size_t i = 0; i < c.size && running; i += 4096) checksum += p[i];
            }
            (void)checksum; emit("retouch_end");
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    release(chunks, false, 0);
    if (fd >= 0) { close(fd); unlink(backing.c_str()); }
    running = false; setPhase(allocated ? "stopped" : "failed", failure); emit("stopped");
}
static void stopWorker() { running = false; if (worker.joinable()) worker.join(); }
static double number(napi_env env, napi_value v) { double n=0; napi_get_value_double(env,v,&n); return n; }
static std::string string(napi_env env, napi_value v) {
    size_t n=0; napi_get_value_string_utf8(env,v,nullptr,0,&n);
    std::vector<char> buf(n+1); napi_get_value_string_utf8(env,v,buf.data(),buf.size(),&n);
    return std::string(buf.data(),n);
}
static napi_value Start(napi_env env, napi_callback_info info) {
    size_t argc=10; napi_value args[10]; napi_get_cb_info(env,info,&argc,args,nullptr,nullptr);
    if (argc!=10) { napi_throw_error(env,nullptr,"start requires ten arguments"); return nullptr; }
    uint64_t daily=static_cast<uint64_t>(number(env,args[0])), peak=static_cast<uint64_t>(number(env,args[1]));
    uint64_t file=static_cast<uint64_t>(number(env,args[2]));
    unsigned hold=static_cast<unsigned>(number(env,args[3])), lifetime=static_cast<unsigned>(number(env,args[4]));
    std::string id=string(env,args[5]), dailyId=string(env,args[6]), dir=string(env,args[7]);
    uint64_t seed=static_cast<uint64_t>(number(env,args[8]));
    bool awaitPeak=false; napi_get_value_bool(env,args[9],&awaitPeak);
    if (daily>peak || file>peak || peak>131072 || !lifetime || id.empty() || dailyId.empty() ||
        id.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")!=std::string::npos) {
        napi_throw_error(env,nullptr,"invalid workload parameters"); return nullptr;
    }
    stopWorker();
    { std::lock_guard<std::mutex> l(guard);
      session=id; requestedPeakSession=id; directory=dir; workloadSeed=seed; anonBytes=fileBytes=readyBytes=peakBytes=0; failure=0; phase="starting"; }
    retouching=false; promoteRequested=false; running=true;
    worker=std::thread(load,daily*MiB,peak*MiB,file*MiB,hold,lifetime,workloadSeed,id,dailyId,awaitPeak);
    return nullptr;
}
static napi_value Stop(napi_env env,napi_callback_info) { stopWorker(); return nullptr; }
static napi_value Retouch(napi_env env,napi_callback_info) { retouching=true; return nullptr; }
static napi_value Promote(napi_env env,napi_callback_info info) {
    size_t argc=1; napi_value args[1]; napi_get_cb_info(env,info,&argc,args,nullptr,nullptr);
    bool accepted=false;
    if (argc==1) {
        const std::string requested=string(env,args[0]);
        std::lock_guard<std::mutex> l(guard);
        if (running && requested==requestedPeakSession && (phase=="daily_ready" || promoteRequested)) {
            promoteRequested=true; accepted=true;
        }
    }
    napi_value result; napi_get_boolean(env,accepted,&result); return result;
}
static napi_value Status(napi_env env,napi_callback_info) {
    std::lock_guard<std::mutex> l(guard); auto text=snapshot("status");
    napi_value out; napi_create_string_utf8(env,text.c_str(),text.size(),&out); return out;
}
static napi_value Record(napi_env env,napi_callback_info info) {
    size_t argc=2; napi_value args[2]; napi_get_cb_info(env,info,&argc,args,nullptr,nullptr);
    if(argc==2) emit(string(env,args[0]),static_cast<int>(number(env,args[1]))); return nullptr;
}
static napi_value Init(napi_env env,napi_value exports) {
    napi_property_descriptor properties[]={
      {"start",nullptr,Start,nullptr,nullptr,nullptr,napi_default,nullptr},
      {"stop",nullptr,Stop,nullptr,nullptr,nullptr,napi_default,nullptr},
      {"retouch",nullptr,Retouch,nullptr,nullptr,nullptr,napi_default,nullptr},
      {"promote",nullptr,Promote,nullptr,nullptr,nullptr,napi_default,nullptr},
      {"status",nullptr,Status,nullptr,nullptr,nullptr,napi_default,nullptr},
      {"record",nullptr,Record,nullptr,nullptr,nullptr,napi_default,nullptr}};
    napi_define_properties(env,exports,sizeof(properties)/sizeof(properties[0]),properties); return exports;
}
static napi_module module={1,0,nullptr,Init,"memload",nullptr,{0}};
extern "C" __attribute__((constructor)) void RegisterModule() { napi_module_register(&module); }
