#pragma once
#include <cmath>
inline bool oracleStructuralPipelineDeadlineAllows(bool pipelinePrepare,double checkedAt,double interval,double deadline) {
 if(!pipelinePrepare)return true;
 if(!std::isfinite(checkedAt)||!std::isfinite(interval)||!std::isfinite(deadline)||checkedAt<0||interval<=0)return false;
 return !pipelinePrepare || checkedAt+interval<deadline;
}
