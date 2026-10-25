// reclaim_interface=high: watermark arithmetic, zswap quota plan, config and
// control parsing. The quota numbers are the experiment plan's scenario B
// (results/linux_oom_lab_rss_plan_20261022/实验设置.md §3/§4).
#include "../src/io.hpp"
#include "../src/watermark.hpp"
#include <functional>
#include <iostream>
using namespace ms;
int checks=0;
void check(bool x,const std::string& msg) { ++checks; if(!x) throw std::runtime_error(msg); }
void rejects(const std::function<void()>& fn,const char* msg) {
    bool failed=false; try { fn(); } catch(const std::exception&) { failed=true; } check(failed,msg);
}
Bytes mib(double x) { return amount(x*static_cast<double>(MiB)); }
bool near(Bytes a,double mibValue,double tol=1.0) { return std::abs(static_cast<double>(a)/MiB-mibValue)<=tol; }
const Bytes Page=4096;

// tmo / hint_mode / brake keys: defaults, the acceptance scenario's daemon section, range checks.
void testNewConfig() {
    const std::string app="[app a]\npath=lab.slice/a\nreclaim=true\npriority=900\n";
    const std::string high="[daemon]\nscope_cgroup=lab.slice\nreclaim_interface=high\npulse_mb=64\nmin_batch_mb=2\n";
    Options d=parseConfig(high+app).options;
    check(!d.tmo && d.tmoIntervalMs==2000 && d.tmoRatio==0.02 && d.tmoMinStep==2*MiB && d.tmoPsiTargetUs==2000 &&
          d.tmoBackoffSec==10,"tmo defaults: off, 2000 ms, 2%, 2 MiB, 2000 us, 10 s");
    check(d.hintMode=="watermark" && d.hintStopSec==5,"hint_mode defaults to watermark, stop 5 s");
    check(d.brakeSomeMs==0 && d.brakeResumeMs==15 && d.brakeResumeSec==2,"brake defaults: off, 15 ms, 2 s");
    check(!parseConfig("[daemon]\n[app a]\npath=x/a\n").options.tmo,"reclaim mode config unchanged");
    const std::string scenario="[daemon]\nscope_cgroup=memlab.slice\nreclaim_interface=high\ninterval_ms=250\nstartup_grace_sec=5\n"
        "wm_low_mb=512\nwm_high_mb=1024\nhint_reserve_mb=512\nhint_gap_mb=256\nfloor_ratio=0.2\nassumed_compress_ratio=3.0\n"
        "pulse_mb=64\npulse_rate_mb_s=256\npulse_timeout_ms=5000\nbackoff_sec=10\nmax_boost_mb=24576\nmin_batch_mb=2\n"
        "tmo=true\ntmo_interval_ms=2000\ntmo_ratio=0.02\ntmo_min_step_mb=2\ntmo_psi_target_us=2000\ntmo_backoff_sec=10\n"
        "hint_mode=rate\nhint_stop_sec=5\nbrake_some_ms=30\nbrake_resume_ms=15\nbrake_resume_sec=2\n"
        "[app chrome]\npath=memlab.slice/memlab-chrome.service\nreclaim=true\nmin_keep_mb=32\npriority=900\n";
    Options s=parseConfig(scenario).options;
    check(s.tmo && s.hintMode=="rate" && s.brakeSomeMs==30 && s.brakeResumeMs==15 && s.brakeResumeSec==2 &&
          s.tmoMinStep==2*MiB && s.hintStopSec==5 && s.intervalMs==250,"acceptance scenario daemon section parsed");
    auto other=parseConfig(high+"tmo_interval_ms=500\ntmo_ratio=0.5\ntmo_min_step_mb=64\ntmo_psi_target_us=10000000\n"
                           "tmo_backoff_sec=600\nhint_stop_sec=0\nbrake_some_ms=1000\nbrake_resume_ms=1000\nbrake_resume_sec=0\n"+app).options;
    check(other.tmoIntervalMs==500 && other.tmoRatio==0.5 && other.tmoMinStep==64*MiB && other.tmoPsiTargetUs==1e7 &&
          other.tmoBackoffSec==600 && other.hintStopSec==0 && other.brakeSomeMs==1000 && other.brakeResumeSec==0,"range ends accepted");
    for(const char* bad:{"tmo_interval_ms=499","tmo_interval_ms=60001","tmo_ratio=0","tmo_ratio=0.51","tmo_min_step_mb=0",
                         "tmo_min_step_mb=65","tmo_psi_target_us=0","tmo_psi_target_us=10000001","tmo_backoff_sec=0.5",
                         "tmo_backoff_sec=601","hint_mode=fast","hint_stop_sec=-1","hint_stop_sec=61","brake_some_ms=-1",
                         "brake_some_ms=1001","brake_resume_ms=0","brake_resume_ms=1001","brake_resume_sec=61","tmo=yes","tmo=True"})
        rejects([&]{parseConfig(high+bad+"\n"+app);},(std::string("out of range or invalid: ")+bad).c_str());
    for(const char* key:{"tmo=true","hint_mode=rate","brake_some_ms=30"})
        rejects([&]{parseConfig(std::string("[daemon]\n")+key+"\n[app a]\npath=x/a\n");},(std::string("needs reclaim_interface=high: ")+key).c_str());
    check(!parseConfig("[daemon]\nbrake_some_ms=0\nhint_mode=watermark\ntmo=false\n[app a]\npath=x/a\n").options.tmo,"off values allowed in reclaim mode");
    rejects([&]{parseConfig(high+"brake_some_ms=10\n"+app);},"brake_resume_ms (15) above brake_some_ms rejected");
    check(parseConfig(high+"brake_some_ms=10\nbrake_resume_ms=10\n"+app).options.brakeSomeMs==10,"resume == on threshold allowed");
    const std::string tiny="[daemon]\nscope_cgroup=lab.slice\nreclaim_interface=high\nmin_batch_mb=1\npulse_mb=2\ntmo_min_step_mb=4\n";
    rejects([&]{parseConfig(tiny+"tmo=true\n"+app);},"tmo_min_step_mb above pulse_mb rejected with tmo");
    rejects([&]{parseConfig(tiny+"hint_mode=rate\n"+app);},"tmo_min_step_mb above pulse_mb rejected with rate");
    check(parseConfig(tiny+app).options.pulse==2*MiB,"tmo_min_step_mb unused without tmo/rate");
}

