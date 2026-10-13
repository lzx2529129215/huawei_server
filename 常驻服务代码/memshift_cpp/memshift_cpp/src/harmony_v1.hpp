#pragma once
#include "io.hpp"
#include <functional>

namespace ms::harmony_v1 {
struct VendorStat {
    std::optional<Bytes> anonKiB, zramKiB, eswapKiB;
};
struct OracleDiskPool { Bytes zramBytes=0, eswapBytes=0; };
bool oracleDiskBackendEnabledText(const std::string& text);
std::optional<OracleDiskPool> parseOracleDiskPool(const std::string& text);
std::optional<Bytes> oracleDiskWorkEstimate(Bytes poolBytes, Bytes rssAnonKiB);
bool oracleDiskPoolMeetsMinimum(Bytes poolBytes, Bytes minimum);
bool oracleDiskPreparationComplete(bool attempted, const std::string& status,
    bool identityVerified, std::int64_t rssDeltaKiB, bool overlap);
Bytes oracleDeviceDonorTotalKeepFloor(const Options& options, const std::string& donorBundle, Bytes appKeep);
bool oracleDiskPostwriteRecheckEligible(unsigned schemaVersion,
    const std::string& profile,const std::string& backend,const std::string& freshnessFailure);
Bytes oracleDiskReservation(Bytes pool, Bytes room, Bytes margin, Bytes minimum);

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
    std::optional<Bytes> rssFileKiB;
    std::optional<Bytes> rssKiB;
};

enum class OracleDeviceCooldownSelection { Available, LegacyBlocked, SkipDonor, WaitForProof, HaltEvent };
OracleDeviceCooldownSelection oracleDeviceCooldownSelection(bool device,unsigned schema,bool diskPrepare,const std::string& backend,bool cooling,bool identityComplete,const std::string& backgroundFailure,bool protectedByControl);

bool oraclePreparationWorkMode(bool device,bool diskPrepare,const std::string& backend);
std::optional<Bytes> oraclePreparationWorkEstimate(const OracleRssSample& sample);
Bytes oraclePreparationWorkReservation(const OracleRssSample& sample,Bytes room,Bytes margin);
bool oraclePreparationFeedbackWithinReservation(bool workMode,Bytes drop,Bytes reservation);
bool oraclePreparationFinalRatioAllowed(const OracleRssSample& sample,Bytes plannedIncrement,unsigned ratio,Bytes anonKeep,Bytes totalKeep);

struct OracleWatermarkPlan {
    unsigned ratio=0;
    Bytes totalAnonProxyBytes=0;
    Bytes currentVmSwapBytes=0;
    Bytes estimatedIncrementBytes=0;
    Bytes estimatedRetainedBytes=0;
};
std::optional<Bytes> oraclePipelineAnonKeep(const OracleRssSample& sample,
    Bytes anonKeep, Bytes totalKeep);
bool oraclePipelineKeepSatisfied(const OracleRssSample& sample,
    Bytes anonKeep, Bytes totalKeep);

std::string oraclePipelineKeepFailure(const std::optional<OracleRssSample>& sample, bool identityVerified,
    Bytes anonKeep, Bytes totalKeep);
bool oraclePrecommitMaySkipDonor(const std::string& fullGuardFailure);

struct OracleRssFeedback {
    bool identityVerified=false;
    bool positiveDrop=false;
    std::int64_t signedDeltaKiB=0; // pre-write RSSAnon minus post-write RSSAnon
    Bytes dropBytes=0;
};

