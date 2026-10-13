#pragma once
#include "core.hpp"
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <optional>
#include <set>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <time.h>
#include <unistd.h>

namespace ms {
inline double monotonic() {
    struct timespec ts{};
    if(::clock_gettime(CLOCK_BOOTTIME,&ts)!=0) throw std::runtime_error("clock_gettime(CLOCK_BOOTTIME) failed");
    return static_cast<double>(ts.tv_sec)+static_cast<double>(ts.tv_nsec)/1.0e9;
}
inline Bytes unixSeconds() {
    return static_cast<Bytes>(std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
}
inline Bytes unixMillis() {
    return static_cast<Bytes>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
}
class FD {
    int fd_ = -1;
public:
    FD() = default;
    explicit FD(int fd) : fd_(fd) {}
    ~FD() { if (fd_ >= 0) ::close(fd_); }
    FD(const FD&) = delete;
    FD& operator=(const FD&) = delete;
    FD(FD&& x) noexcept : fd_(x.fd_) { x.fd_ = -1; }
    FD& operator=(FD&& x) noexcept {
        if (this != &x) { if (fd_ >= 0) ::close(fd_); fd_ = x.fd_; x.fd_ = -1; }
        return *this;
    }
    int get() const { return fd_; }
};
inline std::runtime_error error(const std::string& what, int e = errno) {
    return std::runtime_error(what + ": " + std::strerror(e));
}
inline std::string readFD(int fd, std::size_t limit = 1024*1024) {
    std::string out; char buf[4096];
    for (;;) {
        auto n = ::read(fd,buf,sizeof(buf));
        if (n == 0) return out;
        if (n < 0) { if (errno == EINTR) continue; throw error("read"); }
        out.append(buf,static_cast<std::size_t>(n));
        if (out.size() > limit) throw std::runtime_error("input exceeds size limit");
    }
}
inline std::string readAt(int dir, const std::string& name) {
    FD fd(::openat(dir,name.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW));
    if (fd.get() < 0) throw error("open " + name);
    return readFD(fd.get());
}
inline std::optional<std::string> maybeAt(int dir, const std::string& name) {
    try { return readAt(dir,name); } catch (const std::exception&) { return {}; }
}
inline std::string readPath(const std::string& path) {
    FD fd(::open(path.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW));
    if (fd.get() < 0) throw error("open " + path);
    return readFD(fd.get());
}
inline FD openBeneath(int root, const std::string& relative) {
    if (!safeRelative(relative)) throw std::runtime_error("unsafe relative cgroup path: " + relative);
    FD dir(::fcntl(root,F_DUPFD_CLOEXEC,3));
    if (dir.get() < 0) throw error("dup root fd");
    std::istringstream in(relative); std::string part;
    while (std::getline(in,part,'/')) {
        FD next(::openat(dir.get(),part.c_str(),O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC));
        if (next.get() < 0) throw error("open cgroup " + relative);
        dir = std::move(next);
    }
    return dir;
}
inline Bytes limitAt(int fd, const std::string& field, Bytes missing) {
    auto t = maybeAt(fd,field);
    if (!t) return missing;
    const auto v = trim(*t);
    return v == "max" ? Unlimited : uintValue(v);
}
inline Bytes localSwapRoom(int fd, bool root) {
    // Unknown swap permission is not treated as available swap capacity.
    const auto m = limitAt(fd,"memory.swap.max",root ? Unlimited : 0);
    if (m == Unlimited) return Unlimited;
    auto text = maybeAt(fd,"memory.swap.current");
    if (!text) return 0;
    return sub(m,uintValue(trim(*text)));
}
inline Bytes ancestorSwapRoom(int root, const std::string& path) {
    Bytes room = localSwapRoom(root,true);
    FD fd(::fcntl(root,F_DUPFD_CLOEXEC,3));
    if (fd.get() < 0) throw error("dup root");
    std::istringstream in(path); std::string part;
    while (std::getline(in,part,'/')) {
        FD next(::openat(fd.get(),part.c_str(),O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC));
        if (next.get() < 0) throw error("read ancestor swap budget");
        room = std::min(room,localSwapRoom(next.get(),false));
        fd = std::move(next);
    }
    return room;
}
inline Sample sampleAt(int fd) {
    Sample a;
    struct stat st{};
    if (::fstat(fd,&st) != 0) throw error("fstat cgroup");
    a.device = static_cast<std::uint64_t>(st.st_dev);
    a.inode = static_cast<std::uint64_t>(st.st_ino);
    if (trim(readAt(fd,"cgroup.type")) != "domain")
        throw std::runtime_error("only domain cgroups supported");
    const auto v = counters(readAt(fd,"memory.stat"));
    a.current = uintValue(trim(readAt(fd,"memory.current")));
    a.anon = required(v,"anon"); a.file = required(v,"file");
    a.inactiveAnon = required(v,"inactive_anon"); a.inactiveFile = required(v,"inactive_file");
    a.dirty = required(v,"file_dirty"); a.writeback = required(v,"file_writeback");
    a.unevictable = required(v,"unevictable");
    a.pgfault = required(v,"pgfault"); a.major = required(v,"pgmajfault");
    if (v.count("workingset_refault_file"))
        a.refault = add(get(v,"workingset_refault_file"),get(v,"workingset_refault_anon"));
    else a.refault = required(v,"workingset_refault");
    a.pgsteal = get(v,"pgsteal");
    a.hasProactive = v.count("pgsteal_proactive") != 0;
    a.pgstealProactive = get(v,"pgsteal_proactive");
    auto sw = maybeAt(fd,"memory.swap.current");
    a.swap = sw ? uintValue(trim(*sw)) : 0;
    // Required: unreadable protection files make the target ineligible.
    const auto minText = trim(readAt(fd,"memory.min"));
    const auto lowText = trim(readAt(fd,"memory.low"));
    a.min = minText == "max" ? Unlimited : uintValue(minText);
    a.low = lowText == "max" ? Unlimited : uintValue(lowText);
    a.oomKills = required(counters(readAt(fd,"memory.events")),"oom_kill");
    a.populated = required(counters(readAt(fd,"cgroup.events")),"populated") != 0;
    auto ps = maybeAt(fd,"memory.pressure");
    if (ps) a.psi = parsePSI(*ps);
    a.valid = true;
    return a;
}
inline System systemSample(const std::string& proc) {
    System s; s.time = monotonic();
    KV info; std::istringstream in(readPath(proc+"/meminfo")); std::string line;
    while (std::getline(in,line)) {
        std::istringstream row(line); std::string key, val, unit;
        if (!(row >> key >> val)) continue;
        row >> unit;
        if (!key.empty() && key.back() == ':') key.pop_back();
        const auto n = uintValue(val);
        info[key] = unit == "kB" ? (n > Unlimited/1024 ? Unlimited : n*1024) : n;
    }
    s.total = required(info,"MemTotal"); s.available = required(info,"MemAvailable");
    s.free = required(info,"MemFree"); s.cached=get(info,"Cached"); s.dirty=get(info,"Dirty");
    if(info.count("Writeback")) s.writebackBytes=info.at("Writeback");
    if(info.count("AnonPages")) s.anonPagesBytes=info.at("AnonPages");
    if(info.count("Shmem")) s.shmemBytes=info.at("Shmem");
    s.swapTotal = required(info,"SwapTotal");
    s.swapFree = required(info,"SwapFree"); s.zswap = get(info,"Zswap"); s.zswapped = get(info,"Zswapped");
    if (!s.total || s.available > s.total || s.free > s.total) throw std::runtime_error("inconsistent meminfo");
    try { s.mem = parsePSI(readPath(proc+"/pressure/memory")); } catch (const std::exception&) {}
    try { s.io = parsePSI(readPath(proc+"/pressure/io")); } catch (const std::exception&) {}
    try {
        auto vm = counters(readPath(proc+"/vmstat"));
        s.pswpin = get(vm,"pswpin"); s.pswpout = get(vm,"pswpout");
        if(vm.count("oom_kill_cnt")) s.oomKillCnt=vm.at("oom_kill_cnt");
    } catch (const std::exception&) {}
    return s;
}
inline std::pair<std::string,std::string> assignment(const std::string& line) {
    auto pos = line.find('=');
    if (pos == std::string::npos) throw std::runtime_error("expected key=value: " + line);
    return {trim(line.substr(0,pos)),trim(line.substr(pos+1))};
}
struct Configuration { Options options; std::vector<App> apps; };
inline Configuration parseConfig(const std::string& text) {
    Configuration c; App* app = nullptr;
    std::set<std::string> seen;
    std::istringstream in(text); std::string line; std::string section = "daemon";
    auto setOption = [&](const std::string& k, const std::string& v) {
        auto& o = c.options;
        if (k=="cgroup_root") o.root=v;
        else if(k=="proc_root") o.proc=v;
        else if(k=="log_dir") o.logs=v;
        else if(k=="lock_file") o.lock=v;
        else if(k=="control_file") o.control=v;
        else if(k=="oracle_mode") o.oracleMode=boolValue(v);
        else if(k=="oracle_known_future_free_only") o.oracleKnownFutureFreeOnly=boolValue(v);
        else if(k=="daily_async") o.dailyAsync=boolValue(v);
        else if(k=="daily_known_lead_sec") o.dailyKnownLeadSec=realValue(v);
        else if(k=="daily_known_capacity") o.dailyKnownCapacity=boolValue(v);
        else if(k=="daily_known_strength") o.dailyKnownStrength=boolValue(v);
        else if(k=="daily_known_future_free_only") o.dailyKnownFutureFreeOnly=boolValue(v);
        else if(k=="daily_known_plan_file") o.dailyKnownPlan=v;
        else if(k=="daily_known_evidence_ledger") o.dailyKnownLedger=v;
        else if(k=="daily_recent_reserve") o.dailyRecentReserve=boolValue(v);
        else if(k=="daily_recent_window_sec") o.dailyRecentWindowSec=realValue(v);
        else if(k=="daily_recent_hold_sec") o.dailyRecentHoldSec=realValue(v);
        else if(k=="daily_recent_cap_fraction") o.dailyRecentCapFraction=realValue(v);
        else if(k=="daily_owned_os_snapshot") o.dailyOwnedOsSnapshot=boolValue(v);
        else if(k=="daily_disk_mode") o.dailyDiskMode=v;
        else if(k=="daily_disk_whole_work_mb") o.dailyDiskWholeWork=mbValue(v);
        else if(k=="daily_max_submissions") o.dailyMaxSubmissions=uintValue(v);
        else if(k=="oracle_disk_prepare_anon") o.oracleDiskPrepareAnon=boolValue(v);
        else if(k=="oracle_defer_smaps") o.oracleDeferSmaps=boolValue(v);
        else if(k=="harmony_smaps_interval_ms") { auto n=uintValue(v); if(n!=0 && (n<1000 || n>600000)) throw std::runtime_error("harmony_smaps_interval_ms: 0 or 1000..600000"); o.harmonySmapsIntervalMs=static_cast<unsigned>(n); }
        else if(k=="oracle_profile") {
            if(v!="legacy_two_donor" && v!="peak_transition_os_snapshot")
                throw std::runtime_error("oracle_profile: unsupported value");
            o.oracleProfile=v;
        }
        else if(k=="oracle_backend") {
            if(v!="force_shrink_all" && v!="force_swapout" && v!="proc_file" && v!="proc_file_writeback")
                throw std::runtime_error("oracle_backend: unsupported value");
            o.oracleBackend=v;
            o.oracleBackendExplicit=true;
        }
        else if(k=="oracle_schedule_file") o.oracleSchedule=v;
        else if(k=="oracle_plan_file") o.oraclePlan=v;
        else if(k=="oracle_proof_source") { if(v!="legacy" && v!="device") throw std::runtime_error("oracle_proof_source: legacy/device"); o.oracleProofSource=v; }
        else if(k=="oracle_ledger_file") o.oracleLedger=v;
        else if(k=="oracle_batch_mb") o.oracleBatch=mbValue(v);
        else if(k=="oracle_max_ratio") { auto n=uintValue(v); if(n<1 || n>95) throw std::runtime_error("oracle_max_ratio: 1..95"); o.oracleMaxRatio=static_cast<unsigned>(n); }
        else if(k=="oracle_cumulative_keep_mb") { auto n=uintValue(v); if(n<128 || n>512) throw std::runtime_error("oracle_cumulative_keep_mb: 128..512"); o.oracleCumulativeKeep=mbValue(v); }
        else if(k=="oracle_device_donor_total_keep_mb") { auto n=uintValue(v); if(n<128 || n>512) throw std::runtime_error("oracle_device_donor_total_keep_mb: 128..512"); o.oracleDeviceDonorTotalKeep=mbValue(v); }
        else if(k=="oracle_ratio_semantics") {
            if(v!="resident_incremental" && v!="cumulative_anon_watermark")
                throw std::runtime_error("oracle_ratio_semantics: unsupported value");
            o.oracleRatioSemantics=v;
        }
        else if(k=="oracle_feedback_delay_ms") { auto n=uintValue(v); if(n>10000) throw std::runtime_error("oracle_feedback_delay_ms: 0..10000"); o.oracleFeedbackDelayMs=static_cast<unsigned>(n); }
        else if(k=="oracle_profile_action_interval_ms") { auto n=uintValue(v); if(n<1000 || n>5000) throw std::runtime_error("oracle_profile_action_interval_ms: 1000..5000"); o.oracleProfileActionIntervalMs=static_cast<unsigned>(n); }
        else if(k=="oracle_profile_poll_interval_ms") { auto n=uintValue(v); if(n<100 || n>1000) throw std::runtime_error("oracle_profile_poll_interval_ms: 100..1000"); o.oracleProfilePollIntervalMs=static_cast<unsigned>(n); }
        else if(k=="oracle_global_cap_mb") o.oracleGlobalCap=mbValue(v);
        else if(k=="oracle_case_cap_mb") o.oracleCaseCap=mbValue(v);
        else if(k=="oracle_app_cap_mb") o.oracleAppCap=mbValue(v);
        else if(k=="interval_ms") { auto n=uintValue(v); if(n<200 || n>10000) throw std::runtime_error("interval_ms: 200..10000"); o.intervalMs=static_cast<int>(n); }
        else if(k=="swappiness") { if(v=="-1") o.swappiness=-1; else { auto n=uintValue(v); if(n>200) throw std::runtime_error("swappiness: -1 or 0..200"); o.swappiness=static_cast<int>(n); } }
        else if(k=="allow_anon_hint") o.allowAnon=boolValue(v);
        else if(k=="require_psi") o.requirePSI=boolValue(v);
        else if(k=="horizon_sec") o.horizonSec=realValue(v);
        else if(k=="startup_grace_sec") o.warmupSec=realValue(v);
        else if(k=="cooldown_sec") o.cooldownSec=realValue(v);
        else if(k=="backoff_sec") o.backoffSec=realValue(v);
        else if(k=="alpha") o.alpha=realValue(v);
        else if(k=="anon_weight") o.anonWeight=realValue(v);
        else if(k=="keep_ratio") o.keepRatio=realValue(v);
        else if(k=="episode_fraction") o.episodeFraction=realValue(v);
        else if(k=="max_decline_mb_s") o.maxDeclineMBps=realValue(v);
        else if(k=="growth_guard_mb_s") o.growthGuardMBps=realValue(v);
        else if(k=="refault_guard_mb_s") o.refaultGuardMBps=realValue(v);
        else if(k=="major_guard_per_s") o.majorGuardPS=realValue(v);
        else if(k=="io_some_limit_pct") o.ioSomeLimit=realValue(v);
        else if(k=="memory_full_limit_pct") o.memoryFullLimit=realValue(v);
        else if(k=="app_some_limit_pct") o.appSomeLimit=realValue(v);
        else if(k=="reserve_mb") o.reserve=mbValue(v);
        else if(k=="ready_free_mb") o.readyFree=mbValue(v);
        else if(k=="hysteresis_mb") o.hysteresis=mbValue(v);
        else if(k=="min_batch_mb") o.minBatch=mbValue(v);
        else if(k=="batch_mb") o.batch=mbValue(v);
        else if(k=="max_per_minute_mb") o.perMinute=mbValue(v);
        else if(k=="app_per_minute_mb") o.appPerMinute=mbValue(v);
        else if(k=="swap_reserve_mb") o.swapReserve=mbValue(v);
        else if(k=="log_max_mb") o.logMax=mbValue(v);
        else if(k=="max_boost_mb") o.maxBoost=mbValue(v);
        else throw std::runtime_error("unknown daemon key: " + k);
    };
    while (std::getline(in,line)) {
        line=trim(line); if(line.empty() || line[0]=='#' || line[0]==';') continue;
        if(line.front()=='[' && line.back()==']') {
            auto name=trim(line.substr(1,line.size()-2));
            if(name=="daemon") { app=nullptr; section="daemon"; }
            else if(name.rfind("app ",0)==0) {
                name=trim(name.substr(4));
                if(!safeName(name)) throw std::runtime_error("invalid app name");
                for(auto& a:c.apps) if(a.name==name) throw std::runtime_error("duplicate app name");
                c.apps.push_back({name,"",false,256*MiB}); app=&c.apps.back(); section="app:"+name;
            } else throw std::runtime_error("unknown section: "+name);
            continue;
        }
        auto [k,v]=assignment(line);
        if(!seen.insert(section+":"+k).second) throw std::runtime_error("duplicate key: "+k);
        if(!app) setOption(k,v);
        else if(k=="path") app->path=v;
        else if(k=="reclaim") app->reclaim=boolValue(v);
        else if(k=="wps_window_gate") app->wpsWindowGate=boolValue(v);
        else if(k=="min_keep_mb") app->keep=mbValue(v);
        else throw std::runtime_error("unknown app key: "+k);
    }
    const auto& o=c.options;
    auto check=[](bool ok,const char* msg){ if(!ok) throw std::runtime_error(msg); };
    for(const auto& entry:c.apps) if(entry.wpsWindowGate)
        check(entry.name=="wps" && entry.path=="100/cn.wps.office.hap_20020028" && entry.reclaim &&
            entry.keep>=512*MiB && o.dailyAsync && !o.oracleMode,
            "wps_window_gate requires the exact WPS leaf, daily_async, reclaim and 512 MiB keep");
    check(!o.root.empty() && o.root.front()=='/' && !o.proc.empty() && o.proc.front()=='/',"root paths must be absolute");
    check(!o.logs.empty() && !o.lock.empty(),"log_dir and lock_file required");
    check(o.horizonSec>=0 && o.horizonSec<=60,"horizon_sec: 0..60");
    check(o.warmupSec>=0 && o.cooldownSec>=1 && o.backoffSec>=1,"invalid grace/cooldown/backoff");
    check(o.alpha>0 && o.alpha<=1 && o.anonWeight>=0 && o.anonWeight<=1,"alpha or anon_weight invalid");
    check(o.keepRatio>=0 && o.keepRatio<=1 && o.episodeFraction>0 && o.episodeFraction<=0.5,"keep_ratio or episode_fraction invalid");
    check(o.maxDeclineMBps>0 && o.growthGuardMBps>0 && o.refaultGuardMBps>0 && o.majorGuardPS>0,"guard rates must be positive");
    check(o.ioSomeLimit>0 && o.ioSomeLimit<=100 && o.memoryFullLimit>0 && o.memoryFullLimit<=100 && o.appSomeLimit>0 && o.appSomeLimit<=100,"PSI limits: (0,100]");
    check(o.minBatch>=MiB && o.batch>=o.minBatch && (o.oracleMode || o.batch<=(o.dailyKnownCapacity?1536:(o.dailyKnownStrength?1024:256))*MiB),"batch_mb exceeds mode-specific limit or is below min_batch_mb");
    check(o.perMinute>=o.batch && o.appPerMinute>=o.minBatch && o.logMax>=MiB,"invalid rate/log limits");
    if(o.oracleMode) {
        check(!o.oracleSchedule.empty() && !o.oracleLedger.empty(),"oracle mode requires separate schedule and ledger files");
        check(o.oracleBatch>=MiB && o.oracleGlobalCap>=o.oracleBatch && o.oracleCaseCap>=o.oracleBatch && o.oracleAppCap>=o.oracleBatch,"invalid oracle independent caps");
    }
    check(!o.oracleKnownFutureFreeOnly || (o.oracleMode && o.oracleProofSource=="device" &&
        o.oracleProfile=="peak_transition_os_snapshot" && o.oracleDiskPrepareAnon && o.oracleBackend=="force_swapout"),
        "oracle_known_future_free_only requires device-proven oracle peak-transition disk preparation");
    check(o.dailyKnownLeadSec>=35 && o.dailyKnownLeadSec<=300 && (o.dailyKnownLeadSec==35 || o.dailyKnownCapacity),"daily_known_lead_sec: 35..300; extended horizon requires known-owned capacity");
    check(!o.dailyKnownCapacity || (o.dailyKnownStrength && o.dailyKnownFutureFreeOnly && o.dailyAsync &&
        !o.oracleMode && o.dailyOwnedOsSnapshot && o.dailyDiskMode=="owned_apply" && o.minBatch>=64*MiB),
        "daily_known_capacity requires known-strength exact-owned mixed authority and min_batch_mb>=64");
    check(!o.dailyKnownStrength || (o.dailyKnownFutureFreeOnly && o.dailyAsync && !o.oracleMode &&
        o.dailyOwnedOsSnapshot && o.dailyDiskMode=="owned_apply"),
        "daily_known_strength requires registered known future, owned daily and explicit mixed backend");
    check(!o.dailyKnownFutureFreeOnly || (o.dailyAsync && !o.oracleMode && !o.oracleKnownFutureFreeOnly &&
        !o.dailyRecentReserve && o.oracleProofSource=="legacy" && o.dailyOwnedOsSnapshot &&
        !o.dailyKnownPlan.empty() && o.dailyKnownPlan.front()=='/' && !o.dailyKnownLedger.empty() &&
        o.dailyKnownLedger.front()=='/' && o.dailyKnownPlan!=o.dailyKnownLedger),
        "daily known future requires non-oracle owned daily, separate absolute event/evidence paths, and recent off");
    check(!o.dailyAsync || (!o.oracleMode && o.oracleProofSource=="legacy"),
        "daily_async is non-oracle and cannot consume an experimental device plan");
    check(std::isfinite(o.dailyRecentWindowSec) && o.dailyRecentWindowSec>=1 && o.dailyRecentWindowSec<=60 &&
        std::isfinite(o.dailyRecentHoldSec) && o.dailyRecentHoldSec>=1 && o.dailyRecentHoldSec<=120 &&
        std::isfinite(o.dailyRecentCapFraction) && o.dailyRecentCapFraction>0 && o.dailyRecentCapFraction<=0.5,
        "daily recent reserve window: 1..60, hold: 1..120, cap fraction: (0,0.5]");
    check(!o.dailyRecentReserve || (o.dailyAsync && !o.oracleMode && o.oracleProofSource=="legacy"),
        "daily_recent_reserve requires non-oracle daily_async");
    check(!o.dailyOwnedOsSnapshot || (o.dailyAsync && !o.oracleMode && o.oracleProofSource=="legacy"),
        "daily_owned_os_snapshot requires non-oracle daily_async");
    check(o.dailyDiskMode=="off" || o.dailyDiskMode=="observe" || o.dailyDiskMode=="owned_apply","daily_disk_mode: off/observe/owned_apply");
    check(o.dailyDiskMode=="off" || (o.dailyAsync && !o.oracleMode && o.oracleProofSource=="legacy"),"daily disk requires non-oracle daily_async");
    check(o.dailyDiskMode!="owned_apply" || (o.dailyDiskWholeWork>=o.minBatch &&
        o.dailyDiskWholeWork<=o.perMinute && o.dailyDiskWholeWork<=o.appPerMinute),
        "owned_apply whole-work limit must be explicit and within existing shared/app quotas");
    check(!o.dailyMaxSubmissions || (o.dailyAsync && !o.oracleMode),"daily_max_submissions requires non-oracle daily_async");
    if(o.oracleDeviceDonorTotalKeep<512*MiB) {
        check(o.oracleMode && o.oracleProofSource=="device" && o.oracleProfile=="peak_transition_os_snapshot" &&
            o.oracleDiskPrepareAnon && (o.oracleBackend=="force_shrink_all" || o.oracleBackend=="force_swapout"),
            "lower device donor total keep requires device-proven peak-transition disk-preparation profile");
        check(!c.apps.empty(),"lower device donor total keep requires owned donor entries");
        for(const auto& a:c.apps) {
            bool fixedRoleBundle=false;
            for(unsigned i=1;i<=16;++i) if(a.name=="io.memscene.scene4.workload.a"+std::to_string(i)) fixedRoleBundle=true;
            check(fixedRoleBundle,"lower device donor total keep is restricted to the fixed owned scene4 bundle set");
        }
    }
    if(o.oracleMaxRatio>90) {
        check(o.oracleMode && o.oracleProofSource=="device" && o.oracleProfile=="peak_transition_os_snapshot" &&
            o.oracleDiskPrepareAnon && (o.oracleBackend=="force_shrink_all" || o.oracleBackend=="force_swapout"),
            "oracle max ratio above 90 is restricted to the device-proven peak-transition preparation profile");
        check(!c.apps.empty(),"high-ratio device profile requires fixed owned scene4 apps");
        for(const auto& a:c.apps) {
            bool fixedScene4=false;
            for(unsigned i=1;i<=16;++i) if(a.name=="io.memscene.scene4.workload.a"+std::to_string(i)) fixedScene4=true;
            check(fixedScene4,"high oracle max ratio is restricted to the fixed owned scene4 app set");
        }
    }
    if(o.dailyKnownFutureFreeOnly) {
        std::set<unsigned> owned;
        for(const auto& app:c.apps) if(app.reclaim) {
            unsigned role=0;
            for(unsigned i=1;i<=13;++i) {
                const std::string prefix="100/io.memscene.scene4.workload.a"+std::to_string(i)+"_";
                if(app.path.rfind(prefix,0)==0) {
                    const auto uid=app.path.substr(prefix.size());
                    if(!uid.empty() && uid.find_first_not_of("0123456789")==std::string::npos && uintValue(uid)>0) role=i;
                }
            }
            check(!o.dailyKnownCapacity || app.keep>=128*MiB,"known capacity keep must be at least128MiB");
            check(role && owned.insert(role).second,"daily known future reclaim scope requires unique owned donor leaves a1-a13");
        }
        check(owned.size()==13,"daily known future requires all thirteen registered owned donor leaves");
    }
    check(!c.apps.empty(),"configure at least one [app NAME]");
    for(std::size_t i=0;i<c.apps.size();++i) {
        check(safeRelative(c.apps[i].path),"app path must be non-root, relative, without ..");
        for(std::size_t j=0;j<i;++j)
            check(!ancestor(c.apps[i].path,c.apps[j].path) && !ancestor(c.apps[j].path,c.apps[i].path),"overlapping app cgroups are forbidden");
    }
    return c;
}
struct Control {
    bool paused=false;
    std::set<std::string> protectedApps;
    Bytes boost=0, until=0;
};
inline Control parseControl(const std::string& text) {
    Control c; std::set<std::string> seen;
    std::istringstream in(text); std::string line;
    while(std::getline(in,line)) {
        line=trim(line); if(line.empty() || line.front()=='#') continue;
        auto [k,v]=assignment(line);
        if(!seen.insert(k).second) throw std::runtime_error("duplicate control key");
        if(k=="paused") c.paused=boolValue(v);
        else if(k=="protected_apps") {
            std::istringstream names(v); std::string n;
            while(std::getline(names,n,',')) { n=trim(n); if(!safeName(n)) throw std::runtime_error("invalid protected app name"); c.protectedApps.insert(n); }
        } else if(k=="reserve_extra_mb") c.boost=mbValue(v);
        else if(k=="boost_until_unix") c.until=uintValue(v);
        else throw std::runtime_error("unknown control key: "+k);
    }
    if(seen.size()!=4) throw std::runtime_error("control file must contain all four keys");
    return c;
}
inline std::string controlText(const Control& c) {
    std::string names; for(auto& n:c.protectedApps) { if(!names.empty()) names+=','; names+=n; }
    return "paused="+std::string(c.paused?"true":"false")+"\nprotected_apps="+names+
           "\nreserve_extra_mb="+std::to_string(c.boost/MiB)+"\nboost_until_unix="+std::to_string(c.until)+"\n";
}
} // namespace ms
