#include "snapshot_wire.hpp"
#include "uid_preflight.hpp"
#include "read_result.hpp"
#include "controller_timing.hpp"
#include "stage3_context.hpp"
#include "async_ledger.hpp"
#include "ledger_format.hpp"
#include "retry_scheduler.hpp"
#include "retry_aa.hpp"
#include "role_policy.hpp"
#include "focus_sequence.hpp"
#include <future>
#include <fstream>
#include <iostream>
#include <list>
#include <algorithm>
#include <thread>
namespace dr=device_retry;
namespace {
std::atomic<bool> stopped{false};
void signalStop(int){stopped=true;}
using Ledger=async_ledger::Ledger;
std::string real(double value){std::ostringstream out;out<<std::setprecision(17)<<value;return out.str();}
std::string attemptId(const std::string& hash,unsigned slot,unsigned target,unsigned targetCount) {
    std::ostringstream out;out<<"s4_"<<hash.substr(0,24)<<"_c"<<std::setw(4)<<std::setfill('0')<<(slot*targetCount+target+1);return out.str();
}

}
int main(int argc,char** argv) {
    try {
        if(argc==7 && std::string(argv[1])=="--focus-proof") {
            signal(SIGINT,signalStop);signal(SIGTERM,signalStop);
            const std::string path=argv[2];std::string bytes;
            if(!dr::boundedRead(path,16384,false,bytes))return 3;
            const auto plan=dr::parsePlan(bytes);std::uint64_t index=0;
            if(!dr::number(argv[3],index) || index>=plan.targets.size() || argv[4]!=plan.caseId || !plan.fixedFocusSequence)return 3;
            const auto proof=dr::localFocusIdentity(plan,static_cast<unsigned>(index),path,argv[6],bytes,argv[5],stopped);
            std::cout<<dr::encodeFocusIdentity(proof);std::cout.flush();return std::cout?0:5;
        }
        if(argc==7 && std::string(argv[1])=="--snapshot") {
            std::string bytes;if(!dr::boundedRead(argv[2],16384,false,bytes))return 3;
            const auto plan=dr::parsePlan(bytes);std::uint64_t index=0;
            const std::string idx=argv[3];
            if(idx=="0")index=0;else if(!dr::number(idx,index))return 3;
            if(index>=plan.targets.size())return 3;
            const auto result=dr::localSnapshot(plan,static_cast<unsigned>(index),argv[4],argv[6],bytes,argv[5],stopped);
            std::cout<<dr::encodeSnapshot(result);std::cout.flush();return std::cout?0:5;
        }
        if(argc==3 && std::string(argv[1])=="--uid-preflight-only") {
            signal(SIGINT,signalStop);signal(SIGTERM,signalStop);
            const std::string path=argv[2];std::string bytes;
            if(!dr::boundedRead(path,16384,false,bytes))throw std::runtime_error("plan_read_failed");
            const auto plan=dr::parsePlan(bytes);const double start=dr::boot();
            dr::nativeUidPreflight(plan,path,bytes,path+".uid_verified",stopped);
            const double finish=dr::boot();std::string raw,boot;
            if(!std::isfinite(finish)||finish<0||finish>=plan.launch||!dr::readBoot(boot)||boot!=plan.bootId||
                !dr::boundedRead(path,16384,false,raw)||raw!=bytes||!dr::readUidBinding(path+".uid_verified",bytes))
                throw std::runtime_error("uid_preflight_before_wait_late_or_changed");
            std::cout<<"UID_PREFLIGHT_ONLY targets="<<plan.targets.size()<<" started="<<std::setprecision(17)<<start
                <<" completed="<<finish<<" elapsed="<<(finish-start)<<" AA_REQUESTS=0\n";
            std::cout.flush();return std::cout?0:5;
        }
        if(argc!=5 || std::string(argv[1])!="--plan" || std::string(argv[3])!="--output")return 2;
        signal(SIGINT,signalStop);signal(SIGTERM,signalStop);
        const std::string planPath=argv[2],output=argv[4];
        std::string bytes;if(!dr::boundedRead(planPath,16384,false,bytes))throw std::runtime_error("plan_read_failed");
        const auto plan=dr::parsePlan(bytes);
        const auto hashed=dr::command({"/bin/sha256sum",planPath},stopped,1000,4096);
        if(!hashed.ok || hashed.output.size()<64 || hashed.output.substr(0,64).find_first_not_of("0123456789abcdef")!=std::string::npos)throw std::runtime_error("plan_hash_failed");
        const std::string hash=hashed.output.substr(0,64);
        std::string recheck;if(!dr::boundedRead(planPath,16384,false,recheck) || recheck!=bytes)throw std::runtime_error("plan_changed_during_hash");
        const int claim=open((planPath+".claimed").c_str(),O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);
        if(claim<0)throw std::runtime_error("plan_claim_failed_or_replayed");
        const std::string claimBytes=hash+"\n";
        const auto claimed=write(claim,claimBytes.data(),claimBytes.size());
        const int synced=fsync(claim);const int closed=close(claim);
        if(claimed!=static_cast<ssize_t>(claimBytes.size()) || synced || closed)throw std::runtime_error("plan_claim_write_failed");
        std::string self;char link[4096];const auto size=readlink("/proc/self/exe",link,sizeof(link));
        if(size<=0 || size==static_cast<ssize_t>(sizeof(link)))throw std::runtime_error("self_exe_unknown");
        self.assign(link,static_cast<std::size_t>(size));
        std::string boot;if(!dr::readBoot(boot) || boot!=plan.bootId || dr::boot()<0 || dr::boot()>=plan.launch)throw std::runtime_error("registration_boot_or_T");
        // One native package/UID verification per target, before the fixed T.
        const double uidPreflightStarted=dr::boot();
        const std::string bindingPath=planPath+".uid_verified";
        dr::nativeUidPreflight(plan,planPath,bytes,bindingPath,stopped);
        async_ledger::Writer writer(128,32*1024*1024,[]{stopped=true;});
        Ledger events(writer,output+"/events.tsv"),requests(writer,output+"/requests.tsv"),decisions(writer,output+"/decisions.tsv"),reads(writer,output+"/reads.tsv"),focus(writer,output+"/focus.tsv");
        std::string selfStat;if(!dr::boundedRead("/proc/self/stat",65536,false,selfStat))throw std::runtime_error("identity_stat_unknown");
        std::istringstream statFields(selfStat.substr(selfStat.rfind(')')+1));std::string tick;
        for(unsigned i=0;i<20;++i)if(!(statFields>>tick))throw std::runtime_error("identity_stat_short");
        std::uint64_t ticks=0;if(!dr::number(tick,ticks))throw std::runtime_error("identity_ticks_unknown");
        Ledger identity(writer,output+"/identity.tsv");identity.row(plan.bootId+"\t"+std::to_string(getpid())+"\t"+std::to_string(ticks)+"\t"+self+"\t"+hash);
        events.row("native_uid_binding_verified\ttargets="+std::to_string(plan.targets.size())+"\tchecked_at="+real(dr::boot())+"\tplan_sha256="+hash);
        events.row(std::string(plan.fixedBackgroundRoles?"device_retry_controller_roles_v1":"device_retry_controller_v1")+
            "\tclock_domain=CLOCK_BOOTTIME\tplan_sha256="+hash+"\tT="+real(plan.launch)+
            (plan.fixedBackgroundRoles?"\tretry_targets="+std::to_string(std::count_if(plan.targets.begin(),plan.targets.end(),[](const auto& x){return x.retryTarget;}))+"\tmonitor_donors="+std::to_string(std::count_if(plan.targets.begin(),plan.targets.end(),[](const auto& x){return !x.retryTarget;}))+"\tpeak_fill_workers="+std::to_string(plan.fixedBurst4?4:1):"")+ 
            (plan.fixedFocusSequence?"\tfocus_sequence=fixed_offsets_5_10_15_20_25_30":""));
        if(!plan.fixedFocusSequence)focus.row("focus_sequence_disabled\tplan_header_not_focus_variant");
        dr::Scheduler scheduler;
        for(unsigned n=0;n<plan.targets.size();++n) {
            const auto& target=plan.targets[n];
            scheduler.bindPlannedIdentity(n,target.uid,target.pid,target.start);
        }
        std::array<std::string,16> current;
        current.fill(plan.caseId);
        const auto unchanged=[&](){controller_timing::Scope measure(0,bytes.size());std::string now;return dr::boundedRead(planPath,16384,false,now) && now==bytes && dr::readBoot(boot) && boot==plan.bootId;};
        const double uidPreflightReady=dr::boot();
        if(!std::isfinite(uidPreflightReady)||uidPreflightReady<0||uidPreflightReady>=plan.launch||!unchanged()||!dr::readUidBinding(bindingPath,bytes))throw std::runtime_error("uid_preflight_initialization_late_or_changed");
        events.row("native_uid_preflight_ready\tstarted="+real(uidPreflightStarted)+"\tready="+real(uidPreflightReady)+"\telapsed="+real(uidPreflightReady-uidPreflightStarted));
        const double beforeWait=dr::boot();
        if(!std::isfinite(beforeWait)||beforeWait<0||beforeWait>=plan.launch)throw std::runtime_error("uid_preflight_before_wait_late");
        while(dr::boot()<plan.launch && !stopped.load()) {
            if(!unchanged())throw std::runtime_error("registration_changed_before_T");
            poll(nullptr,0,5);
        }
        if(stopped.load())throw std::runtime_error("stopped_before_initial");
        // Legacy plans promote all registered targets. Closed role plans promote only their
        // twelve target rows; donors stay daily-ready and remain monitored.
        std::array<std::future<dr::Command>,16> initial;
        for(unsigned n=0;n<plan.targets.size();++n) {
            if(!dr::maySubmitInitial(plan,n)) {
                requests.row(std::string(dr::initialDisposition(plan,n))+"\t"+std::to_string(n)+"\t"+plan.caseId+"\t"+real(dr::boot()));
                continue;
            }
            controller_timing::Scope dispatchMeasure(4,0);
            requests.row("initial_pending\t"+std::to_string(n)+"\t"+plan.caseId+"\t"+real(dr::boot()));
            if(!writer.healthy())throw std::runtime_error("async_ledger_invalid_before_initial_spawn");
            initial[n]=std::async(std::launch::async,[&,n]{
                const auto& target=plan.targets[n];
                if(!dr::maySubmitInitial(plan,n))throw std::runtime_error("role_forbids_initial_promotion");
                const auto checked=dr::command({self,"--snapshot",planPath,std::to_string(n),plan.caseId,"/bin/ps",bindingPath},stopped,8000,1100000);
                if(!checked.ok){dr::Command c;c.reason="initial_snapshot_"+checked.reason;return c;}
                const auto sample=dr::decodeSnapshot(checked.output,plan,n,plan.caseId);
                if(!sample.known || sample.authority.flags.absent ||
                   !sample.authority.flags.identityMatches || !sample.authority.flags.singlePidAndMember ||
                   sample.journal.pid!=target.pid || sample.journal.start!=target.start || sample.journal.uid!=target.uid ||
                   !retry_core::fresh(dr::boot(),sample.authority.readStart)) {
                    dr::Command c;c.reason="initial_identity_unknown_or_stale";return c;
                }
                dr::ExecGate gate{true,plan.launch,std::min(plan.launch+30.0,sample.authority.readStart+2.5),plan.bootId,planPath,bytes};
                return dr::command(dr::initialArgs("/bin/aa",target.bundle,plan.caseId,
                    plan.fixedBurst4 && target.retryTarget ? 4u : 1u),stopped,15000,1048576,gate);
            });
        }
        struct FocusRead { bool ok=false,inventoryKnown=false,absent=false,generationDifferent=false;std::string reason;double start=-1,end=-1;std::uint64_t pid=0,ticks=0,uid=0; };
        std::atomic<bool> focusIntegrityInvalid{false};
        std::atomic<bool> focusProofUnknown{false};
        std::thread focusThread;
        if(plan.fixedFocusSequence) {
            events.row("focus_sequence_begin\tclock_domain=CLOCK_BOOTTIME\tbase_T="+real(plan.launch)+
                       "\toffsets_s=5,10,15,20,25,30\ttarget_rotation=a14,a15,a16");
            focusThread=std::thread([&] {try {
                const auto readIdentity=[&](unsigned index) {
                    FocusRead out;
                    const auto& target=plan.targets.at(index);
                    const auto command=dr::command({self,"--focus-proof",planPath,std::to_string(index),plan.caseId,"/bin/ps",bindingPath},stopped,3000,4096);
                    out.start=command.pre;out.end=command.post;
                    if(!command.ok){out.reason="identity_snapshot_"+command.reason;return out;}
                    try {
                        std::istringstream fields(command.output);std::string magic,k,a,g,p,s,u,begin,end,reason;
                        if(!(fields>>magic>>k>>a>>g>>p>>s>>u>>begin>>end>>reason) || magic!="device_retry_focus_identity_v1")throw std::runtime_error("focus_proof_wire");
                        out.inventoryKnown=k=="1";out.absent=a=="1";out.generationDifferent=g=="1";
                        out.pid=std::stoull(p);out.ticks=std::stoull(s);out.uid=std::stoull(u);
                        out.start=std::stod(begin);out.end=std::stod(end);out.reason=reason;
                        if(!out.inventoryKnown)return out;
                        out.ok=!out.absent && !out.generationDifferent && out.pid==target.pid && out.ticks==target.start && out.uid==target.uid &&
                            std::isfinite(out.start) && std::isfinite(out.end) && out.end>=out.start && out.end-out.start<=2.5;
                        if(out.ok)out.reason="same_planned_pid_start_uid_memcg";
                        return out;
                    } catch(const std::exception& e) {out.reason=std::string("identity_decode_")+e.what();return out;}
                };
                const auto identityFields=[](const FocusRead& x) {
                    return "\tread_start="+real(x.start)+"\tread_end="+real(x.end)+"\tpid="+std::to_string(x.pid)+
                           "\tstart_ticks="+std::to_string(x.ticks)+"\tuid="+std::to_string(x.uid)+
                           "\tidentity_reason="+dr::hex(x.reason);
                };
                std::array<bool,3> knownAbsent{};
                std::array<bool,3> targetStopped{};
                for(unsigned seq=0;seq<dr::focus_sequence::offsetsSeconds.size();++seq) {
                    const unsigned index=dr::focus_sequence::targetIndex(seq);
                    const auto& target=plan.targets[index];
                    const unsigned targetOrdinal=index-dr::focus_sequence::firstTargetIndex;
                    const double scheduled=dr::focus_sequence::scheduledAt(plan.launch,seq);
                    while(!stopped.load() && dr::boot()>=0 && dr::boot()<scheduled)poll(nullptr,0,2);
                    const double wake=dr::boot();
                    if(stopped.load() || wake<0) {
                        focus.row("focus\t"+std::to_string(seq)+"\t"+std::to_string(index)+"\t"+target.bundle+
                                  "\t"+real(scheduled)+"\t"+real(wake)+"\tstopped_before_schedule\t0\t-1\t-1");
                        continue;
                    }
                    if(knownAbsent[targetOrdinal] || targetStopped[targetOrdinal]) {
                        focus.row("focus\t"+std::to_string(seq)+"\t"+std::to_string(index)+"\t"+target.bundle+
                                  "\t"+real(scheduled)+"\t"+real(wake)+"\t"+
                                  (knownAbsent[targetOrdinal]?"prior_known_absent_skip":"prior_identity_unknown_skip")+"\t0\t-1\t-1");
                        continue;
                    }
                    if(wake>scheduled+2.5) {
                        focus.row("focus\t"+std::to_string(seq)+"\t"+std::to_string(index)+"\t"+target.bundle+
                                  "\t"+real(scheduled)+"\t"+real(wake)+"\tlate_before_identity\t0\t-1\t-1\t\t");
                        continue;
                    }
                    const auto before=readIdentity(index);
                    if(!before.ok) {
                        focus.row("focus\t"+std::to_string(seq)+"\t"+std::to_string(index)+"\t"+target.bundle+
                                  "\t"+real(scheduled)+"\t"+real(wake)+"\tprecheck_skip\t0\t-1\t-1"+identityFields(before));
                        if(before.absent)knownAbsent[targetOrdinal]=true;
                        if(before.generationDifferent){focusProofUnknown=true;targetStopped[targetOrdinal]=true;focus.row("focus_sequence_note\tsequence="+std::to_string(seq)+"\treason=pre_action_new_generation_observed;pid="+std::to_string(before.pid)+";start_ticks="+std::to_string(before.ticks));continue;}
                        if(!before.inventoryKnown){focusProofUnknown=true;targetStopped[targetOrdinal]=true;}
                        continue; // Never start/focus a replacement generation.
                    }
                    const double now=dr::boot();
                    if(now<scheduled || now>scheduled+2.5) {
                        focus.row("focus\t"+std::to_string(seq)+"\t"+std::to_string(index)+"\t"+target.bundle+
                                  "\t"+real(scheduled)+"\t"+real(now)+"\tprecheck_late_skip\t0\t-1\t-1"+identityFields(before));
                        continue;
                    }
                    dr::ExecGate gate{true,scheduled,scheduled+2.5,plan.bootId,planPath,bytes};
                    const auto call=dr::command(dr::focus_sequence::args("/bin/aa",target.bundle,plan.caseId),stopped,4000,262144,gate);
                    if(!call.gatePassed) {
                        focus.row("focus\t"+std::to_string(seq)+"\t"+std::to_string(index)+"\t"+target.bundle+
                                  "\t"+real(scheduled)+"\t"+real(call.execGateAt)+"\texec_gate_skip\t0\t"+
                                  std::to_string(call.status)+"\t"+real(call.pre)+"\t"+real(call.post)+"\t"+dr::hex(call.reason)+
                                  identityFields(before)+"\t"+dr::hex(call.output)+"\t"+dr::hex(call.error));
                        continue;
                    }
                    const auto after=readIdentity(index);
                    const bool sameGeneration=after.ok && after.pid==before.pid && after.ticks==before.ticks && after.uid==before.uid;
                    focus.row("focus\t"+std::to_string(seq)+"\t"+std::to_string(index)+"\t"+target.bundle+
                              "\t"+real(scheduled)+"\t"+real(call.execGateAt)+"\t"+(call.ok?"aa_return_ok":"aa_return_error")+
                              "\t1\t"+std::to_string(call.status)+"\t"+real(call.pre)+"\t"+real(call.post)+
                              identityFields(before)+"\tpost_"+dr::hex(after.reason)+"\tpost_pid="+std::to_string(after.pid)+
                              "\tpost_start_ticks="+std::to_string(after.ticks)+"\tpost_uid="+std::to_string(after.uid)+
                              "\tsame_generation="+std::to_string(sameGeneration)+"\tstdout_hex="+dr::hex(call.output)+
                              "\tstderr_hex="+dr::hex(call.error));
                    if(!sameGeneration) {
                        if(after.absent) {
                            knownAbsent[targetOrdinal]=true;
                            focus.row("focus_sequence_note\tsequence="+std::to_string(seq)+"\treason=post_action_known_absent");
                        } else if(after.generationDifferent) {
                            focusProofUnknown=true;targetStopped[targetOrdinal]=true;
                            focus.row("focus_sequence_note\tsequence="+std::to_string(seq)+"\treason=post_action_new_generation_observed;pid="+std::to_string(after.pid)+";start_ticks="+std::to_string(after.ticks));
                        } else {
                            focusProofUnknown=true;targetStopped[targetOrdinal]=true;
                            focus.row("focus_sequence_note\tsequence="+std::to_string(seq)+"\treason=post_action_identity_unknown");
                        }
                    }
                }
                focus.row(std::string("focus_sequence_end\tintegrity=")+(focusIntegrityInvalid.load()?"invalid":"preserved")+
                          "\tidentity_proof="+(focusProofUnknown.load()?"unknown":"complete")+"\tboottime="+real(dr::boot()));
            } catch(const std::exception& e) {
                focusIntegrityInvalid=true;focus.row("focus_sequence_exception\treason="+dr::hex(e.what()));
            } catch(...) {
                focusIntegrityInvalid=true;focus.row("focus_sequence_exception\treason=unknown");
            }});
        }
        struct FocusThreadJoiner { std::thread& thread;~FocusThreadJoiner(){if(thread.joinable())thread.join();} } focusThreadJoiner{focusThread};
        for(unsigned n=0;n<plan.targets.size();++n) {
            if(!dr::maySubmitInitial(plan,n))continue;
            controller_timing::Scope resultMeasure(3,0);
            auto owned=std::make_shared<dr::Command>(initial[n].get());const auto& c=*owned;
            {controller_timing::Scope measure(2,c.output.size()+c.error.size());
            requests.deferred(4096+3*(c.output.size()+c.error.size()),[owned,n,caseId=plan.caseId,T=plan.launch]{return ledger_format::initial(*owned,n,caseId,T);});
            }
            if(!c.gatePassed || c.status==125 || c.status==126 || c.status==127 || !c.reaped){scheduler.invalidate();continue;}
            scheduler.initialSubmission(c.execGateAt);scheduler.target(n).refusal=dr::refusal(c);
            if(c.reason=="timeout" || c.reason=="child_unreaped" || c.reason=="clock_failed")scheduler.invalidate();
        }
        if(scheduler.origin()<0)throw std::runtime_error("no_initial_submission");
        events.row("initial_complete\torigin="+real(scheduler.origin()));
        struct ReadJob {unsigned target;std::uint64_t generation;std::string attempt;std::future<dr::Command> future;};
        struct LaunchResult {dr::Command call,proof;bool presubmit=false;std::string reason;};
        struct LaunchJob {unsigned target,slot;std::uint64_t generation;std::string attempt;std::future<LaunchResult> future;};
        std::list<ReadJob> readJobs;std::list<LaunchJob> launchJobs;
        unsigned previousSlot=0,readCursor=0;
        dr::ReadDispatchPacer readPacer;
        const double origin=scheduler.origin();
        while((dr::boot()<origin+330.0 && !stopped.load() && !scheduler.invalid()) || !readJobs.empty() || !launchJobs.empty()) {
            controller_timing::Scope iterationMeasure(7,0);
            const double now=dr::boot();
            if(now<0 || !writer.healthy() || !unchanged())scheduler.invalidate();
            const bool canSubmit=now>=0 && now<origin+330.0 && !stopped.load() && !scheduler.invalid();
            for(auto it=readJobs.begin();it!=readJobs.end();) {
                controller_timing::Scope futureMeasure(3,0);
                stage3_context::Scope context(0,it->target,it->generation);
                if(it->future.wait_for(std::chrono::milliseconds(0))!=std::future_status::ready){++it;continue;}
                context.step(1);
                auto owned=std::make_shared<dr::Command>(it->future.get());const auto& call=*owned;
                const auto read=dr::consumeReadResult(call,it->generation,plan.bootId,[&](){
                    controller_timing::Scope measure(1,call.output.size());
                    return dr::decodeSnapshot(call.output,plan,it->target,it->attempt);
                },[&](const std::string& reason){
                    reads.row("unknown\t"+std::to_string(it->target)+"\t"+dr::hex(reason));
                },[&](unsigned phase){context.step(phase,call.output.size());});
                context.step(3,call.output.size()+call.error.size());
                {controller_timing::Scope measure(2,call.output.size()+call.error.size());
                reads.deferred(4096+3*(call.output.size()+call.error.size()),[owned,n=it->target,g=it->generation,attempt=it->attempt]{return ledger_format::snapshot(*owned,n,g,attempt);});
                }
                context.step(4);
                if(call.reason=="child_unreaped")scheduler.invalidate();
                scheduler.acceptRead(it->target,read,dr::boot());
                context.step(5);it=readJobs.erase(it);context.step(6);
            }
            for(auto it=launchJobs.begin();it!=launchJobs.end();) {
                controller_timing::Scope futureMeasure(3,0);
                stage3_context::Scope context(1,it->target,it->generation);
                if(it->future.wait_for(std::chrono::milliseconds(0))!=std::future_status::ready){++it;continue;}
                context.step(1);
                auto owned=std::make_shared<LaunchResult>(it->future.get());const auto& result=*owned;const auto& c=result.call;
                context.step(2,c.output.size()+result.proof.output.size());
                bool submitted=false;
                if(result.presubmit && c.gatePassed && c.status!=125 && c.status!=126 && c.status!=127 && c.reaped)
                    submitted=scheduler.actualSubmission(it->slot,it->target,it->generation,c.execGateAt,true);
                if(submitted){current[it->target]=it->attempt;scheduler.target(it->target).refusal=dr::refusal(c);}
                else {scheduler.target(it->target).launchInFlight=false;scheduler.target(it->target).retryReadAfter=dr::boot()+1.0;}
                context.step(3,c.output.size()+c.error.size()+result.proof.output.size());
                {controller_timing::Scope measure(2,c.output.size()+c.error.size()+result.proof.output.size());
                requests.deferred(4096+3*(c.output.size()+c.error.size()+result.proof.output.size()),[owned,slot=it->slot,n=it->target,attempt=it->attempt,submitted]{return ledger_format::retry(owned->call,owned->proof,owned->reason,slot,n,attempt,submitted);});
                }
                context.step(4);
                if(c.reason=="timeout" || c.reason=="child_unreaped" || c.status==127 || c.reason=="clock_failed")scheduler.invalidate();
                context.step(5);it=launchJobs.erase(it);context.step(6);
            }
            const auto slot=now>=origin?static_cast<unsigned>((now-origin)/30.0):0;
            {controller_timing::Scope schedulerMeasure(5,0);
            if(canSubmit && slot>=1 && slot<=10) {
                if(slot!=previousSlot){events.row("slot_begin\t"+std::to_string(slot)+"\t"+real(now));previousSlot=slot;}
            for(unsigned n=0;n<plan.targets.size();++n) {
                    if(dr::monitorOnlyRole(plan,n)) {
                        scheduler.monitorOnly(slot,n,now);
                        continue;
                    }
                    scheduler.consider(slot,n,now);
                    if(launchJobs.size()>=8 || !scheduler.beginLaunch(slot,n,now))continue;
                    const auto generation=scheduler.target(n).generation;
                    const auto previous=current[n];const auto attempt=attemptId(hash,slot,n,static_cast<unsigned>(plan.targets.size()));
                    const auto journal=scheduler.target(n).latest.journal;
                    const auto refusal=scheduler.target(n).refusal;
                    const auto absent=scheduler.target(n).consecutiveAbsent;
                    requests.row("retry_pending\t"+std::to_string(slot)+"\t"+std::to_string(n)+"\t"+attempt+"\t"+std::to_string(generation)+"\t"+real(dr::boot()));
                    if(!writer.healthy()){scheduler.invalidate();break;}
                    controller_timing::Scope dispatchMeasure(4,0);
                    launchJobs.push_back({n,slot,generation,attempt,std::async(std::launch::async,[&,n,slot,previous,attempt,journal,refusal,absent]{
                        LaunchResult result;
                        if(!dr::maySubmitRetry(plan,n)) {
                            result.reason="role_forbids_retry";return result;
                        }
                        const auto check=dr::command({self,"--snapshot",planPath,std::to_string(n),previous,"/bin/ps",bindingPath},stopped,8000,1100000);
                        result.proof=check;
                        if(!check.ok){result.reason="presubmit_"+check.reason;return result;}
                        const auto sample=dr::decodeSnapshot(check.output,plan,n,previous);
                        if(!sample.known || !retry_core::fresh(dr::boot(),sample.authority.readStart) || (!sample.authority.flags.absent && (sample.journal.pid!=journal.pid || sample.journal.start!=journal.start || sample.journal.uid!=journal.uid)) || !retry_core::presubmit(journal,refusal,absent,sample.authority.flags)){result.reason="presubmit_identity_unknown_or_changed";return result;}
                        dr::ExecGate gate{true,origin+static_cast<double>(slot)*30.0,std::min({origin+static_cast<double>(slot+1)*30.0,origin+330.0,sample.authority.readStart+2.5}),plan.bootId,planPath,bytes};
                        if(!dr::maySubmitRetry(plan,n)) {
                            result.reason="role_forbids_final_aa_submission";return result;
                        }
                        result.presubmit=true;
                        result.call=dr::command(dr::replacementArgs("/bin/aa",plan.targets[n].bundle,attempt,
                            plan.fixedBackgroundRoles?static_cast<unsigned>(plan.targetPeakMiB):0,
                            plan.fixedBackgroundRoles?static_cast<unsigned>(plan.targetFileMiB):0,
                            plan.fixedBurst4 && plan.targets[n].retryTarget ? 4u : 1u),stopped,15000,1048576,gate);
                        return result;
                    })});
                }
            }
            }
            if(canSubmit && readJobs.size()<4 && readPacer.due(now)) {
                const int selected=scheduler.nextReadTarget(now,static_cast<unsigned>(plan.targets.size()),static_cast<unsigned>(readJobs.size()),readCursor);
                if(selected>=0) {
                const unsigned n=static_cast<unsigned>(selected);
                auto& target=scheduler.target(n);
                readPacer.dispatched(now);
                if(!writer.healthy()){scheduler.invalidate();continue;}
                controller_timing::Scope dispatchMeasure(4,0);
                readJobs.push_back({n,target.generation,current[n],std::async(std::launch::async,[&,n,attempt=current[n]]{
                    return dr::command({self,"--snapshot",planPath,std::to_string(n),attempt,"/bin/ps",bindingPath},stopped,8000,1100000);
                })});
                }
            }
            {controller_timing::Scope pollMeasure(6,0);poll(nullptr,0,10);}
        }
        if(focusThread.joinable())focusThread.join();
        if(focusIntegrityInvalid.load()) {
            scheduler.invalidate();
            events.row("focus_sequence_integrity_invalid\tnew_or_unknown_generation_after_focus");
        }
        // All submitted jobs were explicitly consumed and recorded before terminal.
        // No successful workload application is killed by this controller.
        if(!writer.drain())throw std::runtime_error("async_ledger_output_failed_before_decisions");
        for(unsigned slot=1;slot<=10;++slot)for(unsigned n=0;n<plan.targets.size();++n) {
            const auto& decision=scheduler.record(slot,n);
            const auto role=dr::roleName(plan,n);
            decisions.row(std::to_string(slot)+"\t"+std::to_string(n)+"\t"+std::to_string(decision.resolved)+"\t"+std::to_string(decision.submitted)+"\t"+std::to_string(static_cast<int>(decision.decision))+"\t"+std::to_string(decision.generation)+"\t"+real(decision.observedAt)+"\t"+real(decision.readStart)+"\t"+role);
            if(((slot-1)*static_cast<unsigned>(plan.targets.size())+n+1)%64==0 && !writer.drain())throw std::runtime_error("async_ledger_decision_output_failed");
        }
        if(!writer.drain())throw std::runtime_error("async_ledger_output_failed");
        // Only after all active jobs were drained: no diagnostic I/O in hot loop.
        {
            Ledger diagnostics(writer,output+"/main_timing.tsv");
            const auto captured=controller_timing::stats;
            const auto backgroundCount=writer.written;const auto backgroundTotal=writer.backgroundSeconds;const auto backgroundMax=writer.backgroundMax;
            diagnostics.row("async_background_serialize_write\t"+std::to_string(backgroundCount)+"\t"+real(backgroundTotal)+"\t"+real(backgroundMax));
            for(unsigned k=0;k<8;++k){const auto& v=captured.stages[k];diagnostics.row("summary\t"+std::to_string(k)+"\t"+std::to_string(v.count)+"\t"+std::to_string(v.bytes)+"\t"+real(v.total)+"\t"+real(v.max)+"\t"+std::to_string(v.errors)+"\t"+real(v.cpuTotal)+"\t"+real(v.cpuMax)+"\t"+real(v.waitTotal)+"\t"+std::to_string(v.cpuUnavailable));}
            for(unsigned i=0;i<captured.used;++i){const auto& v=captured.slow[i];diagnostics.row("slow\t"+std::to_string(v.kind)+"\t"+real(v.pre)+"\t"+real(v.post)+"\t"+real(v.monoPre)+"\t"+real(v.monoPost)+"\t"+std::to_string(v.bytes)+"\t"+real(v.cpuPre)+"\t"+real(v.cpuPost));}
            diagnostics.row("dropped_slow\t"+std::to_string(captured.dropped));
        }
        {
            Ledger detail(writer, output+"/stage3_context.tsv");
            for(unsigned g=0;g<2;++g)for(unsigned k=0;k<9;++k){const auto& v=stage3_context::store.totals[g][k];detail.row("summary\t"+std::to_string(g)+"\t"+std::to_string(k)+"\t"+std::to_string(v.count)+"\t"+real(v.total)+"\t"+real(v.max)+"\t"+real(v.cpuTotal)+"\t"+std::to_string(v.errors)+"\t"+std::to_string(v.cpuUnavailable));}
            for(unsigned i=0;i<stage3_context::store.used;++i){const auto& e=stage3_context::store.longest[i];detail.row("slow\t"+std::to_string(e.group)+"\t"+std::to_string(e.target)+"\t"+std::to_string(e.generation)+"\t"+std::to_string(e.phase)+"\t"+std::to_string(e.bytes)+"\t"+real(e.pre)+"\t"+real(e.post)+"\t"+real(e.monoPre)+"\t"+real(e.monoPost)+"\t"+real(e.cpuPre)+"\t"+real(e.cpuPost));}
            detail.row("omitted\t"+std::to_string(stage3_context::store.omitted));
            if(!writer.drain())throw std::runtime_error("stage3_context_output_failed");
        }
        if(!writer.healthy())scheduler.invalidate();
        events.row("terminal\tinvalid="+std::to_string(scheduler.invalid())+"\tdecision_coverage="+std::to_string(scheduler.covered(static_cast<unsigned>(plan.targets.size())))+"\twindow_finished="+std::to_string(dr::boot()>=origin+330.0)+"\tboottime="+real(dr::boot()));
        if(!writer.finish())throw std::runtime_error("async_ledger_final_output_failed");
        return scheduler.invalid()?6:0;
    } catch(const std::exception& e) {std::cerr<<"CONTROLLER_INVALID "<<e.what()<<'\n';return 7;}
}
