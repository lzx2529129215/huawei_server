#include "../src/io.hpp"
#include <iostream>
#include <functional>
using namespace ms;
int checks=0;
void check(bool x,const char* msg) { ++checks; if(!x) throw std::runtime_error(msg); }
void rejects(const std::function<void()>& fn,const char* msg) {
    bool failed=false; try { fn(); } catch(const std::exception&) { failed=true; } check(failed,msg);
}
int main() {
    try {
        check(sub(1,2)==0,"saturating subtraction");
        check(add(Unlimited-2,10)==Unlimited,"saturating addition");
        check(delta(2,100)==0,"counter reset");
        check(uintValue("123")==123,"integer parser");
        rejects([]{uintValue("-1");},"negative integer rejected");
        rejects([]{uintValue("12x");},"trailing junk rejected");
        rejects([]{realValue("nan");},"nan rejected");
        rejects([]{mbValue("18446744073709551615");},"MiB overflow rejected");
        check(!safeRelative("../root") && !safeRelative("/root") && !safeRelative("a//b"),"unsafe paths rejected");
        check(safeRelative("user.slice/app-test.scope"),"valid cgroup path");
        check(ancestor("a/b","a/b/c") && !ancestor("a/b","a/bc"),"ancestor boundary");
        auto psi=parsePSI("some avg10=2.30 avg60=1.00 avg300=0.20 total=12345\nfull avg10=0.50 avg60=0.2 avg300=0.1 total=10\n");
        check(psi.valid && psi.some10==2.30 && psi.fullTotal==10,"PSI parser");
        check(!parsePSI("some avg10=0.00 total=0\n").valid,"missing PSI full fails closed");
        check(stallPercent(100000,0,1)==10,"PSI units microseconds to percent");
        RollingLimit rate; rate.charge(0,100); rate.charge(30,150);
        check(rate.room(59,300)==50,"rolling cap active");
        check(rate.room(60,300)==150,"rolling expiry");
        check(rate.room(91,300)==300,"rolling fully expired");
        std::string config="[daemon]\nreserve_mb=1024\n[app app1]\npath=apps/app1\nreclaim=true\n";
        auto c=parseConfig(config);
        check(c.apps.size()==1 && c.apps[0].reclaim && c.options.reserve==1024*MiB,"configuration parse");
        rejects([&]{parseConfig(config+"unknown=1\n");},"unknown key rejected");
        rejects([&]{parseConfig(config+"[app app2]\npath=apps/app1/child\n");},"overlapping cgroups rejected");
        rejects([&]{parseConfig(config+"path=apps/second\n");},"duplicate key rejected");
        rejects([]{parseConfig("[daemon]\nalpha=2\n[app x]\npath=a\n");},"unsafe alpha rejected");
        auto ctl=parseControl("paused=false\nprotected_apps=browser,editor\nreserve_extra_mb=100\nboost_until_unix=999\n");
        check(ctl.protectedApps.size()==2 && ctl.boost==100*MiB,"control parse");
        check(parseControl(controlText(ctl)).protectedApps==ctl.protectedApps,"control roundtrip");
        rejects([]{parseControl("paused=false\n");},"partial control fails closed");
        Options o; App app{"app","apps/app",true,256*MiB};
        System s; s.total=16*1024*MiB; s.available=700*MiB; s.free=400*MiB; s.swapFree=4*1024*MiB;
        s.mem.valid=true; s.io.valid=true;
        Features f; f.protectedNow=false; f.age=60; f.episodeRoom=1024*MiB; f.minuteRoom=1024*MiB;
        f.now.valid=true; f.now.populated=true; f.now.current=4*1024*MiB;
        f.now.inactiveFile=1024*MiB; f.now.inactiveAnon=1024*MiB; f.now.swapRoom=4*1024*MiB; f.prev=f.now;
        auto calc=[&]{return donorBudget(o,app,f,s,1024*MiB,1024*MiB,4096);};
        check(calc().bytes==64*MiB,"bounded eligible budget");
        f.protectedNow=true; check(calc().bytes==0,"foreground protected"); f.protectedNow=false;
        app.reclaim=false; check(calc().bytes==0,"explicit opt-in required"); app.reclaim=true;
        f.age=0; check(calc().reason=="startup_grace","startup grace"); f.age=60;
        f.now.low=f.now.current; check(calc().bytes==0,"memory.low budgeting floor"); f.now.low=0;
        f.now.min=Unlimited; check(calc().bytes==0,"memory.min=max budgeting floor"); f.now.min=0;
        f.refaultBytesPS=2*MiB; check(calc().reason=="refault_guard","refault guard"); f.refaultBytesPS=0;
        f.growthBytesPS=10*MiB; check(calc().reason=="growing_app","growth guard"); f.growthBytesPS=0;
        f.episodeRoom=4*MiB; check(calc().bytes==0,"episode budget exhausted"); f.episodeRoom=1024*MiB;
        f.minuteRoom=0; check(calc().bytes==0,"app rate limit"); f.minuteRoom=1024*MiB;
        check(donorBudget(o,app,f,s,1024*MiB,0,4096).bytes==0,"global rate limit");
        f.prev.valid=false; check(calc().reason=="need_two_samples","two samples required"); f.prev.valid=true;
        f.now.inactiveFile=0; f.prev.inactiveFile=0; s.swapFree=0;
        check(calc().bytes==0,"no swap means no anon hint");
        s.swapFree=4*1024*MiB; f.now.swapRoom=0;
        check(calc().bytes==0,"ancestor swap limit respected in budget");
        f.now.swapRoom=4*1024*MiB; o.swappiness=0;
        check(calc().bytes==0,"swappiness zero removes anon hint"); o.swappiness=-1;
        f.now.inactiveFile=512*MiB; f.prev.inactiveFile=512*MiB; o.allowAnon=false;
        f.now.dirty=512*MiB; check(calc().bytes==0,"dirty file estimate excluded"); f.now.dirty=0;
        check(calc().bytes>0,"clean file candidate");
        check(pressureBlock(o,s,6,0)=="io_pressure","fast I/O PSI guard");
        check(pressureBlock(o,s,0,3)=="memory_full_pressure","memory full PSI guard");
        s.io.valid=false; check(pressureBlock(o,s,0,0)=="psi_unavailable","missing PSI fails closed"); s.io.valid=true;
        EWMAPredictor pred; s.time=1; s.available=2000*MiB;
        auto p=pred.update(s,5,256*MiB); check(p.predictedAvailable==s.available,"forecast first sample");
        s.time=2; s.available=1900*MiB; p=pred.update(s,5,256*MiB);
        check(p.predictedAvailable<s.available,"declining availability forecast");
        s.time=100; s.available=3000*MiB; p=pred.update(s,5,256*MiB);
        check(p.predictedAvailable==s.available,"rising availability not predicted as demand");
        Forecast flat{s.available,0}; s.free=512*MiB;
        check(deficit(o,s,flat,0)==0,"no artificial deficit on idle system");
        check(deficit(o,s,flat,3000*MiB)>0,"launch hint can request extra reserve");
        std::cout<<"PASS: "<<checks<<" checks\n"; return 0;
    } catch(const std::exception& e) { std::cerr<<"FAIL after "<<checks<<" checks: "<<e.what()<<'\n'; return 1; }
}
