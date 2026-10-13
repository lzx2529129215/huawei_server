#include "daily_action_executor.hpp"
#include "daily_action_schedule.hpp"
#include "daily_disk_backend.hpp"
#include "device_daily_evidence.hpp"
#include "device_proc_reader.hpp"
#include "oracle_poll_wake.hpp"
#include "oracle_pipeline_deadline.hpp"
#include "final_authorization.hpp"
#include "postwrite_floor_isolation.hpp"
#include "harmony_v1.hpp"
#include "device_oracle_evidence.hpp"
#include <filesystem>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <thread>
#include <future>
#include <set>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/utsname.h>
#include <unistd.h>
#include <time.h>

namespace ms::harmony_v1 {
namespace fs = std::filesystem;
namespace {
constexpr const char* Schema = "harmony-v1-observe-3";
constexpr Bytes ProbeLimit = 128*MiB;
constexpr Bytes KeepSlack = 128*1024;

void requireOracleDiskBackendEnabled() {
    // Read the observed vendor status interface. Never change global swap policy.
    std::string status;
    try { status=readPath("/sys/block/zram0/hyperhold_enable"); }
    catch(const std::exception&) {
        throw std::runtime_error("disk_backend_unavailable: hyperhold status unreadable; no disk pipeline write permitted");
    }
    if(!oracleDiskBackendEnabledText(status))
        throw std::runtime_error("disk_backend_unavailable: hyperhold or reclaim_in disabled/unknown; no disk pipeline write permitted");
}

std::string csvCell(const std::string& s) {
    std::string out = "\"";
    for (const char c : s) { if (c == '"') out += '"'; out += c; }
    return out + '"';
}

class CSV {
    fs::path path_;
    std::string header_;
    Bytes limit_, size_=0;
    std::ofstream out_;
    void open() {
        size_=fs::exists(path_)?fs::file_size(path_):0;
        if(size_) {
            std::ifstream existing(path_);
            std::string firstLine;
            std::getline(existing,firstLine);
            if(firstLine!=header_) throw std::runtime_error("log schema mismatch: "+path_.string());
        }
        out_.exceptions(std::ios::badbit|std::ios::failbit);
        out_.open(path_,std::ios::app);
        if(!size_) { out_<<header_<<'\n'; out_.flush(); size_=header_.size()+1; }
    }
public:
    CSV(const fs::path& path, std::string header, Bytes limit):path_(path),header_(std::move(header)),limit_(limit) {
        open();
    }
    void row(const std::vector<std::string>& values) {
        std::string line;
        for (const auto& value : values) {
            if (!line.empty()) line += ',';
            line += csvCell(value);
        }
        line += '\n';
        if (size_ + line.size() > limit_) {
            out_.close();
            const auto old=path_.string()+".1";
            if(fs::exists(old)) fs::remove(old);
            fs::rename(path_,old);
            open();
        }
        out_<<line; out_.flush(); size_+=line.size();
    }
};

struct RootInfo {
    unsigned long magic = 0;
    std::string mountPoint;
    bool alias = false;
};

std::string decodeMountField(const std::string& field) {
    std::string out;
    for (std::size_t i = 0; i < field.size(); ++i) {
        if (field[i] == '\\' && i + 3 < field.size() &&
            field[i+1] >= '0' && field[i+1] <= '7' &&
            field[i+2] >= '0' && field[i+2] <= '7' &&
            field[i+3] >= '0' && field[i+3] <= '7') {
            const int value = (field[i+1]-'0')*64 + (field[i+2]-'0')*8 + field[i+3]-'0';
            out.push_back(static_cast<char>(value)); i += 3;
        } else out.push_back(field[i]);
    }
    return out;
}

RootInfo validateRoot(const Options& options) {
    const auto selfCgroup = readPath(options.proc + "/self/cgroup");
    if (!hasMemoryController(selfCgroup))
        throw std::runtime_error("/proc/self/cgroup does not report the memory controller");
    // /proc/mounts is a symlink on this HarmonyOS build; use the equivalent proc self view
    // without relaxing readPath's O_NOFOLLOW rule for general configuration/data files.
    const auto mounts = parseMounts(readPath(options.proc + "/self/mounts"));
    struct stat configured{};
    if (::stat(options.root.c_str(), &configured) != 0) throw error("stat cgroup_root");
    RootInfo info;
    bool found = false;
    for (const auto& mount : mounts) {
        if (mount.fsType != "cgroup") continue;
        if (mount.mountPoint != options.root) {
            struct stat mounted{};
            if (::stat(mount.mountPoint.c_str(), &mounted) != 0) continue;
            if (configured.st_dev != mounted.st_dev || configured.st_ino != mounted.st_ino) continue;
        }
        std::string controllers;
        try { controllers=readPath(mount.mountPoint + "/controllers"); }
        catch(const std::exception&) { continue; }
        std::istringstream names(controllers);
        std::string name;
        bool mountedMemory = false;
        while (names >> name) mountedMemory = mountedMemory || name == "memory";
        if (!mountedMemory) continue;
        struct statfs fsType{};
        if (::statfs(options.root.c_str(), &fsType) != 0) throw error("statfs cgroup_root");
        info.magic = static_cast<unsigned long>(fsType.f_type); // recorded only; HongMeng reports cgroup2 magic on a v1-style hierarchy
        info.mountPoint = mount.mountPoint;
        info.alias = mount.mountPoint != options.root;
        found = true;
        break;
    }
    if (!found) throw std::runtime_error("cgroup_root is not bound to a memory cgroup mount listed in /proc/mounts");
    return info;
}

std::string optionalRaw(int dir, const std::string& file) {
    const auto value = maybeAt(dir,file);
    return value ? trim(*value) : std::string{};
}

std::string rawPSI(const std::string& path, bool& present) {
    try { auto value = trim(readPath(path)); present = !value.empty(); return value; }
    catch (const std::exception&) { present = false; return {}; }
}

std::map<std::string,Bytes> parseKiBFields(const std::string& text) {
    std::map<std::string,Bytes> fields;
    std::istringstream lines(text); std::string line;
    while(std::getline(lines,line)) {
        std::istringstream row(line); std::string key,value,unit,extra;
        if(!(row>>key>>value>>unit) || (row>>extra) || unit!="kB") continue;
        if(!key.empty() && key.back()==':') key.pop_back();
        try { fields[key]=uintValue(value); } catch(const std::exception&) {}
    }
    return fields;
}

//lzx-note
struct ProcessAggregate {
    std::size_t listed=0, statusRead=0, smapsRead=0;
    double smapsElapsedMs=0;
    std::optional<double> smapsCpuMs=0.0;
    std::optional<Bytes> rssKiB, vmSwapKiB, rssAnonKiB, rssFileKiB;
    std::optional<Bytes> pssKiB, pssAnonKiB, pssFileKiB, privateKiB, smapsSwapKiB;
    Bytes minfltDelta=0, majfltDelta=0;
    std::size_t deltaMatched=0;
};

std::optional<double> threadCpuMillis() {
    timespec value{};
    if(::clock_gettime(CLOCK_THREAD_CPUTIME_ID,&value)!=0) return {};
    return static_cast<double>(value.tv_sec)*1000.0+static_cast<double>(value.tv_nsec)/1000000.0;
}

void addMetric(std::optional<Bytes>& total, const std::optional<Bytes>& value) {
    if(!value) { total.reset(); return; }
    if(!total) total=*value;
    else total=add(*total,*value);
}

std::vector<std::uint64_t> processIds(int dir) {
    std::vector<std::uint64_t> ids;
    const auto text=readAt(dir,"cgroup.procs");
    std::istringstream lines(text); std::string line;
    while(std::getline(lines,line)) {
        line=trim(line); if(line.empty()) continue;
        try { ids.push_back(uintValue(line)); } catch(const std::exception&) {}
    }
    return ids;
}

struct CgroupMemberObservation {
    bool readOk=false;
    bool parseValid=false;
    std::size_t entryCount=0;
    std::size_t invalidCount=0;
    std::vector<std::uint64_t> uniquePids;
    std::string raw;
};

CgroupMemberObservation observeCgroupMembers(int dir) {
    CgroupMemberObservation observation;
    try { observation.raw=readAt(dir,"cgroup.procs"); }
    catch(const std::exception&) { return observation; }
    observation.readOk=true;
    observation.parseValid=true;
    std::set<std::uint64_t> unique;
    std::istringstream lines(observation.raw);
    std::string line;
    while(std::getline(lines,line)) {
        line=trim(line);
        if(line.empty()) continue;
        try {
            const auto pid=uintValue(line);
            if(pid==0) { observation.parseValid=false; ++observation.invalidCount; }
            else { ++observation.entryCount; unique.insert(pid); }
        } catch(const std::exception&) { observation.parseValid=false; ++observation.invalidCount; }
    }
    observation.uniquePids.assign(unique.begin(),unique.end());
    return observation;
}

std::string safeMemberRaw(const std::string& raw) {
    std::string out;
    out.reserve(std::min<std::size_t>(raw.size(),256));
    for(unsigned char c:raw) {
        if(out.size()>=256) { out+="..."; break; }
        if(c=='\\' || c=='"') { out.push_back('\\'); out.push_back(static_cast<char>(c)); }
        else if(c=='\n') out+="\\n";
        else if(c=='\r') out+="\\r";
        else if(c=='\t') out+="\\t";
        else if(c>=32 && c<127) out.push_back(static_cast<char>(c));
        else out.push_back('?');
    }
    return out;
}

//lzx-note
/**
 * 逐个打开配置中的cgroup目录，读取组统计和成员进程
 * 再读取 /proc/<pid>/status, smaps_rollup, stat 等信息
 */
ProcessAggregate sampleProcesses(const std::vector<std::uint64_t>& ids,
    const std::map<std::uint64_t,ProcStat>& previous,std::map<std::uint64_t,ProcStat>& current,
    bool readSmaps=true) {
    ProcessAggregate total; total.listed=ids.size();
    std::optional<Bytes> rss,swap,rssAnon,rssFile,pss,pssAnon,pssFile,privatePages,smapsSwap;
    bool statusComplete=true,smapsComplete=readSmaps;
    for(const auto pid:ids) {
        try {
            const auto status=parseProcMemory(readPath("/proc/"+std::to_string(pid)+"/status"),{});
            ++total.statusRead;
            if(!status.rssKiB || !status.vmSwapKiB || !status.rssAnonKiB || !status.rssFileKiB) statusComplete=false;
            addMetric(rss,status.rssKiB); addMetric(swap,status.vmSwapKiB);
            addMetric(rssAnon,status.rssAnonKiB); addMetric(rssFile,status.rssFileKiB);
        } catch(const std::exception&) { statusComplete=false; }
        if(readSmaps) {
            const auto smapsStart=std::chrono::steady_clock::now();
            const auto cpuStart=threadCpuMillis();
            try {
                const auto smaps=parseProcMemory({},readPath("/proc/"+std::to_string(pid)+"/smaps_rollup"));
                if(!smaps.pssKiB || !smaps.pssAnonKiB || !smaps.pssFileKiB || !smaps.privateKiB || !smaps.smapsSwapKiB) smapsComplete=false;
                addMetric(pss,smaps.pssKiB); addMetric(pssAnon,smaps.pssAnonKiB);
                addMetric(pssFile,smaps.pssFileKiB); addMetric(privatePages,smaps.privateKiB);
                addMetric(smapsSwap,smaps.smapsSwapKiB); ++total.smapsRead;
            } catch(const std::exception&) { smapsComplete=false; }
            total.smapsElapsedMs+=std::chrono::duration<double,std::milli>(
                std::chrono::steady_clock::now()-smapsStart).count();
            const auto cpuEnd=threadCpuMillis();
            if(total.smapsCpuMs && cpuStart && cpuEnd && *cpuEnd>=*cpuStart)
                *total.smapsCpuMs+=*cpuEnd-*cpuStart;
            else total.smapsCpuMs.reset();

        }
        try {
            const auto stat=parseProcStat(pid,readPath("/proc/"+std::to_string(pid)+"/stat"));
            current[pid]=stat;
            const auto prior=previous.find(pid);
            if(prior!=previous.end() && prior->second.startTicks==stat.startTicks &&
               stat.minflt>=prior->second.minflt && stat.majflt>=prior->second.majflt) {
                total.minfltDelta=add(total.minfltDelta,stat.minflt-prior->second.minflt);
                total.majfltDelta=add(total.majfltDelta,stat.majflt-prior->second.majflt);
                ++total.deltaMatched;
            }
        } catch(const std::exception&) {}
    }
    total.rssKiB=statusComplete?rss:std::nullopt;
    total.vmSwapKiB=statusComplete?swap:std::nullopt;
    total.rssAnonKiB=statusComplete?rssAnon:std::nullopt;
    total.rssFileKiB=statusComplete?rssFile:std::nullopt;
    total.pssKiB=smapsComplete?pss:std::nullopt;
    total.pssAnonKiB=smapsComplete?pssAnon:std::nullopt;
    total.pssFileKiB=smapsComplete?pssFile:std::nullopt;
    total.privateKiB=smapsComplete?privatePages:std::nullopt;
    total.smapsSwapKiB=smapsComplete?smapsSwap:std::nullopt;
    return total;
}

std::string optionalNumber(const std::optional<Bytes>& value) {
    return value?std::to_string(*value):std::string{};
}

std::string rawCounter(const std::string& text,const std::string& wanted) {
    std::istringstream lines(text); std::string line;
    while(std::getline(lines,line)) {
        std::istringstream row(line); std::string key,value;
        if(row>>key>>value && key==wanted) return value;
    }
    return {};
}

std::string appPopulated(int dir) {
    const auto text = maybeAt(dir,"cgroup.procs");
    if (!text) return "unknown";
    std::istringstream in(*text); std::string pid;
    return (in >> pid) ? "yes" : "no";
}

struct ProbeIdentity { std::uint64_t pid=0; ProcStat stat; std::string exe; };
bool processInAppCgroup(const std::string& cg,const std::string& appPath) {
    std::istringstream lines(cg); std::string line;
    while(std::getline(lines,line)) {
        const auto first=line.find(':');
        const auto second=first==std::string::npos?first:line.find(':',first+1);
        if(second==std::string::npos) continue;
        std::istringstream controllers(line.substr(first+1,second-first-1));
        std::string controller; bool memory=false;
        while(std::getline(controllers,controller,',')) memory=memory || controller=="memory";
        if(memory && line.substr(second+1)=="/"+appPath) return true;
    }
    return false;
}
ProbeIdentity validateProbe(int dir,const std::string& root,const std::string& path) {
    if(root!="/dev/memcg" || path.rfind("memshift_probe_",0)!=0 || path.find('/')!=std::string::npos)
        throw std::runtime_error("ratio apply scope must be a direct /dev/memcg/memshift_probe_* group");
    const auto ids=processIds(dir);
    if(ids.size()!=1) throw std::runtime_error("probe group must contain exactly one process");
    ProbeIdentity identity; identity.pid=ids.front();
    const std::string proc="/proc/"+std::to_string(identity.pid);
    identity.stat=parseProcStat(identity.pid,readPath(proc+"/stat"));
    identity.exe=fs::read_symlink(proc+"/exe").string();
    if(identity.exe.empty() || identity.exe.size()<std::string("harmony_v1_isolated_workload").size() ||
       identity.exe.substr(identity.exe.size()-std::string("harmony_v1_isolated_workload").size())!="harmony_v1_isolated_workload")
        throw std::runtime_error("probe PID executable is not harmony_v1_isolated_workload");
    const auto cg=readPath(proc+"/cgroup");
    if(!processInAppCgroup(cg,path)) throw std::runtime_error("probe PID cgroup identity mismatch");
    const auto cur=uintValue(trim(readAt(dir,"memory.current")));
    if(cur>ProbeLimit) throw std::runtime_error("probe cgroup exceeds 128 MiB cap");
    return identity;
}

bool backgroundEligible(int dir,const App& app,const std::vector<std::uint64_t>& expectedIds,
                       const std::map<std::uint64_t,ProcStat>& sampled,
                       const std::atomic<bool>* cancel=nullptr) {
    try {
        if(expectedIds.empty() || processIds(dir)!=expectedIds) return false;
        const auto groupScore=maybeAt(dir,"memory.app_score");
        if(!groupScore) return false;
        const auto parsedGroupScore=uintValue(trim(*groupScore));
        if(parsedGroupScore<(app.wpsWindowGate?400U:1U)) return false;
        std::map<std::uint64_t,std::uint64_t> members;
        std::uint64_t wpsMain=0;
        const auto identitiesMatch=[&] {
            if(processIds(dir)!=expectedIds) return false;
            wpsMain=0;members.clear();
            for(const auto pid:expectedIds) {
                const auto prior=sampled.find(pid);
                if(prior==sampled.end()) return false;
                const std::string proc="/proc/"+std::to_string(pid);
                const auto now=parseProcStat(pid,readPath(proc+"/stat"));
                if(now.startTicks!=prior->second.startTicks ||
                    !processInAppCgroup(readPath(proc+"/cgroup"),app.path)) return false;
                const auto score=trim(readPath(proc+"/oom_score_adj"));
                std::size_t used=0;const auto value=std::stol(score,&used);
                if(used!=score.size()) return false;
                if(!app.wpsWindowGate) {if(value<=0) return false;continue;}
                if(value<=-900) return false;
                std::istringstream status(readPath(proc+"/status"));std::string line;bool uidOk=false;
                while(std::getline(status,line)) if(line.rfind("Uid:",0)==0) {
                    std::istringstream row(line.substr(4));std::uint64_t uid=0;
                    uidOk=static_cast<bool>(row>>uid) && uid==20020028;break;
                }
                if(!uidOk) return false;
                const auto cmd=readPath(proc+"/cmdline");const auto name=cmd.substr(0,cmd.find('\0'));
                if(name=="cn.wps.office.hap") {if(wpsMain) return false;wpsMain=pid;}
                else if(name.rfind("cn.wps.office.hap:",0)!=0) return false;
                members[pid]=now.startTicks;
            }
            return true;
        };
        if(!identitiesMatch()) return false;
        if(!app.wpsWindowGate) return true;
        if(!wpsMain || app.name!="wps" || app.path!="100/cn.wps.office.hap_20020028") return false;
        static const std::atomic<bool> neverCancel{false};
        const auto proof=sampleWpsWindowEvidence(members,wpsMain,20020028,cancel?*cancel:neverCancel);
        return proof.verified && identitiesMatch() &&
            uintValue(trim(readAt(dir,"memory.app_score")))>=400 && device_proc::boot()-proof.oldest<=2.5;
    } catch(const std::exception&) {return false;}
}

std::optional<Bytes> aggregateRssAnon(const std::vector<std::uint64_t>& ids) {
    if(ids.empty()) return {};
    Bytes total=0;
    for(const auto pid:ids) {
        try {
            const auto status=parseProcMemory(readPath("/proc/"+std::to_string(pid)+"/status"),{});
            if(!status.rssAnonKiB) return {};
            total=add(total,*status.rssAnonKiB);
        } catch(const std::exception&) { return {}; }
    }
    return total;
}

bool sameProbe(int dir,const std::string& root,const std::string& path,const ProbeIdentity& before) {
    const auto after=validateProbe(dir,root,path);
    return after.pid==before.pid && after.stat.startTicks==before.stat.startTicks && after.exe==before.exe;
}


std::optional<Bytes> dailyWorkerGap(const Options& options,const System& sys,const Forecast& forecast,
    const daily::Request& request,Bytes boost,const std::shared_ptr<daily::recent::Store>& store,
    const std::shared_ptr<DeviceOracleEvidence>& knownEvidence) {
    if(options.dailyKnownFutureFreeOnly) {
        if(!knownEvidence || !request.knownEvent) return {};
        const auto latest=knownEvidence->current("daily_final:"+request.actionId);
        if(!daily::known::sameRegistration(*request.knownEvent,latest) ||
           !daily::known::memberMatches(latest,request.groupPath,request.members)) return {};
        // Event lookup precedes the final control and Free/deadline read.
        const auto control=parseControl(readPath(options.control));
        if(control.paused || daily::disk::targetProtected(options,request.app,request.groupPath,control.protectedApps)) return Bytes{0};
        const auto fresh=systemSample(options.proc);
        return daily::known::gap(latest,options.reserve,fresh,monotonic(),options.dailyKnownLeadSec);
    }
    const Bytes target=options.dailyRecentReserve?daily::recent::workerTarget(options,request.recentTarget,
        store->read(),sys.time,sys.total):options.readyFree;
    return daily::refreshedGap(options,sys,forecast,request.forecastSampledAt,boost,target);
}

// Owned worker adapter: no references to mutable sampling state or log streams.
daily::Result performDailyAction(const Options& options,const App& app,const daily::Request& request,
    const std::map<std::uint64_t,ProcStat>& expected,const std::optional<ProbeIdentity>& probeIdentity,
    bool probeProfile,const Forecast& forecast,const std::function<bool()>& stopping,
    const std::atomic<bool>& cancelled,const std::shared_ptr<daily::recent::Store>& recentStore,
    const std::shared_ptr<DeviceOracleEvidence>& knownEvidence) {
    daily::Result rejected; rejected.request=request;
    try {
        if(!daily::capacityAdapterAllows(options,request,probeProfile) ||
           (request.knownCapacity && (request.groupPath!=app.path || request.keepBytes!=std::max(app.keep,128*MiB))))
            throw std::runtime_error("known_capacity_adapter_authority");
        if(request.knownStrength && (!options.dailyKnownStrength || !options.dailyKnownFutureFreeOnly ||
           probeProfile || !daily::disk::ownedQualification(options,app.path,request.members.size(),probeProfile) ||
           !daily::strengthAuthority(request))) throw std::runtime_error("known_strength_adapter_authority");
        FD root(::open(options.root.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW));
        if(root.get()<0) throw error("daily root open");
        FD dir=openBeneath(root.get(),app.path);
        // Open before collecting the final write authority. No control write on open.
        FD writer(::openat(dir.get(),"memory.force_shrink_anon",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
        if(writer.get()<0) throw error("daily force_shrink_anon open");
        const auto fresh=[&]() {
            daily::Evidence e; struct stat group{};
            if(::fstat(dir.get(),&group)!=0) throw error("daily group fstat");
            e.groupDevice=static_cast<std::uint64_t>(group.st_dev); e.groupInode=static_cast<std::uint64_t>(group.st_ino);
            const auto ids=processIds(dir.get());
            for(const auto pid:ids) {const auto p=parseProcStat(pid,readPath(options.proc+"/"+std::to_string(pid)+"/stat"));
                e.members[pid]=p.startTicks;e.faultCounters[pid]={p.minflt,p.majflt};}
            const auto anon=aggregateRssAnon(ids); const auto stat=maybeAt(dir.get(),"memory.stat");
            const auto vendor=parseVendorStat(stat.value_or(std::string{}));
            if(!anon || !vendor.anonKiB || *anon>Unlimited/1024 || *vendor.anonKiB>Unlimited/1024)
                throw std::runtime_error("daily RSS/vendor anonymous bytes unknown");
            e.rssAnonBytes=*anon*1024; e.denominatorBytes=*vendor.anonKiB*1024;
            const auto sys=systemSample(options.proc);
            const auto latest=parseControl(readPath(options.control));
            const Bytes boost=latest.until>unixSeconds()?latest.boost:0;
            const auto refreshed=dailyWorkerGap(options,sys,forecast,request,boost,recentStore,knownEvidence);
            e.systemGap=refreshed.value_or(0); e.swapRoom=sub(sys.swapFree,options.swapReserve);
            if(!app.reclaim || !options.allowAnon || options.swappiness==0) e.blocked="anonymous_reclaim_disabled";
            else e.blocked=daily::harmonyUnverifiedPsiBlock(options,sys);
            if(e.blocked.empty() && !refreshed) e.blocked="forecast_expired";
            const auto appPressure=maybeAt(dir.get(),"memory.pressure");
            const auto appPsi=appPressure?parsePSI(*appPressure):PSI{};
            if(e.blocked.empty()) e.blocked=daily::harmonyUnverifiedPsiBlock(options,sys,appPsi);
            e.controlActive=!latest.paused && !daily::disk::targetProtected(options,app.name,app.path,latest.protectedApps) && !stopping() && !cancelled.load();
            if(daily::disk::targetProtected(options,app.name,app.path,latest.protectedApps)) {
                e.blocked="runtime_control_protected";return e; // Before selecting proof or score eligibility.
            }
            if(request.qualification=="owned_os_snapshot" && !probeProfile &&
               daily::disk::ownedQualification(options,app.path,request.members.size(),probeProfile)) {
                const auto proof=sampleDeviceDailyEvidence(app.path,request.groupDevice,request.groupInode,request.members,cancelled);
                e.background=proof.verified;e.authorityExpires=proof.verified?proof.oldest+2.5:0;
                if(proof.aaParsed) e.qualificationDiagnostic="aa_matches="+std::to_string(proof.aaMatchCount)+
                    ";aa_state="+proof.aaState+";aa_reason="+proof.aaReason;
                if(e.blocked.empty() && !proof.verified) e.blocked="owned_device_snapshot:"+proof.reason;
                // Evidence collection can be slow: refresh the system/control gates after it.
                const auto latestSys=systemSample(options.proc);const auto latestControl=parseControl(readPath(options.control));
                const auto latestGap=dailyWorkerGap(options,latestSys,forecast,request,
                    latestControl.until>unixSeconds()?latestControl.boost:0,recentStore,knownEvidence);
                e.systemGap=latestGap.value_or(0);e.swapRoom=sub(latestSys.swapFree,options.swapReserve);
                if(e.blocked.empty() && !latestGap) e.blocked="forecast_expired";
                if(e.blocked.empty()) e.blocked=daily::harmonyUnverifiedPsiBlock(options,latestSys);
                e.controlActive=!latestControl.paused && !daily::disk::targetProtected(options,app.name,app.path,latestControl.protectedApps) &&
                    !stopping() && !cancelled.load();
                const auto refreshedIds=processIds(dir.get());e.members.clear();
                e.faultCounters.clear();
                for(const auto pid:refreshedIds) {const auto p=parseProcStat(pid,readPath(options.proc+"/"+std::to_string(pid)+"/stat"));
                    e.members[pid]=p.startTicks;e.faultCounters[pid]={p.minflt,p.majflt};}
                const auto refreshedAnon=aggregateRssAnon(refreshedIds);
                const auto refreshedStat=parseVendorStat(maybeAt(dir.get(),"memory.stat").value_or(std::string{}));
                if(!refreshedAnon || !refreshedStat.anonKiB || *refreshedAnon>Unlimited/1024 || *refreshedStat.anonKiB>Unlimited/1024)
                    throw std::runtime_error("owned fresh anonymous fields unknown");
                e.rssAnonBytes=*refreshedAnon*1024;e.denominatorBytes=*refreshedStat.anonKiB*1024;
                const auto finalPressure=maybeAt(dir.get(),"memory.pressure");const auto finalPsi=finalPressure?parsePSI(*finalPressure):PSI{};
                if(e.blocked.empty()) e.blocked=daily::harmonyUnverifiedPsiBlock(options,latestSys,finalPsi);
            } else e.background=probeProfile?(probeIdentity && sameProbe(dir.get(),options.root,app.path,*probeIdentity)):
                backgroundEligible(dir.get(),app,ids,expected,&cancelled);
            return e;
        };
        daily::Hooks hooks;
        hooks.probe=fresh;
        hooks.write=[&](const std::string& payload) {
            errno=0; const auto n=::write(writer.get(),payload.data(),payload.size());
            const int saved=n<0?errno:0;
            return daily::WriteReceipt{static_cast<long>(n),saved};
        };
        hooks.feedbackDelay=[] { std::this_thread::sleep_for(std::chrono::milliseconds(250)); };
        hooks.feedback=[&] {
            const auto e=fresh();
            return daily::Feedback{e.groupDevice==request.groupDevice && e.groupInode==request.groupInode &&
                e.members==request.members && e.background && e.controlActive && e.blocked.empty(),e.rssAnonBytes,e.faultCounters};
        };
        hooks.now=[] { return monotonic(); };
        hooks.cancelled=[&] { return stopping() || cancelled.load(); };
        if(options.dailyRecentReserve || options.dailyKnownFutureFreeOnly) hooks.finalGap=[&]() -> std::optional<Bytes> {
            const auto sys=systemSample(options.proc);const auto control=parseControl(readPath(options.control));
            if(control.paused || daily::disk::targetProtected(options,app.name,app.path,control.protectedApps) ||
               stopping() || cancelled.load()) return Bytes{0};
            return dailyWorkerGap(options,sys,forecast,request,control.until>unixSeconds()?control.boost:0,recentStore,knownEvidence);
        };
        hooks.ratioForBudget=[](Bytes cap,Bytes denominator) { return ratioForBudget(cap,denominator); };
        auto result=daily::execute(request,hooks);
        if(request.qualification=="owned_os_snapshot" && result.writeAttempted.value_or(false) &&
           result.writeLength && *result.writeLength>=0 && (!result.feedbackVerified || result.uncertain))
            result.uncertain=true; // Do not continue an owned two-stage chain after unknown preparation.
        return result;
    } catch(const std::exception& e) { rejected.status="rejected:"+std::string(e.what()); return rejected; }
}

daily::Result performDailyDiskAction(const Options& options,const App& app,const daily::Request& request,
    const std::map<std::uint64_t,ProcStat>& expected,const Forecast& forecast,
    const std::function<bool()>& stopping,const std::atomic<bool>& cancelled,
    const std::shared_ptr<daily::recent::Store>& recentStore,
    const std::shared_ptr<DeviceOracleEvidence>& knownEvidence) {
    daily::Result rejected;rejected.request=request;
    try {
        if(!daily::capacityAdapterAllows(options,request) ||
           (request.knownCapacity && (request.groupPath!=app.path || request.keepBytes!=std::max(app.keep,128*MiB))))
            throw std::runtime_error("known_capacity_adapter_authority");
        if(request.knownStrength && (!options.dailyKnownStrength || !options.dailyKnownFutureFreeOnly ||
           !daily::strengthAuthority(request))) throw std::runtime_error("known_strength_adapter_authority");
        if(request.qualification!="owned_os_snapshot" || !daily::disk::diskApplyEnabled(options,app.path,request.members.size()))
            throw std::runtime_error("disk_requires_explicit_owned_singleton_mode");
        FD root(::open(options.root.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW));
        if(root.get()<0) throw error("daily disk root open");
        FD dir=openBeneath(root.get(),app.path);
        FD writer(::openat(dir.get(),"memory.force_swapout",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
        if(writer.get()<0) throw error("daily force_swapout open");
        daily::disk::Hooks hooks;
        hooks.probe=[&] {
            daily::disk::Evidence e;struct stat group{};
            if(::fstat(dir.get(),&group)!=0) throw error("daily disk group fstat");
            e.base.groupDevice=static_cast<std::uint64_t>(group.st_dev);
            e.base.groupInode=static_cast<std::uint64_t>(group.st_ino);
            const auto initialControl=parseControl(readPath(options.control));
            if(initialControl.paused || daily::disk::targetProtected(options,app.name,app.path,initialControl.protectedApps)) {
                e.base.blocked="runtime_control_protected";return e;
            }
            // Guard read occurs before final AA/WMS/proc proof. No global writes.
            requireOracleDiskBackendEnabled();e.backendEnabled=true;
            const auto proof=sampleDeviceDailyEvidence(app.path,request.groupDevice,request.groupInode,request.members,cancelled);
            e.deviceSnapshotVerified=proof.verified;e.oldestProof=proof.oldest;e.proofReason=proof.reason;
            const auto ids=processIds(dir.get());
            for(const auto pid:ids) {const auto p=parseProcStat(pid,readPath(options.proc+"/"+std::to_string(pid)+"/stat"));
                e.base.members[pid]=p.startTicks;e.base.faultCounters[pid]={p.minflt,p.majflt};}
            const auto anon=aggregateRssAnon(ids);const auto raw=maybeAt(dir.get(),"memory.stat");
            const auto pool=raw?parseOracleDiskPool(*raw):std::optional<OracleDiskPool>{};
            if(!anon || *anon>Unlimited/1024) throw std::runtime_error("daily disk anonymous RSS unknown");
            e.base.rssAnonBytes=*anon*1024;e.base.denominatorBytes=e.base.rssAnonBytes;
            if(pool) {e.poolKnown=true;e.zramBytes=pool->zramBytes;e.eswapBytes=pool->eswapBytes;}
            const auto sys=systemSample(options.proc);const auto control=parseControl(readPath(options.control));
            const auto gap=dailyWorkerGap(options,sys,forecast,request,control.until>unixSeconds()?control.boost:0,recentStore,knownEvidence);
            e.base.systemGap=gap.value_or(0);e.base.swapRoom=sub(sys.swapFree,options.swapReserve);
            if(!app.reclaim || !options.allowAnon || options.swappiness==0) e.base.blocked="anonymous_reclaim_disabled";
            else e.base.blocked=daily::harmonyUnverifiedPsiBlock(options,sys);
            if(e.base.blocked.empty() && !gap) e.base.blocked="forecast_expired";
            const auto pressure=maybeAt(dir.get(),"memory.pressure");const auto psi=pressure?parsePSI(*pressure):PSI{};
            if(e.base.blocked.empty()) e.base.blocked=daily::harmonyUnverifiedPsiBlock(options,sys,psi);
            e.base.controlActive=!control.paused && !daily::disk::targetProtected(options,app.name,app.path,control.protectedApps) &&
                !stopping() && !cancelled.load();
            e.base.background=proof.verified;
            // The provider validates exact singleton membership/start/UID/cgroup.
            // Do not import the office score/adj heuristic into this owned-only scope.
            (void)expected;
            return e;
        };
        hooks.write=[&](const std::string& command) {errno=0;const auto n=::write(writer.get(),command.data(),command.size());
            const int saved=n<0?errno:0;return daily::WriteReceipt{static_cast<long>(n),saved};};
        hooks.feedbackDelay=[] {std::this_thread::sleep_for(std::chrono::milliseconds(250));};
        hooks.now=[] {return monotonic();};hooks.cancelled=[&] {return stopping() || cancelled.load();};
        if(options.dailyRecentReserve || options.dailyKnownFutureFreeOnly) hooks.finalGap=[&]() -> std::optional<Bytes> {
            const auto sys=systemSample(options.proc);const auto control=parseControl(readPath(options.control));
            if(control.paused || daily::disk::targetProtected(options,app.name,app.path,control.protectedApps) ||
               stopping() || cancelled.load()) return Bytes{0};
            const auto gap=dailyWorkerGap(options,sys,forecast,request,control.until>unixSeconds()?control.boost:0,recentStore,knownEvidence);
            if(!request.pairedAfter.empty()) {
                daily::Evidence e;struct stat group{};
                if(::fstat(dir.get(),&group)!=0) throw error("paired final group");
                if(static_cast<std::uint64_t>(group.st_dev)!=request.groupDevice ||
                   static_cast<std::uint64_t>(group.st_ino)!=request.groupInode) throw std::runtime_error("paired final group changed");
                const auto ids=processIds(dir.get());
                for(const auto pid:ids) {const auto p=parseProcStat(pid,readPath(options.proc+"/"+std::to_string(pid)+"/stat"));
                    e.members[pid]=p.startTicks;e.faultCounters[pid]={p.minflt,p.majflt};}
                if(e.members!=request.members) throw std::runtime_error("paired final members changed");
                const auto anon=aggregateRssAnon(ids);
                if(!anon || *anon>Unlimited/1024) throw std::runtime_error("paired final RSS unknown");
                e.rssAnonBytes=*anon*1024;
                if(const auto why=daily::pairProtection(request,e);!why.empty()) throw std::runtime_error(why);
            }
            return gap;
        };
        return daily::disk::execute(request,options.dailyDiskWholeWork,hooks);
    } catch(const std::exception& e) {rejected.status="disk_rejected:"+std::string(e.what());return rejected;}
}

std::vector<std::string> splitPipe(const std::string& line) {
    std::vector<std::string> fields; std::istringstream in(line); std::string field;
    while(std::getline(in,field,'|')) fields.push_back(trim(field));
    return fields;
}
OracleRequest parseOracleRequest(const std::string& text) {
    std::istringstream in(text); std::string header,row;
    if(!std::getline(in,header) || !std::getline(in,row)) throw std::runtime_error("oracle schedule requires header and one request row");
    const std::string base="event_id|case_id|launch_boottime_s|planned_needed_bytes|";
    const std::string donorFields="bundle|uid|pid|start_ticks|cold_ready|window_ready|cold_ready_boottime_s|background_since_s|last_heartbeat_s|aa_pid|aa_uid|aa_state|aa_observed_boottime_s|window_visible|window_focused|window_observed_boottime_s|proc_verified|proc_start_ticks|proc_observed_boottime_s|lifecycle_clear|lifecycle_observed_boottime_s|background_evidence_source";
    auto prefixedFields=[](const std::string& prefix,const std::string& fields) { std::istringstream names(fields); std::ostringstream result; std::string name; bool first=true; while(std::getline(names,name,'|')) { if(!first) result<<'|'; result<<prefix<<name; first=false; } return result.str(); };
    std::string extra; if(std::getline(in,extra) && !trim(extra).empty()) throw std::runtime_error("oracle schedule must contain exactly one request");
    const std::string legacyHeader=base+prefixedFields("donor_a_",donorFields)+"|"+prefixedFields("donor_b_",donorFields);
    auto f=splitPipe(row); OracleRequest r;
    if(trim(header)==legacyHeader) {
        if(f.size()!=48) throw std::runtime_error("oracle schedule field count mismatch");
        r.id=f[0]; r.caseId=f[1]; r.launch=realValue(f[2]); r.needed=uintValue(f[3]);
        for(int i=0;i<2;++i) { const auto n=static_cast<std::size_t>(4+i*22); OracleDonor d;
            d.bundle=f[n]; d.uid=f[n+1]; d.pid=uintValue(f[n+2]); d.start=uintValue(f[n+3]); d.coldReady=boolValue(f[n+4]); d.dailyReady=d.coldReady; d.windowReady=boolValue(f[n+5]); d.readyAt=realValue(f[n+6]); d.backgroundSince=realValue(f[n+7]); d.lastHeartbeat=realValue(f[n+8]);
            d.aaPid=uintValue(f[n+9]); d.aaUid=uintValue(f[n+10]); d.aaState=f[n+11]; d.aaObserved=realValue(f[n+12]); d.windowVisible=boolValue(f[n+13]); d.windowFocused=boolValue(f[n+14]); d.windowObserved=realValue(f[n+15]); d.procVerified=boolValue(f[n+16]); d.procStart=uintValue(f[n+17]); d.procObserved=realValue(f[n+18]); d.lifecycleClear=boolValue(f[n+19]); d.lifecycleObserved=realValue(f[n+20]); d.evidenceSource=f[n+21]; r.donors.push_back(d);
        }
    } else {
        const std::string fields="bundle|uid|pid|start_ticks|daily_ready|window_ready|daily_ready_boottime_s|background_since_s|aa_pid|aa_uid|aa_state|aa_observed_boottime_s|window_visible|window_focused|window_observed_boottime_s|proc_verified|proc_start_ticks|proc_observed_boottime_s|lifecycle_clear|lifecycle_observed_boottime_s|background_evidence_source";
        std::ostringstream expectedV2,expectedV3,expectedV3_16,expectedV2_16;
        expectedV2<<"schema_version|event_id|case_id|launch_boottime_s|planned_needed_bytes|donor_count";
        expectedV3<<"schema_version|event_id|case_id|launch_boottime_s|planned_needed_bytes|donor_count";
        for(unsigned i=1;i<=8;++i) {
            const auto prefix="donor_"+std::to_string(i)+"_";
            expectedV2<<'|'<<prefixedFields(prefix,fields);
            expectedV3<<'|'<<prefixedFields(prefix,fields+"|writeback_file_path|writeback_file_dev|writeback_file_ino|writeback_file_size");
        }
        expectedV2_16<<expectedV2.str();
        for(unsigned i=9;i<=16;++i) expectedV2_16<<'|'<<prefixedFields("donor_"+std::to_string(i)+"_",fields);
        expectedV3_16<<expectedV3.str();
        for(unsigned i=9;i<=16;++i) expectedV3_16<<'|'<<prefixedFields("donor_"+std::to_string(i)+"_",fields+"|writeback_file_path|writeback_file_dev|writeback_file_ino|writeback_file_size");
        const bool schema3=trim(header)==expectedV3.str() || trim(header)==expectedV3_16.str();
        if(trim(header)!=expectedV2.str() && trim(header)!=expectedV2_16.str() && !schema3) throw std::runtime_error("oracle schedule header mismatch");
        if(f.size()<6 || (schema3?f[0]!="3":f[0]!="2")) throw std::runtime_error("oracle schedule schema version mismatch");
        r.schemaVersion=schema3?3U:2U; r.id=f[1]; r.caseId=f[2]; r.launch=realValue(f[3]); r.needed=uintValue(f[4]);
        const auto count=uintValue(f[5]); const std::size_t donorWidth=schema3?25U:21U;
        if(count<1 || count>16U || f.size()!=6+count*donorWidth) throw std::runtime_error(schema3?"oracle v3 donor count/field count mismatch":"oracle v2 donor count/field count mismatch");
        std::set<std::pair<std::uint64_t,std::uint64_t>> fileIds;
        for(std::size_t i=0;i<count;++i) { const auto n=6+i*donorWidth; OracleDonor d;
            d.bundle=f[n]; d.uid=f[n+1]; d.pid=uintValue(f[n+2]); d.start=uintValue(f[n+3]); d.dailyReady=boolValue(f[n+4]); d.windowReady=boolValue(f[n+5]); d.readyAt=realValue(f[n+6]); d.backgroundSince=realValue(f[n+7]);
            d.aaPid=uintValue(f[n+8]); d.aaUid=uintValue(f[n+9]); d.aaState=f[n+10]; d.aaObserved=realValue(f[n+11]); d.windowVisible=boolValue(f[n+12]); d.windowFocused=boolValue(f[n+13]); d.windowObserved=realValue(f[n+14]); d.procVerified=boolValue(f[n+15]); d.procStart=uintValue(f[n+16]); d.procObserved=realValue(f[n+17]); d.lifecycleClear=boolValue(f[n+18]); d.lifecycleObserved=realValue(f[n+19]); d.evidenceSource=f[n+20]; r.donors.push_back(d);
            if(schema3) {
                d.writebackFilePath=f[n+21]; d.writebackFileDev=uintValue(f[n+22]); d.writebackFileIno=uintValue(f[n+23]); d.writebackFileSize=uintValue(f[n+24]);
                if(d.writebackFilePath!="memscene_"+r.caseId+".pages" || !d.writebackFileDev || !d.writebackFileIno || !d.writebackFileSize ||
                   !fileIds.insert({d.writebackFileDev,d.writebackFileIno}).second)
                    throw std::runtime_error("oracle v3 writeback file registration invalid or duplicate");
                r.donors.back()=d;
            }
        }
    }
    if(r.id.empty() || r.caseId.empty()) throw std::runtime_error("oracle event fields invalid");
    if(!safeName(r.id) || !safeName(r.caseId) || r.launch<=0 || r.needed==0) throw std::runtime_error("oracle event fields invalid");
    std::set<std::string> bundles,leaves; std::set<std::uint64_t> pids,uids;
    for(auto& d:r.donors) {
        const bool readiness=r.schemaVersion>=2?d.dailyReady:d.coldReady;
        if(d.bundle.empty() || d.bundle.find('/')!=std::string::npos || d.uid.empty() || !d.pid || !d.start || !readiness || !d.windowReady || d.readyAt<=0 || d.backgroundSince<=0 || d.backgroundSince<d.readyAt || d.aaPid!=d.pid || d.aaUid!=uintValue(d.uid) || d.aaState!="BACKGROUND" || d.aaObserved<=0 || d.windowVisible || d.windowFocused || d.windowObserved<=0 || !d.procVerified || d.procStart!=d.start || d.procObserved<=0 || !d.lifecycleClear || d.lifecycleObserved<=0 || (r.schemaVersion==1 && d.lastHeartbeat<0) || (d.evidenceSource!="hap_heartbeat" && d.evidenceSource!="os_snapshot") || (r.schemaVersion>=2 && d.evidenceSource!="os_snapshot"))
            throw std::runtime_error("oracle donor registration invalid");
        if(r.schemaVersion>=2) {
            bool allowed=false; for(unsigned i=1;i<=16U;++i) if(d.bundle=="io.memscene.scene4.workload.a"+std::to_string(i)) allowed=true;
            if(!allowed) throw std::runtime_error("oracle profile target is outside the exact a1..a16 allowlist");
        }
        if(!bundles.insert(d.bundle).second || !pids.insert(d.pid).second ||
            (r.schemaVersion>=2 && (!uids.insert(uintValue(d.uid)).second || !leaves.insert(d.bundle+"_"+d.uid).second)))
            throw std::runtime_error("oracle donor registrations duplicate");
    }
    if(r.schemaVersion>=2 && r.donors.size()>16U) throw std::runtime_error("oracle profile donor limit exceeded");
    return r;
}
OracleRequest getCurrentOracleRequest(const Options& o,const std::shared_ptr<DeviceOracleEvidence>& evidence,const std::string& context="main") {
    if(o.oracleProofSource=="device") {if(!evidence)throw std::runtime_error("device_provider_missing");return evidence->current(context);}
    return parseOracleRequest(readPath(o.oracleSchedule));
}
std::string statusUid(const std::string& status) {
    std::istringstream in(status); std::string line;
    while(std::getline(in,line)) if(line.rfind("Uid:",0)==0) { std::istringstream row(line.substr(4)); std::string uid; if(row>>uid) return uid; }
    throw std::runtime_error("oracle donor UID unavailable");
}
std::optional<OracleRssSample> sampleOracleRss(const OracleDonor& donor,const App& app) {
    try {
        const std::string proc="/proc/"+std::to_string(donor.pid);
        const auto before=readPath(proc+"/stat");
        const auto status=readPath(proc+"/status");
        const auto cgroup=readPath(proc+"/cgroup");
        const auto after=readPath(proc+"/stat");
        return parseOracleRssSample(donor.pid,uintValue(donor.uid),donor.start,
            app.path,before,status,cgroup,after);
    } catch(const std::exception&) { return {}; }
}
struct OracleWritebackFile { FD root,fd; std::uint64_t rootDev=0,rootIno=0; };
bool isProcFileBackend(const std::string& backend) { return backend=="proc_file" || backend=="proc_file_writeback"; }
OracleWritebackFile openOracleWritebackTestFile(const OracleDonor& donor,const App& app,const std::string& caseId) {
    if(donor.writebackFilePath!="memscene_"+caseId+".pages")
        throw std::runtime_error("writeback_file_path_not_exact_case_test_file");
    const std::string proc="/proc/"+std::to_string(donor.pid);
    const auto before=parseProcStat(donor.pid,readPath(proc+"/stat"));
    if(before.startTicks!=donor.start || statusUid(readPath(proc+"/status"))!=donor.uid ||
       !processInAppCgroup(readPath(proc+"/cgroup"),app.path))
        throw std::runtime_error("writeback_target_identity_changed_before_open");
    // Following this one proc magic link selects the target process mount namespace.
    FD namespaceRoot(::open((proc+"/root").c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC));
    if(namespaceRoot.get()<0) throw error("open target process root namespace");
    struct stat rootIdentity{};
    if(::fstat(namespaceRoot.get(),&rootIdentity)!=0) throw error("stat target process root namespace");
    FD dir(::fcntl(namespaceRoot.get(),F_DUPFD_CLOEXEC,3));
    if(dir.get()<0) throw error("dup target process root namespace");
    for(const char* part:{"data","storage","el2","base","haps","entry","files"}) {
        FD next(::openat(dir.get(),part,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW));
        if(next.get()<0) throw error("open target filesDir component");
        dir=std::move(next);
    }
    FD file(::openat(dir.get(),donor.writebackFilePath.c_str(),O_RDWR|O_CLOEXEC|O_NOFOLLOW));
    if(file.get()<0) throw error("open registered writeback test file");
    struct stat fileStat{};
    if(::fstat(file.get(),&fileStat)!=0) throw error("fstat registered writeback test file");
    if(!S_ISREG(fileStat.st_mode) || fileStat.st_nlink!=1 || fileStat.st_size<0 ||
       static_cast<std::uint64_t>(fileStat.st_uid)!=uintValue(donor.uid) ||
       static_cast<std::uint64_t>(fileStat.st_dev)!=donor.writebackFileDev ||
       static_cast<std::uint64_t>(fileStat.st_ino)!=donor.writebackFileIno ||
       static_cast<std::uint64_t>(fileStat.st_size)!=donor.writebackFileSize)
        throw std::runtime_error("registered_writeback_file_identity_mismatch");
    FD currentRoot(::open((proc+"/root").c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC));
    struct stat currentRootIdentity{};
    const auto after=parseProcStat(donor.pid,readPath(proc+"/stat"));
    if(currentRoot.get()<0 || ::fstat(currentRoot.get(),&currentRootIdentity)!=0 ||
       currentRootIdentity.st_dev!=rootIdentity.st_dev || currentRootIdentity.st_ino!=rootIdentity.st_ino ||
       after.startTicks!=donor.start || statusUid(readPath(proc+"/status"))!=donor.uid ||
       !processInAppCgroup(readPath(proc+"/cgroup"),app.path))
        throw std::runtime_error("writeback_process_or_namespace_identity_changed_after_open");
    OracleWritebackFile opened;
    opened.root=std::move(namespaceRoot); opened.fd=std::move(file);
    opened.rootDev=static_cast<std::uint64_t>(rootIdentity.st_dev); opened.rootIno=static_cast<std::uint64_t>(rootIdentity.st_ino);
    return opened;
}
void verifyOracleWritebackFile(const OracleWritebackFile& file,const OracleDonor& donor,const App& app) {
    const std::string proc="/proc/"+std::to_string(donor.pid);
    const auto before=parseProcStat(donor.pid,readPath(proc+"/stat"));
    struct stat registered{},currentRoot{},heldRoot{};
    FD root(::open((proc+"/root").c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC));
    if(root.get()<0 || ::fstat(root.get(),&currentRoot)!=0 || ::fstat(file.root.get(),&heldRoot)!=0 || ::fstat(file.fd.get(),&registered)!=0)
        throw error("verify writeback file/process root identity");
    const auto after=parseProcStat(donor.pid,readPath(proc+"/stat"));
    if(before.startTicks!=donor.start || after.startTicks!=donor.start ||
       currentRoot.st_dev!=heldRoot.st_dev || currentRoot.st_ino!=heldRoot.st_ino ||
       static_cast<std::uint64_t>(currentRoot.st_dev)!=file.rootDev || static_cast<std::uint64_t>(currentRoot.st_ino)!=file.rootIno ||
       statusUid(readPath(proc+"/status"))!=donor.uid || !processInAppCgroup(readPath(proc+"/cgroup"),app.path) ||
       !S_ISREG(registered.st_mode) || registered.st_nlink!=1 || registered.st_size<0 ||
       static_cast<std::uint64_t>(registered.st_uid)!=uintValue(donor.uid) ||
       static_cast<std::uint64_t>(registered.st_dev)!=donor.writebackFileDev ||
       static_cast<std::uint64_t>(registered.st_ino)!=donor.writebackFileIno ||
       static_cast<std::uint64_t>(registered.st_size)!=donor.writebackFileSize)
        throw std::runtime_error("writeback_file_or_process_identity_changed");
}
std::string oracleFreshnessFailureImpl(bool structuralEvidenceValid,double now,
    const std::vector<std::pair<std::string,double>>& observations,double maxAgeSeconds) {
    if(!structuralEvidenceValid) return "identity_or_background_invariant_failed";
    if(!std::isfinite(now) || !std::isfinite(maxAgeSeconds) || maxAgeSeconds<=0)
        return "clock_or_freshness_limit_invalid";
    for(const auto& item:observations) {
        if(!std::isfinite(item.second) || item.second<=0) return item.first+"_telemetry_missing";
        if(item.second>now) return item.first+"_timestamp_in_future";
        if(now-item.second>maxAgeSeconds) return item.first+"_evidence_stale";
    }
    return {};
}
bool oracleFreshnessFailureRetryableImpl(const std::string& reason) {
    return reason.find("_evidence_stale")!=std::string::npos ||
        reason.find("_telemetry_missing")!=std::string::npos;
}
std::string oracleBackgroundFailure(const Options& o,const App& app,const OracleDonor& donor,double now,
    CgroupMemberObservation* memberObservation=nullptr, std::optional<OracleRssSample>* keepSample=nullptr, bool checkFreshness=true) {
    if(now<donor.readyAt) return o.oracleProfile=="peak_transition_os_snapshot"?"clock_before_daily_ready":"clock_before_cold_ready";
    if(now<donor.backgroundSince) return "clock_before_background_enter";
    if(donor.aaPid!=donor.pid) return "aa_pid_mismatch";
    if(donor.aaUid!=uintValue(donor.uid)) return "aa_uid_mismatch";
    if(donor.aaState!="BACKGROUND") return "aa_not_background";
    if(donor.windowVisible) return "window_visible";
    if(donor.windowFocused) return "window_focused";
    if(!donor.procVerified) return "proc_identity_unverified";
    if(donor.procStart!=donor.start) return "proc_start_ticks_mismatch";
    if(!donor.lifecycleClear) return "lifecycle_not_clear";
    if(donor.evidenceSource!="hap_heartbeat" && donor.evidenceSource!="os_snapshot") return "evidence_source_unknown";
    if(o.oracleProfile=="peak_transition_os_snapshot" && donor.evidenceSource!="os_snapshot") return "os_snapshot_required";
    if(o.oracleProfile!="peak_transition_os_snapshot" && donor.lastHeartbeat>now) return "heartbeat_timestamp_in_future";
    if(o.oracleProfile!="peak_transition_os_snapshot" && donor.evidenceSource=="hap_heartbeat" && donor.lastHeartbeat<=0) return "heartbeat_telemetry_missing";
    std::vector<std::pair<std::string,double>> observations={{"aa",donor.aaObserved},{"window",donor.windowObserved},
       {"proc",donor.procObserved},{"lifecycle",donor.lifecycleObserved}};
    if(o.oracleProfile!="peak_transition_os_snapshot" && donor.evidenceSource=="hap_heartbeat") observations.emplace_back("heartbeat",donor.lastHeartbeat);
    const auto freshness=oracleFreshnessFailureImpl(true,now,observations,2.5);
    if(checkFreshness && !freshness.empty()) return freshness;
    const std::string prefix="io.memscene.scene4.workload.a";
    bool bundleAllowed=false;
    for(unsigned i=1;i<=(o.oracleProfile=="peak_transition_os_snapshot"?16U:2U);++i)
        if(donor.bundle==prefix+std::to_string(i)) bundleAllowed=true;
    if(!bundleAllowed) return "donor_bundle_not_allowlisted";
    const auto slash=app.path.find_last_of('/'); const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
    if(leaf!=donor.bundle+"_"+donor.uid) return "cgroup_leaf_mismatch";
    FD root(::open(o.root.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW)); if(root.get()<0) return "cgroup_root_unavailable";
    FD dir(openBeneath(root.get(),app.path)); if(dir.get()<0) return "cgroup_leaf_unavailable";
    std::vector<std::uint64_t> ids;
    if(o.oracleProfile=="peak_transition_os_snapshot") {
        const auto members=observeCgroupMembers(dir.get());
        if(memberObservation) *memberObservation=members;
        if(!members.readOk) return "cgroup_member_list_read_error";
        if(!members.parseValid) return "cgroup_member_list_parse_unknown";
        if(members.entryCount!=1 || members.uniquePids.size()!=1) return "cgroup_member_count_changed";
        ids=members.uniquePids;
    } else ids=processIds(dir.get());
    if(ids.size()!=1) return "cgroup_member_count_changed";
    if(ids.front()!=donor.pid) return "cgroup_pid_mismatch";
    if(o.oracleDiskPrepareAnon) {
        // This profile promises process-resident floors. Vendor memory.current
        // is not interchangeable with VmRSS (e.g. file page ownership differs).
        // Resident floor is checked after all live identity and command-line checks below.
    } else {
        const auto current=maybeAt(dir.get(),"memory.current"); if(!current) return "memory_current_telemetry_missing";
        const Bytes keepFloor=o.oracleRatioSemantics=="cumulative_anon_watermark"?o.oracleCumulativeKeep:512*MiB;
        if(uintValue(trim(*current))<keepFloor) return "min_keep_below_floor";
    }
    for(const auto pid:ids) {
        const std::string proc="/proc/"+std::to_string(pid);
        const auto stat=parseProcStat(pid,readPath(proc+"/stat"));
        const auto cg=readPath(proc+"/cgroup"); if(!processInAppCgroup(cg,app.path)) return "proc_cgroup_membership_changed";
        if(pid==donor.pid) {
            if(stat.startTicks!=donor.start) return "proc_start_ticks_changed";
            if(statusUid(readPath(proc+"/status"))!=donor.uid) return "proc_uid_changed";
            const auto cmdline=readPath(proc+"/cmdline"); if(cmdline.find(donor.bundle)==std::string::npos) return "proc_cmdline_changed";
        }
    }
    if(o.oracleDiskPrepareAnon) {
        const auto resident=sampleOracleRss(donor,app);
        if(keepSample) *keepSample=resident;
        return oraclePipelineKeepFailure(resident,true,o.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep));
    }
    return {};
}
bool sameOracleRegistration(const OracleRequest& a,const OracleRequest& b) {
    if(!a.planFingerprint.empty() || !b.planFingerprint.empty())
        return !a.planFingerprint.empty() && a.planFingerprint==b.planFingerprint && a.id==b.id && a.caseId==b.caseId && a.launch==b.launch && a.needed==b.needed;
    if(a.id!=b.id || a.caseId!=b.caseId || a.launch!=b.launch || a.needed!=b.needed || a.schemaVersion!=b.schemaVersion || a.donors.size()!=b.donors.size()) return false;
    for(std::size_t i=0;i<a.donors.size();++i) { const auto& x=a.donors[i]; const auto& y=b.donors[i];
        if(x.bundle!=y.bundle || x.uid!=y.uid || x.pid!=y.pid || x.start!=y.start || x.coldReady!=y.coldReady || x.dailyReady!=y.dailyReady || x.windowReady!=y.windowReady || x.readyAt!=y.readyAt || x.backgroundSince!=y.backgroundSince || y.lastHeartbeat<x.lastHeartbeat ||
           (a.schemaVersion==3 && (x.writebackFilePath!=y.writebackFilePath || x.writebackFileDev!=y.writebackFileDev || x.writebackFileIno!=y.writebackFileIno || x.writebackFileSize!=y.writebackFileSize))) return false;
    }
    return true;
}
struct OracleActionResult { std::string actionId,eventId,caseId,bundle,status,evidenceSource,feedbackSampleStatus,ratioSemantics="resident_incremental",backend="force_shrink_all"; std::uint64_t pid=0,start=0; std::string uid; double deadline=0,elapsedMs=0,aaObserved=0,windowObserved=0,procObserved=0,lifecycleObserved=0; std::optional<double> syscallStartBoot,syscallReturnBoot,writeDurationMs,feedbackCompleteBoot,writebackStartBoot,writebackReturnBoot; std::optional<bool> writeOverlapsDeadline,feedbackOverlapsDeadline; std::optional<Bytes> readyFreeTargetBytes,observedFreeBytes,freeGapBytes,estimatedDonorAnonRoomBytes; Bytes needed=0,remaining=0,nominal=0,input=0,feedbackDrop=0,preRssAnonKiB=0,postRssAnonKiB=0,preRssFileKiB=0,postRssFileKiB=0,preRssTotalKiB=0,postRssTotalKiB=0,keepReserveBytes=0,preFree=0,postFree=0,preAvailable=0,postAvailable=0,preCached=0,postCached=0,preDirty=0,postDirty=0,writebackRssAnonKiB=0,writebackRssFileKiB=0,writebackFree=0,writebackAvailable=0,writebackCached=0,writebackDirty=0,totalAnonProxyBytes=0,currentVmSwapBytes=0,estimatedIncrementBytes=0,estimatedRetainedBytes=0; std::int64_t signedRssDeltaKiB=0,signedRssFileDeltaKiB=0; unsigned ratio=0,plannedRatio=0,schemaVersion=1; long writeLength=-1,writebackReturn=-2; int errorNo=0,writebackErrno=0; bool isolateFloorDonor=false; std::string floorIsolationEvidence; bool preparationWorkMode=false,writeAttempted=false,writebackAttempted=false,feedbackIdentityVerified=false,fileFeedbackPresent=false,overlap=false; };
void appendOracleWritebackStage(const Options& options,const OracleActionResult& result,const char* stage,
    const std::optional<double>& start,const std::optional<double>& returned,long returnValue,int errorNumber,
    const struct stat* fileStat,const OracleRssSample* rss,const System* system,const std::string& status) {
    const fs::path path=options.oracleLedger+".proc_file_writeback_v1.csv";
    static const std::string header="action_id,event_id,case_id,bundle,pid,uid,start_ticks,stage,file_path,file_dev,file_inode,file_size,file_uid,syscall_start_boottime_s,syscall_return_boottime_s,return_value,errno,rss_anon_kib,rss_file_kib,dirty_bytes,cached_bytes,memfree_bytes,memavailable_bytes,status";
    const bool fresh=!fs::exists(path) || fs::file_size(path)==0;
    if(!fresh) { std::ifstream prior(path); std::string existing; if(!std::getline(prior,existing) || existing!=header) throw std::runtime_error("proc_file_writeback ledger schema mismatch"); }
    std::ofstream out(path,std::ios::app); if(!out) throw std::runtime_error("cannot append proc_file_writeback ledger");
    if(fresh) out<<header<<'\n';
    auto number=[](const std::optional<double>& value) { return value?std::to_string(*value):std::string{}; };
    out<<csvCell(result.actionId)<<','<<csvCell(result.eventId)<<','<<csvCell(result.caseId)<<','<<csvCell(result.bundle)<<','<<result.pid<<','<<csvCell(result.uid)<<','<<result.start<<','<<stage<<','<<csvCell("memscene_"+result.caseId+".pages")<<','
       <<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_dev)):std::string{})<<','<<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_ino)):std::string{})<<','<<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_size)):std::string{})<<','<<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_uid)):std::string{})<<','
       <<number(start)<<','<<number(returned)<<','<<returnValue<<','<<errorNumber<<','<<(rss?std::to_string(rss->rssAnonKiB):std::string{})<<','<<(rss&&rss->rssFileKiB?std::to_string(*rss->rssFileKiB):std::string{})<<','
       <<(system?std::to_string(system->dirty):std::string{})<<','<<(system?std::to_string(system->cached):std::string{})<<','<<(system?std::to_string(system->free):std::string{})<<','<<(system?std::to_string(system->available):std::string{})<<','<<csvCell(status)<<'\n';
    out.flush(); if(!out) throw std::runtime_error("proc_file_writeback ledger write failed");
}
void appendOraclePostwriteRecheckStage(const Options& options,const OracleActionResult& result,
    double started,double ended,const std::string& initialReason,const std::string& finalReason,
    bool recovered,const struct stat* fileStat,const OracleRssSample* rss,const System* system) {
    const fs::path path=options.oracleLedger+".postwrite_recheck_v1.csv";
    static const std::string header="action_id,event_id,case_id,bundle,pid,uid,start_ticks,stage_start_boottime_s,stage_end_boottime_s,initial_stale_reason,final_reason,recovered,file_dev,file_inode,file_size,file_uid,rss_anon_kib,rss_file_kib,rss_total_kib,dirty_bytes,cached_bytes,memfree_bytes,memavailable_bytes";
    const bool fresh=!fs::exists(path) || fs::file_size(path)==0;
    if(!fresh) { std::ifstream prior(path); std::string existing; if(!std::getline(prior,existing) || existing!=header) throw std::runtime_error("postwrite recheck ledger schema mismatch"); }
    std::ofstream out(path,std::ios::app); if(!out) throw std::runtime_error("cannot append postwrite recheck ledger");
    if(fresh) out<<header<<'\n';
    out<<csvCell(result.actionId)<<','<<csvCell(result.eventId)<<','<<csvCell(result.caseId)<<','<<csvCell(result.bundle)<<','<<result.pid<<','<<csvCell(result.uid)<<','<<result.start<<','
       <<std::setprecision(17)<<started<<','<<ended<<','<<csvCell(initialReason)<<','<<csvCell(finalReason)<<','<<(recovered?"true":"false")<<','
       <<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_dev)):std::string{})<<','
       <<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_ino)):std::string{})<<','
       <<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_size)):std::string{})<<','
       <<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_uid)):std::string{})<<','
       <<(rss?std::to_string(rss->rssAnonKiB):std::string{})<<','<<(rss&&rss->rssFileKiB?std::to_string(*rss->rssFileKiB):std::string{})<<','
       <<(rss&&rss->rssKiB?std::to_string(*rss->rssKiB):std::string{})<<','
       <<(system?std::to_string(system->dirty):std::string{})<<','<<(system?std::to_string(system->cached):std::string{})<<','
       <<(system?std::to_string(system->free):std::string{})<<','<<(system?std::to_string(system->available):std::string{})<<'\n';
    out.flush(); if(!out) throw std::runtime_error("postwrite recheck ledger write failed");
}
void appendOraclePostFsyncRecheckStage(const Options& options,const OracleActionResult& result,
    double started,double ended,const std::string& initialReason,const std::string& finalReason,
    bool recovered,const struct stat* fileStat,const OracleRssSample* rss,const System* system) {
    const fs::path path=options.oracleLedger+".post_fsync_recheck_v1.csv";
    static const std::string header="action_id,event_id,case_id,bundle,pid,uid,start_ticks,stage_start_boottime_s,stage_end_boottime_s,wait_duration_ms,initial_reason,final_reason,recovered,file_dev,file_inode,file_size,file_uid,rss_anon_kib,rss_file_kib,rss_total_kib,dirty_bytes,cached_bytes,memfree_bytes,memavailable_bytes";
    const bool fresh=!fs::exists(path) || fs::file_size(path)==0;
    if(!fresh) { std::ifstream prior(path); std::string existing; if(!std::getline(prior,existing) || existing!=header) throw std::runtime_error("post-fsync recheck ledger schema mismatch"); }
    std::ofstream out(path,std::ios::app); if(!out) throw std::runtime_error("cannot append post-fsync recheck ledger");
    if(fresh) out<<header<<'\n';
    out<<csvCell(result.actionId)<<','<<csvCell(result.eventId)<<','<<csvCell(result.caseId)<<','<<csvCell(result.bundle)<<','<<result.pid<<','<<csvCell(result.uid)<<','<<result.start<<','
       <<std::setprecision(17)<<started<<','<<ended<<','<<((ended-started)*1000.0)<<','<<csvCell(initialReason)<<','<<csvCell(finalReason)<<','<<(recovered?"true":"false")<<','
       <<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_dev)):std::string{})<<','
       <<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_ino)):std::string{})<<','
       <<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_size)):std::string{})<<','
       <<(fileStat?std::to_string(static_cast<std::uint64_t>(fileStat->st_uid)):std::string{})<<','
       <<(rss?std::to_string(rss->rssAnonKiB):std::string{})<<','<<(rss&&rss->rssFileKiB?std::to_string(*rss->rssFileKiB):std::string{})<<','
       <<(rss&&rss->rssKiB?std::to_string(*rss->rssKiB):std::string{})<<','
       <<(system?std::to_string(system->dirty):std::string{})<<','<<(system?std::to_string(system->cached):std::string{})<<','
       <<(system?std::to_string(system->free):std::string{})<<','<<(system?std::to_string(system->available):std::string{})<<'\n';
    out.flush(); if(!out) throw std::runtime_error("post-fsync recheck ledger write failed");
}
struct OracleSwapTelemetryRow {
    std::string groupScope,groupPath,node;
    OracleSwapTelemetryRead read;
};
std::vector<OracleSwapTelemetryRow> sampleOracleSwapGroups(int root,const std::string& leafPath,const std::string& proc,bool includeGlobal) {
    std::vector<OracleSwapTelemetryRow> rows;
    const auto slash=leafPath.rfind('/');
    const std::string parentPath=slash==std::string::npos?std::string{}:leafPath.substr(0,slash);
    const std::string parentScope=slash==std::string::npos?"root_scope":"uid_parent_scope";
    for(const auto& group:std::vector<std::pair<std::string,std::string>>{{"leaf_scope",leafPath},{parentScope,parentPath}}) {
        FD groupFd;
        int groupError=0;
        try {
            if(group.second.empty()) groupFd=FD(::fcntl(root,F_DUPFD_CLOEXEC,3));
            else groupFd=openBeneath(root,group.second);
            if(groupFd.get()<0) groupError=errno;
        } catch(const std::exception&) { groupError=errno; }
        for(const auto& node:std::vector<std::string>{"memory.eswap_info","memory.zswap_info","memory.stat"}) {
            OracleSwapTelemetryRow row; row.groupScope=group.first; row.groupPath=group.second; row.node=node;
            if(groupError) {
                row.read.status="group_open_error"; row.read.errorNumber=groupError;
                row.read.startedBoottime=monotonic(); row.read.endedBoottime=row.read.startedBoottime;
            } else row.read=readOracleSwapTelemetryNode(groupFd.get(),node,4096);
            rows.push_back(std::move(row));
        }
    }
    // Vendor file-swap counter is system-wide: retain raw data, never attribute
    // its delta to this donor or count it as physically freed memory.
    OracleSwapTelemetryRow diskRow;
    diskRow.groupScope="system_scope";
    diskRow.groupPath="/sys/block/zram0";
    diskRow.node="hyperhold_io_info_file";
    FD diskDir(::open(diskRow.groupPath.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC));
    if(diskDir.get()<0) {
        diskRow.read.errorNumber=errno;
        diskRow.read.status="group_open_error";
        diskRow.read.startedBoottime=monotonic();
        diskRow.read.endedBoottime=diskRow.read.startedBoottime;
    } else diskRow.read=readOracleSwapTelemetryNode(diskDir.get(),diskRow.node,4096);
    rows.push_back(std::move(diskRow));
    if(includeGlobal) {
        FD procDir(::open(proc.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC));
        for(const auto& node:std::vector<std::string>{"vmstat","meminfo"}) {
            OracleSwapTelemetryRow row; row.groupScope="system_scope"; row.groupPath=proc; row.node=node;
            if(procDir.get()<0) { row.read.status="group_open_error"; row.read.errorNumber=errno; row.read.startedBoottime=monotonic(); row.read.endedBoottime=row.read.startedBoottime; }
            else row.read=readOracleSwapTelemetryNode(procDir.get(),node,16384);
            rows.push_back(std::move(row));
        }
    }
    return rows;
}
void appendOracleSwapTelemetry(const Options& options,const OracleActionResult& result,
    const std::vector<OracleSwapTelemetryRow>& before,const std::vector<OracleSwapTelemetryRow>& after) noexcept {
    try {
        const fs::path path=options.oracleLedger+".swap_telemetry_v1.csv";
        static const std::string header="action_id,event_id,case_id,bundle,pid,uid,start_ticks,write_attempted,write_return,write_errno,syscall_start_boottime_s,syscall_return_boottime_s,stage,read_start_boottime_s,read_end_boottime_s,read_duration_ms,group_scope,group_path,node,read_status,read_errno,raw";
        const bool fresh=!fs::exists(path) || fs::file_size(path)==0;
        if(!fresh) { std::ifstream prior(path); std::string existing; if(!std::getline(prior,existing) || existing!=header) throw std::runtime_error("swap telemetry ledger schema mismatch"); }
        std::ofstream out(path,std::ios::app); if(!out) throw std::runtime_error("cannot append swap telemetry ledger");
        if(fresh) out<<header<<'\n';
        auto writeRows=[&](const std::vector<OracleSwapTelemetryRow>& rows,const char* stage) {
            for(const auto& row:rows) {
                const auto duration=(row.read.endedBoottime-row.read.startedBoottime)*1000.0;
                const auto fields=std::vector<std::string>{result.actionId,result.eventId,result.caseId,result.bundle,
                    std::to_string(result.pid),result.uid,std::to_string(result.start),result.writeAttempted?"true":"false",
                    std::to_string(result.writeLength),std::to_string(result.errorNo),
                    result.syscallStartBoot?std::to_string(*result.syscallStartBoot):std::string{},
                    result.syscallReturnBoot?std::to_string(*result.syscallReturnBoot):std::string{},stage,
                    std::to_string(row.read.startedBoottime),std::to_string(row.read.endedBoottime),std::to_string(duration),
                    row.groupScope,row.groupPath,row.node,row.read.status,std::to_string(row.read.errorNumber),row.read.raw};
                out<<oracleSwapTelemetryCsvRow(fields)<<'\n';
            }
        };
        writeRows(before,"before_write"); writeRows(after,"after_write");
        out.flush(); if(!out) throw std::runtime_error("swap telemetry ledger write failed");
    } catch(const std::exception& e) {
        std::cerr<<"WARN oracle_swap_telemetry_sidecar action_id="<<result.actionId<<" error="<<e.what()<<'\n';
    }
}
OracleActionResult performOracleAction(const Options& o,const App& app,const OracleDonor& donor,const OracleRequest& req,unsigned ratio,unsigned priorSuccessfulRatio,Bytes nominal,Bytes remaining,Bytes input,const Forecast& forecast,const std::string& actionId,Bytes plannedIncrement,const std::shared_ptr<DeviceOracleEvidence>& evidence) {
    OracleActionResult r; r.actionId=actionId; r.eventId=req.id; r.caseId=req.caseId; r.bundle=donor.bundle; r.pid=donor.pid; r.uid=donor.uid; r.start=donor.start;
    r.deadline=req.launch; r.needed=req.needed; r.remaining=remaining; r.nominal=nominal; r.ratio=ratio; r.plannedRatio=ratio; r.schemaVersion=req.schemaVersion; r.ratioSemantics=o.oracleRatioSemantics; r.input=input; r.backend=o.oracleBackend;
    for(const auto& registered:req.donors) if(registered.bundle==donor.bundle) { r.evidenceSource=registered.evidenceSource; r.aaObserved=registered.aaObserved; r.windowObserved=registered.windowObserved; r.procObserved=registered.procObserved; r.lifecycleObserved=registered.lifecycleObserved; }
    r.preparationWorkMode=oraclePreparationWorkMode(o.oracleProofSource=="device",o.oracleDiskPrepareAnon,o.oracleBackend);
    const double begin=monotonic();
    auto finishNoProc=[&](const std::string& status) { r.status=status; r.elapsedMs=(monotonic()-begin)*1000; r.overlap=monotonic()>req.launch; return r; };
    bool writeAttempted=false, writeComplete=false;
    std::optional<OracleDiskPool> diskBefore;
    Bytes diskWorkBefore=0;
    Control controlAtProcWrite; bool haveControlAtProcWrite=false;
    try {
        if(begin>=req.launch) throw std::runtime_error("event_deadline_passed_before_action_start");
        auto gate=parseControl(readPath(o.control)); if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle)) throw std::runtime_error("runtime_control_paused_or_protected");
        const auto latest=getCurrentOracleRequest(o,evidence,actionId);
        if(!sameOracleRegistration(req,latest)) throw std::runtime_error("latest_schedule_registration_changed");
        const OracleDonor* live=nullptr; for(const auto& item:latest.donors) if(item.bundle==donor.bundle) live=&item;
        if(!live) throw std::runtime_error("oracle_donor_missing_before_write");
        const auto liveFailure=oracleBackgroundFailure(o,app,*live,begin);
        if(!liveFailure.empty()) throw std::runtime_error((oracleFreshnessFailureRetryableImpl(liveFailure)?"oracle_evidence_defer:":"oracle_evidence_reject:")+liveFailure);
        FD root(::open(o.root.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW)); if(root.get()<0) throw error("open oracle cgroup root");
        FD dir=openBeneath(root.get(),app.path);
        FD writer;
        OracleWritebackFile writebackFile;
        if(o.oracleBackend=="proc_file_writeback") {
            if(req.schemaVersion!=3 || o.oracleProfile!="peak_transition_os_snapshot")
                throw std::runtime_error("proc_file_writeback_requires_profile_schema3");
            writebackFile=openOracleWritebackTestFile(donor,app,req.caseId);
        }
        if(isProcFileBackend(o.oracleBackend)) {
            const std::string proc="/proc/"+std::to_string(donor.pid)+"/reclaim";
            writer=FD(::open(proc.c_str(),O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
        } else writer=FD(::openat(dir.get(),o.oracleBackend=="force_swapout"?"memory.force_swapout":"memory.force_shrink_all",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
        if(writer.get()<0) throw error(isProcFileBackend(o.oracleBackend)?"open proc reclaim":"open memory.force_shrink_all");
        const double writeNow=monotonic(); if(writeNow>=req.launch) throw std::runtime_error("event_deadline_passed_before_write");
        gate=parseControl(readPath(o.control)); if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle)) throw std::runtime_error("runtime_control_changed_before_write");
        const auto immediatelyLatest=getCurrentOracleRequest(o,evidence,actionId);
        if(!sameOracleRegistration(latest,immediatelyLatest)) throw std::runtime_error("latest_schedule_changed_immediately_before_write");
        const OracleDonor* immediatelyLive=nullptr; for(const auto& item:immediatelyLatest.donors) if(item.bundle==donor.bundle) immediatelyLive=&item;
        if(!immediatelyLive) throw std::runtime_error("oracle_donor_missing_immediately_before_write");
        const auto immediateFailure=oracleBackgroundFailure(o,app,*immediatelyLive,monotonic());
        if(!immediateFailure.empty()) throw std::runtime_error((oracleFreshnessFailureRetryableImpl(immediateFailure)?"oracle_evidence_defer:":"oracle_evidence_reject:")+immediateFailure);
        r.evidenceSource=immediatelyLive->evidenceSource; r.aaObserved=immediatelyLive->aaObserved; r.windowObserved=immediatelyLive->windowObserved; r.procObserved=immediatelyLive->procObserved; r.lifecycleObserved=immediatelyLive->lifecycleObserved;
        auto preSample=sampleOracleRss(*immediatelyLive,app);
        if(!preSample) throw std::runtime_error("prewrite_rss_identity_sample_unavailable");
        r.preRssAnonKiB=preSample->rssAnonKiB;
        r.preRssTotalKiB=preSample->rssKiB.value_or(0);
        r.preRssFileKiB=preSample->rssFileKiB.value_or(0);
        if(o.oracleBackend=="proc_file_writeback") {
            const auto wbSysBefore=systemSample(o.proc);
            const auto wbFileBefore=sampleOracleRss(*immediatelyLive,app);
            struct stat wbStatBefore{};
            if(!wbFileBefore || !wbFileBefore->rssFileKiB || ::fstat(writebackFile.fd.get(),&wbStatBefore)!=0)
                throw std::runtime_error("writeback_preflight_sample_or_fstat_unknown");
            r.writebackRssAnonKiB=wbFileBefore->rssAnonKiB; r.writebackRssFileKiB=*wbFileBefore->rssFileKiB;
            r.writebackFree=wbSysBefore.free; r.writebackAvailable=wbSysBefore.available; r.writebackCached=wbSysBefore.cached; r.writebackDirty=wbSysBefore.dirty;
            appendOracleWritebackStage(o,r,"fdatasync_submit_preflight",{}, {},-2,0,&wbStatBefore,&*wbFileBefore,&wbSysBefore,"not_submitted_stage_ledger_flushed_before_gate");
            const double submitGate=monotonic();
            if(submitGate>=req.launch) throw std::runtime_error("event_deadline_passed_after_writeback_stage_ledger_before_fdatasync");
            gate=parseControl(readPath(o.control));
            if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle))
                throw std::runtime_error("runtime_control_changed_before_fdatasync");
            const auto submitRequest=getCurrentOracleRequest(o,evidence,actionId);
            if(!sameOracleRegistration(req,submitRequest)) throw std::runtime_error("schedule_registration_changed_before_fdatasync");
            const OracleDonor* submitDonor=nullptr;
            for(const auto& item:submitRequest.donors) if(item.bundle==donor.bundle) submitDonor=&item;
            const double submitFreshness=monotonic();
            if(!submitDonor) throw std::runtime_error("donor_missing_before_fdatasync");
            const auto submitFailure=oracleBackgroundFailure(o,app,*submitDonor,submitFreshness);
            if(!submitFailure.empty()) throw std::runtime_error((oracleFreshnessFailureRetryableImpl(submitFailure)?"oracle_evidence_defer:":"oracle_evidence_reject:")+submitFailure);
            verifyOracleWritebackFile(writebackFile,donor,app);
            const auto submitRss=sampleOracleRss(*submitDonor,app);
            const auto submitSystem=systemSample(o.proc);
            if(!submitRss || !submitRss->rssFileKiB || !submitRss->rssKiB ||
               !oracleProcFileWritebackEstimateFits(*submitRss->rssFileKiB,donor.writebackFileSize,nominal) ||
               !oracleProcFileWritebackSystemGapFits(oracleEventGap(o,submitSystem,forecast,req.needed),nominal))
                throw std::runtime_error("writeback_pre_submit_rss_or_shared_system_reservation_no_longer_fits");
            const double writebackStart=monotonic();
            if(writebackStart>=req.launch) throw std::runtime_error("event_deadline_passed_at_fdatasync_start");
            r.writebackStartBoot=writebackStart; r.writebackAttempted=true;
            errno=0; const int syncResult=::fdatasync(writebackFile.fd.get()); const int syncErrno=syncResult<0?errno:0;
            const double syncReturn=monotonic(); r.writebackReturnBoot=syncReturn; r.writebackReturn=syncResult; r.writebackErrno=syncErrno;
            auto wbSysAfter=systemSample(o.proc);
            auto wbFileAfter=sampleOracleRss(*immediatelyLive,app);
            struct stat wbStatAfter{};
            const bool wbStatOk=::fstat(writebackFile.fd.get(),&wbStatAfter)==0;
            appendOracleWritebackStage(o,r,"fdatasync_returned",r.writebackStartBoot,r.writebackReturnBoot,syncResult,syncErrno,wbStatOk?&wbStatAfter:nullptr,wbFileAfter?&*wbFileAfter:nullptr,&wbSysAfter,
                syncResult==0?"fdatasync_returned_success":"fdatasync_returned_error_no_proc");
            if(syncResult!=0) { r.errorNo=syncErrno; return finishNoProc("writeback_failed_no_proc_no_fallback"); }
            if(syncReturn>=req.launch) return finishNoProc("writeback_completed_after_deadline_no_proc");
            verifyOracleWritebackFile(writebackFile,donor,app);
            const auto currentControl=parseControl(readPath(o.control));
            if(currentControl.paused || currentControl.protectedApps.count(app.name) || currentControl.protectedApps.count(donor.bundle)) {
                return finishNoProc("writeback_completed_control_changed_no_proc");
            }
            auto afterSyncRequest=getCurrentOracleRequest(o,evidence,actionId);
            if(!sameOracleRegistration(req,afterSyncRequest)) return finishNoProc("writeback_completed_schedule_changed_no_proc");
            const OracleDonor* afterSyncDonor=nullptr;
            for(const auto& item:afterSyncRequest.donors) if(item.bundle==donor.bundle) afterSyncDonor=&item;
            double afterSyncNow=monotonic();
            if(!afterSyncDonor) return finishNoProc("writeback_completed_donor_missing_no_proc");
            std::string afterSyncFailure=oracleBackgroundFailure(o,app,*afterSyncDonor,afterSyncNow);
            if(!afterSyncFailure.empty()) {
                if(!oracleWritebackPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,afterSyncFailure))
                    return finishNoProc("writeback_completed_identity_or_background_changed_no_proc:"+afterSyncFailure);
                const double recheckStarted=afterSyncNow;
                std::string finalReason=afterSyncFailure;
                bool recovered=false,registrationStable=true,controlStable=true,fileIdentityVerified=false;
                bool rssKeepFits=false,systemReservationFits=false;
                std::optional<OracleRssSample> refreshedRss;
                std::optional<System> refreshedSystem;
                std::optional<struct stat> refreshedFileStat;
                const Bytes keepFloor=oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep);
                while(!recovered) {
                    const double checkTime=monotonic();
                    const double remainingSeconds=oracleWritebackPostwriteRecheckRemaining(recheckStarted,checkTime,req.launch);
                    if(remainingSeconds<=0) {
                        finalReason=checkTime>=req.launch?"event_deadline_passed_during_post_fsync_recheck":"post_fsync_freshness_recheck_timeout";
                        break;
                    }
                    try {
                        const auto recheckControl=parseControl(readPath(o.control));
                        if(recheckControl.paused || recheckControl.protectedApps.count(app.name) || recheckControl.protectedApps.count(donor.bundle)) {
                            controlStable=false; finalReason="runtime_control_paused_or_protected_during_post_fsync_recheck"; break;
                        }
                        if(recheckControl.paused!=currentControl.paused || recheckControl.protectedApps!=currentControl.protectedApps ||
                           recheckControl.boost!=currentControl.boost || recheckControl.until!=currentControl.until) {
                            controlStable=false; finalReason="runtime_control_changed_during_post_fsync_recheck"; break;
                        }
                        auto recheckRequest=getCurrentOracleRequest(o,evidence,actionId);
                        if(!sameOracleRegistration(req,recheckRequest)) {
                            registrationStable=false; finalReason="schedule_registration_changed_during_post_fsync_recheck"; break;
                        }
                        const OracleDonor* recheckDonor=nullptr;
                        for(const auto& item:recheckRequest.donors) if(item.bundle==donor.bundle) recheckDonor=&item;
                        if(!recheckDonor) { finalReason="donor_missing_during_post_fsync_recheck"; break; }
                        const double evidenceTime=monotonic();
                        const auto evidenceFailure=oracleBackgroundFailure(o,app,*recheckDonor,evidenceTime);
                        if(!evidenceFailure.empty()) {
                            finalReason=evidenceFailure;
                            if(!oracleWritebackPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,evidenceFailure)) break;
                        } else {
                            verifyOracleWritebackFile(writebackFile,donor,app);
                            struct stat currentFile{};
                            if(::fstat(writebackFile.fd.get(),&currentFile)!=0) throw error("fstat writeback file during post-fsync recheck");
                            refreshedFileStat=currentFile; fileIdentityVerified=true;
                            refreshedRss=sampleOracleRss(*recheckDonor,app);
                            refreshedSystem=systemSample(o.proc);
                            if(!refreshedRss || !refreshedRss->rssFileKiB || !refreshedRss->rssKiB ||
                               *refreshedRss->rssFileKiB>std::numeric_limits<Bytes>::max()/1024 ||
                               *refreshedRss->rssKiB>std::numeric_limits<Bytes>::max()/1024) {
                                finalReason="post_fsync_recheck_rss_unknown"; break;
                            }
                            const Bytes fileKiB=*refreshedRss->rssFileKiB;
                            const Bytes residentKiB=*refreshedRss->rssKiB;
                            rssKeepFits=oracleProcFileWritebackEstimateFits(fileKiB,donor.writebackFileSize,nominal) &&
                                residentKiB>=fileKiB && residentKiB-fileKiB>=keepFloor/1024;
                            const Bytes currentGap=oracleEventGap(o,*refreshedSystem,forecast,req.needed);
                            systemReservationFits=oracleProcFileWritebackSystemGapFits(currentGap,nominal);
                            if(!rssKeepFits) { finalReason="post_fsync_recheck_file_estimate_or_keep_reserve_failed"; break; }
                            if(!systemReservationFits) { finalReason="post_fsync_recheck_shared_system_reservation_failed"; break; }
                            const auto finalControl=parseControl(readPath(o.control));
                            if(finalControl.paused || finalControl.protectedApps.count(app.name) || finalControl.protectedApps.count(donor.bundle) ||
                               finalControl.paused!=currentControl.paused || finalControl.protectedApps!=currentControl.protectedApps ||
                               finalControl.boost!=currentControl.boost || finalControl.until!=currentControl.until) {
                                controlStable=false; finalReason="runtime_control_changed_after_post_fsync_sample"; break;
                            }
                            auto finalRequest=getCurrentOracleRequest(o,evidence,actionId);
                            if(!sameOracleRegistration(req,finalRequest)) {
                                registrationStable=false; finalReason="schedule_registration_changed_after_post_fsync_sample"; break;
                            }
                            const OracleDonor* finalDonor=nullptr;
                            for(const auto& item:finalRequest.donors) if(item.bundle==donor.bundle) finalDonor=&item;
                            if(!finalDonor) { finalReason="donor_missing_after_post_fsync_sample"; break; }
                            const double finalGateTime=monotonic();
                            const auto finalFailure=oracleBackgroundFailure(o,app,*finalDonor,finalGateTime);
                            if(!oracleWritebackPreProcRecheckMayResume(req.schemaVersion,o.oracleProfile,o.oracleBackend,
                                   afterSyncFailure,finalFailure,registrationStable,controlStable,fileIdentityVerified,
                                   rssKeepFits,systemReservationFits,finalGateTime<req.launch)) {
                                finalReason=finalFailure.empty()?"post_fsync_recheck_gate_incomplete":finalFailure;
                                if(finalGateTime>=req.launch) finalReason="event_deadline_passed_after_post_fsync_sample";
                                if(!finalFailure.empty() && !oracleWritebackPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,finalFailure)) break;
                            } else {
                                afterSyncRequest=std::move(finalRequest);
                                afterSyncDonor=nullptr;
                                for(const auto& item:afterSyncRequest.donors) if(item.bundle==donor.bundle) afterSyncDonor=&item;
                                if(!afterSyncDonor) { finalReason="donor_missing_after_post_fsync_sample"; break; }
                                wbFileAfter=refreshedRss; wbSysAfter=*refreshedSystem; afterSyncNow=finalGateTime;
                                finalReason="fresh_same_registration_control_identity_file_and_budget_verified";
                                recovered=true; break;
                            }
                        }
                    } catch(const std::exception& e) {
                        finalReason=std::string("post_fsync_recheck_exception:")+e.what(); break;
                    }
                    const double afterCheck=monotonic();
                    const double sleepSeconds=std::min(0.2,oracleWritebackPostwriteRecheckRemaining(recheckStarted,afterCheck,req.launch));
                    if(sleepSeconds<=0) {
                        finalReason=afterCheck>=req.launch?"event_deadline_passed_during_post_fsync_recheck":"post_fsync_freshness_recheck_timeout";
                        break;
                    }
                    std::this_thread::sleep_for(std::chrono::duration<double>(sleepSeconds));
                }
                const double recheckEnded=monotonic();
                if(oracleWritebackPostwriteRecheckRemaining(recheckStarted,recheckEnded,req.launch)<=0 && recovered) {
                    recovered=false;
                    finalReason=recheckEnded>=req.launch?"event_deadline_passed_before_post_fsync_recheck_completion":"post_fsync_freshness_recheck_timeout";
                }
                struct stat finalFileStat{};
                if(!refreshedFileStat && ::fstat(writebackFile.fd.get(),&finalFileStat)==0) refreshedFileStat=finalFileStat;
                appendOraclePostFsyncRecheckStage(o,r,recheckStarted,recheckEnded,afterSyncFailure,finalReason,recovered,
                    refreshedFileStat?&*refreshedFileStat:nullptr,refreshedRss?&*refreshedRss:nullptr,
                    refreshedSystem?&*refreshedSystem:nullptr);
                if(!recovered) return finishNoProc("writeback_completed_post_fsync_recheck_halt:"+finalReason);
            }
            if(afterSyncNow>=req.launch) return finishNoProc("writeback_completed_after_deadline_no_proc");
            if(!wbFileAfter || !wbFileAfter->rssFileKiB || !wbFileAfter->rssKiB ||
               !oracleProcFileWritebackEstimateFits(*wbFileAfter->rssFileKiB,donor.writebackFileSize,nominal)) {
                return finishNoProc("writeback_completed_file_rss_or_reservation_unknown_no_proc");
            }
            const auto gapAfterWriteback=oracleEventGap(o,wbSysAfter,forecast,req.needed);
            const bool budgetStillFits=oracleProcFileWritebackSystemGapFits(gapAfterWriteback,nominal);
            if(!oracleWritebackMayProceed(syncResult==0,afterSyncNow<req.launch,true,budgetStillFits))
                return finishNoProc("writeback_completed_current_system_gap_below_reserved_estimate_no_proc");
            preSample=std::move(wbFileAfter);
        }
        if(isProcFileBackend(o.oracleBackend)) {
            if(o.oracleProfile!="peak_transition_os_snapshot" || !preSample->rssFileKiB || !preSample->rssKiB)
                throw std::runtime_error("proc_file requires profile and known process VmRSS/RssFile estimate");
            if(*preSample->rssFileKiB>std::numeric_limits<Bytes>::max()/1024 ||
                *preSample->rssKiB>std::numeric_limits<Bytes>::max()/1024)
                throw std::runtime_error("prewrite whole-file estimate byte conversion overflow");
            const Bytes fileBytes=*preSample->rssFileKiB*1024;
            const Bytes residentBytes=*preSample->rssKiB*1024;
            const Bytes keepFloor=oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep);
            if(fileBytes>nominal)
                throw std::runtime_error("prewrite RssFile exceeds conservatively reserved whole-category estimate");
            if(residentBytes<fileBytes || residentBytes-fileBytes<keepFloor)
                throw std::runtime_error("prewrite whole-file estimate violates resident keep reserve");
            r.preRssFileKiB=*preSample->rssFileKiB;
            r.preRssTotalKiB=*preSample->rssKiB;
            r.estimatedRetainedBytes=residentBytes-fileBytes;
            r.keepReserveBytes=keepFloor;
            const auto sysBefore=systemSample(o.proc); r.preFree=sysBefore.free; r.preAvailable=sysBefore.available;
        }
        r.feedbackSampleStatus="prewrite_identity_verified";
        // Proc sampling is separate from the OS/window/lifecycle evidence
        // refresh. Re-read control and the full current schedule after it, so
        // a slow sample cannot carry stale background authorization to write.
        const double finalGateTime=monotonic();
        if(finalGateTime>=req.launch) throw std::runtime_error("event_deadline_passed_after_prewrite_rss_sample");
        gate=parseControl(readPath(o.control));
        if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle))
            throw std::runtime_error("runtime_control_changed_after_prewrite_rss_sample");
        const auto finalLatest=getCurrentOracleRequest(o,evidence,actionId);
        if(!sameOracleRegistration(req,finalLatest))
            throw std::runtime_error("latest_schedule_registration_changed_after_prewrite_rss_sample");
        const OracleDonor* finalLive=nullptr;
        for(const auto& item:finalLatest.donors) if(item.bundle==donor.bundle) finalLive=&item;
        const double finalFreshnessTime=monotonic();
        if(!finalLive) throw std::runtime_error("oracle_donor_missing_after_prewrite_rss_sample");
        const auto finalFailure=oracleBackgroundFailure(o,app,*finalLive,finalFreshnessTime);
        if(!finalFailure.empty()) throw std::runtime_error((oracleFreshnessFailureRetryableImpl(finalFailure)?"oracle_evidence_defer:":"oracle_evidence_reject:")+finalFailure);
        unsigned writeRatio=ratio;
        if(o.oracleRatioSemantics=="cumulative_anon_watermark" && o.oracleBackend=="force_shrink_all") {
            if(!preSample->vmSwapKiB) throw std::runtime_error("prewrite_vmswap_telemetry_missing");
            const Bytes liveResident=preSample->rssAnonKiB*1024;
            const auto pipelineKeep=o.oracleDiskPrepareAnon
                ?oraclePipelineAnonKeep(*preSample,o.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep))
                :std::optional<Bytes>{o.oracleCumulativeKeep};
            if(!pipelineKeep) throw std::runtime_error("pipeline_prewrite_total_rss_keep_unknown");
            const Bytes estimateLimit=r.preparationWorkMode?plannedIncrement:nominal;
            const Bytes prewriteCap=std::min(estimateLimit,sub(liveResident,*pipelineKeep+64*MiB));
            const auto prePlan=oracleCumulativeWatermarkForCap(liveResident,*preSample->vmSwapKiB*1024,prewriteCap,ratio);
            r.ratio=prePlan.ratio;
            r.ratioSemantics=o.oracleRatioSemantics;
            r.totalAnonProxyBytes=prePlan.totalAnonProxyBytes;
            r.currentVmSwapBytes=prePlan.currentVmSwapBytes;
            r.estimatedIncrementBytes=prePlan.estimatedIncrementBytes;
            r.estimatedRetainedBytes=prePlan.estimatedRetainedBytes;
            if(prePlan.ratio==0 || prePlan.estimatedIncrementBytes<o.minBatch ||
               !oracleRatioCanAdvance(priorSuccessfulRatio,prePlan.ratio)) {
                r.status="cumulative_watermark_already_met_or_below_min_batch";
                r.feedbackSampleStatus="prewrite_identity_and_swap_verified_no_write";
                return r;
            }
            writeRatio=prePlan.ratio;
        }
        r.ratio=writeRatio;
        const auto payload=(isProcFileBackend(o.oracleBackend) || o.oracleBackend=="force_swapout")?std::string("1"):std::to_string(writeRatio);
        System procSysBefore;
        struct stat procFileStat{};
        std::optional<OracleRequest> procGateRequest;
        std::optional<OracleRequest> finalProcRequest;
        double procGateNow=0;
        if(o.oracleBackend=="proc_file_writeback") {
            procSysBefore=systemSample(o.proc);
            if(::fstat(writebackFile.fd.get(),&procFileStat)!=0) throw error("fstat writeback file before proc_file");
            appendOracleWritebackStage(o,r,"proc_file_submit_preflight",{}, {},-2,0,&procFileStat,&*preSample,&procSysBefore,"proc_file_not_submitted_stage_ledger_flushed_before_gate");
            if(monotonic()>=req.launch) return finishNoProc("writeback_completed_deadline_before_proc_stage_ledger");
            gate=parseControl(readPath(o.control));
            if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle))
                return finishNoProc("writeback_completed_control_changed_before_proc");
            procGateRequest.emplace(getCurrentOracleRequest(o,evidence,actionId));
            if(!sameOracleRegistration(req,*procGateRequest)) return finishNoProc("writeback_completed_schedule_changed_before_proc");
            const OracleDonor* procGateDonor=nullptr;
            for(const auto& item:procGateRequest->donors) if(item.bundle==donor.bundle) procGateDonor=&item;
            procGateNow=monotonic();
            if(!procGateDonor) return finishNoProc("writeback_completed_donor_missing_before_proc");
            const auto procGateFailure=oracleBackgroundFailure(o,app,*procGateDonor,procGateNow);
            if(!procGateFailure.empty())
                return finishNoProc("writeback_completed_identity_or_background_changed_before_proc:"+procGateFailure);
            verifyOracleWritebackFile(writebackFile,donor,app);
            const auto lastPreProcSample=sampleOracleRss(*procGateDonor,app);
            procSysBefore=systemSample(o.proc);
            if(!lastPreProcSample || !lastPreProcSample->rssFileKiB || !lastPreProcSample->rssKiB ||
               *lastPreProcSample->rssFileKiB>std::numeric_limits<Bytes>::max()/1024 ||
               *lastPreProcSample->rssKiB>std::numeric_limits<Bytes>::max()/1024 ||
               !oracleProcFileWritebackEstimateFits(*lastPreProcSample->rssFileKiB,donor.writebackFileSize,nominal))
                return finishNoProc("writeback_completed_current_file_estimate_or_reservation_unknown_before_proc");
            const Bytes lastFileBytes=*lastPreProcSample->rssFileKiB*1024;
            const Bytes lastResidentBytes=*lastPreProcSample->rssKiB*1024;
            const Bytes keepFloor=oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep);
            if(lastResidentBytes<lastFileBytes || lastResidentBytes-lastFileBytes<keepFloor ||
               !oracleProcFileWritebackSystemGapFits(oracleEventGap(o,procSysBefore,forecast,req.needed),nominal))
                return finishNoProc("writeback_completed_current_keep_or_system_budget_gate_before_proc");
            preSample=lastPreProcSample;
            finalLive=procGateDonor;
            r.preRssAnonKiB=preSample->rssAnonKiB; r.preRssFileKiB=*preSample->rssFileKiB; r.preRssTotalKiB=*preSample->rssKiB;
            r.preFree=procSysBefore.free; r.preAvailable=procSysBefore.available;
            r.estimatedRetainedBytes=lastResidentBytes-lastFileBytes; r.keepReserveBytes=keepFloor;
            if(::fstat(writebackFile.fd.get(),&procFileStat)!=0) throw error("fstat writeback file after proc gate");
            appendOracleWritebackStage(o,r,"proc_file_gate_passed",{}, {},-2,0,&procFileStat,&*preSample,&procSysBefore,"final_identity_and_budget_gate_passed_before_log_flush");
            if(monotonic()>=req.launch) return finishNoProc("writeback_completed_deadline_after_proc_gate_ledger");
            gate=parseControl(readPath(o.control));
            if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle))
                return finishNoProc("writeback_completed_control_changed_after_proc_gate_ledger");
            controlAtProcWrite=gate; haveControlAtProcWrite=true;
            finalProcRequest.emplace(getCurrentOracleRequest(o,evidence,actionId));
            if(!sameOracleRegistration(req,*finalProcRequest)) return finishNoProc("writeback_completed_schedule_changed_after_proc_gate_ledger");
            const OracleDonor* finalProcDonor=nullptr;
            for(const auto& item:finalProcRequest->donors) if(item.bundle==donor.bundle) finalProcDonor=&item;
            const double finalProcGateTime=monotonic();
            if(!finalProcDonor) return finishNoProc("writeback_completed_donor_missing_after_proc_gate_ledger");
            const auto finalProcFailure=oracleBackgroundFailure(o,app,*finalProcDonor,finalProcGateTime);
            if(!finalProcFailure.empty())
                return finishNoProc("writeback_completed_identity_or_background_changed_after_proc_gate_ledger:"+finalProcFailure);
            verifyOracleWritebackFile(writebackFile,donor,app);
            const auto finalPreProcSample=sampleOracleRss(*finalProcDonor,app);
            procSysBefore=systemSample(o.proc);
            if(!finalPreProcSample || !finalPreProcSample->rssFileKiB || !finalPreProcSample->rssKiB ||
               *finalPreProcSample->rssFileKiB>std::numeric_limits<Bytes>::max()/1024 ||
               *finalPreProcSample->rssKiB>std::numeric_limits<Bytes>::max()/1024 ||
               !oracleProcFileWritebackEstimateFits(*finalPreProcSample->rssFileKiB,donor.writebackFileSize,nominal))
                return finishNoProc("writeback_completed_final_file_estimate_or_reservation_unknown");
            const Bytes finalFileBytes=*finalPreProcSample->rssFileKiB*1024;
            const Bytes finalResidentBytes=*finalPreProcSample->rssKiB*1024;
            if(finalResidentBytes<finalFileBytes || finalResidentBytes-finalFileBytes<keepFloor ||
               !oracleProcFileWritebackSystemGapFits(oracleEventGap(o,procSysBefore,forecast,req.needed),nominal))
                return finishNoProc("writeback_completed_final_keep_or_system_budget_gate");
            preSample=finalPreProcSample; finalLive=finalProcDonor;
            r.preRssAnonKiB=preSample->rssAnonKiB; r.preRssFileKiB=*preSample->rssFileKiB; r.preRssTotalKiB=*preSample->rssKiB;
            r.preFree=procSysBefore.free; r.preAvailable=procSysBefore.available;
        }
        if(o.oracleBackend=="proc_file_writeback") {
            verifyOracleWritebackFile(writebackFile,donor,app);
            gate=parseControl(readPath(o.control));
            if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle))
                return finishNoProc("writeback_completed_runtime_control_changed_before_proc_syscall");
            const double identityCheckTime=monotonic();
            if(identityCheckTime>=req.launch) return finishNoProc("writeback_completed_deadline_at_final_identity_gate_before_proc");
            if(!finalProcRequest || !finalLive) return finishNoProc("writeback_completed_final_donor_missing_before_proc");
            const auto finalIdentityFailure=oracleBackgroundFailure(o,app,*finalLive,identityCheckTime);
            if(!finalIdentityFailure.empty())
                return finishNoProc("writeback_completed_final_identity_gate_before_proc:"+finalIdentityFailure);
        }
        // Capture the last pre-submit exchange snapshot first, then repeat the
        // existing final authorization checks so this read-only sampling can
        // never carry a stale deadline or donor authorization into write().
        const auto swapBefore=sampleOracleSwapGroups(root.get(),app.path,o.proc,o.oracleProofSource=="device" && o.oracleDiskPrepareAnon);
        const double finalTelemetryGate=monotonic();
        if(finalTelemetryGate>=req.launch) throw std::runtime_error("event_deadline_passed_after_pre_action_swap_telemetry");
        gate=parseControl(readPath(o.control));
        if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle))
            throw std::runtime_error("runtime_control_changed_after_pre_action_swap_telemetry");
        const auto telemetryLatest=getCurrentOracleRequest(o,evidence,actionId);
        if(!sameOracleRegistration(req,telemetryLatest))
            throw std::runtime_error("schedule_registration_changed_after_pre_action_swap_telemetry");
        const OracleDonor* telemetryLive=nullptr;
        for(const auto& item:telemetryLatest.donors) if(item.bundle==donor.bundle) telemetryLive=&item;
        if(!telemetryLive) throw std::runtime_error("donor_missing_after_pre_action_swap_telemetry");
        const auto telemetryFailure=oracleBackgroundFailure(o,app,*telemetryLive,monotonic());
        if(!telemetryFailure.empty()) throw std::runtime_error("oracle_evidence_reject_after_pre_action_swap_telemetry:"+telemetryFailure);
        if(o.oracleBackend=="proc_file_writeback") verifyOracleWritebackFile(writebackFile,donor,app);
        if(o.oracleBackend=="force_swapout") {
            diskBefore=parseOracleDiskPool(readAt(dir.get(),"memory.stat"));
            const auto rss=sampleOracleRss(*telemetryLive,app);
            if(!diskBefore || !rss || (o.oracleDiskPrepareAnon
                ?!oraclePipelineKeepSatisfied(*rss,o.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep))
                :rss->rssAnonKiB*1024<oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep)))
                throw std::runtime_error("disk_pool_or_identity_unknown_or_resident_below_keep");
            if(!oracleDiskPoolMeetsMinimum(diskBefore->zramBytes,o.minBatch))
                return finishNoProc("disk_pool_below_minimum_no_write");
            const auto work=oracleDiskWorkEstimate(diskBefore->zramBytes,rss->rssAnonKiB);
            // An observed transfer can include anonymous pages that entered Zram
            // during submission. Charge resident anonymous memory as well as the
            // existing pool; command 1 is not a byte-limited pool-drain request.
            if(!work || oracleDiskReservation(*work,nominal,0,o.minBatch)==0)
                return finishNoProc("disk_pool_changed_outside_reserved_estimate_no_write");
            diskWorkBefore=*work;
            // Recheck the full gate after the final pool read. There is no kernel
            // byte limit on command 1; this is an anonymous working-set estimate.
            const auto diskGate=getCurrentOracleRequest(o,evidence,actionId);
            const auto diskControl=parseControl(readPath(o.control));
            if(!sameOracleRegistration(req,diskGate) || diskControl.paused ||
               diskControl.protectedApps.count(app.name) || diskControl.protectedApps.count(donor.bundle))
                throw std::runtime_error("disk_runtime_authorization_changed");
            const OracleDonor* diskDonor=nullptr;
            for(const auto& item:diskGate.donors) if(item.bundle==donor.bundle) diskDonor=&item;
            if(!diskDonor || !oracleBackgroundFailure(o,app,*diskDonor,monotonic()).empty())
                throw std::runtime_error("disk_background_or_identity_not_fresh");
            const auto diskSystem=systemSample(o.proc); r.preFree=diskSystem.free; r.preAvailable=diskSystem.available;
            if(sub(diskSystem.swapFree,o.swapReserve)<nominal)
                throw std::runtime_error("disk_swap_headroom_below_reserved_work_estimate");
        }
        if(r.preparationWorkMode) {
            const auto liveWork=sampleOracleRss(*telemetryLive,app);
            if(!liveWork || !oraclePreparationWorkReservation(*liveWork,nominal,0))
                return finishNoProc("preparation_work_changed_outside_reservation_no_write");
            if(!oraclePipelineKeepSatisfied(*liveWork,o.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep)))
                throw std::runtime_error("preparation_final_keep_unknown_or_below_floor");
            if(!oraclePreparationFinalRatioAllowed(*liveWork,plannedIncrement,writeRatio,o.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep)))
                return finishNoProc("preparation_final_ratio_no_longer_within_expected_keep_cap_no_write");
            const auto liveSystem=systemSample(o.proc); r.preFree=liveSystem.free; r.preAvailable=liveSystem.available;
            if(oracleEventGap(o,liveSystem,forecast,req.needed)<nominal)
                return finishNoProc("preparation_system_deficit_below_reservation_no_write");
            if(sub(liveSystem.swapFree,o.swapReserve)<nominal)
                return finishNoProc("preparation_swap_headroom_below_reservation_no_write");
            const auto workLatest=getCurrentOracleRequest(o,evidence,actionId);
            const auto workControl=parseControl(readPath(o.control));
            const OracleDonor* workDonor=nullptr;
            for(const auto& item:workLatest.donors) if(item.bundle==donor.bundle) workDonor=&item;
            const bool registrationControlValid=sameOracleRegistration(req,workLatest) && workDonor &&
                !workControl.paused && !workControl.protectedApps.count(app.name) &&
                !workControl.protectedApps.count(donor.bundle);
            if(!registrationControlValid)
                throw std::runtime_error("preparation_final_registration_control_or_donor_changed");
            // Check every live identity/background/keep invariant before treating only
            // time expiry as deferrable. No write has occurred; charge is retained.
            const auto finalLiveGuardFailure=oracleBackgroundFailure(o,app,*workDonor,monotonic(),nullptr,nullptr,false);
            const double authorizationNow=monotonic();
            const auto freshnessFailure=oracleFreshnessFailureImpl(true,authorizationNow,
                {{"aa",workDonor->aaObserved},{"window",workDonor->windowObserved},
                 {"proc",workDonor->procObserved},{"lifecycle",workDonor->lifecycleObserved}},2.5);
            const auto disposition=finalAuthorizationDisposition(registrationControlValid,finalLiveGuardFailure.empty(),
                freshnessFailure.c_str(),writeAttempted,r.writebackAttempted);
            if(disposition==FinalAuthorizationDisposition::Reject)
                throw std::runtime_error("preparation_final_authorization_rejected:"+
                    (finalLiveGuardFailure.empty()?freshnessFailure:finalLiveGuardFailure));
            if(disposition==FinalAuthorizationDisposition::DeferFreshness) {
                double observed=workDonor->aaObserved;
                if(freshnessFailure=="window_evidence_stale") observed=workDonor->windowObserved;
                else if(freshnessFailure=="proc_evidence_stale") observed=workDonor->procObserved;
                else if(freshnessFailure=="lifecycle_evidence_stale") observed=workDonor->lifecycleObserved;
                std::ostringstream detail; detail<<std::setprecision(17)<<"oracle_evidence_defer:"<<freshnessFailure
                    <<":age_s="<<(authorizationNow-observed)<<":observed="<<observed<<":checked="<<authorizationNow;
                throw std::runtime_error(detail.str());
            }
        }
        if(o.oracleKnownFutureFreeOnly) {
            const auto knownRequest=getCurrentOracleRequest(o,evidence,actionId);
            const auto knownControl=parseControl(readPath(o.control));
            if(!sameOracleRegistration(req,knownRequest) || knownControl.paused ||
               knownControl.protectedApps.count(app.name) || knownControl.protectedApps.count(donor.bundle))
                throw std::runtime_error("known_future_final_registration_or_control_changed");
            const OracleDonor* knownDonor=nullptr;
            for(const auto& item:knownRequest.donors) if(item.bundle==donor.bundle) knownDonor=&item;
            if(!knownDonor) throw std::runtime_error("known_future_final_donor_missing");
            const auto knownFailure=oracleBackgroundFailure(o,app,*knownDonor,monotonic());
            if(!knownFailure.empty()) throw std::runtime_error("known_future_final_identity_or_background:"+knownFailure);
            // Re-read actual Free after all potentially slow authorization reads. Never reuse a trend or
            // enlarge this action beyond its already charged reservation.
            const auto knownSystem=systemSample(o.proc);
            r.readyFreeTargetBytes=oracleEventFreeTarget(o,req.needed);
            r.preFree=knownSystem.free; r.preAvailable=knownSystem.available;
            r.freeGapBytes=oracleEventGap(o,knownSystem,forecast,req.needed);
            if(*r.freeGapBytes<nominal)
                return finishNoProc("known_future_free_gap_below_reserved_work_no_write");
        }
        if(o.oracleBackend=="force_swapout" || o.oracleDiskPrepareAnon)
            requireOracleDiskBackendEnabled();
        const double syscallStart=monotonic();
        if(syscallStart>=req.launch) throw std::runtime_error("event_deadline_passed_at_syscall_start");
        r.syscallStartBoot=syscallStart;
        writeAttempted=true; r.writeAttempted=true; errno=0; const auto count=::write(writer.get(),payload.data(),payload.size());
        const int writeErrno=count<0?errno:0;
        const double syscallReturn=monotonic();
        r.syscallReturnBoot=syscallReturn;
        r.writeDurationMs=(syscallReturn-syscallStart)*1000.0;
        r.writeOverlapsDeadline=oracleWriteCrossedDeadline(syscallStart,syscallReturn,req.launch);
        r.errorNo=writeErrno; r.writeLength=static_cast<long>(count); r.elapsedMs=(syscallReturn-begin)*1000;
        const auto swapAfter=sampleOracleSwapGroups(root.get(),app.path,o.proc,o.oracleProofSource=="device" && o.oracleDiskPrepareAnon);
        appendOracleSwapTelemetry(o,r,swapBefore,swapAfter);
        writeComplete=isProcFileBackend(o.oracleBackend)
            ?oracleProcFileWriteComplete(static_cast<long>(count),payload.size())
            :count==static_cast<ssize_t>(payload.size());
        const auto postSample=sampleOracleRss(*finalLive,app);
        std::optional<System> procReturnSystem;
        if(isProcFileBackend(o.oracleBackend)) {
            try { procReturnSystem=systemSample(o.proc); } catch(const std::exception&) {}
        }
        if(o.oracleBackend=="proc_file_writeback") {
            struct stat returnedFileStat{};
            const bool returnedFileStatOk=::fstat(writebackFile.fd.get(),&returnedFileStat)==0;
            appendOracleWritebackStage(o,r,"proc_file_returned",r.syscallStartBoot,r.syscallReturnBoot,
                static_cast<long>(count),writeErrno,returnedFileStatOk?&returnedFileStat:nullptr,
                postSample?&*postSample:nullptr,procReturnSystem?&*procReturnSystem:nullptr,
                count==static_cast<ssize_t>(payload.size())?"proc_file_write_returned_complete":"proc_file_write_returned_error_or_partial_no_retry");
        }
        if(count!=static_cast<ssize_t>(payload.size())) r.status="write_failed_or_partial_no_retry";
        else {
            r.status="write_accepted_feedback_unknown";
            const unsigned delay=o.oracleProfile=="peak_transition_os_snapshot"?0U:o.oracleFeedbackDelayMs;
            if(delay>0) std::this_thread::sleep_for(std::chrono::milliseconds(delay));
            System procSysAfter; bool haveProcSysAfter=false;
            const auto feedback=compareOracleRssSamples(donor.pid,uintValue(donor.uid),donor.start,preSample,postSample);
            r.feedbackIdentityVerified=feedback.identityVerified;
            if(postSample && feedback.identityVerified) {
                r.postRssAnonKiB=postSample->rssAnonKiB;
                r.postRssTotalKiB=postSample->rssKiB.value_or(0);
                r.postRssFileKiB=postSample->rssFileKiB.value_or(0);
                r.signedRssDeltaKiB=feedback.signedDeltaKiB;
                r.feedbackDrop=feedback.dropBytes;
                if(o.oracleBackend=="force_swapout") {
                    const auto diskAfter=parseOracleDiskPool(readAt(dir.get(),"memory.stat"));
                    const auto diskSystem=systemSample(o.proc);
                    r.postFree=diskSystem.free; r.postAvailable=diskSystem.available;
                    const Bytes transfer=diskBefore && diskAfter?sub(diskAfter->eswapBytes,diskBefore->eswapBytes):0;
                    const bool exceeded=transfer>nominal;
                    const bool positive=!exceeded && diskBefore && diskAfter && diskAfter->zramBytes<diskBefore->zramBytes &&
                        diskAfter->eswapBytes>diskBefore->eswapBytes && (o.oracleDiskPrepareAnon
                            ?oraclePipelineKeepSatisfied(*postSample,o.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep))
                            :postSample->rssAnonKiB*1024>=oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep));
                    const fs::path diskPath=o.oracleLedger+".disk_v2.csv";
                    const std::string diskHeader="action_id,backend,command,nominal_estimated_bytes,pre_zram_bytes,post_zram_bytes,pre_eswap_bytes,post_eswap_bytes,pre_memfree_bytes,post_memfree_bytes,pre_memavailable_bytes,post_memavailable_bytes,positive_swap_transition,physical_release_bound,pre_anonymous_work_estimate_bytes,observed_eswap_increase_bytes,transfer_exceeded_reservation";
                    const bool diskFresh=!fs::exists(diskPath) || fs::file_size(diskPath)==0;
                    if(!diskFresh) { std::ifstream prior(diskPath); std::string header; if(!std::getline(prior,header) || header!=diskHeader) throw std::runtime_error("disk ledger schema mismatch"); }
                    std::ofstream diskLog(diskPath,std::ios::app);
                    if(diskFresh) diskLog<<diskHeader<<'\n';
                    diskLog<<oracleSwapTelemetryCsvRow({r.actionId,o.oracleBackend,"1",std::to_string(nominal),
                        diskBefore?std::to_string(diskBefore->zramBytes):"",diskAfter?std::to_string(diskAfter->zramBytes):"",
                        diskBefore?std::to_string(diskBefore->eswapBytes):"",diskAfter?std::to_string(diskAfter->eswapBytes):"",
                        std::to_string(r.preFree),std::to_string(r.postFree),std::to_string(r.preAvailable),std::to_string(r.postAvailable),
                        positive?"true":"false","unknown_not_byte_limited",std::to_string(diskWorkBefore),
                        std::to_string(transfer),exceeded?"true":"false"})<<'\n';
                    diskLog.flush(); if(!diskLog) throw std::runtime_error("disk ledger write failed");
                    r.feedbackSampleStatus=positive?"verified_identity_and_zram_to_eswap_transition":"disk_feedback_unknown_or_nonpositive";
                    r.status=exceeded?"write_accepted_disk_transfer_exceeded_reservation_halt":
                        positive?"write_accepted_disk_swap_transition_observed":"write_accepted_feedback_nonpositive_halt";
                } else if(isProcFileBackend(o.oracleBackend)) {
                    if(procReturnSystem) { procSysAfter=*procReturnSystem; haveProcSysAfter=true; }
                    r.postFree=procSysAfter.free; r.postAvailable=procSysAfter.available; r.postCached=procSysAfter.cached; r.postDirty=procSysAfter.dirty;
                    if(!haveProcSysAfter || !oracleProcFileFeedbackMayContinue(true,preSample->rssFileKiB,postSample->rssFileKiB)) {
                        r.feedbackSampleStatus="postwrite_file_or_system_sample_unknown_halt";
                    } else {
                        r.postRssFileKiB=*postSample->rssFileKiB;
                        r.fileFeedbackPresent=true;
                        r.signedRssFileDeltaKiB=static_cast<std::int64_t>(*preSample->rssFileKiB)-static_cast<std::int64_t>(*postSample->rssFileKiB);
                        if(r.signedRssFileDeltaKiB>0) {
                            r.feedbackDrop=static_cast<Bytes>(r.signedRssFileDeltaKiB)*1024;
                            r.feedbackSampleStatus="verified_positive_process_rssfile_delta_observation";
                            r.status="write_accepted_file_feedback_measured_positive_rss_observation";
                        } else { r.feedbackSampleStatus=r.signedRssFileDeltaKiB==0?"verified_zero_rssfile_delta_halt_unknown_effect":"verified_rssfile_growth_halt_unknown_effect"; r.status="write_accepted_feedback_nonpositive_halt"; }
                    }
                } else if(o.oracleDiskPrepareAnon &&
                    !oraclePipelineKeepSatisfied(*postSample,o.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep))) {
                    r.feedbackSampleStatus="pipeline_postwrite_total_or_anon_keep_violated";
                    r.status="write_accepted_keep_violation_halt";
                } else if(feedback.positiveDrop) {
                    r.feedbackSampleStatus="verified_positive_process_rss_delta_observation";
                    r.status="write_accepted_feedback_measured_positive_rss_observation";
                } else {
                    r.feedbackSampleStatus=feedback.signedDeltaKiB==0?"verified_zero_delta_halt_unknown_effect":"verified_rss_growth_halt_unknown_effect";
                    r.status="write_accepted_feedback_nonpositive_halt";
                }
            } else {
                r.feedbackSampleStatus="postwrite_identity_or_rss_sample_unknown_halt";
            }
            if(o.oracleBackend=="proc_file_writeback") {
                struct stat procFileAfter{};
                const bool procFileStatOk=::fstat(writebackFile.fd.get(),&procFileAfter)==0;
                appendOracleWritebackStage(o,r,"proc_file_feedback",r.syscallStartBoot,r.syscallReturnBoot,
                    static_cast<long>(count),writeErrno,procFileStatOk?&procFileAfter:nullptr,postSample?&*postSample:nullptr,
                    haveProcSysAfter?&procSysAfter:nullptr,r.feedbackSampleStatus);
            }
            const double postwriteCheckBoot=monotonic();
            try {
                const auto postReq=getCurrentOracleRequest(o,evidence,actionId);
                const OracleDonor* postDonor=nullptr; for(const auto& item:postReq.donors) if(item.bundle==donor.bundle) postDonor=&item;
                std::string postwriteFailure;
                std::optional<OracleRssSample> floorGuardSample;
                const std::string originalFeedbackStatus=r.feedbackSampleStatus;
                if(!sameOracleRegistration(req,postReq)) postwriteFailure="schedule_registration_changed";
                else if(!postDonor) postwriteFailure="donor_missing_from_schedule";
                else postwriteFailure=oracleBackgroundFailure(o,app,*postDonor,postwriteCheckBoot,nullptr,&floorGuardSample);
                // This exception never turns the write into success. A fresh full guard
                // must reach its final, verified floor check; every earlier failure halts.
                if(postwriteFailure=="pipeline_process_keep_verified_below_floor" &&
                   o.oracleProofSource=="device" && o.oracleProfile=="peak_transition_os_snapshot" && req.schemaVersion==2 &&
                   o.oracleDiskPrepareAnon && o.oracleBackend=="force_shrink_all") {
                    const auto isolationControl=parseControl(readPath(o.control));
                    const auto isolationReq=getCurrentOracleRequest(o,evidence,actionId);
                    const auto finalIsolationReq=getCurrentOracleRequest(o,evidence,actionId);
                    const OracleDonor* isolationDonor=nullptr;
                    for(const auto& item:finalIsolationReq.donors) if(item.bundle==donor.bundle) isolationDonor=&item;
                    std::optional<OracleRssSample> isolationSample;
                    const double isolationCheck=monotonic();
                    const std::string isolationGuard=isolationDonor
                        ?oracleBackgroundFailure(o,app,*isolationDonor,isolationCheck,nullptr,&isolationSample)
                        :"donor_missing_from_schedule";
                    const auto finalIsolationControl=parseControl(readPath(o.control));
                    const auto completeSample=[](const std::optional<OracleRssSample>& sample) {
                        return sample && sample->rssKiB && sample->rssFileKiB && sample->vmSwapKiB;
                    };
                    const auto latestFeedback=compareOracleRssSamples(donor.pid,uintValue(donor.uid),donor.start,preSample,isolationSample);
                    const double isolationFinished=monotonic();
                    const auto floorEvidence=constructPostwriteFloorEvidence(
                        true,writeComplete && r.writeAttempted && r.errorNo==0,
                        completeSample(preSample) && completeSample(postSample) && feedback.identityVerified && feedback.positiveDrop,
                        r.status.c_str(),isolationGuard.c_str(),isolationDonor &&
                            oracleFreshnessFailureImpl(true,isolationFinished,{{"aa",isolationDonor->aaObserved},{"window",isolationDonor->windowObserved},
                                {"proc",isolationDonor->procObserved},{"lifecycle",isolationDonor->lifecycleObserved}},2.5).empty(),
                        completeSample(isolationSample) && latestFeedback.identityVerified,latestFeedback.positiveDrop,
                        sameOracleRegistration(req,isolationReq) && sameOracleRegistration(req,finalIsolationReq),
                        !isolationControl.paused && isolationControl.paused==gate.paused &&
                            isolationControl.protectedApps==gate.protectedApps && isolationControl.boost==gate.boost && isolationControl.until==gate.until &&
                            !finalIsolationControl.paused && finalIsolationControl.protectedApps==gate.protectedApps &&
                            finalIsolationControl.boost==gate.boost && finalIsolationControl.until==gate.until,
                        r.syscallStartBoot && r.syscallReturnBoot && *r.syscallStartBoot<req.launch &&
                            *r.syscallReturnBoot<req.launch && isolationFinished<req.launch,
                        r.preparationWorkMode && feedback.dropBytes<=nominal && latestFeedback.dropBytes<=nominal);
                    r.isolateFloorDonor=isolatePostwriteFloorDonor(floorEvidence);
                    r.floorIsolationEvidence="original_feedback="+originalFeedbackStatus+";original_guard="+postwriteFailure+";guard="+isolationGuard+
                        ";checked_at="+std::to_string(isolationCheck)+";finished_at="+std::to_string(isolationFinished)+
                        ";registration_same="+(floorEvidence.registrationSame?"true":"false")+
                        ";control_same="+(floorEvidence.controlSame?"true":"false")+
                        ";complete_write="+(floorEvidence.completeWrite?"true":"false")+
                        ";original_complete="+(floorEvidence.originalFeedbackComplete?"true":"false")+
                        ";latest_complete="+(floorEvidence.latestSampleComplete?"true":"false")+
                        ";latest_positive="+(floorEvidence.latestPositiveFeedback?"true":"false")+
                        ";within_deadline="+(floorEvidence.withinDeadline?"true":"false")+
                        ";within_reservation="+(floorEvidence.withinReservation?"true":"false")+
                        ";latest_drop_bytes="+std::to_string(latestFeedback.dropBytes)+";reservation_bytes="+std::to_string(nominal)+
                        ";control_paused="+(finalIsolationControl.paused?"true":"false")+
                        ";control_protected_count="+std::to_string(finalIsolationControl.protectedApps.size())+
                        ";control_boost="+std::to_string(finalIsolationControl.boost)+
                        ";control_until="+std::to_string(finalIsolationControl.until)+
                        ";plan_fingerprint="+isolationReq.planFingerprint+
                        ";proof_sequence="+std::to_string(finalIsolationReq.proofSequence)+
                        ";pid="+std::to_string(donor.pid)+";uid="+donor.uid+";start_ticks="+std::to_string(donor.start)+
                        ";latest_anon_kib="+(isolationSample?std::to_string(isolationSample->rssAnonKiB):"unknown")+
                        ";latest_rss_kib="+(isolationSample && isolationSample->rssKiB?std::to_string(*isolationSample->rssKiB):"unknown")+
                        ";latest_file_kib="+(isolationSample && isolationSample->rssFileKiB?std::to_string(*isolationSample->rssFileKiB):"unknown")+
                        ";latest_swap_kib="+(isolationSample && isolationSample->vmSwapKiB?std::to_string(*isolationSample->vmSwapKiB):"unknown")+
                        ";aa_age_s="+(isolationDonor?std::to_string(isolationFinished-isolationDonor->aaObserved):"unknown")+
                        ";window_age_s="+(isolationDonor?std::to_string(isolationFinished-isolationDonor->windowObserved):"unknown")+
                        ";proc_age_s="+(isolationDonor?std::to_string(isolationFinished-isolationDonor->procObserved):"unknown")+
                        ";lifecycle_age_s="+(isolationDonor?std::to_string(isolationFinished-isolationDonor->lifecycleObserved):"unknown")+
                        ";anon_keep_bytes="+std::to_string(o.oracleCumulativeKeep)+";total_keep_bytes="+std::to_string(oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep));
                }
                // A completed anonymous write may outlive the AA/WMS snapshot.
                // Wait only for refreshed evidence; never resubmit the write.
                const bool diskPositive=o.oracleBackend=="force_swapout" &&
                    r.status=="write_accepted_disk_swap_transition_observed";
                const auto mayRefresh=[&](const std::string& reason) {
                    return oracleAnonPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,reason) ||
                        (diskPositive && oracleDiskPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,reason));
                };
                if(mayRefresh(postwriteFailure) && r.feedbackIdentityVerified &&
                   (feedback.positiveDrop || diskPositive) && postwriteCheckBoot<req.launch) {
                    const std::string initialFailure=postwriteFailure;
                    const double started=postwriteCheckBoot;
                    bool recovered=false;
                    while(oracleWritebackPostwriteRecheckRemaining(started,monotonic(),req.launch)>0) {
                        const auto currentControl=parseControl(readPath(o.control));
                        if(currentControl.paused || currentControl.protectedApps.count(app.name) ||
                           currentControl.protectedApps.count(donor.bundle) || currentControl.paused!=gate.paused ||
                           currentControl.protectedApps!=gate.protectedApps || currentControl.boost!=gate.boost ||
                           currentControl.until!=gate.until) {
                            postwriteFailure="runtime_control_changed_during_anon_recheck"; break;
                        }
                        const auto freshReq=getCurrentOracleRequest(o,evidence,actionId);
                        if(!sameOracleRegistration(req,freshReq)) {
                            postwriteFailure="schedule_registration_changed_during_anon_recheck"; break;
                        }
                        const OracleDonor* freshDonor=nullptr;
                        for(const auto& item:freshReq.donors) if(item.bundle==donor.bundle) freshDonor=&item;
                        if(!freshDonor) { postwriteFailure="donor_missing_during_anon_recheck"; break; }
                        postwriteFailure=oracleBackgroundFailure(o,app,*freshDonor,monotonic());
                        if(postwriteFailure.empty()) {
                            const auto freshRss=sampleOracleRss(*freshDonor,app);
                            const auto freshFeedback=compareOracleRssSamples(donor.pid,uintValue(donor.uid),donor.start,preSample,freshRss);
                            const auto freshDisk=diskPositive?parseOracleDiskPool(readAt(dir.get(),"memory.stat")):std::optional<OracleDiskPool>{};
                            const bool positiveStillValid=diskPositive
                                ?freshDisk && diskBefore && freshDisk->eswapBytes>diskBefore->eswapBytes &&
                                    freshDisk->zramBytes<diskBefore->zramBytes &&
                                    sub(freshDisk->eswapBytes,diskBefore->eswapBytes)<=nominal
                                :freshFeedback.positiveDrop;
                            const Bytes requiredKeep=diskPositive?oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep):o.oracleCumulativeKeep;
                            if(!freshFeedback.identityVerified || !positiveStillValid || !freshRss ||
                               (o.oracleDiskPrepareAnon
                                   ?!oraclePipelineKeepSatisfied(*freshRss,o.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep))
                                   :freshRss->rssAnonKiB<requiredKeep/1024)) {
                                postwriteFailure="anon_recheck_identity_rss_or_keep_unknown"; break;
                            }
                            // Verify the same registration and protection again after sampling.
                            const auto finalControl=parseControl(readPath(o.control));
                            const auto finalReq=getCurrentOracleRequest(o,evidence,actionId);
                            const OracleDonor* finalDonor=nullptr;
                            for(const auto& item:finalReq.donors) if(item.bundle==donor.bundle) finalDonor=&item;
                            if(finalControl.paused || finalControl.protectedApps!=gate.protectedApps ||
                               finalControl.boost!=gate.boost || finalControl.until!=gate.until ||
                               !sameOracleRegistration(req,finalReq) || !finalDonor) {
                                postwriteFailure="registration_or_control_changed_after_anon_recheck_sample"; break;
                            }
                            postwriteFailure=oracleBackgroundFailure(o,app,*finalDonor,monotonic());
                            if(postwriteFailure.empty()) {
                                if(oracleWritebackPostwriteRecheckRemaining(started,monotonic(),req.launch)<=0) {
                                    postwriteFailure="anon_recheck_deadline_or_timeout"; break;
                                }
                                r.postRssAnonKiB=freshRss->rssAnonKiB;
                                r.postRssTotalKiB=freshRss->rssKiB.value_or(0);
                                r.postRssFileKiB=freshRss->rssFileKiB.value_or(0);
                                r.signedRssDeltaKiB=freshFeedback.signedDeltaKiB;
                                r.feedbackDrop=freshFeedback.dropBytes;
                                r.aaObserved=finalDonor->aaObserved;
                                r.windowObserved=finalDonor->windowObserved;
                                r.procObserved=finalDonor->procObserved;
                                r.lifecycleObserved=finalDonor->lifecycleObserved;
                                r.feedbackSampleStatus=diskPositive?"verified_disk_transition_after_fresh_background_recheck":
                                    "verified_positive_process_rss_delta_after_fresh_background_recheck";
                                recovered=true; break;
                            }
                        }
                        if(!mayRefresh(postwriteFailure)) break;
                        const double recheckSleep=std::min(0.1,oracleWritebackPostwriteRecheckRemaining(started,monotonic(),req.launch));
                        if(recheckSleep<=0) break;
                        std::this_thread::sleep_for(std::chrono::duration<double>(recheckSleep));
                    }
                    if(!recovered && mayRefresh(postwriteFailure))
                        postwriteFailure="anon_postwrite_freshness_recheck_timeout";
                    appendOraclePostwriteRecheckStage(o,r,started,monotonic(),initialFailure,postwriteFailure,
                        recovered,nullptr,nullptr,nullptr);
                }
                if(!postwriteFailure.empty()) {
                    const bool mayRecheck=oracleWritebackPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,postwriteFailure) &&
                        r.feedbackIdentityVerified && r.fileFeedbackPresent && haveProcSysAfter && postwriteCheckBoot<req.launch;
                    if(mayRecheck) {
                        const double recheckStarted=postwriteCheckBoot;
                        std::string recheckFinalReason=postwriteFailure;
                        bool recheckRecovered=false;
                        std::optional<struct stat> recheckFileStat;
                        std::optional<OracleRssSample> recheckRss;
                        std::optional<System> recheckSystem;
                        while(!recheckRecovered) {
                            const double checkBoot=monotonic();
                            const double remainingSeconds=oracleWritebackPostwriteRecheckRemaining(recheckStarted,checkBoot,req.launch);
                            if(remainingSeconds<=0) {
                                recheckFinalReason=checkBoot>=req.launch?"event_deadline_passed_during_recheck":"postwrite_freshness_recheck_timeout";
                                break;
                            }
                            try {
                                const auto recheckControl=parseControl(readPath(o.control));
                                if(recheckControl.paused || recheckControl.protectedApps.count(app.name) || recheckControl.protectedApps.count(donor.bundle)) {
                                    recheckFinalReason="runtime_control_paused_or_protected_during_recheck";
                                    break;
                                }
                                if(!haveControlAtProcWrite || recheckControl.paused!=controlAtProcWrite.paused ||
                                   recheckControl.protectedApps!=controlAtProcWrite.protectedApps ||
                                   recheckControl.boost!=controlAtProcWrite.boost || recheckControl.until!=controlAtProcWrite.until) {
                                    recheckFinalReason="runtime_control_changed_during_recheck";
                                    break;
                                }
                                const auto recheckReq=getCurrentOracleRequest(o,evidence,actionId);
                                if(!sameOracleRegistration(req,recheckReq)) {
                                    recheckFinalReason="schedule_registration_changed_during_recheck";
                                    break;
                                }
                                const OracleDonor* recheckDonor=nullptr;
                                for(const auto& item:recheckReq.donors) if(item.bundle==donor.bundle) recheckDonor=&item;
                                if(!recheckDonor) {
                                    recheckFinalReason="donor_missing_from_schedule_during_recheck";
                                    break;
                                }
                                verifyOracleWritebackFile(writebackFile,*recheckDonor,app);
                                const double evidenceBoot=monotonic();
                                const std::string evidenceFailure=oracleBackgroundFailure(o,app,*recheckDonor,evidenceBoot);
                                if(!evidenceFailure.empty()) {
                                    if(oracleWritebackPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,evidenceFailure)) {
                                        recheckFinalReason=evidenceFailure;
                                    } else {
                                        recheckFinalReason="background_or_identity_changed_during_recheck:"+evidenceFailure;
                                        break;
                                    }
                                } else {
                                    if(monotonic()>=req.launch) {
                                        recheckFinalReason="event_deadline_passed_before_recheck_feedback_sample";
                                        break;
                                    }
                                    auto freshSample=sampleOracleRss(*recheckDonor,app);
                                    if(!freshSample) {
                                        recheckFinalReason="postwrite_recheck_rss_identity_sample_unknown";
                                        break;
                                    }
                                    System freshSystem=systemSample(o.proc);
                                    verifyOracleWritebackFile(writebackFile,*recheckDonor,app);
                                    const auto freshFeedback=compareOracleRssSamples(donor.pid,uintValue(donor.uid),donor.start,preSample,freshSample);
                                    const Bytes keepFloor=oracleDeviceDonorTotalKeepFloor(o,donor.bundle,app.keep);
                                    if(!oracleProcFileRecheckFeedbackMayContinue(freshFeedback.identityVerified,true,
                                           preSample->rssFileKiB,freshSample->rssFileKiB,freshSample->rssKiB,keepFloor)) {
                                        recheckFinalReason=!freshFeedback.identityVerified?"postwrite_recheck_process_identity_changed":
                                            !freshSample->rssFileKiB || !freshSample->rssKiB?"postwrite_recheck_file_or_total_rss_unknown":
                                            *freshSample->rssKiB<*freshSample->rssFileKiB?"postwrite_recheck_rss_components_invalid":
                                            (*freshSample->rssKiB-*freshSample->rssFileKiB)<(keepFloor/1024)?"postwrite_recheck_keep_reserve_failed":
                                            "postwrite_recheck_file_rss_not_decreased";
                                        recheckRss=std::move(freshSample);
                                        recheckSystem=freshSystem;
                                        struct stat currentFile{};
                                        if(::fstat(writebackFile.fd.get(),&currentFile)==0) recheckFileStat=currentFile;
                                        break;
                                    }
                                    const auto freshAfterEvidence=getCurrentOracleRequest(o,evidence,actionId);
                                    if(!sameOracleRegistration(req,freshAfterEvidence)) {
                                        recheckFinalReason="schedule_registration_changed_after_recheck_sample";
                                        break;
                                    }
                                    const auto finalControl=parseControl(readPath(o.control));
                                    if(finalControl.paused || finalControl.protectedApps.count(app.name) || finalControl.protectedApps.count(donor.bundle) ||
                                       finalControl.paused!=controlAtProcWrite.paused || finalControl.protectedApps!=controlAtProcWrite.protectedApps ||
                                       finalControl.boost!=controlAtProcWrite.boost || finalControl.until!=controlAtProcWrite.until) {
                                        recheckFinalReason="runtime_control_changed_after_recheck_sample";
                                        break;
                                    }
                                    const OracleDonor* finalDonor=nullptr;
                                    for(const auto& item:freshAfterEvidence.donors) if(item.bundle==donor.bundle) finalDonor=&item;
                                    if(!finalDonor) {
                                        recheckFinalReason="donor_missing_after_recheck_sample";
                                        break;
                                    }
                                    const std::string postSampleBackgroundFailure=oracleBackgroundFailure(o,app,*finalDonor,monotonic());
                                    if(!postSampleBackgroundFailure.empty()) {
                                        if(oracleWritebackPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,postSampleBackgroundFailure)) {
                                            recheckFinalReason=postSampleBackgroundFailure;
                                        } else {
                                            recheckFinalReason="background_or_identity_changed_after_recheck_sample:"+postSampleBackgroundFailure;
                                            break;
                                        }
                                    } else {
                                        recheckRss=std::move(freshSample);
                                        recheckSystem=freshSystem;
                                        struct stat currentFile{};
                                        if(::fstat(writebackFile.fd.get(),&currentFile)==0) recheckFileStat=currentFile;
                                        r.feedbackIdentityVerified=true;
                                        r.postRssAnonKiB=recheckRss->rssAnonKiB;
                                        r.postRssFileKiB=*recheckRss->rssFileKiB;
                                        r.postRssTotalKiB=*recheckRss->rssKiB;
                                        r.signedRssDeltaKiB=freshFeedback.signedDeltaKiB;
                                        r.signedRssFileDeltaKiB=static_cast<std::int64_t>(*preSample->rssFileKiB)-static_cast<std::int64_t>(*recheckRss->rssFileKiB);
                                        r.feedbackDrop=static_cast<Bytes>(r.signedRssFileDeltaKiB)*1024;
                                        r.fileFeedbackPresent=true;
                                        r.keepReserveBytes=keepFloor;
                                        r.postFree=recheckSystem->free; r.postAvailable=recheckSystem->available;
                                        r.postCached=recheckSystem->cached; r.postDirty=recheckSystem->dirty;
                                        r.feedbackSampleStatus="postwrite_recheck_recovered_after_"+postwriteFailure;
                                        r.status="write_accepted_file_feedback_measured_positive_rss_observation";
                                        recheckFinalReason="fresh_same_registration_identity_background_and_file_feedback_verified";
                                        recheckRecovered=true;
                                        break;
                                    }
                                }
                            } catch(const std::exception& e) {
                                recheckFinalReason=std::string("postwrite_recheck_exception:")+e.what();
                                break;
                            }
                            const double afterCheck=monotonic();
                            const double sleepSeconds=std::min(0.2,oracleWritebackPostwriteRecheckRemaining(recheckStarted,afterCheck,req.launch));
                            if(sleepSeconds<=0) {
                                recheckFinalReason=afterCheck>=req.launch?"event_deadline_passed_during_recheck":"postwrite_freshness_recheck_timeout";
                                break;
                            }
                            std::this_thread::sleep_for(std::chrono::duration<double>(sleepSeconds));
                        }
                        const double recheckEnded=monotonic();
                        if(oracleWritebackPostwriteRecheckRemaining(recheckStarted,recheckEnded,req.launch)<=0 && recheckRecovered) {
                            recheckRecovered=false;
                            recheckFinalReason=recheckEnded>=req.launch?"event_deadline_passed_before_recheck_completion":
                                "postwrite_freshness_recheck_timeout";
                            r.status="write_accepted_postwrite_recheck_halt:"+recheckFinalReason;
                        } else if(!recheckRecovered) {
                            r.status="write_accepted_postwrite_recheck_halt:"+recheckFinalReason;
                            r.feedbackSampleStatus="postwrite_recheck_initial="+postwriteFailure+";final="+recheckFinalReason;
                        }
                        struct stat currentFile{};
                        if(!recheckFileStat && ::fstat(writebackFile.fd.get(),&currentFile)==0) recheckFileStat=currentFile;
                        appendOraclePostwriteRecheckStage(o,r,recheckStarted,recheckEnded,postwriteFailure,recheckFinalReason,
                            recheckRecovered,recheckFileStat?&*recheckFileStat:nullptr,recheckRss?&*recheckRss:nullptr,
                            recheckSystem?&*recheckSystem:nullptr);
                    } else {
                        const std::string detail="postwrite_check_boottime_s="+std::to_string(postwriteCheckBoot)+":"+postwriteFailure;
                        r.feedbackSampleStatus=detail;
                        r.status="write_accepted_postwrite_identity_or_background_failure:"+postwriteFailure;
                    }
                }
            } catch(const std::exception& e) {
                r.isolateFloorDonor=false;
                const std::string detail="postwrite_check_boottime_s="+std::to_string(postwriteCheckBoot)+":schedule_check_exception:"+e.what();
                r.feedbackSampleStatus=detail;
                r.status=std::string("write_accepted_postwrite_schedule_check_exception:")+e.what();
            }
            const double feedbackComplete=monotonic();
            r.feedbackCompleteBoot=feedbackComplete;
            r.feedbackOverlapsDeadline=oracleFeedbackCrossedDeadline(feedbackComplete,req.launch);
            if(*r.feedbackOverlapsDeadline) r.isolateFloorDonor=false;
        }
    } catch(const std::exception& e) {
        r.isolateFloorDonor=false;
        if(writeAttempted) {
            if(writeComplete) {
                r.status=std::string("write_accepted_feedback_unknown_after_exception:")+e.what();
                if(r.feedbackSampleStatus.empty()) r.feedbackSampleStatus=std::string("postwrite_exception:")+e.what();
            } else r.status=std::string("write_failed_or_partial_no_retry:")+e.what();
        }
        else if(r.writebackAttempted) r.status=std::string("writeback_submitted_no_proc_after_exception:")+e.what();
        else { const std::string reason=e.what(); r.status=reason.rfind("oracle_evidence_defer:",0)==0?std::string("rejected:")+reason:std::string("rejected:")+reason; r.errorNo=reason.rfind("oracle_evidence_defer:",0)==0?0:errno; }
        if(writeAttempted) {
            try { const double feedbackComplete=monotonic(); r.feedbackCompleteBoot=feedbackComplete; r.feedbackOverlapsDeadline=oracleFeedbackCrossedDeadline(feedbackComplete,req.launch); }
            catch(...) { /* Keep unavailable timestamps null rather than inventing a value. */ }
        }
        r.elapsedMs=(monotonic()-begin)*1000;
    }
    r.overlap=monotonic()>req.launch; return r;
}
void appendOracleLedger(const Options& o,const OracleActionResult& r) {
    const auto& header=oracleActionLedgerHeaderV2();
    // Validate both schemas before writing either file, so a stale/bad timing
    // sibling can never leave an unmatched compatibility-ledger row.
    const fs::path timingPath=o.oracleLedger+".timing_v3.csv";
    const auto& timingHeader=oracleActionTimingLedgerHeaderV3();
    bool timingFresh=!fs::exists(timingPath) || fs::file_size(timingPath)==0;
    if(!timingFresh) {
        std::ifstream existing(timingPath); std::string priorHeader;
        if(!std::getline(existing,priorHeader)) throw std::runtime_error("oracle timing V3 ledger header missing");
        validateOracleActionTimingLedgerHeaderV3(priorHeader);
    }
    bool fresh=!fs::exists(o.oracleLedger) || fs::file_size(o.oracleLedger)==0;
    if(!fresh) {
        std::ifstream existing(o.oracleLedger); std::string priorHeader;
        if(!std::getline(existing,priorHeader)) throw std::runtime_error("oracle ledger header missing");
        validateOracleActionLedgerHeader(priorHeader);
    }
    std::ofstream out(o.oracleLedger,std::ios::app); if(!out) throw std::runtime_error("cannot append oracle ledger");
    if(fresh) out<<header<<'\n';
    out<<csvCell(r.actionId)<<','<<csvCell(r.eventId)<<','<<csvCell(r.caseId)<<','<<csvCell(r.bundle)<<','<<r.pid<<','<<csvCell(r.uid)<<','<<r.start<<','<<std::setprecision(12)<<r.deadline<<','<<r.needed<<','<<r.remaining<<','<<r.input<<','<<r.preRssAnonKiB<<','<<r.postRssAnonKiB<<','<<r.signedRssDeltaKiB<<','<<r.feedbackDrop<<','<<(r.feedbackIdentityVerified?"true":"false")<<','<<csvCell(r.feedbackSampleStatus)<<','<<r.ratio<<','<<r.nominal<<','<<r.writeLength<<','<<r.errorNo<<','<<r.elapsedMs<<','<<csvCell(r.status)<<','<<(r.overlap?"true":"false")<<','<<csvCell(r.evidenceSource)<<','<<r.aaObserved<<','<<r.windowObserved<<','<<r.procObserved<<','<<r.lifecycleObserved<<'\n';
    out.flush(); if(!out) throw std::runtime_error("oracle ledger write failed");

    if(r.readyFreeTargetBytes || r.observedFreeBytes || r.freeGapBytes || r.estimatedDonorAnonRoomBytes) {
        const fs::path budgetPath=o.oracleLedger+".free_budget_v1.csv";
        static const std::string budgetHeader="action_id,event_id,case_id,bundle,ready_free_target_bytes,observed_free_bytes,free_gap_bytes,estimated_donor_anon_room_after_floor_margin_bytes,stop_reason";
        const bool budgetFresh=!fs::exists(budgetPath) || fs::file_size(budgetPath)==0;
        if(!budgetFresh) {
            std::ifstream prior(budgetPath); std::string existing;
            if(!std::getline(prior,existing) || existing!=budgetHeader)
                throw std::runtime_error("oracle Free budget sidecar schema mismatch");
        }
        const auto text=[](const std::optional<Bytes>& value){ return value?std::to_string(*value):std::string{}; };
        std::ofstream budget(budgetPath,std::ios::app); if(!budget) throw std::runtime_error("cannot append oracle Free budget sidecar");
        if(budgetFresh) budget<<budgetHeader<<'\n';
        budget<<csvCell(r.actionId)<<','<<csvCell(r.eventId)<<','<<csvCell(r.caseId)<<','<<csvCell(r.bundle)<<','
            <<text(r.readyFreeTargetBytes)<<','<<text(r.observedFreeBytes)<<','<<text(r.freeGapBytes)<<','
            <<text(r.estimatedDonorAnonRoomBytes)<<','<<csvCell(r.status)<<'\n';
        budget.flush(); if(!budget) throw std::runtime_error("oracle Free budget sidecar write failed");
    }

    if(o.oracleDiskPrepareAnon && r.writeAttempted) {
        const fs::path keepPath=o.oracleLedger+".pipeline_keep_v1.csv";
        const bool keepFresh=!fs::exists(keepPath) || fs::file_size(keepPath)==0;
        std::ofstream keepLog(keepPath,std::ios::app);
        if(keepFresh) keepLog<<"action_id,backend,pre_anon_kib,post_anon_kib,pre_file_kib,post_file_kib,pre_total_kib,post_total_kib,anon_floor_bytes,app_floor_bytes,status\n";
        keepLog<<oracleSwapTelemetryCsvRow({r.actionId,r.backend,std::to_string(r.preRssAnonKiB),std::to_string(r.postRssAnonKiB),
            std::to_string(r.preRssFileKiB),std::to_string(r.postRssFileKiB),std::to_string(r.preRssTotalKiB),std::to_string(r.postRssTotalKiB),
            std::to_string(o.oracleCumulativeKeep),std::to_string(oracleDeviceDonorTotalKeepFloor(o,r.bundle,0)),r.status})<<'\n';
        keepLog.flush(); if(!keepLog) throw std::runtime_error("pipeline keep ledger write failed");
    }

    if(r.backend=="proc_file" && !r.actionId.empty()) {
        const fs::path filePath=o.oracleLedger+".proc_file_v1.csv";
        static const std::string fileHeader="action_id,event_id,case_id,bundle,pid,uid,start_ticks,backend,request_bytes,estimated_rssfile_bytes,write_length,errno,syscall_start_boottime_s,syscall_return_boottime_s,pre_vmrss_kib,pre_rssfile_kib,post_rssfile_kib,estimated_retained_rss_bytes,keep_reserve_bytes,signed_rssfile_delta_kib,pre_rssanon_kib,post_rssanon_kib,pre_memfree_bytes,post_memfree_bytes,pre_memavailable_bytes,post_memavailable_bytes,status";
        const bool fileFresh=!fs::exists(filePath) || fs::file_size(filePath)==0;
        if(!fileFresh) { std::ifstream prior(filePath); std::string h; if(!std::getline(prior,h) || h!=fileHeader) throw std::runtime_error("proc_file V1 ledger schema mismatch"); }
        std::ofstream fileOut(filePath,std::ios::app); if(!fileOut) throw std::runtime_error("cannot append proc_file ledger");
        if(fileFresh) fileOut<<fileHeader<<'\n';
        auto opt=[](const std::optional<double>& value) { return value?std::to_string(*value):std::string{}; };
        fileOut<<csvCell(r.actionId)<<','<<csvCell(r.eventId)<<','<<csvCell(r.caseId)<<','<<csvCell(r.bundle)<<','<<r.pid<<','<<csvCell(r.uid)<<','<<r.start<<",proc_file,null,"<<r.nominal<<','<<(r.writeAttempted?std::to_string(r.writeLength):std::string{})<<','<<(r.writeAttempted?std::to_string(r.errorNo):std::string{})<<','<<opt(r.syscallStartBoot)<<','<<opt(r.syscallReturnBoot)<<','<<r.preRssTotalKiB<<','<<r.preRssFileKiB<<','<<r.postRssFileKiB<<','<<r.estimatedRetainedBytes<<','<<r.keepReserveBytes<<','<<r.signedRssFileDeltaKiB<<','<<r.preRssAnonKiB<<','<<r.postRssAnonKiB<<','<<r.preFree<<','<<r.postFree<<','<<r.preAvailable<<','<<r.postAvailable<<','<<csvCell(r.status)<<'\n';
        fileOut.flush(); if(!fileOut) throw std::runtime_error("proc_file ledger write failed");
    }

    if(!r.floorIsolationEvidence.empty()) {
        const fs::path path=o.oracleLedger+".postwrite_floor_v1.csv";
        const bool floorFresh=!fs::exists(path) || fs::file_size(path)==0;
        std::ofstream floorOut(path,std::ios::app); if(!floorOut) throw std::runtime_error("cannot append floor disposition ledger");
        if(floorFresh) floorOut<<"action_id,event_id,bundle,original_status,disposition,evidence\n";
        floorOut<<csvCell(r.actionId)<<','<<csvCell(r.eventId)<<','<<csvCell(r.bundle)<<','<<csvCell(r.status)<<','
            <<(r.isolateFloorDonor && !r.overlap?"disable_donor_for_event":"halt_event")<<','<<csvCell(r.floorIsolationEvidence)<<'\n';
        floorOut.flush(); if(!floorOut) throw std::runtime_error("floor disposition ledger flush failed");
    }
    if(r.preparationWorkMode) {
        const fs::path workPath=o.oracleLedger+".preparation_work_v1.csv";
        const bool workFresh=!fs::exists(workPath) || fs::file_size(workPath)==0;
        std::ofstream workLog(workPath,std::ios::app); if(!workLog) throw std::runtime_error("cannot append preparation work ledger");
        if(workFresh) workLog<<"action_id,status,estimated_increment_bytes,conservative_work_reservation_bytes,observed_anon_drop_bytes,drop_exceeds_expected,drop_exceeds_reservation,estimate_semantics\n";
        const bool observed=r.feedbackIdentityVerified && r.preRssAnonKiB && r.postRssAnonKiB;
        workLog<<csvCell(r.actionId)<<','<<csvCell(r.status)<<','<<r.estimatedIncrementBytes<<','<<r.nominal<<','
            <<(observed?std::to_string(r.feedbackDrop):std::string{})<<','
            <<(observed?(r.feedbackDrop>r.estimatedIncrementBytes?"true":"false"):"")<<','
            <<(observed?(r.feedbackDrop>r.nominal?"true":"false"):"")
            <<",observable_whole_process_work_not_kernel_byte_bound\n";
        workLog.flush(); if(!workLog) throw std::runtime_error("preparation work ledger flush failed");
    }
    if(r.ratioSemantics=="cumulative_anon_watermark") {
        const fs::path watermarkPath=o.oracleLedger+".watermark_v1.csv";
        static const std::string watermarkHeader="action_id,event_id,case_id,donor_bundle,status,planned_ratio,actual_ratio,reserved_nominal_bytes,total_anon_proxy_bytes,current_vmswap_bytes,estimated_increment_bytes,estimated_retained_bytes";
        const bool watermarkFresh=!fs::exists(watermarkPath) || fs::file_size(watermarkPath)==0;
        if(!watermarkFresh) {
            std::ifstream existing(watermarkPath); std::string priorHeader;
            if(!std::getline(existing,priorHeader) || priorHeader!=watermarkHeader)
                throw std::runtime_error("cumulative watermark ledger schema mismatch");
        }
        std::ofstream watermark(watermarkPath,std::ios::app);
        if(!watermark) throw std::runtime_error("cannot append cumulative watermark ledger");
        if(watermarkFresh) watermark<<watermarkHeader<<'\n';
        const std::string actualRatio=r.syscallStartBoot?std::to_string(r.ratio):std::string();
        watermark<<csvCell(r.actionId)<<','<<csvCell(r.eventId)<<','<<csvCell(r.caseId)<<','<<csvCell(r.bundle)<<','
            <<csvCell(r.status)<<','<<r.plannedRatio<<','<<actualRatio<<','<<r.nominal<<','<<r.totalAnonProxyBytes<<','
            <<r.currentVmSwapBytes<<','<<r.estimatedIncrementBytes<<','<<r.estimatedRetainedBytes<<'\n';
        watermark.flush(); if(!watermark) throw std::runtime_error("cumulative watermark ledger write failed");
    }

    // Keep the established ledger schema byte-for-byte stable. Detailed timing
    // is written to a sibling V3 ledger; legacy trigger_boottime_s continues to
    // mean the scheduled deadline for existing readers.
    auto number=[](const std::optional<double>& value) { if(!value) return std::string(); std::ostringstream s; s<<std::setprecision(17)<<*value; return s.str(); };
    auto boolean=[](const std::optional<bool>& value) { return !value?std::string():(*value?std::string("true"):std::string("false")); };
    std::ofstream timing(timingPath,std::ios::app); if(!timing) throw std::runtime_error("cannot append oracle timing V3 ledger");
    if(timingFresh) timing<<timingHeader<<'\n';
    timing<<csvCell(r.actionId)<<','<<csvCell(r.eventId)<<','<<csvCell(r.caseId)<<','<<csvCell(r.status)<<','
          <<std::setprecision(17)<<r.deadline<<','<<number(r.syscallStartBoot)<<','<<number(r.syscallReturnBoot)<<','
          <<number(r.writeDurationMs)<<','<<number(r.feedbackCompleteBoot)<<','<<boolean(r.writeOverlapsDeadline)<<','
          <<boolean(r.feedbackOverlapsDeadline)<<','<<r.elapsedMs<<','<<(r.overlap?"true":"false")<<'\n';
    timing.flush(); if(!timing) throw std::runtime_error("oracle timing V3 ledger write failed");
}

