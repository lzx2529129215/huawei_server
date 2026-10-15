#pragma once
#include "device_proc_reader.hpp"
#include <atomic>
#include <poll.h>
#include <signal.h>
#include <sys/wait.h>
extern char** environ;
namespace device_retry {
using ms::harmony_v1::device_proc::boot;
struct ExecGate {
    bool enabled=false;
    double notBefore=0,deadline=0;
    // Preallocated in the parent. The child uses only async-signal-safe calls.
    std::string expectedBoot;
    std::string planPath{},planBytes{};
};
struct Command {
    bool ok=false,gatePassed=false,reaped=false;
    int status=-1;
    double pre=0,post=0,execGateAt=-1;
    std::string output,error,reason;
};
struct ChildGateRecord { int passed; double at; };
inline bool childGate(const ExecGate& gate,ChildGateRecord& record) {
    timespec t{};
    if(clock_gettime(CLOCK_BOOTTIME,&t)!=0)return false;
    record.at=static_cast<double>(t.tv_sec)+static_cast<double>(t.tv_nsec)/1e9;
    if(!gate.enabled){record.passed=1;return true;}
    if(record.at<gate.notBefore || record.at>=gate.deadline)return false;
    const int fd=open("/proc/sys/kernel/random/boot_id",O_RDONLY|O_CLOEXEC);
    if(fd<0)return false;
    char buffer[64];const ssize_t n=read(fd,buffer,sizeof(buffer));close(fd);
    if(n!=37 || gate.expectedBoot.size()!=36 || buffer[36]!='\n')return false;
    for(unsigned i=0;i<36;++i)if(buffer[i]!=gate.expectedBoot[i])return false;
    if(!gate.planPath.empty()) {
        const int plan=open(gate.planPath.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
        if(plan<0)return false;
        char data[16385];ssize_t used=0;bool good=true;
        for(;;) {
            const ssize_t got=read(plan,data+used,sizeof(data)-static_cast<std::size_t>(used));
            if(got<0){if(errno==EINTR)continue;good=false;break;}
            if(got==0)break;
            used+=got;if(used>=static_cast<ssize_t>(sizeof(data))){good=false;break;}
        }
        close(plan);
        if(!good || used!=static_cast<ssize_t>(gate.planBytes.size()))return false;
        for(ssize_t i=0;i<used;++i)if(data[i]!=gate.planBytes[static_cast<std::size_t>(i)])return false;
    }
    // Recheck time after the boot read, immediately before returning to exec.
    if(clock_gettime(CLOCK_BOOTTIME,&t)!=0)return false;
    record.at=static_cast<double>(t.tv_sec)+static_cast<double>(t.tv_nsec)/1e9;
    record.passed=record.at>=gate.notBefore && record.at<gate.deadline ? 1:0;
    return record.passed==1;
}
inline Command command(const std::vector<std::string>& args,
                       const std::atomic<bool>& stop,unsigned milliseconds,
                       std::size_t limit,const ExecGate& gate={},bool createGroup=true) {
    Command r;r.pre=boot();
    if(r.pre<0){r.reason="clock_failed";return r;}
    if(args.empty() || args[0].empty() || args[0][0]!='/') {
        r.reason="absolute_command_required";return r;
    }
    std::vector<char*> argv;
    for(const auto& a:args)argv.push_back(const_cast<char*>(a.c_str()));
    argv.push_back(nullptr);
    int out[2],err[2],gated[2];
    if(pipe2(out,O_CLOEXEC)!=0){r.reason="pipe_failed";return r;}
    if(pipe2(err,O_CLOEXEC)!=0){close(out[0]);close(out[1]);r.reason="pipe_failed";return r;}
    if(pipe2(gated,O_CLOEXEC)!=0){for(int fd:{out[0],out[1],err[0],err[1]})close(fd);r.reason="pipe_failed";return r;}
    const pid_t pid=fork();
    if(pid==0) {
        if(createGroup)setpgid(0,0);
        if(dup2(out[1],STDOUT_FILENO)<0 || dup2(err[1],STDERR_FILENO)<0)_exit(126);
        close(out[0]);close(out[1]);close(err[0]);close(err[1]);close(gated[0]);
        ChildGateRecord record{0,-1};
        const bool permitted=childGate(gate,record);
        const ssize_t written=write(gated[1],&record,sizeof(record));close(gated[1]);
        if(!permitted || written!=static_cast<ssize_t>(sizeof(record)))_exit(125);
        execve(argv[0],argv.data(),environ);_exit(127);
    }
    close(out[1]);close(err[1]);close(gated[1]);
    if(pid<0){close(out[0]);close(err[0]);close(gated[0]);r.reason="fork_failed";return r;}
    const bool ownGroup=createGroup && (setpgid(pid,pid)==0 || getpgid(pid)==pid);
    bool setupFailed=false;
    for(int fd:{out[0],err[0],gated[0]})
        if(fcntl(fd,F_SETFL,O_NONBLOCK)<0)setupFailed=true;
    bool eof[3]={false,false,false};bool done=false,cancel=setupFailed,ownsChild=true;int status=0;
    if(setupFailed)r.reason="nonblocking_setup_failed";
    std::string gateBytes;
    while(cancel || !done || !eof[0] || !eof[1] || !eof[2]) {
        const int fds[3]={out[0],err[0],gated[0]};
        std::string* texts[3]={&r.output,&r.error,&gateBytes};
        for(unsigned i=0;i<3 && !cancel;++i) {
            if(eof[i])continue;
            for(;;) {
                char buf[8192];const ssize_t n=read(fds[i],buf,sizeof(buf));
                if(n>0) {
                    if(texts[i]->size()+static_cast<std::size_t>(n)>(i==2?sizeof(ChildGateRecord):limit)) {
                        r.reason="output_limit";cancel=true;break;
                    }
                    texts[i]->append(buf,static_cast<std::size_t>(n));
                } else {
                    if(n==0)eof[i]=true;
                    else if(errno!=EAGAIN && errno!=EINTR){r.reason="read_error";cancel=true;}
                    break;
                }
            }
        }
        // Observe exit without reaping: the retained child identity prevents
        // PID/PGID reuse while pipes may still be held by its descendants.
        if(!done) {
            siginfo_t info{};
            const int w=waitid(P_PID,static_cast<id_t>(pid),&info,WEXITED|WNOHANG|WNOWAIT);
            if(w==0 && info.si_pid==pid)done=true;
            else if(w<0 && errno!=EINTR){if(errno==ECHILD)ownsChild=false;r.reason="wait_error";cancel=true;}
        }
        if(stop.load()){r.reason="stopped";cancel=true;}
        const double now=boot();
        if(now<0){r.reason="clock_failed";cancel=true;}
        else if((now-r.pre)*1000.0>static_cast<double>(milliseconds)){r.reason="timeout";cancel=true;}
        if(cancel) {
            // Child has not been reaped, so this group cannot be a reused PID.
            if(ownsChild && ownGroup)kill(-pid,SIGKILL);
            if(ownsChild && !done)kill(pid,SIGKILL);
            break;
        }
        if(done && eof[0] && eof[1] && eof[2])break;
        pollfd pollers[3]={{eof[0]?-1:out[0],POLLIN,0},{eof[1]?-1:err[0],POLLIN,0},{eof[2]?-1:gated[0],POLLIN,0}};
        poll(pollers,3,10);
    }
    close(out[0]);close(err[0]);close(gated[0]);
    // Never block indefinitely in waitpid after cancellation. A child still in
    // uninterruptible sleep is explicitly unreaped and invalidates the trial.
    for(unsigned attempt=0;attempt<100;++attempt) {
        const auto w=waitpid(pid,&status,WNOHANG);
        if(w==pid){r.reaped=true;break;}
        if(w<0 && errno!=EINTR){r.reason="reap_error";break;}
        poll(nullptr,0,10);
    }
    if(!r.reaped)r.reason="child_unreaped";
    r.post=boot();
    r.status=r.reaped?(WIFEXITED(status)?WEXITSTATUS(status):128+(WIFSIGNALED(status)?WTERMSIG(status):0)):-1;
    if(gateBytes.size()==sizeof(ChildGateRecord)) {
        ChildGateRecord record{};std::memcpy(&record,gateBytes.data(),sizeof(record));
        r.gatePassed=record.passed==1;r.execGateAt=record.at;
    }
    r.ok=!cancel && r.reaped && done && eof[0] && eof[1] && eof[2] && r.status==0 &&
         r.gatePassed && r.output.find('\0')==std::string::npos && r.post>=r.pre;
    if(!r.ok && r.reason.empty())r.reason=!r.gatePassed?"exec_gate_rejected":"nonzero_or_incomplete";
    return r;
}
}
