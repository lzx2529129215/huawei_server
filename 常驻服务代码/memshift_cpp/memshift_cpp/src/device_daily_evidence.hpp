#pragma once
#include "io.hpp"
#include <atomic>
#include <map>
#include <vector>
namespace ms::harmony_v1 {
struct DailyDeviceEvidence {
    bool verified=false;
    std::string reason="not_sampled";
    // Target-only diagnostics; never retain raw AA records or other apps.
    bool aaParsed=false;
    unsigned aaMatchCount=0;
    std::string aaState="UNKNOWN",aaReason="not_parsed";
    double oldest=0;
    std::uint64_t pid=0,startTicks=0,uid=0;
};
struct DailyMemberSnapshot {
    std::uint64_t pid=0,uid=0,startTicks=0;
    std::string statBefore,status,cgroup,oomScoreAdj,statAfter;
};
// PC-native WPS keeps its UIAbility FOREGROUND after minimization. Verify the
// main process is non-foreground in MemoryManagerService and every owned WMS
// window is invisible; a windowless cloud process is not a visible window.
struct WpsWindowEvidence {
    bool verified=false;
    std::string reason="not_sampled";
    double oldest=0;
};
WpsWindowEvidence parseWpsWindowEvidence(const std::map<std::uint64_t,std::uint64_t>& members,
    std::uint64_t mainPid,std::uint64_t uid,const std::string& memoryDump,
    const std::string& windowTable,const std::map<std::uint64_t,std::string>& details,
    double oldest,double now);
WpsWindowEvidence sampleWpsWindowEvidence(const std::map<std::uint64_t,std::uint64_t>& members,
    std::uint64_t mainPid,std::uint64_t uid,const std::atomic<bool>& stop);
// One selected singleton app. No schedule, future event, host registration, or
// background thread. Called on the existing daily action worker only.
DailyDeviceEvidence sampleDeviceDailyEvidence(const std::string& leaf,
    std::uint64_t groupDevice,std::uint64_t groupInode,
    const std::map<std::uint64_t,std::uint64_t>& expected,
    const std::map<std::uint64_t,std::uint64_t>& expectedUids,const std::atomic<bool>& stop);
// Pure parser for fixtures and the real provider. AA/WMS establish a current
// lifecycle snapshot, not an independent lifecycle event stream.
DailyDeviceEvidence parseDeviceDailyEvidence(const std::string& leaf,std::uint64_t pid,
    std::uint64_t start,const std::string& statBefore,const std::string& status,
    const std::string& cmdline,const std::string& cgroup,const std::string& members,
    const std::string& statAfter,const std::string& aa,const std::string& table,
    const std::map<std::uint64_t,std::string>& details,double oldest,double now);
DailyDeviceEvidence parseBrowserDailyGroupEvidence(const std::string& leaf,
    const std::map<std::uint64_t,std::uint64_t>& expected,
    const std::map<std::uint64_t,std::uint64_t>& expectedUids,
    std::uint64_t primaryPid,std::uint64_t primaryStart,
    const std::vector<DailyMemberSnapshot>& processes,const std::string& membersBefore,
    const std::string& membersAfter,const std::string& appScoreBefore,
    const std::string& appScoreAfter,const std::string& primaryCmdline,
    const std::string& memoryManager,const std::string& aa,const std::string& table,
    const std::map<std::uint64_t,std::string>& details,double oldest,double now);
}