void event(CSV& log, const std::string& level, const std::string& name, const std::string& message) {
    log.row({std::to_string(unixMillis()),Schema,level,name,message});
    std::cerr << level << " " << name << " " << message << '\n';
}
} // namespace

OracleSwapTelemetryRead readOracleSwapTelemetryNode(int groupFd,const std::string& node,std::size_t limit) {
    if(node!="memory.eswap_info" && node!="memory.zswap_info" &&
       node!="memory.stat" && node!="hyperhold_io_info_file" && node!="vmstat" && node!="meminfo")
        throw std::runtime_error("unsupported swap telemetry node");
    limit=std::min<std::size_t>(limit,(node=="vmstat" || node=="meminfo")?16384:4096);
    OracleSwapTelemetryRead result;
    result.startedBoottime=monotonic();
    FD fd(::openat(groupFd,node.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW));
    if(fd.get()<0) {
        result.errorNumber=errno;
        result.status=errno==ENOENT?"missing":"open_error";
        result.endedBoottime=monotonic();
        return result;
    }
    char buffer[1024];
    bool truncated=false;
    for(;;) {
        const auto room=result.raw.size()<limit?limit-result.raw.size():0;
        const auto readLimit=std::min<std::size_t>(sizeof(buffer),room+1);
        const auto n=::read(fd.get(),buffer,readLimit);
        if(n==0) break;
        if(n<0) {
            if(errno==EINTR) continue;
            result.errorNumber=errno; result.status="read_error"; result.raw.clear();
            result.endedBoottime=monotonic(); return result;
        }
        const auto count=static_cast<std::size_t>(n);
        const auto keep=std::min(room,count);
        result.raw.append(buffer,keep);
        if(keep<count) { truncated=true; break; }
    }
    result.status=truncated?"truncated":"ok";
    result.endedBoottime=monotonic();
    return result;
}

