// Fake-only fixture: no proc/sysfs reads, reclaim, service control or large allocation.
#include "../src/daily_disk_backend.hpp"
#include "../src/daily_action_schedule.hpp"
#include "../src/io.hpp"
#include <iostream>
#include <chrono>
#include <csignal>
#include <unistd.h>
using namespace ms;
using namespace ms::harmony_v1;
namespace {
void require(bool yes,const char* why) { if(!yes) throw std::runtime_error(why); }
daily::Request request(std::uint64_t sequence=1) {
    daily::Request r; r.actionId="fake_generation_daily_"+std::to_string(sequence); r.sequence=sequence;
    r.app="fake"; r.members={{7,91}}; r.groupDevice=1; r.groupInode=2;
    r.reservedBytes=64*MiB; r.keepBytes=512*MiB; r.minBatch=MiB; r.plannedRatio=20;
    r.reservedAt=100; r.forecastSampledAt=100; return r;
}
struct Fixture {
    daily::Evidence e;
    unsigned writes=0;
    double now=100;
    long returned=-2;
    bool feedback=true;
    Fixture() { e.groupDevice=1; e.groupInode=2; e.members={{7,91}};
        e.background=true; e.controlActive=true; e.rssAnonBytes=1024*MiB;
        e.denominatorBytes=1024*MiB; e.systemGap=64*MiB; e.swapRoom=64*MiB; }
    daily::Hooks hooks() {
        daily::Hooks h;
        h.probe=[&]{return e;}; h.cancelled=[] {return false;}; h.now=[&]{return now;};
        h.ratioForBudget=[](Bytes cap,Bytes denominator) {return denominator?static_cast<unsigned>(std::min<Bytes>(20,(cap*100)/denominator)):0U;};
        h.write=[&](const std::string& payload) {++writes;now+=0.01;
            return daily::WriteReceipt{returned==-2?static_cast<long>(payload.size()):returned,returned==-1?5:0};};
        h.feedbackDelay=[]{}; h.feedback=[&]{return daily::Feedback{feedback,900*MiB};};
        return h;
    }
};
daily::Result await(daily::Executor& worker) {
    for(unsigned i=0;i<2000;++i) { if(auto result=worker.poll()) return *result;
        std::this_thread::sleep_for(std::chrono::milliseconds(1)); }
    throw std::runtime_error("fake worker completion timeout");
}
OracleRequest knownEvent() {
    OracleRequest e;e.id="s4_abcdef_c0000_peak_transition";e.caseId="s4_abcdef_c0000";
    e.schemaVersion=2;e.launch=130;e.needed=19968*MiB;e.planFingerprint=std::string(64,'a');e.proofSequence=1;
    for(unsigned i=1;i<=13;++i) {
        OracleDonor d;d.bundle="io.memscene.scene4.workload.a"+std::to_string(i);d.uid=std::to_string(100+i);
        d.pid=1000+i;d.start=900+i;d.procStart=d.start;d.aaPid=d.pid;d.aaUid=100+i;
        d.dailyReady=d.procVerified=d.windowReady=d.lifecycleClear=true;d.readyAt=90;d.aaState="BACKGROUND";
        d.aaObserved=d.windowObserved=d.procObserved=d.lifecycleObserved=100;e.donors.push_back(d);
    }
    return e;
}
void dailyKnownFutureTests() {
    auto e=knownEvent();System s;s.total=32ULL*1024*MiB;s.free=14ULL*1024*MiB;
    require(daily::known::gap(e,1024*MiB,s,101)==6656*MiB,"known demand gap must use Free only and exceed half total when required");
    s.free=22ULL*1024*MiB;require(daily::known::gap(e,1024*MiB,s,101)==0,"known target met means no gap");
    const auto rejects=[&](OracleRequest bad,double now,System sample) {bool caught=false;try{(void)daily::known::gap(bad,1024*MiB,sample,now);}catch(...){caught=true;}require(caught,"invalid or stale known event must reject");};
    rejects(e,130,s);rejects(e,94,s);rejects(e,103,s);auto bad=e;bad.needed=0;rejects(bad,101,s);
    bad=e;bad.donors.pop_back();rejects(bad,101,s);bad=e;bad.donors[0].procStart++;rejects(bad,101,s);
    bad=e;bad.donors[0].bundle="office.app";rejects(bad,101,s);bad=e;bad.planFingerprint="";rejects(bad,101,s);
    bad=e;bad.needed=Unlimited;rejects(bad,101,s);auto invalid=s;invalid.free=invalid.total+1;rejects(e,101,invalid);
    auto early=e;early.launch=190;
    rejects(early,101,s); // Default35 must still reject an event89s away.
    require(daily::known::gap(early,1024*MiB,s,101,95)==0,"extended horizon accepts fresh registered event");
    bool stale=false;try{daily::known::gap(early,1024*MiB,s,103,95);}catch(...){stale=true;}
    require(stale,"longer lead never extends2.5s device proof TTL");
    bool expired=false;try{daily::known::gap(early,1024*MiB,s,190,95);}catch(...){expired=true;}
    require(expired,"longer lead never permits action at T");
    auto changed=e;changed.proofSequence++;require(daily::known::sameRegistration(e,changed),"fresh proof sequence may advance");
    changed.needed++;require(!daily::known::sameRegistration(e,changed),"registered need frozen");changed=e;changed.donors[0].start++;
    require(!daily::known::sameRegistration(e,changed),"registered PID generation frozen");
    require(daily::known::memberMatches(e,"100/io.memscene.scene4.workload.a1_101",{{1001,901}}),"exact event member");
    require(!daily::known::memberMatches(e,"100/io.memscene.scene4.workload.a1_101",{{1001,902}}),"event member reuse rejected");
    Fixture f;auto r=request();r.hardDeadline=100.5;auto h=f.hooks();
    h.finalGap=[&]()->std::optional<Bytes>{f.now=100.5;return 64*MiB;};
    require(daily::execute(r,h).status=="rejected:known_future_deadline_at_write" && f.writes==0,"T reached after final evidence means no anonymous write");
    r.hardDeadline=101;h=f.hooks();require(daily::execute(r,h).feedbackVerified && f.writes==1,"preT daily action remains executable");
    r=request(2);r.hardDeadline=102;require(daily::execute(r,f.hooks()).feedbackVerified && f.writes==2,"same donor may receive a new independently budgeted daily action; no event one-shot key");
    r.hardDeadline=std::numeric_limits<double>::quiet_NaN();require(!daily::deadlineAllows(r,101),"invalid deadline rejects");
    std::string cfg="daily_async=true\ndaily_owned_os_snapshot=true\ndaily_known_future_free_only=true\ndaily_known_plan_file=/fake/plan\ndaily_known_evidence_ledger=/fake/evidence\n";
    for(unsigned i=1;i<=13;++i)cfg+="[app a"+std::to_string(i)+"]\npath=100/io.memscene.scene4.workload.a"+std::to_string(i)+"_"+std::to_string(100+i)+"\nreclaim=true\nmin_keep_mb=512\n";
    require(parseConfig(cfg).options.dailyKnownFutureFreeOnly,"known daily scope valid without oracle executor");
    bool mixed=false;try{parseConfig("daily_recent_reserve=true\n"+cfg);}catch(...){mixed=true;}require(mixed,"known and recent inputs cannot mix");
    bool wrong=false;try{parseConfig("daily_async=true\ndaily_owned_os_snapshot=true\ndaily_known_future_free_only=true\ndaily_known_plan_file=/fake/plan\ndaily_known_evidence_ledger=/fake/evidence\n[app office]\npath=100/office_101\nreclaim=true\n");}catch(...){wrong=true;}require(wrong,"office cannot opt into ideal owned event scope");
}
void knownStrengthAndPairTests() {
    auto r=request();r.knownEvent=knownEvent();r.hardDeadline=130;r.knownStrength=true;
    r.groupPath="100/io.memscene.scene4.workload.a1_101";r.members={{1001,901}};
    r.qualification="owned_os_snapshot";r.reservedBytes=1024*MiB;r.plannedRatio=60;
    Fixture f;f.e.members=r.members;f.e.rssAnonBytes=2048*MiB;f.e.denominatorBytes=1536*MiB;
    f.e.systemGap=f.e.swapRoom=2048*MiB;f.e.faultCounters={{1001,{50,1}}};
    auto h=f.hooks();h.feedback=[&]{return daily::Feedback{true,900*MiB,{{1001,{60,1}}}};};
    const auto good=daily::execute(r,h);
    require(good.submittedRatio==60 && good.nominalBytes==daily::ratioNominal(1536*MiB,60) &&
        good.nominalBytes<=r.reservedBytes && good.preparationFaultSafe,"known opt-in sixty percent within reserved/fresh cap");
    auto old=r;old.knownStrength=false;const auto oldResult=daily::execute(old,h);
    require(oldResult.submittedRatio==20,"default daily hard twenty remains equivalent");
    for(unsigned kind=0;kind<5;++kind) {
        auto bad=r;
        if(kind==0) bad.knownEvent.reset();
        if(kind==1) bad.qualification="office_score_adj";
        if(kind==2) bad.groupPath="100/com.huawei.hmos.browser_101";
        if(kind==3) bad.members={{1001,902}};
        if(kind==4) bad.knownEvent->donors.pop_back();
        const auto count=f.writes;const auto denied=daily::execute(bad,h);
        require(!denied.writeAttempted.value_or(false) && f.writes==count,"numeric ratio opt-in cannot grant unregistered/office authority");
    }
    auto reduced=r;reduced.reservedBytes=128*MiB;auto small=daily::execute(reduced,h);
    require(small.nominalBytes<=128*MiB && small.submittedRatio<60,"fresh ratio only shrinks to reserved budget");
    f.e.rssAnonBytes=600*MiB;auto floor=daily::execute(r,h);
    require(floor.nominalBytes<=88*MiB-128*1024,"worker uses min RSS/denominator minus keep and slack");
    f.e.background=false;const auto count=f.writes;require(!daily::execute(r,h).writeAttempted.value_or(false) && count==f.writes,"foreground never gains strength authority");
    f.e.background=true;f.e.members={{1001,902}};
    require(!daily::execute(r,h).writeAttempted.value_or(false),"fresh start_ticks change prevents high ratio write");
    require(daily::ratioWithin(MiB,1536*MiB,60)==0,"sub quantum never rounds up");
    daily::PairedDisk pair;pair.completed(good,true);require(pair.isTarget(0),"successful positive preparation arms one pairing opportunity");
    auto disk=r;disk.backend="force_swapout";disk.actionId="fake_new_disk_action";disk.sequence=2;
    require(pair.bind(disk) && disk.pairedAfter==good.request.actionId && disk.actionId!=disk.pairedAfter,"pair uses a distinct action and immutable origin");
    daily::Evidence current;current.members=disk.members;current.faultCounters={{1001,{61,1}}};current.rssAnonBytes=900*MiB;
    require(daily::pairProtection(disk,current).empty(),"new pair permits fresh no-fault unchanged RSS");
    current.faultCounters={{1001,{61,2}}};require(!daily::pairProtection(disk,current).empty(),"latest worker major fault blocks pair");
    current.faultCounters={{1001,{1085,1}}};require(!daily::pairProtection(disk,current).empty(),"latest worker minor fault guard blocks pair");
    current.faultCounters={{1001,{61,1}}};current.rssAnonBytes=902*MiB;
    require(!daily::pairProtection(disk,current).empty(),"latest rebound blocks pair");
    auto changed=disk;changed.groupInode++;require(!pair.bind(changed),"memcg rebuild cannot inherit pair");
    changed=disk;changed.members={{1001,902}};require(!pair.bind(changed),"new PID generation cannot inherit pair");
    auto event=*r.knownEvent;event.planFingerprint=std::string(64,'b');pair.refresh(event,101);require(!pair.isTarget(0),"new event invalidates pending pair");
    pair.completed(good,true);pair.refresh(r.knownEvent,130);require(!pair.isTarget(0),"T expires pair, no late disk");
    auto unsafe=good;unsafe.preparationFaultSafe=false;pair.completed(unsafe,true);require(!pair.isTarget(0),"faults during anon write/feedback prevent pairing");
    unsafe=good;unsafe.uncertain=true;pair.completed(unsafe,true);require(!pair.isTarget(0),"unknown preparation never chains");
    pair.completed(good,true);pair.cancel();require(!pair.bind(disk),"consumed/rejected opportunity cannot retry or fallback");
    // Real production disk executor with injected evidence and a counted fake writer.
    unsigned writes=0;daily::disk::Evidence e;e.base.groupDevice=1;e.base.groupInode=2;e.base.members=r.members;
    e.base.faultCounters={{1001,{61,1}}};e.base.rssAnonBytes=900*MiB;e.base.background=e.base.controlActive=true;
    e.base.systemGap=e.base.swapRoom=2048*MiB;e.poolKnown=false;e.deviceSnapshotVerified=e.backendEnabled=true;e.oldestProof=100;
    daily::disk::Hooks dh;dh.now=[] {return 100.5;};dh.cancelled=[] {return false;};dh.feedbackDelay=[] {};
    dh.probe=[&] {return e;};dh.write=[&](const std::string&) {++writes;return daily::WriteReceipt{1,0};};
    disk.reservedBytes=1536*MiB;
    require(!daily::disk::execute(disk,2048*MiB,dh).writeAttempted.value_or(false) && writes==0,"no fresh pool means no actual disk writer invocation");
    e.poolKnown=true;e.zramBytes=128*MiB;e.base.faultCounters={{1001,{61,2}}};
    require(!daily::disk::execute(disk,2048*MiB,dh).writeAttempted.value_or(false) && writes==0,"paired disk production guard rejects fresh faults before callback");
    // A preparation fault can occur after pre-probe, while the sampling loop has
    // already advanced its previous counters. It must not be lost at feedback.
    f.e.rssAnonBytes=2048*MiB;f.e.members=r.members;h.feedback=[] {return daily::Feedback{true,900*MiB,{{1001,{60,2}}}};};
    auto faulted=daily::execute(r,h);require(faulted.feedbackVerified && !faulted.preparationFaultSafe,"prepare-time major fault preserves success receipt but forbids pair");
    h.feedback=[] {return daily::Feedback{true,900*MiB,{}};};
    require(!daily::execute(r,h).preparationFaultSafe,"missing feedback faults do not fabricate safe pairing");
    std::string cfg="daily_async=true\ndaily_owned_os_snapshot=true\ndaily_known_future_free_only=true\ndaily_known_plan_file=/fake/plan\ndaily_known_evidence_ledger=/fake/evidence\ndaily_disk_mode=owned_apply\ndaily_disk_whole_work_mb=2048\nmax_per_minute_mb=40960\napp_per_minute_mb=8192\n";
    for(unsigned i=1;i<=13;++i) cfg+="[app a"+std::to_string(i)+"]\npath=100/io.memscene.scene4.workload.a"+std::to_string(i)+"_"+std::to_string(100+i)+"\nreclaim=true\nmin_keep_mb=512\n";
    require(!parseConfig(cfg).options.dailyKnownStrength,"new strength defaults off");
    require(parseConfig("daily_known_strength=true\nbatch_mb=1024\n"+cfg).options.dailyKnownStrength,"explicit known exact13 mixed may reserve1024");
    bool rejected=false;try {parseConfig("batch_mb=1024\n"+cfg);}catch(...) {rejected=true;}require(rejected,"default nonoracle cap256 unchanged");
    rejected=false;try {parseConfig("daily_known_strength=true\ndaily_async=true\n[app office]\npath=100/office_10\n");}catch(...) {rejected=true;}require(rejected,"office cannot opt into known strength");
}

void knownCapacityTests() {
    auto r=request();r.knownEvent=knownEvent();r.hardDeadline=130;r.knownStrength=true;r.knownCapacity=true;
    r.groupPath="100/io.memscene.scene4.workload.a1_101";r.members={{1001,901}};
    r.qualification="owned_os_snapshot";r.keepBytes=128*MiB;r.minBatch=64*MiB;
    r.reservedBytes=1536*MiB;r.plannedRatio=95;
    require(!Options{}.dailyKnownCapacity && daily::keepFloor(128*MiB,false)==512*MiB &&
        daily::keepFloor(0,true)==128*1024,"default512 and isolated probe128KiB remain unchanged");
    require(daily::capacityAuthority(r) && daily::policyKeep(128*MiB,false,&r)==128*MiB &&
        daily::ratioCeiling(r)==95,"low floor and95 require complete known-owned authority");
    Options options;options.dailyKnownCapacity=options.dailyKnownStrength=options.dailyKnownFutureFreeOnly=true;
    options.dailyAsync=options.dailyOwnedOsSnapshot=true;options.dailyDiskMode="owned_apply";options.minBatch=64*MiB;
    require(daily::capacityAdapterAllows(options,r) && !daily::capacityAdapterAllows(options,r,true),
        "production adapter requires matching explicit options and excludes probe");
    for(unsigned kind=0;kind<7;++kind) {
        auto bad=options;
        if(kind==0) bad.dailyKnownCapacity=false;if(kind==1) bad.dailyKnownStrength=false;
        if(kind==2) bad.dailyKnownFutureFreeOnly=false;if(kind==3) bad.dailyOwnedOsSnapshot=false;
        if(kind==4) bad.dailyDiskMode="off";if(kind==5) bad.oracleMode=true;if(kind==6) bad.dailyAsync=false;
        require(!daily::capacityAdapterAllows(bad,r),"request flag cannot bypass production option authority");
    }
    Fixture f;f.e.members=r.members;f.e.rssAnonBytes=f.e.denominatorBytes=1536*MiB;
    f.e.systemGap=f.e.swapRoom=4096*MiB;f.e.faultCounters={{1001,{50,1}}};
    auto h=f.hooks();h.feedback=[] {return daily::Feedback{true,150*MiB,{{1001,{60,1}}}};};
    const auto good=daily::execute(r,h);
    require(good.submittedRatio==91 && good.nominalBytes==daily::ratioNominal(1536*MiB,91) &&
        good.nominalBytes<=1536*MiB-128*MiB-128*1024 && good.feedbackVerified && good.preparationFaultSafe,
        "1536MiB donor floors request to91 percent;95 is a ceiling, never a forced percentage");
    auto old=r;old.knownCapacity=false;old.keepBytes=512*MiB;
    auto oldHooks=h;oldHooks.feedback=[] {return daily::Feedback{true,900*MiB,{{1001,{60,1}}}};};
    require(daily::execute(old,oldHooks).submittedRatio==60,"old strength512/60 unchanged");
    old.knownStrength=false;
    require(daily::execute(old,oldHooks).submittedRatio==20,"ordinary512/20 unchanged");
    for(unsigned kind=0;kind<8;++kind) {
        auto bad=r;
        if(kind==0) bad.knownEvent.reset();if(kind==1) bad.knownStrength=false;
        if(kind==2) bad.qualification="office_score_adj";if(kind==3) bad.groupPath="100/office_101";
        if(kind==4) bad.members={{1001,902}};if(kind==5) bad.knownEvent->donors.pop_back();
        if(kind==6) bad.keepBytes=127*MiB;if(kind==7) bad.minBatch=MiB;
        const auto before=f.writes;const auto denied=daily::execute(bad,h);
        require(!denied.writeAttempted.value_or(false) && f.writes==before && !daily::capacityAuthority(bad),
            "forged capacity flag, office, wrong identity, lower floor or small minimum never writes");
        require(daily::policyKeep(128*MiB,false,&bad)==512*MiB,"invalid authority cannot lower planning floor");
    }
    auto reduced=r;reduced.reservedBytes=100*MiB;
    const auto small=daily::execute(reduced,h);
    require(small.submittedRatio==6 && small.nominalBytes<=100*MiB,"fresh ceiling only shrinks to frozen shared reservation");
    reduced.reservedBytes=MiB;require(!daily::execute(reduced,h).writeAttempted.value_or(false),"one percent quantum is never rounded up");
    f.e.rssAnonBytes=f.e.denominatorBytes=184*MiB;
    require(!daily::execute(r,h).writeAttempted.value_or(false),"184MiB minus128 floor/slack is below64 minimum");
    f.e.rssAnonBytes=f.e.denominatorBytes=1536*MiB;f.e.systemGap=63*MiB;
    require(!daily::execute(r,h).writeAttempted.value_or(false),"fresh gap below64 stops, no expansion of precharge");
    f.e.systemGap=4096*MiB;f.e.background=false;
    require(!daily::execute(r,h).writeAttempted.value_or(false),"foreground still rejects capacity write");
    f.e.background=true;f.e.members={{1001,902}};
    require(!daily::execute(r,h).writeAttempted.value_or(false),"fresh PID reuse still rejects capacity write");
    f.e.members=r.members;
    auto deadline=h;deadline.finalGap=[&]()->std::optional<Bytes>{f.now=130;return 4096*MiB;};
    require(!daily::execute(r,deadline).writeAttempted.value_or(false),"T reached while collecting final evidence never writes");f.now=100;
    daily::PairedDisk pair;pair.completed(good,true);auto disk=r;disk.backend="force_swapout";disk.actionId="new_disk";
    require(pair.bind(disk) && disk.pairedAfter==good.request.actionId,"capacity pair has independent action and same authority");
    auto mismatched=disk;mismatched.knownCapacity=false;mismatched.keepBytes=512*MiB;
    require(!pair.bind(mismatched),"pair cannot cross capacity mode or keep floor");
    auto e=f.e;e.rssAnonBytes=150*MiB;e.faultCounters={{1001,{61,2}}};
    require(!daily::pairProtection(disk,e).empty(),"capacity pair still refuses latest fault");
    auto faulted=good;faulted.preparationFaultSafe=false;pair.completed(faulted,true);
    require(!pair.isTarget(0),"unsafe preparation never creates capacity pair");
    pair.completed(good,true);pair.refresh(r.knownEvent,130);require(!pair.isTarget(0),"T invalidates capacity pair");
    Fixture partial;partial.e=f.e;partial.returned=1;auto partialHooks=partial.hooks();
    daily::Executor worker;Bytes charged=0;
    require(worker.submit(r,[&](const std::atomic<bool>&){return daily::execute(r,partialHooks);},
        [&]{charged+=r.reservedBytes;}),"capacity action reserves before queue");
    const auto unknown=await(worker);
    require(unknown.uncertain && charged==r.reservedBytes && partial.writes==1 &&
        !worker.submit(r,[&](const std::atomic<bool>&){return daily::execute(r,partialHooks);},[&]{charged+=r.reservedBytes;}),
        "partial capacity receipt retains charge and globally halts without second writer");worker.join();
    std::string cfg="daily_async=true\ndaily_owned_os_snapshot=true\ndaily_known_strength=true\ndaily_known_future_free_only=true\ndaily_known_plan_file=/fake/plan\ndaily_known_evidence_ledger=/fake/evidence\ndaily_disk_mode=owned_apply\ndaily_disk_whole_work_mb=2048\nmax_per_minute_mb=40960\napp_per_minute_mb=8192\n";
    for(unsigned i=1;i<=13;++i)cfg+="[app a"+std::to_string(i)+"]\npath=100/io.memscene.scene4.workload.a"+std::to_string(i)+"_"+std::to_string(100+i)+"\nreclaim=true\nmin_keep_mb=128\n";
    const std::string capacity="daily_known_capacity=true\nbatch_mb=1536\nmin_batch_mb=64\n";
    auto extended=r;extended.knownEvent->launch=190;extended.hardDeadline=190;
    require(!daily::strengthAuthority(extended),"default request rejects89s horizon");
    extended.knownLeadSec=95;require(daily::strengthAuthority(extended),"immutable extended request validates fresh event");
    auto longOptions=parseConfig("daily_known_lead_sec=95\n"+capacity+cfg).options;
    require(daily::capacityAdapterAllows(longOptions,extended),"options bind extended request horizon");
    extended.knownLeadSec=96;require(!daily::capacityAdapterAllows(longOptions,extended),"request horizon mismatch rejected");
    const auto parsed=parseConfig(capacity+cfg);
    require(parsed.options.dailyKnownCapacity && parsed.options.batch==1536*MiB && parsed.options.minBatch==64*MiB,
        "explicit exact13 capacity parses1536/64");
    const auto rejects=[&](const std::string& text) {bool caught=false;try{(void)parseConfig(text);}catch(...){caught=true;}require(caught,"illegal capacity config rejects");};
    rejects("batch_mb=1536\nmin_batch_mb=64\n"+cfg);
    auto bad=capacity+cfg;bad.replace(bad.find("daily_known_strength=true"),25,"daily_known_strength=false");rejects(bad);
    bad=capacity+cfg;bad.replace(bad.find("min_batch_mb=64"),15,"min_batch_mb=1");rejects(bad);
    bad=capacity+cfg;bad.replace(bad.find("100/io.memscene.scene4.workload.a1_101"),36,"100/office_101");rejects(bad);
    bad=capacity+cfg;bad.replace(bad.find("min_keep_mb=128"),15,"min_keep_mb=127");rejects(bad);
}

void knownReturnSpacingTests() {
    daily::Result good;good.request=request();good.request.knownStrength=true;good.request.qualification="owned_os_snapshot";
    good.request.groupPath="100/io.memscene.scene4.workload.a1_101";good.request.members={{1001,901}};
    good.request.knownEvent=knownEvent();good.request.hardDeadline=130;good.status="write_accepted_feedback_verified";
    good.writeAttempted=true;good.writeLength=2;good.error=0;good.submittedRatio=60;good.feedbackVerified=true;
    good.startedAt=100;good.returnedAt=101;
    daily::Schedule late(2);late.complete(good,110,10,true);
    require(late.eligible(1,110) && !late.eligible(0,119.999) && late.eligible(0,120),"late poll can consume elapsed return spacing, but app cooldown stays poll based");
    daily::Schedule slow(2);good.returnedAt=105;slow.complete(good,105.1,10,true);
    require(!slow.eligible(1,105.999) && slow.eligible(1,106),"slow write requires full second after return, not start");
    require(slow.pairedEligible(0,106) && !slow.eligible(0,106),"pair only bypasses successful anon cooldown");
    slow.defer(0,105.2,30);require(!slow.pairedEligible(0,106),"pair cannot bypass fault/rebound explicit protection defer");
    for(unsigned kind=0;kind<11;++kind) {
        auto bad=good;
        if(kind==0) bad.startedAt.reset();if(kind==1) bad.returnedAt.reset();
        if(kind==2) bad.returnedAt=106;if(kind==3) bad.startedAt=106;
        if(kind==4) bad.returnedAt=std::numeric_limits<double>::quiet_NaN();
        if(kind==5) bad.startedAt=std::numeric_limits<double>::infinity();
        if(kind==6) {bad.writeLength=-1;bad.status="write_failed_charge_retained";}
        if(kind==7) bad.writeLength=1;if(kind==8) bad.writeAttempted=false;
        if(kind==9) bad.request.knownStrength=false;if(kind==10) bad.feedbackVerified=false;
        daily::Schedule s(2);s.complete(bad,105.1,10,true);
        require(!s.eligible(1,106.099) && s.eligible(1,106.1),"bad/no-write/nonowned receipt retains poll-plus-one gate");
    }
    auto unknown=good;unknown.uncertain=true;daily::Schedule halted(2);halted.complete(unknown,105.1,10,true);
    require(!halted.eligible(1,1000) && !halted.pairedEligible(0,1000),"unknown globally halts paired and ordinary admission");
    daily::Schedule ordinary(2);ordinary.complete(good,110,10);
    require(!ordinary.eligible(1,110.999) && ordinary.eligible(1,111),"default-off schedule retains original poll anchor");
}
void knownScanAdmissionTests() {
    daily::Result previous;previous.request=request();previous.request.appIndex=4;
    previous.status="write_failed_charge_retained";previous.writeAttempted=true;
    previous.writeLength=-1;previous.error=5;
    daily::Schedule frozen(13),legacy(13);
    frozen.complete(previous,100,10);legacy.complete(previous,100,10);
    frozen.submitted(4);legacy.submitted(4);
    // Scan starts at a6 before globalUntil=101, then wraps to a1 after it.
    // This models the observed per-row time progression, without real sleeps.
    const auto pick=[&](daily::Schedule& s,bool known,double start) {
        std::optional<std::size_t> chosen;
        for(std::size_t pos=0;pos<13;++pos) {
            const auto index=daily::scanIndex(pos,13,s.scanStart(),true);
            const double actual=start+0.026*pos;
            if(s.eligible(index,daily::scanQualificationTime(known,start,actual))) {chosen=index;break;}
        }
        return chosen;
    };
    require(pick(legacy,false,100.8)==0,"ordinary per-row clock behavior is unchanged");
    require(!pick(frozen,true,100.8),"crossing threshold mid scan cannot favor wrapped a1 over a6");
    auto next=pick(frozen,true,101.2);
    require(next==5,"next eligible scan retains cursor and starts with a6");
    frozen.submitted(*next);require(frozen.scanStart()==6,"successful selection advances original app index cursor");
    require(daily::scanIndex(0,13,frozen.scanStart(),false)==0,"old sync app ordering remains unchanged");
    const auto invalid=std::numeric_limits<double>::quiet_NaN();
    require(!frozen.eligible(6,daily::scanQualificationTime(true,invalid,102)),"invalid frozen time cannot grant admission");
    frozen.defer(6,101.2,30);
    require(!frozen.pairedEligible(6,daily::scanQualificationTime(true,102,140)),"pair cannot bypass protection based on a later per-row time");
    auto unknown=previous;unknown.uncertain=true;frozen.complete(unknown,102,10,true);
    require(!frozen.eligible(7,1000) && !frozen.pairedEligible(7,1000),"unknown still halts all scan and pair admission");
    // Frozen scheduling is not a cached write authorization: production execute
    // still probes current background/member evidence and final real-time T.
    auto r=request();r.knownStrength=true;r.knownEvent=knownEvent();r.hardDeadline=130;
    r.qualification="owned_os_snapshot";r.groupPath="100/io.memscene.scene4.workload.a1_101";
    r.members={{1001,901}};r.plannedRatio=60;r.reservedBytes=1024*MiB;
    Fixture f;f.e.members=r.members;f.e.rssAnonBytes=2048*MiB;f.e.denominatorBytes=1536*MiB;
    f.e.systemGap=f.e.swapRoom=2048*MiB;f.e.faultCounters={{1001,{50,1}}};
    f.e.background=false;require(!daily::execute(r,f.hooks()).writeAttempted.value_or(false) && f.writes==0,"frozen scan never bypasses fresh foreground guard");
    f.e.background=true;f.e.members={{1001,902}};
    require(!daily::execute(r,f.hooks()).writeAttempted.value_or(false) && f.writes==0,"frozen scan never bypasses fresh start_ticks change");
    f.e.members=r.members;auto h=f.hooks();h.finalGap=[&]()->std::optional<Bytes>{f.now=130;return 1024*MiB;};
    require(!daily::execute(r,h).writeAttempted.value_or(false) && f.writes==0,"final actual time T blocks write despite earlier eligible scan");
}
void submissionLimit() {
    for(const std::uint64_t maximum:{0ULL,1ULL}) {
        daily::Executor worker(maximum);unsigned reserved=0,performed=0;
        const auto job=[&](const std::atomic<bool>&) {daily::Result r;++performed;r.status="write_accepted_feedback_verified";
            r.feedbackVerified=true;return r;};
        require(worker.submit(request(1),job,[&]{++reserved;}),"first limited admission");
        (void)await(worker);
        const bool second=worker.submit(request(2),job,[&]{++reserved;});
        if(!maximum) {require(second,"default zero limit remains unlimited");(void)await(worker);}
        else require(!second && worker.busy(),"device worker hard limit survives completion, no host pause needed");
        require(reserved==(maximum?1U:2U) && performed==reserved,"rejected second submission never charges or writes");
    }
}
void dailyFairnessAndSpacing() {
    daily::Schedule schedule(13);
    std::vector<unsigned> visits(13,0);
    std::vector<std::size_t> configMapping;
    for(std::size_t i=0;i<13;++i) configMapping.push_back(100+i);
    double now=100;
    for(unsigned turn=0;turn<26;++turn) {
        const auto fixedStart=schedule.scanStart();
        bool found=false;
        for(std::size_t position=0;position<13;++position) {
            const auto index=daily::scanIndex(position,13,fixedStart,true);
            require(configMapping[index]==100+index,"rotation must retain original appIndex telemetry binding");
            if(!schedule.eligible(index,now)) continue;
            ++visits[index];schedule.submitted(index);
            daily::Result r;r.request.appIndex=index;r.status="write_accepted_feedback_verified";r.feedbackVerified=true;
            schedule.complete(r,now,10);found=true;break;
        }
        require(found,"eligible tail must remain reachable while front cools");
        now+=1;
    }
    for(const auto count:visits) require(count==2,"all thirteen donors must receive two turns without prefix starvation");
    for(std::size_t position=0;position<13;++position)
        require(daily::scanIndex(position,13,7,false)==position,"legacy/oracle scan mapping must stay in config order");
    daily::Schedule cooling(13);cooling.defer(0,100,60);
    require(!cooling.eligible(0,101) && cooling.eligible(12,101),"one app failure/fault backoff must not block unrelated tail");
    daily::Result failed;failed.request.appIndex=1;failed.status="write_failed_charge_retained";
    cooling.complete(failed,101,10);
    require(!cooling.eligible(1,102) && cooling.eligible(12,102),"known failed write keeps per-app sixty-second backoff");
    daily::Result unknown;unknown.request.appIndex=12;unknown.uncertain=true;
    cooling.complete(unknown,103,10);
    require(!cooling.eligible(0,1000) && !cooling.eligible(5,1000),"unknown result must globally halt every daily app");

    // Actual single worker deliberately finishes before a delayed poll. Use fake
    // monotonic times, so the test checks the bound without slow wall-clock sleeps.
    daily::Executor worker;daily::Schedule spaced(2);
    std::mutex mutex;std::condition_variable ready;bool entered=false,release=false;
    std::vector<double> writes;
    auto first=request();first.appIndex=0;
    const auto job=[&](const std::atomic<bool>&) {
        {std::unique_lock<std::mutex> lock(mutex);entered=true;ready.notify_one();ready.wait(lock,[&]{return release;});}
        writes.push_back(100.25);daily::Result r;r.request=first;r.returnedAt=100.25;
        r.status="write_accepted_feedback_verified";r.feedbackVerified=true;return r;
    };
    require(worker.submit(first,job,[]{}),"first fake write admitted");spaced.submitted(0);
    {std::unique_lock<std::mutex> lock(mutex);ready.wait(lock,[&]{return entered;});}
    require(worker.busy() && !worker.poll(),"delayed worker must retain single pending slot");
    {std::lock_guard<std::mutex> lock(mutex);release=true;ready.notify_one();}
    const auto completed=await(worker);spaced.complete(completed,120,10);
    require(!spaced.eligible(1,120) && !spaced.eligible(1,120.999) && spaced.eligible(1,121),
        "global minimum starts at completion poll, not old write return");
    auto second=request(2);second.appIndex=1;
    require(worker.submit(second,[&](const std::atomic<bool>&) {writes.push_back(121);daily::Result r;r.request=second;return r;},[]{}),
        "next fake write only after scheduler spacing");
    (void)await(worker);
    require(writes.size()==2 && writes[1]-writes[0]>=1,"delayed worker writes must stay at least one second apart");
}
void ownedNoWriteRetryClassification() {
    Fixture f;auto r=request();r.qualification="owned_os_snapshot";
    f.e.blocked="owned_device_snapshot:aa_background_unknown_or_foreground";
    f.e.qualificationDiagnostic="aa_matches=1;aa_state=FOREGROUND;aa_reason=foreground";
    const auto rejected=daily::execute(r,f.hooks());
    require(f.writes==0 && rejected.writeAttempted==false && daily::ownedQualificationNoWrite(rejected) &&
        daily::backoffSeconds(rejected,10)==10,"known no-write AA refusal may recheck fresh qualification after ten seconds");
    require(rejected.qualificationDiagnostic==f.e.qualificationDiagnostic &&
        rejected.status=="rejected:owned_device_snapshot:aa_background_unknown_or_foreground",
        "target-only AA diagnostics propagate without changing status or retry classification");
    auto disk=rejected;disk.request.backend="force_swapout";disk.status="disk_rejected:background_or_control_changed";
    require(daily::ownedQualificationNoWrite(disk) && daily::backoffSeconds(disk,10)==10,
        "known no-write disk background/control refusal may recheck without cancelling guards");
    for(unsigned kind=0;kind<10;++kind) {
        auto denied=disk;
        if(kind==0) denied.request.qualification="office_score_adj";
        if(kind==1) denied.writeAttempted.reset();
        if(kind==2) denied.writeAttempted=true;
        if(kind==3) denied.writeLength=-1;
        if(kind==4) denied.uncertain=true;
        if(kind==5) denied.status="disk_rejected:member_or_group_identity_changed";
        if(kind==6) denied.status="disk_rejected:request_or_forecast_expired";
        if(kind==7) denied.status="disk_rejected:unclassified_exception";
        if(kind==8) denied.request.backend="force_shrink_anon";
        if(kind==9) {denied.writeAttempted=true;denied.writeLength=0;denied.status="disk_partial_write_unknown_charge_retained";}
        require(!daily::ownedQualificationNoWrite(denied) && daily::backoffSeconds(denied,10)==60,
            "office/unknown/attempted/error/partial/identity/forecast refusals retain conservative sixty-second backoff");
    }
    // A recheck is a new charged action and a new proof, never a replay/refund.
    daily::Executor worker;Bytes charged=0;unsigned probes=0;
    std::vector<std::string> actionIds;
    Fixture fresh;
    for(unsigned attempt=1;attempt<=3;++attempt) {
        auto next=request(attempt);next.qualification="owned_os_snapshot";
        if(attempt==1) fresh.e.blocked="owned_device_snapshot:aa_background_unknown_or_foreground";
        else {fresh.e.blocked.clear();fresh.e.members=attempt==2?
            std::map<std::uint64_t,std::uint64_t>{{7,92}}:next.members;}
        auto hooks=fresh.hooks();const auto originalProbe=hooks.probe;
        hooks.probe=[&]{++probes;return originalProbe();};
        require(worker.submit(next,[&,next,hooks](const std::atomic<bool>&) {
            return daily::execute(next,hooks);
        },[&]{charged+=next.reservedBytes;actionIds.push_back(next.actionId);}),
            "new recheck requires a distinct charged action");
        const auto completed=await(worker);
        require(charged==attempt*next.reservedBytes,"qualification refusal never refunds prior charge");
        if(attempt==1) require(daily::ownedQualificationNoWrite(completed),"first fresh AA refusal is classified");
        if(attempt==2) require(completed.status=="rejected:member_or_group_identity_changed" && fresh.writes==0,
            "new recheck must still reject changed identity before writing");
        if(attempt==3) require(completed.feedbackVerified && fresh.writes==1,
            "only a newly verified proof can permit the later single write");
    }
    require(probes==3 && actionIds[0]!=actionIds[1] && actionIds[1]!=actionIds[2],
        "each attempt must probe again with a unique action id");
    daily::Schedule schedule(2);schedule.complete(rejected,100,10);
    require(!schedule.eligible(0,109.999) && schedule.eligible(0,110) && !schedule.eligible(1,100.999) && schedule.eligible(1,101),
        "known no-write recheck retains per-app ten seconds and global completion one second");
}
void ordinaryUnknownFeedbackHalts() {
    for(unsigned kind=0;kind<2;++kind) {
        Fixture f;f.feedback=false;auto hooks=f.hooks();
        if(kind==1) hooks.feedback=[]()->daily::Feedback {throw std::runtime_error("fake feedback unavailable");};
        daily::Executor worker;const auto r=request();
        require(r.qualification=="office_score_adj","fixture exercises ordinary office, not owned override");
        require(worker.submit(r,[&](const std::atomic<bool>&){return daily::execute(r,hooks);},[]{}),"ordinary fake admission");
        const auto completed=await(worker);
        unsigned extraReservations=0;
        require(completed.uncertain && f.writes==1 && worker.busy() &&
            !worker.submit(request(2),[&](const std::atomic<bool>&){return daily::execute(request(2),hooks);},[&]{++extraReservations;}) &&
            !extraReservations,"accepted ordinary write with unknown/throwing feedback must globally halt without replay");
    }
}
void blockedWorker(bool timeout) {
    daily::Executor worker;
    std::mutex mutex; std::condition_variable ready;
    bool entered=false,release=false;
    std::atomic<unsigned> writes{0},reserved{0};
    RollingLimit global,app;
    auto r=request();
    const auto job=[&](const std::atomic<bool>&) {
        daily::Result result;result.request=r;
        { std::unique_lock<std::mutex> lock(mutex); entered=true; ready.notify_one();
          ready.wait(lock,[&]{return release;}); }
        ++writes;result.writeAttempted=true;result.writeLength=1;result.error=0;
        result.status="write_failed_charge_retained";return result;
    };
    const bool submitted=worker.submit(r,job,[&]{global.charge(100,r.reservedBytes);app.charge(100,r.reservedBytes);++reserved;});
    { std::unique_lock<std::mutex> lock(mutex); ready.wait(lock,[&]{return entered;}); }
    unsigned samplingTicks=0;
    for(unsigned n=0;n<1000;++n) { if(!worker.poll()) ++samplingTicks; }
    const bool second=worker.submit(request(2),job,[&]{++reserved;});
    std::optional<daily::Result> unknown;
    if(timeout) unknown=worker.timeout(161,60);
    const bool stillBusy=worker.busy();
    { std::lock_guard<std::mutex> lock(mutex); release=true; ready.notify_one(); }
    const auto result=await(worker);
    const bool duplicate=worker.submit(r,job,[&]{++reserved;});
    require(submitted && !second && !duplicate && samplingTicks==1000 && stillBusy,"pending must not block sampling or allow another action");
    require(reserved==1 && writes==1 && global.used(100)==r.reservedBytes && app.used(100)==r.reservedBytes,
        "reserve once before submission, failed write keeps shared/app charge and never retries");
    if(timeout) {
        require(unknown && !unknown->writeAttempted && unknown->uncertain,"timeout must report unknown attempt state");
        require(result.status.rfind("completed_after_timeout:",0)==0 && result.uncertain && worker.busy(),
            "late completion must preserve timeout halt; no detach or second worker");
    }
    worker.join();
}
}
void recentReserveTests() {
    using namespace daily::recent;
    Options o;o.dailyAsync=true;o.dailyRecentReserve=true;
    require(!Options{}.dailyRecentReserve,"recent global pressure proxy defaults off");
    const std::string base="[daemon]\ndaily_async=true\ndaily_recent_reserve=true\n[app fake]\npath=apps/fake\n";
    const auto configured=parseConfig(base).options;
    require(configured.dailyRecentWindowSec==30 && configured.dailyRecentHoldSec==60 &&
        configured.dailyRecentCapFraction==0.5,"registered thirty/sixty/half defaults");
    for(const std::string bad:{"daily_recent_window_sec=0","daily_recent_hold_sec=121","daily_recent_cap_fraction=0.51"}) {
        auto text=base;text.insert(text.find("[app"),bad+"\n");bool refused=false;
        try {(void)parseConfig(text);}catch(const std::exception&){refused=true;}
        require(refused,"invalid recent window/hold/cap must be rejected");
    }
    auto synchronous=base;synchronous.replace(synchronous.find("daily_async=true"),16,"daily_async=false");
    bool refused=false;try {(void)parseConfig(synchronous);}catch(const std::exception&){refused=true;}
    require(refused,"recent reserve cannot alter legacy synchronous policy");
    System sample;sample.time=100;sample.total=32768*MiB;sample.available=24576*MiB;sample.free=20480*MiB;
    Store store;const auto initial=store.update(o,sample);
    require(!initial.signal && targetAt(o,initial,100,sample.total)==o.readyFree,"first sample only establishes baseline");
    sample.time=101;sample.available=8192*MiB;sample.free=4096*MiB;
    const auto dropped=store.update(o,sample);
    require(dropped.signal==16384*MiB && dropped.target==sample.total/2 && dropped.expiresAt==161,
        "observed decline immediately creates bounded reserve; H is hold, never maturation delay");
    require(dropped.epoch==initial.epoch,"new samples do not invalidate in-flight generation");
    sample.time=108.6;sample.available=24576*MiB;sample.free=20480*MiB;
    const auto recovered=store.update(o,sample);
    require(recovered.signal==dropped.signal && recovered.expiresAt==161 && recovered.epoch==dropped.epoch,
        "7.6s real-endpoint gap retains held witness without renewing it when new D is zero");
    for(unsigned i=0;i<4;++i) (void)store.read();
    require(store.read().sampledAt==108.6 && store.read().expiresAt==161 &&
        targetAt(o,store.read(),113.6,sample.total)==o.readyFree,"reading old store never refreshes five-second input age");
    sample.time=139;const auto disconnected=store.update(o,sample);
    require(disconnected.epoch!=recovered.epoch && !disconnected.signal,"gap greater than W resets, not invented growth");
    sample.time=140;sample.available=1024*MiB;const auto newGeneration=store.update(o,sample);
    require(workerTarget(o,dropped,newGeneration,140,sample.total)==o.readyFree,"reset epoch cannot authorize an old request");
    sample.time=139;const auto backwards=store.update(o,sample);
    require(!backwards.signal && backwards.epoch!=newGeneration.epoch,"backward time clears old pressure");
    sample.time=140;sample.total=16384*MiB;sample.available=1024*MiB;
    const auto resized=store.update(o,sample);
    require(!resized.signal && resized.epoch!=backwards.epoch,"total changes establish a new baseline");
    sample.time=std::numeric_limits<double>::quiet_NaN();store.update(o,sample);
    require(!store.read().valid,"nonfinite time invalidates shared authority");
    store.invalidate();require(!store.read().valid,"read failure explicitly invalidates shared authority");

    Store duration;System steady;steady.time=100;steady.total=32768*MiB;steady.available=24576*MiB;steady.free=4096*MiB;
    duration.update(o,steady);
    for(unsigned t=101;t<=191;++t) {
        steady.time=t;steady.available=8192*MiB;const auto value=duration.update(o,steady);
        if(t==130) require(value.expiresAt==190,"last actual D within W has its own H expiry");
        if(t==160) require(value.expiresAt==190,"held G cannot renew expiry from a later low stable sample");
        if(t==190) require(value.signal && targetAt(o,value,190,steady.total)>o.readyFree,"hold includes its exact sixty-second boundary");
        if(t==191) require(!value.signal && targetAt(o,value,191,steady.total)==o.readyFree,"just beyond H expires without permanent pressure target");
    }
    Snapshot frozen{4096*MiB,3840*MiB,32768*MiB,100,160,7,true};
    Snapshot higher{8192*MiB,7936*MiB,32768*MiB,101,161,7,true};
    require(workerTarget(o,frozen,higher,101,32768*MiB)==frozen.target,"worker cannot upgrade frozen request target");
    auto lower=higher;lower.target=2048*MiB;lower.signal=1792*MiB;
    require(workerTarget(o,frozen,lower,101,32768*MiB)==lower.target,"fresh lower target reduces reservation authority");
    auto expired=frozen;expired.expiresAt=101;
    require(workerTarget(o,expired,higher,101.001,32768*MiB)==o.readyFree,"witness expiry applies even with fresh shared samples");
    o.dailyRecentReserve=false;
    require(workerTarget(o,frozen,higher,101,32768*MiB)==o.readyFree,"off ignores every dynamic target");
    for(const Bytes free:{Bytes{0},256*MiB,4096*MiB}) {
        System sys;sys.time=101;sys.total=32768*MiB;sys.available=4096*MiB;sys.free=free;
        const auto gap=daily::refreshedGap(o,sys,Forecast{4096*MiB,0},100,0,workerTarget(o,frozen,higher,101,sys.total));
        require(gap && *gap==deficit(o,sys,Forecast{4096*MiB,0},0),"off preserves same-input gap decisions");
    }
    for(unsigned mode=0;mode<3;++mode) {
        Fixture f;auto hooks=f.hooks();auto r=request();r.qualification=mode==2?"owned_os_snapshot":"office_score_adj";
        hooks.finalGap=[&]()->std::optional<Bytes>{return mode==1?std::optional<Bytes>{}:Bytes{0};};
        const auto out=daily::execute(r,hooks);
        require(!out.writeAttempted.value_or(false) && f.writes==0,"final target recovery/expiry unknown refuses before anonymous write");
    }
    Fixture small;auto hooks=small.hooks();hooks.finalGap=[]()->std::optional<Bytes>{return 32*MiB;};
    const auto shrunk=daily::execute(request(),hooks);
    require(small.writes==1 && shrunk.nominalBytes<=32*MiB && shrunk.nominalBytes<=request().reservedBytes,
        "final reduced target only shrinks one precharged anonymous request");
    Fixture slow;auto late=slow.hooks();late.finalGap=[&]()->std::optional<Bytes>{slow.now=103;return 64*MiB;};
    require(!daily::execute(request(),late).writeAttempted.value_or(false) && slow.writes==0,
        "slow final target check still respects original request expiry");
}

