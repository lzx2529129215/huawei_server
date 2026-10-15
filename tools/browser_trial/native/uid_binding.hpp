#pragma once
#include "retry_authority_native.hpp"
namespace device_retry {
inline bool bundleUidMatches(const std::string& raw,std::uint64_t expected){std::set<std::uint64_t> uids;const std::regex pattern("\"uid\"\\s*:\\s*(\\d+)");for(std::sregex_iterator i(raw.begin(),raw.end(),pattern),end;i!=end;++i){std::uint64_t n=0;if(!number((*i)[1],n))return false;uids.insert(n);}return uids==std::set<std::uint64_t>{expected};}
inline std::string bindingBytes(const std::string& plan){return "native_uid_binding_v1\n"+plan;}
inline bool bindingMatches(const std::string& raw,const std::string& plan){return raw==bindingBytes(plan);}
inline bool readUidBinding(const std::string& path,const std::string& plan){struct stat st{};if(lstat(path.c_str(),&st)||!S_ISREG(st.st_mode)||st.st_uid!=geteuid()||(st.st_mode&0777)!=0600)return false;std::string raw;return boundedRead(path,16448,false,raw)&&bindingMatches(raw,plan);}
inline bool createUidBinding(const std::string& path,const std::string& plan){const int fd=open(path.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);if(fd<0)return false;const auto raw=bindingBytes(plan);const auto n=write(fd,raw.data(),raw.size());const int sync=fsync(fd),closed=close(fd);return n==static_cast<ssize_t>(raw.size())&&!sync&&!closed;}
}
