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

//lzx-note 
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
    std::string writebackFilePath;
    std::uint64_t pid=0, start=0, aaPid=0, aaUid=0, procStart=0;
    std::uint64_t writebackFileDev=0, writebackFileIno=0, writebackFileSize=0;
    bool coldReady=false,dailyReady=false,windowReady=false, windowVisible=true,windowFocused=true,lifecycleClear=false,procVerified=false;
    double readyAt=0,backgroundSince=0,lastHeartbeat=0;
    double aaObserved=0,windowObserved=0,procObserved=0,lifecycleObserved=0;
};
struct OracleRequest { std::string id, caseId; double launch=0; Bytes needed=0; unsigned schemaVersion=1; std::vector<OracleDonor> donors; };
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
        std::ostringstream expectedV2,expectedV3;
        expectedV2<<"schema_version|event_id|case_id|launch_boottime_s|planned_needed_bytes|donor_count";
        expectedV3<<"schema_version|event_id|case_id|launch_boottime_s|planned_needed_bytes|donor_count";
        for(unsigned i=1;i<=8;++i) {
            const auto prefix="donor_"+std::to_string(i)+"_";
            expectedV2<<'|'<<prefixedFields(prefix,fields);
            expectedV3<<'|'<<prefixedFields(prefix,fields+"|writeback_file_path|writeback_file_dev|writeback_file_ino|writeback_file_size");
        }
        const bool schema3=trim(header)==expectedV3.str();
        if(trim(header)!=expectedV2.str() && !schema3) throw std::runtime_error("oracle schedule header mismatch");
        if(f.size()<6 || (schema3?f[0]!="3":f[0]!="2")) throw std::runtime_error("oracle schedule schema version mismatch");
        r.schemaVersion=schema3?3U:2U; r.id=f[1]; r.caseId=f[2]; r.launch=realValue(f[3]); r.needed=uintValue(f[4]);
        const auto count=uintValue(f[5]); const std::size_t donorWidth=schema3?25U:21U;
        if(count<1 || count>8 || f.size()!=6+count*donorWidth) throw std::runtime_error(schema3?"oracle v3 donor count/field count mismatch":"oracle v2 donor count/field count mismatch");
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
            bool allowed=false; for(unsigned i=1;i<=8;++i) if(d.bundle=="io.memscene.scene4.workload.a"+std::to_string(i)) allowed=true;
            if(!allowed) throw std::runtime_error("oracle profile target is outside the exact a1..a8 allowlist");
        }
        if(!bundles.insert(d.bundle).second || !pids.insert(d.pid).second ||
            (r.schemaVersion>=2 && (!uids.insert(uintValue(d.uid)).second || !leaves.insert(d.bundle+"_"+d.uid).second)))
            throw std::runtime_error("oracle donor registrations duplicate");
    }
    if(r.schemaVersion>=2 && r.donors.size()>8) throw std::runtime_error("oracle profile donor limit exceeded");
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
    CgroupMemberObservation* memberObservation=nullptr) {
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
    if(!freshness.empty()) return freshness;
    const std::string prefix="io.memscene.scene4.workload.a";
    bool bundleAllowed=false;
    for(unsigned i=1;i<=(o.oracleProfile=="peak_transition_os_snapshot"?8U:2U);++i)
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
    const auto current=maybeAt(dir.get(),"memory.current"); if(!current) return "memory_current_telemetry_missing";
    const Bytes keepFloor=o.oracleRatioSemantics=="cumulative_anon_watermark"?o.oracleCumulativeKeep:512*MiB;
    if(uintValue(trim(*current))<keepFloor) return "min_keep_below_floor";
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
bool sameOracleRegistration(const OracleRequest& a,const OracleRequest& b) {
    if(a.id!=b.id || a.caseId!=b.caseId || a.launch!=b.launch || a.needed!=b.needed || a.schemaVersion!=b.schemaVersion || a.donors.size()!=b.donors.size()) return false;
    for(std::size_t i=0;i<a.donors.size();++i) { const auto& x=a.donors[i]; const auto& y=b.donors[i];
        if(x.bundle!=y.bundle || x.uid!=y.uid || x.pid!=y.pid || x.start!=y.start || x.coldReady!=y.coldReady || x.dailyReady!=y.dailyReady || x.windowReady!=y.windowReady || x.readyAt!=y.readyAt || x.backgroundSince!=y.backgroundSince || y.lastHeartbeat<x.lastHeartbeat ||
           (a.schemaVersion==3 && (x.writebackFilePath!=y.writebackFilePath || x.writebackFileDev!=y.writebackFileDev || x.writebackFileIno!=y.writebackFileIno || x.writebackFileSize!=y.writebackFileSize))) return false;
    }
    return true;
}
struct OracleActionResult { std::string actionId,eventId,caseId,bundle,status,evidenceSource,feedbackSampleStatus,ratioSemantics="resident_incremental",backend="force_shrink_all"; std::uint64_t pid=0,start=0; std::string uid; double deadline=0,elapsedMs=0,aaObserved=0,windowObserved=0,procObserved=0,lifecycleObserved=0; std::optional<double> syscallStartBoot,syscallReturnBoot,writeDurationMs,feedbackCompleteBoot,writebackStartBoot,writebackReturnBoot; std::optional<bool> writeOverlapsDeadline,feedbackOverlapsDeadline; Bytes needed=0,remaining=0,nominal=0,input=0,feedbackDrop=0,preRssAnonKiB=0,postRssAnonKiB=0,preRssFileKiB=0,postRssFileKiB=0,preRssTotalKiB=0,postRssTotalKiB=0,keepReserveBytes=0,preFree=0,postFree=0,preAvailable=0,postAvailable=0,preCached=0,postCached=0,preDirty=0,postDirty=0,writebackRssAnonKiB=0,writebackRssFileKiB=0,writebackFree=0,writebackAvailable=0,writebackCached=0,writebackDirty=0,totalAnonProxyBytes=0,currentVmSwapBytes=0,estimatedIncrementBytes=0,estimatedRetainedBytes=0; std::int64_t signedRssDeltaKiB=0,signedRssFileDeltaKiB=0; unsigned ratio=0,plannedRatio=0,schemaVersion=1; long writeLength=-1,writebackReturn=-2; int errorNo=0,writebackErrno=0; bool writeAttempted=false,writebackAttempted=false,feedbackIdentityVerified=false,fileFeedbackPresent=false,overlap=false; };
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
std::vector<OracleSwapTelemetryRow> sampleOracleSwapGroups(int root,const std::string& leafPath) {
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
        for(const auto& node:std::vector<std::string>{"memory.eswap_info","memory.zswap_info"}) {
            OracleSwapTelemetryRow row; row.groupScope=group.first; row.groupPath=group.second; row.node=node;
            if(groupError) {
                row.read.status="group_open_error"; row.read.errorNumber=groupError;
                row.read.startedBoottime=monotonic(); row.read.endedBoottime=row.read.startedBoottime;
            } else row.read=readOracleSwapTelemetryNode(groupFd.get(),node,4096);
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
OracleActionResult performOracleAction(const Options& o,const App& app,const OracleDonor& donor,const OracleRequest& req,unsigned ratio,unsigned priorSuccessfulRatio,Bytes nominal,Bytes remaining,Bytes input,const Forecast& forecast,const std::string& actionId) {
    OracleActionResult r; r.actionId=actionId; r.eventId=req.id; r.caseId=req.caseId; r.bundle=donor.bundle; r.pid=donor.pid; r.uid=donor.uid; r.start=donor.start;
    r.deadline=req.launch; r.needed=req.needed; r.remaining=remaining; r.nominal=nominal; r.ratio=ratio; r.plannedRatio=ratio; r.schemaVersion=req.schemaVersion; r.ratioSemantics=o.oracleRatioSemantics; r.input=input; r.backend=o.oracleBackend;
    for(const auto& registered:req.donors) if(registered.bundle==donor.bundle) { r.evidenceSource=registered.evidenceSource; r.aaObserved=registered.aaObserved; r.windowObserved=registered.windowObserved; r.procObserved=registered.procObserved; r.lifecycleObserved=registered.lifecycleObserved; }
    const double begin=monotonic();
    auto finishNoProc=[&](const std::string& status) { r.status=status; r.elapsedMs=(monotonic()-begin)*1000; r.overlap=monotonic()>req.launch; return r; };
    bool writeAttempted=false, writeComplete=false;
    Control controlAtProcWrite; bool haveControlAtProcWrite=false;
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
        } else writer=FD(::openat(dir.get(),"memory.force_shrink_all",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
        if(writer.get()<0) throw error(isProcFileBackend(o.oracleBackend)?"open proc reclaim":"open memory.force_shrink_all");
        const double writeNow=monotonic(); if(writeNow>=req.launch) throw std::runtime_error("event_deadline_passed_before_write");
        gate=parseControl(readPath(o.control)); if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle)) throw std::runtime_error("runtime_control_changed_before_write");
        const auto immediatelyLatest=parseOracleRequest(readPath(o.oracleSchedule));
        if(!sameOracleRegistration(latest,immediatelyLatest)) throw std::runtime_error("latest_schedule_changed_immediately_before_write");
        const OracleDonor* immediatelyLive=nullptr; for(const auto& item:immediatelyLatest.donors) if(item.bundle==donor.bundle) immediatelyLive=&item;
        if(!immediatelyLive) throw std::runtime_error("oracle_donor_missing_immediately_before_write");
        const auto immediateFailure=oracleBackgroundFailure(o,app,*immediatelyLive,monotonic());
        if(!immediateFailure.empty()) throw std::runtime_error((oracleFreshnessFailureRetryableImpl(immediateFailure)?"oracle_evidence_defer:":"oracle_evidence_reject:")+immediateFailure);
        r.evidenceSource=immediatelyLive->evidenceSource; r.aaObserved=immediatelyLive->aaObserved; r.windowObserved=immediatelyLive->windowObserved; r.procObserved=immediatelyLive->procObserved; r.lifecycleObserved=immediatelyLive->lifecycleObserved;
        auto preSample=sampleOracleRss(*immediatelyLive,app);
        if(!preSample) throw std::runtime_error("prewrite_rss_identity_sample_unavailable");
        r.preRssAnonKiB=preSample->rssAnonKiB;
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
            const auto submitRequest=parseOracleRequest(readPath(o.oracleSchedule));
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
               !oracleProcFileWritebackSystemGapFits(deficit(o,submitSystem,forecast,req.needed),nominal))
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
            auto afterSyncRequest=parseOracleRequest(readPath(o.oracleSchedule));
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
                const Bytes keepFloor=std::max(app.keep,512*MiB);
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
                        auto recheckRequest=parseOracleRequest(readPath(o.oracleSchedule));
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
                            const Bytes currentGap=deficit(o,*refreshedSystem,forecast,req.needed);
                            systemReservationFits=oracleProcFileWritebackSystemGapFits(currentGap,nominal);
                            if(!rssKeepFits) { finalReason="post_fsync_recheck_file_estimate_or_keep_reserve_failed"; break; }
                            if(!systemReservationFits) { finalReason="post_fsync_recheck_shared_system_reservation_failed"; break; }
                            const auto finalControl=parseControl(readPath(o.control));
                            if(finalControl.paused || finalControl.protectedApps.count(app.name) || finalControl.protectedApps.count(donor.bundle) ||
                               finalControl.paused!=currentControl.paused || finalControl.protectedApps!=currentControl.protectedApps ||
                               finalControl.boost!=currentControl.boost || finalControl.until!=currentControl.until) {
                                controlStable=false; finalReason="runtime_control_changed_after_post_fsync_sample"; break;
                            }
                            auto finalRequest=parseOracleRequest(readPath(o.oracleSchedule));
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
            const auto gapAfterWriteback=deficit(o,wbSysAfter,forecast,req.needed);
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
            const Bytes keepFloor=std::max(app.keep,512*MiB);
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
        if(o.oracleRatioSemantics=="cumulative_anon_watermark" && o.oracleBackend=="force_shrink_all") {
            if(!preSample->vmSwapKiB) throw std::runtime_error("prewrite_vmswap_telemetry_missing");
            const Bytes liveResident=preSample->rssAnonKiB*1024;
            const Bytes prewriteCap=std::min(nominal,sub(liveResident,o.oracleCumulativeKeep+64*MiB));
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
        const auto payload=isProcFileBackend(o.oracleBackend)?std::string("1"):std::to_string(writeRatio);
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
            procGateRequest.emplace(parseOracleRequest(readPath(o.oracleSchedule)));
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
            const Bytes keepFloor=std::max(app.keep,512*MiB);
            if(lastResidentBytes<lastFileBytes || lastResidentBytes-lastFileBytes<keepFloor ||
               !oracleProcFileWritebackSystemGapFits(deficit(o,procSysBefore,forecast,req.needed),nominal))
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
            finalProcRequest.emplace(parseOracleRequest(readPath(o.oracleSchedule)));
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
               !oracleProcFileWritebackSystemGapFits(deficit(o,procSysBefore,forecast,req.needed),nominal))
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
        const auto swapBefore=sampleOracleSwapGroups(root.get(),app.path);
        const double finalTelemetryGate=monotonic();
        if(finalTelemetryGate>=req.launch) throw std::runtime_error("event_deadline_passed_after_pre_action_swap_telemetry");
        gate=parseControl(readPath(o.control));
        if(gate.paused || gate.protectedApps.count(app.name) || gate.protectedApps.count(donor.bundle))
            throw std::runtime_error("runtime_control_changed_after_pre_action_swap_telemetry");
        const auto telemetryLatest=parseOracleRequest(readPath(o.oracleSchedule));
        if(!sameOracleRegistration(req,telemetryLatest))
            throw std::runtime_error("schedule_registration_changed_after_pre_action_swap_telemetry");
        const OracleDonor* telemetryLive=nullptr;
        for(const auto& item:telemetryLatest.donors) if(item.bundle==donor.bundle) telemetryLive=&item;
        if(!telemetryLive) throw std::runtime_error("donor_missing_after_pre_action_swap_telemetry");
        const auto telemetryFailure=oracleBackgroundFailure(o,app,*telemetryLive,monotonic());
        if(!telemetryFailure.empty()) throw std::runtime_error("oracle_evidence_reject_after_pre_action_swap_telemetry:"+telemetryFailure);
        if(o.oracleBackend=="proc_file_writeback") verifyOracleWritebackFile(writebackFile,donor,app);
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
        const auto swapAfter=sampleOracleSwapGroups(root.get(),app.path);
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
                r.signedRssDeltaKiB=feedback.signedDeltaKiB;
                r.feedbackDrop=feedback.dropBytes;
                if(isProcFileBackend(o.oracleBackend)) {
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
                const auto postReq=parseOracleRequest(readPath(o.oracleSchedule));
                const OracleDonor* postDonor=nullptr; for(const auto& item:postReq.donors) if(item.bundle==donor.bundle) postDonor=&item;
                std::string postwriteFailure;
                if(!sameOracleRegistration(req,postReq)) postwriteFailure="schedule_registration_changed";
                else if(!postDonor) postwriteFailure="donor_missing_from_schedule";
                else postwriteFailure=oracleBackgroundFailure(o,app,*postDonor,postwriteCheckBoot);
                // A completed anonymous write may outlive the AA/WMS snapshot.
                // Wait only for refreshed evidence; never resubmit the write.
                if(oracleAnonPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,postwriteFailure) &&
                   r.feedbackIdentityVerified && feedback.positiveDrop && postwriteCheckBoot<req.launch) {
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
                        const auto freshReq=parseOracleRequest(readPath(o.oracleSchedule));
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
                            if(!freshFeedback.identityVerified || !freshFeedback.positiveDrop || !freshRss ||
                               freshRss->rssAnonKiB<o.oracleCumulativeKeep/1024) {
                                postwriteFailure="anon_recheck_identity_rss_or_keep_unknown"; break;
                            }
                            // Verify the same registration and protection again after sampling.
                            const auto finalControl=parseControl(readPath(o.control));
                            const auto finalReq=parseOracleRequest(readPath(o.oracleSchedule));
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
                                r.signedRssDeltaKiB=freshFeedback.signedDeltaKiB;
                                r.feedbackDrop=freshFeedback.dropBytes;
                                r.aaObserved=finalDonor->aaObserved;
                                r.windowObserved=finalDonor->windowObserved;
                                r.procObserved=finalDonor->procObserved;
                                r.lifecycleObserved=finalDonor->lifecycleObserved;
                                r.feedbackSampleStatus="verified_positive_process_rss_delta_after_fresh_background_recheck";
                                recovered=true; break;
                            }
                        }
                        if(!oracleAnonPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,postwriteFailure)) break;
                        const double recheckSleep=std::min(0.1,oracleWritebackPostwriteRecheckRemaining(started,monotonic(),req.launch));
                        if(recheckSleep<=0) break;
                        std::this_thread::sleep_for(std::chrono::duration<double>(recheckSleep));
                    }
                    if(!recovered && oracleAnonPostwriteRecheckEligible(req.schemaVersion,o.oracleProfile,o.oracleBackend,postwriteFailure))
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
                                const auto recheckReq=parseOracleRequest(readPath(o.oracleSchedule));
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
                                    const Bytes keepFloor=std::max(app.keep,512*MiB);
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
                                    const auto freshAfterEvidence=parseOracleRequest(readPath(o.oracleSchedule));
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
                const std::string detail="postwrite_check_boottime_s="+std::to_string(postwriteCheckBoot)+":schedule_check_exception:"+e.what();
                r.feedbackSampleStatus=detail;
                r.status=std::string("write_accepted_postwrite_schedule_check_exception:")+e.what();
            }
            const double feedbackComplete=monotonic();
            r.feedbackCompleteBoot=feedbackComplete;
            r.feedbackOverlapsDeadline=oracleFeedbackCrossedDeadline(feedbackComplete,req.launch);
        }
    } catch(const std::exception& e) {
        if(writeAttempted) {
            if(writeComplete) {
                r.status=std::string("write_accepted_feedback_unknown_after_exception:")+e.what();
                if(r.feedbackSampleStatus.empty()) r.feedbackSampleStatus=std::string("postwrite_exception:")+e.what();
            } else r.status=std::string("write_failed_or_partial_no_retry:")+e.what();
        }
        else if(r.writebackAttempted) r.status=std::string("writeback_submitted_no_proc_after_exception:")+e.what();
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
    if(node!="memory.eswap_info" && node!="memory.zswap_info")
        throw std::runtime_error("unsupported swap telemetry node");
    limit=std::min<std::size_t>(limit,4096);
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
                for(unsigned i=1;i<=(options.oracleProfile=="peak_transition_os_snapshot"?8U:2U);++i) {
                    const std::string bundle="io.memscene.scene4.workload.a"+std::to_string(i);
                    const bool appNameAllowed=options.oracleProfile!="peak_transition_os_snapshot" || app.name==bundle;
                    if(leaf.rfind(bundle+"_",0)==0 && appNameAllowed) allowed=true;
                }
                if(!allowed) throw std::runtime_error("oracle mode may reclaim only exact configured scene4 self-donor leaves");
                ++selfDonors;
            }
            if(selfDonors<1 || selfDonors>(options.oracleProfile=="peak_transition_os_snapshot"?8U:2U) ||
                (options.oracleProfile!="peak_transition_os_snapshot" && selfDonors!=2))
                throw std::runtime_error("oracle mode requires 2 legacy or 1..8 explicitly allowlisted profile donors");
            if(options.oracleProfile=="peak_transition_os_snapshot" && !options.oracleBackendExplicit)
                throw std::runtime_error("peak_transition_os_snapshot requires an explicit oracle_backend choice");
            if(isProcFileBackend(options.oracleBackend) && options.oracleProfile!="peak_transition_os_snapshot")
                throw std::runtime_error("proc_file backends are restricted to the explicit peak_transition_os_snapshot profile");
            if(isProcFileBackend(options.oracleBackend) && options.oracleRatioSemantics!="resident_incremental")
                throw std::runtime_error("proc_file backends do not use anonymous ratio/watermark semantics");
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
    std::vector<Bytes> lastFeedbackFileKiB(config.apps.size(),0);
    std::vector<bool> haveFeedback(config.apps.size(),false);
    std::vector<bool> oracleFeedbackFileCategory(config.apps.size(),false);
    std::vector<bool> oracleFeedbackDonorCooldownOnly(config.apps.size(),false);
    bool actionThisTick=false;
    std::string tickDecision=ratioProfile?"ratio_profile_idle":"observation_only",tickTarget;
    Bytes tickBudget=0;
    auto deadline=std::chrono::steady_clock::now();
    auto nextFullObservation=std::chrono::steady_clock::now();
    bool oracleFastPollActive=false;
    int samplingErrors=0;
    std::optional<std::future<OracleActionResult>> oracleAction;
    std::map<std::string,std::string> oracleFingerprints;
    std::set<std::string> oracleHaltedEvents, oracleExpiredEvents, oracleUnknownDeadlineRows;
    std::set<std::string> oracleSubmittedTargets;
    std::map<std::string,unsigned> oracleNextDonor;
    std::map<std::string,unsigned> oraclePositiveRatioByEventDonor;
    std::map<std::string,double> oracleDonorBlockedUntil;
    std::map<std::string,double> oraclePrecommitMemberWaitStart;
    std::map<std::string,std::string> oracleActiveEventForDonor;
    std::map<std::string,std::pair<std::uint64_t,std::uint64_t>> oracleActiveIdentityForDonor;
    RollingLimit oracleGlobalLimit;
    std::map<std::string,RollingLimit> oracleCaseLimits,oracleAppLimits;
    double lastOracleSubmit=0;
    double oracleActionDeadline=0;
    std::string oracleActionEvent,oracleActionId;
    //lzx-note 鸿蒙常驻服务主循环如下：
    while(!stopping() && (!ticks || tick<ticks)) { 
        // 中文导读：这是鸿蒙后端的主循环。普通 harmony-v1 每轮采样并记录；
        // 只有显式 --apply 进入 ratio profile 时，应用处理分支才可能写设备接口。
        // Oracle profile 还有独立的事件调度逻辑，因此本循环内相关代码会更复杂。

        
        // 记录本轮开始时刻；后面用单调时钟控制周期，不受系统时间校准影响。
        /**
         * lzx-note
         *      std::chrono::steady_clock：   单调时钟，适合测量时间间隔。系统校时或用户调整日期时间时，它不会像日历时钟那样跳动。
                now()：                       读取这个时钟此刻的时间点，返回一个 time_point。
                const auto iterationStart：   让编译器推导返回类型，并把这个时间点存起来；const 表示后面不重新赋值。
            后面代码用它作比较，
       */
        const auto iterationStart=std::chrono::steady_clock::now();  //lzx-note-normal

        // Oracle 事件活跃时可能提高轮询频率；完整系统/应用采样仍按周期节拍执行。
        /**
         *  决定本轮要不要做一次完整的系统和应用采样
         *      只有：
         *          当前是 peak_transition_os_snapshot
         *          并且快速轮询事件活跃
         *          并且完整采样周期还没到
         *      才跳过完整采样
         * 因为要在靠近事件截止时间时更频繁地检查事件计划，避免错过短暂的操作窗口。
         * 但完整应用采样会读取每个应用组和多个 /proc/<pid>/... 文件，
         * 频繁执行会增加设备上的 I/O 和 CPU 开销。
         * 
         * 快速轮询：关注外部事件计划里的回收事件是否临近截止时间，是否仍有效，
         * 以及是否满足继续准备动作的条件。
         */
        const bool fullObservationTick=oracleProfileRunsFullObservation(  //lzx-note-normal
            options.oracleProfile=="peak_transition_os_snapshot",oracleFastPollActive,
            iterationStart>=nextFullObservation);


//lzx-note 测试逻辑begin===================================================================================================================================================================================================================================================================
 
        // Oracle 的回收动作在异步任务中执行：若任务完成，本轮取回结果并写账本/更新反馈状态。
        if(oracleAction && oracleAction->wait_for(std::chrono::milliseconds(0))==std::future_status::ready) {
            try { const auto result=oracleAction->get(); appendOracleLedger(options,result); event(eventLog,"INFO",result.bundle,"oracle action_id="+result.actionId+" status="+result.status+" nominal_bytes="+std::to_string(result.nominal)+" overlap="+(result.overlap?"true":"false"));
                if(options.oracleProfile=="peak_transition_os_snapshot" && isProcFileBackend(options.oracleBackend) && (result.writeAttempted || result.writebackAttempted))
                    oracleSubmittedTargets.insert(result.eventId+"|"+result.bundle);
                if(oracleFeedbackMayContinue(result.status,result.feedbackIdentityVerified,
                       isProcFileBackend(result.backend)?result.signedRssFileDeltaKiB:result.signedRssDeltaKiB,
                       isProcFileBackend(result.backend)?result.fileFeedbackPresent:result.postRssAnonKiB!=0)) {
                    const std::string ratioKey=result.eventId+"|"+result.bundle;
                    if(!isProcFileBackend(result.backend)) {
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
                } else if(result.status.rfind("rejected:oracle_evidence_defer:",0)==0 && !result.overlap) {
                    for(const auto& app:config.apps) {
                        const auto slash=app.path.find_last_of('/');
                        const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
                        if(leaf==result.bundle+"_"+result.uid) { oracleDonorBlockedUntil[app.name]=monotonic()+0.5; break; }
                    }
                    try { const auto request=parseOracleRequest(readPath(options.oracleSchedule));
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
    //lzx-note 常驻逻辑--运行时控制开关 end===================================================================================================================================================================================================================================================================

    //lzx-note 常驻逻辑--读取整机内存状态 begin===================================================================================================================================================================================================================================================================

        //sys用来存放读取结果
        System sys;  //lzx-note-normal
        //systemSample() 读取 /proc/meminfo，填入整机的物理内存总量、可用内存、空闲内存、Swap 总量和剩余量等信息。
        //它还会尝试读取 PSI 和 /proc/vmstat，但读取这些附加信息失败不会让这次整机采样失败
        try { sys=systemSample(options.proc); }  //lzx-note-normal
        catch(const std::exception& e) { ++samplingErrors; event(eventLog,"ERROR","system",e.what()); break; }  //lzx-note-normal
    //lzx-note 常驻逻辑--读取整机内存状态 end===================================================================================================================================================================================================================================================================
        
    //lzx-note 常驻逻辑--预测可用内存 begin===================================================================================================================================================================================================================================================================
        
        //调用之前创建的 EWMA 趋势预测器 predictor，使用连续轮次的MemAvailable变化估算可用内存的短期趋势，并结合 horizonSec 预测一段时间后的可用量
        const Forecast forecast=predictor.update(sys,options.horizonSec,options.maxDeclineMBps*MiB);  //lzx-note-normal
    //lzx-note 常驻逻辑--预测可用内存 end===================================================================================================================================================================================================================================================================
        
        //ratio_profile_idle是开启提前回收，observation_only是只观察，不进行真的回收
        tickDecision=ratioProfile?"ratio_profile_idle":"observation_only"; tickTarget.clear(); tickBudget=0;  //lzx-note-normal
        actionThisTick=false;  //lzx-note-normal

    //lzx-note 常驻逻辑--预留内存 begin===================================================================================================================================================================================================================================================================        
        
        Bytes controlBoost=0;  //lzx-note-normal
        //将control.boost的值赋给controlBoost
        if(ratioProfile && controlState=="active" && control.until>unixSeconds()) controlBoost=control.boost;  //lzx-note-normal
    
        //计算整机内存缺口--deficit分别计算两个缺口【可用内存缺口，空闲内存缺口】，再取较大者。
        const Bytes systemGap=deficit(options,sys,forecast,controlBoost);  //lzx-note-normal
        bool hasMemPsi=false,hasIoPsi=false;
        std::string memoryPsi,ioPsi;
        // 读取PSI
        if(fullObservationTick) {
            memoryPsi=rawPSI(options.proc+"/pressure/memory",hasMemPsi);
            ioPsi=rawPSI(options.proc+"/pressure/io",hasIoPsi);
        }

        // 遍历配置的应用组
        if(fullObservationTick) for(std::size_t appIndex=0;appIndex<config.apps.size();++appIndex) {  //lzx-note-normal
            const auto& app=config.apps[appIndex];  //lzx-note-normal
            try {
                FD dir=openBeneath(root.get(),app.path);  //lzx-note-normal
                const auto current=optionalRaw(dir.get(),"memory.current");  //lzx-note-normal
                const auto stat=maybeAt(dir.get(),"memory.stat");  //lzx-note-normal
                if(current.empty() || !stat) throw std::runtime_error("memory.current or memory.stat unreadable");  //lzx-note-normal
                const auto values=parseVendorStat(*stat);  //lzx-note-normal
                const auto ids=processIds(dir.get());  //lzx-note-normal
                std::map<std::uint64_t,ProcStat> currentProcesses;  //lzx-note-normal
                const auto processes=sampleProcesses(ids,previousProcesses[appIndex],currentProcesses);  //lzx-note-normal
                previousProcesses[appIndex]=currentProcesses;  //lzx-note-normal
    //lzx-note 常驻逻辑--预留内存 end===================================================================================================================================================================================================================================================================        
  

//lzx-note 测试逻辑begin===================================================================================================================================================================================================================================================================
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
//lzx-note 测试逻辑end===================================================================================================================================================================================================================================================================
            

            if(ratioProfile) {  //lzx-note-normal
                reason=app.reclaim?"ratio profile awaiting EWMA warmup":"app is not explicitly reclaim-enabled";  //lzx-note-normal
                if(controlState!="active") reason="control is not confirmed active";  //lzx-note-normal
                else if(!app.reclaim) reason="app is not explicitly reclaim-enabled";  //lzx-note-normal
                else if(control.protectedApps.count(app.name)) reason="app protected by runtime control";  //lzx-note-normal
                else if(!processes.rssAnonKiB || !processes.vmSwapKiB || processes.listed==0 || processes.statusRead!=processes.listed) reason="process telemetry incomplete";  //lzx-note-normal
                else if(tick<2 || monotonic()-daemonStart<options.warmupSec) reason="EWMA/startup warmup";  //lzx-note-normal
                else if(monotonic()<nextProbeAction) reason="probe cooldown";  //lzx-note-normal
                else if(actionThisTick) reason="one reclaim action per interval";  //lzx-note-normal
                else if(appProbeMinute[appIndex].used(monotonic())>=options.appPerMinute ||
                        globalProbeMinute.used(monotonic())>=options.perMinute) reason="ratio profile per-minute cap reached";
                else if(systemGap==0) {
                    if(!probeProfile && !backgroundEligible(dir.get(),app.path,ids,currentProcesses))
                        reason="protected: app_score/oom_score_adj/member identity does not confirm background eligibility";
                    else reason="forecast reserve satisfied";
                }
                else {  //lzx-note-normal
                    try {
                        std::optional<ProbeIdentity> probeIdentity;
                        if(probeProfile) probeIdentity=validateProbe(dir.get(),options.root,app.path);
                        else if(!backgroundEligible(dir.get(),app.path,ids,currentProcesses))
                            throw std::runtime_error("app_score/oom_score_adj/member identity does not confirm background eligibility");
                        if(!values.anonKiB) throw std::runtime_error("vendor Anon telemetry unavailable");  //lzx-note-normal
                        const Bytes residentAnon=*processes.rssAnonKiB*1024;  //lzx-note-normal
                        const Bytes ratioDenominator=*values.anonKiB*1024;  //lzx-note-normal
                        const Bytes donorAnon=std::min(ratioDenominator,residentAnon);  //lzx-note-normal
                        const Bytes keep=std::max(app.keep,KeepSlack);  //lzx-note-normal
                        const Bytes cap=std::min({systemGap,sub(donorAnon,keep),  //lzx-note-normal
                            options.batch,sub(options.appPerMinute,appProbeMinute[appIndex].used(monotonic())),  //lzx-note-normal
                            sub(options.perMinute,globalProbeMinute.used(monotonic()))});  //lzx-note-normal
                        const Bytes safeCap=sub(cap,KeepSlack);  //lzx-note-normal
                        budgetText=std::to_string(safeCap);  //lzx-note-normal
                        const unsigned ratio=ratioForBudget(safeCap,ratioDenominator);  //lzx-note-normal
                        ratioText=std::to_string(ratio);  //lzx-note-normal
                        const Bytes nominalRequest=(ratioDenominator*ratio)/100;  //lzx-note-normal
                        if(ratio<1) reason="budget below one-percent ratio quantum";  //lzx-note-normal
                        else if(nominalRequest<options.minBatch) reason="nominal request below configured min_batch";  //lzx-note-normal
                        else if(probeProfile && !sameProbe(dir.get(),options.root,app.path,*probeIdentity)) reason="probe PID identity changed before write";
                        else if(!probeProfile && !backgroundEligible(dir.get(),app.path,ids,currentProcesses)) reason="background eligibility or member identity changed before write";
                        else {
                            Control latest=parseControl(readPath(options.control));  //lzx-note-normal
                            if(latest.paused || latest.protectedApps.count(app.name)) reason="control pause/protection changed before write";  //lzx-note-normal
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
                                           !backgroundEligible(dir.get(),app.path,ids,currentProcesses)))
                                            throw std::runtime_error("post-write process identity or background eligibility changed");  //lzx-note-normal
                                        const auto postAnon=aggregateRssAnon(ids);  //lzx-note-normal
                                        if(!postAnon) throw std::runtime_error("post-write aggregate proc telemetry incomplete");  //lzx-note-normal
                                        const Bytes beforeAnon=*processes.rssAnonKiB;  //lzx-note-normal
                                        const Bytes observedDrop=beforeAnon>*postAnon?(beforeAnon-*postAnon)*1024:0;  //lzx-note-normal
                                        lastFeedbackAnonKiB[appIndex]=*postAnon; haveFeedback[appIndex]=true;  //lzx-note-normal
                                        std::ostringstream elapsedText; elapsedText<<std::fixed<<std::setprecision(3)<<elapsed;  //lzx-note-normal
                                        reason="write_ms="+elapsedText.str()+"; planned_bytes="+budgetText+"; observed_rssanon_drop_bytes="+std::to_string(observedDrop)+" (diagnostic only)";  //lzx-note-normal
                                        event(eventLog,"INFO",app.name,"probe reclaim accepted ratio="+payload+" planned_bytes="+budgetText+" observed_rssanon_drop_bytes="+std::to_string(observedDrop));  //lzx-note-normal
                                    } catch(const std::exception& e) {
                                        reason="write accepted; feedback unknown: "+std::string(e.what());  //lzx-note-normal
                                        nextProbeAction=monotonic()+60.0;  //lzx-note-normal
                                        event(eventLog,"WARN",app.name,reason);  //lzx-note-normal
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
        // 记下下一次完整采样时间；Oracle 活动时的快速轮询不必每次都重采全部应用。
        if(fullObservationTick) nextFullObservation=std::chrono::steady_clock::now()+std::chrono::milliseconds(options.intervalMs);
        const double oracleSubmitInterval=options.oracleProfile=="peak_transition_os_snapshot"
            ?static_cast<double>(options.oracleProfileActionIntervalMs)/1000.0:1.0;
        // Oracle profile 读取外部事件计划、核验身份和时限，并可能排队独立异步动作。
        // 这是测试专用路径，与普通 harmony-v1 观察循环不是同一套策略。
        if(oracleProfile && !oracleAction &&
           (oracleFastPollActive || monotonic()-lastOracleSubmit>=oracleSubmitInterval)) {
            try {
                const auto request=parseOracleRequest(readPath(options.oracleSchedule));
                const double now=monotonic();
                const bool scheduleCompatible=oracleScheduleCompatible(options.oracleProfile,options.oracleBackend,request.schemaVersion);
                if(!scheduleCompatible) {
                    event(eventLog,"ERROR","oracle","schedule schema/profile mismatch; event rejected"); oracleHaltedEvents.insert(request.id);
                }
                if(options.oracleProfile=="peak_transition_os_snapshot")
                    oracleFastPollActive=scheduleCompatible && request.launch>now && request.launch-now<=15.0 &&
                        !oracleHaltedEvents.count(request.id);
                std::ostringstream fingerprintOut; fingerprintOut<<request.caseId<<'|'<<std::setprecision(12)<<request.launch<<'|'<<request.needed;
                fingerprintOut<<'|'<<request.schemaVersion;
                for(const auto& donor:request.donors) fingerprintOut<<'|'<<donor.bundle<<'|'<<donor.uid<<'|'<<donor.pid<<'|'<<donor.start<<'|'<<donor.coldReady<<'|'<<donor.dailyReady<<'|'<<donor.windowReady<<'|'<<donor.readyAt<<'|'<<donor.backgroundSince;
                if(request.schemaVersion==3) for(const auto& donor:request.donors) fingerprintOut<<'|'<<donor.writebackFilePath<<'|'<<donor.writebackFileDev<<'|'<<donor.writebackFileIno<<'|'<<donor.writebackFileSize;
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
                    else if(now-lastOracleSubmit<oracleSubmitInterval) {
                        // Keep polling the active event quickly, but do not prepare or reserve
                        // another action until the independent action-start interval elapses.
                    }
                    else {
                        unsigned donorsBelowFloor=0,donorsAtRatioCeiling=0;
                        const auto donorCount=static_cast<unsigned>(request.donors.size());
                        for(unsigned attempt=0;attempt<donorCount;++attempt) {
                            const unsigned donorIndex=(oracleNextDonor[request.id]+attempt)%donorCount; const auto& donor=request.donors[donorIndex];
                            const auto appIt=std::find_if(config.apps.begin(),config.apps.end(),[&](const App& app){
                                const auto slash=app.path.find_last_of('/'); const auto leaf=slash==std::string::npos?app.path:app.path.substr(slash+1);
                                return app.reclaim && oracleAppMatchesDonor(options.oracleProfile,app.name,leaf,donor.bundle,donor.uid);
                            });
                            if(appIt==config.apps.end()) { event(eventLog,"ERROR","oracle","donor bundle/path/UID is outside the exact self-workload allowlist"); oracleHaltedEvents.insert(request.id); break; }
                            const std::string targetKey=request.id+"|"+donor.bundle;
                            if(options.oracleProfile=="peak_transition_os_snapshot" && isProcFileBackend(options.oracleBackend) &&
                                !oracleProfileTargetMaySubmit(oracleSubmittedTargets.count(targetKey)!=0,
                                    donor.pid!=0 && donor.start!=0 && donor.procVerified && donor.procStart==donor.start)) {
                                ++donorsBelowFloor; continue;
                            }
                            const auto cooldown=oracleDonorBlockedUntil.find(appIt->name);
                            if(cooldown!=oracleDonorBlockedUntil.end() && now<cooldown->second) {
                                const bool identityComplete=donor.pid!=0 && donor.start!=0 && donor.procVerified && donor.procStart==donor.start;
                                if(oracleProfileDonorCooldownScope(request.schemaVersion,options.oracleProfile,options.oracleBackend)) {
                                    if(!oracleProfileCooldownMayContinue(request.schemaVersion,options.oracleProfile,options.oracleBackend,identityComplete)) {
                                        event(eventLog,"WARN","oracle",donor.bundle+" feedback cooldown has unknown donor identity; halting_event");
                                        oracleHaltedEvents.insert(request.id); break;
                                    }
                                    std::string cooldownGuardFailure;
                                    try { cooldownGuardFailure=oracleBackgroundFailure(options,*appIt,donor,now); }
                                    catch(const std::exception& e) { cooldownGuardFailure=std::string("identity_probe_error:")+e.what(); }
                                    if(!cooldownGuardFailure.empty()) {
                                        if(!oracleFreshnessFailureRetryableImpl(cooldownGuardFailure)) {
                                            event(eventLog,"WARN","oracle",donor.bundle+" feedback cooldown guard_failure="+cooldownGuardFailure+"; halting_event");
                                            oracleHaltedEvents.insert(request.id);
                                        } else event(eventLog,"WARN","oracle",donor.bundle+" feedback cooldown guard_failure="+cooldownGuardFailure+"; waiting_for_fresh_proof");
                                        break;
                                    }
                                    ++donorsBelowFloor;
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
                                options.oracleProfile,options.oracleBackend);
                            if(memberWaitStarted && !oraclePrecommitMemberWaitWindowOpen(true,memberWaitAt,now,request.launch)) {
                                event(eventLog,"WARN","oracle",donor.bundle+" cgroup_member_count_retry=stopped reason=retry_window_or_deadline_expired"+
                                    " wait_start_boot="+std::to_string(memberWaitAt)+" now_boot="+std::to_string(now)+
                                    " deadline_boot="+std::to_string(request.launch)+"; halting_event");
                                oraclePrecommitMemberWaitStart.erase(memberWaitKey);
                                oracleHaltedEvents.insert(request.id); break;
                            }
                            std::string guardFailure;
                            CgroupMemberObservation memberObservation;
                            try { guardFailure=oracleBackgroundFailure(options,*appIt,donor,now,&memberObservation); }
                            catch(const std::exception& e) { guardFailure=std::string("identity_probe_error:")+e.what(); }
                            const double guardCheckedAt=monotonic();
                            if(!guardFailure.empty()) {
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
                                if(isProcFileBackend(options.oracleBackend)) {
                                    const auto fileSample=sampleOracleRss(donor,*appIt);
                                    if(!fileSample || !fileSample->rssFileKiB || !fileSample->rssKiB || *fileSample->rssFileKiB==0)
                                        throw std::runtime_error("process_vmrss_or_rssfile_telemetry_missing");
                                    if(*fileSample->rssFileKiB>std::numeric_limits<Bytes>::max()/1024 ||
                                       *fileSample->rssKiB>std::numeric_limits<Bytes>::max()/1024)
                                        throw std::runtime_error("process_vmrss_or_rssfile_bytes_overflow");
                                    donorBytes=*fileSample->rssFileKiB*1024;
                                    donorResidentBytes=*fileSample->rssKiB*1024;
                                    if(donorResidentBytes<donorBytes || donorResidentBytes-donorBytes<std::max(appIt->keep,512*MiB)) {
                                        event(eventLog,"WARN",donor.bundle,"proc_file skipped: retained_other_below_keep_reserve");
                                        ++donorsBelowFloor; continue;
                                    }
                                } else {
                                    const auto processAnon=aggregateRssAnon({donor.pid});
                                    if(!anon || !processAnon) throw std::runtime_error("anon_telemetry_missing");
                                    donorBytes=std::min(*anon*1024,*processAnon*1024);
                                }
                                const Bytes eventGap=deficit(options,sys,forecast,request.needed);
                                const Bytes globalRoom=oracleGlobalLimit.room(now,options.oracleGlobalCap);
                                const Bytes caseRoom=oracleCaseLimits[request.caseId].room(now,options.oracleCaseCap);
                                const Bytes appRoom=oracleAppLimits[donor.bundle].room(now,options.oracleAppCap);
                                const Bytes keepFloor=options.oracleRatioSemantics=="cumulative_anon_watermark"?options.oracleCumulativeKeep:512*MiB;
                                const Bytes cap=isProcFileBackend(options.oracleBackend)
                                    ?sub(std::min({eventGap,options.oracleBatch,globalRoom,caseRoom,appRoom}),64*MiB)
                                    :sub(std::min({eventGap,sub(sys.swapFree,options.swapReserve),sub(donorBytes,keepFloor),options.oracleBatch,globalRoom,caseRoom,appRoom}),64*MiB);
                                unsigned ratio=0;
                                Bytes nominal=0;
                                OracleWatermarkPlan watermark;
                                if(isProcFileBackend(options.oracleBackend)) {
                                    nominal=options.oracleBackend=="proc_file_writeback"
                                        ?oracleProcFileWritebackReservation(donorBytes,donor.writebackFileSize,donorResidentBytes,
                                            std::max(appIt->keep,512*MiB),eventGap,options.oracleBatch,globalRoom,caseRoom,appRoom,64*MiB,options.minBatch)
                                        :oracleProcFileReservation(donorBytes,donorResidentBytes,
                                            std::max(appIt->keep,512*MiB),eventGap,options.oracleBatch,
                                            globalRoom,caseRoom,appRoom,64*MiB,options.minBatch);
                                    if(nominal==0) {
                                        const Bytes limiting=std::min({eventGap,options.oracleBatch,globalRoom,caseRoom,appRoom});
                                        const Bytes requiredFileEstimate=options.oracleBackend=="proc_file_writeback"?std::max(donorBytes,donor.writebackFileSize):donorBytes;
                                        const std::string skip=requiredFileEstimate<options.minBatch?"below_min_batch_or_registered_file_too_small":
                                            limiting==eventGap?"system_deficit":limiting==options.oracleBatch?"single_batch_cap":
                                            limiting==globalRoom?"global_shared_room":limiting==caseRoom?"case_shared_room":"app_shared_room";
                                        event(eventLog,"WARN",donor.bundle,"proc_file skipped: conservative_file_work_estimate_exceeds_"+skip);
                                        ++donorsBelowFloor; continue;
                                    }
                                } else if(options.oracleRatioSemantics=="cumulative_anon_watermark") {
                                    const auto fresh=sampleOracleRss(donor,*appIt);
                                    if(!fresh || !fresh->vmSwapKiB) throw std::runtime_error("cumulative_watermark_proc_or_vmswap_unknown");
                                    const Bytes liveResident=fresh->rssAnonKiB*1024;
                                    const Bytes candidateCap=std::min(cap,sub(liveResident,options.oracleCumulativeKeep+64*MiB));
                                    watermark=oracleCumulativeWatermarkForCap(liveResident,*fresh->vmSwapKiB*1024,candidateCap,options.oracleMaxRatio);
                                    ratio=watermark.ratio;
                                    nominal=watermark.estimatedIncrementBytes;
                                } else {
                                    ratio=oracleRatioForCap(cap,donorBytes,options.oracleMaxRatio);
                                    nominal=donorBytes?(donorBytes*ratio)/100:0;
                                }
                                if(!isProcFileBackend(options.oracleBackend) && (ratio==0 || nominal<options.minBatch)) {
                                    ++donorsBelowFloor;
                                    continue;
                                }
                                const auto priorRatioIt=oraclePositiveRatioByEventDonor.find(request.id+"|"+donor.bundle);
                                const unsigned priorSuccessfulRatio=priorRatioIt==oraclePositiveRatioByEventDonor.end()?0:priorRatioIt->second;
                                if(!isProcFileBackend(options.oracleBackend) && priorSuccessfulRatio && !oracleRatioCanAdvance(priorSuccessfulRatio,ratio)) {
                                    ++donorsAtRatioCeiling;
                                    event(eventLog,"INFO","oracle",donor.bundle+" ratio_skipped_candidate="+std::to_string(ratio)+
                                          " prior_positive_ratio="+std::to_string(priorSuccessfulRatio)+" reason=no_strict_increase");
                                    continue;
                                }
                                const double reserveCheckAt=monotonic();
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
                                const std::string actionId=request.id+"_"+donor.bundle+"_"+std::to_string(unixMillis());
                                OracleActionResult reserved; reserved.actionId=actionId; reserved.eventId=request.id; reserved.caseId=request.caseId; reserved.bundle=donor.bundle; reserved.pid=donor.pid; reserved.uid=donor.uid; reserved.start=donor.start; reserved.deadline=request.launch; reserved.needed=request.needed; reserved.remaining=eventGap; reserved.input=donorBytes; reserved.ratio=ratio; reserved.plannedRatio=ratio; reserved.nominal=nominal; reserved.evidenceSource=donor.evidenceSource; reserved.aaObserved=donor.aaObserved; reserved.windowObserved=donor.windowObserved; reserved.procObserved=donor.procObserved; reserved.lifecycleObserved=donor.lifecycleObserved; reserved.ratioSemantics=options.oracleRatioSemantics; reserved.backend=options.oracleBackend; reserved.totalAnonProxyBytes=watermark.totalAnonProxyBytes; reserved.currentVmSwapBytes=watermark.currentVmSwapBytes; reserved.estimatedIncrementBytes=watermark.estimatedIncrementBytes; reserved.estimatedRetainedBytes=watermark.estimatedRetainedBytes; reserved.status="reserved_before_write_pending_result"; appendOracleLedger(options,reserved);
                                if(options.oracleBackend=="proc_file_writeback") {
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
                                tickDecision="oracle_reserved_and_queued"; tickTarget=donor.bundle; tickBudget=nominal;
                                oracleAction.emplace(std::async(std::launch::async,[&,app=*appIt,donor,request,ratio,priorSuccessfulRatio,nominal,eventGap,donorBytes,forecast,actionId]{ return performOracleAction(options,app,donor,request,ratio,priorSuccessfulRatio,nominal,eventGap,donorBytes,forecast,actionId); }));
                                if(options.oracleProfile=="peak_transition_os_snapshot") oracleFastPollActive=true;
                                break;
                            } catch(const std::exception& e) { event(eventLog,"ERROR","oracle",donor.bundle+" action preflight failed: "+e.what()); oracleHaltedEvents.insert(request.id); break; }
                        }
                        if(donorsBelowFloor==donorCount && !oracleHaltedEvents.count(request.id)) {
                            OracleActionResult skipped; skipped.actionId=request.id+"_capacity_skip_"+std::to_string(unixMillis()); skipped.eventId=request.id; skipped.caseId=request.caseId; skipped.deadline=request.launch; skipped.needed=request.needed; skipped.status=isProcFileBackend(options.oracleBackend)?"skipped_profile_targets_exhausted_or_file_estimate_over_budget":"skipped_both_donors_below_min_batch_or_budget_floor"; appendOracleLedger(options,skipped); oracleHaltedEvents.insert(request.id);
                        }
                        if(donorsAtRatioCeiling==donorCount && !oracleHaltedEvents.count(request.id)) {
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
        const unsigned loopIntervalMs=oracleLoopIntervalMs(
            options.oracleProfile=="peak_transition_os_snapshot",oracleFastPollActive,
            options.oracleProfilePollIntervalMs,static_cast<unsigned>(options.intervalMs));
        deadline+=std::chrono::milliseconds(loopIntervalMs);
        if(deadline<std::chrono::steady_clock::now()) deadline=std::chrono::steady_clock::now();
        while(!stopping() && std::chrono::steady_clock::now()<deadline) {  //lzx-note-normal
            const auto remain=deadline-std::chrono::steady_clock::now();
            std::this_thread::sleep_for(std::min(remain,std::chrono::duration_cast<decltype(remain)>(std::chrono::milliseconds(100))));  //lzx-note-normal
        }
    }
    // 循环结束后记录退出原因；若 Oracle 异步任务仍在运行，等待其返回并落账再退出。
    event(eventLog,"INFO","daemon",stopping()?"stopped by SIGINT/SIGTERM":"observation finished");
    if(oracleAction) {
        try { const auto result=oracleAction->get(); appendOracleLedger(options,result); event(eventLog,"INFO",result.bundle,"oracle final ledger action_id="+result.actionId+" status="+result.status+" overlap="+(result.overlap?"true":"false")); }
        catch(const std::exception& e) { event(eventLog,"ERROR","oracle_ledger",e.what()); ++samplingErrors; }
        oracleAction.reset();
    }
    return samplingErrors?2:0;
}
} // namespace ms::harmony_v1