// Control file peak_at_unix and the memshiftctl hint rule.
void testControlPeakAt() {
    const std::string four="paused=false\nprotected_apps=gimp\nreserve_extra_mb=9892\nboost_until_unix=100\n";
    auto c=parseControl(four+"peak_targets=gimp:2107\npeak_at_unix=1761300000\n");
    check(c.peakAt==1761300000 && c.peakTargets.size()==1,"peak_at_unix parsed with peak_targets");
    check(controlText(c).find("peak_at_unix=1761300000\n")!=std::string::npos,"peak_at_unix written when set");
    auto again=parseControl(controlText(c));
    check(again.peakAt==c.peakAt && again.peakTargets==c.peakTargets && again.boost==c.boost && again.until==c.until &&
          again.protectedApps==c.protectedApps,"control round trip with peak_at_unix");
    check(parseControl(four+"peak_at_unix=5\n").peakAt==5,"peak_at_unix without peak_targets");
    auto old=parseControl(four);
    check(old.peakAt==0 && controlText(old)==four,"four-key control: no peak time, text unchanged");
    check(parseControl(four+"peak_at_unix=0\n").peakAt==0 && controlText(parseControl(four+"peak_at_unix=0\n"))==four,
          "peak_at_unix=0 means unknown and is not written");
    rejects([]{parseControl("paused=false\nprotected_apps=\nreserve_extra_mb=0\npeak_targets=a:1\npeak_at_unix=5\n");},
            "missing key rejected even with both optional keys");
    rejects([&]{parseControl(four+"peak_at_unix=5\npeak_at_unix=6\n");},"duplicate peak_at_unix rejected");
    rejects([&]{parseControl(four+"peak_at_unix=12.5\n");},"fractional peak_at_unix rejected in the control file");
    rejects([&]{parseControl(four+"peak_at_unix=-1\n");},"negative peak_at_unix rejected");

    const Bytes now=1000;
    Control k=parseControl(four);
    HintUpdate h; h.peakAt=2000;
    rejects([&]{applyHint(k,h,now);},"--peak-at-unix without --boost-mb rejected");
    h=HintUpdate{}; h.targets="gimp:1";
    rejects([&]{applyHint(k,h,now);},"--peak-targets without --boost-mb rejected");
    h=HintUpdate{}; h.boost=6000*MiB; h.ttl=60; h.targets="gimp:2107,shotcut:6434"; h.peakAt=1040;
    applyHint(k,h,now);
    check(k.boost==6000*MiB && k.until==1060 && k.peakAt==1040 && k.peakTargets.size()==2,"boost sets expiry, targets and peak time");
    check(k.protectedApps.count("gimp") && !k.paused,"hint leaves protection and pause alone");
    applyHint(k,HintUpdate{},now);
    check(k.boost==6000*MiB && k.peakAt==1040 && k.peakTargets.size()==2,"no hint options: hint unchanged");
    h=HintUpdate{}; h.boost=512*MiB;
    applyHint(k,h,now);
    check(k.boost==512*MiB && k.peakAt==0 && k.peakTargets.empty(),"a new boost without them clears peak time and targets");
    h.peakAt=1040; h.targets="gimp:1"; applyHint(k,h,now);
    h=HintUpdate{}; h.boost=0; h.peakAt=1040; h.targets="gimp:1";
    applyHint(k,h,now);
    check(k.boost==0 && k.until==0 && k.peakAt==0 && k.peakTargets.empty(),"clearing the boost clears peak time and targets");
    h=HintUpdate{}; h.boost=512*MiB; h.targets="none"; h.peakAt=0;
    applyHint(k,h,now);
    check(k.peakTargets.empty() && k.peakAt==0 && k.boost==512*MiB,"targets none and peak 0 mean none");
    check(unixTimeValue("1761300000")==1761300000 && unixTimeValue("100.4")==100 && unixTimeValue("100.5")==101 &&
          unixTimeValue("99.99")==100,"--peak-at-unix: integer, decimals rounded");
    for(const char* bad:{"","-5","1e9",".5","5.","1.2.3","12a"})
        rejects([&]{unixTimeValue(bad);},(std::string("bad unix time rejected: '")+bad+"'").c_str());
}

