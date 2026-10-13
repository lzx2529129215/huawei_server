#include "io.hpp"
#include "harmony_v1.hpp"
#include <atomic>
#include <condition_variable>
#include <csignal>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <sys/file.h>
#include <thread>

namespace fs = std::filesystem;
using namespace ms;
namespace {
volatile std::sig_atomic_t stopping = 0;
void onSignal(int) { stopping = 1; }
std::string num(double n) { std::ostringstream out; out<<std::fixed<<std::setprecision(3)<<n; return out.str(); }
std::string u(Bytes n) { return std::to_string(n); }
std::string cell(const std::string& s) {
    std::string v="\""; for(char c:s) { if(c=='\"') v+='\"'; v+=c; } return v+'\"';
}
class CSV {
    fs::path path_;
    std::string header_;
    Bytes limit_, size_=0;
    std::ofstream out_;
    void open() {
        size_=fs::exists(path_)?fs::file_size(path_):0;
        out_.exceptions(std::ios::badbit|std::ios::failbit);
        out_.open(path_,std::ios::app);
        if(!size_) { out_<<header_<<'\n'; out_.flush(); size_=header_.size()+1; }
    }
public:
    CSV(const fs::path& path,std::string header,Bytes limit):path_(path),header_(std::move(header)),limit_(limit) { open(); }
    void row(const std::vector<std::string>& values) {
        std::string line;
        for(auto& v:values) { if(!line.empty()) line+=','; line+=cell(v); }
        line+='\n';
        if(size_+line.size()>limit_) {
            out_.close(); auto old=path_.string()+".1";
            if(fs::exists(old)) fs::remove(old);
            fs::rename(path_,old); open();
        }
        out_<<line; out_.flush(); size_+=line.size();
    }
};
Control controlNow(const Options& o) {
    Control c;
    if(!o.control.empty()) c=parseControl(readPath(o.control));
    if(c.boost>o.maxBoost) throw std::runtime_error("control boost exceeds max_boost_mb");
    if(c.boost && c.until>add(unixSeconds(),300)) throw std::runtime_error("boost expiry too far in future (max 300s)");
    return c;
}
bool isOwnTarget(const Options& o,const std::string& path) {
    std::istringstream in(readPath(o.proc+"/self/cgroup")); std::string line;
    while(std::getline(in,line)) if(line.rfind("0::/",0)==0) {
        const auto self=line.substr(4);
        if(self.empty()) return false; // daemon in namespace root: child targets are not ancestors
        if(!safeRelative(self)) throw std::runtime_error("unresolvable self cgroup; host/root namespace required");
        return ancestor(path,self);
    }
    throw std::runtime_error("cannot determine daemon cgroup v2 membership");
}
struct Job {
    std::size_t index=0;
    FD dir;
    App app;
    Features features;
    Bytes requested=0,expectedDrop=0;
    std::string reason;
};
struct Result {
    std::size_t index=0;
    std::string name,status,message,reason;
    Bytes planned=0,written=0;
    int err=0;
    double writeMs=0,totalMs=0;
    Sample before,after;
    std::optional<System> sysBefore,sysAfter;
};
class Worker {
    Options options_;
    int root_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::optional<Job> pending_;
    std::optional<Result> completed_;
    bool stop_=false,running_=false;
    double started_=0;
    std::thread thread_;
    Result execute(Job j) {
        Result r; r.index=j.index; r.name=j.app.name; r.planned=j.requested; r.reason=j.reason;
        const double begin=monotonic();
        try {
            auto ctl=controlNow(options_);
            if(ctl.paused || ctl.protectedApps.count(j.app.name) || isOwnTarget(options_,j.app.path)) {
                r.status="cancelled_protected"; return r;
            }
            r.before=sampleAt(j.dir.get());
            if(r.before.inode!=j.features.now.inode || r.before.device!=j.features.now.device)
                throw std::runtime_error("cgroup generation changed");
            r.before.swapRoom=ancestorSwapRoom(root_,j.app.path);
            r.sysBefore=systemSample(options_.proc);
            auto block=pressureBlock(options_,*r.sysBefore,0,0);
            if(!block.empty()) { r.status="cancelled_"+block; return r; }
            // Revalidate immediately before writing; at most ONE bounded job is in flight.
            Features fresh=j.features; fresh.now=r.before;
            Bytes liveBoost=ctl.until>unixSeconds()?ctl.boost:0;
            liveBoost=std::min(liveBoost,sub(r.sysBefore->total/2,std::min(options_.reserve,r.sysBefore->total/2)));
            const Forecast recheck{sub(r.sysBefore->available,j.expectedDrop),0};
            const Bytes liveGap=deficit(options_,*r.sysBefore,recheck,liveBoost);
            auto b=donorBudget(options_,j.app,fresh,*r.sysBefore,std::min(liveGap,j.requested),j.requested,
                               static_cast<Bytes>(::sysconf(_SC_PAGESIZE)));
            const Bytes toSubmit=std::min(b.bytes,j.requested);
            if(!toSubmit) { r.status="cancelled_budget_changed"; return r; }
            std::string command=u(toSubmit);
            if(options_.swappiness>=0) command+=" swappiness="+std::to_string(options_.swappiness);
            command+='\n';
            FD file(::openat(j.dir.get(),"memory.reclaim",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
            if(file.get()<0) {
                r.err=errno; r.status="open_error"; r.message=std::strerror(r.err); r.written=0;
            } else {
                const double t=monotonic();
                r.written=toSubmit; // requested bytes submitted, NOT bytes actually freed
                // Do not blindly retry EINTR or EAGAIN: some reclaim may already have happened.
                const auto n=::write(file.get(),command.data(),command.size());
                r.err=n<0?errno:0;
                r.writeMs=(monotonic()-t)*1000;
                if(n==static_cast<ssize_t>(command.size())) r.status="ok";
                else if(n<0 && r.err==EAGAIN) r.status="partial_or_unmet";
                else if(n<0) { r.status="write_error"; r.message=std::strerror(r.err); }
                else { r.status="short_write"; r.err=EIO; }
            }
            try { r.after=sampleAt(j.dir.get()); } catch(const std::exception& e) { r.message+="; after: "+std::string(e.what()); }
            try { r.sysAfter=systemSample(options_.proc); } catch(const std::exception& e) { r.message+="; sys_after: "+std::string(e.what()); }
        } catch(const std::exception& e) { r.status="cancelled_read_error"; r.message=e.what(); }
        r.totalMs=(monotonic()-begin)*1000;
        return r;
    }
    void loop() {
        for(;;) {
            std::unique_lock<std::mutex> lock(mutex_);
            cv_.wait(lock,[&]{return stop_ || pending_.has_value();});
            if(stop_) return;
            Job job=std::move(*pending_); pending_.reset(); running_=true; started_=monotonic();
            lock.unlock();
            auto result=execute(std::move(job));
            lock.lock(); completed_=std::move(result); running_=false;
        }
    }
public:
    Worker(Options o,int root):options_(std::move(o)),root_(root),thread_([this]{loop();}) {}
    ~Worker() {
        { std::lock_guard<std::mutex> lock(mutex_); stop_=true; pending_.reset(); }
        cv_.notify_one();
        // Linux may block inside reclaim/writeback. No hard cancellation guarantee.
        if(thread_.joinable()) thread_.join();
    }
    bool busy() { std::lock_guard<std::mutex> lock(mutex_); return running_ || pending_ || completed_; }
    double runningFor() { std::lock_guard<std::mutex> lock(mutex_); return running_?monotonic()-started_:0; }
    bool submit(Job job) {
        std::lock_guard<std::mutex> lock(mutex_);
        if(running_ || pending_ || completed_ || stop_) return false;
        pending_=std::move(job); cv_.notify_one(); return true;
    }
    std::optional<Result> take() {
        std::lock_guard<std::mutex> lock(mutex_);
        auto r=std::move(completed_); completed_.reset(); return r;
    }
};
struct State {
    Sample previous;
    double firstSeen=0,lastTime=0,nextReclaim=0,refaultUntil=0,lastPenalty=-1e9,lastRecovery=0;
    double growth=0,refaultRate=0,majorRate=0,adaptive=1;
    Bytes episodeCap=0,episodeSpent=0;
    bool disabled=false;
    RollingLimit rate;
};
struct Args { std::string config,backend="linux-v2"; bool apply=false,observe=false,check=false,fixture=false; Bytes ticks=0; };
Args arguments(int argc,char** argv) {
    Args a;
    for(int i=1;i<argc;++i) {
        std::string k=argv[i];
        auto val=[&]{ if(i+1>=argc) throw std::runtime_error("missing value for "+k); return std::string(argv[++i]); };
        if(k=="--config") a.config=val();
        else if(k=="--backend") a.backend=val();
        else if(k=="--apply") a.apply=true;
        else if(k=="--observe") a.observe=true;
        else if(k=="--check") a.check=true;
        else if(k=="--fixture") a.fixture=true;
        else if(k=="--ticks") a.ticks=uintValue(val());
        else if(k=="--help") {
            std::cout<<"memshiftd 0.1.0\nUsage: memshiftd --config FILE [--backend linux-v2|harmony-v1|harmony-v1-ratio|harmony-v1-ratio-probe] [--observe|--apply] [--check] [--ticks N]\n"
                     <<"Default: observation only. --fixture allows ONLY observation against synthetic files.\n";
            std::exit(0);
        } else throw std::runtime_error("unknown option: "+k);
    }
    if(a.config.empty()) throw std::runtime_error("--config is required");
    if(a.fixture && a.apply) throw std::runtime_error("--fixture and --apply are mutually exclusive");
    if(a.observe && a.apply) throw std::runtime_error("--observe and --apply are mutually exclusive");
    return a;
}
void resultRow(CSV& log,const Result& r) {
    auto change=[](Bytes a,Bytes b){ return num(static_cast<double>(a)-static_cast<double>(b)); };
    log.row({u(unixMillis()),r.name,r.status,r.reason,u(r.planned),u(r.written),std::to_string(r.err),
             num(r.writeMs),num(r.totalMs),r.before.valid?u(r.before.current):"",r.after.valid?u(r.after.current):"",
             r.before.valid&&r.after.valid?change(r.after.current,r.before.current):"",
             r.before.valid&&r.after.valid?change(r.after.swap,r.before.swap):"",
             r.before.valid&&r.after.valid?u(delta(r.after.pgsteal,r.before.pgsteal)):"",
             r.before.hasProactive&&r.after.hasProactive?u(delta(r.after.pgstealProactive,r.before.pgstealProactive)):"",
             r.sysBefore&&r.sysAfter?change(r.sysAfter->available,r.sysBefore->available):"",
             r.sysBefore&&r.sysAfter?change(r.sysAfter->free,r.sysBefore->free):"",r.message});
}
int run(const Args& args) {
    harmony_v1::validateBackendRequest(args.backend,args.apply);
    if(args.backend!="linux-v2" && args.fixture)
        throw std::runtime_error("--fixture is supported only by the linux-v2 test backend");
    auto cfg=parseConfig(readPath(args.config)); const auto& o=cfg.options;
    if(o.dailyKnownFutureFreeOnly && args.backend!="harmony-v1-ratio")
        throw std::runtime_error("daily_known_future_free_only requires --backend harmony-v1-ratio");
    if(o.dailyRecentReserve && args.backend!="harmony-v1-ratio")
        throw std::runtime_error("daily_recent_reserve requires --backend harmony-v1-ratio");
    if(args.backend!="linux-v2")
        return harmony_v1::run(cfg,args.backend,args.check,args.apply,args.ticks,[]{return stopping!=0;});
    if(!args.fixture && o.proc!="/proc") throw std::runtime_error("proc_root must be /proc unless --fixture");
    FD root(::open(o.root.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW));
    if(root.get()<0) throw error("open cgroup root");
    struct statfs stfs{};
    if(::fstatfs(root.get(),&stfs)!=0) throw error("statfs cgroup root");
    constexpr long Cgroup2Magic=0x63677270;
    if(!args.fixture && stfs.f_type!=Cgroup2Magic) throw std::runtime_error("cgroup_root must be a real cgroup v2 mount");
    // The root is used for ancestor checks; it must not be an arbitrary subtree.
    if(!args.fixture && fs::canonical(o.root)!=fs::path("/sys/fs/cgroup"))
        throw std::runtime_error("baseline supports cgroup_root=/sys/fs/cgroup (mount root) only");
    if(args.apply && maybeAt(root.get(),"memory.max"))
        throw std::runtime_error("namespace/subtree root detected; this baseline applies only at host cgroup mount root");
    const auto initial=systemSample(o.proc);
    if(o.reserve>=initial.total)
        throw std::runtime_error("reserve_mb must be below MemTotal");
    if(o.readyFree>=initial.total) throw std::runtime_error("ready_free_mb must be below MemTotal");
    if(o.requirePSI && (!initial.mem.valid || !initial.io.valid)) throw std::runtime_error("global PSI unavailable; check kernel configuration");
    for(const auto& app:cfg.apps)
        if(!args.fixture && isOwnTarget(o,app.path)) throw std::runtime_error("daemon is inside configured target: "+app.name);
    if(!o.control.empty()) {
        try { auto c=controlNow(o); for(auto& n:c.protectedApps) {
                bool found=false; for(auto& app:cfg.apps) found=found || app.name==n;
                if(!found) throw std::runtime_error("unknown protected app: "+n);
            }
        } catch(const std::exception& e) {
            if(args.check) throw;
            std::cerr<<"Control unavailable: reclaim will pause until fixed: "<<e.what()<<'\n';
        }
    }
    if(args.check) {
        std::cout<<"mode="<<(args.apply?"apply":"observe")<<" cgroup2="<<(stfs.f_type==Cgroup2Magic)
                 <<" MemTotal_MiB="<<initial.total/MiB<<" SwapFree_MiB="<<initial.swapFree/MiB
                 <<" PSI="<<(initial.mem.valid&&initial.io.valid)<<'\n';
        int failures=0;
        for(const auto& app:cfg.apps) {
            try {
                auto fd=openBeneath(root.get(),app.path); auto s=sampleAt(fd.get());
                s.swapRoom=ancestorSwapRoom(root.get(),app.path);
                FD reclaim(::openat(fd.get(),"memory.reclaim",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
                bool writable=reclaim.get()>=0;
                std::cout<<app.name<<" current_MiB="<<s.current/MiB<<" donor="<<app.reclaim
                         <<" reclaim_writable="<<writable<<" ancestor_swap_room_MiB="
                         <<(s.swapRoom==Unlimited?"max":u(s.swapRoom/MiB))<<'\n';
                if(args.apply && app.reclaim && !writable) ++failures;
            } catch(const std::exception& e) { ++failures; std::cout<<app.name<<" unavailable: "<<e.what()<<'\n'; }
        }
        std::cout<<"No memory.reclaim writes were performed. Missing application groups can be created before starting the app.\n";
        return failures?2:0;
    }
    FD lock(::open(o.lock.c_str(),O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC,0640));
    if(lock.get()<0) throw error("open lock_file (create its parent directory first)");
    if(::flock(lock.get(),LOCK_EX|LOCK_NB)!=0) throw error("another daemon holds lock_file");
    fs::create_directories(o.logs);
    CSV systemLog(fs::path(o.logs)/"system.csv",
        "unix_ms,mode,mem_total,mem_available,mem_free,swap_free,zswap,zswapped,predicted_available,decline_bytes_s,deficit_bytes,boost_bytes,mem_some_pct,mem_full_pct,io_some_pct,pswpin,pswpout,episode,decision,target,planned_bytes",o.logMax);
    CSV appLog(fs::path(o.logs)/"apps.csv",
        "unix_ms,app,cgroup_inode,current_bytes,anon_bytes,file_bytes,inactive_anon,inactive_file,swap_bytes,pgfault,pgmajfault,refault,oom_kill,pgsteal,app_some_pct,growth_bytes_s,refault_activity_bytes_s,major_per_s,adaptive,cold_hint_bytes,budget_bytes,reason",o.logMax);
    CSV reclaimLog(fs::path(o.logs)/"reclaim.csv",
        "unix_ms,app,status,trigger,planned_bytes,submitted_bytes,errno,write_ms,total_ms,current_before,current_after,current_change,swap_change,pgsteal_delta,pgsteal_proactive_delta,mem_available_change,mem_free_change,message",o.logMax);
    CSV events(fs::path(o.logs)/"events.csv","unix_ms,level,app,message",o.logMax);
    auto event=[&](const std::string& level,const std::string& app,const std::string& message){
        events.row({u(unixMillis()),level,app,message});
        std::cerr<<level<<" "<<app<<" "<<message<<'\n';
    };
    event("INFO","daemon",std::string("mode=")+(args.apply?"APPLY":"OBSERVE")+"; baseline EWMA, not ML/WSS; targets="+u(cfg.apps.size()));
    event("WARN","daemon","host-wide memory policy; not a container/ancestor memory.max quota controller; foreground integration is external");
    if(!initial.swapTotal) event("WARN","daemon","no configured swap: anonymous cold hint disabled by available swap budget");
    std::unique_ptr<Predictor> predictor=std::make_unique<EWMAPredictor>();
    Worker worker(o,root.get());
    std::vector<State> states(cfg.apps.size());
    RollingLimit globalRate;
    bool episode=false,haveLast=false;
    System lastSys;
    std::string lastBlock;
    double lastSlowNotice=-1e9;
    Bytes tick=0;
    bool samplingFailed=false;
    const long ps=::sysconf(_SC_PAGESIZE);
    if(ps<=0) throw std::runtime_error("cannot determine page size");
    const Bytes page=static_cast<Bytes>(ps);
    auto deadline=std::chrono::steady_clock::now();
    while(!stopping && (!args.ticks || tick<args.ticks)) {
        ++tick;
        if(auto result=worker.take()) {
            resultRow(reclaimLog,*result);
            auto& state=states.at(result->index);
            const bool same=!result->before.valid || (state.previous.inode==result->before.inode && state.previous.device==result->before.device);
            if(same) {
                const double now=monotonic();
                // Cooldown measured from completion, not just submission.
                state.nextReclaim=std::max(state.nextReclaim,now+o.cooldownSec);
                bool poor=false;
                if(result->before.valid && result->after.valid) {
                    auto steal=delta(result->after.pgsteal,result->before.pgsteal);
                    poor=static_cast<double>(steal)*static_cast<double>(page)<0.1*static_cast<double>(result->written);
                }
                if(result->err || poor || result->writeMs>1000) {
                    state.nextReclaim=std::max(state.nextReclaim,now+o.backoffSec);
                    state.adaptive=std::max(0.125,state.adaptive*0.5);
                    state.lastPenalty=now;
                }
                if(result->err==EINVAL || result->err==EACCES || result->err==EPERM || result->err==EROFS || result->err==EOPNOTSUPP) {
                    state.disabled=true;
                    event("ERROR",result->name,"target disabled until restart/recreation; inspect reclaim.csv, permissions, or swappiness support");
                }
            }
        }
        System s;
        try { s=systemSample(o.proc); }
        catch(const std::exception& e) { event("ERROR","system",e.what()); samplingFailed=true; break; }
        auto f=predictor->update(s,o.horizonSec,o.maxDeclineMBps*MiB);
        double recentIO=0,recentFull=0,recentSome=0;
        if(haveLast) {
            double dt=s.time-lastSys.time;
            if(s.io.valid && lastSys.io.valid) recentIO=stallPercent(s.io.someTotal,lastSys.io.someTotal,dt);
            if(s.mem.valid && lastSys.mem.valid) {
                recentFull=stallPercent(s.mem.fullTotal,lastSys.mem.fullTotal,dt);
                recentSome=stallPercent(s.mem.someTotal,lastSys.mem.someTotal,dt);
            }
        }
        lastSys=s; haveLast=true;
        std::string block=pressureBlock(o,s,recentIO,recentFull);
        Control ctl;
        try {
            ctl=controlNow(o);
            for(auto& name:ctl.protectedApps) {
                bool known=false; for(auto& app:cfg.apps) known=known || app.name==name;
                if(!known) throw std::runtime_error("unknown protected app: "+name);
            }
            if(ctl.paused) block="manual_pause";
        } catch(const std::exception& e) { block="control_invalid"; if(lastBlock!=block) event("ERROR","control",e.what()); }
        Bytes boost=(ctl.until>unixSeconds())?ctl.boost:0;
        // Never target impossible host reserves from a mistaken launch hint.
        boost=std::min(boost,sub(s.total/2,std::min(o.reserve,s.total/2)));
        const Bytes gap=deficit(o,s,f,boost);
        if(!episode && gap>=o.minBatch) {
            episode=true;
            for(auto& state:states) { state.episodeSpent=0; state.episodeCap=0; }
            event("INFO","policy","begin reserve episode");
        } else if(episode && f.predictedAvailable>=add(add(o.reserve,boost),o.hysteresis) && s.free>=o.readyFree && !worker.busy()) {
            episode=false; event("INFO","policy","reserve episode recovered");
        }
        if(block!=lastBlock) {
            if(!block.empty()) event("WARN","policy","extra reclaim paused: "+block);
            else if(!lastBlock.empty()) event("INFO","policy","extra reclaim resumed");
            lastBlock=block;
        }
        if(worker.runningFor()>2 && s.time-lastSlowNotice>10) {
            event("WARN","worker","memory.reclaim is slow; sampler remains live; no additional jobs queued"); lastSlowNotice=s.time;
        }
        const bool workerBusy=worker.busy();
        std::optional<Job> best;
        for(std::size_t i=0;i<cfg.apps.size();++i) {
            const auto& app=cfg.apps[i]; auto& state=states[i];
            FD dir; Sample cur; std::string readError;
            try {
                dir=openBeneath(root.get(),app.path); cur=sampleAt(dir.get());
                cur.swapRoom=ancestorSwapRoom(root.get(),app.path);
                if(!args.fixture && isOwnTarget(o,app.path)) throw std::runtime_error("daemon moved inside target");
            } catch(const std::exception& e) { cur.valid=false; readError=e.what(); }
            if(!cur.valid) {
                if(state.previous.valid || tick==1 || tick%30==0) event("WARN",app.name,readError);
                state.previous={}; continue;
            }
            Sample prev=state.previous;
            bool reset=!prev.valid || prev.inode!=cur.inode || prev.device!=cur.device ||
                       (!prev.populated && cur.populated) || cur.pgfault<prev.pgfault || cur.refault<prev.refault;
            if(reset) {
                // Preserve rolling rate limit across group disappearance/recreation.
                auto rate=std::move(state.rate); state=State{}; state.rate=std::move(rate);
                state.firstSeen=s.time; state.lastRecovery=s.time; prev={};
                event("INFO",app.name,"new/restarted cgroup; startup grace enabled");
            }
            double appSome=cur.psi.some10;
            if(prev.valid && s.time>state.lastTime) {
                double dt=s.time-state.lastTime;
                double beta=1-std::exp(-dt/2);
                double growth=(static_cast<double>(cur.current)-static_cast<double>(prev.current))/dt;
                state.growth=(1-beta)*state.growth+beta*growth;
                double ref=static_cast<double>(delta(cur.refault,prev.refault))*static_cast<double>(page)/dt;
                double maj=static_cast<double>(delta(cur.major,prev.major))/dt;
                state.refaultRate=(1-beta)*state.refaultRate+beta*ref;
                state.majorRate=(1-beta)*state.majorRate+beta*maj;
                if(cur.psi.valid && prev.psi.valid) appSome=std::max(appSome,stallPercent(cur.psi.someTotal,prev.psi.someTotal,dt));
                if(ref>o.refaultGuardMBps*MiB || maj>o.majorGuardPS || appSome>=o.appSomeLimit) {
                    state.refaultUntil=s.time+o.backoffSec;
                    if(s.time-state.lastPenalty>=o.backoffSec) {
                        state.adaptive=std::max(0.125,state.adaptive*0.5); state.lastPenalty=s.time;
                        event("WARN",app.name,"activity/PSI guard; lower alpha and back off (not causal attribution)");
                    }
                } else if(s.time-state.lastPenalty>60 && s.time-state.lastRecovery>30) {
                    state.adaptive=std::min(1.0,state.adaptive+0.05); state.lastRecovery=s.time;
                }
            }
            if(episode && !state.episodeSpent)
                state.episodeCap=std::max(state.episodeCap,amount(o.episodeFraction*static_cast<double>(cur.current)));
            Features features; features.now=cur; features.prev=prev;
            features.now.psi.some10=appSome;
            features.age=s.time-state.firstSeen; features.growthBytesPS=state.growth;
            features.refaultBytesPS=state.refaultRate; features.majorPS=state.majorRate;
            features.protectedNow=ctl.protectedApps.count(app.name)!=0;
            features.adaptive=state.adaptive;
            features.episodeRoom=sub(state.episodeCap,state.episodeSpent);
            features.minuteRoom=state.rate.room(s.time,o.appPerMinute);
            auto b=donorBudget(o,app,features,s,gap,globalRate.room(s.time,o.perMinute),page);
            if(!episode || gap<o.minBatch) { b.bytes=0; b.reason="reserve_satisfied"; }
            else if(!block.empty()) { b.bytes=0; b.reason=block; }
            else if(state.disabled) { b.bytes=0; b.reason="target_disabled"; }
            else if(s.time<state.refaultUntil) { b.bytes=0; b.reason="refault_backoff"; }
            else if(s.time<state.nextReclaim) { b.bytes=0; b.reason="cooldown"; }
            else if(workerBusy) { b.bytes=0; b.reason="worker_busy"; }
            appLog.row({u(unixMillis()),app.name,u(cur.inode),u(cur.current),u(cur.anon),u(cur.file),u(cur.inactiveAnon),u(cur.inactiveFile),
                        u(cur.swap),u(cur.pgfault),u(cur.major),u(cur.refault),u(cur.oomKills),u(cur.pgsteal),num(appSome),num(state.growth),
                        num(state.refaultRate),num(state.majorRate),num(state.adaptive),u(b.coldHint),u(b.bytes),b.reason});
            state.previous=cur; state.lastTime=s.time;
            // Greedy baseline: most available low-risk bytes, then round-robin through cooldown.
            if(b.bytes && (!best || b.bytes>best->requested)) {
                Job job; job.index=i; job.dir=std::move(dir); job.app=app; job.features=features;
                job.expectedDrop=sub(s.available,f.predictedAvailable);
                job.requested=b.bytes; job.reason=boost?"launch_hint":(s.available<o.reserve?"reserve_low":
                    (f.predictedAvailable<o.reserve?"trend_prediction":"ready_free_low"));
                best=std::move(job);
            }
        }
        std::string target,decision=block.empty()?"monitor":block;
        Bytes planned=0;
        if(best && !stopping) {
            target=best->app.name; planned=best->requested; auto index=best->index;
            bool accepted=false;
            if(args.apply) { accepted=worker.submit(std::move(*best)); decision=accepted?"submitted":"worker_busy"; }
            else {
                Result simulated; simulated.name=target; simulated.planned=planned; simulated.status="observe_only"; simulated.reason=best->reason;
                resultRow(reclaimLog,simulated); accepted=true; decision="would_reclaim";
            }
            if(accepted) {
                globalRate.charge(s.time,planned); auto& state=states[index];
                state.rate.charge(s.time,planned); state.episodeSpent=add(state.episodeSpent,planned);
                state.nextReclaim=s.time+o.cooldownSec;
            }
        }
        systemLog.row({u(unixMillis()),args.apply?"apply":"observe",u(s.total),u(s.available),u(s.free),u(s.swapFree),u(s.zswap),u(s.zswapped),
                       u(f.predictedAvailable),num(f.declineBytesPS),u(gap),u(boost),num(std::max(s.mem.some10,recentSome)),
                       num(std::max(s.mem.full10,recentFull)),num(std::max(s.io.some10,recentIO)),u(s.pswpin),u(s.pswpout),
                       episode?"1":"0",decision,target,u(planned)});
        deadline+=std::chrono::milliseconds(o.intervalMs);
        if(deadline<std::chrono::steady_clock::now()) deadline=std::chrono::steady_clock::now();
        while(!stopping && std::chrono::steady_clock::now()<deadline) {
            auto remain=deadline-std::chrono::steady_clock::now();
            std::this_thread::sleep_for(std::min(remain,std::chrono::duration_cast<decltype(remain)>(std::chrono::milliseconds(100))));
        }
    }
    event("INFO","daemon","stopping; waiting for any in-flight reclaim (no hard syscall timeout)");
    // Drain in-flight result so a finite run does not lose its final reclaim record.
    while(worker.busy()) {
        if(auto r=worker.take()) { resultRow(reclaimLog,*r); break; }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    return samplingFailed?2:0;
}
} // namespace
int main(int argc,char** argv) {
    try {
        std::signal(SIGINT,onSignal); std::signal(SIGTERM,onSignal);
        return run(arguments(argc,argv));
    } catch(const std::exception& e) { std::cerr<<"FATAL: "<<e.what()<<'\n'; return 1; }
}
