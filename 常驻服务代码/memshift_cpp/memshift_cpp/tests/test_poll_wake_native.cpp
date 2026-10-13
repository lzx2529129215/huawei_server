#include "../src/oracle_poll_wake.hpp"
#include "../src/harmony_v1.hpp"
#include <iostream>
using namespace ms::harmony_v1;
int main() {
 const double wake=100.91+oracleAlignedPollWaitSeconds(true,.2,100.91,100,1,130);
 if(wake<101 || !oracleMayReserveAction(false,wake,100,1,130)) return 1;
 if(oracleMayReserveAction(true,wake,100,1,130)) return 2;
 if(oracleMayReserveAction(false,100.999,100,1,130)) return 3;
 if(oracleMayReserveAction(false,130,100,1,130)) return 4;
 if(oracleMayReserveAction(false,131,100,1,130)) return 5;
 if(oracleAlignedPollWaitSeconds(false,.2,100.91,100,1,130)!=.2) return 6;
 if(oracleAlignedPollWaitSeconds(true,.2,101,100,1,130)!=.2) return 7;
 if(oracleAlignedPollWaitSeconds(true,.2,130,100,1,130)!=.2) return 8;
 if(oracleAlignedPollWaitSeconds(true,.2,100.9999,100,1,130)<.001) return 9;
 std::cout<<"SUBMIT_ALIGNED_PRODUCTION_GATES_COMPLETE checks=9\n";
 return 0;
}