std::string oracleSwapTelemetryCsvRow(const std::vector<std::string>& fields) {
    std::string line;
    for(const auto& field:fields) {
        if(!line.empty()) line+=',';
        line+=csvCell(field);
    }
    return line;
}

std::string oracleFreshnessFailure(bool structuralEvidenceValid,double now,
    const std::vector<std::pair<std::string,double>>& observations,double maxAgeSeconds) {
    return oracleFreshnessFailureImpl(structuralEvidenceValid,now,observations,maxAgeSeconds);
}
bool oracleFreshnessFailureRetryable(const std::string& reason) {
    return oracleFreshnessFailureRetryableImpl(reason);
}
bool oracleUnsubmittedEvidenceMayDefer(const std::string& status,
    bool writeAttempted,bool writebackAttempted,bool overlap) {
    if(writeAttempted || writebackAttempted || overlap) return false;
    const std::string early="rejected:oracle_evidence_defer:";
    if(status.rfind(early,0)==0)
        return oracleFreshnessFailureRetryableImpl(status.substr(early.size()));
    const std::string late="rejected:oracle_evidence_reject_after_pre_action_swap_telemetry:";
    if(status.rfind(late,0)!=0) return false;
    const auto reason=status.substr(late.size());
    // Sampling may age authorization before the first write. Keep the charge,
    // defer, and require all normal gates again; never retry an unknown effect.
    return reason=="aa_evidence_stale" || reason=="window_evidence_stale" ||
        reason=="proc_evidence_stale" || reason=="lifecycle_evidence_stale";
}

