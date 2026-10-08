#include "io.hpp"
#include <filesystem>
#include <iostream>
#include <sys/file.h>
using namespace ms;
int main(int argc,char** argv) {
    std::string file,temporary;
    try {
        bool init=false,show=false,changed=false;
        std::optional<bool> pause;
        std::optional<std::string> protect;
        std::optional<Bytes> boost;
        Bytes ttl=30;
        for(int i=1;i<argc;++i) {
            std::string k=argv[i];
            auto val=[&]{if(i+1>=argc) throw std::runtime_error("missing value for "+k);return std::string(argv[++i]);};
            if(k=="--file") file=val();
            else if(k=="--init") init=true;
            else if(k=="--show") show=true;
            else if(k=="--pause") { pause=boolValue(val()); changed=true; }
            else if(k=="--protect") { protect=val(); changed=true; }
            else if(k=="--boost-mb") { boost=mbValue(val()); changed=true; }
            else if(k=="--ttl") ttl=uintValue(val());
            else if(k=="--help") {
                std::cout<<"memshiftctl --file FILE [--init] [--show] [--pause true|false]\n"
                         <<"            [--protect name1,name2|none] [--boost-mb N --ttl SECONDS]\n"
                         <<"Atomic merge: unspecified fields are preserved. TTL: 1..300.\n"; return 0;
            } else throw std::runtime_error("unknown option: "+k);
        }
        if(file.empty()) throw std::runtime_error("--file is required");
        if(ttl<1 || ttl>300) throw std::runtime_error("ttl must be 1..300 seconds");
        if(show && (changed || init)) throw std::runtime_error("--show cannot modify state");
        if(show || (!changed && !init)) { std::cout<<controlText(parseControl(readPath(file))); return 0; }
        FD lock(::open((file+".lock").c_str(),O_CREAT|O_RDWR|O_NOFOLLOW|O_CLOEXEC,0640));
        if(lock.get()<0 || ::flock(lock.get(),LOCK_EX)!=0) throw error("control lock");
        Control c;
        if(std::filesystem::exists(file)) c=parseControl(readPath(file));
        else if(!init) throw std::runtime_error("control file missing; use --init once");
        if(pause) c.paused=*pause;
        if(protect) {
            c.protectedApps.clear();
            if(*protect!="none") {
                std::istringstream in(*protect); std::string name;
                while(std::getline(in,name,',')) {
                    name=trim(name);
                    if(!safeName(name)) throw std::runtime_error("invalid protected app name");
                    c.protectedApps.insert(name);
                }
            }
        }
        if(boost) { c.boost=*boost; c.until=*boost?add(unixSeconds(),ttl):0; }
        const auto text=controlText(c);
        temporary=file+".tmp."+std::to_string(::getpid());
        {
            FD out(::open(temporary.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0640));
            if(out.get()<0) throw error("create control temporary file");
            std::size_t done=0;
            while(done<text.size()) {
                auto n=::write(out.get(),text.data()+done,text.size()-done);
                if(n<0 && errno==EINTR) continue;
                if(n<=0) throw error("write control file",n<0?errno:EIO);
                done+=static_cast<std::size_t>(n);
            }
            if(::fsync(out.get())!=0) throw error("fsync control file");
        }
        if(::rename(temporary.c_str(),file.c_str())!=0) throw error("rename control file");
        temporary.clear();
        std::cout<<text; return 0;
    } catch(const std::exception& e) {
        if(!temporary.empty()) ::unlink(temporary.c_str());
        std::cerr<<"ERROR: "<<e.what()<<'\n'; return 1;
    }
}