void knownFutureFreeGapTests() {
    Options o; System sys; sys.total=32768*MiB;sys.free=14000*MiB;sys.available=16000*MiB;
    Forecast f{100*MiB,256*static_cast<double>(MiB)};const Bytes net=3*(8192-1536)*MiB;
    require(!o.oracleKnownFutureFreeOnly && oracleEventGap(o,sys,f,net)==deficit(o,sys,f,net),
        "default oracle input decisions must preserve the old deficit formula");
    o.oracleKnownFutureFreeOnly=true;o.reserve=1024*MiB;
    require(oracleEventFreeTarget(o,net)==20992*MiB && oracleEventFreeTarget(o,net)>sys.total/2,
        "known target uses actual net increment plus reserve without half-total clipping");
    require(oracleEventGap(o,sys,f,net)==6992*MiB,"known gap uses physical Free only");
    f.predictedAvailable=32768*MiB;f.declineBytesPS=0;o.readyFree=256*MiB;
    require(oracleEventGap(o,sys,f,net)==6992*MiB,"trend and fixed ready watermark must not alter exact-event gap");
    sys.free=20000*MiB;require(oracleEventGap(o,sys,f,net)==992*MiB,"fresh Free recovery shrinks the action allowance");
    sys.free=22000*MiB;require(oracleEventGap(o,sys,f,net)==0,"target reached must prevent further reservation/write");
    bool refused=false;try {(void)oracleEventFreeTarget(o,std::numeric_limits<Bytes>::max());}catch(const std::exception&){refused=true;}
    require(refused,"known increment addition overflow must reject, not wrap or silently clip");
    sys.total=16000*MiB;sys.free=10000*MiB;refused=false;
    try {(void)oracleEventGap(o,sys,f,net);}catch(const std::exception&){refused=true;}
    require(refused,"impossible target above MemTotal must fail closed");
    sys.total=32768*MiB;sys.free=sys.total+1;refused=false;
    try {(void)oracleEventGap(o,sys,f,net);}catch(const std::exception&){refused=true;}
    require(refused,"Free above MemTotal must be invalid rather than silently suppress an event");
    refused=false;try {(void)oracleEventFreeTarget(o,0);}catch(const std::exception&){refused=true;}
    require(refused,"zero known demand must reject instead of creating a reserve-only event");
    refused=false;try {(void)parseConfig("[daemon]\noracle_known_future_free_only=true\n");}catch(const std::exception& e){refused=std::string(e.what()).find("oracle_known_future_free_only")!=std::string::npos;}
    require(refused,"exact-event opt-in must never silently enter ordinary daily mode");
}
int main() {
    // Bounded only within this fake fixture; no dependency on a device timeout tool.
    std::signal(SIGALRM,SIG_DFL); ::alarm(30);
    try {
        recentReserveTests();
        knownFutureFreeGapTests();
        require(!Options{}.dailyAsync && !Options{}.dailyMaxSubmissions,"daily options must default off/unlimited");
        dailyKnownFutureTests();
        knownStrengthAndPairTests();knownCapacityTests();knownReturnSpacingTests();knownScanAdmissionTests();
        submissionLimit();
        dailyFairnessAndSpacing();
        ordinaryUnknownFeedbackHalts();
        ownedNoWriteRetryClassification();
        require(daily::keepFloor(256*MiB,false)==512*MiB && daily::keepFloor(700*MiB,false)==700*MiB &&
            daily::keepFloor(16*MiB,true)==16*MiB,"new real-app daily path must retain at least 512 MiB without changing isolated probe floor");
        const auto c=parseConfig("[daemon]\ndaily_async=true\n[app fake]\npath=apps/fake\nreclaim=true\nmin_keep_mb=512\n");
        require(c.options.dailyAsync && !c.options.oracleMode,"daily opt-in must remain non-oracle");
        const std::string legalOracle="[daemon]\ndaily_async=false\noracle_mode=true\n"
            "oracle_schedule_file=/fake/schedule\noracle_ledger_file=/fake/ledger\n[app fake]\npath=apps/fake\nmin_keep_mb=512\n";
        require(parseConfig(legalOracle).options.oracleMode,"negative mixed-mode test must start with a valid oracle config");
        auto mixedOracle=legalOracle;
        mixedOracle.replace(mixedOracle.find("daily_async=false"),std::string("daily_async=false").size(),"daily_async=true");
        bool mixed=false;
        try { (void)parseConfig(mixedOracle); }
        catch(const std::exception& e) { mixed=std::string(e.what()).find("daily_async is non-oracle")!=std::string::npos; }
        require(mixed,"daily/oracle mixing must fail specifically at its isolation guard");
        for(unsigned kind=0;kind<8;++kind) {
            Fixture f;
            if(kind==0) f.e.members={{7,92}};
            if(kind==1) ++f.e.groupInode;
            if(kind==2) f.e.background=false;
            if(kind==3) f.e.controlActive=false;
            if(kind==4) f.e.rssAnonBytes=512*MiB;
            if(kind==5) f.e.swapRoom=0;
            if(kind==6) f.e.blocked="anonymous_reclaim_disabled";
            if(kind==7) f.e.blocked="psi_unavailable";
            const auto result=daily::execute(request(),f.hooks());
            require(f.writes==0 && result.writeAttempted==false,"changed identity/protection/keep/swap/PSI must refuse before write");
        }
        {
            Fixture f;auto r=request();r.plannedRatio=3;
            const auto result=daily::execute(r,f.hooks());
            require(f.writes==1 && result.submittedRatio==3 && result.nominalBytes<=r.reservedBytes &&
                result.feedbackVerified && result.writeMs && *result.writeMs>0,"fresh ratio may only shrink and receipt must retain timing/feedback");
        }
        {
            Fixture f;f.returned=0;
            const auto result=daily::execute(request(),f.hooks());
            require(f.writes==1 && result.uncertain && result.writeLength==0 && result.status=="write_partial_unknown_charge_retained",
                "partial write must be unknown, charged, one invocation");
        }
        {
            Fixture f;f.returned=-1;
            const auto result=daily::execute(request(),f.hooks());
            require(f.writes==1 && result.error==5 && result.writeLength==-1 && daily::backoffSeconds(result,10)==60,
                "write errno retained; failure backs off without replay");
        }
        {
            Fixture f;f.feedback=false;
            const auto result=daily::execute(request(),f.hooks());
            require(f.writes==1 && !result.feedbackVerified && result.uncertain && daily::backoffSeconds(result,10)==60,
                "missing feedback must not become success or trigger another write");
        }
        {
            Fixture f;auto h=f.hooks();h.probe=[&]{f.now=103;return f.e;};
            require(daily::execute(request(),h).status=="rejected:request_stale" && f.writes==0,"slow fresh proof must expire before write");
        }
        {
            Options options; System fresh; fresh.total=8192*MiB; fresh.available=2048*MiB;
            fresh.free=512*MiB; fresh.time=101; Forecast sampled{100*MiB,0};
            const auto recovered=daily::refreshedGap(options,fresh,sampled,100,0);
            Fixture f; f.e.systemGap=recovered.value_or(0);
            require(recovered && *recovered==0 && daily::execute(request(),f.hooks()).writeAttempted==false && f.writes==0,
                "available recovery must remove the old forecast deficit and prevent write");
            fresh.available=1000*MiB;
            const auto smaller=daily::refreshedGap(options,fresh,sampled,100,0);
            Fixture small;small.e.systemGap=smaller.value_or(0);
            const auto shrunk=daily::execute(request(),small.hooks());
            require(smaller && *smaller==24*MiB && shrunk.nominalBytes<=24*MiB && small.writes==1,
                "partly recovered available must only reduce the reserved submission");
            fresh.time=106;
            require(!daily::refreshedGap(options,fresh,sampled,100,0),"expired prediction must be unknown");
            Fixture expired;expired.now=101;auto stale=request();stale.forecastSampledAt=95;
            require(daily::execute(stale,expired.hooks()).status=="rejected:forecast_expired" && expired.writes==0,
                "expired forecast must refuse even a newly reserved task");
            options.horizonSec=0;fresh.time=100.2;fresh.available=1000*MiB;
            const auto immediate=daily::refreshedGap(options,fresh,Forecast{0,500*static_cast<double>(MiB)},100,0);
            require(immediate && *immediate==24*MiB,"zero horizon must use fresh immediate deficit without extrapolation");
            fresh.available=2048*MiB;
            const auto immediateRecovery=daily::refreshedGap(options,fresh,sampled,100,0);
            require(immediateRecovery && *immediateRecovery==0,"zero horizon recovery must produce zero deficit");
            fresh.time=101;
            require(!daily::refreshedGap(options,fresh,sampled,100,0),"zero horizon evidence still expires at the one-second bound");
        }
        {
            Options options; options.requirePSI=false; System raw; raw.mem.valid=true;raw.mem.full10=18.18;
            PSI app;app.valid=true;app.some10=18.18;
            require(pressureBlock(options,raw,0,0)=="memory_full_pressure",
                "Linux memory pressure protection must remain unchanged");
            require(daily::harmonyUnverifiedPsiBlock(options,raw,app).empty(),
                "Harmony unverified 18.18 fields must be diagnostic, not Linux PSI veto");
            Fixture f;auto hooks=f.hooks();hooks.probe=[&]{auto e=f.e;
                e.blocked=daily::harmonyUnverifiedPsiBlock(options,raw,app);return e;};
            require(daily::execute(request(),hooks).feedbackVerified && f.writes==1,
                "Harmony diagnostic PSI must not independently block a protected fake action");
            raw.io.valid=true;raw.io.some10=18.18;
            require(pressureBlock(options,raw,0,0)=="io_pressure",
                "Linux IO pressure protection must remain unchanged");
            options.requirePSI=true;Fixture required;auto denied=required.hooks();
            denied.probe=[&]{auto e=required.e;e.blocked=daily::harmonyUnverifiedPsiBlock(options,raw,app);return e;};
            require(daily::execute(request(),denied).status=="rejected:harmony_psi_unverified_required" && required.writes==0,
                "Harmony required PSI must reject rather than treat raw telemetry as verified");
        }
        blockedWorker(false);blockedWorker(true);
        for(unsigned i=0;i<10;++i) {daily::Executor worker;worker.join();}
        std::cout<<"FAKE_ONLY_PASS nonblocking_poll single_worker identity control keep ratio_shrink reservation failure partial feedback timeout_late_completion join defaults\n";
        return 0;
    } catch(const std::exception& e) { std::cerr<<e.what()<<'\n';return 1; }
}
