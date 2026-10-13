#pragma once
namespace ms::harmony_v1 {
inline bool floorTextEqual(const char* a,const char* b) {
    if(!a || !b) return false;
    while(*a && *a==*b) { ++a; ++b; }
    return *a==*b;
}
struct PostwriteFloorEvidence {
    bool scope=false, completeWrite=false, originalFeedbackComplete=false;
    bool guardFloorOnly=false, latestSampleComplete=false, registrationSame=false;
    bool controlSame=false, withinDeadline=false, withinReservation=false, latestPositiveFeedback=false;
};
inline PostwriteFloorEvidence constructPostwriteFloorEvidence(
    bool scope,bool completeWrite,bool originalComplete,const char* originalStatus,
    const char* finalGuard,bool fresh,bool sampleComplete,bool latestPositive,bool registrationSame,
    bool controlSame,bool withinDeadline,bool withinReservation) {
    return {scope,completeWrite,originalComplete && floorTextEqual(originalStatus,"write_accepted_keep_violation_halt"),
        fresh && floorTextEqual(finalGuard,"pipeline_process_keep_verified_below_floor"),
        sampleComplete,registrationSame,controlSame,withinDeadline,withinReservation,latestPositive};
}
inline bool isolatePostwriteFloorDonor(const PostwriteFloorEvidence& e) {
    return e.scope && e.completeWrite && e.originalFeedbackComplete &&
        e.guardFloorOnly && e.latestSampleComplete && e.registrationSame &&
        e.controlSame && e.withinDeadline && e.withinReservation && e.latestPositiveFeedback;
}
inline bool floorDisabledKeyMatches(const char* disabledEvent,const char* disabledBundle,
    const char* event,const char* bundle) {
    return floorTextEqual(disabledEvent,event) && floorTextEqual(disabledBundle,bundle);
}
}
