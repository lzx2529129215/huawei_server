#pragma once
#include "bounded_command.hpp"
#include "retry_core.hpp"
#include <regex>
namespace device_retry {
inline std::vector<std::string> initialArgs(const std::string& aa,const std::string& bundle,const std::string& caseId,
                                            unsigned peakFillWorkers=1) {
    return {aa,"start","-b",bundle,"-a","EntryAbility","-m","entry","--ps","op","promote",
        "--ps","case_id",caseId,"--pi","peak_fill_workers",std::to_string(peakFillWorkers)};
}
inline std::vector<std::string> focusArgs(const std::string& aa,const std::string& bundle,const std::string& caseId) {
    return {aa,"start","-b",bundle,"-a","EntryAbility","-m","entry","--ps","op","focus","--ps","case_id",caseId};
}
inline std::vector<std::string> replacementArgs(const std::string& aa,const std::string& bundle,
                                                const std::string& attempt,
                                                unsigned targetPeakMiB=0,unsigned targetFileMiB=0,
                                                unsigned peakFillWorkers=1) {
    const auto peak=std::to_string(targetPeakMiB?targetPeakMiB:2592);
    const auto file=std::to_string(targetPeakMiB?targetFileMiB:600);
    return {aa,"start","-b",bundle,"-a","EntryAbility","-m","entry",
        "--ps","case_id",attempt,"--ps","daily_case_id",attempt+"_daily",
        "--ps","bundle",bundle,"--pi","daily_mib","0","--pi","peak_mib",peak,
        "--pi","file_mib",file,"--pi","hold_seconds","420","--pi","lifetime_seconds","1200",
        "--pi","seed","20261005","--pi","await_peak","0","--pi","cold_start_ready","1",
        "--pi","no_periodic_msync","1","--pi","peak_fill_workers",std::to_string(peakFillWorkers)};
}
inline bool aaOutputFailure(const Command& c) {
    return c.status!=0 || std::regex_search(c.output,std::regex("\\b(error|failed|invalid)\\b",std::regex::icase));
}
inline retry_core::Refusal refusal(const Command& c) {
    retry_core::Refusal r;
    // A timed out/cancelled command or exec failure cannot prove framework refusal.
    r.runtimeError=c.reaped && c.gatePassed && c.status!=125 && c.status!=126 && c.status!=127 &&
        (c.reason.empty() || c.reason=="nonzero_or_incomplete") && c.error.empty() && aaOutputFailure(c);
    bool any=false,all=true;const std::regex code("Error Code\\s*:\\s*(\\d+)");
    for(std::sregex_iterator it(c.output.begin(),c.output.end(),code),end;it!=end;++it) {
        any=true;if((*it)[1]!="10108101")all=false;
    }
    r.only10108101=any && all;
    r.failedToStart=std::regex_search(c.output,std::regex("failed to start ability",std::regex::icase));
    const auto outer=c.output.substr(0,c.output.find("Error cause:"));
    r.outerTimeoutTransport=std::regex_search(outer,std::regex("timeout|timed out|transport",std::regex::icase));
    return r;
}
}
