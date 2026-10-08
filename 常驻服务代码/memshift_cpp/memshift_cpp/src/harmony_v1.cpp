#include "harmony_v1.hpp"
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

namespace ms::harmony_v1 {
namespace fs = std::filesystem;
namespace {
constexpr const char* Schema = "harmony-v1-observe-3";
constexpr Bytes ProbeLimit = 128*MiB;
constexpr Bytes KeepSlack = 128*1024;

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

struct ProcessAggregate {
    std::size_t listed=0, statusRead=0, smapsRead=0;
    std::optional<Bytes> rssKiB, vmSwapKiB, rssAnonKiB, rssFileKiB;
    std::optional<Bytes> pssKiB, pssAnonKiB, pssFileKiB, privateKiB, smapsSwapKiB;
    Bytes minfltDelta=0, majfltDelta=0;
    std::size_t deltaMatched=0;
};

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

ProcessAggregate sampleProcesses(const std::vector<std::uint64_t>& ids,
    const std::map<std::uint64_t,ProcStat>& previous,std::map<std::uint64_t,ProcStat>& current) {
    ProcessAggregate total; total.listed=ids.size();
    std::optional<Bytes> rss,swap,rssAnon,rssFile,pss,pssAnon,pssFile,privatePages,smapsSwap;
    bool statusComplete=true,smapsComplete=true;
    for(const auto pid:ids) {
        try {
            const auto status=parseProcMemory(readPath("/proc/"+std::to_string(pid)+"/status"),{});
            ++total.statusRead;
            if(!status.rssKiB || !status.vmSwapKiB || !status.rssAnonKiB || !status.rssFileKiB) statusComplete=false;
            addMetric(rss,status.rssKiB); addMetric(swap,status.vmSwapKiB);
            addMetric(rssAnon,status.rssAnonKiB); addMetric(rssFile,status.rssFileKiB);
        } catch(const std::exception&) { statusComplete=false; }
        try {
            const auto smaps=parseProcMemory({},readPath("/proc/"+std::to_string(pid)+"/smaps_rollup"));
            if(!smaps.pssKiB || !smaps.pssAnonKiB || !smaps.pssFileKiB || !smaps.privateKiB || !smaps.smapsSwapKiB) smapsComplete=false;
            addMetric(pss,smaps.pssKiB); addMetric(pssAnon,smaps.pssAnonKiB);
            addMetric(pssFile,smaps.pssFileKiB); addMetric(privatePages,smaps.privateKiB);
            addMetric(smapsSwap,smaps.smapsSwapKiB); ++total.smapsRead;
        } catch(const std::exception&) { smapsComplete=false; }
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

bool backgroundEligible(int dir,const std::string& appPath,const std::vector<std::uint64_t>& expectedIds,
                       const std::map<std::uint64_t,ProcStat>& sampled) {
    if(expectedIds.empty() || processIds(dir)!=expectedIds) return false;
    const auto groupScore=maybeAt(dir,"memory.app_score");
    if(!groupScore || uintValue(trim(*groupScore))==0) return false;
    for(const auto pid:expectedIds) {
        const auto prior=sampled.find(pid);
        if(prior==sampled.end()) return false;
        const std::string proc="/proc/"+std::to_string(pid);
        const auto now=parseProcStat(pid,readPath(proc+"/stat"));
        if(now.startTicks!=prior->second.startTicks) return false;
        const auto cg=readPath(proc+"/cgroup");
        if(!processInAppCgroup(cg,appPath)) return false;
        const auto score=trim(readPath(proc+"/oom_score_adj"));
        std::size_t used=0; const auto value=std::stol(score,&used);
        if(used!=score.size() || value<=0) return false;
    }
    return true;
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

struct OracleDonor {
    std::string bundle, uid, aaState, evidenceSource;
    std::uint64_t pid=0, start=0, aaPid=0, aaUid=0, procStart=0;
    bool coldReady=false,windowReady=false, windowVisible=true,windowFocused=true,lifecycleClear=false,procVerified=false;
    double coldReadyAt=0,backgroundSince=0,lastHeartbeat=0;
    double aaObserved=0,windowObserved=0,procObserved=0,lifecycleObserved=0;
};
struct OracleRequest { std::string id, caseId; double launch=0; Bytes needed=0; OracleDonor donors[2]; };
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
    const std::string expected=base+prefixedFields("donor_a_",donorFields)+"|"+prefixedFields("donor_b_",donorFields);
    if(trim(header)!=expected) throw std::runtime_error("oracle schedule header mismatch");
    std::string extra; if(std::getline(in,extra) && !trim(extra).empty()) throw std::runtime_error("oracle schedule must contain exactly one request");
    const auto f=splitPipe(row); if(f.size()!=48) throw std::runtime_error("oracle schedule field count mismatch");
    OracleRequest r; r.id=f[0]; r.caseId=f[1]; r.launch=realValue(f[2]); r.needed=uintValue(f[3]);
    if(!safeName(r.id) || !safeName(r.caseId) || r.launch<=0 || r.needed==0) throw std::runtime_error("oracle event fields invalid");
    for(int i=0;i<2;++i) { const auto n=static_cast<std::size_t>(4+i*22); auto& d=r.donors[i];
        d.bundle=f[n]; d.uid=f[n+1]; d.pid=uintValue(f[n+2]); d.start=uintValue(f[n+3]); d.coldReady=boolValue(f[n+4]); d.windowReady=boolValue(f[n+5]); d.coldReadyAt=realValue(f[n+6]); d.backgroundSince=realValue(f[n+7]); d.lastHeartbeat=realValue(f[n+8]);
        d.aaPid=uintValue(f[n+9]); d.aaUid=uintValue(f[n+10]); d.aaState=f[n+11]; d.aaObserved=realValue(f[n+12]); d.windowVisible=boolValue(f[n+13]); d.windowFocused=boolValue(f[n+14]); d.windowObserved=realValue(f[n+15]); d.procVerified=boolValue(f[n+16]); d.procStart=uintValue(f[n+17]); d.procObserved=realValue(f[n+18]); d.lifecycleClear=boolValue(f[n+19]); d.lifecycleObserved=realValue(f[n+20]); d.evidenceSource=f[n+21];
        if(d.bundle.empty() || d.bundle.find('/')!=std::string::npos || d.uid.empty() || !d.pid || !d.start || !d.coldReady || !d.windowReady || d.coldReadyAt<=0 || d.backgroundSince<=0 || d.lastHeartbeat<0 || d.backgroundSince<d.coldReadyAt || d.aaPid!=d.pid || d.aaUid!=uintValue(d.uid) || d.aaState!="BACKGROUND" || d.aaObserved<=0 || d.windowVisible || d.windowFocused || d.windowObserved<=0 || !d.procVerified || d.procStart!=d.start || d.procObserved<=0 || !d.lifecycleClear || d.lifecycleObserved<=0 || (d.evidenceSource!="hap_heartbeat" && d.evidenceSource!="os_snapshot"))
            throw std::runtime_error("oracle donor registration invalid");
    }
    if(r.donors[0].pid==r.donors[1].pid || r.donors[0].bundle==r.donors[1].bundle) throw std::runtime_error("oracle donor registrations duplicate");
    return r;
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
std::string oracleBackgroundFailure(const Options& o,const App& app,const OracleDonor& donor,double now) {
    if(now<donor.coldReadyAt) return "clock_before_cold_ready";
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
    if(donor.lastHeartbeat>now) return "heartbeat_timestamp_in_future";
    if(donor.evidenceSource=="hap_heartbeat" && donor.lastHeartbeat<=0) return "heartbeat_telemetry_missing";
    std::vector<std::pair<std::string,double>> observations={{"aa",donor.aaObserved},{"window",donor.windowObserved},
       {"proc",donor.procObserved},{"lifecycle",donor.lifecycleObserved}};
    if(donor.evidenceSource=="hap_heartbeat") observations.emplace_back("heartbeat",donor.lastHeartbeat);
    const auto freshness=oracleFreshnessFailureImpl(true,now,observations,2.5);
    if(!freshness.empty()) return freshness;
    const std::string a1="io.memscene.scene4.workload.a1",a2="io.memscene.scene4.workload.a2";
    if(donor.bundle!=a1 && donor.bundle!=a2) return "donor_bundle_not_allowlisted";
    const auto slash=app.path.find_last_of('/'); const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
    if(leaf!=donor.bundle+"_"+donor.uid) return "cgroup_leaf_mismatch";
    FD root(::open(o.root.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW)); if(root.get()<0) return "cgroup_root_unavailable";
    FD dir(openBeneath(root.get(),app.path)); if(dir.get()<0) return "cgroup_leaf_unavailable";
    const auto ids=processIds(dir.get());
    if(ids.size()!=1) return "cgroup_member_count_changed";
    if(ids.front()!=donor.pid) return "cgroup_pid_mismatch";
    const auto current=maybeAt(dir.get(),"memory.current"); if(!current) return "memory_current_telemetry_missing";
    if(uintValue(trim(*current))<512*MiB) return "min_keep_below_floor";
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
    return {};
}
bool freshOracleBackground(const Options& o,const App& app,const OracleDonor& donor,double now) {
    return oracleBackgroundFailure(o,app,donor,now).empty();
}
bool sameOracleRegistration(const OracleRequest& a,const OracleRequest& b) {
    if(a.id!=b.id || a.caseId!=b.caseId || a.launch!=b.launch || a.needed!=b.needed) return false;
    for(int i=0;i<2;++i) { const auto& x=a.donors[i]; const auto& y=b.donors[i];
        if(x.bundle!=y.bundle || x.uid!=y.uid || x.pid!=y.pid || x.start!=y.start || x.coldReady!=y.coldReady || x.windowReady!=y.windowReady || x.coldReadyAt!=y.coldReadyAt || x.backgroundSince!=y.backgroundSince || y.lastHeartbeat<x.lastHeartbeat) return false;
    }
    return true;
}
struct OracleActionResult { std::string actionId,eventId,caseId,bundle,status,evidenceSource,feedbackSampleStatus,ratioSemantics="resident_incremental"; std::uint64_t pid=0,start=0; std::string uid; double deadline=0,elapsedMs=0,aaObserved=0,windowObserved=0,procObserved=0,lifecycleObserved=0; std::optional<double> syscallStartBoot,syscallReturnBoot,writeDurationMs,feedbackCompleteBoot; std::optional<bool> writeOverlapsDeadline,feedbackOverlapsDeadline; Bytes needed=0,remaining=0,nominal=0,input=0,feedbackDrop=0,preRssAnonKiB=0,postRssAnonKiB=0,totalAnonProxyBytes=0,currentVmSwapBytes=0,estimatedIncrementBytes=0,estimatedRetainedBytes=0; std::int64_t signedRssDeltaKiB=0; unsigned ratio=0,plannedRatio=0; long writeLength=-1; int errorNo=0; bool feedbackIdentityVerified=false,overlap=false; };
OracleActionResult performOracleAction(const Options& o,const App& app,const OracleDonor& donor,const OracleRequest& req,unsigned ratio,unsigned priorSuccessfulRatio,Bytes nominal,Bytes remaining,Bytes input,const std::string& actionId) {
    OracleActionResult r; r.actionId=actionId; r.eventId=req.id; r.caseId=req.caseId; r.bundle=donor.bundle; r.pid=donor.pid; r.uid=donor.uid; r.start=donor.start;
    r.deadline=req.launch; r.needed=req.needed; r.remaining=remaining; r.nominal=nominal; r.ratio=ratio; r.plannedRatio=ratio; r.ratioSemantics=o.oracleRatioSemantics; r.input=input;
    for(const auto& registered:req.donors) if(registered.bundle==donor.bundle) { r.evidenceSource=registered.evidenceSource; r.aaObserved=registered.aaObserved; r.windowObserved=registered.windowObserved; r.procObserved=registered.procObserved; r.lifecycleObserved=registered.lifecycleObserved; }
    const double begin=monotonic();
    bool writeAttempted=false, writeComplete=false;
    try {
        if(begin>=req.launch) throw std::runtime_error("event_deadline_passed_before_action_start");
        auto gate=parseControl(readPath(o.control)); if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle)) throw std::runtime_error("runtime_control_paused_or_protected");
        const auto latest=parseOracleRequest(readPath(o.oracleSchedule));
        if(!sameOracleRegistration(req,latest)) throw std::runtime_error("latest_schedule_registration_changed");
        const OracleDonor* live=nullptr; for(const auto& item:latest.donors) if(item.bundle==donor.bundle) live=&item;
        if(!live) throw std::runtime_error("oracle_donor_missing_before_write");
        const auto liveFailure=oracleBackgroundFailure(o,app,*live,begin);
        if(!liveFailure.empty()) throw std::runtime_error((oracleFreshnessFailureRetryableImpl(liveFailure)?"oracle_evidence_defer:":"oracle_evidence_reject:")+liveFailure);
        FD root(::open(o.root.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW)); if(root.get()<0) throw error("open oracle cgroup root");
        FD dir=openBeneath(root.get(),app.path);
        FD writer(::openat(dir.get(),"memory.force_shrink_all",O_WRONLY|O_CLOEXEC|O_NOFOLLOW)); if(writer.get()<0) throw error("open memory.force_shrink_all");
        const double writeNow=monotonic(); if(writeNow>=req.launch) throw std::runtime_error("event_deadline_passed_before_write");
        gate=parseControl(readPath(o.control)); if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle)) throw std::runtime_error("runtime_control_changed_before_write");
        const auto immediatelyLatest=parseOracleRequest(readPath(o.oracleSchedule));
        if(!sameOracleRegistration(latest,immediatelyLatest)) throw std::runtime_error("latest_schedule_changed_immediately_before_write");
        const OracleDonor* immediatelyLive=nullptr; for(const auto& item:immediatelyLatest.donors) if(item.bundle==donor.bundle) immediatelyLive=&item;
        if(!immediatelyLive) throw std::runtime_error("oracle_donor_missing_immediately_before_write");
        const auto immediateFailure=oracleBackgroundFailure(o,app,*immediatelyLive,monotonic());
        if(!immediateFailure.empty()) throw std::runtime_error((oracleFreshnessFailureRetryableImpl(immediateFailure)?"oracle_evidence_defer:":"oracle_evidence_reject:")+immediateFailure);
        r.evidenceSource=immediatelyLive->evidenceSource; r.aaObserved=immediatelyLive->aaObserved; r.windowObserved=immediatelyLive->windowObserved; r.procObserved=immediatelyLive->procObserved; r.lifecycleObserved=immediatelyLive->lifecycleObserved;
        const auto preSample=sampleOracleRss(*immediatelyLive,app);
        if(!preSample) throw std::runtime_error("prewrite_rss_identity_sample_unavailable");
        r.preRssAnonKiB=preSample->rssAnonKiB; r.feedbackSampleStatus="prewrite_identity_verified";
        // Proc sampling is separate from the OS/window/lifecycle evidence
        // refresh. Re-read control and the full current schedule after it, so
        // a slow sample cannot carry stale background authorization to write.
        const double finalGateTime=monotonic();
        if(finalGateTime>=req.launch) throw std::runtime_error("event_deadline_passed_after_prewrite_rss_sample");
        gate=parseControl(readPath(o.control));
        if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle))
            throw std::runtime_error("runtime_control_changed_after_prewrite_rss_sample");
        const auto finalLatest=parseOracleRequest(readPath(o.oracleSchedule));
        if(!sameOracleRegistration(req,finalLatest))
            throw std::runtime_error("latest_schedule_registration_changed_after_prewrite_rss_sample");
        const OracleDonor* finalLive=nullptr;
        for(const auto& item:finalLatest.donors) if(item.bundle==donor.bundle) finalLive=&item;
        const double finalFreshnessTime=monotonic();
        if(!finalLive) throw std::runtime_error("oracle_donor_missing_after_prewrite_rss_sample");
        const auto finalFailure=oracleBackgroundFailure(o,app,*finalLive,finalFreshnessTime);
        if(!finalFailure.empty()) throw std::runtime_error((oracleFreshnessFailureRetryableImpl(finalFailure)?"oracle_evidence_defer:":"oracle_evidence_reject:")+finalFailure);
        unsigned writeRatio=ratio;
        if(o.oracleRatioSemantics=="cumulative_anon_watermark") {
            if(!preSample->vmSwapKiB) throw std::runtime_error("prewrite_vmswap_telemetry_missing");
            const Bytes liveResident=preSample->rssAnonKiB*1024;
            const Bytes prewriteCap=std::min(nominal,sub(liveResident,576*MiB));
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
        const auto payload=std::to_string(writeRatio);
        const double syscallStart=monotonic();
        if(syscallStart>=req.launch) throw std::runtime_error("event_deadline_passed_at_syscall_start");
        r.syscallStartBoot=syscallStart;
        writeAttempted=true; errno=0; const auto count=::write(writer.get(),payload.data(),payload.size());
        const int writeErrno=count<0?errno:0;
        const double syscallReturn=monotonic();
        r.syscallReturnBoot=syscallReturn;
        r.writeDurationMs=(syscallReturn-syscallStart)*1000.0;
        r.writeOverlapsDeadline=oracleWriteCrossedDeadline(syscallStart,syscallReturn,req.launch);
        r.errorNo=writeErrno; r.writeLength=static_cast<long>(count); r.elapsedMs=(syscallReturn-begin)*1000;
        writeComplete=count==static_cast<ssize_t>(payload.size());
        if(count!=static_cast<ssize_t>(payload.size())) r.status="write_failed_or_partial_no_retry";
        else {
            r.status="write_accepted_feedback_unknown";
            if(o.oracleFeedbackDelayMs>0)
                std::this_thread::sleep_for(std::chrono::milliseconds(o.oracleFeedbackDelayMs));
            const auto postSample=sampleOracleRss(*finalLive,app);
            const auto feedback=compareOracleRssSamples(donor.pid,uintValue(donor.uid),donor.start,preSample,postSample);
            r.feedbackIdentityVerified=feedback.identityVerified;
            if(postSample && feedback.identityVerified) {
                r.postRssAnonKiB=postSample->rssAnonKiB;
                r.signedRssDeltaKiB=feedback.signedDeltaKiB;
                r.feedbackDrop=feedback.dropBytes;
                if(feedback.positiveDrop) {
                    r.feedbackSampleStatus="verified_positive_process_rss_delta_observation";
                    r.status="write_accepted_feedback_measured_positive_rss_observation";
                } else {
                    r.feedbackSampleStatus=feedback.signedDeltaKiB==0?"verified_zero_delta_halt_unknown_effect":"verified_rss_growth_halt_unknown_effect";
                    r.status="write_accepted_feedback_nonpositive_halt";
                }
            } else {
                r.feedbackSampleStatus="postwrite_identity_or_rss_sample_unknown_halt";
            }
            const auto postReq=parseOracleRequest(readPath(o.oracleSchedule));
            const OracleDonor* postDonor=nullptr; for(const auto& item:postReq.donors) if(item.bundle==donor.bundle) postDonor=&item;
            if(!sameOracleRegistration(req,postReq) || !postDonor || !freshOracleBackground(o,app,*postDonor,monotonic())) r.status="write_accepted_identity_or_background_changed_postwrite";
            const double feedbackComplete=monotonic();
            r.feedbackCompleteBoot=feedbackComplete;
            r.feedbackOverlapsDeadline=oracleFeedbackCrossedDeadline(feedbackComplete,req.launch);
        }
    } catch(const std::exception& e) {
        if(writeAttempted) r.status=writeComplete?"write_accepted_feedback_unknown_after_exception":"write_failed_or_partial_no_retry";
        else { const std::string reason=e.what(); r.status=reason.rfind("oracle_evidence_defer:",0)==0?std::string("rejected:")+reason:std::string("rejected:")+reason; r.errorNo=errno; }
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

std::string oracleFreshnessFailure(bool structuralEvidenceValid,double now,
    const std::vector<std::pair<std::string,double>>& observations,double maxAgeSeconds) {
    return oracleFreshnessFailureImpl(structuralEvidenceValid,now,observations,maxAgeSeconds);
}
bool oracleFreshnessFailureRetryable(const std::string& reason) {
    return oracleFreshnessFailureRetryableImpl(reason);
}

void validateOracleScheduleText(const std::string& text) { (void)parseOracleRequest(text); }

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
        return OracleRssSample{pid,expectedUid,expectedStartTicks,*memory.rssAnonKiB,memory.vmSwapKiB};
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
    return status=="write_accepted_feedback_measured_positive_rss_observation" &&
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

OracleWatermarkPlan oracleCumulativeWatermarkForCap(Bytes residentAnonBytes,
    Bytes currentVmSwapBytes,Bytes capBytes,unsigned maxTargetRatio) {
    OracleWatermarkPlan plan;
    if(residentAnonBytes==0 || maxTargetRatio==0 || maxTargetRatio>90 ||
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

bool oracleRatioCanAdvance(unsigned priorSuccessfulRatio,unsigned candidateRatio) {
    return candidateRatio>0 && candidateRatio>priorSuccessfulRatio;
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
                const bool a1=leaf.rfind("io.memscene.scene4.workload.a1_",0)==0;
                const bool a2=leaf.rfind("io.memscene.scene4.workload.a2_",0)==0;
                if(!a1 && !a2) throw std::runtime_error("oracle mode may reclaim only exact scene4 self-donor a1/a2 bundle leaves");
                ++selfDonors;
            }
            if(selfDonors!=2) throw std::runtime_error("oracle mode requires exactly two reclaim-enabled scene4 self-donors");
            if(options.oracleSchedule==options.oracleLedger) throw std::runtime_error("oracle schedule and action ledger must be separate files");
        }
    }
    for (const auto& app : config.apps)
        if (!safeRelative(app.path)) throw std::runtime_error("unsafe app path: " + app.name);
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
    event(eventLog,"INFO","daemon",oracleProfile?"backend=harmony-v1-ratio; isolated scene4 oracle enabled; percent input is empirical and is not a physical byte guarantee":ratioProfile?(probeProfile?"backend=harmony-v1-ratio-probe; HongMeng 1.13.0 empirical 1..20 ratio; sole verified workload only; scalar is not bytes":"backend=harmony-v1-ratio; HongMeng 1.13.0 empirical ratio profile; allowlisted app_score and per-process oom_score_adj gates required"):
        "backend=harmony-v1; schema="+std::string(Schema)+"; observation only; no candidate budget or reclaim action");
    event(eventLog,"INFO","cgroup_root","mount="+rootInfo.mountPoint+" validated_via_alias="+(rootInfo.alias?"true":"false")+
          " statfs_magic_raw="+std::to_string(rootInfo.magic)+"; proc topology and controller files validated");
    event(eventLog,"INFO","schema","system.csv/apps.csv use harmony-v1-observe-3 schema and differ from Linux cgroup-v2 logs");
    if(controlState=="unknown") event(eventLog,"WARN","control","control file unavailable or invalid; control state unknown");

    std::vector<std::map<std::uint64_t,ProcStat>> previousProcesses(config.apps.size());
    std::uint64_t tick=0;
    double nextProbeAction=0;
    RollingLimit globalProbeMinute;
    std::vector<RollingLimit> appProbeMinute(config.apps.size());
    const double daemonStart=monotonic();
    std::vector<Bytes> lastFeedbackAnonKiB(config.apps.size(),0);
    std::vector<bool> haveFeedback(config.apps.size(),false);
    bool actionThisTick=false;
    std::string tickDecision=ratioProfile?"ratio_profile_idle":"observation_only",tickTarget;
    Bytes tickBudget=0;
    auto deadline=std::chrono::steady_clock::now();
    int samplingErrors=0;
    std::optional<std::future<OracleActionResult>> oracleAction;
    std::map<std::string,std::string> oracleFingerprints;
    std::set<std::string> oracleHaltedEvents, oracleExpiredEvents, oracleUnknownDeadlineRows;
    std::map<std::string,unsigned> oracleNextDonor;
    std::map<std::string,unsigned> oraclePositiveRatioByEventDonor;
    std::map<std::string,double> oracleDonorBlockedUntil;
    std::map<std::string,std::string> oracleActiveEventForDonor;
    std::map<std::string,std::pair<std::uint64_t,std::uint64_t>> oracleActiveIdentityForDonor;
    RollingLimit oracleGlobalLimit;
    std::map<std::string,RollingLimit> oracleCaseLimits,oracleAppLimits;
    double lastOracleSubmit=0;
    double oracleActionDeadline=0;
    std::string oracleActionEvent,oracleActionId;
    while(!stopping() && (!ticks || tick<ticks)) {
        if(oracleAction && oracleAction->wait_for(std::chrono::milliseconds(0))==std::future_status::ready) {
            try { const auto result=oracleAction->get(); appendOracleLedger(options,result); event(eventLog,"INFO",result.bundle,"oracle action_id="+result.actionId+" status="+result.status+" nominal_bytes="+std::to_string(result.nominal)+" overlap="+(result.overlap?"true":"false"));
                if(oracleFeedbackMayContinue(result.status,result.feedbackIdentityVerified,
                       result.signedRssDeltaKiB,result.postRssAnonKiB!=0)) {
                    const std::string ratioKey=result.eventId+"|"+result.bundle;
                    auto& priorRatio=oraclePositiveRatioByEventDonor[ratioKey];
                    priorRatio=std::max(priorRatio,result.ratio);
                    for(std::size_t i=0;i<config.apps.size();++i) {
                        const auto slash=config.apps[i].path.find_last_of('/');
                        const auto leaf=slash==std::string::npos?config.apps[i].path:config.apps[i].path.substr(slash+1);
                        if(leaf!=result.bundle+"_"+result.uid) continue;
                        lastFeedbackAnonKiB[i]=result.postRssAnonKiB; haveFeedback[i]=true; oracleActiveEventForDonor[config.apps[i].name]=result.eventId; oracleActiveIdentityForDonor[config.apps[i].name]={result.pid,result.start}; break;
                    }
                } else if(result.status.rfind("rejected:oracle_evidence_defer:",0)==0 && !result.overlap) {
                    for(const auto& app:config.apps) {
                        const auto slash=app.path.find_last_of('/');
                        const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
                        if(leaf==result.bundle+"_"+result.uid) { oracleDonorBlockedUntil[app.name]=monotonic()+0.5; break; }
                    }
                    for(std::size_t i=0;i<2;++i) {
                        const std::string bundle=i==0?"io.memscene.scene4.workload.a1":"io.memscene.scene4.workload.a2";
                        if(result.bundle==bundle) { oracleNextDonor[result.eventId]=static_cast<unsigned>(i); break; }
                    }
                    event(eventLog,"WARN",result.bundle,"oracle action deferred without write: "+result.status);
                } else oracleHaltedEvents.insert(result.eventId);
                if(result.overlap) oracleHaltedEvents.insert(result.eventId);
            }
            catch(const std::exception& e) { event(eventLog,"ERROR","oracle_ledger",e.what()); oracleHaltedEvents.insert(oracleActionEvent); ++samplingErrors; }
            oracleAction.reset();
        }
        if(oracleAction && monotonic()>=oracleActionDeadline && !oracleUnknownDeadlineRows.count(oracleActionId)) {
            OracleActionResult r; r.actionId=oracleActionId; r.eventId=oracleActionEvent; r.deadline=oracleActionDeadline; r.status="reserved_action_still_inflight_at_deadline_unknown"; r.overlap=true; appendOracleLedger(options,r); oracleUnknownDeadlineRows.insert(oracleActionId); oracleHaltedEvents.insert(oracleActionEvent);
        }
        ++tick;
        if(!options.control.empty()) {
            const std::string previousControlState=controlState;
            try {
                control=parseControl(readPath(options.control));
                controlState=control.paused?"paused":"active";
            } catch(const std::exception&) {
                control=Control{}; controlState="unknown";
            }
            if(controlState!=previousControlState)
                event(eventLog,"INFO","control","control state changed to "+controlState+"; sampling continues; writes gated by control");
        }
        System sys;
        try { sys=systemSample(options.proc); }
        catch(const std::exception& e) { ++samplingErrors; event(eventLog,"ERROR","system",e.what()); break; }
        const Forecast forecast=predictor.update(sys,options.horizonSec,options.maxDeclineMBps*MiB);
        tickDecision=ratioProfile?"ratio_profile_idle":"observation_only"; tickTarget.clear(); tickBudget=0;
        actionThisTick=false;
        Bytes controlBoost=0;
        if(ratioProfile && controlState=="active" && control.until>unixSeconds()) controlBoost=control.boost;
        const Bytes systemGap=deficit(options,sys,forecast,controlBoost);
        bool hasMemPsi=false,hasIoPsi=false;
        const auto memoryPsi=rawPSI(options.proc+"/pressure/memory",hasMemPsi);
        const auto ioPsi=rawPSI(options.proc+"/pressure/io",hasIoPsi);
        for(std::size_t appIndex=0;appIndex<config.apps.size();++appIndex) {
            const auto& app=config.apps[appIndex];
            try {
                FD dir=openBeneath(root.get(),app.path);
                const auto current=optionalRaw(dir.get(),"memory.current");
                const auto stat=maybeAt(dir.get(),"memory.stat");
                if(current.empty() || !stat) throw std::runtime_error("memory.current or memory.stat unreadable");
                const auto values=parseVendorStat(*stat);
                const auto ids=processIds(dir.get());
                std::map<std::uint64_t,ProcStat> currentProcesses;
                const auto processes=sampleProcesses(ids,previousProcesses[appIndex],currentProcesses);
                previousProcesses[appIndex]=currentProcesses;
            const Bytes residentAnonKiB=processes.rssAnonKiB.value_or(0);
            if(oracleProfile && app.reclaim && haveFeedback[appIndex]) {
                const auto eventIt=oracleActiveEventForDonor.find(app.name);
                const auto identityIt=oracleActiveIdentityForDonor.find(app.name);
                bool identityOk=processes.rssAnonKiB.has_value() && processes.listed==1 && processes.statusRead==1 && identityIt!=oracleActiveIdentityForDonor.end();
                if(identityOk) { const auto p=currentProcesses.find(identityIt->second.first); identityOk=p!=currentProcesses.end() && p->second.startTicks==identityIt->second.second; }
                if(!identityOk) {
                    if(eventIt!=oracleActiveEventForDonor.end()) oracleHaltedEvents.insert(eventIt->second);
                    oracleDonorBlockedUntil.erase(app.name);
                    haveFeedback[appIndex]=false;
                    oracleActiveEventForDonor.erase(app.name);
                    oracleActiveIdentityForDonor.erase(app.name);
                    event(eventLog,"WARN",app.name,"oracle donor identity or RSS telemetry changed/missing; event stopped");
                } else if(residentAnonKiB>lastFeedbackAnonKiB[appIndex]+1024) {
                    oracleDonorBlockedUntil[app.name]=monotonic()+options.backoffSec;
                    haveFeedback[appIndex]=false;
                    event(eventLog,"WARN",app.name,"oracle post-action resident anon recovered by >1MiB; donor cooldown");
                } else if(processes.majfltDelta>0 || processes.minfltDelta>1024) {
                    oracleDonorBlockedUntil[app.name]=monotonic()+options.backoffSec;
                    haveFeedback[appIndex]=false;
                    event(eventLog,"WARN",app.name,"oracle post-action major/minor fault guard; donor cooldown");
                }
            }
            if(ratioProfile && haveFeedback[appIndex] && residentAnonKiB>lastFeedbackAnonKiB[appIndex]+1024) {
                nextProbeAction=std::max(nextProbeAction,monotonic()+options.backoffSec);
                event(eventLog,"WARN",app.name,"resident anonymous RSS recovered by >1 MiB after reclaim; cooldown/backoff active");
                haveFeedback[appIndex]=false;
            }
            if(ratioProfile && haveFeedback[appIndex] && (processes.majfltDelta>0 || processes.minfltDelta>1024)) {
                nextProbeAction=std::max(nextProbeAction,monotonic()+options.backoffSec);
                event(eventLog,"WARN",app.name,"post-reclaim process fault activity observed; cooldown/backoff active");
                haveFeedback[appIndex]=false;
            }
            std::string reason=controlState=="unknown"?"control state unknown; observation only":
                    controlState=="paused"?"manual pause; observation only":
                    control.protectedApps.count(app.name)?"runtime protected; observation only":
                    "process counters are diagnostics only; coldness/reclaim units unverified; budget unavailable";
            std::string action="none",budgetText,ratioText;
            if(ratioProfile) {
                reason=app.reclaim?"ratio profile awaiting EWMA warmup":"app is not explicitly reclaim-enabled";
                if(controlState!="active") reason="control is not confirmed active";
                else if(!app.reclaim) reason="app is not explicitly reclaim-enabled";
                else if(control.protectedApps.count(app.name)) reason="app protected by runtime control";
                else if(!processes.rssAnonKiB || !processes.vmSwapKiB || processes.listed==0 || processes.statusRead!=processes.listed) reason="process telemetry incomplete";
                else if(tick<2 || monotonic()-daemonStart<options.warmupSec) reason="EWMA/startup warmup";
                else if(monotonic()<nextProbeAction) reason="probe cooldown";
                else if(actionThisTick) reason="one reclaim action per interval";
                else if(appProbeMinute[appIndex].used(monotonic())>=options.appPerMinute ||
                        globalProbeMinute.used(monotonic())>=options.perMinute) reason="ratio profile per-minute cap reached";
                else if(systemGap==0) {
                    if(!probeProfile && !backgroundEligible(dir.get(),app.path,ids,currentProcesses))
                        reason="protected: app_score/oom_score_adj/member identity does not confirm background eligibility";
                    else reason="forecast reserve satisfied";
                }
                else {
                    try {
                        std::optional<ProbeIdentity> probeIdentity;
                        if(probeProfile) probeIdentity=validateProbe(dir.get(),options.root,app.path);
                        else if(!backgroundEligible(dir.get(),app.path,ids,currentProcesses))
                            throw std::runtime_error("app_score/oom_score_adj/member identity does not confirm background eligibility");
                        if(!values.anonKiB) throw std::runtime_error("vendor Anon telemetry unavailable");
                        const Bytes residentAnon=*processes.rssAnonKiB*1024;
                        const Bytes ratioDenominator=*values.anonKiB*1024;
                        const Bytes donorAnon=std::min(ratioDenominator,residentAnon);
                        const Bytes keep=std::max(app.keep,KeepSlack);
                        const Bytes cap=std::min({systemGap,sub(donorAnon,keep),
                            options.batch,sub(options.appPerMinute,appProbeMinute[appIndex].used(monotonic())),
                            sub(options.perMinute,globalProbeMinute.used(monotonic()))});
                        const Bytes safeCap=sub(cap,KeepSlack);
                        budgetText=std::to_string(safeCap);
                        const unsigned ratio=ratioForBudget(safeCap,ratioDenominator);
                        ratioText=std::to_string(ratio);
                        const Bytes nominalRequest=(ratioDenominator*ratio)/100;
                        if(ratio<1) reason="budget below one-percent ratio quantum";
                        else if(nominalRequest<options.minBatch) reason="nominal request below configured min_batch";
                        else if(probeProfile && !sameProbe(dir.get(),options.root,app.path,*probeIdentity)) reason="probe PID identity changed before write";
                        else if(!probeProfile && !backgroundEligible(dir.get(),app.path,ids,currentProcesses)) reason="background eligibility or member identity changed before write";
                        else {
                            Control latest=parseControl(readPath(options.control));
                            if(latest.paused || latest.protectedApps.count(app.name)) reason="control pause/protection changed before write";
                            else {
                                FD writer(::openat(dir.get(),"memory.force_shrink_anon",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
                                if(writer.get()<0) throw error("open memory.force_shrink_anon");
                                const std::string payload=std::to_string(ratio);
                                const auto started=monotonic();
                                const auto count=::write(writer.get(),payload.data(),payload.size());
                                const int writeErr=count<0?errno:0;
                                const auto elapsed=(monotonic()-started)*1000;
                                if(count!=static_cast<ssize_t>(payload.size())) {
                                    action="write_error"; reason="vendor write failed errno="+std::to_string(writeErr);
                                    nextProbeAction=monotonic()+60.0; tickDecision="ratio_write_error";
                                } else {
                                    std::this_thread::sleep_for(std::chrono::milliseconds(250));
                                    appProbeMinute[appIndex].charge(monotonic(),safeCap);
                                    globalProbeMinute.charge(monotonic(),safeCap);
                                    nextProbeAction=monotonic()+std::max(10.0,options.cooldownSec);
                                    tickDecision="ratio_submitted"; tickTarget=app.name; tickBudget=safeCap; actionThisTick=true;
                                    action="accepted_ratio_"+payload;
                                    try {
                                        const auto postPids=processIds(dir.get());
                                        if(postPids!=ids || (probeProfile?!sameProbe(dir.get(),options.root,app.path,*probeIdentity):
                                           !backgroundEligible(dir.get(),app.path,ids,currentProcesses)))
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
                                        reason="write accepted; feedback unknown: "+std::string(e.what());
                                        nextProbeAction=monotonic()+60.0;
                                        event(eventLog,"WARN",app.name,reason);
                                    }
                                }
                            }
                        }
                    } catch(const std::exception& e) {
                        const std::string message=e.what();
                        if(message.find("app_score/oom_score_adj/member identity")!=std::string::npos) {
                            reason="protected: "+message; action="protected";
                        } else { reason=std::string("apply gate: ")+message; action="rejected"; }
                    }
                }
            }
            if(action!="none") { tickDecision=action; tickTarget=app.name; }
            appLog.row({std::to_string(unixMillis()),Schema,app.name,app.path,appPopulated(dir.get()),current,
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
        if(oracleProfile && !oracleAction && monotonic()-lastOracleSubmit>=1.0) {
            try {
                const auto request=parseOracleRequest(readPath(options.oracleSchedule));
                const double now=monotonic();
                std::ostringstream fingerprintOut; fingerprintOut<<request.caseId<<'|'<<std::setprecision(12)<<request.launch<<'|'<<request.needed;
                for(const auto& donor:request.donors) fingerprintOut<<'|'<<donor.bundle<<'|'<<donor.uid<<'|'<<donor.pid<<'|'<<donor.start<<'|'<<donor.coldReady<<'|'<<donor.windowReady<<'|'<<donor.coldReadyAt<<'|'<<donor.backgroundSince;
                const auto fingerprint=fingerprintOut.str();
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
                    else {
                        unsigned donorsBelowFloor=0,donorsAtRatioCeiling=0;
                        for(unsigned attempt=0;attempt<2;++attempt) {
                            const unsigned donorIndex=(oracleNextDonor[request.id]+attempt)%2; const auto& donor=request.donors[donorIndex];
                            const std::string expectedBundle=donorIndex==0?"io.memscene.scene4.workload.a1":"io.memscene.scene4.workload.a2";
                            const auto appIt=std::find_if(config.apps.begin(),config.apps.end(),[&](const App& app){
                                const auto slash=app.path.find_last_of('/'); const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
                                return app.reclaim && donor.bundle==expectedBundle && leaf.rfind(expectedBundle+"_",0)==0;
                            });
                            if(appIt==config.apps.end()) { event(eventLog,"ERROR","oracle","donor bundle/path is outside the exact a1/a2 self-workload allowlist"); oracleHaltedEvents.insert(request.id); break; }
                            const auto cooldown=oracleDonorBlockedUntil.find(appIt->name);
                            if(cooldown!=oracleDonorBlockedUntil.end() && now<cooldown->second) {
                                event(eventLog,"WARN","oracle",donor.bundle+" feedback cooldown remains active");
                                break;
                            }
                            if(control.protectedApps.count(appIt->name) || control.protectedApps.count(donor.bundle)) { event(eventLog,"WARN","oracle",donor.bundle+" is protected by runtime control"); oracleHaltedEvents.insert(request.id); break; }
                            if(!donor.coldReady) { event(eventLog,"WARN","oracle",donor.bundle+" guard_failure=cold_ready_false; halting event"); oracleHaltedEvents.insert(request.id); break; }
                            std::string guardFailure;
                            try { guardFailure=oracleBackgroundFailure(options,*appIt,donor,now); }
                            catch(const std::exception& e) { guardFailure=std::string("identity_probe_error:")+e.what(); }
                            if(!guardFailure.empty()) {
                                if(oracleFreshnessFailureRetryableImpl(guardFailure)) {
                                    event(eventLog,"WARN","oracle",donor.bundle+" guard_failure="+guardFailure+"; waiting_for_fresh_proof");
                                    break;
                                }
                                event(eventLog,"WARN","oracle",donor.bundle+" guard_failure="+guardFailure+"; halting_event"); oracleHaltedEvents.insert(request.id); break;
                            }
                            try {
                                FD appDir=openBeneath(root.get(),appIt->path);
                                const auto stat=maybeAt(appDir.get(),"memory.stat");
                                const auto anon=stat?parseVendorStat(*stat).anonKiB:std::optional<Bytes>{};
                                const auto processAnon=aggregateRssAnon({donor.pid});
                                if(!anon || !processAnon) throw std::runtime_error("anon_telemetry_missing");
                                const Bytes donorBytes=std::min(*anon*1024,*processAnon*1024);
                                const Bytes eventGap=deficit(options,sys,forecast,request.needed);
                                const Bytes swapRoom=sub(sys.swapFree,options.swapReserve);
                                const Bytes globalRoom=oracleGlobalLimit.room(now,options.oracleGlobalCap);
                                const Bytes caseRoom=oracleCaseLimits[request.caseId].room(now,options.oracleCaseCap);
                                const Bytes appRoom=oracleAppLimits[donor.bundle].room(now,options.oracleAppCap);
                                const Bytes rawCap=std::min({eventGap,swapRoom,sub(donorBytes,512*MiB),options.oracleBatch,globalRoom,caseRoom,appRoom});
                                const Bytes cap=sub(rawCap,64*MiB);
                                unsigned ratio=0;
                                Bytes nominal=0;
                                OracleWatermarkPlan watermark;
                                if(options.oracleRatioSemantics=="cumulative_anon_watermark") {
                                    const auto fresh=sampleOracleRss(donor,*appIt);
                                    if(!fresh || !fresh->vmSwapKiB) throw std::runtime_error("cumulative_watermark_proc_or_vmswap_unknown");
                                    const Bytes liveResident=fresh->rssAnonKiB*1024;
                                    const Bytes candidateCap=std::min(cap,sub(liveResident,576*MiB));
                                    watermark=oracleCumulativeWatermarkForCap(liveResident,*fresh->vmSwapKiB*1024,candidateCap,options.oracleMaxRatio);
                                    ratio=watermark.ratio;
                                    nominal=watermark.estimatedIncrementBytes;
                                } else {
                                    ratio=oracleRatioForCap(cap,donorBytes,options.oracleMaxRatio);
                                    nominal=donorBytes?(donorBytes*ratio)/100:0;
                                }
                                if(ratio==0 || nominal<options.minBatch) {
                                    ++donorsBelowFloor;
                                    continue;
                                }
                                const auto priorRatioIt=oraclePositiveRatioByEventDonor.find(request.id+"|"+donor.bundle);
                                const unsigned priorSuccessfulRatio=priorRatioIt==oraclePositiveRatioByEventDonor.end()?0:priorRatioIt->second;
                                if(priorSuccessfulRatio && !oracleRatioCanAdvance(priorSuccessfulRatio,ratio)) {
                                    ++donorsAtRatioCeiling;
                                    event(eventLog,"INFO","oracle",donor.bundle+" ratio_skipped_candidate="+std::to_string(ratio)+
                                          " prior_positive_ratio="+std::to_string(priorSuccessfulRatio)+" reason=no_strict_increase");
                                    continue;
                                }
                                if(monotonic()>=request.launch) break;
                                // Persist reservation before submitting. Missing completion later means this amount remains conservatively charged.
                                const std::string actionId=request.id+"_"+donor.bundle+"_"+std::to_string(unixMillis());
                                OracleActionResult reserved; reserved.actionId=actionId; reserved.eventId=request.id; reserved.caseId=request.caseId; reserved.bundle=donor.bundle; reserved.pid=donor.pid; reserved.uid=donor.uid; reserved.start=donor.start; reserved.deadline=request.launch; reserved.needed=request.needed; reserved.remaining=eventGap; reserved.input=donorBytes; reserved.ratio=ratio; reserved.plannedRatio=ratio; reserved.nominal=nominal; reserved.evidenceSource=donor.evidenceSource; reserved.aaObserved=donor.aaObserved; reserved.windowObserved=donor.windowObserved; reserved.procObserved=donor.procObserved; reserved.lifecycleObserved=donor.lifecycleObserved; reserved.ratioSemantics=options.oracleRatioSemantics; reserved.totalAnonProxyBytes=watermark.totalAnonProxyBytes; reserved.currentVmSwapBytes=watermark.currentVmSwapBytes; reserved.estimatedIncrementBytes=watermark.estimatedIncrementBytes; reserved.estimatedRetainedBytes=watermark.estimatedRetainedBytes; reserved.status="reserved_before_write_pending_result"; appendOracleLedger(options,reserved);
                                oracleGlobalLimit.charge(now,nominal); oracleCaseLimits[request.caseId].charge(now,nominal); oracleAppLimits[donor.bundle].charge(now,nominal);
                                lastOracleSubmit=now; oracleActionDeadline=request.launch; oracleActionEvent=request.id; oracleActionId=actionId; oracleNextDonor[request.id]=(donorIndex+1)%2;
                                tickDecision="oracle_reserved_and_queued"; tickTarget=donor.bundle; tickBudget=nominal;
                                oracleAction.emplace(std::async(std::launch::async,[&,app=*appIt,donor,request,ratio,priorSuccessfulRatio,nominal,eventGap,donorBytes,actionId]{ return performOracleAction(options,app,donor,request,ratio,priorSuccessfulRatio,nominal,eventGap,donorBytes,actionId); }));
                                break;
                            } catch(const std::exception& e) { event(eventLog,"ERROR","oracle",donor.bundle+" action preflight failed: "+e.what()); oracleHaltedEvents.insert(request.id); break; }
                        }
                        if(donorsBelowFloor==2 && !oracleHaltedEvents.count(request.id)) {
                            OracleActionResult skipped; skipped.actionId=request.id+"_capacity_skip_"+std::to_string(unixMillis()); skipped.eventId=request.id; skipped.caseId=request.caseId; skipped.deadline=request.launch; skipped.needed=request.needed; skipped.status="skipped_both_donors_below_min_batch_or_budget_floor"; appendOracleLedger(options,skipped); oracleHaltedEvents.insert(request.id);
                        }
                        if(donorsAtRatioCeiling==2 && !oracleHaltedEvents.count(request.id)) {
                            OracleActionResult skipped; skipped.actionId=request.id+"_ratio_ceiling_skip_"+std::to_string(unixMillis()); skipped.eventId=request.id; skipped.caseId=request.caseId; skipped.deadline=request.launch; skipped.needed=request.needed; skipped.status="skipped_no_ratio_increase_after_positive_feedback"; appendOracleLedger(options,skipped); oracleHaltedEvents.insert(request.id);
                        }
                    }
                }
            } catch(const std::exception& e) { event(eventLog,"ERROR","oracle_schedule",e.what()); }
        }
        systemLog.row({std::to_string(unixMillis()),Schema,std::to_string(sys.total),std::to_string(sys.available),
            std::to_string(sys.free),std::to_string(sys.swapTotal),std::to_string(sys.swapFree),
            std::to_string(forecast.predictedAvailable),std::to_string(static_cast<long long>(forecast.declineBytesPS)),
            memoryPsi,hasMemPsi?"present_unverified":"unknown",ioPsi,hasIoPsi?"present_unverified":"unknown",
            controlState=="unknown"?"control_unknown":controlState=="paused"?"paused":tickDecision,
            tickBudget?std::to_string(tickBudget):std::string{},tickDecision,tickTarget});
        deadline+=std::chrono::milliseconds(options.intervalMs);
        if(deadline<std::chrono::steady_clock::now()) deadline=std::chrono::steady_clock::now();
        while(!stopping() && std::chrono::steady_clock::now()<deadline) {
            const auto remain=deadline-std::chrono::steady_clock::now();
            std::this_thread::sleep_for(std::min(remain,std::chrono::duration_cast<decltype(remain)>(std::chrono::milliseconds(100))));
        }
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
