#pragma once
#include "retry_local_io.hpp"
#include <iomanip>
namespace device_retry {
inline std::string hex(const std::string& raw) {
    static const char* digits="0123456789abcdef";std::string out;out.reserve(raw.size()*2);
    for(const unsigned char c:raw){out+=digits[c>>4];out+=digits[c&15];}return out;
}
inline std::string unhex(const std::string& raw) {
    if(raw.size()%2)throw std::runtime_error("wire_hex_length");std::string out;
    for(std::size_t i=0;i<raw.size();i+=2) {
        unsigned value=0;
        for(unsigned j=0;j<2;++j) {
            const auto pos=std::string("0123456789abcdef").find(raw[i+j]);
            if(pos==std::string::npos)throw std::runtime_error("wire_hex");
            value=value*16+static_cast<unsigned>(pos);
        }
        out+=static_cast<char>(value);
    }
    return out;
}
inline std::string ids(const std::vector<std::uint64_t>& values) {
    std::string out;for(const auto value:values){if(!out.empty())out+=' ';out+=std::to_string(value);}return out;
}
inline std::string encodeSnapshot(const Snapshot& s) {
    std::ostringstream out;out<<std::setprecision(17);
    out<<"device_retry_snapshot_v1\nknown="<<s.known<<"\nreason="<<hex(s.reason)
       <<"\nstart="<<s.authority.readStart<<"\nend="<<s.authority.readEnd
       <<"\nboot_pre="<<s.authority.bootBefore<<"\nboot_post="<<s.authority.bootAfter
       <<"\nabsent="<<s.authority.flags.absent<<"\npids="<<ids(s.authority.pids)
       <<"\nmembers="<<ids(s.authority.members)<<"\npeak="<<hex(s.peak)
       <<"\ndaily="<<hex(s.daily)<<"\ncomplete="<<s.journal.complete
       <<"\nidentity="<<s.authority.flags.identityMatches
       <<"\nsingle="<<s.authority.flags.singlePidAndMember<<"\nEND\n";
    return out.str();
}
inline Snapshot decodeSnapshot(const std::string& raw,const Plan& plan,unsigned index,const std::string& attempt) {
    if(raw.size()>1100000 || raw.empty() || raw.back()!='\n')throw std::runtime_error("wire_bounds");
    std::istringstream in(raw);std::string line;
    if(!std::getline(in,line) || line!="device_retry_snapshot_v1")throw std::runtime_error("wire_header");
    std::map<std::string,std::string> fields;bool ended=false;
    while(std::getline(in,line)) {
        if(line=="END"){ended=true;break;}
        const auto split=line.find('=');
        if(split==std::string::npos || !fields.emplace(line.substr(0,split),line.substr(split+1)).second)throw std::runtime_error("wire_fields");
    }
    if(!ended || std::getline(in,line) || fields.size()!=14)throw std::runtime_error("wire_incomplete");
    const auto field=[&](const std::string& key)->const std::string&{return fields.at(key);};
    const auto boolean=[&](const std::string& key){const auto& v=field(key);if(v!="0" && v!="1")throw std::runtime_error("wire_boolean");return v=="1";};
    const auto real=[&](const std::string& key){std::size_t used=0;const auto value=std::stod(field(key),&used);if(used!=field(key).size() || !std::isfinite(value))throw std::runtime_error("wire_time");return value;};
    Snapshot s;s.known=boolean("known");s.reason=unhex(field("reason"));
    s.authority.readStart=real("start");s.authority.readEnd=real("end");
    s.authority.bootBefore=field("boot_pre");s.authority.bootAfter=field("boot_post");
    const auto parseIds=[&](const std::string& key){std::vector<std::uint64_t> out;std::istringstream values(field(key));std::string text;while(values>>text){std::uint64_t n=0;if(!number(text,n))throw std::runtime_error("wire_pid");out.push_back(n);}return out;};
    s.authority.pids=parseIds("pids");s.authority.members=parseIds("members");
    s.peak=unhex(field("peak"));s.daily=unhex(field("daily"));
    s.journal=parseJournal(s.peak,s.daily,attempt,plan.targets.at(index).uid,boolean("complete"),!s.daily.empty());
    s.authority.flags.known=s.known;s.authority.flags.absent=boolean("absent");
    s.authority.flags.identityMatches=boolean("identity");s.authority.flags.singlePidAndMember=boolean("single");
    if(s.known && (s.authority.bootBefore!=plan.bootId || s.authority.bootAfter!=plan.bootId ||
       s.authority.readStart<0 || s.authority.readEnd<s.authority.readStart ||
       s.authority.readEnd-s.authority.readStart>8))throw std::runtime_error("wire_identity_time");
    return s;
}
}