void testPsiWindowAndBrake() {
    PsiWindow w;
    for(int i=0;i<=8;++i) w.add(0.25*i,static_cast<Bytes>(1000*i),1.0);   // 4 ms of stall per second
    check(w.over(1.0)==4000,"steady 4000 us/s: 4000 us over 1 s");
    check(w.over(0.5)==2000,"shorter span from the kept history");
    PsiWindow v;
    v.add(0,0,1.0); v.add(0.6,6000,1.0); v.add(1.2,12000,1.0);
    check(v.over(1.0)==10000,"total at now - 1 s interpolated between the samples around it");
    PsiWindow young;
    young.add(0,0,1.0); young.add(0.5,500,1.0);
    check(young.over(1.0)==500,"less than 1 s of history: window from the oldest sample");
    young.add(0.75,100,1.0);
    check(young.over(1.0)==0,"counter going backwards restarts the window");
    PsiWindow one; one.add(5,123456,1.0);
    check(one.over(1.0)==0,"a single sample has no stall");

    Brake b(30,15,2);
    auto at=[&](double t,Bytes us){ return b.update(t,us); };
    check(!at(0,0) && !at(0.25,0),"brake off without stall");
    check(at(0.5,30000) && b.windowMs()==30,"brake on at exactly 30 ms in the last 1 s");
    check(at(0.75,30000) && at(1.0,30000) && at(1.25,30000),"stays on while the window holds the burst");
    check(at(1.5,30000) && b.windowMs()==0,"window empty again: still on, resume timer starts");
    check(at(2.0,30000) && at(2.5,40000),"below 15 ms (10 ms) for 1 s: still on");
    check(at(3.0,56000) && b.windowMs()==26,"26 ms again: resume timer reset");
    check(at(3.5,56000) && b.windowMs()==16,"16 ms is not below the resume level");
    check(at(4.0,56000) && at(5.0,56000) && at(5.99,56000),"below again since 4.0 s: on until 2 s have passed");
    check(!at(6.0,56000),"off after 2 s below 15 ms");
    check(!at(6.25,56000+29999),"29.999 ms: stays off");
    check(at(7.0,56000+30000),"30 ms within the last 1 s: on again");
    Brake quick(30,15,0);
    check(quick.update(0,0)==false && quick.update(0.5,40000) && !quick.update(1.6,40000),"resume_sec 0: off at the first quiet window");
    Brake off(0,15,2);
    check(!off.update(0,0) && !off.update(0.5,900000),"threshold 0 never brakes");
}

