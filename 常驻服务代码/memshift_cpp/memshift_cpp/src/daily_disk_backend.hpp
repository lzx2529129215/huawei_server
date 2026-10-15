#pragma once
#include "daily_action_executor.hpp"
#include <set>

namespace ms::harmony_v1::daily::disk {
// force_swapout accepts a command, not a byte count. These are logical whole
// work estimates, NOT an enforced physical IO limit or a queue-drain promise.
constexpr Bytes Margin=64*MiB;
// Logical planning slack, precharged once. Never a physical write limit.
constexpr Bytes PlanningHeadroom=16*MiB;
inline bool browserLeaf(const std::string& path) {
    const std::string prefix="100/com.huawei.hmos.browser_";
    if(path.rfind(prefix,0)!=0) return false;
    const auto uid=path.substr(prefix.size());
    if(uid.empty() || uid.find_first_not_of("0123456789")!=std::string::npos) return false;
    try { return uintValue(uid)>0; } catch(...) { return false; }
}
inline bool ownedLeaf(const std::string& path) {
    if(browserLeaf(path)) return true;
    for(unsigned i=1;i<=13;++i) {
        const std::string prefix="100/io.memscene.scene4.workload.a"+std::to_string(i)+"_";
        if(path.rfind(prefix,0)==0) {
            const auto uid=path.substr(prefix.size());
            if(!uid.empty() && uid.find_first_not_of("0123456789")==std::string::npos) {
                try { return uintValue(uid)>0; } catch(...) { return false; }
            }
        }
    }
    return false;
}
inline std::optional<Bytes> wholeWork(Bytes zram,Bytes anon) {
    if(!zram || zram>Unlimited-anon) return {};
    return zram+anon;
}
inline bool ownedProtectionScope(const Options& options,const std::string& path) {
    // Protection must survive unknown/multiple members and failed proof.
    return (options.dailyOwnedOsSnapshot || options.dailyDiskMode=="owned_apply") && ownedLeaf(path) &&
        (!browserLeaf(path) || options.dailyBrowserDonorProfile);
}
inline bool ownedQualification(const Options& options,const std::string& path,std::size_t members,
    bool isolatedProbe=false) {
    // Legacy owned_apply configurations retain their existing qualification.
    // The explicit opt-in permits the same fresh proof for anonymous-only work.
    if(options.dailyBrowserDonorProfile && browserLeaf(path))
        return options.dailyAsync && !options.oracleMode && options.oracleProofSource=="legacy" &&
            !isolatedProbe && options.dailyOwnedOsSnapshot &&
            (options.dailyDiskMode=="off" || options.dailyDiskMode=="owned_apply") && members>0;
    return options.dailyAsync && !options.oracleMode && options.oracleProofSource=="legacy" &&
        !isolatedProbe && ownedProtectionScope(options,path) && members==1;
}
inline bool diskApplyEnabled(const Options& options,const std::string& path,std::size_t members,
    bool isolatedProbe=false) {
    return options.dailyDiskMode=="owned_apply" && ownedQualification(options,path,members,isolatedProbe);
}
inline bool registeredBrowserGroup(const Request& r) {
    if(r.knownEvent && r.knownEvent->schemaVersion==3 && r.knownEvent->donors.size()==1 &&
       r.knownEvent->donors.front().bundle=="com.huawei.hmos.browser" && !r.members.empty())
        return r.knownEvent->donors.front().members==r.members &&
            r.knownEvent->donors.front().memberUids==r.memberUids;
    return false;
}
inline std::string ownedBundle(const std::string& path) {
    return ownedLeaf(path)?path.substr(4,path.rfind('_')-4):std::string{};
}
inline bool targetProtected(const Options& options,const std::string& alias,const std::string& path,
    const std::set<std::string>& protectedApps) {
    return protectedApps.count(alias) ||
        (ownedProtectionScope(options,path) && protectedApps.count(ownedBundle(path)));
}
struct Plan { Bytes wholeWork=0,reservation=0; std::string reason; bool admitted=false; };
inline Plan plan(const std::string& path,Bytes zram,Bytes anon,Bytes keep,Bytes minimum,
    Bytes explicitLimit,Bytes sharedRoom,Bytes gap,Bytes swapRoom,bool reserveHeadroom=true,
    const Request* authority=nullptr) {
    Plan p;
    const auto work=wholeWork(zram,anon);
    if(work) p.wholeWork=*work;
    if(!ownedLeaf(path)) p.reason="disk_scope_not_owned_singleton_test_leaf";
    else if(!work || zram<minimum) p.reason="disk_pool_missing_below_minimum_or_overflow";
    else if(keep<((authority && authority->groupPath==path && authority->keepBytes==keep &&
        capacityAuthority(*authority))?128:512)*MiB || anon<keep) p.reason="disk_keep_floor_not_satisfied";
    else if(*work>Unlimited-Margin-(reserveHeadroom?PlanningHeadroom:0)) p.reason="disk_reservation_overflow";
    else {
        p.reservation=*work+Margin+(reserveHeadroom?PlanningHeadroom:0);
        const Bytes cap=std::min({explicitLimit,sharedRoom,gap,swapRoom});
        if(!explicitLimit || p.reservation>cap) p.reason="whole_work_exceeds_explicit_shared_gap_or_swap_room";
        else { p.admitted=true; p.reason="owned_whole_work_estimate_admitted_not_physical_limit"; }
    }
    return p;
}
struct Evidence {
    daily::Evidence base;
    Bytes zramBytes=0,eswapBytes=0,ncZramBytes=0;
    bool poolKnown=false,deviceSnapshotVerified=false,backendEnabled=false;
    double oldestProof=0;
    std::string proofReason;
};
struct Hooks {
    std::function<Evidence()> probe;
    std::function<daily::WriteReceipt(const std::string&)> write;
    std::function<void()> feedbackDelay;
    std::function<double()> now;
    std::function<bool()> cancelled;
    std::function<std::optional<Bytes>()> finalGap;
};
inline std::string blocked(const Request& r,const Evidence& e,double now,Bytes limit,
    std::string* budgetDiagnostic=nullptr) {
    if(r.backend!="force_swapout" || r.qualification!="owned_os_snapshot") return "owned_device_qualification_required";
    const double age=now-r.reservedAt,forecastAge=now-r.forecastSampledAt;
    if(!std::isfinite(age) || age<0 || age>r.maxAgeSeconds || !std::isfinite(forecastAge) ||
       forecastAge<0 || forecastAge>=r.forecastMaxAgeSeconds) return "request_or_forecast_expired";
    if(!e.base.blocked.empty()) return e.base.blocked;
    if(e.base.groupDevice!=r.groupDevice || e.base.groupInode!=r.groupInode ||
       (r.members.size()!=1 && !registeredBrowserGroup(r)) || e.base.members!=r.members)
        return "member_or_group_identity_changed";
    if(const auto protectedReason=pairProtection(r,e.base);!protectedReason.empty()) return protectedReason;
    if(!e.base.background || !e.base.controlActive) return "background_or_control_changed";
    if(!e.deviceSnapshotVerified || !std::isfinite(e.oldestProof) || e.oldestProof<=0 ||
       now<e.oldestProof || now-e.oldestProof>2.5) return "device_snapshot_unknown_or_expired:"+e.proofReason;
    if(!e.backendEnabled) return "disk_backend_disabled_or_unknown";
    if(!e.poolKnown || e.ncZramBytes!=0) return "disk_pool_unknown_or_nc_nonzero";
    const auto p=plan(r.groupPath,e.zramBytes,e.base.rssAnonBytes,r.keepBytes,r.minBatch,
        limit,r.reservedBytes,e.base.systemGap,e.base.swapRoom,false,&r);
    if(budgetDiagnostic) *budgetDiagnostic="fresh_work="+std::to_string(p.wholeWork)+
        ";required_with_margin="+std::to_string(p.reservation)+";reserved_cap="+std::to_string(r.reservedBytes)+
        ";explicit_cap="+std::to_string(limit)+";fresh_gap="+std::to_string(e.base.systemGap)+
        ";swap_room="+std::to_string(e.base.swapRoom);
    return p.admitted?std::string{}:p.reason;
}
inline Result execute(const Request& r,Bytes limit,const Hooks& h) {
    Result out; out.request=r;
    try {
        if((r.knownStrength || r.knownCapacity) && !strengthAuthority(r)) {out.status="disk_rejected:known_strength_authority";return out;}
        if(h.cancelled()) {out.status="disk_rejected:stopping";return out;}
        auto before=h.probe();
        const auto refusal=blocked(r,before,h.now(),limit,&out.diskBudgetDiagnostic);
        if(!refusal.empty()) {out.status="disk_rejected:"+refusal;return out;}
        if(h.finalGap) {
            const auto gap=h.finalGap();
            if(!gap) {out.status="disk_rejected:final_forecast_or_target_unknown";return out;}
            before.base.systemGap=std::min(before.base.systemGap,*gap);
        }
        // The adapter opens the writer and reads status BEFORE this final probe.
        if(h.cancelled()) {out.status="disk_rejected:stopping_at_write";return out;}
        if(const auto final=blocked(r,before,h.now(),limit,&out.diskBudgetDiagnostic);!final.empty()) {
            out.status="disk_rejected_at_write:"+final;return out;
        }
        if(!daily::deadlineAllows(r,h.now())) {
            out.status="disk_rejected:known_future_deadline_at_write";return out;
        }
        out.nominalBytes=wholeWork(before.zramBytes,before.base.rssAnonBytes).value();
        out.preAnonBytes=before.base.rssAnonBytes;
        out.diskZramBefore=before.zramBytes;out.diskEswapBefore=before.eswapBytes;
        out.startedAt=h.now();out.writeAttempted=true;
        const auto written=h.write("1"); // command only; never encode a byte budget here
        out.returnedAt=h.now();out.writeMs=(*out.returnedAt-*out.startedAt)*1000;
        out.writeLength=written.length;out.error=written.error;
        if(written.length<0) out.status="disk_write_failed_charge_retained";
        else if(written.length!=1) {out.status="disk_partial_write_unknown_charge_retained";out.uncertain=true;}
        else {
            h.feedbackDelay();
            const auto after=h.probe();
            out.postAnonBytes=after.base.rssAnonBytes;
            out.diskZramAfter=after.zramBytes;out.diskEswapAfter=after.eswapBytes;
            // Post-feedback does not re-require a pool minimum or positive gap.
            const double age=h.now()-after.oldestProof;
            out.feedbackVerified=after.deviceSnapshotVerified && std::isfinite(age) && age>=0 && age<=2.5 &&
                after.base.groupDevice==r.groupDevice && after.base.groupInode==r.groupInode &&
                after.base.members==r.members && (r.members.size()==1 || registeredBrowserGroup(r)) &&
                after.base.background && after.base.controlActive &&
                after.base.blocked.empty() && after.poolKnown && after.ncZramBytes==0 &&
                after.base.rssAnonBytes>=r.keepBytes;
            const bool transition=after.eswapBytes>before.eswapBytes && after.zramBytes<before.zramBytes;
            out.status=out.feedbackVerified && transition?"disk_command_accepted_transition_observed":
                "disk_command_accepted_effect_unknown";
            if(after.eswapBytes>before.eswapBytes && after.eswapBytes-before.eswapBytes>r.reservedBytes) {
                out.status="disk_transfer_exceeded_logical_reservation";out.uncertain=true;
            } else if(!out.feedbackVerified || !transition) out.uncertain=true;
            // No MemFree gain or IO completion claim follows from this transition.
        }
    } catch(const std::exception& e) {
        out.status=std::string(out.writeAttempted.value_or(false)?"disk_write_or_feedback_unknown:":"disk_rejected:")+e.what();
        out.uncertain=out.writeAttempted.value_or(false);
    } catch(...) {out.status="disk_worker_exception_unknown";out.uncertain=true;}
    return out;
}
} // namespace ms::harmony_v1::daily::disk
