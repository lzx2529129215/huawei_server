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
        std::cout<<"watermark tests passed: "<<checks<<" checks\n";
        return 0;
    } catch(const std::exception& e) { std::cerr<<"FAILED: "<<e.what()<<" (after "<<checks<<" checks)\n"; return 1; }
}
