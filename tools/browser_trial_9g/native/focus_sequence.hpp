#pragma once
#include <array>
#include <cstdint>
#include <string>
#include <vector>

namespace device_retry::focus_sequence {

inline constexpr std::array<unsigned, 6> offsetsSeconds{5, 10, 15, 20, 25, 30};
inline constexpr unsigned firstTargetIndex = 0;
inline constexpr unsigned targetCount = 3;

inline unsigned targetIndex(unsigned sequenceIndex) {
    return firstTargetIndex + (sequenceIndex % targetCount);
}

inline double scheduledAt(double T, unsigned sequenceIndex) {
    return T + offsetsSeconds.at(sequenceIndex);
}

inline bool samePlannedIdentity(bool known, bool absent, bool identityMatches,
                                bool singlePidAndMember, const std::vector<std::uint64_t>& pids,
                                const std::vector<std::uint64_t>& members,
                                std::uint64_t pid) {
    return known && !absent && identityMatches && singlePidAndMember &&
        pids.size() == 1 && members.size() == 1 && pids[0] == pid && members[0] == pid;
}

inline std::vector<std::string> args(const std::string& aa, const std::string& bundle,
                                     const std::string& caseId) {
    return {aa, "start", "-b", bundle, "-a", "EntryAbility", "-m", "entry",
            "--ps", "op", "focus", "--ps", "case_id", caseId};
}

} // namespace device_retry::focus_sequence