void testDeviceOracleTargetGuard() {
    Options options;options.oracleProfile="peak_transition_os_snapshot";
    App app;OracleDonor target;target.uid="23";target.pid=17;target.start=91;target.aaPid=17;target.aaUid=23;target.readyAt=100;target.backgroundSince=100;target.aaState="FOREGROUND";
    if(oracleBackgroundFailure(options,app,target,200)!="aa_not_background")throw std::runtime_error("actual worker target foreground guard bypassed");
    target.aaState="BACKGROUND";target.aaUid=24;
    if(oracleBackgroundFailure(options,app,target,200)!="aa_uid_mismatch")throw std::runtime_error("actual worker target identity guard bypassed");
}

void validateOracleScheduleText(const std::string& text) { (void)parseOracleRequest(text); }

std::optional<OracleDiskPool> parseOracleDiskPool(const std::string& text) {
    std::map<std::string,Bytes> values;
    std::istringstream lines(text); std::string line;
    while(std::getline(lines,line)) {
        std::istringstream row(line); std::string key,value,unit,extra;
        if(!(row>>key) || (key!="Zram:" && key!="Eswap:" && key!="Nc_Zram:")) continue;
        if(!(row>>value>>unit) || unit!="kB" || (row>>extra) || values.count(key)) return {};
        try { values[key]=uintValue(value); } catch(...) { return {}; }
        if(values[key]>std::numeric_limits<Bytes>::max()/1024) return {};
    }
    if(values.size()!=3 || values.at("Nc_Zram:")!=0) return {};
    return OracleDiskPool{values.at("Zram:")*1024,values.at("Eswap:")*1024};
}
std::optional<Bytes> oracleDiskWorkEstimate(Bytes poolBytes,Bytes rssAnonKiB) {
    if(poolBytes==0 || rssAnonKiB>std::numeric_limits<Bytes>::max()/1024) return {};
    return daily::disk::wholeWork(poolBytes,rssAnonKiB*1024);
}

