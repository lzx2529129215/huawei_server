#pragma once
#include "core.hpp"
#include "daily_recent_reserve.hpp"
#include "daily_known_future.hpp"
#include <atomic>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <optional>
#include <thread>

namespace ms::harmony_v1::daily {
// Harmony's ratio backend explicitly has no verified PSI semantics. Raw values
// remain diagnostic; require_psi=true is rejected by backend preflight and here.
// This policy is ONLY for the new Harmony daily adapter, never Linux pressureBlock.
inline std::string harmonyUnverifiedPsiBlock(const Options& options,const System& system,
    const PSI& app=PSI{}) {
    (void)system; (void)app;
    return options.requirePSI?"harmony_psi_unverified_required":std::string{};
}
struct Request {
    std::string actionId, app, groupPath, backend="force_shrink_anon";
    std::string qualification="office_score_adj";
    std::uint64_t sequence=0, groupDevice=0, groupInode=0;
    std::size_t appIndex=0;
    std::map<std::uint64_t,std::uint64_t> members,memberUids;
    Bytes reservedBytes=0, keepBytes=0, minBatch=0;
    unsigned plannedRatio=0;
    double knownLeadSec=35; // Immutable event horizon, never a proof TTL.
    bool knownCapacity=false; // Requires registered known-strength authority; numeric keep/ratio never grants it.
    bool knownStrength=false; // Authority is revalidated independently, never a trusted requested ceiling.
    std::string pairedAfter;
    Bytes pairPostAnon=0;
    std::map<std::uint64_t,std::pair<std::uint64_t,std::uint64_t>> pairFaults; // minor,major at successful preparation feedback.
    std::optional<OracleRequest> knownEvent; // Frozen event/identity registration; never a one-shot donor key.
    double hardDeadline=0; // Zero preserves ordinary daily behavior.
    recent::Snapshot recentTarget; // Immutable per-action target, never upgraded by newer samples.
    double reservedAt=0, maxAgeSeconds=2, forecastSampledAt=0, forecastMaxAgeSeconds=5;
};
inline bool deadlineAllows(const Request& r,double now) {
    if(!r.knownEvent && r.hardDeadline==0) return true;
    return std::isfinite(r.hardDeadline) && r.hardDeadline>0 && std::isfinite(now) && now<r.hardDeadline &&
        (!r.knownEvent || r.hardDeadline==r.knownEvent->launch);
}
struct Evidence {
    std::uint64_t groupDevice=0, groupInode=0;
    std::map<std::uint64_t,std::uint64_t> members;
    bool background=false, controlActive=false;
    std::map<std::uint64_t,std::pair<std::uint64_t,std::uint64_t>> faultCounters;
    Bytes rssAnonBytes=0, denominatorBytes=0, systemGap=0, swapRoom=0;
    double authorityExpires=0; // Optional owned-device snapshot expiry; legacy daily score path uses 0.
    std::string blocked,qualificationDiagnostic;
};
struct Feedback { bool verified=false; Bytes rssAnonBytes=0;
    std::map<std::uint64_t,std::pair<std::uint64_t,std::uint64_t>> faultCounters; };
struct WriteReceipt { long length=-1; int error=0; };
struct Result {
    Request request;
    std::string status="not_submitted",qualificationDiagnostic,diskBudgetDiagnostic;
    std::optional<bool> writeAttempted=false;
    std::optional<long> writeLength;
    std::optional<int> error;
    std::optional<double> startedAt, returnedAt, writeMs;
    unsigned submittedRatio=0;
    Bytes nominalBytes=0, preAnonBytes=0, postAnonBytes=0;
    Bytes diskZramBefore=0,diskZramAfter=0,diskEswapBefore=0,diskEswapAfter=0;
    bool feedbackVerified=false, uncertain=false, preparationFaultSafe=false;
    std::map<std::uint64_t,std::pair<std::uint64_t,std::uint64_t>> feedbackFaults;
};
struct Hooks {
    std::function<Evidence()> probe;
    std::function<WriteReceipt(const std::string&)> write;
    std::function<void()> feedbackDelay;
    std::function<Feedback()> feedback;
    std::function<double()> now;
    std::function<bool()> cancelled;
    std::function<unsigned(Bytes,Bytes)> ratioForBudget;
    std::function<std::optional<Bytes>()> finalGap; // Optional new-policy recheck after qualification, before any write.
};
inline Bytes keepFloor(Bytes configured,bool isolatedProbe) {
    return std::max(configured,isolatedProbe?Bytes{128*1024}:512*MiB);
}
inline std::optional<Bytes> refreshedGap(const Options& options,const System& fresh,
    const Forecast& sampled,double sampledAt,Bytes boost,std::optional<Bytes> readyTarget={}) {
    const double age=fresh.time-sampledAt;
    if(!std::isfinite(age) || age<0 || age>=std::max(1.0,options.horizonSec) ||
       !std::isfinite(sampled.declineBytesPS) || !fresh.total) return {};
    const double remaining=std::max(0.0,options.horizonSec-age);
    const double decline=std::clamp(sampled.declineBytesPS,0.0,options.maxDeclineMBps*static_cast<double>(MiB));
    const Bytes drop=std::min(amount(decline*remaining),fresh.total/4);
    const Forecast projected{sub(fresh.available,drop),decline};
    return std::max(deficit(options,fresh,projected,boost),sub(readyTarget.value_or(options.readyFree),fresh.free));
}
// Options and immutable request must both authorize the experimental floor/ceiling.
inline bool capacityOptions(const Options& o) {
    return o.dailyKnownCapacity && o.dailyKnownStrength && o.dailyKnownFutureFreeOnly &&
        o.dailyAsync && !o.oracleMode && o.oracleProofSource=="legacy" &&
        o.dailyOwnedOsSnapshot &&
        (o.dailyDiskMode=="owned_apply" || (o.dailyBrowserDonorProfile && o.dailyDiskMode=="off")) &&
        o.minBatch>=(o.dailyBrowserDonorProfile?MiB:64*MiB);
}
// Immutable request authority plus the adapter's fresh known::gap and owned proof
// are both required. A caller cannot obtain 60% by setting a numeric ceiling.
inline bool strengthAuthority(const Request& r) {
    if((r.knownLeadSec!=35 && !r.knownCapacity) || !r.knownStrength || !r.knownEvent || r.qualification!="owned_os_snapshot" ||
       r.keepBytes<(r.knownCapacity?128:512)*MiB ||
       (r.knownCapacity && r.minBatch<(r.knownEvent && r.knownEvent->schemaVersion==3?MiB:64*MiB)) ||
       !r.groupInode || !deadlineAllows(r,r.reservedAt) ||
       !known::memberMatches(*r.knownEvent,r.groupPath,r.members,r.memberUids)) return false;
    try { known::validate(*r.knownEvent,r.reservedAt,Unlimited,0,r.knownLeadSec); } catch(...) { return false; }
    return true;
}
inline bool capacityAuthority(const Request& r) { return r.knownCapacity && strengthAuthority(r); }
inline bool capacityAdapterAllows(const Options& o,const Request& r,bool isolatedProbe=false) {
    return !r.knownCapacity || (!isolatedProbe && capacityOptions(o) && r.knownLeadSec==o.dailyKnownLeadSec && capacityAuthority(r));
}
inline Bytes policyKeep(Bytes configured,bool isolatedProbe,const Request* authority=nullptr) {
    // Keep the existing isolated probe128KiB semantics. Only complete event authority lowers normal512MiB.
    if(!isolatedProbe && authority && capacityAuthority(*authority)) return std::max(configured,128*MiB);
    return keepFloor(configured,isolatedProbe);
}
inline unsigned ratioCeiling(const Request& r) {
    return capacityAuthority(r)?95U:(strengthAuthority(r)?60U:20U);
}
inline Bytes ratioNominal(Bytes denominator,unsigned ratio) {
    return (denominator/100)*ratio+(denominator%100)*ratio/100;
}
inline unsigned ratioWithin(Bytes cap,Bytes denominator,unsigned ceiling) {
    if(!denominator) return 0;
    for(unsigned ratio=std::min(95U,ceiling);ratio>0;--ratio)
        if(ratioNominal(denominator,ratio)<=cap) return ratio;
    return 0;
}
inline bool verifiedWrite(const Result& r) {
    if(r.uncertain || !r.writeAttempted.value_or(false) || !r.writeLength || !r.error ||
       *r.error!=0 || !r.feedbackVerified) return false;
    if(r.request.backend=="force_shrink_anon") return r.status=="write_accepted_feedback_verified" &&
        r.submittedRatio>0 && *r.writeLength==static_cast<long>(std::to_string(r.submittedRatio).size());
    return r.request.backend=="force_swapout" && r.status=="disk_command_accepted_transition_observed" && *r.writeLength==1;
}
inline std::string pairProtection(const Request& r,const Evidence& e) {
    if(r.pairedAfter.empty()) return {};
    if(r.backend!="force_swapout" || !strengthAuthority(r) || r.pairFaults.size()!=1 ||
       e.faultCounters.size()!=1 || r.pairFaults.begin()->first!=r.members.begin()->first ||
       e.faultCounters.begin()->first!=r.members.begin()->first) return "paired_fault_identity_unknown";
    const auto prior=r.pairFaults.begin()->second,current=e.faultCounters.begin()->second;
    if(current.first<prior.first || current.second<prior.second || current.second>prior.second ||
       current.first-prior.first>1024) return "paired_fault_backoff";
    if(e.rssAnonBytes>add(r.pairPostAnon,MiB)) return "paired_rss_rebound_backoff";
    return {};
}
inline Result execute(const Request& request,const Hooks& hooks) {
    Result r; r.request=request;
    try {
        if((request.knownStrength || request.knownCapacity) && !strengthAuthority(request)) {r.status="rejected:known_strength_authority";return r;}
        if(hooks.cancelled()) { r.status="rejected:stopping"; return r; }
        const auto e=hooks.probe();
        r.qualificationDiagnostic=e.qualificationDiagnostic;
        const double age=hooks.now()-request.reservedAt;
        const double forecastAge=hooks.now()-request.forecastSampledAt;
        if(!std::isfinite(age) || age<0 || age>request.maxAgeSeconds) r.status="rejected:request_stale";
        else if(!std::isfinite(forecastAge) || forecastAge<0 || forecastAge>=request.forecastMaxAgeSeconds)
            r.status="rejected:forecast_expired";
        else if(!e.blocked.empty()) r.status="rejected:"+e.blocked;
        else if(e.groupDevice!=request.groupDevice || e.groupInode!=request.groupInode ||
                e.members.empty() || e.members!=request.members) r.status="rejected:member_or_group_identity_changed";
        else if(!e.background || !e.controlActive) r.status="rejected:background_or_control_changed";
        else if(e.authorityExpires>0 && hooks.now()>e.authorityExpires) r.status="rejected:device_snapshot_expired";
        else {
            const auto finalGap=hooks.finalGap?hooks.finalGap():std::optional<Bytes>{e.systemGap};
            if(!finalGap) {r.status="rejected:final_forecast_or_target_unknown";return r;}
            const Bytes donor=std::min(e.rssAnonBytes,e.denominatorBytes);
            const Bytes room=sub(sub(donor,request.keepBytes),128*1024);
            const Bytes cap=std::min({request.reservedBytes,room,e.systemGap,*finalGap,e.swapRoom});
            const unsigned ceiling=ratioCeiling(request);
            const unsigned budgetRatio=request.knownStrength?ratioWithin(cap,e.denominatorBytes,ceiling):hooks.ratioForBudget(cap,e.denominatorBytes);
            const unsigned ratio=std::min({request.plannedRatio,ceiling,budgetRatio});
            // Divide first so the nominal byte calculation cannot overflow.
            const Bytes nominal=(e.denominatorBytes/100)*ratio+(e.denominatorBytes%100)*ratio/100;
            if(!ratio || nominal<request.minBatch || nominal>request.reservedBytes || nominal>cap)
                r.status="rejected:fresh_budget_or_keep_below_minimum";
            else if(hooks.cancelled()) r.status="rejected:stopping";
            else if(e.authorityExpires>0 && hooks.now()>e.authorityExpires) r.status="rejected:device_snapshot_expired_at_write";
            else if(const double finalAge=hooks.now()-request.reservedAt;
                !std::isfinite(finalAge) || finalAge<0 || finalAge>request.maxAgeSeconds)
                r.status="rejected:request_stale_at_write";
            else if(const double forecastAgeAtWrite=hooks.now()-request.forecastSampledAt;
                !std::isfinite(forecastAgeAtWrite) || forecastAgeAtWrite<0 || forecastAgeAtWrite>=request.forecastMaxAgeSeconds)
                r.status="rejected:forecast_expired_at_write";
            else if(!deadlineAllows(request,hooks.now()))
                r.status="rejected:known_future_deadline_at_write";
            else {
                const std::string payload=std::to_string(ratio);
                r.submittedRatio=ratio; r.nominalBytes=nominal; r.preAnonBytes=e.rssAnonBytes;
                r.startedAt=hooks.now(); r.writeAttempted=true;
                // Exactly one invocation. No retry, alternate backend, or refund.
                const auto written=hooks.write(payload);
                r.returnedAt=hooks.now(); r.writeMs=(*r.returnedAt-*r.startedAt)*1000.0;
                r.writeLength=written.length; r.error=written.error;
                if(written.length<0) r.status="write_failed_charge_retained";
                else if(written.length!=static_cast<long>(payload.size())) {
                    r.status="write_partial_unknown_charge_retained"; r.uncertain=true;
                } else {
                    hooks.feedbackDelay();
                    const auto feedback=hooks.feedback();
                    r.feedbackVerified=feedback.verified && feedback.rssAnonBytes>=request.keepBytes;
                    r.postAnonBytes=feedback.rssAnonBytes; r.feedbackFaults=feedback.faultCounters;
                    if(request.knownStrength && e.faultCounters.size()==1 && feedback.faultCounters.size()==1 &&
                       e.faultCounters.begin()->first==request.members.begin()->first &&
                       feedback.faultCounters.begin()->first==request.members.begin()->first) {
                        const auto before=e.faultCounters.begin()->second,after=feedback.faultCounters.begin()->second;
                        r.preparationFaultSafe=after.first>=before.first && after.second==before.second && after.first-before.first<=1024;
                    }
                    r.status=r.feedbackVerified?"write_accepted_feedback_verified":"write_accepted_feedback_unknown";
                }
            }
        }
    } catch(const std::exception& e) {
        r.status=std::string(r.writeAttempted.value_or(false)?"write_or_feedback_unknown:":"rejected:")+e.what();
        r.uncertain=r.writeAttempted.value_or(false) && !r.writeLength;
    } catch(...) { r.status="worker_exception_unknown"; r.uncertain=true; }
    // Every new daily app halts admission after accepted/partial write with unknown feedback.
    // A known negative write retains charge/backoff without claiming an unknown transfer.
    if(r.writeAttempted.value_or(false) &&
       r.writeLength && *r.writeLength>=0 && !r.feedbackVerified) r.uncertain=true;
    return r;
}
// Retry only fresh qualification discovery after these exact, known no-write
// refusals. Never refund reservation or reuse the same action/request.
inline bool ownedQualificationNoWrite(const Result& r) {
    if(r.request.qualification!="owned_os_snapshot" || !r.writeAttempted ||
       *r.writeAttempted || r.writeLength || r.uncertain) return false;
    return (r.request.backend=="force_shrink_anon" &&
        r.status=="rejected:owned_device_snapshot:aa_background_unknown_or_foreground") ||
        (r.request.backend=="force_swapout" && r.status=="disk_rejected:background_or_control_changed");
}
inline double backoffSeconds(const Result& r,double cooldown) {
    if(ownedQualificationNoWrite(r)) return std::max(10.0,cooldown);
    return !r.uncertain && (r.status=="write_accepted_feedback_verified" ||
        r.status=="disk_command_accepted_transition_observed")?std::max(10.0,cooldown):60.0;
}

// One resident worker, at most one task or unconsumed result. Poll never waits
// for the task. Timeout closes admission and does not interrupt a kernel write.
class Executor {
public:
    using Job=std::function<Result(const std::atomic<bool>&)>;
private:
    mutable std::mutex mutex_;
    std::condition_variable ready_;
    std::thread worker_;
    Job job_;
    std::optional<Result> result_;
    Request pending_;
    std::atomic<bool> cancelled_{false};
    bool stop_=false, occupied_=false, halted_=false, timedOut_=false;
    std::uint64_t lastSequence_=0;
    std::uint64_t maxSubmissions_=0,submissions_=0;
    void loop() {
        for(;;) {
            Job job;
            { std::unique_lock<std::mutex> lock(mutex_);
              ready_.wait(lock,[&]{return stop_ || static_cast<bool>(job_);});
              if(!job_) return;
              job=std::move(job_); job_={}; }
            Result completed;
            try { completed=job(cancelled_); }
            catch(...) { completed.request=pending_; completed.status="worker_exception_unknown";
                completed.writeAttempted.reset(); completed.uncertain=true; }
            { std::lock_guard<std::mutex> lock(mutex_);
              if(timedOut_) { completed.status="completed_after_timeout:"+completed.status; completed.uncertain=true; }
              result_=std::move(completed);
              if(result_->uncertain) halted_=true; }
        }
    }
public:
    explicit Executor(std::uint64_t maximum=0):maxSubmissions_(maximum) { worker_=std::thread([this]{loop();}); }
    ~Executor() { join(); }
    Executor(const Executor&)=delete;
    Executor& operator=(const Executor&)=delete;
    bool busy() const { std::lock_guard<std::mutex> lock(mutex_); return occupied_ || halted_ || stop_ ||
        (maxSubmissions_ && submissions_>=maxSubmissions_); }
    bool submit(const Request& request,const Job& job,const std::function<void()>& reserve) {
        // Allocate owned copies before charging. Moves after reservation cannot
        // allocate a second task or lose a charged task on a copy exception.
        Request owned=request; Job ownedJob=job;
        std::lock_guard<std::mutex> lock(mutex_);
        if(occupied_ || halted_ || stop_ || (maxSubmissions_ && submissions_>=maxSubmissions_) ||
           request.sequence<=lastSequence_ || request.actionId.empty()) return false;
        // Caller charges shared app/global limits and flushes its reservation
        // ledger before the worker can see this task. A failed ledger blocks it.
        try { reserve(); } catch(...) { halted_=true; throw; }
        ++submissions_; // Only a successful precharge can consume a submission; never refunded.
        pending_=std::move(owned); lastSequence_=request.sequence; occupied_=true; cancelled_=false;
        job_=std::move(ownedJob); ready_.notify_one(); return true;
    }
    std::optional<Result> poll() {
        std::unique_lock<std::mutex> lock(mutex_,std::try_to_lock);
        if(!lock || !result_) return {};
        auto r=std::move(result_); result_.reset(); occupied_=false; return r;
    }
    std::optional<Result> timeout(double now,double seconds) {
        std::unique_lock<std::mutex> lock(mutex_,std::try_to_lock);
        if(!lock || !occupied_ || result_ || halted_ || !std::isfinite(now) || now-pending_.reservedAt<seconds) return {};
        halted_=true; timedOut_=true; cancelled_=true;
        Result r; r.request=pending_; r.status="pending_timeout_unknown_admission_halted";
        r.writeAttempted.reset(); r.uncertain=true; return r;
    }
    void join() {
        { std::lock_guard<std::mutex> lock(mutex_); stop_=true; cancelled_=true; ready_.notify_one(); }
        // No detach: an already blocked kernel write may delay shutdown.
        if(worker_.joinable()) worker_.join();
    }
};
} // namespace ms::harmony_v1::daily
