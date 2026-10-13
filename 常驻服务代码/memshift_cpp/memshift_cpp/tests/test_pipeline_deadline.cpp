#include "../src/oracle_pipeline_deadline.hpp"
#include <limits>
#include <cstdio>
int main(){if(!oracleStructuralPipelineDeadlineAllows(true,98.999,1,100))return 1;if(oracleStructuralPipelineDeadlineAllows(true,99,1,100))return 2;if(oracleStructuralPipelineDeadlineAllows(true,99.858,1,100))return 3;if(!oracleStructuralPipelineDeadlineAllows(false,99.858,1,100))return 4;if(oracleStructuralPipelineDeadlineAllows(true,std::numeric_limits<double>::quiet_NaN(),1,100))return 5;if(oracleStructuralPipelineDeadlineAllows(true,99,1,std::numeric_limits<double>::infinity()))return 6;if(oracleStructuralPipelineDeadlineAllows(true,101,1,100))return 7;std::puts("PASS structural deadline 7 boundaries; no completion guarantee");return 0;}
