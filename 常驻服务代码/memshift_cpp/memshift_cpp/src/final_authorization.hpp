#pragma once
namespace ms { namespace harmony_v1 {
enum class FinalAuthorizationDisposition { Allow, DeferFreshness, Reject };
inline bool exactFinalReason(const char* a,const char* b) {
    if(!a || !b) return false;
    while(*a && *a==*b) { ++a; ++b; }
    return *a==*b;
}
inline FinalAuthorizationDisposition finalAuthorizationDisposition(
    bool registrationControlValid,bool liveGuardsValid,const char* freshness,
    bool writeAttempted,bool writebackAttempted) {
    if(!registrationControlValid || !liveGuardsValid || writeAttempted || writebackAttempted)
        return FinalAuthorizationDisposition::Reject;
    if(exactFinalReason(freshness,"")) return FinalAuthorizationDisposition::Allow;
    if(exactFinalReason(freshness,"aa_evidence_stale") || exactFinalReason(freshness,"window_evidence_stale") ||
       exactFinalReason(freshness,"proc_evidence_stale") || exactFinalReason(freshness,"lifecycle_evidence_stale"))
        return FinalAuthorizationDisposition::DeferFreshness;
    return FinalAuthorizationDisposition::Reject;
}
}}
