// Device-local sampling avoids a 4 Hz HDC round trip. No workload allocations.
#include <algorithm>
#include <cerrno>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <dirent.h>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t alive = 1;
static void stop(int) { alive = 0; }
static double now() { timespec t{}; clock_gettime(CLOCK_BOOTTIME, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static int64_t wallMs() { timespec t{}; clock_gettime(CLOCK_REALTIME,&t); return int64_t(t.tv_sec)*1000+t.tv_nsec/1000000; }
static std::string read(const std::string& p) {
    std::ifstream f(p); std::ostringstream o; o << f.rdbuf(); return o.str();
}
static std::string trim(std::string s) {
    while (!s.empty() && (s.back()=='\n' || s.back()=='\r' || s.back()==' ')) s.pop_back();
    return s;
}
static std::string json(const std::string& s) {
    std::ostringstream o; o << '"';
    for (unsigned char c : s) {
        if(c=='"' || c=='\\') o << '\\' << c;
        else if(c=='\n') o << "\\n";
        else if(c=='\r') o << "\\r";
        else if(c=='\t') o << "\\t";
        else if(c<32) o << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(c) << std::dec;
        else o << c;
    }
    return o.str() + '"';
}
static int64_t value(const std::string& text, const std::string& key) {
    std::istringstream in(text); std::string line;
    while(std::getline(in,line)) if(line.rfind(key,0)==0) {
        std::istringstream v(line.substr(key.size())); int64_t n; if(v>>n) return n;
    }
    return -1;
}
static void sampleProcesses(std::ofstream& out, const std::map<int,std::string>& apps, double t, bool heavy) {
    std::set<std::string> pids;
    for(const auto& app: apps) {
        const std::string leaf="100/"+app.second+"_"+std::to_string(app.first);
        std::string members=read("/dev/memcg/"+leaf+"/cgroup.procs");
        if(members.empty()) members=read("/dev/cgroup/memory/default/"+leaf+"/cgroup.procs");
        std::istringstream in(members); std::string pid; while(in>>pid) pids.insert(pid);
    }
    std::ostringstream frame; bool firstMember=true;
    for(const auto& pid:pids) {
        if(pid.empty() || pid.find_first_not_of("0123456789")!=std::string::npos) continue;
        const auto base="/proc/"+pid+"/";
        const auto first=read(base+"stat"), status=read(base+"status");
        const int uid=static_cast<int>(value(status,"Uid:"));
        auto app=apps.find(uid); if(app==apps.end()) continue;
        const auto cg=read(base+"cgroup");
        const auto score=read(base+"oom_score_adj");
        const auto smaps=heavy ? read(base+"smaps_rollup") : "";
        const auto last=read(base+"stat");
        auto start=[](const std::string& s) {
            auto pos=s.rfind(')'); if(pos==std::string::npos) return std::string{};
            std::istringstream in(s.substr(pos+1)); std::string v;
            for(int i=0;i<=19;++i) if(!(in>>v)) return std::string{};
            return v;
        };
        const auto id=start(first);
        if(id.empty() || id!=start(last)) continue;
        if(!firstMember) frame << ',';
        firstMember=false; frame << json(pid+":"+id);
        out << "{\"time\":" << now() << ",\"bundle\":" << json(app->second)
            << ",\"pid\":" << pid << ",\"uid\":" << uid << ",\"start_ticks\":" << json(id)
            << ",\"stat\":" << json(last) << ",\"status\":" << json(status)
            << ",\"cgroup\":" << json(cg) << ",\"oom_score_adj\":" << json(trim(score))
            << ",\"smaps\":" << json(smaps) << "}\n";
    }
    out << "{\"time\":" << now() << ",\"sample_start\":" << t << ",\"event\":\"process_frame\",\"members\":[" << frame.str() << "]}\n";
    out.flush();
}

static bool clockMarker(const std::string& root,const std::string& kind) {
    int fd=open("/dev/kmsg",O_WRONLY|O_CLOEXEC); if(fd<0) return false;
    std::ostringstream s; s << "REALAPP_CLOCK run=" << root << " kind=" << kind
                            << " time=" << std::fixed << std::setprecision(6) << now() << "\n";
    const std::string text=s.str(); const bool ok=write(fd,text.data(),text.size())==static_cast<ssize_t>(text.size());
    close(fd);return ok;
}

static bool drainKernel(int fd,std::ofstream& out) {
    if(fd<0) return false;
    char buf[65536];ssize_t n;
    while((n=::read(fd,buf,sizeof(buf)))>0)
        out << "{\"time\":" << now() << ",\"raw\":" << json(std::string(buf,n)) << "}\n";
    bool ok=n<0 && (errno==EAGAIN || errno==EWOULDBLOCK);
    if(!ok) out << "{\"time\":" << now() << ",\"read_error\":" << errno << "}\n";
    out.flush();return ok;
}

int main(int argc,char** argv) {
    if(argc!=3) return 2;
    const std::string root=argv[1];
    if(root.rfind("/data/local/tmp/realapp_dataset_",0)!=0 || root.find("..")!=std::string::npos) return 2;
    const int ttl=std::atoi(argv[2]); if(ttl<=0 || ttl>1800) return 2;
    std::map<int,std::string> apps; std::ifstream list(root+"/apps.tsv");
    int uid; std::string bundle; while(list>>uid>>bundle) apps[uid]=bundle;
    std::ofstream sys(root+"/system.jsonl"), proc(root+"/processes.jsonl"), events(root+"/gate.jsonl"), kernel(root+"/kernel.jsonl");
    if(!sys || !proc || !events || !kernel) return 3;
    for(auto* s : {&sys,&proc,&events,&kernel}) *s << std::fixed << std::setprecision(6);
    signal(SIGTERM,stop); signal(SIGINT,stop);
    const int kfd=open("/dev/kmsg",O_RDONLY|O_NONBLOCK|O_CLOEXEC);
    bool kernelGood=kfd>=0;
    if(kfd>=0 && lseek(kfd,0,SEEK_END)<0) kernelGood=false;
    const auto boot=trim(read("/proc/sys/kernel/random/boot_id"));
    const double begin=now(), deadline=begin+ttl;
    double next=begin, nextProc=begin, nextHeavy=begin, nextClock=begin+5, last=-1;
    int consecutive=0; bool attempted=false;
    const std::string gatePath="/proc/sys/kernel/hyperhold/enable";
    events << "{\"time\":" << begin << ",\"event\":\"ready\",\"kernel_open\":" << (kernelGood?"true":"false")
           << ",\"boot_id\":" << json(boot) << ",\"run\":" << json(root) << ",\"wall_ms\":" << wallMs() << "}\n"; events.flush();
    kernelGood=clockMarker(root,"begin") && kernelGood;
    // The watchdog owns restoration. This process only samples and closes once.
    while(alive && now()<deadline && access((root+"/stop").c_str(),F_OK)!=0) {
        const double t=now();
        if(t>=nextClock) { kernelGood=clockMarker(root,"tick") && kernelGood; nextClock=t+5; }
        if(trim(read("/proc/sys/kernel/random/boot_id"))!=boot) break;
        const auto mem=read("/proc/meminfo");
        const auto available=value(mem,"MemAvailable:");
        const auto gate=trim(read(gatePath));
        const auto detail=trim(read("/sys/block/zram0/hyperhold_enable"));
        const auto io=read("/sys/block/zram0/hyperhold_io_info_file");
        const auto stage=trim(read(root+"/stage"));
        const bool armed=stage=="actions" || stage=="post";
        const bool below=available>=0 && available*1024LL*10 < 8LL*1024*1024*1024;
        if(armed && below) consecutive=(last>=0 && t-last>=0.20 && t-last<=0.50)?consecutive+1:1;
        else consecutive=0;
        last=t;
        if(consecutive>=2 && !attempted) {
            attempted=true; errno=0; ssize_t written=-1;
            // Preservation of zram is checked before and after the write.
            int err=0;
            if(detail.find("reclaim_in enable")!=std::string::npos && gate=="enable") {
                int fd=open(gatePath.c_str(),O_WRONLY|O_CLOEXEC);
                if(fd>=0) { written=write(fd,"disable\n",8); err=errno; close(fd); }
                else err=errno;
            } else err=EINVAL;
            events << "{\"time\":" << now() << ",\"event\":\"close_request\",\"written\":" << written
                   << ",\"errno\":" << err << ",\"readback\":" << json(trim(read(gatePath)))
                   << ",\"detail\":" << json(trim(read("/sys/block/zram0/hyperhold_enable"))) << "}\n";
            events.flush();
        }
        sys << "{\"time\":" << t << ",\"stage\":" << json(stage) << ",\"meminfo\":" << json(mem)
            << ",\"gate\":" << json(gate) << ",\"gate_detail\":" << json(detail)
            << ",\"swap_io\":" << json(io);
        if(t>=nextProc) {
            sys << ",\"vmstat\":" << json(read("/proc/vmstat")) << ",\"pressure\":" << json(read("/proc/pressure/memory"))
                << ",\"service_control\":" << json(read(trim(read(root+"/control_path"))));
            const bool heavy=t>=nextHeavy;
            sampleProcesses(proc,apps,t,heavy);
            nextProc=t+1.0; if(heavy) nextHeavy=t+30.0;
        }
        sys << "}\n"; sys.flush();
        kernelGood=drainKernel(kfd,kernel) && kernelGood;
        next+=0.25; double delay=next-now();
        if(delay>0) { timespec ts{static_cast<time_t>(delay),static_cast<long>((delay-static_cast<long>(delay))*1e9)}; nanosleep(&ts,nullptr); }
        else next=now();
    }
    kernelGood=clockMarker(root,"end") && kernelGood;
    kernelGood=drainKernel(kfd,kernel) && kernelGood;
    if(kfd>=0) close(kfd);
    events << "{\"time\":" << now() << ",\"event\":\"stopped\",\"wall_ms\":" << wallMs()
           << ",\"kernel_read_ok\":" << (kernelGood?"true":"false") << "}\n";
    return 0;
}
