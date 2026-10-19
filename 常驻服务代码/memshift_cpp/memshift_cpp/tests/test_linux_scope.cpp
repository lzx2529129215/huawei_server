#include "../src/linux_scope.hpp"
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <stdlib.h>
using namespace ms;
namespace fs = std::filesystem;
int checks=0;
void check(bool x,const char* msg) { ++checks; if(!x) throw std::runtime_error(msg); }
void rejects(const std::function<void()>& fn,const char* msg) {
    bool failed=false; try { fn(); } catch(const std::exception&) { failed=true; } check(failed,msg);
}
void put(const fs::path& p,const std::string& text) {
    fs::create_directories(p.parent_path()); std::ofstream(p)<<text;
}
const char* Stat =
    "anon 1073741824\nfile 536870912\nshmem 67108864\nfile_dirty 134217728\nfile_writeback 33554432\n"
    "slab_reclaimable 20971520\npswpin 7\npswpout 11\n";
const char* Pressure = "some avg10=3.00 avg60=1.00 avg300=0.50 total=4000\nfull avg10=1.00 avg60=0.50 avg300=0.10 total=900\n";
int main() {
    try {
        System host; host.time=5; host.swapTotal=16384*MiB; host.swapFree=16000*MiB; host.pswpin=1; host.pswpout=2;
        ScopeText t;
        t.max="5368709120\n"; t.current="2147483648\n"; t.stat=Stat;
        t.swapMax="max\n"; t.swapCurrent="0\n"; t.memPressure=Pressure; t.ioPressure=Pressure;
        auto s=scopeSystemFromText(t,host);
        check(s.total==5120*MiB,"scope total from memory.max");
        check(s.free==3072*MiB,"scope free = max - current");
        // clean = 512 - 64 shmem - 128 dirty - 32 writeback = 288 MiB; slab/2 = 10 MiB.
        check(s.available==(3072+288+10)*MiB,"available = free + clean file + slab/2");
        check(s.dirty==128*MiB && s.writebackBytes && *s.writebackBytes==32*MiB,"dirty and writeback carried");
        check(s.swapTotal==host.swapTotal && s.swapFree==host.swapFree,"unlimited scope swap uses host swap");
        check(s.pswpin==7 && s.pswpout==11,"scope swap counters preferred");
        check(s.mem.valid && s.mem.some10==3.0 && s.io.valid && s.time==5,"scope PSI and host clock");
        t.swapMax="1073741824\n"; t.swapCurrent="268435456\n";
        s=scopeSystemFromText(t,host);
        check(s.swapTotal==1024*MiB && s.swapFree==768*MiB,"scope swap budget clamps host swap");
        t.current="6442450944\n";
        s=scopeSystemFromText(t,host);
        check(s.free==0 && s.available<=s.total,"over-limit scope saturates free at zero");
        t.max="max\n";
        rejects([&]{scopeSystemFromText(t,host);},"unlimited scope rejected");
        t.max="5368709120\n"; t.stat="anon 1\n";
        rejects([&]{scopeSystemFromText(t,host);},"missing memory.stat counters rejected");

        const std::string base="[daemon]\nscope_cgroup=lab.slice\n";
        auto c=parseConfig(base+"[app a]\npath=lab.slice/a.service\nreclaim=true\n");
        check(c.options.scope=="lab.slice","scope_cgroup parsed");
        validateScopeTargets(c.options,c.apps);
        rejects([]{parseConfig("[daemon]\nscope_cgroup=../x\n[app a]\npath=x/a\n");},"unsafe scope path rejected");
        auto outside=parseConfig(base+"[app a]\npath=other.slice/a.service\n");
        rejects([&]{validateScopeTargets(outside.options,outside.apps);},"app outside scope rejected");
        auto equal=parseConfig(base+"[app a]\npath=lab.slice\n");
        rejects([&]{validateScopeTargets(equal.options,equal.apps);},"scope itself is not an app");
        auto prefix=parseConfig(base+"[app a]\npath=lab.slice2/a.service\n");
        rejects([&]{validateScopeTargets(prefix.options,prefix.apps);},"path-prefix lookalike rejected");

        Options capped; capped.reserve=410*MiB;
        check(boostCap(capped,16384*MiB)==(8192-410)*MiB,"default boost cap is half of memory minus reserve");
        capped.boostCapFraction=1.0;
        check(boostCap(capped,16384*MiB)==(16384-410)*MiB,"boost_cap_fraction=1 allows memory minus reserve");
        check(parseConfig("[daemon]\nboost_cap_fraction=1.0\n[app a]\npath=x/a\n").options.boostCapFraction==1.0,"boost_cap_fraction parsed");
        rejects([]{parseConfig("[daemon]\nboost_cap_fraction=1.5\n[app a]\npath=x/a\n");},"boost_cap_fraction above 1 rejected");
        char pattern[]="/tmp/memshift_scope_XXXXXX";
        const char* dir=::mkdtemp(pattern);
        check(dir!=nullptr,"temp fixture directory");
        const fs::path rootDir=fs::path(dir)/"cgroup", procDir=fs::path(dir)/"proc";
        put(procDir/"meminfo","MemTotal: 16000000 kB\nMemFree: 9000000 kB\nMemAvailable: 12000000 kB\nSwapTotal: 4194304 kB\nSwapFree: 4194304 kB\n");
        put(procDir/"pressure"/"memory",Pressure); put(procDir/"pressure"/"io",Pressure);
        put(procDir/"vmstat","pswpin 1\npswpout 2\n");
        const auto scopeDir=rootDir/"lab.slice";
        put(scopeDir/"memory.max","5368709120\n"); put(scopeDir/"memory.current","1073741824\n");
        put(scopeDir/"memory.stat",Stat); put(scopeDir/"memory.pressure",Pressure); put(scopeDir/"io.pressure",Pressure);
        FD root(::open(rootDir.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC));
        check(root.get()>=0,"open fixture root");
        Options o; o.proc=procDir.string(); o.scope="lab.slice";
        auto fromFiles=machineSample(o,root.get());
        check(fromFiles.total==5120*MiB && fromFiles.free==4096*MiB,"machineSample reads the scope cgroup");
        check(fromFiles.swapTotal==4096*MiB,"missing scope swap files fall back to host swap");
        o.scope.clear();
        auto hostOnly=machineSample(o,root.get());
        check(hostOnly.total==16000000ULL*1024,"empty scope keeps host meminfo behavior");
        fs::remove_all(dir);
        std::cout<<"linux scope checks passed: "<<checks<<'\n';
        return 0;
    } catch(const std::exception& e) {
        std::cerr<<"FAILED after "<<checks<<" checks: "<<e.what()<<'\n';
        return 1;
    }
}
