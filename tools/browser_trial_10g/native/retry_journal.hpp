#pragma once
#include "retry_core.hpp"
#include "device_proc_reader.hpp"
#include <map>
#include <stdexcept>
namespace device_retry {
struct JsonValue {std::string text;bool string=false;};
class FlatJson {
    const std::string& raw;std::size_t pos=0;
    void ws(){while(pos<raw.size() && (raw[pos]==' ' || raw[pos]=='\t' || raw[pos]=='\r'))++pos;}
    char take(){if(pos>=raw.size())throw std::runtime_error("journal_json_eof");return raw[pos++];}
    std::string str() {
        if(take()!='"')throw std::runtime_error("journal_json_string");
        std::string out;
        for(;;) {
            const char c=take();if(c=='"')return out;
            if(static_cast<unsigned char>(c)<32)throw std::runtime_error("journal_json_control");
            if(c!='\\'){out+=c;continue;}
            const char e=take();
            if(e=='"' || e=='\\' || e=='/')out+=e;
            else if(e=='n')out+='\n';else if(e=='r')out+='\r';else if(e=='t')out+='\t';
            else if(e=='b')out+='\b';else if(e=='f')out+='\f';
            else throw std::runtime_error("journal_json_escape");
        }
    }
public:
    explicit FlatJson(const std::string& x):raw(x){}
    std::map<std::string,JsonValue> parse() {
        std::map<std::string,JsonValue> out;ws();if(take()!='{')throw std::runtime_error("journal_json_object");
        ws();if(pos<raw.size() && raw[pos]=='}'){++pos;ws();if(pos!=raw.size())throw std::runtime_error("journal_json_trailing");return out;}
        for(;;) {
            ws();const auto key=str();ws();if(take()!=':')throw std::runtime_error("journal_json_colon");ws();
            JsonValue v;
            if(pos<raw.size() && raw[pos]=='"'){v.string=true;v.text=str();}
            else {
                const auto start=pos;while(pos<raw.size() && raw[pos]!=',' && raw[pos]!='}' && raw[pos]!=' ' && raw[pos]!='\t' && raw[pos]!='\r')++pos;
                v.text=raw.substr(start,pos-start);
                if(v.text!="true" && v.text!="false" && v.text!="null") {
                    if(v.text.empty())throw std::runtime_error("journal_json_empty");
                    std::size_t i=v.text[0]=='-'?1:0;
                    if(i>=v.text.size())throw std::runtime_error("journal_json_number");
                    if(v.text[i]=='0' && i+1<v.text.size())throw std::runtime_error("journal_json_leading_zero");
                    for(;i<v.text.size();++i)if(v.text[i]<'0' || v.text[i]>'9')throw std::runtime_error("journal_json_number");
                }
            }
            if(!out.emplace(key,v).second)throw std::runtime_error("journal_json_duplicate");
            ws();const char c=take();if(c=='}')break;if(c!=',')throw std::runtime_error("journal_json_separator");
        }
        ws();if(pos!=raw.size())throw std::runtime_error("journal_json_trailing");return out;
    }
};
inline std::string stringField(const std::map<std::string,JsonValue>& fields,const std::string& key) {
    const auto it=fields.find(key);if(it==fields.end() || !it->second.string)throw std::runtime_error("journal_string_missing");
    return it->second.text;
}
inline std::uint64_t numberField(const std::map<std::string,JsonValue>& fields,const std::string& key) {
    const auto it=fields.find(key);std::uint64_t value=0;
    if(it==fields.end() || it->second.string || !ms::harmony_v1::device_proc::number(it->second.text,value))throw std::runtime_error("journal_number_missing");
    return value;
}
inline retry_core::Journal parseJournal(const std::string& peak,const std::string& daily,
                                       const std::string& attempt,std::uint64_t expectedUid,
                                       bool peakExists,bool dailyExists) {
    retry_core::Journal result;
    if(peak.size()>262144 || daily.size()>262144 || (!peakExists && !peak.empty()) ||
       (!dailyExists && !daily.empty()))throw std::runtime_error("journal_bounds");
    const auto add=[&](const std::string& raw,const std::string& expected,bool isPeak) {
        if(raw.empty())return;
        if(raw.back()!='\n' || raw.find('\0')!=std::string::npos)throw std::runtime_error("journal_truncated");
        std::istringstream lines(raw);std::string line;
        while(std::getline(lines,line)) {
            if(line.empty())continue;
            const auto fields=FlatJson(line).parse();
            if(stringField(fields,"case_id")!=expected)throw std::runtime_error("journal_wrong_attempt");
            const auto pid=numberField(fields,"pid"),start=numberField(fields,"start_ticks"),uid=numberField(fields,"uid");
            if(uid!=expectedUid)throw std::runtime_error("journal_uid");
            if(result.peakRows || result.dailyRows) {
                if(pid!=result.pid || start!=result.start || uid!=result.uid)throw std::runtime_error("journal_mixed_generation");
            } else {result.pid=pid;result.start=start;result.uid=uid;}
            if(isPeak)result.peakRows=true;else result.dailyRows=true;
            const auto event=stringField(fields,"event"),phase=stringField(fields,"phase");
            result.failed=result.failed || event=="allocation_failed";
            result.stopped=result.stopped || (event=="stopped" && phase=="failed");
        }
    };
    add(peak,attempt,true);add(daily,attempt+"_daily",false);
    result.oneGeneration=result.peakRows || result.dailyRows;
    result.uidMatches=result.oneGeneration && result.uid==expectedUid;
    // A donor intentionally has no peak journal. Its daily journal is still
    // parsed as a complete, identity-bound file for monitor-only coverage.
    // The daily snapshot is complete only when the reader obtained a whole,
    // newline-terminated file and every row parsed into the same identity.
    // boundedRead fails closed on size overflow; the parser fails closed on
    // a partial final line or mixed PID/start/UID rows.
    result.dailyComplete=dailyExists && !daily.empty() && daily.back()=='\n' &&
        result.dailyRows && result.oneGeneration && result.uidMatches;
    result.complete=peakExists && result.peakRows;
    return result;
}
}
