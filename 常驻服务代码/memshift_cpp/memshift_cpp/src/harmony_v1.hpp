#pragma once
#include "io.hpp"
#include <functional>

namespace ms::harmony_v1 {
struct VendorStat {
    std::optional<Bytes> anonKiB, zramKiB, eswapKiB;
};

struct ProcStat {
    std::uint64_t pid=0, startTicks=0, minflt=0, majflt=0;
};

struct ProcMemory {
    std::optional<Bytes> rssKiB, vmSwapKiB, rssAnonKiB, rssFileKiB;
    std::optional<Bytes> pssKiB, pssAnonKiB, pssFileKiB, privateKiB, smapsSwapKiB;
};

struct OracleRssSample {
    std::uint64_t pid=0, uid=0, startTicks=0;
    Bytes rssAnonKiB=0;
    std::optional<Bytes> vmSwapKiB;
};

struct OracleWatermarkPlan {
    unsigned ratio=0;
    Bytes totalAnonProxyBytes=0;
    Bytes currentVmSwapBytes=0;
    Bytes estimatedIncrementBytes=0;
    Bytes estimatedRetainedBytes=0;
};

struct OracleRssFeedback {
    bool identityVerified=false;
    bool positiveDrop=false;
    std::int64_t signedDeltaKiB=0; // pre-write RSSAnon minus post-write RSSAnon
    Bytes dropBytes=0;
};

struct MountEntry {
    std::string source, mountPoint, fsType, options;
};

VendorStat parseVendorStat(const std::string& text);
ProcMemory parseProcMemory(const std::string& status, const std::string& smapsRollup);
ProcStat parseProcStat(std::uint64_t pid, const std::string& statText);
std::optional<OracleRssSample> parseOracleRssSample(std::uint64_t pid,
    std::uint64_t expectedUid, std::uint64_t expectedStartTicks,
    const std::string& appPath, const std::string& statBefore,
    const std::string& status, const std::string& cgroup,
    const std::string& statAfter);
OracleRssFeedback compareOracleRssSamples(std::uint64_t expectedPid,
    std::uint64_t expectedUid, std::uint64_t expectedStartTicks,
    const std::optional<OracleRssSample>& before,
    const std::optional<OracleRssSample>& after);
bool oracleFeedbackMayContinue(const std::string& status, bool identityVerified,
    std::int64_t signedDeltaKiB, bool postSamplePresent);
const std::string& oracleActionLedgerHeaderV2();
void validateOracleActionLedgerHeader(const std::string& header);
const std::string& oracleActionTimingLedgerHeaderV3();
void validateOracleActionTimingLedgerHeaderV3(const std::string& header);
bool oracleWriteCrossedDeadline(double syscallStartBoottime, double syscallReturnBoottime, double deadlineBoottime);
bool oracleFeedbackCrossedDeadline(double feedbackCompleteBoottime, double deadlineBoottime);
bool hasMemoryController(const std::string& cgroupText);
std::vector<MountEntry> parseMounts(const std::string& mountsText);
void validateBackendRequest(const std::string& backend, bool apply);
unsigned ratioForBudget(Bytes budgetBytes, Bytes residentAnonBytes);
unsigned oracleRatioForCap(Bytes capBytes, Bytes donorBytes, unsigned maxRatio);
OracleWatermarkPlan oracleCumulativeWatermarkForCap(Bytes residentAnonBytes,
    Bytes currentVmSwapBytes, Bytes capBytes, unsigned maxTargetRatio);
bool oracleRatioCanAdvance(unsigned priorSuccessfulRatio, unsigned candidateRatio);
// Returns a retryable reason only when all identity/background facts remain
// valid but their bounded observation timestamps have expired.
std::string oracleFreshnessFailure(bool structuralEvidenceValid, double now,
    const std::vector<std::pair<std::string,double>>& observations,
    double maxAgeSeconds=2.5);
bool oracleFreshnessFailureRetryable(const std::string& reason);
// Parser seam for deterministic protocol tests; performs no device I/O.
void validateOracleScheduleText(const std::string& text);
int run(const Configuration& config, const std::string& backend, bool check, bool apply, Bytes ticks, const std::function<bool()>& stopping);
} // namespace ms::harmony_v1
