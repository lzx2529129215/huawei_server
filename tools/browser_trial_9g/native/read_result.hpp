#pragma once
#include "snapshot_wire.hpp"
#include "retry_scheduler.hpp"
namespace device_retry {
// Main thread only. Expected command failures remain Unknown without throwing.
template<class Decode,class Unknown,class Phase>
inline CompletedRead consumeReadResult(const Command& call,std::uint64_t generation,
        const std::string& expectedBoot,Decode decode,Unknown unknown,Phase phase) {
    CompletedRead read;read.generation=generation;
    if(!call.ok) {
        read.sameBoot=true;read.known=false;
        phase(7);unknown(call.reason);return read;
    }
    try {
        phase(2);const auto snapshot=decode();
        read.known=snapshot.known;read.started=snapshot.authority.readStart;read.completed=snapshot.authority.readEnd;
        read.sameBoot=(snapshot.authority.bootBefore.empty()||snapshot.authority.bootBefore==expectedBoot)&&
            (snapshot.authority.bootAfter.empty()||snapshot.authority.bootAfter==expectedBoot);
        read.journal=snapshot.journal;read.authority=snapshot.authority.flags;
    }catch(const std::exception& error){
        read.sameBoot=true;read.known=false;
        phase(8);unknown(error.what());
    }
    return read;
}
}
