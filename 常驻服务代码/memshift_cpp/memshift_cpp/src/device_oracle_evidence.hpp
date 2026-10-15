#pragma once
#include "io.hpp"
#include <map>
#include <memory>
namespace ms::harmony_v1 {
struct OracleDonor {
    std::string bundle, uid, aaState, evidenceSource;
    std::string writebackFilePath;
    std::map<std::uint64_t,std::uint64_t> members; // Fresh whole-cgroup PID/start_ticks snapshot.
    std::map<std::uint64_t,std::uint64_t> memberUids; // Includes isolated renderer UIDs.
    std::uint64_t pid=0, start=0, aaPid=0, aaUid=0, procStart=0;
    std::uint64_t writebackFileDev=0, writebackFileIno=0, writebackFileSize=0;
    bool coldReady=false,dailyReady=false,windowReady=false, windowVisible=true,windowFocused=true,lifecycleClear=false,procVerified=false;
    double readyAt=0,backgroundSince=0,lastHeartbeat=0;
    double aaObserved=0,windowObserved=0,procObserved=0,lifecycleObserved=0;
};
struct OracleRequest { std::string id, caseId; double launch=0; Bytes needed=0; unsigned schemaVersion=1; std::vector<OracleDonor> donors; std::uint64_t proofSequence=0; std::string planFingerprint; };

class DeviceOracleEvidence {
    struct Impl;std::unique_ptr<Impl> impl_;
public:
    DeviceOracleEvidence(const std::string& planPath,const std::string& ledgerPath);
    ~DeviceOracleEvidence();
    OracleRequest current(const std::string& context);
    void recordReservation(const std::string& actionId,const OracleRequest& request);
};
OracleRequest parseDeviceOraclePlan(const std::string& text);
std::string deviceOracleProtectedReason(const OracleDonor& donor,double now);
void testDeviceOracleTargetGuard();
void testDeviceOracleEvidenceParsers();
}
