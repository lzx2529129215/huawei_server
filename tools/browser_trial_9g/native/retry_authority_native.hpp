#pragma once
#include "retry_plan.hpp"
#include "retry_core.hpp"
#include "bounded_command.hpp"
#include <regex>
namespace device_retry {
struct AuthoritySample {
    retry_core::Authority flags;
    std::vector<std::uint64_t> pids,members;
    double readStart=-1,readEnd=-1;
    std::string bootBefore,bootAfter,reason="unknown";
};
inline bool parseAuthority(const std::string& bm,const std::string& ps,
                           const std::string& cg,bool groupAbsent,
                           const Target& target,AuthoritySample& result) {
    std::set<std::uint64_t> uids;
    const std::regex uidPattern("\"uid\"\\s*:\\s*(\\d+)");
    for(std::sregex_iterator i(bm.begin(),bm.end(),uidPattern),end;i!=end;++i) {
        std::uint64_t uid=0;if(!number((*i)[1],uid))return false;uids.insert(uid);
    }
    if(uids!=std::set<std::uint64_t>{target.uid}){result.reason="uid_unknown_or_changed";return false;}
    std::istringstream rows(ps);std::string line;
    if(!std::getline(rows,line))return false;
    std::istringstream header(line);std::string a,b,c,d,extra;
    if(!(header>>a>>b>>c>>d) || (header>>extra) ||
       a!="PID" || b!="UID" || c!="NAME" || d!="ARGS")return false;
    while(std::getline(rows,line)) {
        if(line.find_first_not_of(" \t\r")==std::string::npos)continue;
        std::istringstream fields(line);std::string pidText,uidText,name,args;
        if(!(fields>>pidText>>uidText>>name))return false;
        std::getline(fields,args);const auto first=args.find_first_not_of(" \t");
        if(first!=std::string::npos)args=args.substr(first);else args.clear();
        std::uint64_t pid=0,uid=0;
        if(!number(pidText,pid) || uidText.empty() || uidText.find_first_not_of("0123456789")!=std::string::npos)return false;
        if(uidText=="0")uid=0;else if(!number(uidText,uid))return false;
        const auto matches=[&](const std::string& x){return x==target.bundle || x.rfind(target.bundle+":",0)==0;};
        const bool named=matches(name) || matches(args);
        if(named && uid!=target.uid){result.reason="process_uid_mismatch";return false;}
        if(uid==target.uid || named)result.pids.push_back(pid);
    }
    if(!groupAbsent) {
        std::istringstream members(cg);std::string pidText;
        while(members>>pidText){std::uint64_t pid=0;if(!number(pidText,pid))return false;result.members.push_back(pid);}
    } else if(!cg.empty())return false;
    const std::set<std::uint64_t> owned(result.pids.begin(),result.pids.end());
    for(const auto pid:result.members)if(!owned.count(pid)){result.reason="inventory_inconsistent";return false;}
    result.flags.known=true;
    result.flags.absent=result.pids.empty() && result.members.empty();
    result.reason=result.flags.absent?"absent":"live";
    return true;
}
inline bool liveGeneration(AuthoritySample& sample,const Target& target,
                           const retry_core::Journal& journal) {
    if(sample.pids!=std::vector<std::uint64_t>{journal.pid} ||
       sample.members!=std::vector<std::uint64_t>{journal.pid})return false;
    sample.flags.singlePidAndMember=true;
    const Donor expected{target.bundle,journal.pid,target.uid,journal.start};
    const auto base="/proc/"+std::to_string(journal.pid);
    std::string before,status,after;
    const bool good=boundedRead(base+"/stat",65536,false,before) &&
                    boundedRead(base+"/status",65536,false,status) &&
                    boundedRead(base+"/stat",65536,false,after) &&
                    statMatches(before,expected) && statusMatches(status,expected) &&
                    statMatches(after,expected);
    sample.flags.identityMatches=good;return good;
}
}
