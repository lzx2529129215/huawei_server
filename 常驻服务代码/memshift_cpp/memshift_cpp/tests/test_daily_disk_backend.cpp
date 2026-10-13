// Fake-only. Never call sampleDeviceDailyEvidence or any real writer/collector.
#include "../src/daily_disk_backend.hpp"
#include "../src/device_daily_evidence.hpp"
#include "../src/device_proc_reader.hpp"
#include <iostream>
#include <csignal>
#include <unistd.h>
using namespace ms;using namespace ms::harmony_v1;
namespace {
void check(bool ok,const char* why) {if(!ok) throw std::runtime_error(why);}
const std::string leaf="100/io.memscene.scene4.workload.a1_23";
struct ParserFixture {
    std::string before=device_proc::fakeStat(17,91),after=before;
    std::string status="Pid: 17\nUid: 23 23 23 23\n";
    std::string cmd="io.memscene.scene4.workload.a1",cg="1:memory:/"+leaf+"\n",members="17\n";
    std::string aa="AppRunningRecord ID #0\nprocess name [io.memscene.scene4.workload.a1]\npid #17\nuid #23\nstate #BACKGROUND\n";
    std::string table="WindowName DisplayId Pid WinId\nowned 0 17 9\nother 0 18 10\nFocus window: 10\nTotal window num: 2\n";
    std::map<std::uint64_t,std::string> details={{9,"Pid: 17\nWinId: 9\nIsVisible: false\nFocusable: true\n"}};
    double oldest=100,now=101;
    DailyDeviceEvidence run() {return parseDeviceDailyEvidence(leaf,17,91,before,status,cmd,cg,members,after,aa,table,details,oldest,now);}
};
daily::Request request() {
    daily::Request r;r.actionId="fake_generation_daily_1";r.sequence=1;r.groupPath=leaf;r.app="owned";
    r.backend="force_swapout";r.qualification="owned_os_snapshot";r.members={{17,91}};r.groupDevice=1;r.groupInode=2;
    r.reservedBytes=1344*MiB;r.keepBytes=512*MiB;r.minBatch=MiB;r.reservedAt=100;
    r.forecastSampledAt=100;r.forecastMaxAgeSeconds=5;r.maxAgeSeconds=2.5;return r;
}
struct Fixture {
    daily::disk::Evidence e;unsigned writes=0,probes=0;long length=1;double now=100.5;
    bool effect=true,throwFeedback=false,excess=false;
    Fixture() {e.base.groupDevice=1;e.base.groupInode=2;e.base.members={{17,91}};
        e.base.background=true;e.base.controlActive=true;e.base.rssAnonBytes=1024*MiB;
        e.base.systemGap=2048*MiB;e.base.swapRoom=2048*MiB;e.zramBytes=256*MiB;
        e.poolKnown=true;e.deviceSnapshotVerified=true;e.backendEnabled=true;e.oldestProof=100;}
    daily::disk::Hooks hooks() {
        daily::disk::Hooks h;h.now=[&]{return now;};h.cancelled=[] {return false;};h.feedbackDelay=[]{};
        h.probe=[&] {++probes;if(probes>1 && throwFeedback) throw std::runtime_error("fake feedback unavailable");
            auto sample=e;if(probes>1 && effect) {sample.zramBytes=0;sample.eswapBytes+=excess?2048*MiB:256*MiB;}return sample;};
        h.write=[&](const std::string& command) {check(command=="1","disk payload must be command, not bytes");++writes;
            return daily::WriteReceipt{length,length<0?5:0};};return h;
    }
    daily::Result run() {return daily::disk::execute(request(),2048*MiB,hooks());}
};

OracleRequest capacityEvent() {
    OracleRequest e;e.id="s4_capacity_c0000_peak_transition";e.caseId="s4_capacity_c0000";e.schemaVersion=2;
    e.launch=130;e.needed=19968*MiB;e.planFingerprint=std::string(64,'a');e.proofSequence=1;
    for(unsigned i=1;i<=13;++i) {
        OracleDonor d;d.bundle="io.memscene.scene4.workload.a"+std::to_string(i);
        d.uid=std::to_string(i==1?23:100+i);d.pid=i==1?17:1000+i;d.start=i==1?91:900+i;
        d.procStart=d.start;d.aaPid=d.pid;d.aaUid=uintValue(d.uid);
        d.dailyReady=d.procVerified=d.windowReady=d.lifecycleClear=true;d.readyAt=90;d.aaState="BACKGROUND";
        d.aaObserved=d.windowObserved=d.procObserved=d.lifecycleObserved=100;e.donors.push_back(d);
    }return e;
}
void capacityPlanAndWorkerTests() {
    auto r=request();r.knownStrength=r.knownCapacity=true;r.knownEvent=capacityEvent();r.hardDeadline=130;
    r.keepBytes=128*MiB;r.minBatch=64*MiB;r.reservedBytes=400*MiB;
    const auto plan=[&](const daily::Request* authority,Bytes cap=2048*MiB) {
        return daily::disk::plan(leaf,128*MiB,192*MiB,128*MiB,64*MiB,cap,cap,cap,cap,true,authority);
    };
    check(!plan(nullptr).admitted,"disk retains512 floor without request authority");
    check(plan(&r).admitted && plan(&r).reservation==400*MiB,"authorized low floor precharges whole320+64+16");
    check(!plan(&r,399*MiB).admitted,"low floor never bypasses explicit/shared/gap/swap cap");
    for(unsigned kind=0;kind<5;++kind) {
        auto bad=r;if(kind==0)bad.knownEvent.reset();if(kind==1)bad.qualification="office_score_adj";
        if(kind==2)bad.members={{17,92}};if(kind==3)bad.knownStrength=false;if(kind==4)bad.knownCapacity=false;
        check(!plan(&bad).admitted,"flag or floor alone cannot grant low-floor disk plan");
    }
    check(!daily::disk::plan(leaf,128*MiB,127*MiB,128*MiB,64*MiB,2048*MiB,2048*MiB,2048*MiB,2048*MiB,true,&r).admitted,
        "disk rejects fresh RSS below authority floor");
    Fixture f;f.e.base.rssAnonBytes=192*MiB;f.e.zramBytes=128*MiB;
    const auto good=daily::disk::execute(r,2048*MiB,f.hooks());
    check(good.feedbackVerified && !good.uncertain && f.writes==1 && good.nominalBytes==320*MiB &&
        good.status=="disk_command_accepted_transition_observed","actual low-floor production disk executor writes exactly command1 through fake hook");
    // Fresh whole work may consume only the original precharge, never expand it.
    Fixture grown;grown.e.base.rssAnonBytes=209*MiB;grown.e.zramBytes=128*MiB;
    check(!daily::disk::execute(r,2048*MiB,grown.hooks()).writeAttempted.value_or(false) && grown.writes==0,
        "fresh work+64 above immutable400 reservation rejects without second backend");
    Fixture foreground;foreground.e.base.rssAnonBytes=192*MiB;foreground.e.zramBytes=128*MiB;foreground.e.base.background=false;
    check(!daily::disk::execute(r,2048*MiB,foreground.hooks()).writeAttempted.value_or(false) && foreground.writes==0,"foreground cannot use low floor disk authority");
    Fixture identity;identity.e.base.rssAnonBytes=192*MiB;identity.e.zramBytes=128*MiB;identity.e.base.members={{17,92}};
    check(!daily::disk::execute(r,2048*MiB,identity.hooks()).writeAttempted.value_or(false) && identity.writes==0,"PID reuse rejects low floor disk");
    Fixture deadline;deadline.e.base.rssAnonBytes=192*MiB;deadline.e.zramBytes=128*MiB;auto h=deadline.hooks();
    h.finalGap=[&]()->std::optional<Bytes>{deadline.now=130;return 2048*MiB;};
    check(!daily::disk::execute(r,2048*MiB,h).writeAttempted.value_or(false) && deadline.writes==0,"capacity disk finalT guard remains");
    Fixture unknown;unknown.e.base.rssAnonBytes=192*MiB;unknown.e.zramBytes=128*MiB;unknown.effect=false;
    check(daily::disk::execute(r,2048*MiB,unknown.hooks()).uncertain && unknown.writes==1,"capacity accepted effect unknown still globally halts");
}

void parserTests() {
    const std::map<std::uint64_t,std::uint64_t> wpsMembers={{17,91},{18,92}};
    const std::string wpsMemory="lruProcList:\n"
        "17 20020028 cn.wps.office.hap -800 100 5 unknown app Yes No default No No Yes No No No No\n"
        "18 20020028 cn.wps.office.hap 0 100 5 unknown app Yes No default Yes No Yes No No No No\n";
    const std::string wpsWindows="WindowName DisplayId Pid WinId\nwps 0 17 9\nother 0 99 10\n"
        "Focus window: 10\nTotal window num: 2\n";
    const std::map<std::uint64_t,std::string> wpsDetails={{9,"Pid: 17\nWinId: 9\nIsVisible: false\nFocusable: true\n"}};
    const auto wps=[&](const std::string& memory,const std::string& table,
        const std::map<std::uint64_t,std::string>& details,double now=101) {
        return parseWpsWindowEvidence(wpsMembers,17,20020028,memory,table,details,100,now).verified;
    };
    check(wps(wpsMemory,wpsWindows,wpsDetails),"minimized WPS main eligible despite foreground cloud");
    auto changedMemory=wpsMemory;changedMemory.replace(changedMemory.find("default No No Yes"),17,"default Yes No Yes");
    check(!wps(changedMemory,wpsWindows,wpsDetails),"WPS main isFore Yes denied");
    auto changedDetails=wpsDetails;changedDetails[9]="Pid: 17\nWinId: 9\nIsVisible: true\nFocusable: true\n";
    check(!wps(wpsMemory,wpsWindows,changedDetails),"visible WPS window denied");
    check(!wps(wpsMemory,wpsWindows,{},101),"missing WPS window detail denied");
    check(!wps(wpsMemory,wpsWindows,wpsDetails,103),"stale WPS window evidence denied");
    auto changedTable=wpsWindows;changedTable.replace(changedTable.find("Focus window: 10"),16,"Focus window: 9");
    check(!wps(wpsMemory,changedTable,wpsDetails),"focused WPS window denied");
    ParserFixture f;const auto background=f.run();
    check(background.verified && background.aaParsed && background.aaMatchCount==1 &&
        background.aaState=="BACKGROUND" && background.aaReason=="background","valid target AA diagnostics");
    const auto rejectedAa=[&](ParserFixture fixture,unsigned matches,const std::string& state,const std::string& reason) {
        const auto result=fixture.run();
        check(!result.verified && result.reason=="aa_background_unknown_or_foreground" && result.aaParsed &&
            result.aaMatchCount==matches && result.aaState==state && result.aaReason==reason,
            "AA diagnostics must split causes without changing the refusal contract");
    };
    f.aa.clear();rejectedAa(f,0,"UNKNOWN","no_matching_record");
    f={};f.aa+=f.aa;rejectedAa(f,2,"UNKNOWN","multiple_matching_records");
    f={};f.aa.replace(f.aa.find("BACKGROUND"),10,"FOREGROUND");rejectedAa(f,1,"FOREGROUND","foreground");
    f={};f.aa.replace(f.aa.find("BACKGROUND"),10,"STARTING");rejectedAa(f,1,"UNKNOWN","unknown_state");
    f={};f.aa.replace(f.aa.find("uid #23"),7,"uid #24");rejectedAa(f,0,"UNKNOWN","no_matching_record");
    f={};
    f.now=102.5001;check(!f.run().verified,"provider itself rejects TTL expiry");
    f={};f.aa+=f.aa;check(!f.run().verified,"AA match must be unique");
    f={};f.aa.replace(f.aa.find("BACKGROUND"),10,"FOREGROUND");check(!f.run().verified,"foreground denied even in owned scope");
    f={};f.details[9]="Pid: 17\nWinId: 9\nIsVisible: true\nFocusable: true\n";check(!f.run().verified,"visible denied");
    f={};f.table.replace(f.table.find("Focus window: 10"),16,"Focus window: 9");check(!f.run().verified,"focused denied");
    f={};f.members="17 18";check(!f.run().verified,"singleton required");
    f={};f.status="Pid: 17\nUid: 23 23 24 23\n";check(!f.run().verified,"all UID fields must match");
    f={};f.cmd="wrapper.io.memscene.scene4.workload.a1";check(!f.run().verified,"substring cmdline denied");
    f={};f.after=device_proc::fakeStat(17,92);check(!f.run().verified,"PID reuse denied");
    Options legacy;legacy.dailyAsync=true;legacy.dailyDiskMode="owned_apply";
    check(!daily::disk::ownedQualification(legacy,"100/com.huawei.hmos.browser_23",1),"office never gets owned override");
    check(!daily::disk::ownedQualification(legacy,leaf,2),"override requires singleton");
    legacy.dailyDiskMode="observe";check(!daily::disk::ownedQualification(legacy,leaf,1),"legacy observe retains score qualification");
}
void qualificationTests() {
    check(!Options{}.dailyOwnedOsSnapshot,"new qualification defaults off");
    for(const auto* mode:{"off","observe","owned_apply"}) {
        Options old;old.dailyAsync=true;old.dailyDiskMode=mode;
        check(daily::disk::ownedQualification(old,leaf,1)==(old.dailyDiskMode=="owned_apply"),
            "all legacy modes retain qualification behavior");
        check(daily::disk::diskApplyEnabled(old,leaf,1)==(old.dailyDiskMode=="owned_apply"),
            "legacy disk admission remains explicit");
    }
    Options owned;owned.dailyAsync=true;owned.dailyOwnedOsSnapshot=true;
    check(daily::disk::ownedQualification(owned,leaf,1),"explicit qualification permits anonymous-only owned work");
    check(!daily::disk::diskApplyEnabled(owned,leaf,1),"disk off cannot be enabled by qualification");
    for(const auto* path:{"100/com.huawei.hmos.browser_23","100/io.memscene.scene4.workload.a14_23",
                         "100/io.memscene.scene4.workload.a1_23/child","../100/io.memscene.scene4.workload.a1_23"})
        check(!daily::disk::ownedQualification(owned,path,1),"no office, target or near-match override");
    check(!daily::disk::ownedQualification(owned,leaf,2) && !daily::disk::ownedQualification(owned,leaf,0),"exact singleton only");
    check(!daily::disk::ownedQualification(owned,leaf,1,true),"isolated probe does not gain owned qualification");
    auto invalid=owned;invalid.oracleMode=true;check(!daily::disk::ownedQualification(invalid,leaf,1),"oracle excluded");
    invalid=owned;invalid.dailyAsync=false;check(!daily::disk::ownedQualification(invalid,leaf,1),"synchronous path excluded");
    invalid=owned;invalid.oracleProofSource="device";check(!daily::disk::ownedQualification(invalid,leaf,1),"future-plan provider excluded");

    const std::string base="[daemon]\ndaily_async=true\ndaily_owned_os_snapshot=true\ndaily_disk_mode=off\n[app fake]\npath=apps/fake\nmin_keep_mb=512\n";
    check(parseConfig(base).options.dailyOwnedOsSnapshot,"explicit anonymous-only opt-in parses");
    auto noAsync=base;noAsync.replace(noAsync.find("daily_async=true"),16,"daily_async=false");
    bool refused=false;try {(void)parseConfig(noAsync);} catch(const std::exception& e) {
        refused=std::string(e.what()).find("daily_owned_os_snapshot")!=std::string::npos;
    }
    check(refused,"owned opt-in requires non-oracle daily async");
    const std::string legalOracle="[daemon]\ndaily_async=false\noracle_mode=true\n"
        "oracle_schedule_file=/fake/schedule\noracle_ledger_file=/fake/ledger\n[app fake]\npath=apps/fake\nmin_keep_mb=512\n";
    check(parseConfig(legalOracle).options.oracleMode,"oracle negative starts from valid config");
    auto mixed=legalOracle;mixed.insert(mixed.find("[app"),"daily_owned_os_snapshot=true\n");
    refused=false;try {(void)parseConfig(mixed);} catch(const std::exception& e) {
        refused=std::string(e.what()).find("daily_owned_os_snapshot")!=std::string::npos;
    }
    check(refused,"new qualification cannot enter oracle mode");

    ParserFixture parser;auto prepared=request();prepared.backend="force_shrink_anon";
    prepared.reservedBytes=64*MiB;prepared.plannedRatio=5;
    daily::Hooks hooks;unsigned anonWrites=0;std::set<std::string> protectedApps;
    hooks.probe=[&] {
        daily::Evidence e;e.groupDevice=1;e.groupInode=2;e.members=prepared.members;
        e.rssAnonBytes=e.denominatorBytes=1024*MiB;e.systemGap=e.swapRoom=64*MiB;
        const auto proof=parser.run();e.background=proof.verified;
        e.controlActive=!daily::disk::targetProtected(owned,prepared.app,leaf,protectedApps);
        e.authorityExpires=proof.verified?proof.oldest+2.5:0;
        if(!proof.verified)e.blocked="owned_device_snapshot:"+proof.reason;
        return e;
    };
    hooks.now=[] {return 101.0;};hooks.cancelled=[] {return false;};hooks.feedbackDelay=[]{};
    hooks.ratioForBudget=[](Bytes,Bytes){return 5U;};
    hooks.write=[&](const std::string& payload){++anonWrites;return daily::WriteReceipt{static_cast<long>(payload.size()),0};};
    hooks.feedback=[] {return daily::Feedback{true,900*MiB};};
    auto result=daily::execute(prepared,hooks);
    check(anonWrites==1 && result.feedbackVerified,"off-disk qualified anonymous work has a real executor path");
    // Off-no-open is enforced at the production adapter's two explicit gates;
    // this fixture tests the shared predicate, not a real adapter invocation.
    protectedApps.insert("io.memscene.scene4.workload.a1");
    check(daily::disk::targetProtected(owned,"different_alias",leaf,protectedApps),"bundle protects a different app alias");
    check(!daily::disk::ownedQualification(owned,leaf,2) &&
        daily::disk::targetProtected(owned,"different_alias",leaf,protectedApps),"protection survives multiple members");
    check(!daily::disk::ownedQualification(owned,leaf,0) &&
        daily::disk::targetProtected(owned,"different_alias",leaf,protectedApps),"protection survives unknown identity");
    result=daily::execute(prepared,hooks);
    check(anonWrites==1 && result.status=="rejected:background_or_control_changed","production shared bundle gate prevents fake executor write");
    prepared.members={{17,91},{18,92}};
    result=daily::execute(prepared,hooks);
    check(anonWrites==1 && result.status=="rejected:background_or_control_changed","multiple current members do not remove bundle protection");
    prepared.members.clear();result=daily::execute(prepared,hooks);
    check(anonWrites==1 && !result.writeAttempted.value_or(false),"unknown members stay protected and cannot execute");
    prepared.members={{17,91}};
    protectedApps.clear();protectedApps.insert(prepared.app);
    result=daily::execute(prepared,hooks);check(anonWrites==1,"alias protection also prevents write");
    protectedApps.clear();
    parser.aa.replace(parser.aa.find("BACKGROUND"),10,"FOREGROUND");
    result=daily::execute(prepared,hooks);check(anonWrites==1 && !result.writeAttempted.value_or(false),"foreground transition still denies anonymous write");
    parser={};parser.after=device_proc::fakeStat(17,92);
    result=daily::execute(prepared,hooks);check(anonWrites==1,"identity transition still denies anonymous write");
    owned.dailyDiskMode="owned_apply";
    check(daily::disk::diskApplyEnabled(owned,leaf,1),"disk requires a separate explicit mode");
}
void diskTests() {
    check(Options{}.dailyDiskMode=="off","daily disk default off");
    const std::string base="[daemon]\ndaily_async=true\n[app fake]\npath=apps/fake\nmin_keep_mb=512\n";
    check(parseConfig(base).options.dailyAsync,"valid base for config negatives");
    const auto config=[&](const std::string& lines){auto text=base;text.insert(text.find("[app"),lines);return parseConfig(text);};
    bool refused=false;try {(void)config("daily_disk_mode=owned_apply\n");} catch(const std::exception& e) {refused=std::string(e.what()).find("whole-work limit")!=std::string::npos;}
    check(refused,"owned mode needs explicit independent work cap within shared quotas");
    check(config("daily_disk_mode=observe\n").options.dailyDiskMode=="observe","observe without authorization is legal");
    const auto p=daily::disk::plan(leaf,256*MiB,1024*MiB,512*MiB,MiB,2048*MiB,2048*MiB,2048*MiB,2048*MiB);
    check(p.admitted && p.reservation==1360*MiB && p.reservation>16*MiB,"whole work is not batch-clamped");
    check(!daily::disk::plan(leaf,256*MiB,1024*MiB,512*MiB,MiB,2048*MiB,32*MiB,2048*MiB,2048*MiB).admitted,"existing global/app quota still limits disk");
    // Initial slack is charged and capped; final verification never adds it again.
    for(unsigned cap=0;cap<4;++cap) {
        Bytes values[]={2048*MiB,2048*MiB,2048*MiB,2048*MiB};values[cap]=p.reservation-1;
        check(!daily::disk::plan(leaf,256*MiB,1024*MiB,512*MiB,MiB,
            values[0],values[1],values[2],values[3]).admitted,"planning slack obeys every cap");
    }
    {
        auto frozen=request();frozen.reservedBytes=p.reservation;Fixture growth;growth.e.zramBytes+=4096;
        auto result=daily::disk::execute(frozen,2048*MiB,growth.hooks());
        check(growth.writes==1 && result.feedbackVerified &&
            result.nominalBytes+daily::disk::Margin<=frozen.reservedBytes,
            "4KiB growth fits one precharged slack, no second margin/slack charge");
        growth={};growth.e.zramBytes+=daily::disk::PlanningHeadroom+4096;
        result=daily::disk::execute(frozen,2048*MiB,growth.hooks());
        check(growth.writes==0 && result.diskBudgetDiagnostic.find("reserved_cap=")!=std::string::npos &&
            !result.writeAttempted.value_or(false),"growth above frozen slack rejects with fresh numeric evidence");
        growth={};growth.e.base.background=false;
        result=daily::disk::execute(frozen,2048*MiB,growth.hooks());
        check(growth.writes==0,"planning slack never overrides background protection");
        growth={};growth.e.base.groupInode=3;
        result=daily::disk::execute(frozen,2048*MiB,growth.hooks());
        check(growth.writes==0,"planning slack never overrides identity protection");
    }
    Fixture f;auto r=f.run();check(f.writes==1 && r.feedbackVerified && !r.uncertain,"one command with observed transition");
    f={};f.e.zramBytes+=MiB;r=f.run();check(f.writes==0,"fresh pool growth cannot exceed precharged whole reservation");
    f={};f.e.base.systemGap=0;r=f.run();check(f.writes==0,"gap recovery denies disk");
    f={};auto targetHooks=f.hooks();targetHooks.finalGap=[]()->std::optional<Bytes>{return 0;};
    r=daily::disk::execute(request(),2048*MiB,targetHooks);
    check(f.writes==0 && !r.writeAttempted.value_or(false),"final expired/recovered target denies disk after qualified probe");
    f={};targetHooks=f.hooks();targetHooks.finalGap=[]()->std::optional<Bytes>{return {};};
    r=daily::disk::execute(request(),2048*MiB,targetHooks);
    check(f.writes==0,"unknown final forecast does not open a transfer after prior positive gap");
    f={};targetHooks=f.hooks();targetHooks.finalGap=[&]()->std::optional<Bytes>{f.now=103;return 2048*MiB;};
    r=daily::disk::execute(request(),2048*MiB,targetHooks);
    check(f.writes==0,"slow final gap recheck retains request and OS proof TTL");
    f={};f.e.base.groupInode=3;r=f.run();check(f.writes==0,"rebuilt group denied");
    f={};f.e.base.members={{17,92}};r=f.run();check(f.writes==0,"prepared identity changed denied");
    f={};f.e.deviceSnapshotVerified=false;r=f.run();check(f.writes==0,"owned scope alone cannot authorize write");
    f={};f.length=0;r=f.run();check(f.writes==1 && r.uncertain,"partial write unknown no retry");
    f={};f.length=-1;r=f.run();check(f.writes==1 && !r.uncertain,"known failure retained no retry");
    f={};f.effect=false;r=f.run();check(f.writes==1 && r.uncertain,"nonpositive feedback halts admission");
    f={};f.throwFeedback=true;r=f.run();check(f.writes==1 && r.uncertain,"feedback exception after accepted write halts admission");
    f={};f.excess=true;r=f.run();check(f.writes==1 && r.uncertain && r.status=="disk_transfer_exceeded_logical_reservation","excess Eswap delta halts and reports estimate exceeded");
    {
        auto prepared=request();prepared.backend="force_shrink_anon";prepared.reservedBytes=64*MiB;prepared.plannedRatio=5;
        daily::Hooks h;daily::Evidence e;e.groupDevice=1;e.groupInode=2;e.members=prepared.members;
        e.background=true;e.controlActive=true;e.rssAnonBytes=e.denominatorBytes=1024*MiB;e.systemGap=e.swapRoom=64*MiB;
        h.probe=[&]{return e;};h.now=[] {return 100.5;};h.cancelled=[] {return false;};
        h.ratioForBudget=[](Bytes,Bytes){return 5U;};h.feedbackDelay=[]{};unsigned writes=0;
        h.write=[&](const std::string& p){++writes;return daily::WriteReceipt{static_cast<long>(p.size()),0};};
        h.feedback=[] {return daily::Feedback{false,900*MiB};};
        auto unknown=daily::execute(prepared,h);check(writes==1 && unknown.uncertain,"owned preparation unknown cannot advance to disk");
        h.feedback=[]()->daily::Feedback {throw std::runtime_error("fake missing feedback");};
        unknown=daily::execute(prepared,h);check(writes==2 && unknown.uncertain,"owned preparation feedback exception halts");
        e.background=false;unknown=daily::execute(prepared,h);check(writes==2,"owned scope without background proof cannot prepare");
    }
    {
        Fixture deadline;auto r=request();r.hardDeadline=100.5;
        auto denied=daily::disk::execute(r,2048*MiB,deadline.hooks());
        check(deadline.writes==0 && denied.status=="disk_rejected:known_future_deadline_at_write","known T gate blocks actual disk callback");
        // Independent pre-T scenario: the refused probe must not masquerade
        // as the next scenario's post-write feedback in this stateful fake.
        deadline={};r.hardDeadline=101;auto accepted=daily::disk::execute(r,2048*MiB,deadline.hooks());
        check(deadline.writes==1 && accepted.feedbackVerified,"known event preT disk uses existing daily backend");
    }
    // Existing single worker must propagate disk unknown and retain charge.
    daily::Executor worker;f={};f.effect=false;Bytes charged=0;
    check(worker.submit(request(),[&](const std::atomic<bool>&){return f.run();},[&]{charged+=request().reservedBytes;}),"disk submit");
    std::optional<daily::Result> done;for(unsigned i=0;i<2000 && !done;++i) {done=worker.poll();if(!done)std::this_thread::sleep_for(std::chrono::milliseconds(1));}
    check(done && done->uncertain && worker.busy() && charged==request().reservedBytes && f.writes==1,"unknown halts shared worker and retains reservation");
}
}
int main() {std::signal(SIGALRM,SIG_DFL);alarm(30);try {parserTests();capacityPlanAndWorkerTests();qualificationTests();diskTests();std::cout<<"FAKE_DAILY_DISK_PASS\n";return 0;}
    catch(const std::exception& e) {std::cerr<<e.what()<<'\n';return 1;}}
