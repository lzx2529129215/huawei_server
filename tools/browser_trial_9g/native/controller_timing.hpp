#pragma once
#include <time.h>
#include <cstdint>
namespace controller_timing {
inline double clock(int id){timespec t{};if(clock_gettime(id,&t))return -1;return double(t.tv_sec)+double(t.tv_nsec)/1e9;}
struct Stat {std::uint64_t count=0,bytes=0,errors=0;double total=0,max=0,cpuTotal=0,cpuMax=0,waitTotal=0;std::uint64_t cpuUnavailable=0;};
struct Slow {unsigned kind=0;double pre=0,post=0,monoPre=0,monoPost=0;std::uint64_t bytes=0;double cpuPre=0,cpuPost=0;};
struct Stats {Stat stages[8]{};Slow slow[24]{};unsigned used=0,dropped=0;
 void add(unsigned k,double pre,double post,double monoPre,double monoPost,std::uint64_t bytes,double cpuPre,double cpuPost){auto& s=stages[k];++s.count;s.bytes+=bytes;if(pre<0||post<pre||monoPre<0||monoPost<monoPre){++s.errors;return;}double d=post-pre;if(cpuPre>=0 && cpuPost>=cpuPre){double cpu=cpuPost-cpuPre;s.cpuTotal+=cpu;if(cpu>s.cpuMax)s.cpuMax=cpu;s.waitTotal+=d-cpu;}else ++s.cpuUnavailable;s.total+=d;if(d>s.max)s.max=d;if(d>=0.1){if(used<24)slow[used++]={k,pre,post,monoPre,monoPost,bytes,cpuPre,cpuPost};else {++dropped;unsigned smallest=0;for(unsigned i=1;i<24;++i)if(slow[i].post-slow[i].pre<slow[smallest].post-slow[smallest].pre)smallest=i;if(d>slow[smallest].post-slow[smallest].pre)slow[smallest]={k,pre,post,monoPre,monoPost,bytes,cpuPre,cpuPost};}}}
};
inline Stats stats;
struct Scope {unsigned kind;std::uint64_t bytes;double pre,mono,cpu;
 Scope(unsigned k,std::uint64_t b):kind(k),bytes(b),pre(clock(CLOCK_BOOTTIME)),mono(clock(CLOCK_MONOTONIC)),cpu(clock(CLOCK_THREAD_CPUTIME_ID)){}
 ~Scope(){double post=clock(CLOCK_BOOTTIME),m=clock(CLOCK_MONOTONIC);stats.add(kind,pre,post,mono,m,bytes,cpu,clock(CLOCK_THREAD_CPUTIME_ID));}
};
}
