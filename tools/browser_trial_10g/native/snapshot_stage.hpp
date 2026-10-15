#pragma once
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <cstdio>
#include <cerrno>
namespace device_retry {
// Best-effort diagnostics only. No allocation, blocking retry, or authority effect.
class SnapshotStage {
    int fd_; bool enabled_=false; unsigned emitted_=0;
public:
    explicit SnapshotStage(int fd=STDERR_FILENO):fd_(fd) {
        const int saved=errno;
        const int flags=fcntl(fd_,F_GETFL);
        enabled_=flags>=0 && fcntl(fd_,F_SETFL,flags|O_NONBLOCK)==0;
        mark(0);errno=saved;
    }
    void mark(unsigned stage) noexcept {
        struct RestoreErrno { int value=errno; ~RestoreErrno(){errno=value;} } restore;
        if(!enabled_ || emitted_>=24)return;
        ++emitted_;struct timespec ts{};
        if(clock_gettime(CLOCK_BOOTTIME,&ts)!=0)return;
        char line[96];const int n=snprintf(line,sizeof(line),"SNAP_STAGE_V1 %u %lld %09ld\n",stage,static_cast<long long>(ts.tv_sec),ts.tv_nsec);
        if(n<=0 || static_cast<unsigned>(n)>=sizeof(line))return;
        const auto written=write(fd_,line,static_cast<size_t>(n));
        if(written!=n)enabled_=false; // missing diagnostics remain unknown
    }
    ~SnapshotStage(){mark(99);}
};
}