bool oracleDiskPoolMeetsMinimum(Bytes poolBytes,Bytes minimum) {
    return poolBytes>0 && poolBytes>=minimum;
}
std::optional<Bytes> oraclePipelineAnonKeep(const OracleRssSample& sample,Bytes anonKeep,Bytes totalKeep) {
    if(!sample.rssKiB || *sample.rssKiB<sample.rssAnonKiB ||
        *sample.rssKiB>std::numeric_limits<Bytes>::max()/1024) return {};
    const Bytes otherResident=(*sample.rssKiB-sample.rssAnonKiB)*1024;
    return std::max(anonKeep,sub(totalKeep,otherResident));
}
bool oraclePipelineKeepSatisfied(const OracleRssSample& sample,Bytes anonKeep,Bytes totalKeep) {
    const auto required=oraclePipelineAnonKeep(sample,anonKeep,totalKeep);
    return required && sample.rssAnonKiB*1024>=*required;
}
std::string oraclePipelineKeepFailure(const std::optional<OracleRssSample>& sample,bool identityVerified,
    Bytes anonKeep,Bytes totalKeep) {
    if(!identityVerified || !sample || sample->pid==0 || sample->uid==0 || sample->startTicks==0)
        return "pipeline_process_keep_identity_or_rss_unknown";
    const auto required=oraclePipelineAnonKeep(*sample,anonKeep,totalKeep);
    if(!required) return "pipeline_process_keep_fields_unknown_or_inconsistent";
    return sample->rssAnonKiB*1024<*required?"pipeline_process_keep_verified_below_floor":"";
}
bool oraclePrecommitMaySkipDonor(const std::string& fullGuardFailure) {
    return fullGuardFailure=="pipeline_process_keep_verified_below_floor";
}
bool oracleDiskPreparationComplete(bool attempted,const std::string& status,
    bool identityVerified,std::int64_t rssDeltaKiB,bool overlap) {
    return attempted && !overlap && identityVerified && rssDeltaKiB>0 &&
        status=="write_accepted_feedback_measured_positive_rss_observation";
}
Bytes oracleDeviceDonorTotalKeepFloor(const Options& o,const std::string& donorBundle,Bytes appKeep) {
    const Bytes legacy=std::max(appKeep,512*MiB);
    if(o.oracleProofSource!="device" || o.oracleProfile!="peak_transition_os_snapshot" ||
       !o.oracleDiskPrepareAnon || (o.oracleBackend!="force_shrink_all" && o.oracleBackend!="force_swapout"))
        return legacy;
    const std::string prefix="io.memscene.scene4.workload.a";
    bool ownedDonor=false;
    for(unsigned i=1;i<=13;++i) if(donorBundle==prefix+std::to_string(i)) ownedDonor=true;
    if(!ownedDonor) return legacy;
    return std::max(appKeep,o.oracleDeviceDonorTotalKeep);
}
Bytes oracleDiskReservation(Bytes pool,Bytes room,Bytes margin,Bytes minimum) {
    if(pool==0 || pool<minimum || margin>room || pool>room-margin) return 0;
    return pool+margin;
}

VendorStat parseVendorStat(const std::string& text) {
    VendorStat result;
    std::istringstream lines(text);
    std::string line;
    while (std::getline(lines,line)) {
        std::istringstream row(line);
        std::string key, value, unit, extra;
        if (!(row >> key >> value >> unit) || (row >> extra) || unit != "kB") continue;
        auto* destination = key == "Anon:" ? &result.anonKiB :
                            key == "Zram:" ? &result.zramKiB :
                            key == "Eswap:" ? &result.eswapKiB : nullptr;
        if (!destination) continue;
        try { *destination = uintValue(value); } catch (const std::exception&) { destination->reset(); }
    }
    return result;
}

bool oracleDiskBackendEnabledText(const std::string& text) {
    std::istringstream input(text);
    std::string name,state,reclaim,reclaimState,extra;
    return (input>>name>>state>>reclaim>>reclaimState) && !(input>>extra) &&
        name=="hyperhold" && state=="enable" && reclaim=="reclaim_in" && reclaimState=="enable";
}

ProcMemory parseProcMemory(const std::string& status,const std::string& smapsRollup) {
    const auto values=parseKiBFields(status+"\n"+smapsRollup);
    ProcMemory result;
    auto get=[&](const char* key)->std::optional<Bytes>{const auto it=values.find(key);return it==values.end()?std::nullopt:std::optional<Bytes>(it->second);};
    result.rssKiB=get("VmRSS"); result.vmSwapKiB=get("VmSwap");
    result.rssAnonKiB=get("RssAnon"); result.rssFileKiB=get("RssFile");
    result.pssKiB=get("Pss"); result.pssAnonKiB=get("Pss_Anon"); result.pssFileKiB=get("Pss_File");
    const auto privateClean=get("Private_Clean"), privateDirty=get("Private_Dirty");
    if(privateClean && privateDirty) result.privateKiB=add(*privateClean,*privateDirty);
    result.smapsSwapKiB=get("Swap");
    return result;
}

ProcStat parseProcStat(std::uint64_t pid,const std::string& statText) {
    std::istringstream header(statText);
    std::string textPid;
    if(!(header>>textPid) || uintValue(textPid)!=pid)
        throw std::runtime_error("/proc/PID/stat PID does not match requested process");
    const auto close=statText.rfind(')');
    if(close==std::string::npos) throw std::runtime_error("malformed /proc/PID/stat");
    std::istringstream fields(statText.substr(close+1)); std::string token;
    ProcStat result; result.pid=pid;
    for(int field=3;field<=22;++field) {
        if(!(fields>>token)) throw std::runtime_error("short /proc/PID/stat");
        if(field==10) result.minflt=uintValue(token);
        else if(field==12) result.majflt=uintValue(token);
        else if(field==22) result.startTicks=uintValue(token);
    }
    return result;
}

std::optional<OracleRssSample> parseOracleRssSample(std::uint64_t pid,
    std::uint64_t expectedUid, std::uint64_t expectedStartTicks,
    const std::string& appPath, const std::string& statBefore,
    const std::string& status, const std::string& cgroup,
    const std::string& statAfter) {
    try {
        const auto textPid=[&](const std::string& statText)->std::uint64_t {
            std::istringstream header(statText); std::string token;
            if(!(header>>token)) throw std::runtime_error("empty /proc/PID/stat");
            return uintValue(token);
        };
        if(textPid(statBefore)!=pid || textPid(statAfter)!=pid) return {};
        const auto before=parseProcStat(pid,statBefore);
        const auto after=parseProcStat(pid,statAfter);
        bool uidMatches=false;
        bool pidMatches=false;
        std::istringstream statusLines(status); std::string line;
        while(std::getline(statusLines,line)) {
            if(line.rfind("Pid:",0)==0) {
                std::istringstream fields(line.substr(4)); std::string value;
                if(fields>>value) pidMatches=uintValue(value)==pid;
            } else if(line.rfind("Uid:",0)==0) {
                std::istringstream fields(line.substr(4)); std::string value; unsigned count=0;
                uidMatches=true;
                while(fields>>value) {
                    if(uintValue(value)!=expectedUid) { uidMatches=false; break; }
                    ++count;
                }
                uidMatches=uidMatches && count==4;
            }
        }
        if(before.pid!=pid || after.pid!=pid || before.startTicks!=expectedStartTicks ||
           after.startTicks!=expectedStartTicks || before.startTicks!=after.startTicks ||
           !pidMatches || !uidMatches || !processInAppCgroup(cgroup,appPath)) return {};
        const auto memory=parseProcMemory(status,{});
        if(!memory.rssAnonKiB) return {};
        // parseKiBFields keeps the proc status integer in kB. Preserve that
        // unit for signed subtraction; compareOracleRssSamples converts only
        // a positive observed drop to bytes.
        return OracleRssSample{pid,expectedUid,expectedStartTicks,*memory.rssAnonKiB,memory.vmSwapKiB,memory.rssFileKiB,memory.rssKiB};
    } catch(const std::exception&) { return {}; }
}

OracleRssFeedback compareOracleRssSamples(std::uint64_t expectedPid,
    std::uint64_t expectedUid, std::uint64_t expectedStartTicks,
    const std::optional<OracleRssSample>& before,
    const std::optional<OracleRssSample>& after) {
    OracleRssFeedback result;
    const auto matches=[&](const std::optional<OracleRssSample>& sample) {
        return sample && sample->pid==expectedPid && sample->uid==expectedUid &&
            sample->startTicks==expectedStartTicks &&
            sample->rssAnonKiB<=static_cast<Bytes>(std::numeric_limits<std::int64_t>::max()) &&
            sample->rssAnonKiB<=std::numeric_limits<Bytes>::max()/1024;
    };
    if(!matches(before) || !matches(after)) return result;
    result.identityVerified=true;
    const auto pre=static_cast<std::int64_t>(before->rssAnonKiB);
    const auto post=static_cast<std::int64_t>(after->rssAnonKiB);
    result.signedDeltaKiB=pre-post;
    if(result.signedDeltaKiB>0) {
        result.positiveDrop=true;
        result.dropBytes=static_cast<Bytes>(result.signedDeltaKiB)*1024;
    }
    return result;
}

bool oracleFeedbackMayContinue(const std::string& status,bool identityVerified,
    std::int64_t signedDeltaKiB,bool postSamplePresent) {
    return (status=="write_accepted_feedback_measured_positive_rss_observation" ||
        status=="write_accepted_file_feedback_measured_positive_rss_observation") &&
        identityVerified && signedDeltaKiB>0 && postSamplePresent;
}

const std::string& oracleActionLedgerHeaderV2() {
    static const std::string header="action_id,event_id,case_id,donor_bundle,pid,uid,start_ticks,trigger_boottime_s,planned_needed_bytes,remaining_bytes,budget_input_bytes,pre_rssanon_kib,post_rssanon_kib,signed_rss_delta_kib,observed_process_rss_drop_bytes,feedback_identity_verified,feedback_sample_status,requested_ratio,nominal_bytes,write_length,errno,elapsed_ms,status,deadline_overlap,background_evidence_source,aa_observed_boottime_s,window_observed_boottime_s,proc_observed_boottime_s,lifecycle_observed_boottime_s";
    return header;
}

void validateOracleActionLedgerHeader(const std::string& header) {
    if(header!=oracleActionLedgerHeaderV2())
        throw std::runtime_error("oracle ledger schema mismatch; refusing to append feedback-v2 rows");
}

const std::string& oracleActionTimingLedgerHeaderV3() {
    static const std::string header="action_id,event_id,case_id,ledger_status,legacy_deadline_boottime_s,syscall_start_boottime_s,syscall_return_boottime_s,write_duration_ms,feedback_complete_boottime_s,write_crossed_deadline,feedback_crossed_deadline,legacy_elapsed_ms,legacy_deadline_overlap";
    return header;
}

void validateOracleActionTimingLedgerHeaderV3(const std::string& header) {
    if(header!=oracleActionTimingLedgerHeaderV3())
        throw std::runtime_error("oracle timing V3 ledger schema mismatch");
}

bool oracleWriteCrossedDeadline(double syscallStartBoottime,double syscallReturnBoottime,double deadlineBoottime) {
    if(!std::isfinite(syscallStartBoottime) || !std::isfinite(syscallReturnBoottime) ||
       !std::isfinite(deadlineBoottime) || syscallReturnBoottime<syscallStartBoottime)
        throw std::runtime_error("invalid syscall/deadline timing values");
    return syscallStartBoottime<deadlineBoottime && syscallReturnBoottime>deadlineBoottime;
}

bool oracleFeedbackCrossedDeadline(double feedbackCompleteBoottime,double deadlineBoottime) {
    if(!std::isfinite(feedbackCompleteBoottime) || !std::isfinite(deadlineBoottime))
        throw std::runtime_error("invalid feedback/deadline timing values");
    return feedbackCompleteBoottime>deadlineBoottime;
}

bool hasMemoryController(const std::string& cgroupText) {
    std::istringstream lines(cgroupText); std::string line;
    while (std::getline(lines,line)) {
        const auto first=line.find(':');
        const auto second=first==std::string::npos?first:line.find(':',first+1);
        if(first==std::string::npos || second==std::string::npos) continue;
        std::istringstream names(line.substr(first+1,second-first-1)); std::string name;
        while(std::getline(names,name,',')) if(name=="memory") return true;
    }
    return false;
}

std::vector<MountEntry> parseMounts(const std::string& mountsText) {
    std::vector<MountEntry> entries;
    std::istringstream lines(mountsText); std::string line;
    while (std::getline(lines,line)) {
        std::istringstream row(line); MountEntry entry;
        if (!(row >> entry.source >> entry.mountPoint >> entry.fsType >> entry.options)) continue;
        entry.mountPoint=decodeMountField(entry.mountPoint);
        entries.push_back(std::move(entry));
    }
    return entries;
}

void validateBackendRequest(const std::string& backend, bool apply) {
    if (backend != "linux-v2" && backend != "harmony-v1" && backend!="harmony-v1-ratio" && backend!="harmony-v1-ratio-probe")
        throw std::runtime_error("backend must be linux-v2, harmony-v1, harmony-v1-ratio, or harmony-v1-ratio-probe");
    if (backend == "harmony-v1" && apply)
        throw std::runtime_error("--apply requires the explicit harmony-v1-ratio-probe profile");
}

unsigned ratioForBudget(Bytes budgetBytes,Bytes residentAnonBytes) {
    if(!residentAnonBytes || !budgetBytes) return 0;
    const auto bounded=std::min(budgetBytes,sub(residentAnonBytes,KeepSlack));
    if(!bounded) return 0;
    const auto ratio=static_cast<unsigned>((bounded*100)/residentAnonBytes);
    return ratio<1?0:std::min(20U,ratio);
}

unsigned oracleRatioForCap(Bytes capBytes,Bytes donorBytes,unsigned maxRatio) {
    if(donorBytes==0 || maxRatio==0) return 0;
    const auto ratio=static_cast<unsigned>((capBytes*100)/donorBytes);
    return std::min(maxRatio,ratio);
}

OracleDeviceCooldownSelection oracleDeviceCooldownSelection(bool device,unsigned schema,bool diskPrepare,
    const std::string& backend,bool cooling,bool identityComplete,const std::string& backgroundFailure,bool protectedByControl) {
    if(!cooling) return OracleDeviceCooldownSelection::Available;
    if(!device || schema!=2 || !diskPrepare || (backend!="force_shrink_all" && backend!="force_swapout"))
        return OracleDeviceCooldownSelection::LegacyBlocked;
    if(!identityComplete || protectedByControl) return OracleDeviceCooldownSelection::HaltEvent;
    if(!backgroundFailure.empty()) return oracleFreshnessFailureRetryableImpl(backgroundFailure)
        ?OracleDeviceCooldownSelection::WaitForProof:OracleDeviceCooldownSelection::HaltEvent;
    return OracleDeviceCooldownSelection::SkipDonor;
}
bool oraclePreparationWorkMode(bool device,bool diskPrepare,const std::string& backend) {
    return device && diskPrepare && backend=="force_shrink_all";
}
std::optional<Bytes> oraclePreparationWorkEstimate(const OracleRssSample& sample) {
    if(!sample.rssKiB || !sample.vmSwapKiB || *sample.rssKiB<sample.rssAnonKiB ||
       *sample.rssKiB>std::numeric_limits<Bytes>::max()/1024 ||
       *sample.vmSwapKiB>std::numeric_limits<Bytes>::max()/1024) return {};
    const Bytes resident=*sample.rssKiB*1024, swapped=*sample.vmSwapKiB*1024;
    if(resident>std::numeric_limits<Bytes>::max()-swapped) return {};
    // Observable whole-process work estimate, not a kernel-enforced byte bound.
    return resident+swapped;
}
Bytes oraclePreparationWorkReservation(const OracleRssSample& sample,Bytes room,Bytes margin) {
    const auto work=oraclePreparationWorkEstimate(sample);
    if(!work || !*work || *work>std::numeric_limits<Bytes>::max()-margin) return 0;
    const Bytes reservation=*work+margin;
    return reservation<=room?reservation:0;
}
bool oraclePreparationFeedbackWithinReservation(bool workMode,Bytes drop,Bytes reservation) {
    return workMode?drop<=reservation:drop<=reservation+64*MiB;
}

OracleWatermarkPlan oracleCumulativeWatermarkForCap(Bytes residentAnonBytes,
    Bytes currentVmSwapBytes,Bytes capBytes,unsigned maxTargetRatio) {
    OracleWatermarkPlan plan;
    if(residentAnonBytes==0 || maxTargetRatio==0 || maxTargetRatio>95 ||
       residentAnonBytes>std::numeric_limits<Bytes>::max()-currentVmSwapBytes)
        return plan;
    const Bytes total=residentAnonBytes+currentVmSwapBytes;
    plan.totalAnonProxyBytes=total;
    plan.currentVmSwapBytes=currentVmSwapBytes;
    // Choose the highest integer total-anon watermark whose estimated
    // incremental swap remains within the separately reserved budget.
    for(unsigned ratio=maxTargetRatio;ratio>0;--ratio) {
        const Bytes target=(total/100)*ratio+((total%100)*ratio)/100;
        if(target<=currentVmSwapBytes) continue;
        const Bytes increment=target-currentVmSwapBytes;
        if(increment>capBytes) continue;
        plan.ratio=ratio;
        plan.estimatedIncrementBytes=increment;
        plan.estimatedRetainedBytes=sub(residentAnonBytes,increment);
        return plan;
    }
    return plan;
}

bool oraclePreparationFinalRatioAllowed(const OracleRssSample& sample,Bytes plannedIncrement,unsigned ratio,Bytes anonKeep,Bytes totalKeep) {
    if(!oraclePreparationWorkEstimate(sample)) return false;
    const auto keep=oraclePipelineAnonKeep(sample,anonKeep,totalKeep);
    if(!keep || *keep>std::numeric_limits<Bytes>::max()-64*MiB) return false;
    const Bytes resident=sample.rssAnonKiB*1024;
    const auto plan=oracleCumulativeWatermarkForCap(resident,*sample.vmSwapKiB*1024,
        std::min(plannedIncrement,sub(resident,*keep+64*MiB)),ratio);
    return ratio>0 && plan.ratio==ratio;
}
bool oracleRatioCanAdvance(unsigned priorSuccessfulRatio,unsigned candidateRatio) {
    return candidateRatio>0 && candidateRatio>priorSuccessfulRatio;
}

Bytes oracleProcFileReservation(Bytes estimatedRssFileBytes,Bytes estimatedResidentBytes,
    Bytes keepReserve,Bytes eventGap,Bytes batchCap,
    Bytes globalRoom,Bytes caseRoom,Bytes appRoom,Bytes safetyMargin,Bytes minBatch) {
    if(estimatedRssFileBytes==0 || estimatedRssFileBytes<minBatch ||
        estimatedResidentBytes<estimatedRssFileBytes ||
        estimatedResidentBytes-estimatedRssFileBytes<keepReserve) return 0;
    const Bytes cap=sub(std::min({eventGap,batchCap,globalRoom,caseRoom,appRoom}),safetyMargin);
    return estimatedRssFileBytes<=cap?estimatedRssFileBytes:0;
}
Bytes oracleProcFileWritebackReservation(Bytes estimatedRssFileBytes,Bytes registeredFileSize,
    Bytes estimatedResidentBytes,Bytes keepReserve,Bytes eventGap,Bytes batchCap,
    Bytes globalRoom,Bytes caseRoom,Bytes appRoom,Bytes safetyMargin,Bytes minBatch) {
    if(estimatedRssFileBytes==0 || registeredFileSize==0 || estimatedResidentBytes<estimatedRssFileBytes ||
       estimatedResidentBytes-estimatedRssFileBytes<keepReserve) return 0;
    const Bytes work=std::max(estimatedRssFileBytes,registeredFileSize);
    if(work<minBatch) return 0;
    const Bytes reserved=add(work,safetyMargin);
    if(reserved==Unlimited) return 0;
    const Bytes cap=std::min({eventGap,batchCap,globalRoom,caseRoom,appRoom});
    return reserved<=cap?reserved:0;
}
bool oracleProcFileWritebackEstimateFits(Bytes currentRssFileKiB,Bytes registeredFileSizeBytes,Bytes reservedBytes) {
    if(currentRssFileKiB==0 || registeredFileSizeBytes==0 || reservedBytes==0 ||
       currentRssFileKiB>std::numeric_limits<Bytes>::max()/1024) return false;
    return currentRssFileKiB*1024<=reservedBytes && registeredFileSizeBytes<=reservedBytes;
}
bool oracleProcFileWritebackSystemGapFits(Bytes currentGapBytes,Bytes reservedBytes) {
    return reservedBytes>0 && currentGapBytes>=reservedBytes;
}

std::optional<bool> oracleDiskPriorityPrepRoomAvailable(const OracleRssSample& sample,
    Bytes anonKeep,Bytes totalKeep,Bytes minBatch,Bytes safetyMargin) {
    if(minBatch==0 || !sample.rssKiB || sample.rssAnonKiB>std::numeric_limits<Bytes>::max()/1024 ||
       *sample.rssKiB>std::numeric_limits<Bytes>::max()/1024) return {};
    const auto keep=oraclePipelineAnonKeep(sample,anonKeep,totalKeep);
    if(!keep || *keep>std::numeric_limits<Bytes>::max()-safetyMargin) return {};
    const Bytes required=*keep+safetyMargin;
    if(sample.rssAnonKiB*1024<required) return false;
    return sample.rssAnonKiB*1024-required>=minBatch;
}
std::vector<OracleDiskPriorityAttempt> oracleDiskPriorityAttempts(
    const std::vector<unsigned>& rotation,const std::vector<bool>& zramPoolReady,
    const std::vector<bool>& diskPrepared,const std::vector<std::optional<bool>>& prepRoomAvailable) {
    std::vector<OracleDiskPriorityAttempt> attempts;
    std::vector<bool> prioritySeen(zramPoolReady.size(),false);
    for(const unsigned index:rotation) {
        const bool prepared=index<diskPrepared.size() && diskPrepared[index];
        const bool noPrepRoom=index<prepRoomAvailable.size() && prepRoomAvailable[index].has_value() && !*prepRoomAvailable[index];
        if(index<zramPoolReady.size() && zramPoolReady[index] && (prepared || noPrepRoom)) {
            attempts.push_back({index,true}); prioritySeen[index]=true;
        }
    }
    for(const unsigned index:rotation) {
        if(index>=prioritySeen.size() || index>=diskPrepared.size()) continue;
        if(!prioritySeen[index] || !diskPrepared[index]) attempts.push_back({index,false});
    }
    return attempts;
}
bool oracleProfileTargetMaySubmit(bool alreadySubmitted,bool identityComplete) {
    return !alreadySubmitted && identityComplete;
}
bool oracleProfileDonorCooldownScope(unsigned schemaVersion,const std::string& profile,
    const std::string& backend) {
    return schemaVersion==3 && profile=="peak_transition_os_snapshot" &&
        backend=="proc_file_writeback";
}
bool oracleProfileCooldownMayContinue(unsigned schemaVersion,const std::string& profile,
    const std::string& backend,bool identityComplete) {
    return oracleProfileDonorCooldownScope(schemaVersion,profile,backend) && identityComplete;
}
bool oraclePrecommitMemberWaitWindowOpen(bool waitStarted,double waitStart,double now,double launchDeadline) {
    if(!std::isfinite(now) || !std::isfinite(launchDeadline) || now>=launchDeadline) return false;
    if(!waitStarted) return true;
    return std::isfinite(waitStart) && waitStart>0 && now>=waitStart && now<waitStart+2.0;
}
OraclePrecommitMemberRetryAction oraclePrecommitMemberRetryAction(bool profileScope,
    bool readOk,bool parseValid,std::size_t entryCount,std::uint64_t solePid,
    std::uint64_t expectedPid,bool waitStarted,double waitStart,double now,double launchDeadline) {
    if(!profileScope) return OraclePrecommitMemberRetryAction::NotApplicable;
    if(!readOk || !parseValid || !oraclePrecommitMemberWaitWindowOpen(waitStarted,waitStart,now,launchDeadline))
        return OraclePrecommitMemberRetryAction::Stop;
    if(entryCount==1) {
        if(solePid!=expectedPid) return OraclePrecommitMemberRetryAction::Stop;
        return waitStarted?OraclePrecommitMemberRetryAction::Recovered:OraclePrecommitMemberRetryAction::NotApplicable;
    }
    return OraclePrecommitMemberRetryAction::Wait;
}
bool oracleWritebackMayProceed(bool fdatasyncSucceeded,bool beforeDeadline,
    bool identityAndBackgroundVerified,bool budgetStillFits) {
    return fdatasyncSucceeded && beforeDeadline && identityAndBackgroundVerified && budgetStillFits;
}
bool oracleScheduleCompatible(const std::string& profile,const std::string& backend,unsigned schemaVersion) {
    if(profile=="peak_transition_os_snapshot")
        return backend=="proc_file_writeback"?schemaVersion==3:schemaVersion==2;
    return profile=="legacy_two_donor" && backend=="force_shrink_all" && schemaVersion==1;
}

bool oracleAppMatchesDonor(const std::string& oracleProfile,const std::string& appName,
    const std::string& appLeaf,const std::string& bundle,const std::string& uid) {
    if(appLeaf!=bundle+"_"+uid) return false;
    return oracleProfile!="peak_transition_os_snapshot" || appName==bundle;
}

bool oracleProcFileWriteComplete(long writeLength,std::size_t payloadLength) {
    return writeLength>=0 && static_cast<std::uint64_t>(writeLength)==payloadLength;
}

