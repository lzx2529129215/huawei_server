#include "../src/postwrite_floor_isolation.hpp"
using namespace ms::harmony_v1;
int main() {
    const auto make=[](const char* finalGuard,bool fresh=true,const char* original="write_accepted_keep_violation_halt") {
        return constructPostwriteFloorEvidence(true,true,true,original,finalGuard,fresh,true,true,true,true,true,true);
    };
    auto good=make("pipeline_process_keep_verified_below_floor");
    if(!isolatePostwriteFloorDonor(good)) return 1;
    const char* guards[]={"aa_not_background","window_visible","window_focused","proc_start_ticks_changed",
        "proc_uid_changed","proc_cgroup_membership_changed","pipeline_process_keep_unknown","aa_evidence_stale",""};
    for(auto guard:guards) if(isolatePostwriteFloorDonor(make(guard))) return 2;
    if(isolatePostwriteFloorDonor(make("pipeline_process_keep_verified_below_floor",false))) return 3;
    if(isolatePostwriteFloorDonor(make("pipeline_process_keep_verified_below_floor",true,"write_failed_or_partial_no_retry"))) return 4;
    bool PostwriteFloorEvidence::* fields[]={&PostwriteFloorEvidence::scope,&PostwriteFloorEvidence::completeWrite,
        &PostwriteFloorEvidence::originalFeedbackComplete,&PostwriteFloorEvidence::guardFloorOnly,
        &PostwriteFloorEvidence::latestSampleComplete,&PostwriteFloorEvidence::registrationSame,
        &PostwriteFloorEvidence::latestPositiveFeedback,&PostwriteFloorEvidence::controlSame,&PostwriteFloorEvidence::withinDeadline,&PostwriteFloorEvidence::withinReservation};
    for(auto field:fields) { auto bad=good; bad.*field=false; if(isolatePostwriteFloorDonor(bad)) return 5; }
    // Production selector key deliberately excludes PID/backend, so replacement
    // identity or another backend never re-enables this bundle in the same event.
    if(!floorDisabledKeyMatches("event1","a2","event1","a2")) return 6;
    if(floorDisabledKeyMatches("event1","a2","event1","a3")) return 7;
    if(floorDisabledKeyMatches("event1","a2","event2","a2")) return 8;
    // a3 is not disabled, but still must pass all original worker guards.
    if(isolatePostwriteFloorDonor(make("proc_uid_changed"))) return 9;
    return 0;
}
