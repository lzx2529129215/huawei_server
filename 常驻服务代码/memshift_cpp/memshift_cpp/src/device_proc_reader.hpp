#pragma once
#include <cerrno>
#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <set>
#include <sstream>
#include <string>
#include <vector>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

namespace ms::harmony_v1::device_proc {
struct Donor {std::string bundle;std::uint64_t pid=0,uid=0,start=0;};
inline double boot(){timespec t{};if(clock_gettime(CLOCK_BOOTTIME,&t)!=0)return -1;return static_cast<double>(t.tv_sec)+static_cast<double>(t.tv_nsec)/1e9;}
inline bool number(const std::string& text,std::uint64_t& result){if(text.empty() || text.find_first_not_of("0123456789")!=std::string::npos)return false;errno=0;char* end=nullptr;result=std::strtoull(text.c_str(),&end,10);return !errno && end && !*end && result>0;}
inline bool bundleAllowed(const std::string& b){for(unsigned i=1;i<=16;++i)if(b=="io.memscene.scene4.workload.a"+std::to_string(i))return true;return false;}
inline bool caseAllowed(const std::string& c){if(c.size()<12 || c.rfind("s4_",0)!=0)return false;const auto pos=c.find("_c",3);if(pos==std::string::npos || pos==3 || c.size()!=pos+6)return false;return c.substr(3,pos-3).find_first_not_of("0123456789abcdef")==std::string::npos && c.substr(pos+2).find_first_not_of("0123456789")==std::string::npos;}
inline bool boundedRead(const std::string& path,std::size_t limit,bool nulAllowed,std::string& out){out.clear();const int fd=open(path.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return false;char buffer[4096];bool ok=true;for(;;){const auto n=read(fd,buffer,sizeof(buffer));if(n<0){if(errno==EINTR)continue;ok=false;break;}if(n==0)break;if(out.size()+static_cast<std::size_t>(n)>limit){ok=false;break;}out.append(buffer,static_cast<std::size_t>(n));}close(fd);return ok && !out.empty() && (nulAllowed || out.find('\0')==std::string::npos);}
inline bool statMatches(const std::string& raw,const Donor& d){const auto split=raw.rfind(')');const auto first=raw.find(' ');if(split==std::string::npos || first==std::string::npos)return false;std::uint64_t pid=0;if(!number(raw.substr(0,first),pid) || pid!=d.pid)return false;std::istringstream in(raw.substr(split+1));std::string token;for(unsigned i=0;i<=19;++i)if(!(in>>token))return false;std::uint64_t ticks=0;return number(token,ticks) && ticks==d.start;}
inline bool statusMatches(const std::string& raw,const Donor& d){std::istringstream rows(raw);std::string line;bool pidOk=false,uidOk=false;while(std::getline(rows,line)){std::istringstream in(line);std::string key;in>>key;if(key=="Pid:"){std::string text,extra;std::uint64_t n=0;if(!(in>>text) || (in>>extra) || !number(text,n) || n!=d.pid)return false;pidOk=true;}if(key=="Uid:"){for(unsigned i=0;i<4;++i){std::string text;std::uint64_t n=0;if(!(in>>text) || !number(text,n) || n!=d.uid)return false;}std::string extra;if(in>>extra)return false;uidOk=true;}}return pidOk && uidOk;}
inline bool membersMatch(const std::string& raw,const Donor& d){std::istringstream in(raw);std::string text,extra;std::uint64_t pid=0;return (in>>text) && !(in>>extra) && number(text,pid) && pid==d.pid;}
inline bool identity(const Donor& d,const std::string& before,const std::string& status,const std::string& cmd,const std::string& cg,const std::string& members,const std::string& after){if(!statMatches(before,d) || !statMatches(after,d) || !statusMatches(status,d) || !membersMatch(members,d))return false;const std::string leaf="/"+d.bundle+"_"+std::to_string(d.uid);const auto pos=cg.find(leaf);return cmd.find(d.bundle)!=std::string::npos && pos!=std::string::npos && (pos+leaf.size()==cg.size() || cg[pos+leaf.size()]=='\n');}
inline std::string fakeStat(std::uint64_t pid,std::uint64_t start){std::string out=std::to_string(pid)+" (owned app)";for(unsigned i=0;i<20;++i)out+=" "+(i==0?std::string("S"):i==19?std::to_string(start):std::string("1"));return out;}
inline int selftest(){Donor d{"io.memscene.scene4.workload.a1",17,23,91};const auto stat=fakeStat(17,91);const std::string status="Pid:\t17\nUid:\t23 23 23 23\n";const std::string cg="1:memory:/100/"+d.bundle+"_23\n";if(!identity(d,stat,status,d.bundle,cg,"17\n",stat))return 1;if(identity(d,stat,status,d.bundle,cg,"",stat) || identity(d,stat,status,d.bundle,cg,"17 18",stat) || identity(d,stat,status,d.bundle,cg,"17",fakeStat(17,92)) || identity(d,stat,"Pid: 17\nUid: 24 24 24 24\n",d.bundle,cg,"17",stat) || identity(d,stat,status,"other",cg,"17",stat))return 2;std::uint64_t n=0;if(number("18446744073709551616",n) || number("-1",n) || bundleAllowed("io.memscene.scene4.workload.a17") || caseAllowed("../../file") || !caseAllowed("s4_abcdef_c0000"))return 3;std::string raw;if(boundedRead("/proc/self/status",1,false,raw) || boundedRead("/not_existing_owned_test",64,false,raw))return 4;std::puts("PROC_SNAPSHOT_SELFTEST_PASS normal missing identity pid_generation uid members cmdline overflow truncation range");return 0;}
inline void section(const std::string& label,const std::string& value){std::printf("%s\n",label.c_str());if(!value.empty()){std::fwrite(value.data(),1,value.size(),stdout);if(value.back()!='\n')std::putchar('\n');}}

}
