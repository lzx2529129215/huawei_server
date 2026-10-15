#pragma once
#include "device_proc_reader.hpp"
#include <array>
#include <cmath>
#include <stdexcept>
namespace device_retry {
using namespace ms::harmony_v1::device_proc;
struct Target { std::string bundle; std::uint64_t uid=0,pid=0,start=0; bool retryTarget=true; };
struct Plan {
    std::string caseId,bootId;
    double launch=0;
    bool fixedBackgroundRoles=false;
    bool fixedFocusSequence=false;
    bool fixedBurst4=false;
    std::uint64_t targetPeakMiB=0,targetFileMiB=0;
    std::uint64_t targetDailyMiB=0,targetCount=0,peakFillWorkers=1;
    std::vector<Target> targets;
};
inline std::vector<std::string> split(const std::string& s) {
    std::vector<std::string> out;
    std::istringstream in(s);
    std::string x;
    while(std::getline(in,x,'|')) out.push_back(x);
    return out;
}
inline bool bootIdValid(const std::string& s) {
    if(s.size()!=36) return false;
    for(std::size_t i=0;i<s.size();++i) {
        if(i==8 || i==13 || i==18 || i==23) {if(s[i]!='-')return false;}
        else if(std::string("0123456789abcdef").find(s[i])==std::string::npos)return false;
    }
    return true;
}
// Separate static format: no host-supplied dynamic absence/background proof.
// Closed role schemas pin each reviewed workload; no plan field can raise limits.
inline Plan parsePlan(const std::string& raw) {
    if(raw.empty() || raw.size()>16384 || raw.back()!='\n' ||
       raw.find('\r')!=std::string::npos || raw.find('\0')!=std::string::npos)
        throw std::runtime_error("plan_bytes");
    std::istringstream in(raw);
    std::string line;
    if(!std::getline(in,line))throw std::runtime_error("plan_header");
    const bool browser3=line=="device_retry_plan_browser_donor_3target_85g_v1";
    const bool rolePlan=browser3 || line=="device_retry_plan_roles_v1" ||
        line=="device_retry_plan_roles_3of16_v1" || line=="device_retry_plan_roles_3of16_v2" ||
        line=="device_retry_plan_roles_3of16_v2_focus" ||
        line=="device_retry_plan_roles_3of16_v3_burst4_focus" ||
        line=="device_retry_plan_roles_3of16_v5_lead90_focus";
    const bool focusSequence=browser3 || line=="device_retry_plan_roles_3of16_v2_focus";
    const bool burst4Sequence=line=="device_retry_plan_roles_3of16_v3_burst4_focus" ||
        line=="device_retry_plan_roles_3of16_v5_lead90_focus";
    const bool role3=!browser3 && (line=="device_retry_plan_roles_3of16_v1" ||
        line=="device_retry_plan_roles_3of16_v2" || focusSequence || burst4Sequence);
    const bool role3v4=line=="device_retry_plan_roles_3of16_v5_lead90_focus";
    const bool role3v3=line=="device_retry_plan_roles_3of16_v3_burst4_focus";
    const bool role3v2=line=="device_retry_plan_roles_3of16_v2" || focusSequence;
    if(!rolePlan && line!="device_retry_plan_v1")throw std::runtime_error("plan_header");
    if(!std::getline(in,line))throw std::runtime_error("plan_meta_missing");
    const auto f=split(line);
    if(browser3 && (f.size()!=16u || f[3]!="20261013" || f[4]!="3" || f[5]!="21504" ||
       f[6]!="1536" || f[7]!="8704" || f[8]!="256" || f[9]!="90" || f[10]!="10" ||
       f[11]!="30" || f[12]!="330" || f[13]!="420" || f[14]!="1200" || f[15]!="4"))
        throw std::runtime_error("browser3_workload_fingerprint");
    if(f.size()!=((browser3)?16u:(role3v3||role3v4)?20u:(role3?19u:(rolePlan?16u:3u))) || !caseAllowed(f[0]) || !bootIdValid(f[1]))
        throw std::runtime_error("plan_meta");
    if(role3) {
        static const std::array<const char*,16> fixedV1={"20261005","13","3","24576","41472","23040",
            "16896","1792","1536","512","6144","256","30","330","420","1200"};
        static const std::array<const char*,16> fixedV2={"20261005","13","3","24576","44160","23040",
            "19584","1792","1536","512","7040","256","30","330","420","1200"};
        static const std::array<const char*,17> fixedV3={"20261005","13","3","24576","44160","23040",
            "19584","1792","1536","512","7040","256","30","330","420","1200","4"};
        static const std::array<const char*,17> fixedV4={"20261005","13","3","24576","44544","19968",
            "19968","1536","1536","1536","8192","256","90","330","420","1200","4"};
        const auto checkFixed=[&](const auto& fixed) {
            for(std::size_t i=0;i<fixed.size();++i)if(f[i+3]!=fixed[i]) return false;
            return true;
        };
        const bool fingerprintOk=role3v4?checkFixed(fixedV4):(role3v3?checkFixed(fixedV3):(role3v2?checkFixed(fixedV2):checkFixed(fixedV1)));
        if(!fingerprintOk)
            throw std::runtime_error(role3v4?"plan_role_3of16_v4_workload_fingerprint":(role3v3?"plan_role_3of16_v3_workload_fingerprint":
                (role3v2?"plan_role_3of16_v2_workload_fingerprint":"plan_role_3of16_workload_fingerprint")));
    } else if(rolePlan && !browser3) {
        static const std::array<const char*,13> fixed={"20261005","24576","41472","3072","16896",
            "4608","512","1920","256","30","330","420","1200"};
        for(std::size_t i=0;i<fixed.size();++i)if(f[i+3]!=fixed[i])
            throw std::runtime_error("plan_role_workload_fingerprint");
    }
    Plan plan; plan.caseId=f[0]; plan.bootId=f[1];
    plan.fixedBackgroundRoles=rolePlan;
    plan.fixedFocusSequence=focusSequence || burst4Sequence;
    plan.fixedBurst4=burst4Sequence;
    if(browser3){plan.fixedBurst4=true;plan.targetCount=3;plan.targetDailyMiB=1536;plan.targetPeakMiB=8704;plan.targetFileMiB=256;plan.peakFillWorkers=4;}
    if(role3 || role3v3 || role3v4) { plan.targetPeakMiB=role3v4?8192:((role3v2||role3v3)?7040:6144);plan.targetFileMiB=256; }
    else if(rolePlan && !browser3) { plan.targetPeakMiB=1920;plan.targetFileMiB=256; }
    char* end=nullptr; errno=0;
    plan.launch=std::strtod(f[2].c_str(),&end);
    if(errno || !end || end==f[2].c_str() || *end ||
       !std::isfinite(plan.launch) || plan.launch<=0)
        throw std::runtime_error("plan_T");
    std::set<std::string> bundles;
    std::set<std::uint64_t> uids,pids;
    const unsigned count=browser3?3u:16u;
    if(!plan.targetCount)plan.targetCount=count;
    for(unsigned i=1;i<=count;++i) {
        if(!std::getline(in,line))throw std::runtime_error("plan_target_missing");
        const auto row=split(line); Target t;
        const unsigned expectedIndex=browser3?i+13:i;
        const auto donorLast=role3?13u:(rolePlan&&!browser3?4u:0u);
        const auto expectedRole=(i<=donorLast?"background_donor":"peak_target");
        if(row.size()!=(rolePlan?5u:4u) || row[0]!="io.memscene.scene4.workload.a"+std::to_string(expectedIndex) ||
           !number(row[1],t.uid) || !number(row[2],t.pid) || t.pid>INT_MAX ||
           !number(row[3],t.start))throw std::runtime_error("plan_target_scope");
        if(rolePlan && row[4]!=expectedRole)throw std::runtime_error("plan_role_scope");
        t.retryTarget=!rolePlan || i>donorLast;
        t.bundle=row[0];
        if(!bundles.insert(t.bundle).second || !uids.insert(t.uid).second ||
           !pids.insert(t.pid).second)throw std::runtime_error("plan_duplicate_identity");
        plan.targets.push_back(t);
    }
    if(std::getline(in,line))throw std::runtime_error("plan_extra");
    return plan;
}
}