void testTmo() {
    Options o; o.reclaimInterface="high"; o.pulse=64*MiB; o.tmo=true;   // 2 s, 2%, 2 MiB, 2000 us, 10 s
    check(tmoStep(o,1000*MiB,0,Page)==amount(0.02*1000*MiB)/Page*Page,"step = 2% of the room at zero PSI");
    check(near(tmoStep(o,1000*MiB,1000,Page),10,0.01),"half the PSI target halves the step");
    check(tmoStep(o,1000*MiB,2000,Page)==0 && tmoStep(o,1000*MiB,2500,Page)==0,"PSI at or above the target: no step");
    check(tmoStep(o,50*MiB,0,Page)==2*MiB,"small step lifted to tmo_min_step");
    check(tmoStep(o,10240*MiB,0,Page)==amount(0.02*10240*MiB)/Page*Page,"large step not capped (the worker splits it into pulses)");
    check(tmoStep(o,777*MiB+123,0,Page)%Page==0,"step page aligned");
    check(tmoHasRoom(o,3*MiB,Page) && !tmoHasRoom(o,2*MiB,Page),"room: one pulse can take tmo_min_step (ratio 3: 2/3 of the room)");

    TmoApp a;
    const Bytes room=1000*MiB;
    a.psi.add(0,0,2.0);
    tmoEvaluate(o,a,0,true,room,Page);
    check(a.dueSince==0 && near(a.step,20,0.01) && a.next==2.0,"first evaluation: no stall yet, due with 2% of the room");
    tmoSubmitted(o,a,0.1);
    check(a.dueSince<0 && std::abs(a.next-2.1)<1e-9,"submitted: not due, next pulse one interval later");
    a.psi.add(1.0,500,2.0); tmoEvaluate(o,a,1.0,true,room,Page);
    check(a.dueSince<0,"not due before the next interval");
    a.psi.add(2.25,1000,2.0); tmoEvaluate(o,a,2.25,true,room,Page);
    // Stall over [0.25, 2.25]: 1000 - 125 (interpolated at 0.25) = 875 us -> 2% x (1 - 875/2000).
    check(a.dueSince==2.25 && a.lastPsi==875 && a.step==amount(0.02*1000*MiB*(1-875.0/2000)),"stall over the last 2 s shrinks the step");
    a.psi.add(3.0,1100,2.0); tmoEvaluate(o,a,3.0,true,room,Page);
    check(a.dueSince==2.25,"still due between evaluations (oldest due time kept)");
    a.psi.add(4.5,4000,2.0); tmoEvaluate(o,a,4.5,true,room,Page);
    check(a.dueSince<0 && a.backoffUntil==14.5 && a.next==14.5 && a.lastPsi>=2000,"stall above the target: paused 10 s");
    a.psi.add(10,4000,2.0); tmoEvaluate(o,a,10,true,room,Page);
    check(a.dueSince<0 && 10<a.backoffUntil,"no pulse during the pause");
    a.psi.add(14.5,4100,2.0); tmoEvaluate(o,a,14.5,true,room,Page);
    check(a.dueSince==14.5 && a.next==16.5,"after the pause: evaluated again on the last interval only");
    tmoEvaluate(o,a,15,false,room,Page);
    check(a.dueSince<0 && a.next==16.5,"ineligible: not due, timer kept");
    a.psi.add(17,4100,2.0); tmoEvaluate(o,a,17,true,2*MiB,Page);
    check(a.dueSince<0 && a.next==19,"at the floor: not due");
    a.psi.add(19,4100,2.0); tmoEvaluate(o,a,19,true,3*MiB,Page);
    check(a.dueSince==19 && a.step==2*MiB,"a little room: one tmo_min_step pulse");
}

