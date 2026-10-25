#include "io.hpp"
#include "linux_scope.hpp"
#include "harmony_v1.hpp"
#include "watermark.hpp"
#include <atomic>
#include <condition_variable>
#include <csignal>
#include <filesystem>
#include <functional>
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
            r.sysBefore=machineSample(options_,root_);
            auto block=pressureBlock(options_,*r.sysBefore,0,0);
            if(!block.empty()) { r.status="cancelled_"+block; return r; }
            // Revalidate immediately before writing; at most ONE bounded job is in flight.
            Features fresh=j.features; fresh.now=r.before;
            Bytes liveBoost=ctl.until>unixSeconds()?ctl.boost:0;
            liveBoost=std::min(liveBoost,boostCap(options_,r.sysBefore->total));
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
            try { r.sysAfter=machineSample(options_,root_); } catch(const std::exception& e) { r.message+="; sys_after: "+std::string(e.what()); }
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
// ---------------- reclaim_interface=high (linux-v2 scope mode) ----------------
// Writes text to one cgroup control file; returns 0 or errno. Retries EINTR,
// so use it only for writes that are safe to repeat (restoring "max").
int writeControl(int dir,const char* name,const std::string& text) {
    FD f(::openat(dir,name,O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
    if(f.get()<0) return errno;
    for(;;) {
        const auto n=::write(f.get(),text.data(),text.size());
        if(n==static_cast<ssize_t>(text.size())) return 0;
        if(n<0 && errno==EINTR) continue;
        return n<0?errno:EIO;
    }
}
Bytes residentOf(const Sample& s) { return add(sub(s.anon,s.unevictable),s.file); }
struct PulseRow {
    std::string status,message;
    Bytes requested=0,target=0,before=0,after=0,swapBefore=0,swapAfter=0;
    double writeMs=0;
    bool restored=true;
    KV statBefore,statAfter;
};
struct PulseJob {
    std::size_t index=0;
    std::string name,path,reason;
    FD dir;
    Bytes want=0;        // net drop of memory.current asked for
    Bytes floorBytes=0;  // anon (minus locked) + file must stay at or above this
    Bytes minPulse=0;    // smallest pulse worth writing; 0 = min_batch_mb (tmo/hint_rate: tmo_min_step_mb)
};
struct PulseResult {
    std::size_t index=0;
    std::string name,path,reason,status,message;
    Bytes want=0,freed=0,requested=0,currentBefore=0,currentAfter=0,swapBefore=0,swapAfter=0;
    int err=0;
    double totalMs=0,writeMs=0;
    std::vector<PulseRow> pulses;
};
// One background thread does all memory.high writes (the zswapd stand-in), so
// the sampler never blocks in reclaim. A job is a series of pulses on one app:
// high = current - N, then high = max again, until the job's net drop is
// reached, the app hits its floor, the kernel makes no progress, or the main
// loop cancels a pulse that runs too long (by writing max itself).
class PulseWorker {
    Options o_;
    Bytes page_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::optional<PulseJob> pending_;
    std::optional<PulseResult> completed_;
    bool stop_=false,running_=false;
    std::atomic<bool> cancel_{false},slowCancel_{false};
    std::atomic<bool> brake_{false};   // set by the main loop while the brake is on
    std::atomic<double> pulseStart_{0};
    double nextPulse_=0;   // monotonic time the next pulse may start (pulse_rate_mb_s)
    double cancelSentFor_=0;
    std::string pulsePath_;
    std::thread thread_;
    static Bytes currentOf(int dir) { return uintValue(trim(readAt(dir,"memory.current"))); }
    static Bytes swapOf(int dir) { auto t=maybeAt(dir,"memory.swap.current"); return t?uintValue(trim(*t)):0; }
    PulseRow pulse(PulseJob& j,Bytes n,KV stat) {
        PulseRow p; p.requested=n; p.statBefore=std::move(stat);
        p.before=currentOf(j.dir.get()); p.swapBefore=swapOf(j.dir.get());
        p.target=sub(p.before,n); p.target-=p.target%page_;
        FD f(::openat(j.dir.get(),"memory.high",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
        if(f.get()<0) { p.status="open_error"; p.message=std::strerror(errno); return p; }
        {
            std::lock_guard<std::mutex> lock(mutex_); pulsePath_=j.path;
        }
        int restoreErr=0;
        {
            // Restore max no matter what (also on an exception): a low
            // memory.high left behind throttles the app.
            struct Guard {
                int dir; int& err;
                // ENOENT/ENODEV: the cgroup is gone (app exited); nothing is left low.
                ~Guard() { err=writeControl(dir,"memory.high","max\n"); if(err==ENOENT || err==ENODEV) err=0; }
            } guard{j.dir.get(),restoreErr};
            const std::string text=u(p.target)+"\n";
            const double t=monotonic();
            pulseStart_=t;
            // The write blocks while the kernel reclaims in this thread. Never retried.
            const auto w=::write(f.get(),text.data(),text.size());
            const int e=w<0?errno:0;
            p.writeMs=(monotonic()-t)*1000;
            pulseStart_=0;
            if(w==static_cast<ssize_t>(text.size())) p.status="ok";
            else if(w<0) { p.status="write_error"; p.message=std::strerror(e); }
            else p.status="short_write";
        }
        if(restoreErr) { p.restored=false; p.message+="restore failed: "+std::string(std::strerror(restoreErr)); }
        return p;
    }
    // Fresh control check before a pulse: why the app may not be pulsed now, if
    // anything. An unreadable control file stops the job too.
    std::optional<std::string> blocked(const PulseJob& j) {
        try {
            auto ctl=controlNow(o_);
            if(ctl.paused) return std::string("paused");
            if(ctl.protectedApps.count(j.name)) return std::string("app protected");
            if(isOwnTarget(o_,j.path)) return std::string("own target");
        } catch(const std::exception& e) { return "control: "+std::string(e.what()); }
        return std::nullopt;
    }
    PulseResult execute(PulseJob j) {
        PulseResult r; r.index=j.index; r.name=j.name; r.path=j.path; r.reason=j.reason; r.want=j.want;
        const double begin=monotonic();
        try {
            auto ctl=controlNow(o_);
            if(ctl.paused || ctl.protectedApps.count(j.name) || isOwnTarget(o_,j.path)) { r.status="cancelled_protected"; return r; }
            r.currentBefore=currentOf(j.dir.get()); r.swapBefore=swapOf(j.dir.get());
            r.currentAfter=r.currentBefore; r.swapAfter=r.swapBefore;
            r.status="ok";
            const Bytes minPulse=j.minPulse?j.minPulse:o_.minBatch;
            std::optional<Bytes> lastScopeSome;   // brake_pulse_ms: scope some total before the previous pulse
            while(r.freed<j.want) {
                if(cancel_ || brake_) {
                    r.status=slowCancel_?"cancelled_slow":(brake_?"cancelled_brake":(r.pulses.empty()?"cancelled":"stopped"));
                    break;
                }
                auto stat=counters(readAt(j.dir.get(),"memory.stat"));
                const Bytes resident=add(sub(get(stat,"anon"),get(stat,"unevictable")),get(stat,"file"));
                Bytes n=std::min(pulseLimit(o_,sub(resident,j.floorBytes),page_),sub(j.want,r.freed));
                n-=n%page_;
                if(n<minPulse) { r.status=r.pulses.empty()?"at_floor":"floor_reached"; break; }
                // Pace reclaim like a background thread: while a pulse runs, a donor that faults
                // a page in is made to reclaim too, so bursts of pulses add up to visible stall.
                if(o_.pulseRateMBps>0)
                    while(!cancel_ && !brake_ && monotonic()<nextPulse_)
                        std::this_thread::sleep_for(std::chrono::milliseconds(10));
                if(cancel_ || brake_) continue;
                // The runner protects an app right before its peak, possibly while a job on it
                // runs: re-read the control file before every pulse.
                if(auto why=blocked(j)) { r.status="cancelled_protected"; r.message=*why; break; }
                if(o_.brakePulseMs>0 && !o_.scope.empty()) {
                    // Fresh scope stall right before the pulse: the previous pulse of this job
                    // stalled the apps (refaults / memory.high throttling) -> stop the series.
                    std::optional<Bytes> some;
                    try {
                        const auto psi=parsePSI(readPath(o_.root+"/"+o_.scope+"/memory.pressure"));
                        if(psi.valid) some=psi.someTotal;
                    } catch(const std::exception&) {}
                    if(some && lastScopeSome && sub(*some,*lastScopeSome)>=amount(o_.brakePulseMs*1000.0)) {
                        r.status="cancelled_brake_pulse";
                        r.message="scope stall +"+std::to_string(sub(*some,*lastScopeSome)/1000)+" ms since the previous pulse";
                        break;
                    }
                    if(some) lastScopeSome=some;
                }
                if(o_.pulseRateMBps>0)
                    nextPulse_=std::max(nextPulse_,monotonic())+static_cast<double>(n)/MiB/o_.pulseRateMBps;
                auto p=pulse(j,n,std::move(stat));
                // A donor killed or exiting during the pulse frees its memory by exiting, not by
                // reclaim: count nothing for that pulse and end the job.
                bool gone=false;
                try {
                    p.after=currentOf(j.dir.get()); p.swapAfter=swapOf(j.dir.get());
                    p.statAfter=counters(readAt(j.dir.get(),"memory.stat"));
                    gone=required(counters(readAt(j.dir.get(),"cgroup.events")),"populated")==0;
                } catch(const std::exception& e) { gone=true; p.message+="after: "+std::string(e.what()); }
                if(gone) { p.after=p.before; p.swapAfter=p.swapBefore; p.statAfter=p.statBefore; p.status="target_gone"; }
                else { r.currentAfter=p.after; r.swapAfter=p.swapAfter; }
                const Bytes got=sub(p.before,p.after);
                r.requested=add(r.requested,p.requested); r.writeMs+=p.writeMs;
                r.freed=add(r.freed,got);
                const bool failed=p.status!="ok";
                const bool restored=p.restored;
                if(failed) { r.status=p.status; r.message=p.message; if(!gone) r.err=EIO; }
                r.pulses.push_back(std::move(p));
                if(failed || !restored) { if(!restored) r.status="restore_failed"; break; }
                if(got<minPulse/2) { r.status="no_progress"; break; }
            }
        } catch(const std::exception& e) {
            // Reading the donor failed between pulses (most likely it was killed); what was
            // measured before stays, currentAfter is the last good reading.
            r.status=r.pulses.empty()?"cancelled_read_error":"target_gone"; r.message=e.what();
        }
        r.totalMs=(monotonic()-begin)*1000;
        return r;
    }
    void loop() {
        for(;;) {
            std::unique_lock<std::mutex> lock(mutex_);
            cv_.wait(lock,[&]{return stop_ || pending_.has_value();});
            if(stop_) return;
            PulseJob job=std::move(*pending_); pending_.reset(); running_=true; cancel_=false; slowCancel_=false;
            lock.unlock();
            auto result=execute(std::move(job));
            lock.lock(); completed_=std::move(result); running_=false;
        }
    }
public:
    PulseWorker(Options o,Bytes page):o_(std::move(o)),page_(page),thread_([this]{loop();}) {}
    ~PulseWorker() {
        { std::lock_guard<std::mutex> lock(mutex_); stop_=true; pending_.reset(); }
        cancel_=true; cv_.notify_one();
        if(thread_.joinable()) thread_.join();
    }
    bool busy() { std::lock_guard<std::mutex> lock(mutex_); return running_ || pending_ || completed_; }
    bool submit(PulseJob job) {
        std::lock_guard<std::mutex> lock(mutex_);
        if(running_ || pending_ || completed_ || stop_) return false;
        pending_=std::move(job); cv_.notify_one(); return true;
    }
    std::optional<PulseResult> take() {
        std::lock_guard<std::mutex> lock(mutex_);
        auto r=std::move(completed_); completed_.reset(); return r;
    }
    // Ends the job after the pulse in flight. slow: that pulse ran past pulse_timeout_ms.
    void cancel(bool slow=false) { if(slow) slowCancel_=true; cancel_=true; }
    // While on, every job stops before its next pulse (status cancelled_brake).
    void setBrake(bool on) { brake_=on; }
    // Path of the pulse in flight if it has run longer than limit seconds;
    // reported once per pulse.
    std::optional<std::string> slowPulse(double limit) {
        const double start=pulseStart_;
        if(!start || monotonic()-start<limit || start==cancelSentFor_) return {};
        cancelSentFor_=start;
        std::lock_guard<std::mutex> lock(mutex_); return pulsePath_;
    }
};
struct HighState {
    Sample previous;
    double firstSeen=0,backoffUntil=0;
    Bytes reference=0;   // largest anon (minus locked) + file seen
    bool disabled=false;
    TmoApp tmo;          // TMO loop (tmo / hint_mode=rate)
    Bytes maxCurrent=0;  // largest memory.current seen for this cgroup (rate mode need)
    Bytes seenFor=0;     // boost_until_unix of the hint during which it was last seen alive
};
using EventFn=std::function<void(const std::string&,const std::string&,const std::string&)>;
int runHigh(const Args& args,const Configuration& cfg,int root,const EventFn& event) {
    const auto& o=cfg.options;
    const fs::path logs(o.logs);
    const bool rateMode=o.hintMode=="rate";
    const bool tmoTrack=o.tmo || rateMode;      // per-app PSI loop (rate donors use its backoff)
    const bool extra=tmoTrack || o.brakeSomeMs>0;
    const double tmoInterval=o.tmoIntervalMs/1000.0, tickSec=o.intervalMs/1000.0;
    // New columns only with the new options, so existing configs log exactly as before.
    CSV systemLog(logs/"system.csv",
        "unix_ms,mode,mem_total,scope_free,mem_available,swap_free,zswap,zswapped,mem_some_pct,mem_full_pct,io_some_pct,pswpin,pswpout,"
        "hinted,boost_bytes,remaining_peak_bytes,wm_low,wm_high,reclaiming,decision,target,planned_bytes"+
        std::string(extra?",job_reason,brake,brake_window_ms,peak_at_unix,rate_need_bytes,rate_left_s,tmo_due":""),o.logMax);
    CSV appLog(logs/"apps.csv",
        "unix_ms,app,cgroup_inode,current_bytes,anon_bytes,unevictable_bytes,file_bytes,swap_bytes,zswap_bytes,zswapped_bytes,"
        "refault,oom_kill,resident_bytes,reference_bytes,floor_bytes,room_bytes,high_events,psi_some_total_us,psi_full_total_us,reason"+
        std::string(tmoTrack?",tmo_psi_us,tmo_state":""),o.logMax);
    std::optional<CSV> brakeLog;
    if(o.brakeSomeMs>0) brakeLog.emplace(logs/"brake.csv","unix_ms,state,window_ms",o.logMax);
    // One row per pulse job, same columns as the memory.reclaim mode (run_round.py reads them).
    CSV reclaimLog(logs/"reclaim.csv",
        "unix_ms,app,status,trigger,planned_bytes,submitted_bytes,errno,write_ms,total_ms,current_before,current_after,current_change,swap_change,pgsteal_delta,pgsteal_proactive_delta,mem_available_change,mem_free_change,message",o.logMax);
    CSV pulseLog(logs/"pulses.csv",
        "unix_ms,app,job,pulse,status,requested_bytes,target_high,current_before,current_after,net_drop,write_ms,restored,"
        "anon_before,anon_after,file_before,file_after,zswap_before,zswap_after,zswapped_before,zswapped_after,swap_before,swap_after,"
        "zswpout_delta,zswpwb_delta,pswpout_delta,message",o.logMax);
    CSV quotaLog(logs/"quota.csv",
        "unix_ms,app,action,quota_bytes,disk_share_bytes,need_bytes,zswap_capacity_bytes,disk_capacity_bytes,deficit_bytes,disk_anon_bytes,unplaced_bytes,status",o.logMax);
    const long ps=::sysconf(_SC_PAGESIZE);
    if(ps<=0) throw std::runtime_error("cannot determine page size");
    const Bytes page=static_cast<Bytes>(ps);
    event("INFO","daemon","reclaim_interface=high: watermarks "+u(o.wmLow/MiB)+"/"+u(o.wmHigh/MiB)+" MiB, hint reserve "+
          u(o.hintReserve/MiB)+" MiB, floor "+num(o.floorRatio)+", pulse "+u(o.pulse/MiB)+" MiB, rate limit "+
          (o.pulseRateMBps>0?num(o.pulseRateMBps)+" MiB/s":std::string("none"))+", zswap_quota="+(o.zswapQuota?"true":"false"));
    if(extra)
        event("INFO","daemon","tmo="+std::string(o.tmo?"true":"false")+" (every "+u(o.tmoIntervalMs)+" ms, ratio "+num(o.tmoRatio)+
              ", min step "+u(o.tmoMinStep/MiB)+" MiB, PSI target "+num(o.tmoPsiTargetUs)+" us, backoff "+num(o.tmoBackoffSec)+
              " s), hint_mode="+o.hintMode+" (stop "+num(o.hintStopSec)+" s before the peak), brake "+
              (o.brakeSomeMs>0?num(o.brakeSomeMs)+"/"+num(o.brakeResumeMs)+" ms some per 1 s, resume after "+num(o.brakeResumeSec)+" s":std::string("off")));
    PulseWorker worker(o,page);
    std::optional<Brake> brake;
    if(o.brakeSomeMs>0) brake.emplace(o);
    bool brakePsiWarned=false;
    std::string inflight;             // reason of the job the worker holds ("" = none)
    Bytes rateWarnedFor=0;            // boost_until_unix of a rate hint reported without peak_at_unix
    double lastRateAt=0;              // monotonic time of the last accepted rate top-up job (0 = inactive)
    std::vector<HighState> states(cfg.apps.size());
    std::map<std::string,std::size_t> index;
    for(std::size_t i=0;i<cfg.apps.size();++i) index[cfg.apps[i].name]=i;
    std::set<std::string> quotaSet;   // apps whose memory.zswap.max this run changed
    Bytes plannedFor=0;               // boost_until_unix of the hint the quota plan belongs to
    bool reclaiming=false,samplingFailed=false;
    std::string lastBlock;
    Bytes tick=0,job=0;
    auto restoreQuotas=[&](const std::string& why){
        for(const auto& name:quotaSet) {
            std::string status="ok";
            try { auto dir=openBeneath(root,cfg.apps[index.at(name)].path);
                  if(int e=writeControl(dir.get(),"memory.zswap.max","max\n")) status=std::strerror(e); }
            catch(const std::exception& e) { status=e.what(); }
            quotaLog.row({u(unixMillis()),name,"restore","max","","","","","","","",status+" ("+why+")"});
        }
        quotaSet.clear();
    };
    auto logResult=[&](const PulseResult& r){
        ++job;
        int k=0;
        for(const auto& p:r.pulses) {
            auto d=[&](const char* key){ return u(delta(get(p.statAfter,key),get(p.statBefore,key))); };
            pulseLog.row({u(unixMillis()),r.name,u(job),std::to_string(++k),p.status,u(p.requested),u(p.target),u(p.before),u(p.after),
                          num(static_cast<double>(p.before)-static_cast<double>(p.after)),num(p.writeMs),p.restored?"1":"0",
                          u(get(p.statBefore,"anon")),u(get(p.statAfter,"anon")),u(get(p.statBefore,"file")),u(get(p.statAfter,"file")),
                          u(get(p.statBefore,"zswap")),u(get(p.statAfter,"zswap")),u(get(p.statBefore,"zswapped")),u(get(p.statAfter,"zswapped")),
                          u(p.swapBefore),u(p.swapAfter),d("zswpout"),d("zswpwb"),d("pswpout"),p.message});
        }
        const bool any=!r.pulses.empty();
        // A job stopped after pulses that freed memory counts as done (exact status in message).
        static const std::set<std::string> success{"ok","floor_reached","no_progress","stopped","cancelled_slow","target_gone",
                                                   "cancelled_brake","cancelled_brake_pulse","cancelled_protected"};
        reclaimLog.row({u(unixMillis()),r.name,any&&r.freed&&success.count(r.status)?"ok":r.status,r.reason,u(r.want),u(r.requested),std::to_string(r.err),
                        num(r.writeMs),num(r.totalMs),any?u(r.currentBefore):"",any?u(r.currentAfter):"",
                        any?num(static_cast<double>(r.currentAfter)-static_cast<double>(r.currentBefore)):"",
                        any?num(static_cast<double>(r.swapAfter)-static_cast<double>(r.swapBefore)):"","","","","",
                        r.status+(r.message.empty()?"":": "+r.message)});
    };
    auto deadline=std::chrono::steady_clock::now();
    while(!stopping && (!args.ticks || tick<args.ticks)) {
        ++tick;
        if(auto r=worker.take()) {
            logResult(*r);
            inflight.clear();
            auto& st=states.at(r->index);
            if(r->status=="no_progress" || r->status=="cancelled_slow" || r->status=="cancelled_brake_pulse") st.backoffUntil=monotonic()+o.backoffSec;
            if(r->status=="open_error" || r->status=="write_error" || r->status=="restore_failed") {
                st.disabled=true;
                event("ERROR",r->name,"high pulses disabled for this app: "+r->status+" "+r->message);
            }
        }
        if(auto path=worker.slowPulse(o.pulseTimeoutMs/1000.0)) {
            try { auto dir=openBeneath(root,*path); writeControl(dir.get(),"memory.high","max\n"); } catch(const std::exception&) {}
            worker.cancel(true);
            event("WARN","worker","memory.high pulse exceeded pulse_timeout_ms; restored max to cancel it");
        }
        System s;
        try { s=machineSample(o,root); }
        catch(const std::exception& e) { event("ERROR","system",e.what()); samplingFailed=true; break; }
        // Brake on the scope's own stall; the worker checks the flag before every pulse.
        bool brakeOn=false;
        if(brake) {
            if(s.mem.valid) {
                const bool was=brake->on();
                brakeOn=brake->update(s.time,s.mem.someTotal);
                if(brakeOn!=was) {
                    brakeLog->row({u(unixMillis()),brakeOn?"on":"off",num(brake->windowMs())});
                    event(brakeOn?"WARN":"INFO","brake",std::string(brakeOn?"on":"off")+": scope PSI some "+num(brake->windowMs())+" ms over the last 1 s");
                }
            } else {
                brakeOn=brake->on();
                if(!brakePsiWarned) { brakePsiWarned=true; event("WARN","brake","scope memory.pressure unreadable; brake state frozen"); }
            }
            worker.setBrake(brakeOn);
        }
        Control ctl; std::string block;
        try {
            ctl=controlNow(o);
            for(const auto& n:ctl.protectedApps) if(!index.count(n)) throw std::runtime_error("unknown protected app: "+n);
            for(const auto& [n,b]:ctl.peakTargets) if(!index.count(n)) throw std::runtime_error("unknown peak target app: "+n);
            if(ctl.paused) block="manual_pause";
        } catch(const std::exception& e) {
            ctl=Control{}; block="control_invalid";
            if(lastBlock!=block) event("ERROR","control",e.what());
        }
        if(o.requirePSI && (!s.mem.valid || !s.io.valid)) block="psi_unavailable";
        if(block!=lastBlock) {
            if(!block.empty()) event("WARN","policy","pulses paused: "+block);
            else if(!lastBlock.empty()) event("INFO","policy","pulses resumed");
            lastBlock=block;
        }
        const bool hinted=ctl.until>unixSeconds() && ctl.boost>0;
        // Per-app samples.
        std::vector<Sample> cur(cfg.apps.size());
        std::vector<FD> dirs(cfg.apps.size());
        std::map<std::string,Bytes> currents;
        for(std::size_t i=0;i<cfg.apps.size();++i) {
            const auto& app=cfg.apps[i]; auto& st=states[i];
            try {
                dirs[i]=openBeneath(root,app.path); cur[i]=sampleAt(dirs[i].get());
                if(!args.fixture && isOwnTarget(o,app.path)) throw std::runtime_error("daemon moved inside target");
            } catch(const std::exception& e) {
                if(st.previous.valid || tick==1 || tick%60==0) event("WARN",app.name,e.what());
                cur[i].valid=false; st.previous={}; continue;
            }
            const auto& prev=st.previous;
            if(!prev.valid || prev.inode!=cur[i].inode || prev.device!=cur[i].device) {
                st=HighState{}; st.firstSeen=s.time;
                // A fresh cgroup starts with max; an old one may hold a limit from a crashed run.
                if(args.apply && app.reclaim) {
                    writeControl(dirs[i].get(),"memory.high","max\n");
                    if(o.zswapQuota) writeControl(dirs[i].get(),"memory.zswap.max","max\n");
                }
                event("INFO",app.name,"new/restarted cgroup");
            }
            st.reference=std::max(st.reference,residentOf(cur[i]));
            st.maxCurrent=std::max(st.maxCurrent,cur[i].current);
            if(hinted) st.seenFor=ctl.until;
            st.previous=cur[i];
            currents[app.name]=cur[i].current;
            if(tmoTrack) {
                if(cur[i].psi.valid) st.tmo.psi.add(s.time,cur[i].psi.someTotal,tmoInterval);
                else st.tmo.psi.reset();
            }
        }
        const Bytes remaining=!hinted?0:(ctl.peakTargets.empty()?ctl.boost:remainingPeak(ctl.peakTargets,currents));
        auto wmHint=watermarks(o,hinted,remaining);
        wmHint.high=std::min(wmHint.high,s.total); wmHint.low=std::min(wmHint.low,wmHint.high);
        // Rate mode: the hint never raises the watermarks; the low-watermark fallback keeps the normal pair.
        auto wm=wmHint;
        if(rateMode) { wm=watermarks(o,false,0); wm.high=std::min(wm.high,s.total); wm.low=std::min(wm.low,wm.high); }
        // Rate top-up: active from the hint until hint_stop_sec before peak_at_unix.
        const double rateLeftSec=hinted && ctl.peakAt?rateLeft(o,static_cast<double>(ctl.peakAt),static_cast<double>(unixMillis())/1000.0):0;
        const bool rateActive=rateMode && hinted && ctl.peakAt>0 && rateLeftSec>0;
        // Rate mode: the need is computed from the whole hint (apps not started yet count in full)
        // and per-app high-water marks, so it equals the case's deficit at the hint and falls 1:1
        // with reclaim (remainingPeak() counts only live apps at their live size).
        Bytes rateRemaining=0;
        if(rateActive) {
            if(ctl.peakTargets.empty()) rateRemaining=ctl.boost;
            else {
                std::vector<HintApp> hv;
                for(const auto& [n,b]:ctl.peakTargets) {
                    const auto& st=states.at(index.at(n));
                    const bool seen=st.seenFor==ctl.until;
                    hv.push_back({b,seen,seen && cur[index.at(n)].valid,st.maxCurrent});
                }
                rateRemaining=hintRemaining(hv);
            }
        }
        const Bytes need=rateActive?rateNeedHint(o,rateRemaining,s.free):0;
        if(!rateActive || !need) lastRateAt=0;
        if(rateMode && hinted && !ctl.peakAt && rateWarnedFor!=ctl.until) {
            rateWarnedFor=ctl.until; event("WARN","policy","hint_mode=rate: hint without peak_at_unix, no rate top-up");
        }
        if(!reclaiming && s.free<wm.low) { reclaiming=true; event("INFO","policy","free "+u(s.free/MiB)+" MiB below low watermark "+u(wm.low/MiB)+" MiB; reclaim until "+u(wm.high/MiB)+" MiB"); }
        else if(reclaiming && s.free>=wm.high) { reclaiming=false; event("INFO","policy","free "+u(s.free/MiB)+" MiB reached high watermark "+u(wm.high/MiB)+" MiB"); }
        // The worker re-reads only the donor and the control file between pulses; stop its job
        // here when pulses are blocked or braked, a watermark job once free is back at the high
        // watermark, a rate job once the top-up has stopped. TMO jobs are one small step.
        if(worker.busy()) {
            const bool watermarkJob=inflight!="tmo" && inflight!="hint_rate";
            if(!block.empty() || brakeOn || (watermarkJob && !reclaiming) || (inflight=="hint_rate" && !need)) worker.cancel();
        }
        // Donor view: background apps that may give memory now.
        auto eligible=[&](std::size_t i,std::string& why)->bool{
            const auto& app=cfg.apps[i]; const auto& st=states[i];
            if(!app.reclaim || ctl.protectedApps.count(app.name)) { why="protected"; return false; }
            if(!cur[i].valid || !cur[i].populated) { why="not_running"; return false; }
            if(s.time-st.firstSeen<o.warmupSec) { why="startup_grace"; return false; }
            if(st.disabled) { why="target_disabled"; return false; }
            if(monotonic()<st.backoffUntil) { why="backoff"; return false; }
            return true;
        };
        // zswap quota plan, once per hint, before any of that hint's reclaim.
        if(o.zswapQuota && hinted && ctl.until!=plannedFor && block.empty()) {
            plannedFor=ctl.until;
            if(!quotaSet.empty()) restoreQuotas("new hint");
            std::vector<DonorView> donors;
            for(std::size_t i=0;i<cfg.apps.size();++i) {
                std::string why;
                if(!eligible(i,why) && why!="backoff") continue;
                donors.push_back({cfg.apps[i].name,cfg.apps[i].priority,sub(cur[i].anon,cur[i].unevictable),cur[i].file,states[i].reference});
            }
            const auto plan=planZswapQuotas(o,donors,sub(wmHint.high,s.free),page);
            quotaLog.row({u(unixMillis()),"*","plan","","",u(plan.need),u(plan.zswapCapacity),u(plan.diskCapacity),u(plan.deficit),
                          u(plan.diskAnon),u(plan.unplaced),plan.quota.empty()?"no quota needed":u(plan.quota.size())+" apps"});
            for(const auto& [name,q]:plan.quota) {
                std::string status=args.apply?"ok":"observe_only";
                if(args.apply) {
                    try { auto dir=openBeneath(root,cfg.apps[index.at(name)].path);
                          if(int e=writeControl(dir.get(),"memory.zswap.max",u(q)+"\n")) status=std::strerror(e); else quotaSet.insert(name); }
                    catch(const std::exception& e) { status=e.what(); }
                }
                quotaLog.row({u(unixMillis()),name,args.apply?"set":"would_set",u(q),u(plan.diskShare.at(name)),"","","","","","",status});
            }
            event("INFO","policy","zswap quota plan: need "+u(plan.need/MiB)+" MiB, all-zswap capacity "+u(plan.zswapCapacity/MiB)+
                  " MiB, anon to disk "+u(plan.diskAnon/MiB)+" MiB in "+u(plan.quota.size())+" apps");
        }
        if(!hinted && !quotaSet.empty()) restoreQuotas("hint ended");
        // Most expendable eligible donor with room above its floor; TMO evaluation per app.
        std::optional<std::size_t> best;
        std::vector<Bytes> floors(cfg.apps.size(),0),rooms(cfg.apps.size(),0);
        std::vector<RateDonor> rateDonors;
        std::size_t tmoDue=0;
        for(std::size_t i=0;i<cfg.apps.size();++i) {
            const auto& app=cfg.apps[i]; auto& st=states[i];
            std::string why="eligible";
            Bytes floorBytes=0,room=0;
            if(cur[i].valid) { floorBytes=amount(o.floorRatio*static_cast<double>(st.reference)); room=sub(residentOf(cur[i]),floorBytes); }
            floors[i]=floorBytes; rooms[i]=room;
            const bool ok=eligible(i,why);
            if(ok) {
                if(pulseLimit(o,room,page)<o.minBatch) why="at_floor";
                else if(!best || app.priority>cfg.apps[*best].priority) best=i;
            }
            std::string tmoState;
            Bytes tmoPsi=0;
            if(tmoTrack && cur[i].valid) {
                // An app without readable PSI gets no feedback, so no TMO or rate pulses.
                const bool psiOk=cur[i].psi.valid;
                tmoEvaluate(o,st.tmo,s.time,ok && psiOk,room,page);
                const bool backoff=s.time<st.tmo.backoffUntil;
                tmoPsi=st.tmo.psi.over(tmoInterval);
                if(st.tmo.dueSince>=0) ++tmoDue;
                rateDonors.push_back({i,app.priority,room,ok && psiOk,backoff,tmoPsi});
                tmoState=!psiOk?"no_psi":(backoff?"backoff":(st.tmo.dueSince>=0?"due":"idle"));
            }
            if(cur[i].valid) {
                std::vector<std::string> row{u(unixMillis()),app.name,u(cur[i].inode),u(cur[i].current),u(cur[i].anon),u(cur[i].unevictable),u(cur[i].file),
                            u(cur[i].swap),u(cur[i].zswap),u(cur[i].zswapped),u(cur[i].refault),u(cur[i].oomKills),u(residentOf(cur[i])),
                            u(st.reference),u(floorBytes),u(room),u(cur[i].highEvents),u(cur[i].psi.someTotal),u(cur[i].psi.fullTotal),why};
                if(tmoTrack) { row.push_back(u(tmoPsi)); row.push_back(tmoState); }
                appLog.row(row);
            }
        }
        std::string decision=block.empty()?(brakeOn?"brake":(reclaiming?"reclaiming":"monitor")):block,target,jobReason;
        Bytes planned=0;
        // One job (observe: one would-pulse row) on app i; true when accepted.
        auto launch=[&](std::size_t i,Bytes want,Bytes minPulse,const std::string& reason)->bool{
            target=cfg.apps[i].name; planned=want; jobReason=reason;
            if(!args.apply) {
                reclaimLog.row({u(unixMillis()),target,"observe_only",reason,u(want),"0","0","","","","","","","","","","",""});
                decision="would_pulse"; return true;
            }
            PulseJob j; j.index=i; j.name=target; j.path=cfg.apps[i].path; j.reason=reason; j.dir=std::move(dirs[i]);
            j.want=want; j.floorBytes=floors[i]; j.minPulse=minPulse;
            const bool accepted=worker.submit(std::move(j));
            decision=accepted?"submitted":"worker_busy";
            if(accepted) inflight=reason;
            return accepted;
        };
        // At most one job per tick (the worker runs one at a time): the low-watermark
        // fallback first, then the rate top-up while active, then the TMO app due longest.
        if(block.empty() && !brakeOn && !stopping) {
            bool tried=false;
            if(reclaiming && best) {
                Bytes want=sub(wm.high,s.free); want-=want%page;
                if(want>=o.minBatch) { tried=true; launch(*best,want,0,hinted && !rateMode?"peak_hint":"low_watermark"); }
            }
            // While a rate top-up is active it goes before TMO, sized by the time since its last
            // accepted job, so TMO ticks and a busy worker cannot starve it.
            if(!tried && need) {
                if(auto d=pickRateDonor(o,rateDonors,page)) {
                    const double now=monotonic();
                    if(!lastRateAt) lastRateAt=now-tickSec;
                    Bytes want=std::min(rateWant(o,need,rateLeftSec,std::max(tickSec,now-lastRateAt),page),rooms[*d]);
                    want-=want%page;
                    if(want>=o.tmoMinStep) {
                        tried=true;
                        if(launch(*d,want,o.tmoMinStep,"hint_rate")) lastRateAt=now;
                    }
                }
            }
            if(!tried && o.tmo) {
                std::optional<std::size_t> due;
                for(std::size_t i=0;i<cfg.apps.size();++i) {
                    const auto& t=states[i].tmo;
                    if(t.dueSince>=0 && tmoHasRoom(o,rooms[i],page) && (!due || t.dueSince<states[*due].tmo.dueSince)) due=i;
                }
                if(due) {
                    tried=true;
                    if(launch(*due,states[*due].tmo.step,o.tmoMinStep,"tmo")) tmoSubmitted(o,states[*due].tmo,s.time);
                }
            }
        }
        std::vector<std::string> sysRow{u(unixMillis()),args.apply?"apply":"observe",u(s.total),u(s.free),u(s.available),u(s.swapFree),u(s.zswap),u(s.zswapped),
                       num(s.mem.some10),num(s.mem.full10),num(s.io.some10),u(s.pswpin),u(s.pswpout),hinted?"1":"0",u(hinted?ctl.boost:0),
                       u(remaining),u(wm.low),u(wm.high),reclaiming?"1":"0",decision,target,u(planned)};
        if(extra)
            sysRow.insert(sysRow.end(),{jobReason,brakeOn?"1":"0",brake?num(brake->windowMs()):"",u(ctl.peakAt),u(need),
                                        rateActive?num(rateLeftSec):"",u(tmoDue)});
        systemLog.row(sysRow);
        deadline+=std::chrono::milliseconds(o.intervalMs);
        if(deadline<std::chrono::steady_clock::now()) deadline=std::chrono::steady_clock::now();
        while(!stopping && std::chrono::steady_clock::now()<deadline) {
            auto remain=deadline-std::chrono::steady_clock::now();
            std::this_thread::sleep_for(std::min(remain,std::chrono::duration_cast<decltype(remain)>(std::chrono::milliseconds(50))));
            if(auto path=worker.slowPulse(o.pulseTimeoutMs/1000.0)) {
                try { auto dir=openBeneath(root,*path); writeControl(dir.get(),"memory.high","max\n"); } catch(const std::exception&) {}
                worker.cancel(true);
                event("WARN","worker","memory.high pulse exceeded pulse_timeout_ms; restored max to cancel it");
            }
        }
    }
    event("INFO","daemon","stopping; waiting for the pulse in flight (it restores memory.high=max)");
    worker.cancel();
    while(worker.busy()) {
        if(auto r=worker.take()) { logResult(*r); break; }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    // No more pulses: close an open brake interval so brake.csv durations add up.
    if(brake && brake->on()) {
        brakeLog->row({u(unixMillis()),"off",num(brake->windowMs())});
        event("INFO","brake","off: daemon stopping");
    }
    if(!quotaSet.empty()) restoreQuotas("daemon stopping");
    return samplingFailed?2:0;
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
    if(args.backend!="linux-v2" && !o.scope.empty())
        throw std::runtime_error("scope_cgroup is supported only by the linux-v2 backend");
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
    validateScopeTargets(o,cfg.apps);
    if(!o.scope.empty() && !args.fixture && isOwnTarget(o,o.scope))
        throw std::runtime_error("daemon is inside scope_cgroup");
    const auto initial=machineSample(o,root.get());
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
                 <<" PSI="<<(initial.mem.valid&&initial.io.valid)
                 <<" scope="<<(o.scope.empty()?"host":o.scope)<<'\n';
        int failures=0;
        for(const auto& app:cfg.apps) {
            try {
                auto fd=openBeneath(root.get(),app.path); auto s=sampleAt(fd.get());
                s.swapRoom=ancestorSwapRoom(root.get(),app.path);
                FD reclaim(::openat(fd.get(),o.reclaimInterface=="high"?"memory.high":"memory.reclaim",O_WRONLY|O_CLOEXEC|O_NOFOLLOW));
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
    CSV events(fs::path(o.logs)/"events.csv","unix_ms,level,app,message",o.logMax);
    auto event=[&](const std::string& level,const std::string& app,const std::string& message){
        events.row({u(unixMillis()),level,app,message});
        std::cerr<<level<<" "<<app<<" "<<message<<'\n';
    };
    if(o.reclaimInterface=="high") {
        event("INFO","daemon",std::string("mode=")+(args.apply?"APPLY":"OBSERVE")+"; watermark + memory.high pulses; targets="+u(cfg.apps.size()));
        event("WARN","daemon","scope memory policy: machine="+o.scope+" memory.max="+u(initial.total)+"; host meminfo not used");
        return runHigh(args,cfg,root.get(),event);
    }
    CSV systemLog(fs::path(o.logs)/"system.csv",
        "unix_ms,mode,mem_total,mem_available,mem_free,swap_free,zswap,zswapped,predicted_available,decline_bytes_s,deficit_bytes,boost_bytes,mem_some_pct,mem_full_pct,io_some_pct,pswpin,pswpout,episode,decision,target,planned_bytes",o.logMax);
    CSV appLog(fs::path(o.logs)/"apps.csv",
        "unix_ms,app,cgroup_inode,current_bytes,anon_bytes,file_bytes,inactive_anon,inactive_file,swap_bytes,pgfault,pgmajfault,refault,oom_kill,pgsteal,app_some_pct,growth_bytes_s,refault_activity_bytes_s,major_per_s,adaptive,cold_hint_bytes,budget_bytes,reason",o.logMax);
    CSV reclaimLog(fs::path(o.logs)/"reclaim.csv",
        "unix_ms,app,status,trigger,planned_bytes,submitted_bytes,errno,write_ms,total_ms,current_before,current_after,current_change,swap_change,pgsteal_delta,pgsteal_proactive_delta,mem_available_change,mem_free_change,message",o.logMax);
    event("INFO","daemon",std::string("mode=")+(args.apply?"APPLY":"OBSERVE")+"; baseline EWMA, not ML/WSS; targets="+u(cfg.apps.size()));
    if(o.scope.empty())
        event("WARN","daemon","host-wide memory policy; not a container/ancestor memory.max quota controller; foreground integration is external");
    else
        event("WARN","daemon","scope memory policy: machine="+o.scope+" memory.max="+u(initial.total)+"; host meminfo not used for the gap; foreground integration is external");
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
        try { s=machineSample(o,root.get()); }
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
        boost=std::min(boost,boostCap(o,s.total));
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
