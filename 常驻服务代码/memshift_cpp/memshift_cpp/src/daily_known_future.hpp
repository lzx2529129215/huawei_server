#pragma once
#include "device_oracle_evidence.hpp"
namespace ms::harmony_v1::daily::known {
// Ideal device-registered future input only. Execution stays on the daily worker.
inline Bytes target(Bytes needed,Bytes reserve,Bytes total) {
    if(!needed || needed>Unlimited-reserve || !total || needed+reserve>total)
        throw std::runtime_error("known_future_target_invalid");
    return needed+reserve;
}
inline bool sameRegistration(const OracleRequest& a,const OracleRequest& b) {
    if(a.id!=b.id || a.caseId!=b.caseId || a.launch!=b.launch || a.needed!=b.needed ||
       a.planFingerprint!=b.planFingerprint || a.donors.size()!=b.donors.size()) return false;
    for(std::size_t i=0;i<a.donors.size();++i) {
        const auto& x=a.donors[i];const auto& y=b.donors[i];
        if(x.bundle!=y.bundle || x.uid!=y.uid || x.pid!=y.pid || x.start!=y.start) return false;
    }
    return true; // Fresh proof sequence may advance; registration may not.
}
inline void validate(const OracleRequest& e,double now,Bytes total,Bytes reserve,double preparationSeconds=35) {
    (void)target(e.needed,reserve,total);
    if(e.schemaVersion!=2 || e.id.empty() || e.id!=e.caseId+"_peak_transition" ||
       e.planFingerprint.size()!=64 || e.planFingerprint.find_first_not_of("0123456789abcdef")!=std::string::npos ||
       !e.proofSequence || !std::isfinite(now) || !std::isfinite(e.launch) ||
       !std::isfinite(preparationSeconds) || preparationSeconds<35 || preparationSeconds>300 ||
       now<e.launch-preparationSeconds || now>=e.launch || e.donors.size()!=13)
        throw std::runtime_error("known_future_event_unknown_outside_window_or_expired");
    std::set<std::string> bundles,uids;std::set<std::uint64_t> pids;
    for(const auto& d:e.donors) {
        bool owned=false;for(unsigned i=1;i<=13;++i) if(d.bundle=="io.memscene.scene4.workload.a"+std::to_string(i)) owned=true;
        const auto uid=uintValue(d.uid);
        if(!owned || !uid || !d.pid || !d.start || !d.dailyReady || !std::isfinite(d.readyAt) ||
           d.readyAt<=0 || d.readyAt>=e.launch || !bundles.insert(d.bundle).second ||
           !uids.insert(d.uid).second || !pids.insert(d.pid).second || !d.procVerified ||
           d.procStart!=d.start || d.aaPid!=d.pid || d.aaUid!=uid || !d.windowReady || !d.lifecycleClear ||
           (d.aaState!="BACKGROUND" && d.aaState!="FOREGROUND"))
            throw std::runtime_error("known_future_registered_identity_incomplete");
        // This event validates registration. Per-target foreground/window protection
        // is independently re-read by the existing daily owned provider before write.
        for(double t:{d.aaObserved,d.windowObserved,d.procObserved,d.lifecycleObserved})
            if(!std::isfinite(t) || t<=0 || t>now || now-t>2.5)
                throw std::runtime_error("known_future_device_proof_expired");
    }
}
inline bool memberMatches(const OracleRequest& e,const std::string& leaf,
    const std::map<std::uint64_t,std::uint64_t>& members) {
    if(members.size()!=1) return false;
    for(const auto& d:e.donors) if(leaf=="100/"+d.bundle+"_"+d.uid)
        return members.begin()->first==d.pid && members.begin()->second==d.start;
    return false;
}
inline Bytes gap(const OracleRequest& e,Bytes reserve,const System& sys,double now,double preparationSeconds=35) {
    validate(e,now,sys.total,reserve,preparationSeconds);
    if(sys.free>sys.total) throw std::runtime_error("known_future_free_invalid");
    return sub(target(e.needed,reserve,sys.total),sys.free);
}
} // namespace ms::harmony_v1::daily::known
