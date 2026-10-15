#pragma once
namespace retry_core {
struct Journal {
    bool complete=false, dailyComplete=false, oneGeneration=false, uidMatches=false;
    bool peakRows=false, dailyRows=false, failed=false, stopped=false;
    unsigned long long pid=0, start=0, uid=0;
};
struct Authority {
    bool known=false, absent=false, identityMatches=false, singlePidAndMember=false;
};
struct Refusal {
    bool runtimeError=false, only10108101=false, failedToStart=false;
    bool outerTimeoutTransport=false;
};
inline bool explicitRefusal(const Journal& j, const Refusal& r) {
    return r.runtimeError && r.only10108101 && r.failedToStart &&
           !r.outerTimeoutTransport && !j.peakRows && !j.dailyRows;
}
inline unsigned absence(unsigned prior, const Authority& a) {
    return a.known && a.absent ? (prior<1000000 ? prior+1 : prior) : 0;
}
inline bool eligible(const Journal& j, const Refusal& r, unsigned absent) {
    if (explicitRefusal(j,r)) return absent>=2;
    return j.complete && j.oneGeneration && j.uidMatches &&
           (absent>=2 || (j.failed && j.stopped));
}
inline bool presubmit(const Journal& j, const Refusal& r, unsigned absent,
                      const Authority& a) {
    if (!eligible(j,r,absent) || !a.known) return false;
    if (a.absent) return true;
    return !explicitRefusal(j,r) && j.failed && j.stopped &&
           a.singlePidAndMember && a.identityMatches;
}
inline bool fresh(double now, double readStart) {
    return now>=readStart && now-readStart<=2.5;
}
inline bool deadline(double now, double origin, unsigned slot) {
    double end=origin+double(slot+1)*30.0;
    if (end>origin+330.0) end=origin+330.0;
    return slot>=1 && slot<=10 && now>=origin+double(slot)*30.0 && now<end;
}
// Eligibility consumes only a fresh completed read. The separately timed final
// authority read is checked by presubmit; it does not refresh this readStart.
inline bool authorize(const Journal& j, const Refusal& r, unsigned absent,
                      const Authority& a, double now, double readStart,
                      double origin, unsigned slot, bool sameBoot,
                      bool sameGeneration) {
    return sameBoot && sameGeneration && fresh(now,readStart) &&
           deadline(now,origin,slot) && presubmit(j,r,absent,a);
}
inline bool mayRead(double now, double cooldown, bool targetRead,
                    bool targetLaunch, unsigned active) {
    return !targetRead && !targetLaunch && active<4 && now>=cooldown;
}
enum class Decision { Unknown, KnownNotEligible, Eligible, MonitorOnly };
inline Decision decision(const Journal& j,const Refusal& r,unsigned absent,
                         const Authority& a,double now,double readStart,
                         bool sameBoot,bool sameGeneration) {
    // Even a remembered successful journal cannot prove current noneligibility.
    if(!sameBoot || !sameGeneration || !a.known || !fresh(now,readStart))
        return Decision::Unknown;
    if(!explicitRefusal(j,r) && !(j.complete && j.oneGeneration && j.uidMatches))
        return Decision::Unknown;
    return eligible(j,r,absent) ? Decision::Eligible : Decision::KnownNotEligible;
}

}
