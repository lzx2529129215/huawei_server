#pragma once
#include "daily_action_executor.hpp"

namespace ms::harmony_v1::daily {
// Telemetry arrays remain keyed by original config index; only daily scan order rotates.
inline std::size_t scanIndex(std::size_t position,std::size_t count,
    std::size_t start,bool dailyAsync) {
    return dailyAsync && count?(start+position)%count:position;
}
// One immutable admission instant per known-strength scan; only scheduling
// cooldowns use it. Worker evidence, age and hard deadline use the real clock.
inline double scanQualificationTime(bool knownStrength,double frozen,double current) {
    return knownStrength?frozen:current;
}
class Schedule {
    std::vector<double> appUntil_,protectionUntil_;
    double globalUntil_=0;
    std::size_t cursor_=0;
    bool halted_=false;
public:
    explicit Schedule(std::size_t count):appUntil_(count,0),protectionUntil_(count,0) {}
    std::size_t scanStart() const { return cursor_; }
    bool eligible(std::size_t appIndex,double now) const {
        return !halted_ && appIndex<appUntil_.size() && std::isfinite(now) &&
            now>=globalUntil_ && now>=appUntil_[appIndex];
    }
    bool pairedEligible(std::size_t index,double now) const {
        return !halted_ && index<appUntil_.size() && std::isfinite(now) &&
            now>=globalUntil_ && now>=protectionUntil_[index];
    }
    void submitted(std::size_t appIndex) {
        if(appIndex<appUntil_.size()) cursor_=(appIndex+1)%appUntil_.size();
    }
    void defer(std::size_t appIndex,double now,double seconds) {
        if(appIndex>=appUntil_.size() || !std::isfinite(now) || !std::isfinite(seconds)) { halt(); return; }
        appUntil_[appIndex]=std::max(appUntil_[appIndex],now+std::max(0.0,seconds));
        protectionUntil_[appIndex]=std::max(protectionUntil_[appIndex],now+std::max(0.0,seconds));
    }
    void complete(const Result& result,double polledAt,double cooldown,bool knownStrength=false) {
        if(result.uncertain) halt();
        if(result.request.appIndex>=appUntil_.size() || !std::isfinite(polledAt)) {halt();return;}
        appUntil_[result.request.appIndex]=std::max(appUntil_[result.request.appIndex],polledAt+backoffSeconds(result,cooldown));
        // Default spacing starts at consumption. Only the explicit known/owned
        // successful path can use a validated return. The slot remains occupied until poll.
        if(!std::isfinite(polledAt)) { halt(); return; }
        double anchor=polledAt;
        if(knownStrength && strengthAuthority(result.request) && verifiedWrite(result) && result.startedAt && result.returnedAt &&
           std::isfinite(*result.startedAt) && std::isfinite(*result.returnedAt) &&
           *result.startedAt>=0 && *result.startedAt<=*result.returnedAt && *result.returnedAt<=polledAt)
            anchor=*result.returnedAt;
        globalUntil_=std::max(globalUntil_,anchor+1.0);
    }
    void halt() { halted_=true; }
};
// One optional paired opportunity, never a compound job or an uncharged retry.
class PairedDisk {
    std::optional<Request> prepared_;
public:
    void cancel() { prepared_.reset(); }
    void completed(const Result& r,bool enabled) {
        cancel();
        if(enabled && strengthAuthority(r.request) && verifiedWrite(r) &&
           r.request.backend=="force_shrink_anon" && r.postAnonBytes<r.preAnonBytes && r.preparationFaultSafe && r.feedbackFaults.size()==1) {
            prepared_=r.request;prepared_->pairedAfter=r.request.actionId;
            prepared_->pairPostAnon=r.postAnonBytes;prepared_->pairFaults=r.feedbackFaults;
        }
    }
    void refresh(const std::optional<OracleRequest>& e,double now) {
        if(prepared_ && (!e || !prepared_->knownEvent || !known::sameRegistration(*e,*prepared_->knownEvent) ||
           !deadlineAllows(*prepared_,now))) cancel();
    }
    bool isTarget(std::size_t index) const { return prepared_ && prepared_->appIndex==index; }
    std::size_t start(std::size_t fallback) const {return prepared_?prepared_->appIndex:fallback;}
    bool identityMatches(const std::string& path,std::uint64_t device,std::uint64_t inode,
        const std::map<std::uint64_t,std::uint64_t>& members) const {
        return prepared_ && path==prepared_->groupPath && device==prepared_->groupDevice &&
            inode==prepared_->groupInode && members==prepared_->members;
    }
    bool bind(Request& disk) const {
        if(!prepared_ || disk.backend!="force_swapout" || disk.appIndex!=prepared_->appIndex ||
           disk.groupPath!=prepared_->groupPath || disk.groupDevice!=prepared_->groupDevice ||
           disk.groupInode!=prepared_->groupInode || disk.members!=prepared_->members ||
           disk.knownLeadSec!=prepared_->knownLeadSec || disk.knownCapacity!=prepared_->knownCapacity || disk.keepBytes!=prepared_->keepBytes ||
           !strengthAuthority(disk) || !disk.knownEvent ||
           !known::sameRegistration(*disk.knownEvent,*prepared_->knownEvent)) return false;
        disk.pairedAfter=prepared_->pairedAfter;disk.pairPostAnon=prepared_->pairPostAnon;disk.pairFaults=prepared_->pairFaults;
        return true;
    }
};
} // namespace ms::harmony_v1::daily
