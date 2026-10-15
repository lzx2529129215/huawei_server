// Read-only diagnostic. No reclaim/control-node opens, daemon, or future event.
#include "../src/device_daily_evidence.hpp"
#include "../src/device_proc_reader.hpp"
#include "../src/daily_disk_backend.hpp"
#include <csignal>
#include <fstream>
#include <iostream>
#include <iterator>
using namespace ms;using namespace ms::harmony_v1;
namespace {
std::pair<std::uint64_t,std::uint64_t> singleton(const std::string& members,const std::string& stat) {
    std::istringstream in(members);std::string p,extra;std::uint64_t pid=0,start=0;
    if(!(in>>p) || (in>>extra) || !device_proc::number(p,pid)) throw std::runtime_error("singleton_member_required");
    const auto close=stat.rfind(')'),space=stat.find(' ');
    if(close==std::string::npos || space==std::string::npos || stat.substr(0,space)!=p)
        throw std::runtime_error("proc_stat_pid_unknown");
    std::istringstream fields(stat.substr(close+1));std::string token;
    for(unsigned i=0;i<=19;++i) if(!(fields>>token)) throw std::runtime_error("proc_stat_truncated");
    if(!device_proc::number(token,start)) throw std::runtime_error("proc_start_unknown");
    return {pid,start};
}
std::string escaped(const std::string& text) {
    std::string out;for(const char c:text) {if(c=='"' || c=='\\')out+='\\';if(static_cast<unsigned char>(c)>=32)out+=c;}
    return out;
}
int fake() {
    const std::string leaf="100/io.memscene.scene4.workload.a1_23",bundle="io.memscene.scene4.workload.a1";
    const auto stat=device_proc::fakeStat(17,91);const auto id=singleton("17\n",stat);
    const std::string aa="AppRunningRecord ID #0\nprocess name ["+bundle+"]\npid #17\nuid #23\nstate #BACKGROUND\n";
    const std::string table="WindowName DisplayId Pid WinId\nowned 0 17 9\nother 0 18 10\nFocus window: 10\nTotal window num: 2\n";
    const std::map<std::uint64_t,std::string> details={{9,"Pid: 17\nWinId: 9\nIsVisible: false\nFocusable: true\n"}};
    const auto parse=[&](double now) {return parseDeviceDailyEvidence(leaf,id.first,id.second,stat,
        "Pid: 17\nUid: 23 23 23 23\n",bundle,"1:memory:/"+leaf+"\n","17\n",stat,aa,table,details,100,now);};
    if(!parse(101).verified || parse(102.6).verified) throw std::runtime_error("fake_parser_ttl_failed");
    bool denied=false;try {(void)singleton("17 18",stat);} catch(...) {denied=true;}
    const std::string browserLeaf="100/com.huawei.hmos.browser_20020064";
    Options unregisteredBrowser;
    Options registeredBrowser;registeredBrowser.dailyAsync=true;registeredBrowser.dailyOwnedOsSnapshot=true;
    registeredBrowser.dailyBrowserDonorProfile=true;registeredBrowser.dailyDiskMode="owned_apply";
    if(!denied || !daily::disk::ownedLeaf(browserLeaf) ||
       daily::disk::ownedQualification(unregisteredBrowser,browserLeaf,2) ||
       !daily::disk::ownedQualification(registeredBrowser,browserLeaf,21) ||
       daily::disk::ownedLeaf("100/com.huawei.hmos.browser_0") ||
       daily::disk::ownedLeaf("100/com.huawei.hmos.browser_20020064x"))
        throw std::runtime_error("fake_scope_failed");
    std::cout<<"RO_PROVIDER_PROBE_FAKE_PASS\n";return 0;
}
std::string readPlan(const std::string& path) {
    std::ifstream in(path,std::ios::binary);
    if(!in) throw std::runtime_error("browser_plan_unreadable");
    std::string text((std::istreambuf_iterator<char>(in)),std::istreambuf_iterator<char>());
    if(text.empty() || text.size()>65536) throw std::runtime_error("browser_plan_size_invalid");
    return text;
}
int browserPlanProbe(const std::string& planPath) {
    const auto plan=parseDeviceOraclePlan(readPlan(planPath));
    if(plan.schemaVersion!=3 || plan.donors.size()!=1 ||
       plan.donors.front().bundle!="com.huawei.hmos.browser")
        throw std::runtime_error("browser_plan_required");
    const auto& donor=plan.donors.front();
    const std::string leaf="100/"+donor.bundle+"_"+donor.uid;
    const auto groupPath="/dev/memcg/"+leaf;
    struct stat metadata{};
    const int group=open(groupPath.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
    if(group<0) throw std::runtime_error("browser_group_unreadable");
    const bool meta=fstat(group,&metadata)==0;close(group);
    if(!meta) throw std::runtime_error("browser_group_metadata_unknown");
    const double begin=device_proc::boot();const std::atomic<bool> stop{false};
    const auto result=sampleDeviceDailyEvidence(leaf,static_cast<std::uint64_t>(metadata.st_dev),
        static_cast<std::uint64_t>(metadata.st_ino),donor.members,donor.memberUids,stop);
    const double end=device_proc::boot();
    std::cout<<"{\"scope\":\"readonly_browser_group\",\"bundle\":\""<<donor.bundle
        <<"\",\"primary_pid\":"<<donor.pid<<",\"primary_uid\":"<<donor.uid
        <<",\"member_count\":"<<donor.members.size()<<",\"group_device\":"<<metadata.st_dev
        <<",\"group_inode\":"<<metadata.st_ino<<",\"aa_state\":\""<<result.aaState
        <<"\",\"aa_reason\":\""<<escaped(result.aaReason)<<"\",\"verified\":"
        <<(result.verified?"true":"false")<<",\"reason\":\""<<escaped(result.reason)
        <<"\",\"elapsed_ms\":"<<(end-begin)*1000<<"}\n";
    return result.verified?0:1;
}
}
int main(int argc,char** argv) {
    std::signal(SIGALRM,SIG_DFL);alarm(15);
    try {
        if(argc==2 && std::string(argv[1])=="--fake") return fake();
        if(argc==2 && std::string(argv[1])=="--wps") {
            const std::string group="/dev/memcg/100/cn.wps.office.hap_20020028";
            std::string listing;
            if(!device_proc::boundedRead(group+"/cgroup.procs",16384,false,listing))
                throw std::runtime_error("wps_members_unreadable");
            std::map<std::uint64_t,std::uint64_t> members;std::uint64_t mainPid=0,pid=0;
            std::istringstream rows(listing);
            while(rows>>pid) {
                const std::string proc="/proc/"+std::to_string(pid);std::string stat,cmd;
                if(!device_proc::boundedRead(proc+"/stat",65536,false,stat) ||
                   !device_proc::boundedRead(proc+"/cmdline",16384,true,cmd))
                    throw std::runtime_error("wps_member_identity_unreadable");
                members[pid]=singleton(std::to_string(pid),stat).second;
                if(cmd.substr(0,cmd.find('\0'))=="cn.wps.office.hap") mainPid=pid;
            }
            if(!mainPid) throw std::runtime_error("wps_main_process_absent");
            const std::atomic<bool> stop{false};const auto proof=sampleWpsWindowEvidence(members,mainPid,20020028,stop);
            std::cout<<"{\"scope\":\"readonly_wps_windows\",\"main_pid\":"<<mainPid
                <<",\"member_count\":"<<members.size()<<",\"verified\":"<<(proof.verified?"true":"false")
                <<",\"reason\":\""<<escaped(proof.reason)<<"\"}\n";
            return 0;
        }
        if(argc==3 && std::string(argv[1])=="--browser-plan") return browserPlanProbe(argv[2]);
        if(argc!=3 || std::string(argv[1])!="--leaf") throw std::runtime_error("usage: --leaf 100/owned_bundle_uid OR --fake");
        const std::string leaf=argv[2];if(!daily::disk::ownedLeaf(leaf)) throw std::runtime_error("owned_test_leaf_required");
        const double begin=device_proc::boot();if(begin<=0) throw std::runtime_error("boottime_unknown");
        const int root=open("/dev/memcg",O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
        if(root<0) throw std::runtime_error("memcg_root_unreadable");
        const int user=openat(root,"100",O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);close(root);
        if(user<0) throw std::runtime_error("memcg_user_unreadable");
        const int group=openat(user,leaf.substr(4).c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);close(user);
        if(group<0) throw std::runtime_error("configured_group_unreadable");
        struct stat metadata{};const bool meta=fstat(group,&metadata)==0;close(group);
        if(!meta) throw std::runtime_error("configured_group_metadata_unknown");
        std::string members,stat;
        if(!device_proc::boundedRead("/dev/memcg/"+leaf+"/cgroup.procs",16384,false,members)) throw std::runtime_error("members_unreadable");
        std::istringstream list(members);std::string pid;if(!(list>>pid) || pid.find_first_not_of("0123456789")!=std::string::npos)
            throw std::runtime_error("member_pid_unknown");
        if(!device_proc::boundedRead("/proc/"+pid+"/stat",65536,false,stat)) throw std::runtime_error("member_stat_unreadable");
        const auto identity=singleton(members,stat);const std::map<std::uint64_t,std::uint64_t> expected={{identity.first,identity.second}};
        const auto dev=static_cast<std::uint64_t>(metadata.st_dev),ino=static_cast<std::uint64_t>(metadata.st_ino);
        const std::atomic<bool> stop{false};const auto result=sampleDeviceDailyEvidence(leaf,dev,ino,expected,{},stop);
        const double end=device_proc::boot();
        std::cout<<"{\"scope\":\"readonly_single_owned_leaf\",\"pid\":"<<identity.first<<",\"start_ticks\":"<<identity.second
            <<",\"group_device\":"<<dev<<",\"group_inode\":"<<ino<<",\"member_count\":1,\"uid\":"<<result.uid
            <<",\"verified\":"<<(result.verified?"true":"false")<<",\"reason\":\""<<escaped(result.reason)
            <<"\",\"elapsed_ms\":"<<(end-begin)*1000<<"}\n";
        return 0; // verified=false is an observation, never a claim of acceptance.
    } catch(const std::exception& e) {std::cout<<"{\"verified\":false,\"reason\":\""<<escaped(e.what())<<"\"}\n";return 2;}
}
