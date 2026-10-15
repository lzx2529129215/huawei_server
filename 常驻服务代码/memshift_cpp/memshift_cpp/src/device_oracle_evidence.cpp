#include <iostream>
#include <cstdlib>
#include "device_oracle_evidence.hpp"
#include "device_proc_reader.hpp"
#include "device_daily_evidence.hpp"
#include "daily_disk_backend.hpp"
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <mutex>
#include <poll.h>
#include <regex>
#include <thread>
#include <sys/wait.h>
#include <signal.h>
extern char** environ;
namespace ms::harmony_v1 {
namespace {
using namespace device_proc;
struct Command {bool ok=false;int status=-1;std::string output,reason;double pre=0,post=0;};
Command command(const std::vector<std::string>& args,const std::atomic<bool>& stop,unsigned milliseconds=1800,std::size_t limit=1024*1024){Command r;r.pre=boot();if(args.empty() || args[0].empty() || args[0][0]!='/'){r.reason="absolute_command_required";return r;}std::vector<char*> argv;for(const auto& a:args)argv.push_back(const_cast<char*>(a.c_str()));argv.push_back(nullptr);int pipes[2];if(pipe(pipes)!=0){r.reason="pipe_failed";return r;}const pid_t pid=fork();if(pid==0){setpgid(0,0);dup2(pipes[1],STDOUT_FILENO);dup2(pipes[1],STDERR_FILENO);close(pipes[0]);close(pipes[1]);execve(argv[0],argv.data(),environ);_exit(127);}close(pipes[1]);if(pid<0){close(pipes[0]);r.reason="fork_failed";return r;}const bool ownGroup=setpgid(pid,pid)==0 || getpgid(pid)==pid;fcntl(pipes[0],F_SETFL,O_NONBLOCK);bool eof=false,done=false,cancel=false;int status=0;while(!eof || !done){char buffer[8192];for(;;){const auto n=read(pipes[0],buffer,sizeof(buffer));if(n>0){if(r.output.size()+static_cast<std::size_t>(n)>limit){r.reason="output_limit";cancel=true;break;}r.output.append(buffer,static_cast<std::size_t>(n));}else{if(n==0)eof=true;else if(errno!=EAGAIN && errno!=EINTR){r.reason="read_error";cancel=true;}break;}}if(!done){const auto w=waitpid(pid,&status,WNOHANG);if(w==pid)done=true;else if(w<0 && errno!=EINTR){r.reason="wait_error";cancel=true;}}if(stop.load()){r.reason="stopped";cancel=true;}if((boot()-r.pre)*1000>milliseconds){r.reason="timeout";cancel=true;}if(cancel){if(!done){if(ownGroup)kill(-pid,SIGKILL);kill(pid,SIGKILL);while(waitpid(pid,&status,0)<0 && errno==EINTR){}done=true;}break;}if(eof && done)break;pollfd poller{pipes[0],POLLIN,0};poll(&poller,1,20);}close(pipes[0]);r.post=boot();r.status=WIFEXITED(status)?WEXITSTATUS(status):128+(WIFSIGNALED(status)?WTERMSIG(status):0);r.ok=!cancel && done && eof && r.status==0 && r.output.find('\0')==std::string::npos && r.post>=r.pre;if(!r.ok && r.reason.empty())r.reason="nonzero_or_incomplete";return r;}
std::vector<std::string> pipes(const std::string& line){std::vector<std::string> v;std::istringstream in(line);std::string x;while(std::getline(in,x,'|'))v.push_back(x);return v;}
bool zeroNumber(const std::string& text,std::uint64_t& n){if(text=="0"){n=0;return true;}return number(text,n);}
std::string match(const std::string& raw,const std::string& pattern){std::smatch m;if(!std::regex_search(raw,m,std::regex(pattern,std::regex::icase)))return {};return m[1];}
struct AaMatch { unsigned matches=0;std::string state,reason; };
AaMatch aaMatch(const std::string& raw,const OracleDonor& d){const std::regex blocks("AppRunningRecord[^\\n]*[\\s\\S]*?(?=AppRunningRecord|$)",std::regex::icase);AaMatch result;for(std::sregex_iterator it(raw.begin(),raw.end(),blocks),end;it!=end;++it){const std::string block=it->str();const auto bundle=match(block,"(?:^|\\n)\\s*(?:process\\s*name|bundle(?:\\s*name)?|app\\s*name)\\s*(?:[:=#]\\s*|\\s+)\\[?([^\\]\\s,;]+)");const auto pid=match(block,"(?:^|\\n)\\s*pid\\s*[#:=]\\s*(\\d+)\\b");const auto uid=match(block,"\\buid\\s*[#:=]\\s*(\\d+)\\b");if(bundle==d.bundle && pid==std::to_string(d.pid) && uid==d.uid){result.state=match(block,"(?:^|\\n)\\s*state\\s*[#:= ]\\s*(FOREGROUND|BACKGROUND)\\b");++result.matches;}}result.reason=result.matches==0?"no_matching_record":result.matches>1?"multiple_matching_records":result.state=="FOREGROUND"?"foreground":result.state=="BACKGROUND"?"background":"unknown_state";return result;}
// Preserve the Oracle parser contract exactly: one match returns its original state.
std::string aaState(const std::string& raw,const OracleDonor& d){const auto result=aaMatch(raw,d);return result.matches==1?result.state:"";}
struct Window {std::uint64_t pid=0,id=0;std::string name;};
struct Windows {bool valid=false;std::uint64_t focus=0,reportedSessionCount=0;std::vector<Window> rows;};
Windows windows(const std::string& raw){Windows result;std::istringstream lines(raw);std::string line;bool header=false,ended=false,malformed=false;std::set<std::uint64_t> ids;while(std::getline(lines,line)){if(!header){if(std::regex_search(line,std::regex("WindowName\\s+DisplayId\\s+Pid\\s+WinId")))header=true;continue;}if(std::regex_search(line,std::regex("^\\s*Focus window:",std::regex::icase))){ended=true;break;}const auto clean=trim(line);if(clean.empty() || clean.find_first_not_of('-')==std::string::npos)continue;std::istringstream fields(clean);std::string name,display,pid,id;fields>>name>>display>>pid>>id;std::uint64_t p=0,w=0,disp=0;if(!zeroNumber(pid,p) || !zeroNumber(id,w) || display.find_first_not_of("0123456789")!=std::string::npos){malformed=true;continue;}(void)disp;if(!ids.insert(w).second)malformed=true;result.rows.push_back({p,w,name});}const auto f=match(raw,"(?:^|\\n)\\s*Focus window:\\s*(\\d+)\\s*(?:\\n|$)");const auto n=match(raw,"(?:^|\\n)\\s*Total window num:\\s*(\\d+)\\s*(?:\\n|$)");std::uint64_t count=0;unsigned focused=0;if(zeroNumber(f,result.focus))for(const auto& row:result.rows)if(row.id==result.focus)++focused;const bool countKnown=number(n,count);result.reportedSessionCount=count;result.valid=header && ended && !malformed && !result.rows.empty() && countKnown && count>=result.rows.size() && focused==1;return result;}
bool detail(const std::string& raw,const Window& w,bool& visible){const auto pid=match(raw,"(?:^|\\n)\\s*Pid\\s*:\\s*(\\d+)");const auto id=match(raw,"(?:^|\\n)\\s*WinId\\s*:\\s*(\\d+)");const auto v=match(raw,"(?:^|\\n)\\s*IsVisible\\s*:\\s*(true|false)\\b");const auto focusable=match(raw,"(?:^|\\n)\\s*Focusable\\s*:\\s*(true|false)\\b");if(pid!=std::to_string(w.pid) || id!=std::to_string(w.id) || (v!="true" && v!="false") || (focusable!="true" && focusable!="false"))return false;visible=v=="true";return true;}
bool browserDetail(const std::string& raw,const Window& w,bool& visible){
 const auto pid=match(raw,"(?:^|\\n)\\s*Pid\\s*:\\s*(\\d+)");
 const auto id=match(raw,"(?:^|\\n)\\s*WinId\\s*:\\s*(\\d+)");
 const auto v=match(raw,"(?:^|\\n)\\s*IsVisible\\s*:\\s*(true|false)\\b");
 if(pid!=std::to_string(w.pid) || id!=std::to_string(w.id) || (v!="true" && v!="false"))return false;
 visible=v=="true";return true;
}
bool procProof(OracleDonor& d,const std::string& root,double& pre,double& post){
 pre=boot();const auto uid=uintValue(d.uid);const Donor expected{d.bundle,d.pid,uid,d.start};
 const std::string group=root+"/100/"+d.bundle+"_"+d.uid;
 if(d.bundle!="com.huawei.hmos.browser") {
  const std::string base="/proc/"+std::to_string(d.pid);std::string a,status,cmd,cg,members,b;
  bool ok=boundedRead(base+"/stat",65536,false,a);ok=boundedRead(base+"/status",65536,false,status)&&ok;
  ok=boundedRead(base+"/cmdline",16384,true,cmd)&&ok;ok=boundedRead(base+"/cgroup",16384,false,cg)&&ok;
  ok=boundedRead(group+"/cgroup.procs",16384,false,members)&&ok;ok=boundedRead(base+"/stat",65536,false,b)&&ok;
  post=boot();d.members.clear();return ok && pre>0 && post>=pre && identity(expected,a,status,cmd,cg,members,b) &&
      (d.members.emplace(d.pid,d.start),true);
 }
 // Browser windows and renderers share one app memcg. Snapshot every member
 // with a bracketing stat read; require one stable set and positive background
 // scores for the whole registered group.
 std::string membersBefore,scoreBefore,membersAfter,scoreAfter;
 if(!boundedRead(group+"/cgroup.procs",65536,false,membersBefore) ||
    !boundedRead(group+"/memory.app_score",128,false,scoreBefore)) {post=boot();return false;}
 std::set<std::uint64_t> pids;std::istringstream pidRows(membersBefore);std::string token;
 while(pidRows>>token){std::uint64_t pid=0;if(!number(token,pid) || !pids.insert(pid).second || pids.size()>64){post=boot();return false;}}
 if(pids.empty() || !pids.count(d.pid)){post=boot();return false;}
 std::uint64_t appScore=0;if(!number(trim(scoreBefore),appScore) || appScore==0){post=boot();return false;}
 const auto registeredMembers=d.members;const auto registeredUids=d.memberUids;
 std::map<std::uint64_t,std::uint64_t> observedMembers;bool ok=true;
 for(const auto pid:pids) {
  const std::string base="/proc/"+std::to_string(pid);std::string before,status,cg,adj,after;
  ok=boundedRead(base+"/stat",65536,false,before)&&ok;ok=boundedRead(base+"/status",65536,false,status)&&ok;
  ok=boundedRead(base+"/cgroup",16384,false,cg)&&ok;ok=boundedRead(base+"/oom_score_adj",64,false,adj)&&ok;
  ok=boundedRead(base+"/stat",65536,false,after)&&ok;if(!ok)break;
  std::uint64_t startBefore=0,startAfter=0;
  if(!device_proc::statStartTicks(before,pid,startBefore) || !device_proc::statStartTicks(after,pid,startAfter)){ok=false;break;}
  const auto registeredUid=registeredUids.find(pid);
  if(registeredUid==registeredUids.end()){ok=false;break;}
  const Donor member{d.bundle,pid,registeredUid->second,startBefore};std::uint64_t adjValue=0;
  const std::string expectedCg="/100/"+d.bundle+"_"+d.uid;
  bool memoryCg=false;std::istringstream cgRows(cg);std::string row;
  while(std::getline(cgRows,row))if(row.find(":memory:")!=std::string::npos && row.size()>=expectedCg.size() &&
      row.compare(row.size()-expectedCg.size(),expectedCg.size(),expectedCg)==0)memoryCg=true;
  const bool valid=startBefore==startAfter && device_proc::statMatches(before,member) &&
      device_proc::statMatches(after,member) && device_proc::statusMatches(status,member) && memoryCg &&
      number(trim(adj),adjValue) && adjValue>0;
  if(!valid){ok=false;break;}
  observedMembers.emplace(pid,startBefore);
 }
 if(ok) {
  std::set<std::uint64_t> afterPids;std::string afterToken;
  if(!boundedRead(group+"/cgroup.procs",65536,false,membersAfter)) ok=false;
  else {std::istringstream rows(membersAfter);while(rows>>afterToken){std::uint64_t pid=0;if(!number(afterToken,pid) || !afterPids.insert(pid).second){ok=false;break;}}
   if(afterPids!=pids)ok=false;}
 }
 if(ok)ok=boundedRead(group+"/memory.app_score",128,false,scoreAfter) && scoreAfter==scoreBefore;
 if(ok && (registeredMembers.empty() || registeredMembers!=observedMembers ||
           registeredUids.size()!=observedMembers.size() ||
           !registeredUids.count(d.pid) || registeredUids.at(d.pid)!=uid))ok=false;
 if(ok) {
  const std::string primary="/proc/"+std::to_string(d.pid)+"/cmdline";std::string cmd;
  ok=boundedRead(primary,16384,true,cmd) && cmd.find(d.bundle)!=std::string::npos &&
      observedMembers.count(d.pid) && observedMembers.at(d.pid)==d.start;
 }
 if(ok){d.members=observedMembers;d.memberUids=registeredUids;}
 else {d.members=registeredMembers;d.memberUids=registeredUids;}
 post=boot();return ok && pre>0 && post>=pre;
}
}
WpsWindowEvidence parseWpsWindowEvidence(const std::map<std::uint64_t,std::uint64_t>& members,
    std::uint64_t mainPid,std::uint64_t uid,const std::string& memoryDump,
    const std::string& windowTable,const std::map<std::uint64_t,std::string>& details,
    double oldest,double now) {
    WpsWindowEvidence out;out.oldest=oldest;
    if(members.empty() || !members.count(mainPid) || !mainPid || !uid) {out.reason="main_identity_missing";return out;}
    unsigned matched=0;std::istringstream lines(memoryDump);std::string line;
    while(std::getline(lines,line)) {
        std::istringstream fields(line);std::vector<std::string> columns;std::string field;
        while(fields>>field)columns.push_back(field);
        if(columns.size()<12 || columns[0]!=std::to_string(mainPid))continue;
        if(columns[1]!=std::to_string(uid) || columns[2]!="cn.wps.office.hap") {
            out.reason="memory_main_identity_mismatch";return out;
        }
        ++matched;
        if(columns[11]!="No") {out.reason="memory_main_foreground_or_unknown";return out;}
    }
    if(matched!=1) {out.reason="memory_main_row_missing_or_ambiguous";return out;}
    const auto inventory=windows(windowTable);
    if(!inventory.valid) {out.reason="window_inventory_unknown";return out;}
    unsigned mainWindows=0;
    for(const auto& window:inventory.rows) if(members.count(window.pid)) {
        if(window.pid==mainPid)++mainWindows;
        const auto it=details.find(window.id);bool visible=true;
        if(it==details.end() || !detail(it->second,window,visible) || visible || inventory.focus==window.id) {
            out.reason="owned_window_visible_focused_or_unknown";return out;
        }
    }
    if(!mainWindows) {out.reason="main_window_missing";return out;}
    if(!std::isfinite(oldest) || !std::isfinite(now) || oldest<=0 || now<oldest || now-oldest>2.5) {
        out.reason="window_evidence_expired";return out;
    }
    out.verified=true;out.reason="main_not_foreground_and_all_owned_windows_invisible";return out;
}

WpsWindowEvidence sampleWpsWindowEvidence(const std::map<std::uint64_t,std::uint64_t>& members,
    std::uint64_t mainPid,std::uint64_t uid,const std::atomic<bool>& stop) {
    WpsWindowEvidence out;
    if(members.empty() || !members.count(mainPid) || stop.load()) {out.reason="main_identity_missing_or_stopped";return out;}
    const auto memory=command({"/system/bin/hidumper","-s","MemoryManagerService","-a","-r"},stop,1800,2*1024*1024);
    if(!memory.ok) {out.reason="memory_dump_"+memory.reason;return out;}
    const auto table=command({"/system/bin/hidumper","-s","WindowManagerService","-a","-a"},stop);
    if(!table.ok) {out.reason="window_table_"+table.reason;return out;}
    const auto inventory=windows(table.output);std::map<std::uint64_t,std::string> details;
    if(!inventory.valid) {out.reason="window_inventory_unknown";return out;}
    double oldest=std::min(memory.pre,table.pre);
    for(const auto& window:inventory.rows) if(members.count(window.pid)) {
        if(stop.load() || device_proc::boot()-oldest>2.5) {out.reason="window_evidence_expired_or_stopped";return out;}
        const auto item=command({"/system/bin/hidumper","-s","WindowManagerService","-a","-w "+std::to_string(window.id)},stop);
        if(!item.ok) {out.reason="window_detail_"+item.reason;return out;}
        details[window.id]=item.output;oldest=std::min(oldest,item.pre);
    }
    return parseWpsWindowEvidence(members,mainPid,uid,memory.output,table.output,details,oldest,device_proc::boot());
}

DailyDeviceEvidence parseDeviceDailyEvidence(const std::string& leaf,std::uint64_t pid,
    std::uint64_t start,const std::string& before,const std::string& status,const std::string& cmd,
    const std::string& cg,const std::string& members,const std::string& after,const std::string& aa,
    const std::string& table,const std::map<std::uint64_t,std::string>& details,double oldest,double now) {
    DailyDeviceEvidence out;out.oldest=oldest;out.pid=pid;out.startTicks=start;
    if(!daily::disk::ownedLeaf(leaf)) {out.reason="not_owned_test_leaf";return out;}
    const auto split=leaf.rfind('_');const auto bundle=leaf.substr(4,split-4);
    out.uid=uintValue(leaf.substr(split+1));
    const device_proc::Donor expected{bundle,pid,out.uid,start};
    const auto nul=cmd.find('\0');const auto first=cmd.substr(0,nul);
    bool exactGroup=false;std::istringstream cgRows(cg);std::string row;
    while(std::getline(cgRows,row)) {
        const auto colon=row.rfind(':');
        if(colon!=std::string::npos && row.substr(colon+1)=="/"+leaf) exactGroup=true;
    }
    if(first!=bundle || !exactGroup || !device_proc::identity(expected,before,status,cmd,cg,members,after)) {
        out.reason="singleton_uid_cmdline_cgroup_or_start_changed";return out;
    }
    OracleDonor d;d.bundle=bundle;d.pid=pid;d.uid=std::to_string(out.uid);
    const auto parsedAa=aaMatch(aa,d);out.aaParsed=true;out.aaMatchCount=parsedAa.matches;out.aaReason=parsedAa.reason;
    out.aaState=parsedAa.matches==1 && (parsedAa.state=="BACKGROUND" || parsedAa.state=="FOREGROUND")?parsedAa.state:"UNKNOWN";
    // Keep the established rejection/status and retry policy; detail is diagnostic only.
    if(parsedAa.matches!=1 || parsedAa.state!="BACKGROUND") {out.reason="aa_background_unknown_or_foreground";return out;}
    const auto inventory=windows(table);
    if(!inventory.valid) {out.reason="wms_inventory_unknown";return out;}
    unsigned count=0;
    for(const auto& window:inventory.rows) if(window.pid==pid) {
        ++count;const auto it=details.find(window.id);bool visible=true;
        if(it==details.end() || !detail(it->second,window,visible) || visible || inventory.focus==window.id) {
            out.reason="window_visible_focused_or_unknown";return out;
        }
    }
    if(!count) {out.reason="owned_window_missing";return out;}
    if(!std::isfinite(oldest) || !std::isfinite(now) || oldest<=0 || now<oldest || now-oldest>2.5) {
        out.reason="device_snapshot_expired";return out;
    }
    out.verified=true;out.reason="aa_wms_proc_snapshot_verified_no_independent_lifecycle_stream";return out;
}
DailyDeviceEvidence parseBrowserDailyGroupEvidence(const std::string& leaf,
    const std::map<std::uint64_t,std::uint64_t>& expected,
    const std::map<std::uint64_t,std::uint64_t>& expectedUids,std::uint64_t primaryPid,
    std::uint64_t primaryStart,const std::vector<DailyMemberSnapshot>& processes,
    const std::string& membersBefore,const std::string& membersAfter,const std::string& scoreBefore,
    const std::string& scoreAfter,const std::string& cmdline,const std::string& memoryManager,
    const std::string& aa,
    const std::string& table,const std::map<std::uint64_t,std::string>& details,double oldest,double now) {
    DailyDeviceEvidence out;out.oldest=oldest;out.pid=primaryPid;out.startTicks=primaryStart;
    if(!daily::disk::browserLeaf(leaf) || expected.empty() || expected.size()>64 ||
       expectedUids.size()!=expected.size() ||
       !expected.count(primaryPid) || expected.at(primaryPid)!=primaryStart) {
        out.reason="browser_group_registration_invalid";return out;
    }
    const auto split=leaf.rfind('_');const auto bundle=leaf.substr(4,split-4);
    out.uid=uintValue(leaf.substr(split+1));
    if(bundle!="com.huawei.hmos.browser" || cmdline.find(bundle)==std::string::npos) {
        out.reason="browser_primary_cmdline_or_leaf_mismatch";return out;
    }
    if(!expectedUids.count(primaryPid) || expectedUids.at(primaryPid)!=out.uid) {
        out.reason="browser_primary_uid_mismatch";return out;
    }
    const auto parsePids=[](const std::string& raw,std::set<std::uint64_t>& pids) {
        std::istringstream rows(raw);std::string token;
        while(rows>>token) {std::uint64_t pid=0;if(!device_proc::number(token,pid) || !pids.insert(pid).second)return false;}
        return !pids.empty();
    };
    std::set<std::uint64_t> beforePids,afterPids;
    if(!parsePids(membersBefore,beforePids) || !parsePids(membersAfter,afterPids) ||
       beforePids!=afterPids || beforePids.size()!=expected.size()) {
        out.reason="browser_group_members_changed_or_malformed";return out;
    }
    std::map<std::uint64_t,std::uint64_t> observed;
    bool processOk=processes.size()==expected.size();
    for(const auto& p:processes) {
        const auto uid=expectedUids.find(p.pid);
        if(uid==expectedUids.end() || p.uid!=uid->second) {processOk=false;continue;}
        device_proc::Donor d{bundle,p.pid,uid->second,p.startTicks};
        std::int64_t adj=0;
        const auto adjText=trim(p.oomScoreAdj);
        bool adjOk=!adjText.empty();
        try {std::size_t used=0;adj=std::stoll(adjText,&used);adjOk=adjOk && used==adjText.size();}
        catch(...) {adjOk=false;}
        bool memoryCg=false;std::istringstream cgRows(p.cgroup);std::string row;
        const std::string suffix="/100/"+bundle+"_"+std::to_string(out.uid);
        while(std::getline(cgRows,row)) if(row.find(":memory:")!=std::string::npos &&
            row.size()>=suffix.size() && row.compare(row.size()-suffix.size(),suffix.size(),suffix)==0) memoryCg=true;
        const bool identity=device_proc::statMatches(p.statBefore,d) && device_proc::statMatches(p.statAfter,d) &&
            device_proc::statusMatches(p.status,d) && memoryCg && adjOk && adj>0;
        if(!identity || !observed.emplace(p.pid,p.startTicks).second) processOk=false;
    }
    if(!processOk || observed!=expected || beforePids.size()!=observed.size()) {
        out.reason="browser_group_pid_start_uid_cgroup_or_oomadj_mismatch";return out;
    }
    std::map<std::uint64_t,std::pair<std::uint64_t,std::string>> managed;
    std::istringstream mmRows(memoryManager);std::string mmLine;
    while(std::getline(mmRows,mmLine)) {
        const auto pipe=mmLine.find('|');if(pipe==std::string::npos)continue;
        std::istringstream fields(mmLine.substr(pipe+1));std::string pidText,uidText,hostText,realUidText,act,bundle,processName;
        if(!(fields>>pidText>>uidText>>hostText>>realUidText>>act>>bundle>>processName))continue;
        std::uint64_t pid=0,uid=0,hostPid=0,realUid=0;
        if(!device_proc::number(pidText,pid) || !device_proc::number(uidText,uid) ||
           (hostText!="0" && !device_proc::number(hostText,hostPid)) ||
           !device_proc::number(realUidText,realUid))continue;
        if(expected.count(pid)) {
            if(!managed.emplace(pid,std::make_pair(hostPid,processName)).second ||
               uid!=out.uid || realUid!=expectedUids.at(pid) || bundle!="com.huawei.hmos.browser") {
                out.reason="browser_memory_manager_owner_or_realuid_mismatch";return out;
            }
        }
    }
    if(managed.size()!=expected.size()) {out.reason="browser_memory_manager_member_missing";return out;}
    for(const auto& member:expected) {
        const auto row=managed.find(member.first);
        if(row==managed.end() ||
           (member.first==primaryPid ? (row->second.first!=0 || row->second.second!="com.huawei.hmos.browser") :
             (row->second.first!=primaryPid ||
              row->second.second.rfind("com.huawei.hmos.browser:",0)!=0))) {
            out.reason="browser_memory_manager_host_process_relation_mismatch";return out;
        }
    }
    std::uint64_t appBefore=0,appAfter=0;
    if(!device_proc::number(trim(scoreBefore),appBefore) || !device_proc::number(trim(scoreAfter),appAfter) ||
       appBefore==0 || appBefore!=appAfter) {out.reason="browser_group_app_score_not_positive_stable";return out;}
    OracleDonor donor;donor.bundle=bundle;donor.pid=primaryPid;donor.uid=std::to_string(out.uid);
    const auto parsedAa=aaMatch(aa,donor);out.aaParsed=true;out.aaMatchCount=parsedAa.matches;
    out.aaReason=parsedAa.reason;
    out.aaState=parsedAa.matches==1 && (parsedAa.state=="BACKGROUND" || parsedAa.state=="FOREGROUND")?
        parsedAa.state:"UNKNOWN";
    if(parsedAa.matches!=1 || (parsedAa.state!="BACKGROUND" && parsedAa.state!="FOREGROUND")) {
        out.reason="aa_main_identity_unknown";return out;
    }
    const auto inventory=windows(table);
    if(!inventory.valid) {out.reason="browser_wms_inventory_unknown";return out;}
    unsigned browserWindows=0;
    for(const auto& window:inventory.rows) {
        const bool browserName=std::regex_match(window.name,std::regex("browser[0-9]+"));
        if(browserName && !expected.count(window.pid)) {out.reason="browser_window_owned_by_unregistered_pid";return out;}
        if(!expected.count(window.pid)) continue;
        const auto it=details.find(window.id);bool visible=true;
        if(it==details.end() || !browserDetail(it->second,window,visible) || visible || inventory.focus==window.id) {
            out.reason="browser_group_window_visible_focused_or_unknown";return out;
        }
        if(browserName) ++browserWindows;
    }
    if(!browserWindows) {out.reason="browser_window_missing";return out;}
    if(!std::isfinite(oldest) || !std::isfinite(now) || oldest<=0 || now<oldest || now-oldest>2.5) {
        out.reason="browser_device_snapshot_expired";return out;
    }
    out.verified=true;out.reason="browser_group_proc_appscore_aa_wms_snapshot_verified";return out;
}
DailyDeviceEvidence sampleDeviceDailyEvidence(const std::string& leaf,std::uint64_t dev,
    std::uint64_t ino,const std::map<std::uint64_t,std::uint64_t>& expected,
    const std::map<std::uint64_t,std::uint64_t>& expectedUids,const std::atomic<bool>& stop) {
    DailyDeviceEvidence out;
    if(daily::disk::browserLeaf(leaf)) {
        if(expected.empty() || expected.size()>64 || expectedUids.size()!=expected.size()) {
            out.reason="browser_group_registration_invalid";return out;
        }
        const double pre=device_proc::boot();const std::string group="/dev/memcg/"+leaf;
        const auto groupMatches=[&] {struct stat s{};const int fd=open(group.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
            if(fd<0)return false;const bool ok=fstat(fd,&s)==0 && static_cast<std::uint64_t>(s.st_dev)==dev &&
                static_cast<std::uint64_t>(s.st_ino)==ino;close(fd);return ok;};
        if(!groupMatches()) {out.reason="group_inode_changed";return out;}
        std::string membersBefore,scoreBefore,membersAfter,scoreAfter;
        if(!device_proc::boundedRead(group+"/cgroup.procs",65536,false,membersBefore) ||
           !device_proc::boundedRead(group+"/memory.app_score",128,false,scoreBefore)) {
            out.reason="browser_group_initial_snapshot_unreadable";return out;
        }
        std::set<std::uint64_t> observedPids;std::istringstream initialIds(membersBefore);std::string token;
        while(initialIds>>token) {
            std::uint64_t pid=0;if(!device_proc::number(token,pid) || !observedPids.insert(pid).second ||
               observedPids.size()>64) {out.reason="browser_group_member_list_malformed";return out;}
        }
        if(observedPids.size()!=expected.size() ||
           !std::equal(observedPids.begin(),observedPids.end(),expected.begin(),
               [](std::uint64_t pid,const auto& row){return pid==row.first;})) {
            out.reason="browser_group_registered_member_set_changed";return out;
        }
        std::vector<DailyMemberSnapshot> processRows;processRows.reserve(expected.size());
        std::string primaryCmdline;bool ok=true;
        for(const auto& row:expected) {
            if(stop.load() || device_proc::boot()-pre>2.5) {ok=false;break;}
            const auto uidIt=expectedUids.find(row.first);
            if(uidIt==expectedUids.end() || !uidIt->second) {ok=false;break;}
            const std::string proc="/proc/"+std::to_string(row.first);
            DailyMemberSnapshot item;item.pid=row.first;item.uid=uidIt->second;item.startTicks=row.second;
            ok=device_proc::boundedRead(proc+"/stat",65536,false,item.statBefore)&&ok;
            ok=device_proc::boundedRead(proc+"/status",65536,false,item.status)&&ok;
            ok=device_proc::boundedRead(proc+"/cgroup",16384,false,item.cgroup)&&ok;
            ok=device_proc::boundedRead(proc+"/oom_score_adj",64,false,item.oomScoreAdj)&&ok;
            ok=device_proc::boundedRead(proc+"/stat",65536,false,item.statAfter)&&ok;
            if(!ok)break;
            processRows.push_back(std::move(item));
        }
        // Main-process identity comes from the registered donor row.
        if(ok) {
            const auto main=std::find_if(expected.begin(),expected.end(),[&](const auto& row) {
                const auto it=expectedUids.find(row.first);return it!=expectedUids.end() && it->second==uintValue(leaf.substr(leaf.rfind('_')+1));
            });
            if(main==expected.end()) ok=false;
            else {
                out.pid=main->first;out.startTicks=main->second;
                ok=device_proc::boundedRead("/proc/"+std::to_string(out.pid)+"/cmdline",16384,true,primaryCmdline);
            }
        }
        const auto mm=command({"/system/bin/hidumper","-s","MemoryManagerService","-a","-r"},stop,1800,2*1024*1024);
        const auto aa=command({"/system/bin/aa","dump","-r"},stop);
        const auto table=command({"/system/bin/hidumper","-s","WindowManagerService","-a","-a"},stop);
        const auto inventory=windows(table.output);std::map<std::uint64_t,std::string> details;
        if(ok && mm.ok && aa.ok && table.ok && inventory.valid) {
            for(const auto& w:inventory.rows) if(expected.count(w.pid)) {
                if(stop.load() || device_proc::boot()-pre>2.5) {ok=false;break;}
                const auto d=command({"/system/bin/hidumper","-s","WindowManagerService","-a","-w "+std::to_string(w.id)},stop);
                if(!d.ok) {ok=false;break;}details[w.id]=d.output;
            }
        } else ok=false;
        if(ok) ok=device_proc::boundedRead(group+"/cgroup.procs",65536,false,membersAfter) &&
            device_proc::boundedRead(group+"/memory.app_score",128,false,scoreAfter);
        if(!ok || !groupMatches() || stop.load()) {out.reason="browser_group_snapshot_collection_failed";return out;}
        return parseBrowserDailyGroupEvidence(leaf,expected,expectedUids,out.pid,out.startTicks,processRows,
            membersBefore,membersAfter,scoreBefore,scoreAfter,primaryCmdline,mm.output,aa.output,table.output,
            details,pre,device_proc::boot());
    }
    if(!daily::disk::ownedLeaf(leaf) || expected.size()!=1) {out.reason="owned_singleton_scope_required";return out;}
    const double pre=device_proc::boot();const std::string group="/dev/memcg/"+leaf;
    const auto groupMatches=[&] {struct stat s{};const int fd=open(group.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
        if(fd<0)return false;const bool ok=fstat(fd,&s)==0 && static_cast<std::uint64_t>(s.st_dev)==dev &&
            static_cast<std::uint64_t>(s.st_ino)==ino;close(fd);return ok;};
    if(!groupMatches()) {out.reason="group_inode_changed";return out;}
    std::string members,before,status,cmd,cg,after,membersAfter;
    if(!device_proc::boundedRead(group+"/cgroup.procs",16384,false,members)) {out.reason="members_unreadable";return out;}
    std::istringstream ids(members);std::string text,extra;std::uint64_t pid=0;
    if(!(ids>>text) || (ids>>extra) || !device_proc::number(text,pid) || !expected.count(pid)) {
        out.reason="singleton_members_changed";return out;
    }
    const std::string proc="/proc/"+std::to_string(pid);
    bool ok=device_proc::boundedRead(proc+"/stat",65536,false,before);
    ok=device_proc::boundedRead(proc+"/status",65536,false,status)&&ok;
    ok=device_proc::boundedRead(proc+"/cmdline",16384,true,cmd)&&ok;
    ok=device_proc::boundedRead(proc+"/cgroup",16384,false,cg)&&ok;
    const auto aa=command({"/system/bin/aa","dump","-r"},stop);
    const auto table=command({"/system/bin/hidumper","-s","WindowManagerService","-a","-a"},stop);
    const auto inventory=windows(table.output);std::map<std::uint64_t,std::string> details;
    if(ok && aa.ok && table.ok && inventory.valid) {
        for(const auto& w:inventory.rows) if(w.pid==pid) {
            if(stop.load() || device_proc::boot()-pre>2.5) {ok=false;break;}
            const auto d=command({"/system/bin/hidumper","-s","WindowManagerService","-a","-w "+std::to_string(w.id)},stop);
            if(!d.ok) {ok=false;break;}details[w.id]=d.output;
        }
    } else ok=false;
    ok=device_proc::boundedRead(proc+"/stat",65536,false,after)&&ok;
    ok=device_proc::boundedRead(group+"/cgroup.procs",16384,false,membersAfter)&&ok;
    if(!ok || membersAfter!=members || !groupMatches() || stop.load()) {out.reason="read_identity_or_command_incomplete";return out;}
    return parseDeviceDailyEvidence(leaf,pid,expected.at(pid),before,status,cmd,cg,members,after,
        aa.output,table.output,details,pre,device_proc::boot());
}

OracleRequest parseDeviceOraclePlan(const std::string& text) {
 std::istringstream in(text);std::string line;
 if(!std::getline(in,line))throw std::runtime_error("device_plan_header");
 const bool browser=line=="device_browser_donor_plan_v1";
 if(!browser && line!="device_oracle_plan_v1")throw std::runtime_error("device_plan_header");
 if(!std::getline(in,line))throw std::runtime_error("device_plan_fields");
 const auto meta=pipes(line);if(meta.size()!=5)throw std::runtime_error("device_plan_fields");
 OracleRequest plan;plan.id=meta[0];plan.caseId=meta[1];plan.launch=realValue(meta[2]);plan.needed=uintValue(meta[3]);
 const auto count=uintValue(meta[4]);plan.schemaVersion=browser?3U:2U;
 if(!caseAllowed(plan.caseId) || plan.id!=plan.caseId+"_peak_transition" || !std::isfinite(plan.launch) ||
    plan.launch<=0 || !plan.needed || (browser?count!=1:(count<1 || count>16)))
  throw std::runtime_error("device_plan_scope");
 std::set<std::string> bundles,uids;std::set<std::uint64_t> pids;
 for(std::uint64_t i=0;i<count;++i) {
  if(!std::getline(in,line))throw std::runtime_error("device_plan_missing_donor");
  const auto f=pipes(line);if(f.size()!=(browser?6U:5U))throw std::runtime_error("device_plan_donor_fields");
  OracleDonor d;d.bundle=f[0];d.uid=f[1];d.pid=uintValue(f[2]);d.start=uintValue(f[3]);d.readyAt=realValue(f[4]);
  if(browser) {
   std::istringstream members(f[5]);std::string item;
   while(std::getline(members,item,',')) {
    const auto first=item.find(':');const auto second=first==std::string::npos?std::string::npos:item.find(':',first+1);
    if(first==std::string::npos || second==std::string::npos || item.find(':',second+1)!=std::string::npos)
     throw std::runtime_error("device_plan_browser_member_fields");
    const auto memberPid=uintValue(item.substr(0,first));const auto memberUid=uintValue(item.substr(first+1,second-first-1));
    const auto memberStart=uintValue(item.substr(second+1));
    if(memberPid>INT_MAX || !memberUid || !memberStart || !d.members.emplace(memberPid,memberStart).second ||
       !d.memberUids.emplace(memberPid,memberUid).second || d.members.size()>64)
     throw std::runtime_error("device_plan_browser_member_scope");
   }
   if(d.members.empty() || !d.members.count(d.pid) || d.members.at(d.pid)!=d.start ||
      !d.memberUids.count(d.pid) || d.memberUids.at(d.pid)!=uintValue(d.uid))
    throw std::runtime_error("device_plan_browser_primary_member_missing");
  }
  const bool allowed=browser?d.bundle=="com.huawei.hmos.browser":device_proc::bundleAllowed(d.bundle);
  if(!allowed || !d.pid || d.pid>INT_MAX || !uintValue(d.uid) || !d.start || !std::isfinite(d.readyAt) ||
     d.readyAt<=0 || d.readyAt>=plan.launch || !bundles.insert(d.bundle).second ||
     !uids.insert(d.uid).second || !pids.insert(d.pid).second)
   throw std::runtime_error("device_plan_donor_scope");
  d.dailyReady=true;d.evidenceSource="os_snapshot";plan.donors.push_back(d);
 }
 while(std::getline(in,line))if(!trim(line).empty())throw std::runtime_error("device_plan_extra_fields");
 return plan;
}
std::string deviceOracleProtectedReason(const OracleDonor& d,double now){
 if(!d.procVerified || !d.windowReady || !d.lifecycleClear || d.pid==0 || d.start==0 || d.procStart!=d.start || d.aaPid!=d.pid || d.aaUid!=uintValue(d.uid) || (d.aaState!="BACKGROUND" && d.aaState!="FOREGROUND"))return {};
 for(const double stamp:{d.aaObserved,d.windowObserved,d.procObserved,d.lifecycleObserved})if(stamp<=0 || stamp>now || now-stamp>2.5)return {};
 if(d.aaState=="FOREGROUND")return "aa_foreground";
 if(d.windowVisible)return "window_visible";
 if(d.windowFocused)return "window_focused";
 return {};
}
struct DeviceOracleEvidence::Impl {std::string path,ledger,bytes,fingerprint;std::atomic<bool> stop{false};std::thread collector;std::mutex mutex,logMutex;std::condition_variable cv;OracleRequest plan,proof;std::atomic<bool> registered{false},ever{false},sticky{false};bool valid=false;std::string failure="device_proof_pending";std::uint64_t sequence=0,lastReportedSessionCount=0,lastParsedWindowCount=0;std::map<std::string,double> backgrounds;std::ofstream log,access,cycles;
 Impl(std::string p,std::string l):path(std::move(p)),ledger(std::move(l)){log.open(ledger+".device_proof_v1.csv",std::ios::out|std::ios::trunc);access.open(ledger+".device_access_v1.csv",std::ios::out|std::ios::trunc);cycles.open(ledger+".device_cycle_v1.csv",std::ios::out|std::ios::trunc);if(!log || !access || !cycles)throw std::runtime_error("device_proof_log_open");log<<"sequence,event_id,plan_sha256,cycle_pre,cycle_post,donor,aa_pre,wms_table_pre,window_pre,proc_pre,age_at_complete,valid,reason,aa_state,window_visible,window_focused,identity_verified,protected_reason,reported_session_count,parsed_window_count\n";access<<"context,event_id,plan_sha256,sequence,lookup_boottime,valid,reason\n";cycles<<"kind,sequence,event_id,plan_sha256,cycle_pre,cycle_post,valid,reason,reported_session_count,parsed_window_count\n";collector=std::thread([this]{loop();});}
 ~Impl(){stop=true;cv.notify_all();if(collector.joinable())collector.join();}
 void invalidate(const std::string& reason,bool permanent){std::lock_guard<std::mutex> lock(mutex);valid=false;failure=reason;sticky=sticky||permanent;}
 bool unchanged(){std::string current;if(!boundedRead(path,65536,false,current) || current!=bytes){invalidate("device_plan_fingerprint_changed",true);return false;}return true;}
 void loop(){while(!stop.load()){try{if(!registered){std::string candidate;if(!boundedRead(path,65536,false,candidate)){wait();continue;}auto parsed=parseDeviceOraclePlan(candidate);const auto sum=command({"/system/bin/sha256sum",path},stop,1000,4096);std::string verify;if(!sum.ok || sum.output.size()<64 || sum.output.substr(0,64).find_first_not_of("0123456789abcdef")!=std::string::npos || !boundedRead(path,65536,false,verify) || verify!=candidate)throw std::runtime_error("device_plan_registration_hash_failed");if(parsed.launch<=boot())throw std::runtime_error("device_plan_registration_after_T");{std::lock_guard<std::mutex> lock(mutex);bytes=candidate;fingerprint=sum.output.substr(0,64);plan=parsed;plan.planFingerprint=fingerprint;registered=true;} {std::lock_guard<std::mutex> lock(logMutex);cycles<<"accepted,0,"<<plan.id<<','<<fingerprint<<','<<std::setprecision(12)<<boot()<<','<<boot()<<",true,plan_registered,,\n";cycles.flush();if(!cycles)throw std::runtime_error("device_cycle_log_error");} }
 if(sticky){wait();continue;}if(!unchanged()){wait();continue;}const double pre=boot();OracleRequest sampled=plan;sampled.proofSequence=++sequence;const auto aa=command({"/system/bin/aa","dump","-r"},stop);if(!aa.ok)throw std::runtime_error("device_aa_"+aa.reason);const auto table=command({"/system/bin/hidumper","-s","WindowManagerService","-a","-a"},stop);if(!table.ok)throw std::runtime_error("device_wms_table_"+table.reason);const auto inventory=windows(table.output);lastReportedSessionCount=inventory.reportedSessionCount;lastParsedWindowCount=inventory.rows.size();if(!inventory.valid)throw std::runtime_error("device_wms_inventory_unknown");bool complete=true;std::string reason;std::vector<std::string> records;for(auto& d:sampled.donors){const auto state=aaState(aa.output,d);bool known=state=="BACKGROUND" || state=="FOREGROUND";double procPre=0,procPost=0;known=procProof(d,"/dev/memcg",procPre,procPost)&&known;bool visible=false,focused=false;unsigned owned=0;double oldestWindow=table.pre;for(const auto& w:inventory.rows)if((d.bundle=="com.huawei.hmos.browser"?d.members.count(w.pid)!=0:w.pid==d.pid)){++owned;const auto details=command({"/system/bin/hidumper","-s","WindowManagerService","-a","-w "+std::to_string(w.id)},stop);bool v=false;const bool detailOk=d.bundle=="com.huawei.hmos.browser"?browserDetail(details.output,w,v):detail(details.output,w,v);if(!details.ok || !detailOk)known=false;visible=visible||v;focused=focused||inventory.focus==w.id;oldestWindow=std::min(oldestWindow,details.pre);}known=known && owned>0;d.aaPid=d.pid;d.aaUid=uintValue(d.uid);d.aaState=state;d.aaObserved=aa.pre;d.procVerified=known;d.procStart=d.start;d.procObserved=procPre;d.windowReady=known;d.windowVisible=visible;d.windowFocused=focused;d.windowObserved=oldestWindow;d.lifecycleClear=known;d.lifecycleObserved=std::min(aa.pre,oldestWindow);if(known && state=="BACKGROUND" && !visible && !focused){if(!backgrounds.count(d.bundle))backgrounds[d.bundle]=std::max(aa.pre,oldestWindow);d.backgroundSince=backgrounds[d.bundle];}else{backgrounds.erase(d.bundle);d.backgroundSince=0;}const double age=boot()-std::min({d.aaObserved,d.windowObserved,d.procObserved,d.lifecycleObserved});const bool safe=known && age<=2.5;const auto protectedReason=deviceOracleProtectedReason(d,boot());if(!safe){complete=false;reason=!known?"device_identity_or_window_unknown":"device_proof_stale_or_not_ready";}
 std::ostringstream record;record<<sampled.proofSequence<<','<<sampled.id<<','<<fingerprint<<','<<std::setprecision(12)<<pre<<','<<boot()<<','<<d.bundle<<','<<aa.pre<<','<<table.pre<<','<<oldestWindow<<','<<procPre<<','<<age<<','<<(safe?"true":"false")<<','<<(safe?"ok":reason)<<','<<state<<','<<(visible?"true":"false")<<','<<(focused?"true":"false")<<','<<(known?"true":"false")<<','<<protectedReason<<','<<inventory.reportedSessionCount<<','<<inventory.rows.size()<<'\n';records.push_back(record.str());}
 if(!unchanged()){wait();continue;}const double post=boot();for(const auto& d:sampled.donors)if(post-std::min({d.aaObserved,d.windowObserved,d.procObserved,d.lifecycleObserved})>2.5){complete=false;reason="device_proof_stale_at_cycle_complete";} {std::lock_guard<std::mutex> lock(mutex);proof=sampled;valid=complete;failure=complete?"":reason;if(complete)ever=true;else if(ever && reason!="device_proof_stale_at_cycle_complete" && reason!="device_proof_stale_or_not_ready")sticky=true;} {std::lock_guard<std::mutex> lock(logMutex);for(const auto& record:records)log<<record;cycles<<"committed,"<<sampled.proofSequence<<','<<sampled.id<<','<<fingerprint<<','<<std::setprecision(12)<<pre<<','<<post<<','<<(complete?"true":"false")<<','<<(complete?"ok":reason)<<','<<inventory.reportedSessionCount<<','<<inventory.rows.size()<<'\n';cycles.flush();if(!cycles)invalidate("device_cycle_log_error",true);log.flush();if(!log)invalidate("device_proof_log_error",true);} }
 catch(const std::exception& e){invalidate(e.what(),ever);std::lock_guard<std::mutex> lock(logMutex);log<<sequence<<",unknown,"<<fingerprint<<",0,"<<boot()<<",unknown,0,0,0,0,0,false,"<<e.what()<<",,,,,,"<<lastReportedSessionCount<<','<<lastParsedWindowCount<<'\n';log.flush();}wait();}}
 void wait(){std::unique_lock<std::mutex> lock(mutex);cv.wait_for(lock,std::chrono::milliseconds(50),[this]{return stop.load();});}
 OracleRequest current(const std::string& context){if(registered && !unchanged())throw std::runtime_error("device_plan_fingerprint_changed");OracleRequest snapshot;std::string reason;bool ok=false;{std::lock_guard<std::mutex> lock(mutex);snapshot=proof;ok=!sticky && (valid || (ever && (failure=="device_proof_stale_at_cycle_complete" || failure=="device_proof_stale_or_not_ready")));reason=failure;if(!registered || !ever){ok=false;if(!sticky)reason="device_proof_pending";}if(ok)for(const auto& d:snapshot.donors)if(boot()-std::min({d.aaObserved,d.windowObserved,d.procObserved,d.lifecycleObserved})>2.5){reason="device_proof_expired";}} {std::lock_guard<std::mutex> lock(logMutex);access<<context<<','<<snapshot.id<<','<<snapshot.planFingerprint<<','<<snapshot.proofSequence<<','<<std::setprecision(12)<<boot()<<','<<(ok?"true":"false")<<','<<reason<<'\n';access.flush();if(!access)throw std::runtime_error("device_access_log_error");}if(!ok)throw std::runtime_error(reason.empty()?"device_proof_unknown":reason);return snapshot;}
};
DeviceOracleEvidence::DeviceOracleEvidence(const std::string& p,const std::string& l):impl_(new Impl(p,l)){}
DeviceOracleEvidence::~DeviceOracleEvidence()=default;
OracleRequest DeviceOracleEvidence::current(const std::string& context){return impl_->current(context);}
void DeviceOracleEvidence::recordReservation(const std::string& actionId,const OracleRequest& request){std::lock_guard<std::mutex> lock(impl_->logMutex);impl_->access<<"reservation:"<<actionId<<','<<request.id<<','<<request.planFingerprint<<','<<request.proofSequence<<','<<std::setprecision(12)<<boot()<<",true,reserved_from_captured_proof\n";impl_->access.flush();if(!impl_->access)throw std::runtime_error("device_reservation_proof_log_error");}
void testDeviceOracleEvidenceParsers(){const auto plan=parseDeviceOraclePlan("device_oracle_plan_v1\ns4_abcdef_c0000_peak_transition|s4_abcdef_c0000|200|4096|1\nio.memscene.scene4.workload.a1|23|17|91|100\n");if(plan.donors.size()!=1 || plan.donors[0].windowReady || plan.donors[0].procVerified)throw std::runtime_error("static plan contains dynamic trust");const auto browserPlan=parseDeviceOraclePlan("device_browser_donor_plan_v1\ns4_abcdef_c0000_peak_transition|s4_abcdef_c0000|200|4096|1\ncom.huawei.hmos.browser|20020064|35456|23427036|100|35456:20020064:23427036,35739:20100026:23427171\n");if(browserPlan.schemaVersion!=3 || browserPlan.donors.size()!=1 || browserPlan.donors[0].bundle!="com.huawei.hmos.browser" || browserPlan.donors[0].memberUids.size()!=2)throw std::runtime_error("browser group plan parser");bool browserRejected=false;try{parseDeviceOraclePlan("device_browser_donor_plan_v1\ns4_abcdef_c0000_peak_transition|s4_abcdef_c0000|200|4096|2\ncom.huawei.hmos.browser|20020064|35456|23427036|100|35456:20020064:23427036\nio.memscene.scene4.workload.a1|201|42|81|100\n");}catch(...){browserRejected=true;}if(!browserRejected)throw std::runtime_error("browser plan accepted multiple donor budgets");bool rejected=false;try{parseDeviceOraclePlan("device_oracle_plan_v1\ns4_abcdef_c0000_peak_transition|s4_abcdef_c0000|200|4096|1\nio.memscene.scene4.workload.a17|23|17|91|100\n");}catch(...){rejected=true;}if(!rejected)throw std::runtime_error("out of scope donor accepted");OracleDonor d;d.bundle="io.memscene.scene4.workload.a1";d.pid=17;d.uid="23";if(aaState("AppRunningRecord ID #0\nprocess name [io.memscene.scene4.workload.a1]\npid #17\nuid #23\nstate #BACKGROUND\n",d)!="BACKGROUND")throw std::runtime_error("AA format test");const auto table=windows("WindowName DisplayId Pid WinId\nowned 0 17 9\nFocus window: 9\nTotal window num: 1\n");if(!table.valid || table.focus!=9)throw std::runtime_error("WMS table test");const auto countGap=windows("WindowName DisplayId Pid WinId\nowned 0 17 9\nFocus window: 9\nTotal window num: 2\n");if(!countGap.valid || countGap.reportedSessionCount!=2 || countGap.rows.size()!=1)throw std::runtime_error("WMS reported sessions versus displayed rows");
for(const auto& invalid:std::vector<std::string>{"WindowName DisplayId Pid WinId\nowned 0 17 9\nFocus window: 9\nTotal window num: 0\n","WindowName DisplayId Pid WinId\nowned 0 17 9\nFocus window: 9\n","WindowName DisplayId Pid WinId\nowned 0 17 9\nTotal window num: 1\n","WindowName DisplayId Pid WinId\nowned 0 17 9\nFocus window: 8\nTotal window num: 2\n","WindowName DisplayId Pid WinId\nowned 0 17 9\nother 0 18 9\nFocus window: 9\nTotal window num: 2\n","WindowName DisplayId Pid WinId\nowned 0 BAD 9\nFocus window: 9\nTotal window num: 2\n"})if(windows(invalid).valid)throw std::runtime_error("WMS malformed inventory accepted");
if(const char* fixture=std::getenv("MEMSCENE_WMS_REAL_FIXTURE")){std::cout<<"WMS_REAL_FIXTURE_BEGIN\n";std::string raw;if(!boundedRead(fixture,1024*1024,false,raw))throw std::runtime_error("WMS fixture unreadable");const auto real=windows(raw);if(!real.valid || real.rows.size()!=60 || real.reportedSessionCount!=61)throw std::runtime_error("WMS real60/61 fixture rejected");std::cout<<"WMS_REAL_FIXTURE_COMPLETE parsed=60 reported=61\n";}
bool visible=false;if(!detail("Pid: 17\nWinId: 9\nIsVisible: false\nFocusable: true\n",table.rows[0],visible) || visible)throw std::runtime_error("WMS detail test");if(!browserDetail("WindowName: browser0\nWinId: 9\nPid: 17\nIsVisible: false\nParentWindowId: 0\n",table.rows[0],visible) || visible)throw std::runtime_error("browser WMS detail test");OracleDonor protectedDonor;protectedDonor.bundle="io.memscene.scene4.workload.a1";protectedDonor.uid="23";protectedDonor.pid=17;protectedDonor.start=91;protectedDonor.procStart=91;protectedDonor.aaPid=17;protectedDonor.aaUid=23;protectedDonor.procVerified=true;protectedDonor.windowReady=true;protectedDonor.lifecycleClear=true;protectedDonor.aaState="BACKGROUND";protectedDonor.aaObserved=199;protectedDonor.windowObserved=199;protectedDonor.procObserved=199;protectedDonor.lifecycleObserved=199;protectedDonor.windowVisible=false;protectedDonor.windowFocused=true;
 OracleDonor eligible=protectedDonor;eligible.bundle="io.memscene.scene4.workload.a2";eligible.windowFocused=false;
 if(deviceOracleProtectedReason(protectedDonor,200)!="window_focused" || !deviceOracleProtectedReason(eligible,200).empty())throw std::runtime_error("per donor protection classification");
 const std::vector<OracleDonor> candidates{protectedDonor,eligible};const auto selected=std::find_if(candidates.begin(),candidates.end(),[](const OracleDonor& item){return deviceOracleProtectedReason(item,200).empty();});if(selected==candidates.end() || selected->bundle!=eligible.bundle)throw std::runtime_error("protected first eligible second selection");
 OracleDonor unknown=protectedDonor;unknown.procVerified=false;if(!deviceOracleProtectedReason(unknown,200).empty())throw std::runtime_error("unknown identity disguised as protected");OracleDonor changed=eligible;changed.aaState="FOREGROUND";if(deviceOracleProtectedReason(changed,200)!="aa_foreground")throw std::runtime_error("target foreground classification");
 std::atomic<bool> cancel{false};const auto success=command({"/system/bin/sh","-c","printf collector_ok"},cancel);const auto nonzero=command({"/system/bin/sh","-c","exit 7"},cancel);const auto timeout=command({"/system/bin/sh","-c","sleep 2"},cancel,30);const auto limited=command({"/system/bin/sh","-c","printf 12345678"},cancel,1000,4);if(!success.ok || success.output!="collector_ok" || nonzero.ok || nonzero.status!=7 || timeout.ok || timeout.reason!="timeout" || limited.ok || limited.reason!="output_limit")throw std::runtime_error("bounded child exact branch tests");cancel=true;const auto stopped=command({"/system/bin/sh","-c","sleep 2"},cancel);if(stopped.ok || stopped.reason!="stopped")throw std::runtime_error("child cancellation exact branch test");}
}
