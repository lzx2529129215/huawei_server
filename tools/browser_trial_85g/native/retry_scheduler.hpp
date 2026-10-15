#pragma once
#include "retry_core.hpp"
namespace device_retry {
class ReadDispatchPacer {
    double next_=0;
public:
    bool due(double now)const{return now>=0 && now>=next_;}
    void dispatched(double now){next_=now+0.5;}
};
struct CompletedRead {
    unsigned long long generation=0;
    double started=-1,completed=-1;
    bool known=false,sameBoot=false;
    retry_core::Journal journal;
    retry_core::Authority authority;
};
struct TargetState {
    unsigned long long generation=0;
    unsigned long long plannedUid=0,plannedPid=0,plannedStart=0;
    unsigned consecutiveAbsent=0;
    bool readInFlight=false,launchInFlight=false;
    double retryReadAfter=0;
    CompletedRead latest;
    retry_core::Refusal refusal;
};
struct SlotDecision {
    bool resolved=false,submitted=false;
    retry_core::Decision decision=retry_core::Decision::Unknown;
    unsigned long long generation=0;
    double observedAt=-1,readStart=-1;
};
// Only the main controller mutates this state. Workers return captured immutable
// results; a result for any replaced generation is discarded before mutation.
class Scheduler {
    TargetState targets_[16]{};
    SlotDecision decisions_[10][16]{};
    double origin_=-1;
    bool invalid_=false;
public:
    TargetState& target(unsigned n){return targets_[n];}
    const SlotDecision& record(unsigned slot,unsigned n)const{return decisions_[slot-1][n];}
    void bindPlannedIdentity(unsigned n,unsigned long long uid,unsigned long long pid,
                             unsigned long long start) {
        if(n>=16 || !uid || !pid || !start){invalid_=true;return;}
        auto& target=targets_[n];target.plannedUid=uid;target.plannedPid=pid;target.plannedStart=start;
    }
    bool invalid()const{return invalid_;}
    void invalidate(){invalid_=true;}
    void initialSubmission(double at) {
        if(at<0){invalid_=true;return;}
        if(origin_<0 || at<origin_)origin_=at;
    }
    double origin()const{return origin_;}
    bool acceptRead(unsigned n,const CompletedRead& read,double now) {
        auto& target=targets_[n];
        if(read.generation!=target.generation)return false;
        target.readInFlight=false;
        if(!read.sameBoot){invalid_=true;target.consecutiveAbsent=0;return false;}
        if(!read.known || read.started<0 || read.completed<read.started || read.completed>now) {
            target.consecutiveAbsent=0;target.latest=CompletedRead{};
            target.retryReadAfter=now+1.0;return false;
        }
        target.latest=read;
        target.consecutiveAbsent=retry_core::absence(target.consecutiveAbsent,read.authority);
        return true;
    }
    retry_core::Decision consider(unsigned slot,unsigned n,double now) {
        auto& target=targets_[n];
        if(invalid_ || origin_<0 || !retry_core::deadline(now,origin_,slot))return retry_core::Decision::Unknown;
        auto& decision=decisions_[slot-1][n];
        if(decision.submitted)return decision.decision;
        const auto& read=target.latest;
        const auto classification=retry_core::decision(read.journal,target.refusal,
            target.consecutiveAbsent,read.authority,now,read.started,read.sameBoot,
            read.generation==target.generation && read.known);
        // Every non-submitted target needs a fresh observation in this slot.
        const double slotBegin=origin_+static_cast<double>(slot)*30.0;
        const auto current=read.started>=slotBegin?classification:retry_core::Decision::Unknown;
        // Historical coverage is distinct from live submission authorization.
        // A later stale/unknown read cannot erase a decision actually made in
        // this slot. Once eligible was observed, an unsubmitted opportunity
        // remains pending even if a later read becomes unknown/not eligible.
        if(current!=retry_core::Decision::Unknown &&
           (decision.decision!=retry_core::Decision::Eligible || current==retry_core::Decision::Eligible)) {
            decision.decision=current;
            decision.resolved=true;
            decision.observedAt=now;decision.readStart=read.started;decision.generation=target.generation;
        }
        return current;
    }
    // A background donor is observed on the same cadence but is never a
    // promotable/retry target. Its monitor decision requires a complete fresh
    // snapshot and a daily journal; absence is recorded as monitor-only, not
    // converted into a retry or peak failure.
    bool monitorOnly(unsigned slot,unsigned n,double now) {
        if(slot<1 || slot>10 || n>=16 || invalid_ || origin_<0 ||
           !retry_core::deadline(now,origin_,slot))return false;
        auto& target=targets_[n];const auto& read=target.latest;
        if(!read.known || !read.sameBoot || read.generation!=target.generation ||
           !retry_core::fresh(now,read.started) ||
           read.started<origin_+static_cast<double>(slot)*30.0 || !read.authority.known ||
           !read.journal.dailyComplete || !read.journal.oneGeneration || !read.journal.uidMatches ||
           read.journal.uid!=target.plannedUid || read.journal.pid!=target.plannedPid ||
           read.journal.start!=target.plannedStart)return false;
        if(!read.authority.absent &&
           (!read.authority.identityMatches || !read.authority.singlePidAndMember))return false;
        auto& decision=decisions_[slot-1][n];
        decision.resolved=true;decision.submitted=false;
        decision.decision=retry_core::Decision::MonitorOnly;
        decision.generation=target.generation;decision.observedAt=now;
        decision.readStart=read.started;return true;
    }
    bool beginLaunch(unsigned slot,unsigned n,double now) {
        auto& target=targets_[n];
        if(now<target.retryReadAfter || decisions_[slot-1][n].submitted || target.launchInFlight || target.readInFlight ||
           consider(slot,n,now)!=retry_core::Decision::Eligible)return false;
        target.launchInFlight=true;return true;
    }
    // Call only on an actual permitted child submission. A queued job or a
    // failed exec gate is not a request and is never counted as launch failure.
    bool actualSubmission(unsigned slot,unsigned n,unsigned long long capturedGeneration,
                          double gateAt,bool sameBoot) {
        auto& target=targets_[n];
        if(!sameBoot){invalid_=true;return false;}
        if(decisions_[slot-1][n].submitted || capturedGeneration!=target.generation || !target.launchInFlight ||
           !retry_core::deadline(gateAt,origin_,slot))return false;
        auto& decision=decisions_[slot-1][n];
        decision.submitted=true;decision.resolved=true;decision.decision=retry_core::Decision::Eligible;
        ++target.generation;target.consecutiveAbsent=0;target.latest=CompletedRead{};
        target.refusal=retry_core::Refusal{};target.launchInFlight=false;
        return true;
    }
    int nextReadTarget(double now,unsigned targetCount,unsigned active,unsigned& cursor) {
        if(targetCount==0 || targetCount>16)return -1;
        for(unsigned count=0;count<targetCount;++count) {
            const unsigned n=cursor;cursor=(cursor+1)%targetCount;
            auto& target=targets_[n];
            if(!retry_core::mayRead(now,target.retryReadAfter,target.readInFlight,target.launchInFlight,active))continue;
            target.readInFlight=true;return static_cast<int>(n);
        }
        return -1;
    }
    bool covered(unsigned targetCount)const {
        if(invalid_)return false;
        if(targetCount==0 || targetCount>16)return false;
        for(unsigned slot=0;slot<10;++slot)for(unsigned n=0;n<targetCount;++n) {
            const auto& decision=decisions_[slot][n];
            if(!decision.resolved || (decision.decision==retry_core::Decision::Eligible && !decision.submitted))return false;
        }
        return true;
    }
};
}
