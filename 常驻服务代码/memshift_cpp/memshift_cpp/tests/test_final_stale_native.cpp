#include "../src/final_authorization.hpp"
#include "../src/harmony_v1.hpp"
#include <iostream>
using namespace ms::harmony_v1;
int main() {
 using A=FinalAuthorizationDisposition;
 const std::string s="rejected:oracle_evidence_defer:aa_evidence_stale:age_s=2.50134032:observed=6752.33408984:checked=6754.83543016";
 if(finalAuthorizationDisposition(true,true,"aa_evidence_stale",false,false)!=A::DeferFreshness) return 1;
 if(!oracleUnsubmittedEvidenceMayDefer(s,false,false,false)) return 2;
 if(oracleUnsubmittedEvidenceMayDefer(s,true,false,false)) return 3;
 if(oracleUnsubmittedEvidenceMayDefer(s,false,true,false)) return 4;
 if(oracleUnsubmittedEvidenceMayDefer(s,false,false,true)) return 5;
 if(finalAuthorizationDisposition(true,false,"aa_evidence_stale",false,false)!=A::Reject) return 6;
 if(finalAuthorizationDisposition(false,true,"aa_evidence_stale",false,false)!=A::Reject) return 7;
 if(oracleUnsubmittedEvidenceMayDefer("rejected:preparation_final_authorization_rejected:identity_changed",false,false,false)) return 8;
 if(finalAuthorizationDisposition(true,true,"",false,false)!=A::Allow) return 9;
 std::cout<<"FINAL_STALE_PRODUCTION_ROUTING_COMPLETE checks=9\n";
 return 0;
}