bool oracleProcFileFeedbackMayContinue(bool identityVerified,
    const std::optional<Bytes>& preRssFileKiB,const std::optional<Bytes>& postRssFileKiB) {
    return identityVerified && preRssFileKiB && postRssFileKiB &&
        *preRssFileKiB>*postRssFileKiB &&
        *preRssFileKiB<=static_cast<Bytes>(std::numeric_limits<std::int64_t>::max()) &&
        *postRssFileKiB<=static_cast<Bytes>(std::numeric_limits<std::int64_t>::max());
}
bool oracleWritebackPostwriteRecheckEligible(unsigned schemaVersion,
    const std::string& profile,const std::string& backend,const std::string& freshnessFailure) {
    if(schemaVersion!=3 || profile!="peak_transition_os_snapshot" || backend!="proc_file_writeback") return false;
    return freshnessFailure=="aa_evidence_stale" || freshnessFailure=="window_evidence_stale" ||
        freshnessFailure=="proc_evidence_stale" || freshnessFailure=="lifecycle_evidence_stale";
}
bool oracleAnonPostwriteRecheckEligible(unsigned schemaVersion,
    const std::string& profile,const std::string& backend,const std::string& freshnessFailure) {
    if(schemaVersion!=2 || profile!="peak_transition_os_snapshot" || backend!="force_shrink_all") return false;
    return freshnessFailure=="aa_evidence_stale" || freshnessFailure=="window_evidence_stale" ||
        freshnessFailure=="proc_evidence_stale" || freshnessFailure=="lifecycle_evidence_stale";
}
bool oracleDiskPostwriteRecheckEligible(unsigned schemaVersion,
    const std::string& profile,const std::string& backend,const std::string& freshnessFailure) {
    if(backend!="force_swapout") return false;
    return oracleAnonPostwriteRecheckEligible(schemaVersion,profile,"force_shrink_all",freshnessFailure);
}
bool oracleWritebackPreProcRecheckMayResume(unsigned schemaVersion,
    const std::string& profile,const std::string& backend,const std::string& initialFailure,
    const std::string& finalFailure,bool registrationStable,bool controlStable,
    bool fileIdentityVerified,bool rssKeepFits,bool systemReservationFits,bool beforeDeadline) {
    return oracleWritebackPostwriteRecheckEligible(schemaVersion,profile,backend,initialFailure) &&
        finalFailure.empty() && registrationStable && controlStable && fileIdentityVerified &&
        rssKeepFits && systemReservationFits && beforeDeadline;
}
double oracleWritebackPostwriteRecheckRemaining(double startedBoottime,
    double nowBoottime,double eventDeadlineBoottime) {
    if(!std::isfinite(startedBoottime) || !std::isfinite(nowBoottime) ||
       !std::isfinite(eventDeadlineBoottime) || startedBoottime<=0 || nowBoottime<startedBoottime ||
       eventDeadlineBoottime<=nowBoottime) return 0;
    return std::max(0.0,std::min(startedBoottime+2.0,eventDeadlineBoottime)-nowBoottime);
}
bool oracleProcFileRecheckFeedbackMayContinue(bool identityVerified,bool systemSamplePresent,
    const std::optional<Bytes>& preRssFileKiB,const std::optional<Bytes>& postRssFileKiB,
    const std::optional<Bytes>& postRssTotalKiB,Bytes keepReserveBytes) {
    if(!identityVerified || !systemSamplePresent || !postRssTotalKiB || !postRssFileKiB ||
       *postRssTotalKiB<*postRssFileKiB || !oracleProcFileFeedbackMayContinue(true,preRssFileKiB,postRssFileKiB)) return false;
    const Bytes retainedKiB=*postRssTotalKiB-*postRssFileKiB;
    const Bytes keepKiB=keepReserveBytes/1024+(keepReserveBytes%1024?1:0);
    return retainedKiB>=keepKiB;
}
bool oracleProfileRunsFullObservation(bool profileEnabled,bool eventActive,bool observationDue) {
    return !profileEnabled || !eventActive || observationDue;
}
unsigned oracleLoopIntervalMs(bool profileEnabled,bool eventActive,unsigned profilePollMs,unsigned normalIntervalMs) {
    return profileEnabled && eventActive?profilePollMs:normalIntervalMs;
}
bool oracleFastPollWindow(bool device,double now,double launch) {
    return launch>now && launch-now<=(device?35.0:15.0);
}

bool oracleMayReserveAction(bool workerInFlight,double now,double lastSubmit,double minInterval,double launch) {
    return !workerInFlight && now<launch && now-lastSubmit>=minInterval;
}