struct OracleSwapTelemetryRead {
    std::string status, raw;
    int errorNumber=0;
    double startedBoottime=0, endedBoottime=0;
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
OracleSwapTelemetryRead readOracleSwapTelemetryNode(int groupFd, const std::string& node,
    std::size_t limit=4096);
std::string oracleSwapTelemetryCsvRow(const std::vector<std::string>& fields);
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
Bytes oracleProcFileReservation(Bytes estimatedRssFileBytes, Bytes estimatedResidentBytes,
    Bytes keepReserve, Bytes eventGap,
    Bytes batchCap, Bytes globalRoom, Bytes caseRoom, Bytes appRoom,
    Bytes safetyMargin, Bytes minBatch);
Bytes oracleProcFileWritebackReservation(Bytes estimatedRssFileBytes, Bytes registeredFileSize,
    Bytes estimatedResidentBytes, Bytes keepReserve, Bytes eventGap,
    Bytes batchCap, Bytes globalRoom, Bytes caseRoom, Bytes appRoom,
    Bytes safetyMargin, Bytes minBatch);
bool oracleProcFileWritebackEstimateFits(Bytes currentRssFileKiB,
    Bytes registeredFileSizeBytes, Bytes reservedBytes);
bool oracleProcFileWritebackSystemGapFits(Bytes currentGapBytes, Bytes reservedBytes);
struct OracleDiskPriorityAttempt { unsigned donorIndex=0; bool priorityDiskOnly=false; };
std::optional<bool> oracleDiskPriorityPrepRoomAvailable(const OracleRssSample& sample,
    Bytes anonKeep,Bytes totalKeep,Bytes minBatch,Bytes safetyMargin);
std::vector<OracleDiskPriorityAttempt> oracleDiskPriorityAttempts(
    const std::vector<unsigned>& rotation,const std::vector<bool>& zramPoolReady,
    const std::vector<bool>& diskPrepared,const std::vector<std::optional<bool>>& prepRoomAvailable);
bool oracleProfileTargetMaySubmit(bool alreadySubmitted, bool identityComplete);
bool oracleProfileDonorCooldownScope(unsigned schemaVersion, const std::string& profile,
    const std::string& backend);
bool oracleProfileCooldownMayContinue(unsigned schemaVersion, const std::string& profile,
    const std::string& backend, bool identityComplete);
enum class OraclePrecommitMemberRetryAction { NotApplicable, Wait, Recovered, Stop };
OraclePrecommitMemberRetryAction oraclePrecommitMemberRetryAction(bool profileScope,
    bool readOk, bool parseValid, std::size_t entryCount, std::uint64_t solePid,
    std::uint64_t expectedPid, bool waitStarted, double waitStart,
    double now, double launchDeadline);
bool oraclePrecommitMemberWaitWindowOpen(bool waitStarted, double waitStart,
    double now, double launchDeadline);
bool oracleWritebackMayProceed(bool fdatasyncSucceeded, bool beforeDeadline,
    bool identityAndBackgroundVerified, bool budgetStillFits);
bool oracleScheduleCompatible(const std::string& profile, const std::string& backend,
    unsigned schemaVersion);
bool oracleAppMatchesDonor(const std::string& oracleProfile,
    const std::string& appName, const std::string& appLeaf,
    const std::string& bundle, const std::string& uid);
bool oracleProcFileWriteComplete(long writeLength, std::size_t payloadLength);
bool oracleProcFileFeedbackMayContinue(bool identityVerified,
    const std::optional<Bytes>& preRssFileKiB,
    const std::optional<Bytes>& postRssFileKiB);
bool oracleWritebackPostwriteRecheckEligible(unsigned schemaVersion,
    const std::string& profile, const std::string& backend,
    const std::string& freshnessFailure);
bool oracleAnonPostwriteRecheckEligible(unsigned schemaVersion,
    const std::string& profile, const std::string& backend,
    const std::string& freshnessFailure);
bool oracleWritebackPreProcRecheckMayResume(unsigned schemaVersion,
    const std::string& profile, const std::string& backend,
    const std::string& initialFailure, const std::string& finalFailure,
    bool registrationStable, bool controlStable, bool fileIdentityVerified,
    bool rssKeepFits, bool systemReservationFits, bool beforeDeadline);
double oracleWritebackPostwriteRecheckRemaining(double startedBoottime,
    double nowBoottime, double eventDeadlineBoottime);
bool oracleProcFileRecheckFeedbackMayContinue(bool identityVerified,
    bool systemSamplePresent, const std::optional<Bytes>& preRssFileKiB,
    const std::optional<Bytes>& postRssFileKiB,
    const std::optional<Bytes>& postRssTotalKiB, Bytes keepReserveBytes);
bool oracleProfileRunsFullObservation(bool profileEnabled,bool eventActive,bool observationDue);
unsigned oracleLoopIntervalMs(bool profileEnabled,bool eventActive,unsigned profilePollMs,unsigned normalIntervalMs);
bool oracleFastPollWindow(bool device,double now,double launch);
bool oracleMayReserveAction(bool workerInFlight,double now,double lastSubmit,double minInterval,double launch);
bool oracleRatioCanAdvance(unsigned priorSuccessfulRatio, unsigned candidateRatio);
// Returns a retryable reason only when all identity/background facts remain
// valid but their bounded observation timestamps have expired.
std::string oracleFreshnessFailure(bool structuralEvidenceValid, double now,
    const std::vector<std::pair<std::string,double>>& observations,
    double maxAgeSeconds=2.5);
bool oracleFreshnessFailureRetryable(const std::string& reason);
bool oracleUnsubmittedEvidenceMayDefer(const std::string& status,
    bool writeAttempted, bool writebackAttempted, bool overlap);
// Parser seam for deterministic protocol tests; performs no device I/O.
void validateOracleScheduleText(const std::string& text);
int run(const Configuration& config, const std::string& backend, bool check, bool apply, Bytes ticks, const std::function<bool()>& stopping);
} // namespace ms::harmony_v1