void testRate() {
    Options o; o.reclaimInterface="high"; o.pulse=64*MiB; o.hintMode="rate";   // reserve 512, stop 5 s, min step 2
    check(rateNeed(o,6000*MiB,1000*MiB,16384*MiB)==5512*MiB,"need = remaining + hint reserve - free");
    {
        // Whole-hint remaining: unseen apps count in full, live ones target - max seen, gone ones 0.
        std::vector<HintApp> h{{3000*MiB,false,false,0},{2000*MiB,true,true,500*MiB},{4000*MiB,true,false,4000*MiB}};
        check(hintRemaining(h)==4500*MiB,"hint remaining: unseen full, live target - max, gone 0");
        // At the hint nothing runs: need = all targets + reserve - free, larger than the scope allows.
        std::vector<HintApp> all{{10000*MiB,false,false,0},{10000*MiB,false,false,0}};
        check(rateNeedHint(o,hintRemaining(all),16384*MiB)==(20000+512-16384)*MiB,"need at the hint = case deficit (no cap)");
        // Reclaiming 1000 MiB from a live app: max seen stays, free grows, need falls 1:1.
        std::vector<HintApp> live{{10000*MiB,true,true,8000*MiB}};
        const Bytes before=rateNeedHint(o,hintRemaining(live),1000*MiB), after=rateNeedHint(o,hintRemaining(live),2000*MiB);
        check(before-after==1000*MiB,"reclaim lowers the rate need 1:1");
    }
    check(rateNeed(o,20000*MiB,1000*MiB,16384*MiB)==15384*MiB,"hinted high watermark capped at the scope size");
    check(rateNeed(o,100*MiB,2000*MiB,16384*MiB)==0,"enough free: no need");
    check(rateLeft(o,1000,980)==15 && rateLeft(o,1000,995)==0,"left = peak - now - hint_stop_sec");
    const Bytes w=rateWant(o,5512*MiB,15,0.25,Page);
    check(near(w,5512.0/15*0.25,0.01) && w%Page==0,"want = need / left * tick");
    check(rateWant(o,MiB,15,0.25,Page)==2*MiB,"small need: at least tmo_min_step");
    check(rateWant(o,100*MiB,0.01,0.25,Page)==100*MiB,"never more than the need when time is short");
    check(rateWant(o,100*MiB,0,0.25,Page)==0 && rateWant(o,100*MiB,-3,0.25,Page)==0 && rateWant(o,0,10,0.25,Page)==0,
          "stopped at hint_stop_sec, or nothing needed");
    // Donor: most expendable with room, ties by room; backoff / PSI at target / no room / ineligible skipped.
    std::vector<RateDonor> v{
        {0,900,500*MiB,true,false,0},{1,900,800*MiB,true,false,1999},{2,950,100*MiB,true,true,0},
        {3,950,900*MiB,true,false,2000},{4,990,900*MiB,false,false,0},{5,995,2*MiB,true,false,0}};
    auto d=pickRateDonor(o,v,Page);
    check(d && *d==1,"highest priority wins, ties by the largest room; paused/over-target/no-room/ineligible skipped");
    v[2].backoff=false;
    d=pickRateDonor(o,v,Page);
    check(d && *d==2,"a higher-priority donor out of backoff is chosen even with less room");
    check(!pickRateDonor(o,{},Page),"no donor");
}
int main() {
    try {
        Options o; o.reclaimInterface="high";
        // Watermarks: normal pair, raised by a hint, never below normal.
        auto w=watermarks(o,false,0);
        check(w.low==512*MiB && w.high==1024*MiB,"normal watermarks 512/1024");
        w=watermarks(o,true,9892*MiB);
        check(w.high==(9892+512)*MiB && w.low==(9892+512-256)*MiB,"hinted: high = remaining + 512, low = high - 256");
        w=watermarks(o,true,100*MiB);
        check(w.low==512*MiB && w.high==1024*MiB,"small remaining keeps normal watermarks");
        // Remaining peak: only alive apps, never negative.
        std::map<std::string,Bytes> targets{{"shotcut",6434*MiB},{"gimp",2107*MiB},{"gone",500*MiB}};
        std::map<std::string,Bytes> currents{{"shotcut",1128*MiB},{"gimp",2200*MiB}};
        check(remainingPeak(targets,currents)==5306*MiB,"remaining = sum of max(0, target - current) over alive apps");
        // Floor and pulse size.
        DonorView d{"vlc",990,232*MiB,145*MiB,377*MiB};
        check(residentFloor(o,d)==amount(0.2*377*MiB),"floor = 20% of the reference");
        check(residentRoom(o,d)==377*MiB-amount(0.2*377*MiB),"room = anon + file - floor");
        check(pulseLimit(o,3000*MiB,4096)==256*MiB,"pulse capped at pulse_mb");
        check(pulseLimit(o,30*MiB,4096)==20*MiB,"small room: net drop at most 2/3 of the room (ratio 3)");

        // Scenario B background apps (anon, file), priorities = expendability order.
        std::vector<DonorView> b{
            {"bilibili",980,mib(441),mib(615),0},{"douyin",970,mib(946),mib(634),0},{"wechat",900,mib(360),mib(634),0},
            {"dingtalk",920,mib(212),mib(514),0},{"gnote",960,mib(364),mib(173),0},{"rhythmbox",200,mib(66),mib(113),0},
            {"evolution",940,mib(141),mib(26),0},{"vlc",990,mib(232),mib(145),0}};
        auto p=planZswapQuotas(o,b,4367*MiB,4096);
        check(near(p.zswapCapacity,3756),"B all-zswap capacity 3,756");
        check(near(p.deficit,611),"B deficit 611");
        check(near(p.diskAnon,1833,1.5) && p.unplaced==0,"B anon to disk 1,832");
        for(auto n:{"vlc","bilibili","douyin","gnote","evolution"})
            check(p.quota.count(n) && p.quota.at(n)==0,std::string("B full disk app has quota 0: ")+n);
        check(p.quota.count("dingtalk") && near(p.diskShare.at("dingtalk"),133,1.5) && near(p.quota.at("dingtalk"),12.2,0.5),
              "B dingtalk partial: 133 of 170 to disk, quota = (170-133)/3 compressed");
        check(!p.quota.count("wechat") && !p.quota.count("rhythmbox"),"B important apps keep zswap max");
        // Scenario A: all-zswap is enough, no quota.
        std::vector<DonorView> a{
            {"wechat",900,mib(360),mib(634),0},{"feishu",910,mib(1115),mib(798),0},{"dingtalk",920,mib(212),mib(514),0},
            {"thunderbird",930,mib(465),mib(930),0},{"gnote",960,mib(364),mib(173),0},{"rhythmbox",200,mib(66),mib(113),0},
            {"evolution",940,mib(141),mib(26),0},{"vlc",990,mib(232),mib(145),0}};
        p=planZswapQuotas(o,a,4138*MiB,4096);
        check(near(p.zswapCapacity,4242) && p.quota.empty() && p.deficit==0,"A: all-zswap 4,242 covers 4,138");
        // Not enough even with everything to disk.
        p=planZswapQuotas(o,a,6000*MiB,4096);
        check(p.unplaced>0 && p.quota.size()==8,"impossible need: all donors to disk and the rest reported unplaced");
        for(auto& [n,q]:p.quota) check(q==0,"impossible need: every quota 0");

        // Config parsing.
        const std::string base="[daemon]\nscope_cgroup=lab.slice\nreclaim_interface=high\nzswap_quota=true\nwm_low_mb=512\nwm_high_mb=1024\n"
                               "hint_reserve_mb=512\nhint_gap_mb=256\nfloor_ratio=0.2\nassumed_compress_ratio=3\npulse_mb=256\n";
        auto c=parseConfig(base+"[app vlc]\npath=lab.slice/vlc.service\nreclaim=true\npriority=990\n[app gimp]\npath=lab.slice/gimp.service\npriority=-100\n");
        check(c.options.reclaimInterface=="high" && c.options.zswapQuota && c.options.pulse==256*MiB,"high options parsed");
        check(c.apps[0].priority==990 && c.apps[1].priority==-100,"priority parsed, negatives allowed");
        rejects([&]{parseConfig("[daemon]\nreclaim_interface=high\n[app a]\npath=x/a\n");},"high requires scope_cgroup");
        rejects([&]{parseConfig("[daemon]\nzswap_quota=true\n[app a]\npath=x/a\n");},"zswap_quota requires high");
        rejects([&]{parseConfig(base+"floor_ratio_x=1\n[app a]\npath=lab.slice/a\n");},"unknown key rejected");
        rejects([&]{parseConfig("[daemon]\nscope_cgroup=lab.slice\nreclaim_interface=high\nwm_low_mb=1024\nwm_high_mb=512\n[app a]\npath=lab.slice/a\n");},
                "high watermark must exceed low");
        rejects([&]{parseConfig(base+"[app a]\npath=lab.slice/a\npriority=12x\n");},"bad priority rejected");
        check(parseConfig("[daemon]\n[app a]\npath=x/a\n").options.reclaimInterface=="reclaim","default stays memory.reclaim");

        // Control: peak_targets optional and round-trips.
        auto ctl=parseControl("paused=false\nprotected_apps=gimp\nreserve_extra_mb=9892\nboost_until_unix=100\npeak_targets=gimp:2107,shotcut:6434\n");
        check(ctl.peakTargets.size()==2 && ctl.peakTargets.at("shotcut")==6434*MiB,"peak targets parsed");
        check(parseControl(controlText(ctl)).peakTargets==ctl.peakTargets,"peak targets round-trip");
        auto old=parseControl("paused=false\nprotected_apps=\nreserve_extra_mb=0\nboost_until_unix=0\n");
        check(old.peakTargets.empty() && controlText(old).find("peak_targets")==std::string::npos,"four-key control still valid");
        rejects([]{parseControl("paused=false\nprotected_apps=\nreserve_extra_mb=0\npeak_targets=a:1\n");},"missing key still rejected");
        rejects([]{parsePeakTargets("a:1,a:2");},"duplicate target rejected");
        rejects([]{parsePeakTargets("a=1");},"target without colon rejected");

        testNewConfig();
        testControlPeakAt();
        testPsiWindowAndBrake();
        testTmo();
        testRate();
        std::cout<<"watermark tests passed: "<<checks<<" checks\n";
        return 0;
    } catch(const std::exception& e) { std::cerr<<"FAILED: "<<e.what()<<" (after "<<checks<<" checks)\n"; return 1; }
}
