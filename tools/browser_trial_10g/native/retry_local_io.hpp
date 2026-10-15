#pragma once
#include "retry_authority_native.hpp"
#include "retry_journal.hpp"
#include "uid_binding.hpp"
#include "snapshot_stage.hpp"
namespace device_retry {
struct Snapshot {
    bool known=false;
    AuthoritySample authority;
    retry_core::Journal journal;
    std::string peak,daily,reason="unknown";
};
inline bool readBoot(std::string& out) {
    if(!boundedRead("/proc/sys/kernel/random/boot_id",64,false,out))return false;
    if(out.size()!=37 || out.back()!='\n')return false;
    out.pop_back();return bootIdValid(out);
}
inline bool optionalRead(const std::string& path,std::size_t limit,
                         std::string& out,bool& exists) {
    out.clear();exists=false;
    const int fd=open(path.c_str(),O_RDONLY|O_NOFOLLOW|O_CLOEXEC);
    if(fd<0)return errno==ENOENT;
    exists=true;bool good=true;char buffer[4096];
    for(;;) {
        const auto n=read(fd,buffer,sizeof(buffer));
        if(n==0)break;
        if(n<0){if(errno==EINTR)continue;good=false;break;}
        if(out.size()+static_cast<std::size_t>(n)>limit){good=false;break;}
        out.append(buffer,static_cast<std::size_t>(n));
    }
    close(fd);return good && out.find('\0')==std::string::npos;
}
// Invoked only in an owned snapshot child. The controller bounds this entire
// child to 8 seconds, including direct proc/filesystem reads, without HDC.
inline Snapshot localSnapshot(const Plan& plan,unsigned index,const std::string& attempt,
                              const std::string& bindingPath,const std::string& planBytes,const std::string& psPath,
                              const std::atomic<bool>& stop) {
    SnapshotStage diagnostic;
    Snapshot result;
    if(index>=plan.targets.size() || !caseAllowed(attempt)){result.reason="snapshot_scope";return result;}
    const auto& target=plan.targets[index];
    auto& sample=result.authority;sample.readStart=boot();
    if(sample.readStart<0 || !readBoot(sample.bootBefore) || sample.bootBefore!=plan.bootId){result.reason="boot_changed";return result;}
    diagnostic.mark(1);
    if(!readUidBinding(bindingPath,planBytes)){result.reason="uid_binding_unknown_or_changed";return result;}
    diagnostic.mark(2);
    // UID was verified natively once before T; ps remains fresh on every snapshot.
    const std::string verifiedBm="{\"uid\":"+std::to_string(target.uid)+"}";
    const double remaining=8.0-(boot()-sample.readStart);
    if(remaining<=0){result.reason="snapshot_timeout";return result;}
    diagnostic.mark(3);
    const auto ps=command({psPath,"-A","-o","PID,UID,NAME,ARGS"},stop,
                          static_cast<unsigned>(remaining*1000),1048576,{},false);
    diagnostic.mark(4);
    if(!ps.ok || !ps.error.empty()){result.reason="ps_failed";return result;}
    const std::string group="/dev/cgroup/memory/default/100/"+target.bundle+"_"+std::to_string(target.uid);
    diagnostic.mark(5);
    struct stat metadata{};bool absent=false;std::string members;
    if(stat(group.c_str(),&metadata)!=0) {
        if(errno!=ENOENT){result.reason="cgroup_metadata_unknown";return result;}
        absent=true;
    } else {
        bool exists=false;
        if(!S_ISDIR(metadata.st_mode) || !optionalRead(group+"/cgroup.procs",16384,members,exists) || !exists){result.reason="cgroup_read_unknown";return result;}
    }
    diagnostic.mark(6);
    diagnostic.mark(7);
    if(!parseAuthority(verifiedBm,ps.output,members,absent,target,sample)){result.reason="authority_"+sample.reason;return result;}
    diagnostic.mark(8);
    const std::string files="/data/app/el2/100/base/"+target.bundle+"/haps/entry/files/memscene_";
    diagnostic.mark(9);
    bool peakExists=false,dailyExists=false;
    if(!optionalRead(files+attempt+".jsonl",262144,result.peak,peakExists) ||
       !optionalRead(files+attempt+"_daily.jsonl",262144,result.daily,dailyExists)){result.reason="journal_read_unknown";return result;}
    diagnostic.mark(10);
    diagnostic.mark(11);
    try {result.journal=parseJournal(result.peak,result.daily,attempt,target.uid,peakExists,dailyExists);}
    catch(const std::exception& e){result.reason=e.what();return result;}
    diagnostic.mark(12);
    diagnostic.mark(13);
    if(!sample.flags.absent && result.journal.oneGeneration) {
        if(!liveGeneration(sample,target,result.journal)){result.reason="journal_live_generation_unknown";return result;}
    }
    diagnostic.mark(14);
    sample.readEnd=boot();
    if(sample.readEnd<sample.readStart || sample.readEnd-sample.readStart>8 ||
       !readBoot(sample.bootAfter) || sample.bootAfter!=plan.bootId || !readUidBinding(bindingPath,planBytes)){result.reason="boot_or_read_deadline";return result;}
    diagnostic.mark(15);
    result.known=true;result.reason="complete";return result;
}

struct FocusIdentityProof { bool known=false,absent=false,generationDifferent=false;std::uint64_t pid=0,start=0,uid=0;double readStart=-1,readEnd=-1;std::string reason="unknown"; };
inline bool statIdentity(const std::string& raw,std::uint64_t expectedPid,std::uint64_t& pid,std::uint64_t& start) {
    const auto split=raw.rfind(')'),first=raw.find(' ');
    if(split==std::string::npos || first==std::string::npos || !number(raw.substr(0,first),pid) || pid!=expectedPid)return false;
    std::istringstream in(raw.substr(split+1));std::string token;
    for(unsigned i=0;i<=19;++i)if(!(in>>token))return false;
    return number(token,start);
}
inline FocusIdentityProof localFocusIdentity(const Plan& plan,unsigned index,const std::string& planPath,
                                              const std::string& bindingPath,const std::string& planBytes,
                                              const std::string& psPath,const std::atomic<bool>& stop) {
    FocusIdentityProof result;
    if(index>=plan.targets.size() || !plan.fixedFocusSequence){result.reason="focus_scope";return result;}
    const auto& target=plan.targets[index];result.readStart=boot();
    std::string beforeBoot;
    if(result.readStart<0 || !readBoot(beforeBoot) || beforeBoot!=plan.bootId ||
       !readUidBinding(bindingPath,planBytes)){result.reason="boot_or_uid_binding";return result;}
    std::string unchanged;
    if(!boundedRead(planPath,16384,false,unchanged) || unchanged!=planBytes){result.reason="plan_changed";return result;}
    const std::string verifiedBm="{\"uid\":"+std::to_string(target.uid)+"}";
    const double remaining=2.5-(boot()-result.readStart);
    if(remaining<=0){result.reason="identity_deadline";return result;}
    const auto ps=command({psPath,"-A","-o","PID,UID,NAME,ARGS"},stop,static_cast<unsigned>(remaining*1000),1048576,{},false);
    if(!ps.ok || !ps.error.empty()){result.reason="ps_"+ps.reason;return result;}
    const std::string group="/dev/cgroup/memory/default/100/"+target.bundle+"_"+std::to_string(target.uid);
    struct stat metadata{};bool groupAbsent=false;std::string members;
    if(stat(group.c_str(),&metadata)!=0) {
        if(errno!=ENOENT){result.reason="cgroup_metadata";return result;}groupAbsent=true;
    } else {
        bool exists=false;
        if(!S_ISDIR(metadata.st_mode) || !optionalRead(group+"/cgroup.procs",16384,members,exists) || !exists){result.reason="cgroup_members";return result;}
    }
    AuthoritySample authority;
    if(!parseAuthority(verifiedBm,ps.output,members,groupAbsent,target,authority)){result.reason="authority_"+authority.reason;return result;}
    result.absent=authority.flags.absent;
    if(result.absent) {
        result.known=true;result.reason="known_absent";
    } else {
        if(authority.pids.size()!=1 || authority.members.size()!=1 || authority.pids.front()!=authority.members.front()) {
            result.reason="multiple_or_unmatched_members";return result;
        }
        result.pid=authority.pids.front();
        const std::string proc="/proc/"+std::to_string(result.pid);
        std::string statBefore,status,statAfter;
        std::uint64_t pidBefore=0,startBefore=0,pidAfter=0,startAfter=0;
        if(!boundedRead(proc+"/stat",65536,false,statBefore) || !boundedRead(proc+"/status",65536,false,status) ||
           !boundedRead(proc+"/stat",65536,false,statAfter) || !statIdentity(statBefore,result.pid,pidBefore,startBefore) ||
           !statIdentity(statAfter,result.pid,pidAfter,startAfter) || pidBefore!=pidAfter || startBefore!=startAfter ||
           !statusMatches(status,Donor{target.bundle,result.pid,target.uid,startBefore})) {
            result.reason="proc_identity_unstable";return result;
        }
        result.start=startAfter;result.uid=target.uid;
        result.generationDifferent=result.pid!=target.pid || result.start!=target.start;
        if(result.generationDifferent){result.reason="observed_new_pid_start_generation";return result;}
        result.known=true;result.reason="same_planned_pid_start_uid_memcg";
    }
    result.readEnd=boot();std::string afterBoot,afterBinding,afterPlan;
    if(result.readEnd<result.readStart || result.readEnd-result.readStart>2.5 || !readBoot(afterBoot) || afterBoot!=plan.bootId ||
       !readUidBinding(bindingPath,planBytes) || !boundedRead(planPath,16384,false,afterPlan) || afterPlan!=planBytes) {
        result.known=false;result.absent=false;result.generationDifferent=false;result.reason="post_read_boot_plan_or_uid_changed";
    }
    return result;
}
inline std::string encodeFocusIdentity(const FocusIdentityProof& x) {
    return "device_retry_focus_identity_v1\t"+std::to_string(x.known)+"\t"+std::to_string(x.absent)+"\t"+
        std::to_string(x.generationDifferent)+"\t"+std::to_string(x.pid)+"\t"+std::to_string(x.start)+"\t"+
        std::to_string(x.uid)+"\t"+std::to_string(x.readStart)+"\t"+std::to_string(x.readEnd)+"\t"+x.reason+"\n";
}
}
