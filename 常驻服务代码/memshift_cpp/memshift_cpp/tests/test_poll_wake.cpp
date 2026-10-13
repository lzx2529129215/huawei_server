#include "../src/oracle_poll_wake.hpp"
using ms::harmony_v1::oracleAlignedPollWaitSeconds;
static bool near(double a,double b) { return a>b-0.00000001 && a<b+0.00000001; }
int main() {
 if(!near(oracleAlignedPollWaitSeconds(true,.2,100.91,100,1,130),.09)) return 1;
 if(!near(oracleAlignedPollWaitSeconds(false,.2,100.91,100,1,130),.2)) return 2;
 if(!near(oracleAlignedPollWaitSeconds(true,.2,100.5,100,1,130),.2)) return 3;
 if(!near(oracleAlignedPollWaitSeconds(true,.2,101,100,1,130),.2)) return 4;
 if(!near(oracleAlignedPollWaitSeconds(true,.2,101.2,100,1,130),.2)) return 5;
 if(!near(oracleAlignedPollWaitSeconds(true,.2,100.9999,100,1,130),.001)) return 6;
 if(!near(oracleAlignedPollWaitSeconds(true,.2,130,100,1,130),.2)) return 7;
 if(!near(oracleAlignedPollWaitSeconds(true,0,100.91,100,1,130),0)) return 8;
 if(!near(oracleAlignedPollWaitSeconds(true,.2,100.91,0,1,130),.2)) return 9;
 if(!near(oracleAlignedPollWaitSeconds(true,.2,100.91,101,1,130),.2)) return 10;
 // Simulated worker complete at .75, ordinary grid .2 after .01:
 // .81 -> 1.01 old, aligned -> 1.00. No authorization before 1s.
 double now=100.81; const double wait=oracleAlignedPollWaitSeconds(true,.2,now,100,1,130);
 if(now+wait<101 || !near(now+wait,101)) return 11;
 // A busy worker remains busy: wake helper has no authority/inflight state.
 // Repeated use after boundary returns normal sleep, preventing a busy loop.
 if(!near(oracleAlignedPollWaitSeconds(true,.2,now+wait,100,1,130),.2)) return 12;
 return 0;
}
