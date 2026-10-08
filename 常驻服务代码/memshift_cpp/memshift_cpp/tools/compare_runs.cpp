#include "../src/core.hpp"
#include <fstream>
#include <iomanip>
#include <iostream>
#include <optional>
#include <set>
#include <tuple>
using namespace ms;
namespace {
struct Row {
    std::string run,sequence,caseId;
    Bytes launchFail=0,popup=0,oom=0;
    std::optional<double> latency;
};
using Key=std::pair<std::string,std::string>;
std::vector<std::string> split(const std::string& line) {
    std::vector<std::string> out; std::string field; bool quoted=false;
    for(std::size_t i=0;i<line.size();++i) {
        char c=line[i];
        if(c=='\"') {
            if(quoted && i+1<line.size() && line[i+1]=='\"') { field+='\"'; ++i; }
            else quoted=!quoted;
        } else if(c==',' && !quoted) { out.push_back(trim(field)); field.clear(); }
        else field+=c;
    }
    if(quoted) throw std::runtime_error("unterminated CSV quote");
    out.push_back(trim(field)); return out;
}
std::map<Key,Row> load(const std::string& path) {
    std::ifstream in(path); if(!in) throw std::runtime_error("cannot open "+path);
    std::string line;
    if(!std::getline(in,line) || trim(line)!="run_id,sequence,case_id,launch_fail,lowmem_popup,oom_events,launch_ms")
        throw std::runtime_error("invalid CSV header: "+path);
    std::map<Key,Row> rows;
    while(std::getline(in,line)) {
        if(trim(line).empty()) continue;
        auto a=split(line); if(a.size()!=7) throw std::runtime_error("expected seven CSV fields: "+path);
        Row r; r.run=a[0]; r.sequence=a[1]; r.caseId=a[2];
        if(r.run.empty() || r.sequence.empty() || r.caseId.empty()) throw std::runtime_error("empty pairing key");
        r.launchFail=uintValue(a[3]); r.popup=uintValue(a[4]); r.oom=uintValue(a[5]);
        if(!a[6].empty()) {
            double t=realValue(a[6]); if(t<0) throw std::runtime_error("negative launch latency"); r.latency=t;
        }
        if(!r.launchFail && !r.latency) throw std::runtime_error("successful launch needs launch_ms");
        if(!rows.emplace(Key{r.run,r.sequence},r).second) throw std::runtime_error("duplicate run_id,sequence");
    }
    if(rows.empty()) throw std::runtime_error("no measurement rows");
    return rows;
}
struct Stats {
    Bytes fails=0,popups=0,ooms=0;
    std::map<std::string,Bytes> perRun;
    std::vector<double> latencies;
    Bytes total() const { return add(add(fails,popups),ooms); }
};
Stats aggregate(const std::map<Key,Row>& rows) {
    Stats s;
    for(auto& [key,r]:rows) {
        (void)key;
        s.fails=add(s.fails,r.launchFail); s.popups=add(s.popups,r.popup); s.ooms=add(s.ooms,r.oom);
        s.perRun[r.run]=add(s.perRun[r.run],add(add(r.launchFail,r.popup),r.oom));
        if(!r.launchFail && r.latency) s.latencies.push_back(*r.latency);
    }
    std::sort(s.latencies.begin(),s.latencies.end()); return s;
}
std::string p95(const std::vector<double>& a) {
    if(a.empty()) return "N/A";
    auto i=static_cast<std::size_t>(std::ceil(0.95*static_cast<double>(a.size())))-1;
    std::ostringstream out; out<<std::fixed<<std::setprecision(2)<<a[i]; return out.str();
}
}
int main(int argc,char** argv) {
    try {
        if(argc!=3) {
            std::cerr<<"Usage: compare_runs baseline.csv optimized.csv\n"
                     <<"Pair run_id + sequence and require identical case_id. Counts must come from benchmark instrumentation.\n";
            return 1;
        }
        auto a=load(argv[1]),b=load(argv[2]);
        if(a.size()!=b.size()) throw std::runtime_error("different numbers of use cases; comparison refused");
        for(auto& [key,r]:a) {
            auto it=b.find(key);
            if(it==b.end() || it->second.caseId!=r.caseId) throw std::runtime_error("workload sequence mismatch; comparison refused");
        }
        auto x=aggregate(a),y=aggregate(b);
        std::cout<<std::fixed<<std::setprecision(2);
        std::cout<<"Matched cases per version: "<<a.size()<<"; matched runs: "<<x.perRun.size()<<'\n';
        std::cout<<"Metric                   baseline    optimized\n";
        std::cout<<"launch_fail              "<<x.fails<<"           "<<y.fails<<'\n';
        std::cout<<"lowmem_popup             "<<x.popups<<"           "<<y.popups<<'\n';
        std::cout<<"oom_events               "<<x.ooms<<"           "<<y.ooms<<'\n';
        std::cout<<"combined_events          "<<x.total()<<"           "<<y.total()<<'\n';
        std::cout<<"mean_events_per_run      "<<static_cast<double>(x.total())/static_cast<double>(x.perRun.size())
                 <<"           "<<static_cast<double>(y.total())/static_cast<double>(y.perRun.size())<<'\n';
        for(auto& [run,count]:x.perRun) std::cout<<"run "<<run<<" events: "<<count<<" -> "<<y.perRun.at(run)<<'\n';
        if(!x.total()) std::cout<<"Event reduction: N/A (baseline zero; 10% benefit cannot be established).\n";
        else {
            double reduction=100*(static_cast<double>(x.total())-static_cast<double>(y.total()))/static_cast<double>(x.total());
            std::cout<<"Measured event reduction: "<<reduction<<"%\n"
                     <<"Exploratory 10% target: "<<(reduction>=10?"reached in supplied counts":"not reached in supplied counts")<<'\n';
        }
        std::cout<<"Successful-case launch P95 ms: "<<p95(x.latencies)<<" -> "<<p95(y.latencies)<<'\n';
        if(x.total()<100 || x.perRun.size()<3)
            std::cout<<"WARNING: small/incomplete sample; not contract acceptance evidence.\n";
        std::cout<<"No statistical significance, identical-environment proof, or causality is inferred.\n"
                 <<"Successful-case P95 is descriptive: survivor sets may differ. OOM-killed processes are not automatically contract OOM events.\n";
        return 0;
    } catch(const std::exception& e) { std::cerr<<"ERROR: "<<e.what()<<'\n'; return 1; }
}