int run(const Configuration& config,const std::string& backend,bool check,bool apply,Bytes ticks,const std::function<bool()>& stopping) {
    const auto& options=config.options;
    const bool ratioProfile=apply && !options.oracleMode;
    const bool probeProfile=backend=="harmony-v1-ratio-probe";
    const bool oracleProfile=apply && options.oracleMode;
    if(options.oracleMode && backend!="harmony-v1-ratio")
        throw std::runtime_error("oracle_mode requires --backend harmony-v1-ratio");
    if(options.oracleMode && apply && probeProfile)
        throw std::runtime_error("oracle mode does not permit the probe backend");
    if(options.dailyRecentReserve && probeProfile)
        throw std::runtime_error("daily_recent_reserve does not permit the isolated probe backend");
    if (options.proc != "/proc") throw std::runtime_error("harmony-v1 requires proc_root=/proc");
    if(ratioProfile || oracleProfile) {
        struct utsname kernel{};
        if(::uname(&kernel)!=0) throw error("uname");
        if(std::string(kernel.sysname)!="HarmonyOS" || std::string(kernel.release).find("HongMeng Kernel 1.13.0")==std::string::npos)
            throw std::runtime_error("ratio profile is qualified only for HarmonyOS HongMeng Kernel 1.13.0");
        if(options.root!="/dev/memcg" || options.control.empty() || options.requirePSI)
            throw std::runtime_error("ratio profile requires cgroup_root=/dev/memcg, a control_file, and require_psi=false (device PSI is unverified)");
        bool donor=false;
        for(const auto& app:config.apps) if(app.reclaim) {
            donor=true;
            if(probeProfile && (config.apps.size()!=1 || app.path.rfind("memshift_probe_",0)!=0 || app.path.find('/')!=std::string::npos))
                throw std::runtime_error("ratio-probe profile only permits exactly one direct memshift_probe_* target");
            if(!probeProfile && app.path.rfind("memshift_probe_",0)==0)
                throw std::runtime_error("probe cgroups require the separate harmony-v1-ratio-probe profile");
            if(!probeProfile && std::count(app.path.begin(),app.path.end(),'/')!=1)
                throw std::runtime_error("ratio apply allowlist targets must be one user/package cgroup level deep");
        }
        if(!donor) throw std::runtime_error("ratio profile requires at least one reclaim=true allowlisted app");
        if(oracleProfile) {
            std::size_t selfDonors=0;
            for(const auto& app:config.apps) if(app.reclaim) {
                const auto slash=app.path.find_last_of('/'); const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
                bool allowed=false;
                for(unsigned i=1;i<=(options.oracleProfile=="peak_transition_os_snapshot"?16U:2U);++i) {
                    const std::string bundle="io.memscene.scene4.workload.a"+std::to_string(i);
                    const bool appNameAllowed=options.oracleProfile!="peak_transition_os_snapshot" || app.name==bundle;
                    if(leaf.rfind(bundle+"_",0)==0 && appNameAllowed) allowed=true;
                }
                if(!allowed) throw std::runtime_error("oracle mode may reclaim only exact configured scene4 self-donor leaves");
                ++selfDonors;
            }
            if(selfDonors<1 || selfDonors>(options.oracleProfile=="peak_transition_os_snapshot"?16U:2U) ||
                (options.oracleProfile!="peak_transition_os_snapshot" && selfDonors!=2))
                throw std::runtime_error("oracle mode requires 2 legacy or 1..16 explicitly allowlisted profile donors");
            if(options.oracleProfile=="peak_transition_os_snapshot" && !options.oracleBackendExplicit)
                throw std::runtime_error("peak_transition_os_snapshot requires an explicit oracle_backend choice");
            if(isProcFileBackend(options.oracleBackend) && options.oracleProfile!="peak_transition_os_snapshot")
                throw std::runtime_error("proc_file backends are restricted to the explicit peak_transition_os_snapshot profile");
            if(isProcFileBackend(options.oracleBackend) && options.oracleRatioSemantics!="resident_incremental")
                throw std::runtime_error("proc_file backends do not use anonymous ratio/watermark semantics");
            if(options.oracleBackend=="force_swapout" && (options.oracleProfile!="peak_transition_os_snapshot" ||
                options.oracleRatioSemantics!="resident_incremental"))
                throw std::runtime_error("force_swapout is an explicit self-workload profile command, without ratio semantics");
            if(options.oracleDiskPrepareAnon && (options.oracleBackend!="force_swapout" ||
                options.oracleProfile!="peak_transition_os_snapshot"))
                throw std::runtime_error("disk preparation requires explicit self-workload force_swapout");
            if(options.oracleSchedule==options.oracleLedger) throw std::runtime_error("oracle schedule and action ledger must be separate files");
        }
    }
    for (const auto& app : config.apps)
        if (!safeRelative(app.path)) throw std::runtime_error("unsafe app path: " + app.name);
    if(oracleProfile && options.oracleBackend=="force_swapout") requireOracleDiskBackendEnabled();
    const RootInfo rootInfo=validateRoot(options);
    FD root(::open(options.root.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW));
    if(root.get()<0) throw error("open cgroup_root");
    const System initial=systemSample(options.proc);
    EWMAPredictor predictor;

    std::string controlState="not_configured";
    Control control;
    if(!options.control.empty()) {
        try { control=parseControl(readPath(options.control)); controlState=control.paused?"paused":"active"; }
        catch(const std::exception&) { controlState="unknown"; }
    }
    if(check) {
        std::cout << "backend=harmony-v1 mode=check schema=" << Schema
                  << " root=" << options.root << " mount=" << rootInfo.mountPoint
                  << " validated_via_alias=" << (rootInfo.alias?"true":"false")
                  << " statfs_magic_raw=" << rootInfo.magic
                  << " mem_total_bytes=" << initial.total << " mem_available_bytes=" << initial.available
                  << " pressure_policy=unknown control=" << controlState << '\n';
        int failed=0;
        for(const auto& app:config.apps) {
            try {
                FD dir=openBeneath(root.get(),app.path);
                const auto stat=maybeAt(dir.get(),"memory.stat");
                const auto current=maybeAt(dir.get(),"memory.current");
                if(!stat || !current) throw std::runtime_error("memory.stat or memory.current unreadable");
                const auto vendor=parseVendorStat(*stat);
                std::cout << "app=" << app.name << " path=" << app.path << " populated=" << appPopulated(dir.get())
                          << " memory_current_raw=" << trim(*current)
                          << " anon_kib=" << (vendor.anonKiB?std::to_string(*vendor.anonKiB):"unknown")
                          << " zram_kib=" << (vendor.zramKiB?std::to_string(*vendor.zramKiB):"unknown")
                          << " eswap_kib=" << (vendor.eswapKiB?std::to_string(*vendor.eswapKiB):"unknown")
                          << " candidate_budget=unknown reclaim=disabled\n";
            } catch(const std::exception& e) { ++failed; std::cout << "app="<<app.name<<" unavailable="<<e.what()<<'\n'; }
        }
        std::cout << (ratioProfile?"No reclaim was attempted by --check.\n":"No files were written; apply is unsupported for harmony-v1.\n");
        return failed?2:0;
    }

    FD lock(::open(options.lock.c_str(),O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0640));
    if(lock.get()<0) throw error("open lock_file (create its parent directory first)");
    if(::flock(lock.get(),LOCK_EX|LOCK_NB)!=0) throw error("another daemon holds lock_file");
    const fs::path logDir=fs::path(options.logs)/(oracleProfile?"harmony-v1-oracle-scene4":ratioProfile?(probeProfile?"harmony-v1-ratio-probe":"harmony-v1-ratio"):"harmony-v1-observe-v3");
    fs::create_directories(logDir);
    CSV systemLog(logDir/"system.csv",
        "unix_ms,schema,mem_total_bytes,mem_available_bytes,mem_free_bytes,swap_total_bytes,swap_free_bytes,predicted_available_bytes,decline_bytes_s,pressure_memory_raw,pressure_memory_status,pressure_io_raw,pressure_io_status,decision,candidate_budget_bytes,reclaim_action,reclaim_target",options.logMax);
    CSV appLog(logDir/"apps.csv",
        "unix_ms,schema,app,path,populated,memory_current_raw,memory_current_unit,anon_kib,zram_kib,eswap_kib,inactive_anon_raw,inactive_file_raw,anon_refault_raw,proc_count,proc_status_read,proc_smaps_read,process_rss_sum_kib,process_vmswap_sum_kib,process_rssanon_sum_kib,process_rssfile_sum_kib,process_pss_sum_kib,process_pss_anon_sum_kib,process_pss_file_sum_kib,process_private_sum_kib,process_smaps_swap_sum_kib,proc_fault_delta_covered,proc_minflt_delta,proc_majflt_delta,file_candidate_bytes,cold_anon_candidate_bytes,candidate_budget_bytes,reclaim_action,reason",options.logMax);
    CSV eventLog(logDir/"events.csv","unix_ms,schema,level,name,message",options.logMax);
    std::optional<CSV> headroomLog;
    if(oracleProfile) headroomLog.emplace(logDir/"headroom_v1.csv",
        "unix_ms,sample_boottime_s,memavailable_bytes,memfree_bytes,cached_bytes,dirty_bytes,writeback_bytes,anonpages_bytes,shmem_bytes,swapfree_bytes,oom_kill_cnt_known,oom_kill_cnt_raw",options.logMax);
    std::optional<CSV> observationTimingLog;
    if(oracleProfile) observationTimingLog.emplace(logDir/"observation_timing_v1.csv",
        "unix_ms,sample_boottime_s,full_observation_ms,smaps_elapsed_ms,smaps_deferred,proc_count,status_read,smaps_read,fault_delta_covered,smaps_rate_limited,smaps_cpu_ms",options.logMax);
    event(eventLog,"INFO","daemon",oracleProfile?"backend=harmony-v1-ratio; isolated scene4 oracle enabled; percent input is empirical and is not a physical byte guarantee":ratioProfile?(probeProfile?"backend=harmony-v1-ratio-probe; HongMeng 1.13.0 empirical 1..20 ratio; sole verified workload only; scalar is not bytes":"backend=harmony-v1-ratio; HongMeng 1.13.0 empirical ratio profile; allowlisted app_score and per-process oom_score_adj gates required"):
        "backend=harmony-v1; schema="+std::string(Schema)+"; observation only; no candidate budget or reclaim action");
    event(eventLog,"INFO","cgroup_root","mount="+rootInfo.mountPoint+" validated_via_alias="+(rootInfo.alias?"true":"false")+
          " statfs_magic_raw="+std::to_string(rootInfo.magic)+"; proc topology and controller files validated");
    event(eventLog,"INFO","schema","system.csv/apps.csv use harmony-v1-observe-3 schema and differ from Linux cgroup-v2 logs");
    if(controlState=="unknown") event(eventLog,"WARN","control","control file unavailable or invalid; control state unknown");

    std::vector<std::map<std::uint64_t,ProcStat>> previousProcesses(config.apps.size());
    std::uint64_t tick=0;
    double nextProbeAction=0; // Legacy synchronous ratio path only.
    daily::Schedule dailySchedule(config.apps.size());
    const auto recentStore=std::make_shared<daily::recent::Store>();
    std::optional<CSV> recentLog;
    if(options.dailyRecentReserve) recentLog.emplace(logDir/"daily_recent_reserve.csv",
        "sample_monotonic_s,signal_bytes,target_bytes,mem_free_bytes,expires_monotonic_s,epoch",options.logMax);
    RollingLimit globalProbeMinute;
    std::vector<RollingLimit> appProbeMinute(config.apps.size());
    const double daemonStart=monotonic();
    std::vector<Bytes> lastFeedbackAnonKiB(config.apps.size(),0);
    std::vector<Bytes> lastFeedbackFileKiB(config.apps.size(),0);
    std::vector<bool> haveFeedback(config.apps.size(),false);
    std::vector<bool> oracleFeedbackFileCategory(config.apps.size(),false);
    std::vector<bool> oracleFeedbackDonorCooldownOnly(config.apps.size(),false);
    bool actionThisTick=false;
    std::string tickDecision=ratioProfile?"ratio_profile_idle":"observation_only",tickTarget;
    Bytes tickBudget=0;
    auto deadline=std::chrono::steady_clock::now();
    auto nextFullObservation=std::chrono::steady_clock::now();
    auto nextSmapsObservation=nextFullObservation;
    bool oracleFastPollActive=false;
    int samplingErrors=0;
    std::shared_ptr<DeviceOracleEvidence> deviceEvidence;
    if(options.oracleProofSource=="device") {
        if(options.oracleProfile!="peak_transition_os_snapshot" || options.oraclePlan.empty() || options.oraclePlan==options.oracleLedger)
            throw std::runtime_error("device proof requires an explicit static plan and existing peak-transition profile");
        deviceEvidence=std::make_shared<DeviceOracleEvidence>(options.oraclePlan,options.oracleLedger);
    }

    // Event evidence is separate from oracle execution; oracleProfile remains false.
    std::shared_ptr<DeviceOracleEvidence> dailyKnownEvidence;
    if(options.dailyKnownFutureFreeOnly) {
        if(!ratioProfile || probeProfile || oracleProfile) throw std::runtime_error("daily known future is owned ratio-only");
        dailyKnownEvidence=std::make_shared<DeviceOracleEvidence>(options.dailyKnownPlan,options.dailyKnownLedger);
    }
    std::optional<CSV> dailyKnownLog;
    if(options.dailyKnownFutureFreeOnly) dailyKnownLog.emplace(logDir/"daily_known_future.csv",
        "sample_monotonic_s,event_id,plan_sha256,needed_bytes,target_bytes,mem_free_bytes,gap_bytes,T_boottime_s,reason",options.logMax);

    std::unique_ptr<daily::Executor> dailyExecutor;
    std::optional<CSV> dailyLedger;
    std::optional<CSV> dailyDiskPlans;
    std::string dailyGeneration;
    std::uint64_t dailySequence=0;
    if(ratioProfile && options.dailyAsync) {
        event(eventLog,"INFO","daily_psi_policy","Harmony system/app PSI unverified: raw diagnostics only; not threshold authorization or veto; require_psi=true unsupported; Linux/legacy/oracle policy unchanged");
        const auto self=static_cast<std::uint64_t>(::getpid());
        const auto generation=parseProcStat(self,readPath(options.proc+"/"+std::to_string(self)+"/stat")).startTicks;
        dailyGeneration=std::to_string(self)+"_"+std::to_string(generation)+"_"+std::to_string(unixMillis());
        dailyLedger.emplace(logDir/"daily_actions.csv",
            "unix_ms,action_id,app,group_path,group_device,group_inode,member_generations,sequence,phase,reserved_bytes,planned_ratio,submitted_ratio,nominal_bytes,write_attempted,write_length,errno,syscall_start_s,syscall_return_s,write_ms,pre_anon_bytes,post_anon_bytes,feedback_verified,status,charge_retained,backend,zram_before,zram_after,eswap_before,eswap_after,qualification,known_event_id,known_plan_sha256,known_needed_bytes,known_target_bytes,known_T_boottime_s,known_strength,paired_after,pair_feedback_anon_bytes,preparation_fault_safe,known_capacity,keep_bytes,known_lead_sec",options.logMax);
        dailyExecutor=std::make_unique<daily::Executor>(options.dailyMaxSubmissions);
        if(options.dailyDiskMode!="off") dailyDiskPlans.emplace(logDir/"daily_disk_plans.csv",
            "unix_ms,app,path,mode,zram_logical_bytes,rss_anon_bytes,whole_work_bytes,reservation_with_margin,explicit_limit,shared_room,fresh_sample_gap,swap_room,nominal_plan_admitted,device_window_lifecycle_qualification,reason",options.logMax);
    }
    const auto appendDaily=[&](const daily::Result& r,const std::string& phase) {
        if(!dailyLedger) return;
        const auto real=[](const std::optional<double>& x) { return x?std::to_string(*x):std::string{}; };
        std::string members;
        for(const auto& member:r.request.members) {
            if(!members.empty()) members+=';';
            members+=std::to_string(member.first)+":"+std::to_string(member.second);
        }
        dailyLedger->row({std::to_string(unixMillis()),r.request.actionId,r.request.app,r.request.groupPath,
            std::to_string(r.request.groupDevice),std::to_string(r.request.groupInode),members,std::to_string(r.request.sequence),phase,
            std::to_string(r.request.reservedBytes),std::to_string(r.request.plannedRatio),std::to_string(r.submittedRatio),
            std::to_string(r.nominalBytes),r.writeAttempted?(*r.writeAttempted?"true":"false"):"unknown",
            r.writeLength?std::to_string(*r.writeLength):std::string{},r.error?std::to_string(*r.error):std::string{},
            real(r.startedAt),real(r.returnedAt),real(r.writeMs),std::to_string(r.preAnonBytes),std::to_string(r.postAnonBytes),
            r.feedbackVerified?"true":"false",r.status,"true",r.request.backend,
            std::to_string(r.diskZramBefore),std::to_string(r.diskZramAfter),std::to_string(r.diskEswapBefore),std::to_string(r.diskEswapAfter),r.request.qualification,
            r.request.knownEvent?r.request.knownEvent->id:std::string{},
            r.request.knownEvent?r.request.knownEvent->planFingerprint:std::string{},
            r.request.knownEvent?std::to_string(r.request.knownEvent->needed):std::string{},
            r.request.knownEvent?std::to_string(daily::known::target(r.request.knownEvent->needed,options.reserve,initial.total)):std::string{},
            r.request.knownEvent?std::to_string(r.request.knownEvent->launch):std::string{},
            r.request.knownStrength?"true":"false",r.request.pairedAfter,std::to_string(r.request.pairPostAnon),r.preparationFaultSafe?"true":"false",r.request.knownCapacity?"true":"false",std::to_string(r.request.keepBytes),std::to_string(r.request.knownLeadSec)});
    };
    daily::PairedDisk dailyPair;
    const auto acceptDaily=[&](const daily::Result& r) {
        appendDaily(r,"completion");
        if(r.feedbackVerified && !r.uncertain && r.request.appIndex<haveFeedback.size()) {
            lastFeedbackAnonKiB[r.request.appIndex]=r.postAnonBytes/1024;
            haveFeedback[r.request.appIndex]=true;
        }
        dailySchedule.complete(r,monotonic(),options.cooldownSec,options.dailyKnownStrength);
        dailyPair.completed(r,options.dailyKnownStrength);
        if(options.dailyKnownStrength && r.request.knownStrength && r.request.backend=="force_shrink_anon" &&
           daily::verifiedWrite(r) && !r.preparationFaultSafe)
            dailySchedule.defer(r.request.appIndex,monotonic(),options.backoffSec);
        event(eventLog,r.feedbackVerified && !r.uncertain?"INFO":"WARN",r.request.app,
            "daily action_id="+r.request.actionId+" status="+r.status+" charge_retained=true"+
            (r.qualificationDiagnostic.empty()?std::string{}:" qualification_diagnostic="+r.qualificationDiagnostic)+
            (r.diskBudgetDiagnostic.empty()?std::string{}:" disk_budget="+r.diskBudgetDiagnostic));
    };

    std::optional<std::future<OracleActionResult>> oracleAction;
    std::map<std::string,std::string> oracleFingerprints;
    std::set<std::string> oracleHaltedEvents, oracleExpiredEvents, oracleUnknownDeadlineRows;
    std::set<std::string> oracleSubmittedTargets;
    std::set<std::pair<std::string,std::string>> oracleFloorDisabledTargets;
    std::map<std::string,unsigned> oracleNextDonor;
    std::map<std::string,unsigned> oraclePositiveRatioByEventDonor;
    std::set<std::string> oracleDiskPreparedTargets;
    std::set<std::string> oracleDiskSkipLogged;
    std::map<std::string,std::string> oracleDiskPriorityActionIds;
    std::map<std::string,std::string> oracleDiskPrioritySnapshotSignatureByEvent;
    std::set<std::string> oracleStructuralDeadlineLogged;
    std::map<std::string,double> oracleDonorBlockedUntil;
    std::map<std::string,double> oraclePrecommitMemberWaitStart;
    std::map<std::string,std::string> oracleActiveEventForDonor;
    std::map<std::string,std::pair<std::uint64_t,std::uint64_t>> oracleActiveIdentityForDonor;
    RollingLimit oracleGlobalLimit;
    std::map<std::string,RollingLimit> oracleCaseLimits,oracleAppLimits;
    double lastOracleSubmit=0;
    double oracleActionDeadline=0;
    std::string oracleActionEvent,oracleActionId;
    auto logOracleDiskSkipOnce=[&](const std::string& eventId,const std::string& bundle,
        const std::string& reason,const std::string& detail) {
        const std::string key=eventId+"|"+bundle+"|"+reason;
        if(oracleDiskSkipLogged.insert(key).second)
            event(eventLog,"INFO","oracle",bundle+" disk_candidate_skip reason="+reason+
                " event_id="+eventId+" "+detail);
    };
    while(!stopping() && (!ticks || tick<ticks)) {
        const auto iterationStart=std::chrono::steady_clock::now();
        const bool fullObservationTick=oracleProfileRunsFullObservation(
            options.oracleProfile=="peak_transition_os_snapshot",oracleFastPollActive,
            iterationStart>=nextFullObservation);

        if(dailyExecutor) {
            if(const auto result=dailyExecutor->poll()) acceptDaily(*result);
            if(const auto unknown=dailyExecutor->timeout(monotonic(),60.0)) {
                appendDaily(*unknown,"timeout");
                dailySchedule.halt();
                event(eventLog,"ERROR","daily","pending timeout; admission halted; worker retained and joined on exit; kernel write is not interrupted");
            }
        }

        if(oracleAction && oracleAction->wait_for(std::chrono::milliseconds(0))==std::future_status::ready) {
            try { const auto result=oracleAction->get(); appendOracleLedger(options,result); event(eventLog,"INFO",result.bundle,"oracle action_id="+result.actionId+" status="+result.status+" nominal_bytes="+std::to_string(result.nominal)+" overlap="+(result.overlap?"true":"false"));
                if(options.oracleProfile=="peak_transition_os_snapshot" && (isProcFileBackend(result.backend) || result.backend=="force_swapout") && (result.writeAttempted || result.writebackAttempted))
                    oracleSubmittedTargets.insert(result.eventId+"|"+result.bundle);
                if(result.isolateFloorDonor && result.status=="write_accepted_postwrite_identity_or_background_failure:pipeline_process_keep_verified_below_floor" && result.writeAttempted && result.errorNo==0 && result.feedbackIdentityVerified && !result.overlap && result.feedbackOverlapsDeadline && !*result.feedbackOverlapsDeadline) {
                    oracleFloorDisabledTargets.insert({result.eventId,result.bundle});
                    event(eventLog,"WARN",result.bundle,"postwrite disposition=disable_donor_for_event; charge_retained; not_success; "+result.floorIsolationEvidence);
                } else if((result.backend=="force_swapout" && result.status=="write_accepted_disk_swap_transition_observed" && result.feedbackIdentityVerified) || oracleFeedbackMayContinue(result.status,result.feedbackIdentityVerified,
                       isProcFileBackend(result.backend)?result.signedRssFileDeltaKiB:result.signedRssDeltaKiB,
                       isProcFileBackend(result.backend)?result.fileFeedbackPresent:result.postRssAnonKiB!=0)) {
                    const std::string ratioKey=result.eventId+"|"+result.bundle;
                    if(options.oracleDiskPrepareAnon && result.backend=="force_shrink_all") {
                        if(!oracleDiskPreparationComplete(result.writeAttempted,result.status,
                            result.feedbackIdentityVerified,result.signedRssDeltaKiB,result.overlap) ||
                            result.postRssAnonKiB<options.oracleCumulativeKeep/1024 ||
                            result.postRssTotalKiB<oracleDeviceDonorTotalKeepFloor(options,result.bundle,0)/1024 || !oraclePreparationFeedbackWithinReservation(result.preparationWorkMode,result.feedbackDrop,result.nominal)) {
                            oracleHaltedEvents.insert(result.eventId);
                        } else {
                            oracleDiskPreparedTargets.insert(ratioKey);
                            // Planned second stage, never a fallback after an unknown write.
                            const auto latest=getCurrentOracleRequest(options,deviceEvidence);
                            for(std::size_t i=0;i<latest.donors.size();++i)
                                if(latest.id==result.eventId && latest.donors[i].bundle==result.bundle)
                                    oracleNextDonor[result.eventId]=static_cast<unsigned>(i);
                        }
                    }
                    if(!isProcFileBackend(result.backend) && result.backend!="force_swapout") {
                        auto& priorRatio=oraclePositiveRatioByEventDonor[ratioKey];
                        priorRatio=std::max(priorRatio,result.ratio);
                    }
                    for(std::size_t i=0;i<config.apps.size();++i) {
                        const auto slash=config.apps[i].path.find_last_of('/');
                        const auto leaf=slash==std::string::npos?config.apps[i].path:config.apps[i].path.substr(slash+1);
                        if(leaf!=result.bundle+"_"+result.uid) continue;
                        lastFeedbackAnonKiB[i]=result.postRssAnonKiB;
                        lastFeedbackFileKiB[i]=result.postRssFileKiB;
                        oracleFeedbackFileCategory[i]=isProcFileBackend(result.backend);
                        oracleFeedbackDonorCooldownOnly[i]=oracleProfileCooldownMayContinue(
                            result.schemaVersion,options.oracleProfile,result.backend,result.feedbackIdentityVerified);
                        haveFeedback[i]=true; oracleActiveEventForDonor[config.apps[i].name]=result.eventId; oracleActiveIdentityForDonor[config.apps[i].name]={result.pid,result.start}; break;
                    }
                } else if(result.backend=="force_swapout" && !result.writeAttempted &&
                    (result.status=="disk_pool_changed_outside_reserved_estimate_no_write" ||
                     result.status=="disk_pool_below_minimum_no_write") && !result.overlap) {
                    oracleSubmittedTargets.insert(result.eventId+"|"+result.bundle);
                    event(eventLog,"WARN",result.bundle,"disk pool changed before submission; no write; charge retained; skip this target");
                } else if(oracleUnsubmittedEvidenceMayDefer(result.status,
                    result.writeAttempted,result.writebackAttempted,result.overlap)) {
                    for(const auto& app:config.apps) {
                        const auto slash=app.path.find_last_of('/');
                        const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
                        if(leaf==result.bundle+"_"+result.uid) { oracleDonorBlockedUntil[app.name]=monotonic()+0.5; break; }
                    }
                    try { const auto request=getCurrentOracleRequest(options,deviceEvidence);
                        for(std::size_t i=0;i<request.donors.size();++i) if(result.bundle==request.donors[i].bundle) {
                            oracleNextDonor[result.eventId]=static_cast<unsigned>((i+1)%request.donors.size()); break;
                        }
                    } catch(...) { oracleHaltedEvents.insert(result.eventId); }
                    event(eventLog,"WARN",result.bundle,"oracle action deferred without write: "+result.status);
                } else oracleHaltedEvents.insert(result.eventId);
                if(result.overlap) oracleHaltedEvents.insert(result.eventId);
            }
            catch(const std::exception& e) { event(eventLog,"ERROR","oracle_ledger",e.what()); oracleHaltedEvents.insert(oracleActionEvent); ++samplingErrors; }
            oracleAction.reset();
        }
        // 若异步 Oracle 动作跨过事件截止时间仍未返回，记为结果未知并停止该事件继续调度。
        if(oracleAction && monotonic()>=oracleActionDeadline && !oracleUnknownDeadlineRows.count(oracleActionId)) {
            OracleActionResult r; r.actionId=oracleActionId; r.eventId=oracleActionEvent; r.deadline=oracleActionDeadline; r.backend=options.oracleBackend; r.status="reserved_action_still_inflight_at_deadline_unknown"; r.overlap=true; appendOracleLedger(options,r); oracleUnknownDeadlineRows.insert(oracleActionId); oracleHaltedEvents.insert(oracleActionEvent);
        }
        if(oracleAction) oracleFastPollActive=true;
        else if(oracleHaltedEvents.count(oracleActionEvent)) oracleFastPollActive=false;
        // tick 是已进入的循环轮数；--ticks N 时达到 N 轮后在下一次条件检查退出。
        ++tick;

//lzx-note 测试逻辑end===================================================================================================================================================================================================================================================================


    //lzx-note 常驻逻辑--运行时控制开关 begin===================================================================================================================================================================================================================================================================

        // 每轮刷新运行时控制（暂停、前台保护、临时 boost）；状态未知时后续写入会被关闸。
        // 先判断是否配置了控制文件路径，控制文件是用来给服务运行期间动态暂停或保护应用的。
        if(!options.control.empty()) {  //lzx-note-normal
            const std::string previousControlState=controlState;
            try {
                //读取控制文本，决定是否暂停，哪些应用需要保护，以及是否临时提高内存预留目标
                control=parseControl(readPath(options.control));  //lzx-note-normal
                controlState=control.paused?"paused":"active";  //lzx-note-normal
            } catch(const std::exception&) {
                control=Control{}; controlState="unknown";  //lzx-note-normal
            }
            if(controlState!=previousControlState)
                event(eventLog,"INFO","control","control state changed to "+controlState+"; sampling continues; writes gated by control");  //lzx-note-normal
        }
        System sys;
        try { sys=systemSample(options.proc); }
        catch(const std::exception& e) { recentStore->invalidate(); ++samplingErrors; event(eventLog,"ERROR","system",e.what()); break; }
        const Forecast forecast=predictor.update(sys,options.horizonSec,options.maxDeclineMBps*MiB);
        tickDecision=ratioProfile?"ratio_profile_idle":"observation_only"; tickTarget.clear(); tickBudget=0;
        actionThisTick=false;
        Bytes controlBoost=0;
        if(ratioProfile && controlState=="active" && control.until>unixSeconds()) controlBoost=control.boost;
        const bool recentEnabled=ratioProfile && options.dailyAsync && !probeProfile && options.dailyRecentReserve;
        const auto recentSnapshot=recentEnabled?recentStore->update(options,sys):daily::recent::Snapshot{};
        const Bytes recentTarget=recentEnabled?daily::recent::targetAt(options,recentSnapshot,sys.time,sys.total):options.readyFree;
        Bytes systemGap=recentEnabled?std::max(deficit(options,sys,forecast,controlBoost),sub(recentTarget,sys.free)):
            deficit(options,sys,forecast,controlBoost);
        std::optional<OracleRequest> dailyKnownSnapshot;
        if(options.dailyKnownFutureFreeOnly) {
            std::string why="registered_event_free_gap";systemGap=0;
            try {
                auto e=dailyKnownEvidence->current("daily_sample");
                systemGap=daily::known::gap(e,options.reserve,sys,monotonic(),options.dailyKnownLeadSec);
                dailyKnownSnapshot=std::move(e);
            } catch(const std::exception& e) {why=e.what();}
            dailyKnownLog->row({std::to_string(sys.time),dailyKnownSnapshot?dailyKnownSnapshot->id:std::string{},
                dailyKnownSnapshot?dailyKnownSnapshot->planFingerprint:std::string{},
                dailyKnownSnapshot?std::to_string(dailyKnownSnapshot->needed):std::string{},
                dailyKnownSnapshot?std::to_string(daily::known::target(dailyKnownSnapshot->needed,options.reserve,sys.total)):std::string{},
                std::to_string(sys.free),std::to_string(systemGap),dailyKnownSnapshot?std::to_string(dailyKnownSnapshot->launch):std::string{},why});
        }
        if(recentEnabled) recentLog->row({std::to_string(sys.time),std::to_string(recentSnapshot.signal),
            std::to_string(recentTarget),std::to_string(sys.free),std::to_string(recentSnapshot.expiresAt),
            std::to_string(recentSnapshot.epoch)});
        bool hasMemPsi=false,hasIoPsi=false;
        std::string memoryPsi,ioPsi;
        // 读取PSI
        if(fullObservationTick) {
            memoryPsi=rawPSI(options.proc+"/pressure/memory",hasMemPsi);
            ioPsi=rawPSI(options.proc+"/pressure/io",hasIoPsi);
        }
        // Detailed PSS walks are diagnostics only. Keep status/stat sampling
        // and all feedback/protection gates active while deferring these reads.
        const bool deferSmaps=options.oracleDeferSmaps && oracleProfile &&
            options.oracleProfile=="peak_transition_os_snapshot" && (oracleFastPollActive || options.oracleProofSource=="device");
        const bool rateLimitSmaps=options.harmonySmapsIntervalMs!=0 &&
            std::chrono::steady_clock::now()<nextSmapsObservation;
        const bool readSmaps=!deferSmaps && !rateLimitSmaps;
        const auto observationStart=std::chrono::steady_clock::now();
        double observationSmapsMs=0;
        std::optional<double> observationSmapsCpuMs=0.0;
        std::size_t observationProcCount=0,observationStatusRead=0,observationSmapsRead=0,observationFaultCoverage=0;
        // Freeze the start for this tick: submissions advance only the next tick.
        const bool dailyScan=ratioProfile && options.dailyAsync;
        if(options.dailyKnownStrength) dailyPair.refresh(dailyKnownSnapshot,monotonic());
        const auto dailyScanStart=options.dailyKnownStrength?dailyPair.start(dailySchedule.scanStart()):dailySchedule.scanStart();
        const double dailyScanAdmissionAt=options.dailyKnownStrength?monotonic():0.0;
        if(fullObservationTick) for(std::size_t appPosition=0;appPosition<config.apps.size();++appPosition) {
            const auto appIndex=daily::scanIndex(appPosition,config.apps.size(),dailyScanStart,dailyScan);
            const auto& app=config.apps[appIndex];
            try {
                FD dir=openBeneath(root.get(),app.path);
                const auto current=optionalRaw(dir.get(),"memory.current");
                const auto stat=maybeAt(dir.get(),"memory.stat");
                if(current.empty() || !stat) throw std::runtime_error("memory.current or memory.stat unreadable");
                const auto values=parseVendorStat(*stat);
                const auto ids=processIds(dir.get());
                std::map<std::uint64_t,ProcStat> currentProcesses;
                const auto processes=sampleProcesses(ids,previousProcesses[appIndex],currentProcesses,readSmaps);
                observationSmapsMs+=processes.smapsElapsedMs;
                if(observationSmapsCpuMs && processes.smapsCpuMs) *observationSmapsCpuMs+=*processes.smapsCpuMs;
                else observationSmapsCpuMs.reset();
                observationProcCount+=processes.listed; observationStatusRead+=processes.statusRead;
                observationSmapsRead+=processes.smapsRead; observationFaultCoverage+=processes.deltaMatched;
                previousProcesses[appIndex]=currentProcesses;
            const Bytes residentAnonKiB=processes.rssAnonKiB.value_or(0);
            if(oracleProfile && app.reclaim && haveFeedback[appIndex]) {
                const auto eventIt=oracleActiveEventForDonor.find(app.name);
                const auto identityIt=oracleActiveIdentityForDonor.find(app.name);
                bool identityOk=processes.rssAnonKiB.has_value() && processes.listed==1 && processes.statusRead==1 && identityIt!=oracleActiveIdentityForDonor.end();
                if(oracleFeedbackFileCategory[appIndex]) identityOk=identityOk && processes.rssFileKiB.has_value();
                if(identityOk) { const auto p=currentProcesses.find(identityIt->second.first); identityOk=p!=currentProcesses.end() && p->second.startTicks==identityIt->second.second; }
                if(!identityOk) {
                    if(eventIt!=oracleActiveEventForDonor.end()) oracleHaltedEvents.insert(eventIt->second);
                    oracleDonorBlockedUntil.erase(app.name);
                    haveFeedback[appIndex]=false;
                    oracleActiveEventForDonor.erase(app.name);
                    oracleActiveIdentityForDonor.erase(app.name);
                    oracleFeedbackDonorCooldownOnly[appIndex]=false;
                    event(eventLog,"WARN",app.name,"oracle donor identity or RSS telemetry changed/missing; event stopped");
                } else if(oracleFeedbackFileCategory[appIndex] && *processes.rssFileKiB>lastFeedbackFileKiB[appIndex]+1024) {
                    const bool donorOnly=oracleFeedbackDonorCooldownOnly[appIndex];
                    const double cooldownUntil=monotonic()+options.backoffSec;
                    if(!donorOnly && eventIt!=oracleActiveEventForDonor.end()) oracleHaltedEvents.insert(eventIt->second);
                    oracleDonorBlockedUntil[app.name]=cooldownUntil;
                    haveFeedback[appIndex]=false;
                    oracleFeedbackDonorCooldownOnly[appIndex]=false;
                    if(donorOnly) {
                        event(eventLog,"WARN",app.name,"feedback_donor_cooldown reason=whole_process_rssfile_recovery event_action=continue observation_scope=whole_process rss_file_kib="+std::to_string(*processes.rssFileKiB)+" prior_feedback_rss_file_kib="+std::to_string(lastFeedbackFileKiB[appIndex])+" threshold_kib=1024 target_inode_attribution=none feedback_meaning=process_rss_observation boot_s="+std::to_string(cooldownUntil-options.backoffSec)+" cooldown_s="+std::to_string(options.backoffSec));
                        oracleActiveEventForDonor.erase(app.name); oracleActiveIdentityForDonor.erase(app.name);
                    } else event(eventLog,"WARN",app.name,"oracle file-RSS recovery exceeded 1 MiB; event stopped and donor cooled down");
                } else if(residentAnonKiB>lastFeedbackAnonKiB[appIndex]+1024) {
                    const bool donorOnly=oracleFeedbackDonorCooldownOnly[appIndex];
                    const double cooldownUntil=monotonic()+options.backoffSec;
                    if(!donorOnly && eventIt!=oracleActiveEventForDonor.end()) oracleHaltedEvents.insert(eventIt->second);
                    oracleDonorBlockedUntil[app.name]=cooldownUntil;
                    haveFeedback[appIndex]=false;
                    oracleFeedbackDonorCooldownOnly[appIndex]=false;
                    if(donorOnly) {
                        event(eventLog,"WARN",app.name,"feedback_donor_cooldown reason=whole_process_rssanon_recovery event_action=continue observation_scope=whole_process rss_anon_kib="+std::to_string(residentAnonKiB)+" prior_feedback_rss_anon_kib="+std::to_string(lastFeedbackAnonKiB[appIndex])+" threshold_kib=1024 rss_file_kib="+std::to_string(processes.rssFileKiB.value_or(0))+" target_inode_attribution=none feedback_meaning=process_rss_observation boot_s="+std::to_string(cooldownUntil-options.backoffSec)+" cooldown_s="+std::to_string(options.backoffSec));
                        oracleActiveEventForDonor.erase(app.name); oracleActiveIdentityForDonor.erase(app.name);
                    } else {
                        event(eventLog,"WARN",app.name,"oracle post-action resident anon recovered by >1MiB; donor cooldown");
                    }
                } else if(processes.majfltDelta>0 || processes.minfltDelta>1024) {
                    const bool donorOnly=oracleFeedbackDonorCooldownOnly[appIndex];
                    const double cooldownUntil=monotonic()+options.backoffSec;
                    if(!donorOnly && oracleFeedbackFileCategory[appIndex] && eventIt!=oracleActiveEventForDonor.end())
                        oracleHaltedEvents.insert(eventIt->second);
                    oracleDonorBlockedUntil[app.name]=cooldownUntil;
                    haveFeedback[appIndex]=false;
                    oracleFeedbackDonorCooldownOnly[appIndex]=false;
                    if(donorOnly) {
                        event(eventLog,"WARN",app.name,"feedback_donor_cooldown reason=whole_process_fault_feedback event_action=continue observation_scope=whole_process major_fault_delta="+std::to_string(processes.majfltDelta)+" minor_fault_delta="+std::to_string(processes.minfltDelta)+" threshold_major_gt=0 threshold_minor_gt=1024 rss_anon_kib="+std::to_string(residentAnonKiB)+" rss_file_kib="+std::to_string(processes.rssFileKiB.value_or(0))+" target_inode_attribution=none feedback_meaning=process_fault_observation boot_s="+std::to_string(cooldownUntil-options.backoffSec)+" cooldown_s="+std::to_string(options.backoffSec));
                        oracleActiveEventForDonor.erase(app.name); oracleActiveIdentityForDonor.erase(app.name);
                    } else event(eventLog,"WARN",app.name,oracleFeedbackFileCategory[appIndex]?
                        "oracle post-file-action fault guard; event stopped and donor cooled down":
                        "oracle post-action major/minor fault guard; donor cooldown");
                }
            }
            if(ratioProfile && haveFeedback[appIndex] && residentAnonKiB>lastFeedbackAnonKiB[appIndex]+1024) {
                if(options.dailyAsync) {dailySchedule.defer(appIndex,monotonic(),options.backoffSec);if(dailyPair.isTarget(appIndex)) dailyPair.cancel();}
                else nextProbeAction=std::max(nextProbeAction,monotonic()+options.backoffSec);
                event(eventLog,"WARN",app.name,"resident anonymous RSS recovered by >1 MiB after reclaim; cooldown/backoff active");
                haveFeedback[appIndex]=false;
            }
            if(ratioProfile && haveFeedback[appIndex] && (processes.majfltDelta>0 || processes.minfltDelta>1024)) {
                if(options.dailyAsync) {dailySchedule.defer(appIndex,monotonic(),options.backoffSec);if(dailyPair.isTarget(appIndex)) dailyPair.cancel();}
                else nextProbeAction=std::max(nextProbeAction,monotonic()+options.backoffSec);
                event(eventLog,"WARN",app.name,"post-reclaim process fault activity observed; cooldown/backoff active");
                haveFeedback[appIndex]=false;
            }
            if(dailyPair.isTarget(appIndex) && (controlState!="active" || !app.reclaim ||
               daily::disk::targetProtected(options,app.name,app.path,control.protectedApps) ||
               !processes.rssAnonKiB || !processes.vmSwapKiB || processes.listed!=1 || processes.statusRead!=1 || !systemGap))
                dailyPair.cancel();
            if(dailyPair.isTarget(appIndex)) {
                struct stat group{};std::map<std::uint64_t,std::uint64_t> members;
                for(const auto& p:currentProcesses) members[p.first]=p.second.startTicks;
                if(::fstat(dir.get(),&group)!=0 || !dailyPair.identityMatches(app.path,
                   static_cast<std::uint64_t>(group.st_dev),static_cast<std::uint64_t>(group.st_ino),members)) dailyPair.cancel();
            }
            // A planning-only immutable authority; the worker independently rechecks fresh proof, Free and T.
            daily::Request capacityScope;
            if(options.dailyKnownCapacity) {
                capacityScope.knownLeadSec=options.dailyKnownLeadSec;capacityScope.knownCapacity=true;capacityScope.knownStrength=options.dailyKnownStrength;
                capacityScope.groupPath=app.path;capacityScope.qualification="owned_os_snapshot";
                capacityScope.keepBytes=std::max(app.keep,128*MiB);capacityScope.minBatch=options.minBatch;
                capacityScope.reservedAt=monotonic();capacityScope.knownEvent=dailyKnownSnapshot;
                if(dailyKnownSnapshot) capacityScope.hardDeadline=dailyKnownSnapshot->launch;
                struct stat group{};
                if(::fstat(dir.get(),&group)==0) {
                    capacityScope.groupDevice=static_cast<std::uint64_t>(group.st_dev);
                    capacityScope.groupInode=static_cast<std::uint64_t>(group.st_ino);
                }
                for(const auto& p:currentProcesses) capacityScope.members[p.first]=p.second.startTicks;
            }
            const bool capacity=daily::capacityOptions(options) && daily::capacityAuthority(capacityScope) &&
                !probeProfile && daily::disk::ownedQualification(options,app.path,ids.size(),probeProfile);
            const Bytes dailyKeep=daily::policyKeep(app.keep,probeProfile,capacity?&capacityScope:nullptr);
            const bool pairedForApp=options.dailyKnownStrength && dailyPair.isTarget(appIndex);
            std::string reason=controlState=="unknown"?"control state unknown; observation only":
                    controlState=="paused"?"manual pause; observation only":
                    control.protectedApps.count(app.name)?"runtime protected; observation only":
                    "process counters are diagnostics only; coldness/reclaim units unverified; budget unavailable";
            std::string action="none",budgetText,ratioText;
            if(ratioProfile) {
                if(dailyDiskPlans) {
                    const auto pool=parseOracleDiskPool(*stat);
                    const Bytes rss=processes.rssAnonKiB && *processes.rssAnonKiB<=Unlimited/1024?*processes.rssAnonKiB*1024:0;
                    const Bytes room=std::min(sub(options.perMinute,globalProbeMinute.used(monotonic())),
                        sub(options.appPerMinute,appProbeMinute[appIndex].used(monotonic())));
                    const auto p=daily::disk::plan(app.path,pool?pool->zramBytes:0,rss,dailyKeep,
                        options.minBatch,options.dailyDiskWholeWork,room,systemGap,sub(sys.swapFree,options.swapReserve),true,capacity?&capacityScope:nullptr);
                    dailyDiskPlans->row({std::to_string(unixMillis()),app.name,app.path,options.dailyDiskMode,
                        std::to_string(pool?pool->zramBytes:0),std::to_string(rss),std::to_string(p.wholeWork),std::to_string(p.reservation),
                        std::to_string(options.dailyDiskWholeWork),std::to_string(room),std::to_string(systemGap),
                        std::to_string(sub(sys.swapFree,options.swapReserve)),p.admitted?"true":"false","unknown_not_sampled_on_sampling_thread",p.reason});
                }
                reason=app.reclaim?"ratio profile awaiting EWMA warmup":"app is not explicitly reclaim-enabled";
                if(controlState!="active") reason="control is not confirmed active";
                else if(!app.reclaim) reason="app is not explicitly reclaim-enabled";
                else if(daily::disk::targetProtected(options,app.name,app.path,control.protectedApps)) reason="app protected by runtime control";
                else if(!processes.rssAnonKiB || !processes.vmSwapKiB || processes.listed==0 || processes.statusRead!=processes.listed) reason="process telemetry incomplete";
                else if(tick<2 || monotonic()-daemonStart<options.warmupSec) reason="EWMA/startup warmup";
                else if(options.dailyAsync && !(pairedForApp?
                    dailySchedule.pairedEligible(appIndex,daily::scanQualificationTime(options.dailyKnownStrength,dailyScanAdmissionAt,monotonic())):
                    dailySchedule.eligible(appIndex,daily::scanQualificationTime(options.dailyKnownStrength,dailyScanAdmissionAt,monotonic()))))
                    reason="daily app cooldown, global completion spacing, or unknown halt";
                else if(!options.dailyAsync && monotonic()<nextProbeAction) reason="probe cooldown";
                else if(options.dailyAsync && dailyExecutor->busy()) reason="daily worker pending, halted, or lifetime submission limit reached";
                else if(options.dailyAsync && (!options.allowAnon || options.swappiness==0)) reason="anonymous reclaim disabled";
                else if(options.dailyAsync && !daily::harmonyUnverifiedPsiBlock(options,sys).empty()) reason="daily Harmony PSI policy rejected";
                else if(actionThisTick) reason="one reclaim action per interval";
                else if(appProbeMinute[appIndex].used(monotonic())>=options.appPerMinute ||
                        globalProbeMinute.used(monotonic())>=options.perMinute) reason="ratio profile per-minute cap reached";
                else if(systemGap==0) {
                    if(!probeProfile && !app.wpsWindowGate && !backgroundEligible(dir.get(),app,ids,currentProcesses))
                        reason="protected: app_score/oom_score_adj/member identity does not confirm background eligibility";
                    else reason="forecast reserve satisfied";
                }
                else {  //lzx-note-normal
                    try {
                        if(options.dailyKnownCapacity && !capacity) throw std::runtime_error("known_capacity_sample_authority");
                        std::optional<ProbeIdentity> probeIdentity;
                        if(probeProfile) probeIdentity=validateProbe(dir.get(),options.root,app.path);
                        else if(!daily::disk::ownedQualification(options,app.path,ids.size(),probeProfile) &&
                                !backgroundEligible(dir.get(),app,ids,currentProcesses))
                            throw std::runtime_error("app_score/oom_score_adj/member identity does not confirm background eligibility");
                        if(!values.anonKiB) throw std::runtime_error("vendor Anon telemetry unavailable");
                        if(!probeProfile && daily::disk::diskApplyEnabled(options,app.path,ids.size(),probeProfile)) {
                            const auto pool=parseOracleDiskPool(*stat);
                            const Bytes room=std::min(sub(options.perMinute,globalProbeMinute.used(monotonic())),
                                sub(options.appPerMinute,appProbeMinute[appIndex].used(monotonic())));
                            const auto plan=daily::disk::plan(app.path,pool?pool->zramBytes:0,*processes.rssAnonKiB*1024,
                                dailyKeep,options.minBatch,options.dailyDiskWholeWork,room,
                                systemGap,sub(sys.swapFree,options.swapReserve),true,capacity?&capacityScope:nullptr);
                            if(plan.admitted) {
                                struct stat group{};if(::fstat(dir.get(),&group)!=0) throw error("daily disk reservation group");
                                daily::Request request;request.sequence=++dailySequence;
                                request.actionId=dailyGeneration+"_daily_"+std::to_string(request.sequence);
                                request.app=app.name;request.groupPath=app.path;request.appIndex=appIndex;request.backend="force_swapout";
                                request.qualification="owned_os_snapshot";
                                request.groupDevice=static_cast<std::uint64_t>(group.st_dev);request.groupInode=static_cast<std::uint64_t>(group.st_ino);
                                request.members.emplace(ids.front(),currentProcesses.at(ids.front()).startTicks);
                                request.reservedBytes=plan.reservation;request.keepBytes=dailyKeep;request.minBatch=options.minBatch;
                                request.reservedAt=monotonic();request.maxAgeSeconds=2.5;request.forecastSampledAt=sys.time;
                                request.forecastMaxAgeSeconds=std::max(1.0,options.horizonSec);
                                request.recentTarget=recentSnapshot;
                                if(options.dailyKnownFutureFreeOnly) {
                                    if(!dailyKnownSnapshot || !daily::known::memberMatches(*dailyKnownSnapshot,request.groupPath,request.members))
                                        throw std::runtime_error("daily known event registered member mismatch");
                                    request.knownEvent=dailyKnownSnapshot;request.hardDeadline=dailyKnownSnapshot->launch;request.knownStrength=options.dailyKnownStrength;request.knownCapacity=options.dailyKnownCapacity;request.knownLeadSec=options.dailyKnownLeadSec;
                                }
                                if(!daily::capacityAdapterAllows(options,request)) throw std::runtime_error("known_capacity_reservation_authority");
                                if(pairedForApp && !dailyPair.bind(request)) throw std::runtime_error("paired registration or group identity changed");
                                if(pairedForApp) dailyPair.cancel(); // One attempt, including any later worker rejection.
                                const auto job=[options,app,request,expected=currentProcesses,forecast,stopping,recentStore,dailyKnownEvidence](const std::atomic<bool>& cancelled) {
                                    return performDailyDiskAction(options,app,request,expected,forecast,stopping,cancelled,recentStore,dailyKnownEvidence);
                                };
                                if(dailyExecutor->submit(request,job,[&] {
                                    appProbeMinute[appIndex].charge(request.reservedAt,request.reservedBytes);
                                    globalProbeMinute.charge(request.reservedAt,request.reservedBytes);
                                    daily::Result reserved;reserved.request=request;reserved.status="whole_work_reserved_before_submit";appendDaily(reserved,"reservation");
                                })) {
                                    dailySchedule.submitted(appIndex);
                                    action="daily_disk_whole_work_queued";reason="device snapshot qualification pending on worker; nominal estimate is not physical IO limit";
                                    tickDecision=action;tickTarget=app.name;tickBudget=request.reservedBytes;actionThisTick=true;
                                }
                            }
                        }
                        if(pairedForApp) dailyPair.cancel();
                        if(!actionThisTick && !pairedForApp) {
                        const Bytes residentAnon=*processes.rssAnonKiB*1024;
                        const Bytes ratioDenominator=*values.anonKiB*1024;
                        const Bytes donorAnon=std::min(ratioDenominator,residentAnon);
                        const Bytes keep=options.dailyAsync?dailyKeep:std::max(app.keep,KeepSlack);
                        const Bytes cap=std::min({systemGap,sub(donorAnon,keep),
                            options.batch,sub(options.appPerMinute,appProbeMinute[appIndex].used(monotonic())),
                            sub(options.perMinute,globalProbeMinute.used(monotonic()))});
                        const Bytes safeCap=sub(cap,KeepSlack);
                        budgetText=std::to_string(safeCap);
                        const bool strong=options.dailyKnownStrength && dailyKnownSnapshot && !probeProfile &&
                            daily::disk::ownedQualification(options,app.path,ids.size(),probeProfile);
                        const unsigned ratio=strong?daily::ratioWithin(safeCap,ratioDenominator,capacity?95U:60U):ratioForBudget(safeCap,ratioDenominator);
                        ratioText=std::to_string(ratio);
                        const Bytes nominalRequest=strong?daily::ratioNominal(ratioDenominator,ratio):(ratioDenominator*ratio)/100;
                        if(ratio<1) reason="budget below one-percent ratio quantum";
                        else if(nominalRequest<options.minBatch) reason="nominal request below configured min_batch";
                        else if(probeProfile && !sameProbe(dir.get(),options.root,app.path,*probeIdentity)) reason="probe PID identity changed before write";
                        else if(!probeProfile && !daily::disk::ownedQualification(options,app.path,ids.size(),probeProfile) &&
                                !backgroundEligible(dir.get(),app,ids,currentProcesses)) reason="background eligibility or member identity changed before write";
                        else {
                            Control latest=parseControl(readPath(options.control));
                            if(latest.paused || daily::disk::targetProtected(options,app.name,app.path,latest.protectedApps)) reason="control pause/protection changed before write";

                            else if(options.dailyAsync) {
                                struct stat group{};
                                if(::fstat(dir.get(),&group)!=0) throw error("daily reservation group identity");
                                daily::Request request;
                                request.sequence=++dailySequence; request.actionId=dailyGeneration+"_daily_"+std::to_string(request.sequence);
                                request.app=app.name; request.groupPath=app.path; request.appIndex=appIndex;
                                if(!probeProfile && daily::disk::ownedQualification(options,app.path,ids.size(),probeProfile)) request.qualification="owned_os_snapshot";
                                request.groupDevice=static_cast<std::uint64_t>(group.st_dev); request.groupInode=static_cast<std::uint64_t>(group.st_ino);
                                for(const auto pid:ids) {
                                    const auto member=currentProcesses.find(pid);
                                    if(member==currentProcesses.end() || !member->second.startTicks) throw std::runtime_error("daily reservation member identity unknown");
                                    request.members.emplace(pid,member->second.startTicks);
                                }
                                request.reservedBytes=safeCap; request.keepBytes=keep; request.minBatch=options.minBatch;
                                request.plannedRatio=ratio; request.reservedAt=monotonic();
                                request.maxAgeSeconds=std::max(1.0,static_cast<double>(options.intervalMs)/500.0);
                                request.forecastSampledAt=sys.time; request.forecastMaxAgeSeconds=std::max(1.0,options.horizonSec);
                                request.recentTarget=recentSnapshot;
                                if(options.dailyKnownFutureFreeOnly) {
                                    if(!dailyKnownSnapshot || !daily::known::memberMatches(*dailyKnownSnapshot,request.groupPath,request.members))
                                        throw std::runtime_error("daily known event registered member mismatch");
                                    request.knownEvent=dailyKnownSnapshot;request.hardDeadline=dailyKnownSnapshot->launch;request.knownStrength=options.dailyKnownStrength;request.knownCapacity=options.dailyKnownCapacity;request.knownLeadSec=options.dailyKnownLeadSec;
                                }
                                if(!daily::capacityAdapterAllows(options,request,probeProfile)) throw std::runtime_error("known_capacity_reservation_authority");
                                const auto job=[options,app,request,expected=currentProcesses,probeIdentity,probeProfile,forecast,stopping,recentStore,dailyKnownEvidence](const std::atomic<bool>& cancelled) {
                                    return performDailyAction(options,app,request,expected,probeIdentity,probeProfile,forecast,stopping,cancelled,recentStore,dailyKnownEvidence);
                                };
                                const bool submitted=dailyExecutor->submit(request,job,[&] {
                                    appProbeMinute[appIndex].charge(request.reservedAt,safeCap);
                                    globalProbeMinute.charge(request.reservedAt,safeCap);
                                    daily::Result reserved; reserved.request=request; reserved.status="reserved_before_submit";
                                    appendDaily(reserved,"reservation");
                                });
                                if(submitted) {
                                    dailySchedule.submitted(appIndex);
                                    action="daily_reserved_and_queued"; reason="action_id="+request.actionId+"; shared quota reserved before worker; feedback pending";
                                    tickDecision=action; tickTarget=app.name; tickBudget=safeCap; actionThisTick=true;
                                } else { action="daily_submission_refused"; reason="one pending worker or admission halted"; }
                            }
                            else {
                                FD writer(::openat(dir.get(),"memory.force_shrink_anon",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));  //lzx-note-normal
                                if(writer.get()<0) throw error("open memory.force_shrink_anon");  //lzx-note-normal
                                const std::string payload=std::to_string(ratio);  //lzx-note-normal
                                const auto started=monotonic();  //lzx-note-normal
                                const auto count=::write(writer.get(),payload.data(),payload.size());  //lzx-note-normal
                                const int writeErr=count<0?errno:0;  //lzx-note-normal
                                const auto elapsed=(monotonic()-started)*1000;  //lzx-note-normal
                                if(count!=static_cast<ssize_t>(payload.size())) {
                                    action="write_error"; reason="vendor write failed errno="+std::to_string(writeErr);  //lzx-note-normal
                                    nextProbeAction=monotonic()+60.0; tickDecision="ratio_write_error";  //lzx-note-normal
                                } else {
                                    std::this_thread::sleep_for(std::chrono::milliseconds(250));  //lzx-note-normal
                                    appProbeMinute[appIndex].charge(monotonic(),safeCap);  //lzx-note-normal
                                    globalProbeMinute.charge(monotonic(),safeCap);  //lzx-note-normal
                                    nextProbeAction=monotonic()+std::max(10.0,options.cooldownSec);  //lzx-note-normal
                                    tickDecision="ratio_submitted"; tickTarget=app.name; tickBudget=safeCap; actionThisTick=true;  //lzx-note-normal
                                    action="accepted_ratio_"+payload;  //lzx-note-normal
                                    try {
                                        const auto postPids=processIds(dir.get());  //lzx-note-normal
                                        if(postPids!=ids || (probeProfile?!sameProbe(dir.get(),options.root,app.path,*probeIdentity):
                                           !backgroundEligible(dir.get(),app,ids,currentProcesses)))
                                            throw std::runtime_error("post-write process identity or background eligibility changed");
                                        const auto postAnon=aggregateRssAnon(ids);
                                        if(!postAnon) throw std::runtime_error("post-write aggregate proc telemetry incomplete");
                                        const Bytes beforeAnon=*processes.rssAnonKiB;
                                        const Bytes observedDrop=beforeAnon>*postAnon?(beforeAnon-*postAnon)*1024:0;
                                        lastFeedbackAnonKiB[appIndex]=*postAnon; haveFeedback[appIndex]=true;
                                        std::ostringstream elapsedText; elapsedText<<std::fixed<<std::setprecision(3)<<elapsed;
                                        reason="write_ms="+elapsedText.str()+"; planned_bytes="+budgetText+"; observed_rssanon_drop_bytes="+std::to_string(observedDrop)+" (diagnostic only)";
                                        event(eventLog,"INFO",app.name,"probe reclaim accepted ratio="+payload+" planned_bytes="+budgetText+" observed_rssanon_drop_bytes="+std::to_string(observedDrop));
                                    } catch(const std::exception& e) {
                                        reason="write accepted; feedback unknown: "+std::string(e.what());  //lzx-note-normal
                                        nextProbeAction=monotonic()+60.0;  //lzx-note-normal
                                        event(eventLog,"WARN",app.name,reason);  //lzx-note-normal
                                    }
                                }
                            }
                        }
                        }
                    } catch(const std::exception& e) {
                        if(pairedForApp) dailyPair.cancel();
                        const std::string message=e.what();
                        if(message.find("app_score/oom_score_adj/member identity")!=std::string::npos) {
                            reason="protected: "+message; action="protected";
                        } else { reason=std::string("apply gate: ")+message; action="rejected"; }
                    }
                }
            }
            if(action!="none") { tickDecision=action; tickTarget=app.name; }
            appLog.row({std::to_string(unixMillis()),Schema,app.name,app.path,appPopulated(dir.get()),current,  //lzx-note-normal
                    "unknown_unverified",optionalNumber(values.anonKiB),optionalNumber(values.zramKiB),optionalNumber(values.eswapKiB),
                    rawCounter(*stat,"inactive_anon"),rawCounter(*stat,"inactive_file"),optionalRaw(dir.get(),"memory.anon_refault"),
                    std::to_string(processes.listed),std::to_string(processes.statusRead),std::to_string(processes.smapsRead),
                    optionalNumber(processes.rssKiB),optionalNumber(processes.vmSwapKiB),optionalNumber(processes.rssAnonKiB),
                    optionalNumber(processes.rssFileKiB),optionalNumber(processes.pssKiB),optionalNumber(processes.pssAnonKiB),
                    optionalNumber(processes.pssFileKiB),optionalNumber(processes.privateKiB),optionalNumber(processes.smapsSwapKiB),
                    std::to_string(processes.deltaMatched),std::to_string(processes.minfltDelta),std::to_string(processes.majfltDelta),
                    "","",budgetText,action,reason});
            } catch(const std::exception& e) {
                previousProcesses[appIndex].clear();
                appLog.row({std::to_string(unixMillis()),Schema,app.name,app.path,"unknown","","unknown","","","","","","",
                    "","","","","","","","","","","","","","","","","","","none",e.what()});
                event(eventLog,"WARN",app.name,e.what());
            }
        }
        if(fullObservationTick) {
            if(observationTimingLog) observationTimingLog->row({std::to_string(unixMillis()),std::to_string(sys.time),
                std::to_string(std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-observationStart).count()),
                std::to_string(observationSmapsMs),deferSmaps?"true":"false",std::to_string(observationProcCount),
                std::to_string(observationStatusRead),std::to_string(observationSmapsRead),std::to_string(observationFaultCoverage),
                    rateLimitSmaps?"true":"false",observationSmapsCpuMs?std::to_string(*observationSmapsCpuMs):std::string{}});
            // Skipped PSS fields stay unavailable. Never reuse old values for a new PID.
            // Schedule from completion so a slow scan cannot trigger catch-up scans.
            if(readSmaps) nextSmapsObservation=std::chrono::steady_clock::now()+
                std::chrono::milliseconds(options.harmonySmapsIntervalMs);
            nextFullObservation=std::chrono::steady_clock::now()+std::chrono::milliseconds(options.intervalMs);
        }
        const double oracleSubmitInterval=options.oracleProfile=="peak_transition_os_snapshot"
            ?static_cast<double>(options.oracleProfileActionIntervalMs)/1000.0:1.0;
        // Oracle profile 读取外部事件计划、核验身份和时限，并可能排队独立异步动作。
        // 这是测试专用路径，与普通 harmony-v1 观察循环不是同一套策略。
        if(oracleProfile && !oracleAction &&
           (oracleFastPollActive || monotonic()-lastOracleSubmit>=oracleSubmitInterval)) {
            try {
                const auto request=getCurrentOracleRequest(options,deviceEvidence);
                const double now=monotonic();
                const bool scheduleCompatible=oracleScheduleCompatible(options.oracleProfile,options.oracleBackend,request.schemaVersion);
                if(!scheduleCompatible) {
                    event(eventLog,"ERROR","oracle","schedule schema/profile mismatch; event rejected"); oracleHaltedEvents.insert(request.id);
                }
                if(options.oracleProfile=="peak_transition_os_snapshot")
                    oracleFastPollActive=scheduleCompatible && oracleFastPollWindow(options.oracleProofSource=="device",now,request.launch) &&
                        !oracleHaltedEvents.count(request.id);
                std::ostringstream fingerprintOut; fingerprintOut<<request.caseId<<'|'<<std::setprecision(12)<<request.launch<<'|'<<request.needed;
                fingerprintOut<<'|'<<request.schemaVersion;
                for(const auto& donor:request.donors) fingerprintOut<<'|'<<donor.bundle<<'|'<<donor.uid<<'|'<<donor.pid<<'|'<<donor.start<<'|'<<donor.coldReady<<'|'<<donor.dailyReady<<'|'<<donor.windowReady<<'|'<<donor.readyAt<<'|'<<donor.backgroundSince;
                if(request.schemaVersion==3) for(const auto& donor:request.donors) fingerprintOut<<'|'<<donor.writebackFilePath<<'|'<<donor.writebackFileDev<<'|'<<donor.writebackFileIno<<'|'<<donor.writebackFileSize;
                const auto fingerprint=request.planFingerprint.empty()?fingerprintOut.str():request.planFingerprint;
                const auto registered=oracleFingerprints.find(request.id);
                if(registered!=oracleFingerprints.end() && registered->second!=fingerprint) {
                    event(eventLog,"ERROR","oracle","duplicate event_id with changed donor identity; event rejected"); oracleHaltedEvents.insert(request.id);
                } else if(registered==oracleFingerprints.end()) {
                    bool prior=false;
                    if(fs::exists(options.oracleLedger) && fs::file_size(options.oracleLedger)>0) {
                        const auto old=readPath(options.oracleLedger); prior=old.find(","+csvCell(request.id)+",")!=std::string::npos;
                    }
                    if(prior) { event(eventLog,"ERROR","oracle","event_id already exists in durable ledger; duplicate registration rejected"); oracleHaltedEvents.insert(request.id); }
                    else {
                        oracleFingerprints.emplace(request.id,fingerprint);
                        OracleActionResult registeredRow; registeredRow.actionId=request.id+"_register_"+std::to_string(unixMillis()); registeredRow.eventId=request.id; registeredRow.caseId=request.caseId; registeredRow.deadline=request.launch; registeredRow.needed=request.needed; registeredRow.status="event_registered_once"; appendOracleLedger(options,registeredRow);
                    }
                }
                if(now>=request.launch) {
                    if(!oracleExpiredEvents.count(request.id)) for(const auto& donor:request.donors) {
                        OracleActionResult r; r.actionId=request.id+"_"+donor.bundle+"_expired"; r.eventId=request.id; r.caseId=request.caseId; r.bundle=donor.bundle; r.pid=donor.pid; r.uid=donor.uid; r.start=donor.start; r.deadline=request.launch; r.needed=request.needed; r.status="deadline_expired_no_action"; r.overlap=false; appendOracleLedger(options,r);
                    }
                    oracleExpiredEvents.insert(request.id); oracleHaltedEvents.insert(request.id);
                } else if(request.launch-now>35.0) {
                    event(eventLog,"WARN","oracle","schedule outside 35s future window; no action queued");
                } else if(!oracleHaltedEvents.count(request.id)) {
                    const bool ctlValid=controlState=="active" && !control.paused;
                    if(!ctlValid) {
                        event(eventLog,"WARN","oracle","standard control is paused or unknown; action refused");
                        OracleActionResult refused; refused.actionId=request.id+"_control_refused"; refused.eventId=request.id; refused.caseId=request.caseId; refused.deadline=request.launch; refused.needed=request.needed; refused.status="rejected:control_paused_or_unknown"; appendOracleLedger(options,refused); oracleHaltedEvents.insert(request.id);
                    }
                    else if(now-lastOracleSubmit<oracleSubmitInterval) {
                        // Keep polling the active event quickly, but do not prepare or reserve
                        // another action until the independent action-start interval elapses.
                    }
                    else {
                        std::set<std::string> donorsBelowFloorUnique,donorsAtRatioCeilingUnique,donorsProtectedUnique;
                        auto markBelowFloor=[&](const std::string& bundle){donorsBelowFloorUnique.insert(bundle);};
                        auto markRatioCeiling=[&](const std::string& bundle){donorsAtRatioCeilingUnique.insert(bundle);};
                        auto markProtected=[&](const std::string& bundle){donorsProtectedUnique.insert(bundle);};
                        const auto donorCount=static_cast<unsigned>(request.donors.size());
                        const unsigned rotationStart=donorCount?oracleNextDonor[request.id]%donorCount:0;
                        std::vector<unsigned> rotation; rotation.reserve(donorCount);
                        for(unsigned offset=0;offset<donorCount;++offset) rotation.push_back((rotationStart+offset)%donorCount);
                        std::vector<OracleDiskPriorityAttempt> donorAttempts;
                        const bool zramPriorityMode=options.oracleProofSource=="device" &&
                            options.oracleProfile=="peak_transition_os_snapshot" && options.oracleDiskPrepareAnon &&
                            options.oracleBackend=="force_swapout";
                        if(zramPriorityMode) {
                            std::vector<bool> zramPoolReady(donorCount,false),diskPrepared(donorCount,false);
                            std::vector<std::optional<bool>> prepRoomAvailable(donorCount);
                            for(const unsigned index:rotation) {
                                const auto& candidate=request.donors[index];
                                const std::string candidateKey=request.id+"|"+candidate.bundle;
                                diskPrepared[index]=oracleDiskPreparedTargets.count(candidateKey)!=0;
                                if(oracleSubmittedTargets.count(candidateKey)) continue;
                                const auto candidateApp=std::find_if(config.apps.begin(),config.apps.end(),[&](const App& app){
                                    const auto slash=app.path.find_last_of('/');
                                    const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
                                    return app.reclaim && oracleAppMatchesDonor(options.oracleProfile,app.name,leaf,candidate.bundle,candidate.uid);
                                });
                                if(candidateApp==config.apps.end()) continue;
                                try {
                                    FD candidateDir=openBeneath(root.get(),candidateApp->path);
                                    const auto candidateStat=maybeAt(candidateDir.get(),"memory.stat");
                                    const auto candidatePool=candidateStat?parseOracleDiskPool(*candidateStat):std::optional<OracleDiskPool>{};
                                    zramPoolReady[index]=candidatePool && candidatePool->zramBytes>=options.minBatch;
                                    if(zramPoolReady[index] && !diskPrepared[index]) {
                                        const auto rss=sampleOracleRss(candidate,*candidateApp);
                                        if(rss) prepRoomAvailable[index]=oracleDiskPriorityPrepRoomAvailable(*rss,
                                            options.oracleCumulativeKeep,
                                            oracleDeviceDonorTotalKeepFloor(options,candidate.bundle,candidateApp->keep),
                                            options.minBatch,64*MiB);
                                    }
                                } catch(...) { /* unknown ordering hints preserve the normal guarded attempt */ }
                            }
                            donorAttempts=oracleDiskPriorityAttempts(rotation,zramPoolReady,diskPrepared,prepRoomAvailable);
                            std::string prioritySignature;
                            unsigned priorityDonors=0;
                            for(const auto& item:donorAttempts) if(item.priorityDiskOnly) {
                                ++priorityDonors;
                                prioritySignature+=std::to_string(item.donorIndex)+",";
                            }
                            const auto oldSignature=oracleDiskPrioritySnapshotSignatureByEvent.find(request.id);
                            if(oldSignature==oracleDiskPrioritySnapshotSignatureByEvent.end() || oldSignature->second!=prioritySignature) {
                                oracleDiskPrioritySnapshotSignatureByEvent[request.id]=prioritySignature;
                                event(eventLog,"INFO","oracle","event_id="+request.id+
                                    " disk_priority_snapshot priority_unique_donors="+std::to_string(priorityDonors)+
                                    " total_unique_donors="+std::to_string(donorCount)+
                                    " min_batch_bytes="+std::to_string(options.minBatch)+
                                    " pass1=prepared_or_no_anon_prep_room_disk_only pass2=bounded_normal_fallback"+
                                    " anon_prep_room=keep_plus_64mib_margin_plus_min_batch hint_is_ordering_only=true");
                            }
                        } else {
                            donorAttempts.reserve(donorCount);
                            for(const unsigned index:rotation) donorAttempts.push_back({index,false});
                        }
                        for(const auto& donorAttempt:donorAttempts) {
                            const unsigned donorIndex=donorAttempt.donorIndex; const auto& donor=request.donors[donorIndex];
                            if(std::any_of(oracleFloorDisabledTargets.begin(),oracleFloorDisabledTargets.end(),[&](const auto& disabled) {
                                return floorDisabledKeyMatches(disabled.first.c_str(),disabled.second.c_str(),request.id.c_str(),donor.bundle.c_str());
                            })) continue;
                            const auto appIt=std::find_if(config.apps.begin(),config.apps.end(),[&](const App& app){
                                const auto slash=app.path.find_last_of('/'); const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
                                return app.reclaim && oracleAppMatchesDonor(options.oracleProfile,app.name,leaf,donor.bundle,donor.uid);
                            });
                            if(appIt==config.apps.end()) { event(eventLog,"ERROR","oracle","donor bundle/path/UID is outside the exact self-workload allowlist"); oracleHaltedEvents.insert(request.id); break; }
                            if(options.oracleProofSource=="device") {
                                const auto deviceFreshness=oracleFreshnessFailureImpl(true,now,{{"aa",donor.aaObserved},{"window",donor.windowObserved},{"proc",donor.procObserved},{"lifecycle",donor.lifecycleObserved}},2.5);
                                if(!deviceFreshness.empty()) {event(eventLog,"WARN","oracle",donor.bundle+" guard_failure="+deviceFreshness+"; waiting_for_fresh_device_proof");break;}
                                const auto protectedReason=deviceOracleProtectedReason(donor,now);
                                if(!protectedReason.empty()) {
                                    event(eventLog,"INFO","oracle",donor.bundle+" precommit_skip=protected reason="+protectedReason+
                                        " aa_state="+donor.aaState+" visible="+(donor.windowVisible?"true":"false")+
                                        " focused="+(donor.windowFocused?"true":"false")+" identity_verified=true proof_sequence="+std::to_string(request.proofSequence)+
                                        "; no_budget_reserved_no_write");
                                    markProtected(donor.bundle); continue;
                                }
                            }
                            const std::string targetKey=request.id+"|"+donor.bundle;
                            const bool targetAlreadySubmitted=oracleSubmittedTargets.count(targetKey)!=0;
                            if(options.oracleProfile=="peak_transition_os_snapshot" && (isProcFileBackend(options.oracleBackend) || options.oracleBackend=="force_swapout") &&
                                (!oracleProfileTargetMaySubmit(targetAlreadySubmitted,
                                    donor.pid!=0 && donor.start!=0 && donor.procVerified && donor.procStart==donor.start))) {
                                if(targetAlreadySubmitted) {
                                    const auto priorityAction=oracleDiskPriorityActionIds.find(targetKey);
                                    logOracleDiskSkipOnce(request.id,donor.bundle,
                                        "already_submitted",
                                        "target_key="+targetKey+" action_id="+
                                        (priorityAction==oracleDiskPriorityActionIds.end()?"unknown":priorityAction->second)+
                                        " one_shot=true");
                                }
                                markBelowFloor(donor.bundle); continue;
                            }
                            // Both stages share the same per-case/global/app rolling limits.
                            // Each stage revalidates identity, background, control and deadline.
                            const bool preparingDisk=options.oracleDiskPrepareAnon &&
                                !oracleDiskPreparedTargets.count(targetKey) && !donorAttempt.priorityDiskOnly;
                            Options actionOptions=options;
                            if(preparingDisk) {
                                actionOptions.oracleBackend="force_shrink_all";
                                actionOptions.oracleRatioSemantics="cumulative_anon_watermark";
                            }
                            const auto cooldown=oracleDonorBlockedUntil.find(appIt->name);
                            if(cooldown!=oracleDonorBlockedUntil.end() && now<cooldown->second) {
                                const bool identityComplete=donor.pid!=0 && donor.start!=0 && donor.procVerified && donor.procStart==donor.start;
                                const bool devicePipelineCooldown=options.oracleProofSource=="device" && request.schemaVersion==2 &&
                                    actionOptions.oracleDiskPrepareAnon && (actionOptions.oracleBackend=="force_shrink_all" || actionOptions.oracleBackend=="force_swapout");
                                if(devicePipelineCooldown) {
                                    std::string failure;
                                    try { failure=oracleBackgroundFailure(actionOptions,*appIt,donor,now); }
                                    catch(const std::exception& e) { failure=std::string("identity_probe_error:")+e.what(); }
                                    const auto selection=oracleDeviceCooldownSelection(true,request.schemaVersion,
                                        actionOptions.oracleDiskPrepareAnon,actionOptions.oracleBackend,true,identityComplete,failure,
                                        control.protectedApps.count(appIt->name) || control.protectedApps.count(donor.bundle));
                                    if(selection==OracleDeviceCooldownSelection::HaltEvent) {
                                        event(eventLog,"WARN","oracle",donor.bundle+" device_pipeline_cooldown_guard="+failure+"; halting_event");
                                        oracleHaltedEvents.insert(request.id); break;
                                    }
                                    if(selection==OracleDeviceCooldownSelection::WaitForProof) {
                                        event(eventLog,"WARN","oracle",donor.bundle+" device_pipeline_cooldown_guard="+failure+"; waiting_for_fresh_proof");
                                        break;
                                    }
                                    if(selection==OracleDeviceCooldownSelection::SkipDonor) {
                                        markBelowFloor(donor.bundle);
                                        event(eventLog,"INFO","oracle",donor.bundle+" device_pipeline_feedback_cooldown_skip identity_verified=true background_verified=true; no_reservation_no_write; cooldown_unchanged");
                                        continue;
                                    }
                                }
                                if(oracleProfileDonorCooldownScope(request.schemaVersion,actionOptions.oracleProfile,actionOptions.oracleBackend)) {
                                    if(!oracleProfileCooldownMayContinue(request.schemaVersion,actionOptions.oracleProfile,actionOptions.oracleBackend,identityComplete)) {
                                        event(eventLog,"WARN","oracle",donor.bundle+" feedback cooldown has unknown donor identity; halting_event");
                                        oracleHaltedEvents.insert(request.id); break;
                                    }
                                    std::string cooldownGuardFailure;
                                    try { cooldownGuardFailure=oracleBackgroundFailure(actionOptions,*appIt,donor,now); }
                                    catch(const std::exception& e) { cooldownGuardFailure=std::string("identity_probe_error:")+e.what(); }
                                    if(!cooldownGuardFailure.empty()) {
                                        if(!oracleFreshnessFailureRetryableImpl(cooldownGuardFailure)) {
                                            event(eventLog,"WARN","oracle",donor.bundle+" feedback cooldown guard_failure="+cooldownGuardFailure+"; halting_event");
                                            oracleHaltedEvents.insert(request.id);
                                        } else event(eventLog,"WARN","oracle",donor.bundle+" feedback cooldown guard_failure="+cooldownGuardFailure+"; waiting_for_fresh_proof");
                                        break;
                                    }
                                    markBelowFloor(donor.bundle);
                                    event(eventLog,"INFO","oracle",donor.bundle+" feedback cooldown remains active; donor skipped and event continues");
                                    continue;
                                }
                                event(eventLog,"WARN","oracle",donor.bundle+" feedback cooldown remains active"); break;
                            }
                            if(control.protectedApps.count(appIt->name) || control.protectedApps.count(donor.bundle)) { event(eventLog,"WARN","oracle",donor.bundle+" is protected by runtime control"); oracleHaltedEvents.insert(request.id); break; }
                            if(request.schemaVersion==1 && !donor.coldReady) { event(eventLog,"WARN","oracle",donor.bundle+" guard_failure=cold_ready_false; halting event"); oracleHaltedEvents.insert(request.id); break; }
                            const std::string memberWaitKey=request.id+"|"+donor.bundle;
                            const auto memberWait=oraclePrecommitMemberWaitStart.find(memberWaitKey);
                            const bool memberWaitStarted=memberWait!=oraclePrecommitMemberWaitStart.end();
                            const double memberWaitAt=memberWaitStarted?memberWait->second:0.0;
                            const bool memberRetryScope=oracleProfileDonorCooldownScope(request.schemaVersion,
                                actionOptions.oracleProfile,actionOptions.oracleBackend);
                            if(memberWaitStarted && !oraclePrecommitMemberWaitWindowOpen(true,memberWaitAt,now,request.launch)) {
                                event(eventLog,"WARN","oracle",donor.bundle+" cgroup_member_count_retry=stopped reason=retry_window_or_deadline_expired"+
                                    " wait_start_boot="+std::to_string(memberWaitAt)+" now_boot="+std::to_string(now)+
                                    " deadline_boot="+std::to_string(request.launch)+"; halting_event");
                                oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                oracleHaltedEvents.insert(request.id); break;
                            }
                            std::string guardFailure;
                            CgroupMemberObservation memberObservation;
                            std::optional<OracleRssSample> keepSample;
                            try { guardFailure=oracleBackgroundFailure(actionOptions,*appIt,donor,now,&memberObservation,&keepSample); }
                            catch(const std::exception& e) { guardFailure=std::string("identity_probe_error:")+e.what(); }
                            const double guardCheckedAt=monotonic();
                            if(!guardFailure.empty()) {
                                if(oraclePrecommitMaySkipDonor(guardFailure) && keepSample) {
                                    const auto required=oraclePipelineAnonKeep(*keepSample,actionOptions.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep));
                                    event(eventLog,"INFO","oracle",donor.bundle+" precommit_skip=verified_below_keep identity_verified=true"+
                                        " rssanon_kib="+std::to_string(keepSample->rssAnonKiB)+" rss_kib="+std::to_string(*keepSample->rssKiB)+
                                        " required_anon_keep_bytes="+std::to_string(*required)+
                                        " total_keep_bytes="+std::to_string(oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep))+
                                        " now_boot="+std::to_string(guardCheckedAt)+"; no_budget_reserved_no_write");
                                    oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                    markBelowFloor(donor.bundle);
                                    continue;
                                }
                                if(guardFailure=="cgroup_member_count_changed" && memberRetryScope) {
                                    const std::uint64_t solePid=memberObservation.uniquePids.size()==1?memberObservation.uniquePids.front():0;
                                    const auto retry=oraclePrecommitMemberRetryAction(memberRetryScope,
                                        memberObservation.readOk,memberObservation.parseValid,memberObservation.entryCount,
                                        solePid,donor.pid,memberWaitStarted,memberWaitAt,guardCheckedAt,request.launch);
                                    if(retry==OraclePrecommitMemberRetryAction::Wait) {
                                        if(!memberWaitStarted) oraclePrecommitMemberWaitStart.emplace(memberWaitKey,guardCheckedAt);
                                        const double start=memberWaitStarted?memberWaitAt:guardCheckedAt;
                                        event(eventLog,"WARN","oracle",donor.bundle+" guard_failure=cgroup_member_count_changed; bounded_precommit_recheck=waiting"+
                                            " entries="+std::to_string(memberObservation.entryCount)+
                                            " unique="+std::to_string(memberObservation.uniquePids.size())+
                                            " pids="+([&]{std::string s; for(const auto pid:memberObservation.uniquePids){if(!s.empty())s+="|";s+=std::to_string(pid);} return s;})()+
                                            " raw="+safeMemberRaw(memberObservation.raw)+
                                            " wait_start_boot="+std::to_string(start)+" now_boot="+std::to_string(guardCheckedAt)+
                                            " retry_until_boot="+std::to_string(std::min(start+2.0,request.launch)));
                                        break;
                                    }
                                    event(eventLog,"WARN","oracle",donor.bundle+" guard_failure=cgroup_member_count_changed; bounded_precommit_recheck=stopped"+
                                        " entries="+std::to_string(memberObservation.entryCount)+
                                        " unique="+std::to_string(memberObservation.uniquePids.size())+
                                        " pids="+([&]{std::string s; for(const auto pid:memberObservation.uniquePids){if(!s.empty())s+="|";s+=std::to_string(pid);} return s;})()+
                                        " raw="+safeMemberRaw(memberObservation.raw)+
                                        " read_ok="+(memberObservation.readOk?"true":"false")+
                                        " parse_valid="+(memberObservation.parseValid?"true":"false")+
                                        " now_boot="+std::to_string(guardCheckedAt)+" deadline_boot="+std::to_string(request.launch)+"; halting_event");
                                    oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                    oracleHaltedEvents.insert(request.id); break;
                                }
                                if(guardFailure=="cgroup_member_list_read_error" || guardFailure=="cgroup_member_list_parse_unknown") {
                                    event(eventLog,"ERROR","oracle",donor.bundle+" guard_failure="+guardFailure+
                                        " entries="+std::to_string(memberObservation.entryCount)+
                                        " unique="+std::to_string(memberObservation.uniquePids.size())+
                                        " invalid_tokens="+std::to_string(memberObservation.invalidCount)+
                                        " raw="+safeMemberRaw(memberObservation.raw)+
                                        " read_ok="+(memberObservation.readOk?"true":"false")+
                                        " parse_valid="+(memberObservation.parseValid?"true":"false")+
                                        "; halting_event");
                                    oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                    oracleHaltedEvents.insert(request.id); break;
                                }
                                if(oracleFreshnessFailureRetryableImpl(guardFailure)) {
                                    if(memberWaitStarted && !oraclePrecommitMemberWaitWindowOpen(true,memberWaitAt,guardCheckedAt,request.launch)) {
                                        event(eventLog,"WARN","oracle",donor.bundle+" bounded_precommit_recheck=stopped reason=other_freshness_failure_after_window"+
                                            " failure="+guardFailure+" wait_start_boot="+std::to_string(memberWaitAt)+
                                            " now_boot="+std::to_string(guardCheckedAt)+" deadline_boot="+std::to_string(request.launch)+"; halting_event");
                                        oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                        oracleHaltedEvents.insert(request.id); break;
                                    }
                                    event(eventLog,"WARN","oracle",donor.bundle+" guard_failure="+guardFailure+"; waiting_for_fresh_proof");
                                    break;
                                }
                                oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                event(eventLog,"WARN","oracle",donor.bundle+" guard_failure="+guardFailure+"; halting_event"); oracleHaltedEvents.insert(request.id); break;
                            }
                            if(memberWaitStarted) {
                                const auto recovered=oraclePrecommitMemberRetryAction(memberRetryScope,true,
                                    memberObservation.parseValid,memberObservation.entryCount,
                                    memberObservation.uniquePids.size()==1?memberObservation.uniquePids.front():0,
                                    donor.pid,true,memberWaitAt,guardCheckedAt,request.launch);
                                if(recovered!=OraclePrecommitMemberRetryAction::Recovered) {
                                    event(eventLog,"WARN","oracle",donor.bundle+" bounded_precommit_recheck=stopped reason=member_list_not_exactly_original_pid"+
                                        " entries="+std::to_string(memberObservation.entryCount)+
                                        " unique="+std::to_string(memberObservation.uniquePids.size())+
                                        " wait_start_boot="+std::to_string(memberWaitAt)+" now_boot="+std::to_string(guardCheckedAt)+
                                        " deadline_boot="+std::to_string(request.launch)+"; halting_event");
                                    oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                    oracleHaltedEvents.insert(request.id); break;
                                }
                                event(eventLog,"INFO","oracle",donor.bundle+" bounded_precommit_recheck=member_count_recovered"+
                                    " entries="+std::to_string(memberObservation.entryCount)+
                                    " unique="+std::to_string(memberObservation.uniquePids.size())+
                                    " pid="+std::to_string(donor.pid)+" now_boot="+std::to_string(guardCheckedAt)+
                                    "; revalidated_full_guard_before_preflight");
                            }
                            try {
                                FD appDir=openBeneath(root.get(),appIt->path);
                                const auto stat=maybeAt(appDir.get(),"memory.stat");
                                const auto anon=stat?parseVendorStat(*stat).anonKiB:std::optional<Bytes>{};
                                Bytes donorBytes=0;
                                Bytes donorResidentBytes=0;
                                Bytes donorAnonResidentBytes=0;
                                if(actionOptions.oracleBackend=="force_swapout") {
                                    const auto pool=stat?parseOracleDiskPool(*stat):std::optional<OracleDiskPool>{};
                                    const auto resident=sampleOracleRss(donor,*appIt);
                                    if(!pool || !resident) throw std::runtime_error("disk_pool_or_process_identity_unknown");
                                    donorAnonResidentBytes=resident->rssAnonKiB*1024;
                                    if(!oracleDiskPoolMeetsMinimum(pool->zramBytes,actionOptions.minBatch)) {
                                        logOracleDiskSkipOnce(request.id,donor.bundle,"pool_below_min_batch",
                                            "action_id=not_reserved zram_bytes="+std::to_string(pool->zramBytes)+
                                            " eswap_bytes="+std::to_string(pool->eswapBytes)+
                                            " min_batch_bytes="+std::to_string(actionOptions.minBatch));
                                        markBelowFloor(donor.bundle);
                                        continue;
                                    }
                                    if(donorAttempt.priorityDiskOnly && !oracleDiskPreparedTargets.count(targetKey)) {
                                        const auto freshPrepRoom=oracleDiskPriorityPrepRoomAvailable(*resident,
                                            actionOptions.oracleCumulativeKeep,
                                            oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep),
                                            actionOptions.minBatch,64*MiB);
                                        if(!freshPrepRoom || *freshPrepRoom) {
                                            logOracleDiskSkipOnce(request.id,donor.bundle,
                                                freshPrepRoom?"fresh_anon_prep_room_preserved":"fresh_anon_prep_room_unknown",
                                                "action_id=not_reserved zram_bytes="+std::to_string(pool->zramBytes)+
                                                " rssanon_bytes="+std::to_string(resident->rssAnonKiB*1024)+
                                                " min_batch_bytes="+std::to_string(actionOptions.minBatch)+
                                                " priority_disk_only=false fallback=bounded_normal_prep");
                                            continue;
                                        }
                                    }
                                    if(actionOptions.oracleDiskPrepareAnon
                                        ?!oraclePipelineKeepSatisfied(*resident,actionOptions.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep))
                                        :resident->rssAnonKiB*1024<oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep)) {
                                        logOracleDiskSkipOnce(request.id,donor.bundle,"keep_floor",
                                            "action_id=not_reserved zram_bytes="+std::to_string(pool->zramBytes)+
                                            " eswap_bytes="+std::to_string(pool->eswapBytes)+
                                            " rssanon_bytes="+std::to_string(resident->rssAnonKiB*1024)+
                                            " total_keep_bytes="+std::to_string(oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep)));
                                        markBelowFloor(donor.bundle); continue;
                                    }
                                    const auto work=oracleDiskWorkEstimate(pool->zramBytes,resident->rssAnonKiB);
                                    if(!work) { markBelowFloor(donor.bundle); continue; }
                                    donorBytes=*work;
                                } else if(isProcFileBackend(actionOptions.oracleBackend)) {
                                    const auto fileSample=sampleOracleRss(donor,*appIt);
                                    if(!fileSample || !fileSample->rssFileKiB || !fileSample->rssKiB || *fileSample->rssFileKiB==0)
                                        throw std::runtime_error("process_vmrss_or_rssfile_telemetry_missing");
                                    if(*fileSample->rssFileKiB>std::numeric_limits<Bytes>::max()/1024 ||
                                       *fileSample->rssKiB>std::numeric_limits<Bytes>::max()/1024)
                                        throw std::runtime_error("process_vmrss_or_rssfile_bytes_overflow");
                                    donorBytes=*fileSample->rssFileKiB*1024;
                                    donorResidentBytes=*fileSample->rssKiB*1024;
                                    if(donorResidentBytes<donorBytes || donorResidentBytes-donorBytes<oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep)) {
                                        event(eventLog,"WARN",donor.bundle,"proc_file skipped: retained_other_below_keep_reserve");
                                        markBelowFloor(donor.bundle); continue;
                                    }
                                } else {
                                    const auto processAnon=aggregateRssAnon({donor.pid});
                                    if(!anon || !processAnon) throw std::runtime_error("anon_telemetry_missing");
                                    donorBytes=std::min(*anon*1024,*processAnon*1024);
                                    donorAnonResidentBytes=*processAnon*1024;
                                }
                                const Bytes eventGap=oracleEventGap(actionOptions,sys,forecast,request.needed);
                                const Bytes globalRoom=oracleGlobalLimit.room(now,actionOptions.oracleGlobalCap);
                                const Bytes caseRoom=oracleCaseLimits[request.caseId].room(now,actionOptions.oracleCaseCap);
                                const Bytes appRoom=oracleAppLimits[donor.bundle].room(now,actionOptions.oracleAppCap);
                                const Bytes keepFloor=actionOptions.oracleRatioSemantics=="cumulative_anon_watermark"
                                    ?actionOptions.oracleCumulativeKeep
                                    :oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep);
                                const Bytes cap=isProcFileBackend(actionOptions.oracleBackend)
                                    ?sub(std::min({eventGap,actionOptions.oracleBatch,globalRoom,caseRoom,appRoom}),64*MiB)
                                    :sub(std::min({eventGap,sub(sys.swapFree,actionOptions.swapReserve),sub(donorBytes,keepFloor),actionOptions.oracleBatch,globalRoom,caseRoom,appRoom}),64*MiB);
                                unsigned ratio=0;
                                Bytes nominal=0;
                                Bytes plannedIncrement=0;
                                const bool workMode=oraclePreparationWorkMode(actionOptions.oracleProofSource=="device",preparingDisk,actionOptions.oracleBackend);
                                OracleWatermarkPlan watermark;
                                if(actionOptions.oracleBackend=="force_swapout") {
                                    nominal=oracleDiskReservation(donorBytes,std::min({eventGap,sub(sys.swapFree,actionOptions.swapReserve),actionOptions.oracleBatch,globalRoom,caseRoom,appRoom}),64*MiB,actionOptions.minBatch);
                                    if(nominal==0) {
                                        logOracleDiskSkipOnce(request.id,donor.bundle,"budget_or_pool_below_min_batch",
                                            "action_id=not_reserved work_bytes="+std::to_string(donorBytes)+
                                            " event_gap_bytes="+std::to_string(eventGap)+
                                            " swap_room_bytes="+std::to_string(sub(sys.swapFree,actionOptions.swapReserve))+
                                            " batch_bytes="+std::to_string(actionOptions.oracleBatch)+
                                            " global_room_bytes="+std::to_string(globalRoom)+
                                            " case_room_bytes="+std::to_string(caseRoom)+
                                            " app_room_bytes="+std::to_string(appRoom)+
                                            " min_batch_bytes="+std::to_string(actionOptions.minBatch));
                                        markBelowFloor(donor.bundle); continue;
                                    }
                                } else if(isProcFileBackend(actionOptions.oracleBackend)) {
                                    nominal=actionOptions.oracleBackend=="proc_file_writeback"
                                        ?oracleProcFileWritebackReservation(donorBytes,donor.writebackFileSize,donorResidentBytes,
                                            oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep),eventGap,actionOptions.oracleBatch,globalRoom,caseRoom,appRoom,64*MiB,actionOptions.minBatch)
                                        :oracleProcFileReservation(donorBytes,donorResidentBytes,
                                            oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep),eventGap,actionOptions.oracleBatch,
                                            globalRoom,caseRoom,appRoom,64*MiB,actionOptions.minBatch);
                                    if(nominal==0) {
                                        const Bytes limiting=std::min({eventGap,actionOptions.oracleBatch,globalRoom,caseRoom,appRoom});
                                        const Bytes requiredFileEstimate=actionOptions.oracleBackend=="proc_file_writeback"?std::max(donorBytes,donor.writebackFileSize):donorBytes;
                                        const std::string skip=requiredFileEstimate<actionOptions.minBatch?"below_min_batch_or_registered_file_too_small":
                                            limiting==eventGap?"system_deficit":limiting==actionOptions.oracleBatch?"single_batch_cap":
                                            limiting==globalRoom?"global_shared_room":limiting==caseRoom?"case_shared_room":"app_shared_room";
                                        event(eventLog,"WARN",donor.bundle,"proc_file skipped: conservative_file_work_estimate_exceeds_"+skip);
                                        markBelowFloor(donor.bundle); continue;
                                    }
                                } else if(actionOptions.oracleRatioSemantics=="cumulative_anon_watermark") {
                                    const auto fresh=sampleOracleRss(donor,*appIt);
                                    if(!fresh || !fresh->vmSwapKiB) throw std::runtime_error("cumulative_watermark_proc_or_vmswap_unknown");
                                    const Bytes liveResident=fresh->rssAnonKiB*1024;
                                    const auto requiredKeep=preparingDisk
                                        ?oraclePipelineAnonKeep(*fresh,actionOptions.oracleCumulativeKeep,oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep))
                                        :std::optional<Bytes>{actionOptions.oracleCumulativeKeep};
                                    if(!requiredKeep) throw std::runtime_error("pipeline_planning_total_rss_keep_unknown");
                                    const Bytes candidateCap=std::min(cap,sub(liveResident,*requiredKeep+64*MiB));
                                    watermark=oracleCumulativeWatermarkForCap(liveResident,*fresh->vmSwapKiB*1024,candidateCap,actionOptions.oracleMaxRatio);
                                    ratio=watermark.ratio;
                                    plannedIncrement=watermark.estimatedIncrementBytes;
                                    nominal=plannedIncrement;
                                    if(workMode && ratio && plannedIncrement>=actionOptions.minBatch) {
                                        const Bytes workRoom=std::min({eventGap,sub(sys.swapFree,actionOptions.swapReserve),actionOptions.oracleBatch,globalRoom,caseRoom,appRoom});
                                        nominal=oraclePreparationWorkReservation(*fresh,workRoom,64*MiB);
                                        if(!nominal) { markBelowFloor(donor.bundle); continue; }
                                    }
                                } else {
                                    ratio=oracleRatioForCap(cap,donorBytes,actionOptions.oracleMaxRatio);
                                    nominal=donorBytes?(donorBytes*ratio)/100:0;
                                }
                                if(!isProcFileBackend(actionOptions.oracleBackend) && actionOptions.oracleBackend!="force_swapout" && (ratio==0 || nominal<actionOptions.minBatch)) {
                                    if(preparingDisk) {
                                        // No preparation was submitted: existing pool may still be drained
                                        // on a later tick with a fresh, independently budgeted disk action.
                                        oracleDiskPreparedTargets.insert(targetKey);
                                        event(eventLog,"INFO",donor.bundle,"disk_prepare_not_submitted_below_budget_or_keep_floor");
                                    }
                                    markBelowFloor(donor.bundle);
                                    continue;
                                }
                                const auto priorRatioIt=oraclePositiveRatioByEventDonor.find(request.id+"|"+donor.bundle);
                                const unsigned priorSuccessfulRatio=priorRatioIt==oraclePositiveRatioByEventDonor.end()?0:priorRatioIt->second;
                                if(!isProcFileBackend(actionOptions.oracleBackend) && actionOptions.oracleBackend!="force_swapout" && priorSuccessfulRatio && !oracleRatioCanAdvance(priorSuccessfulRatio,ratio)) {
                                    markRatioCeiling(donor.bundle);
                                    event(eventLog,"INFO","oracle",donor.bundle+" ratio_skipped_candidate="+std::to_string(ratio)+
                                          " prior_positive_ratio="+std::to_string(priorSuccessfulRatio)+" reason=no_strict_increase");
                                    continue;
                                }
                                const double reserveCheckAt=monotonic();
                                if(!oracleStructuralPipelineDeadlineAllows(actionOptions.oracleDiskPrepareAnon && preparingDisk,
                                    reserveCheckAt,oracleSubmitInterval,request.launch)) {
                                    if(!std::isfinite(reserveCheckAt) || reserveCheckAt<0) throw std::runtime_error("pipeline_reservation_clock_unknown");
                                    if(oracleStructuralDeadlineLogged.insert(targetKey).second)
                                        event(eventLog,"INFO",donor.bundle,"structural_pipeline_deadline_skip no_reservation_no_write checked_at="+
                                            std::to_string(reserveCheckAt)+" interval="+std::to_string(oracleSubmitInterval)+" deadline="+std::to_string(request.launch));
                                    if(reserveCheckAt>=request.launch) break;
                                    continue;
                                }

                                if(memberWaitStarted && !oraclePrecommitMemberWaitWindowOpen(true,memberWaitAt,reserveCheckAt,request.launch)) {
                                    event(eventLog,"WARN","oracle",donor.bundle+" bounded_precommit_recheck=stopped reason=retry_window_expired_before_reservation"+
                                        " wait_start_boot="+std::to_string(memberWaitAt)+" now_boot="+std::to_string(reserveCheckAt)+
                                        " deadline_boot="+std::to_string(request.launch)+"; no_reservation_no_write");
                                    oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                    oracleHaltedEvents.insert(request.id); break;
                                }
                                if(!oracleMayReserveAction(oracleAction.has_value(),reserveCheckAt,lastOracleSubmit,
                                    oracleSubmitInterval,request.launch)) break;
                                // Persist reservation before submitting. Missing completion later means this amount remains conservatively charged.
                                const std::string actionId=request.id+"_"+donor.bundle+"_"+actionOptions.oracleBackend+"_"+std::to_string(unixMillis());
                                OracleActionResult reserved; reserved.actionId=actionId; reserved.eventId=request.id; reserved.caseId=request.caseId; reserved.bundle=donor.bundle; reserved.pid=donor.pid; reserved.uid=donor.uid; reserved.start=donor.start; reserved.deadline=request.launch; reserved.needed=request.needed; reserved.remaining=eventGap; reserved.input=donorBytes; reserved.ratio=ratio; reserved.plannedRatio=ratio; reserved.nominal=nominal; reserved.evidenceSource=donor.evidenceSource; reserved.aaObserved=donor.aaObserved; reserved.windowObserved=donor.windowObserved; reserved.procObserved=donor.procObserved; reserved.lifecycleObserved=donor.lifecycleObserved; reserved.ratioSemantics=actionOptions.oracleRatioSemantics; reserved.backend=actionOptions.oracleBackend; reserved.totalAnonProxyBytes=watermark.totalAnonProxyBytes; reserved.currentVmSwapBytes=watermark.currentVmSwapBytes; reserved.estimatedIncrementBytes=watermark.estimatedIncrementBytes; reserved.estimatedRetainedBytes=watermark.estimatedRetainedBytes; reserved.preparationWorkMode=workMode; reserved.readyFreeTargetBytes=oracleEventFreeTarget(actionOptions,request.needed); reserved.observedFreeBytes=sys.free; reserved.freeGapBytes=sub(*reserved.readyFreeTargetBytes,sys.free); if(donorAnonResidentBytes) reserved.estimatedDonorAnonRoomBytes=sub(sub(donorAnonResidentBytes,oracleDeviceDonorTotalKeepFloor(actionOptions,donor.bundle,appIt->keep)),64*MiB); reserved.status="reserved_before_write_pending_result"; if(deviceEvidence) deviceEvidence->recordReservation(actionId,request); appendOracleLedger(actionOptions,reserved);
                                if(actionOptions.oracleBackend=="proc_file_writeback") {
                                    const Bytes workEstimate=std::max(donorBytes,donor.writebackFileSize);
                                    event(eventLog,"INFO","oracle",donor.bundle+" writeback_budget action_id="+actionId+
                                        " work_estimate_bytes="+std::to_string(workEstimate)+
                                        " reservation_margin_bytes="+std::to_string(nominal-workEstimate)+
                                        " nominal_reserved_bytes="+std::to_string(nominal)+
                                        " nominal_semantics=charged_estimate_plus_margin observed_rss_delta_is_separate=true");
                                }
                                oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                const double reservedAt=monotonic();
                                oracleGlobalLimit.charge(reservedAt,nominal); oracleCaseLimits[request.caseId].charge(reservedAt,nominal); oracleAppLimits[donor.bundle].charge(reservedAt,nominal);
                                lastOracleSubmit=reservedAt; oracleActionDeadline=request.launch; oracleActionEvent=request.id; oracleActionId=actionId; oracleNextDonor[request.id]=(donorIndex+1)%donorCount;
                                if(donorAttempt.priorityDiskOnly && actionOptions.oracleBackend=="force_swapout") {
                                    oracleDiskPriorityActionIds[targetKey]=actionId;
                                }
                                tickDecision="oracle_reserved_and_queued"; tickTarget=donor.bundle; tickBudget=nominal;
                                oracleAction.emplace(std::async(std::launch::async,[&,app=*appIt,donor,request,ratio,priorSuccessfulRatio,nominal,eventGap,donorBytes,forecast,actionId,actionOptions,plannedIncrement]{ return performOracleAction(actionOptions,app,donor,request,ratio,priorSuccessfulRatio,nominal,eventGap,donorBytes,forecast,actionId,plannedIncrement,deviceEvidence); }));
                                if(actionOptions.oracleProfile=="peak_transition_os_snapshot") oracleFastPollActive=true;
                                break;
                            } catch(const std::exception& e) { event(eventLog,"ERROR","oracle",donor.bundle+" action preflight failed: "+e.what()); oracleHaltedEvents.insert(request.id); break; }
                        }
                        if(!donorsProtectedUnique.empty()) event(eventLog,"INFO","oracle","protected_donor_count_unique="+std::to_string(donorsProtectedUnique.size()));
                        if(donorsBelowFloorUnique.size()==donorCount && options.oracleBackend!="force_swapout" && !oracleHaltedEvents.count(request.id)) {
                            OracleActionResult skipped; skipped.actionId=request.id+"_capacity_skip_"+std::to_string(unixMillis()); skipped.eventId=request.id; skipped.caseId=request.caseId; skipped.deadline=request.launch; skipped.needed=request.needed; skipped.readyFreeTargetBytes=oracleEventFreeTarget(options,request.needed); skipped.observedFreeBytes=sys.free; skipped.freeGapBytes=sub(*skipped.readyFreeTargetBytes,sys.free); skipped.status=isProcFileBackend(options.oracleBackend)?"skipped_profile_targets_exhausted_or_file_estimate_over_budget":"skipped_both_donors_below_min_batch_or_budget_floor"; appendOracleLedger(options,skipped); oracleHaltedEvents.insert(request.id);
                        }
                        if(donorsAtRatioCeilingUnique.size()==donorCount && !oracleHaltedEvents.count(request.id)) {
                            OracleActionResult skipped; skipped.actionId=request.id+"_ratio_ceiling_skip_"+std::to_string(unixMillis()); skipped.eventId=request.id; skipped.caseId=request.caseId; skipped.deadline=request.launch; skipped.needed=request.needed; skipped.status="skipped_no_ratio_increase_after_positive_feedback"; appendOracleLedger(options,skipped); oracleHaltedEvents.insert(request.id);
                        }
                    }
                }
            } catch(const std::exception& e) { event(eventLog,"ERROR","oracle_schedule",e.what()); }
        }
        if(fullObservationTick) systemLog.row({std::to_string(unixMillis()),Schema,std::to_string(sys.total),std::to_string(sys.available),  //lzx-note-normal
            std::to_string(sys.free),std::to_string(sys.swapTotal),std::to_string(sys.swapFree),
            std::to_string(forecast.predictedAvailable),std::to_string(static_cast<long long>(forecast.declineBytesPS)),
            memoryPsi,hasMemPsi?"present_unverified":"unknown",ioPsi,hasIoPsi?"present_unverified":"unknown",
            controlState=="unknown"?"control_unknown":controlState=="paused"?"paused":tickDecision,
            tickBudget?std::to_string(tickBudget):std::string{},tickDecision,tickTarget});
        if(fullObservationTick && headroomLog) {
            const auto optionalBytes=[](const std::optional<Bytes>& value) { return value?std::to_string(*value):std::string{}; };
            headroomLog->row({std::to_string(unixMillis()),std::to_string(sys.time),std::to_string(sys.available),
                std::to_string(sys.free),std::to_string(sys.cached),std::to_string(sys.dirty),optionalBytes(sys.writebackBytes),
                optionalBytes(sys.anonPagesBytes),optionalBytes(sys.shmemBytes),std::to_string(sys.swapFree),
                sys.oomKillCnt?"true":"false",optionalBytes(sys.oomKillCnt)});
        }
        const unsigned loopIntervalMs=oracleLoopIntervalMs(
            options.oracleProfile=="peak_transition_os_snapshot",oracleFastPollActive,
            options.oracleProfilePollIntervalMs,static_cast<unsigned>(options.intervalMs));
        deadline+=std::chrono::milliseconds(loopIntervalMs);
        const auto sleepCheckedAt=std::chrono::steady_clock::now();
        if(deadline<sleepCheckedAt) deadline=sleepCheckedAt;
        // Shorten only the device event's next wake to the unchanged action-start
        // gate. This does not authorize an action or wait on a worker/provider.
        const double normalSleep=std::chrono::duration<double>(deadline-sleepCheckedAt).count();
        const double alignedSleep=oracleAlignedPollWaitSeconds(
            options.oracleProofSource=="device" && options.oracleProfile=="peak_transition_os_snapshot" && oracleFastPollActive,
            normalSleep,monotonic(),lastOracleSubmit,oracleSubmitInterval,oracleActionDeadline);
        if(alignedSleep<normalSleep) deadline=sleepCheckedAt+
            std::chrono::duration_cast<std::chrono::steady_clock::duration>(std::chrono::duration<double>(alignedSleep));
        while(!stopping() && std::chrono::steady_clock::now()<deadline) {
            const auto remain=deadline-std::chrono::steady_clock::now();
            std::this_thread::sleep_for(std::min(remain,std::chrono::duration_cast<decltype(remain)>(std::chrono::milliseconds(100))));  //lzx-note-normal
        }
    }
    if(dailyExecutor) {
        dailyExecutor->join(); // May wait for an already blocked kernel write; never detach.
        if(const auto result=dailyExecutor->poll()) acceptDaily(*result);
    }
    event(eventLog,"INFO","daemon",stopping()?"stopped by SIGINT/SIGTERM":"observation finished");
    if(oracleAction) {
        try { const auto result=oracleAction->get(); appendOracleLedger(options,result); event(eventLog,"INFO",result.bundle,"oracle final ledger action_id="+result.actionId+" status="+result.status+" overlap="+(result.overlap?"true":"false")); }
        catch(const std::exception& e) { event(eventLog,"ERROR","oracle_ledger",e.what()); ++samplingErrors; }
        oracleAction.reset();
    }
    return samplingErrors?2:0;
}
} // namespace ms::harmony_v1
