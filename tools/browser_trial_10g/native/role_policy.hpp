#pragma once
#include "retry_plan.hpp"

namespace device_retry {
inline bool maySubmitInitial(const Plan& plan,unsigned index) {
    return index<plan.targets.size() && (!plan.fixedBackgroundRoles || plan.targets[index].retryTarget);
}
inline bool maySubmitRetry(const Plan& plan,unsigned index) {
    return index<plan.targets.size() && (!plan.fixedBackgroundRoles || plan.targets[index].retryTarget);
}
inline bool monitorOnlyRole(const Plan& plan,unsigned index) {
    return plan.fixedBackgroundRoles && index<plan.targets.size() && !plan.targets[index].retryTarget;
}
inline const char* roleName(const Plan& plan,unsigned index) {
    if(index>=plan.targets.size())return "out_of_scope";
    if(!plan.fixedBackgroundRoles)return "legacy_target";
    return plan.targets[index].retryTarget?"peak_target":"background_donor";
}
inline const char* initialDisposition(const Plan& plan,unsigned index) {
    return maySubmitInitial(plan,index)?"initial_pending":"initial_monitor_only";
}
}
