#include "../src/final_authorization.hpp"
using namespace ms::harmony_v1;
extern "C" int main() {
 using A=FinalAuthorizationDisposition;
 if(finalAuthorizationDisposition(true,true,"",false,false)!=A::Allow)return 1;
 if(finalAuthorizationDisposition(true,true,"aa_evidence_stale",false,false)!=A::DeferFreshness)return 2;
 if(finalAuthorizationDisposition(true,true,"",false,false)!=A::Allow)return 3;
 if(finalAuthorizationDisposition(true,false,"aa_evidence_stale",false,false)!=A::Reject)return 4;
 if(finalAuthorizationDisposition(false,true,"aa_evidence_stale",false,false)!=A::Reject)return 5;
 if(finalAuthorizationDisposition(true,true,"aa_telemetry_missing",false,false)!=A::Reject)return 6;
 if(finalAuthorizationDisposition(true,true,"aa_evidence_stale",true,false)!=A::Reject)return 7;
 if(finalAuthorizationDisposition(true,true,"aa_evidence_stale",false,true)!=A::Reject)return 8;
 if(finalAuthorizationDisposition(true,true,"aa_timestamp_in_future",false,false)!=A::Reject)return 9;
 return 0;
}
