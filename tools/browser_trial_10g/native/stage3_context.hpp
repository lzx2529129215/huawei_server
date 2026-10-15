#pragma once
#include "controller_timing.hpp"
namespace stage3_context {
// Main-thread-only fixed storage. No allocation or sink IO in checkpoints.
struct Entry {unsigned group=0,target=0,phase=0;std::uint64_t generation=0,bytes=0;double pre=0,post=0,monoPre=0,monoPost=0,cpuPre=0,cpuPost=0;};
struct Store {controller_timing::Stat totals[2][9]{};Entry longest[24]{};unsigned used=0;std::uint64_t omitted=0;
 void add(const Entry& e){auto& s=totals[e.group][e.phase];++s.count;s.bytes+=e.bytes;if(e.pre<0||e.post<e.pre){++s.errors;return;}const double wall=e.post-e.pre;s.total+=wall;if(wall>s.max)s.max=wall;if(e.cpuPre>=0&&e.cpuPost>=e.cpuPre){s.cpuTotal+=e.cpuPost-e.cpuPre;}else ++s.cpuUnavailable;
 if(wall<0.01)return;if(used<24){longest[used++]=e;return;}++omitted;unsigned k=0;for(unsigned i=1;i<24;++i)if(longest[i].post-longest[i].pre<longest[k].post-longest[k].pre)k=i;if(wall>longest[k].post-longest[k].pre)longest[k]=e;}
};
inline Store store;
struct Scope {Entry e;Scope(unsigned group,unsigned target,std::uint64_t generation){e.group=group;e.target=target;e.generation=generation;start();}
 void start(){e.pre=controller_timing::clock(CLOCK_BOOTTIME);e.monoPre=controller_timing::clock(CLOCK_MONOTONIC);e.cpuPre=controller_timing::clock(CLOCK_THREAD_CPUTIME_ID);}
 void close(){e.post=controller_timing::clock(CLOCK_BOOTTIME);e.monoPost=controller_timing::clock(CLOCK_MONOTONIC);e.cpuPost=controller_timing::clock(CLOCK_THREAD_CPUTIME_ID);store.add(e);}
 void step(unsigned phase,std::uint64_t bytes=0){close();e.phase=phase;e.bytes=bytes;start();}~Scope(){close();}
};
}
