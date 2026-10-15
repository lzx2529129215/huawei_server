#pragma once
#include "uid_binding.hpp"
#include "retry_plan.hpp"
namespace device_retry {
template<class Gate,class Verify,class Bind>
inline void runUidPreflight(const Plan& plan,Gate gate,Verify verify,Bind bind) {
    for(const auto& target:plan.targets) {
        if(!gate())throw std::runtime_error("uid_preflight_boot_or_T");
        if(!verify(target))throw std::runtime_error("uid_preflight_failed");
    }
    if(!gate())throw std::runtime_error("uid_preflight_late_or_plan_changed");
    if(!bind())throw std::runtime_error("uid_binding_write_failed_or_exists");
    if(!gate())throw std::runtime_error("uid_preflight_binding_late_or_changed");
}
inline void nativeUidPreflight(const Plan& plan,const std::string& path,
        const std::string& bytes,const std::string& binding,std::atomic<bool>& stopped) {
    const auto gate=[&](){std::string boot,raw;const double now=device_retry::boot();
        return !stopped.load()&&std::isfinite(now)&&now>=0&&now<plan.launch&&
            readBoot(boot)&&boot==plan.bootId&&boundedRead(path,16384,false,raw)&&raw==bytes;};
    runUidPreflight(plan,gate,[&](const Target& target){
        const auto bm=command({"/bin/bm","dump","-n",target.bundle},stopped,4000,1048576);
        return bm.ok&&bm.error.empty()&&bundleUidMatches(bm.output,target.uid);
    },[&](){return createUidBinding(binding,bytes)&&readUidBinding(binding,bytes);});
}
}
