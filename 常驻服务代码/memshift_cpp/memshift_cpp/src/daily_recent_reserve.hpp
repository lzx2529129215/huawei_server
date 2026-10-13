#pragma once
#include "core.hpp"
#include <mutex>

namespace ms::harmony_v1::daily::recent {
// A global consumption proxy, not an allocation or physical-release estimate.
constexpr double FreshSeconds=5.0; // Worker snapshot bound; the original forecast/request expiry checks still apply.
struct Snapshot {
    Bytes target=0, signal=0, total=0;
    double sampledAt=0, expiresAt=0;
    std::uint64_t epoch=0;
    bool valid=false;
};
inline Bytes boundedTarget(const Options& o,Bytes total,Bytes signal) {
    const Bytes cap=amount(static_cast<double>(total)*o.dailyRecentCapFraction);
    return std::max(o.readyFree,std::min(cap,add(o.readyFree,signal)));
}
inline bool fresh(const Snapshot& s,double now,Bytes total) {
    return s.valid && s.total==total && std::isfinite(now) && std::isfinite(s.sampledAt) &&
        std::isfinite(s.expiresAt) && now>=s.sampledAt && now-s.sampledAt<FreshSeconds && now<=s.expiresAt;
}
inline Bytes targetAt(const Options& o,const Snapshot& s,double now,Bytes total) {
    return o.dailyRecentReserve && fresh(s,now,total)?
        std::min(s.target,boundedTarget(o,total,s.signal)):o.readyFree;
}
inline Bytes workerTarget(const Options& o,const Snapshot& reserved,const Snapshot& latest,double now,Bytes total) {
    if(!o.dailyRecentReserve || reserved.epoch!=latest.epoch) return o.readyFree;
    // The immutable request cannot inherit a later larger signal or extend its hold.
    return std::min(targetAt(o,reserved,now,total),targetAt(o,latest,now,total));
}
class Store {
    mutable std::mutex mutex_;
    std::deque<std::pair<double,Bytes>> available_,drops_;
    Snapshot snapshot_;
    double last_=0;
    Bytes total_=0;
    std::uint64_t epoch_=0;
    void reset() {available_.clear();drops_.clear();snapshot_={};last_=0;total_=0;++epoch_;}
public:
    Snapshot update(const Options& o,const System& s) {
        std::lock_guard<std::mutex> lock(mutex_);
        if(!o.dailyRecentReserve) {reset();return snapshot_;}
        if(!std::isfinite(s.time) || s.time<=0 || !s.total || s.available>s.total || s.free>s.total) {
            reset();return snapshot_;
        }
        if(last_ && (s.time<=last_ || s.time-last_>o.dailyRecentWindowSec || s.total!=total_)) reset();
        last_=s.time;total_=s.total;
        while(!available_.empty() && available_.front().first<s.time-o.dailyRecentWindowSec) available_.pop_front();
        Bytes peak=s.available;
        for(const auto& sample:available_) peak=std::max(peak,sample.second);
        const Bytes drop=sub(peak,s.available);
        available_.emplace_back(s.time,s.available);
        // H holds each observed D from its own timestamp; never wait H for a peak.
        if(drop) drops_.emplace_back(s.time,drop);
        while(!drops_.empty() && s.time-drops_.front().first>o.dailyRecentHoldSec) drops_.pop_front();
        Bytes signal=0;double expiry=s.time;
        for(const auto& sample:drops_) if(sample.second>=signal) {
            signal=sample.second;expiry=sample.first+o.dailyRecentHoldSec;
        }
        snapshot_={boundedTarget(o,s.total,signal),signal,s.total,s.time,expiry,epoch_,signal!=0};
        return snapshot_;
    }
    void invalidate() {std::lock_guard<std::mutex> lock(mutex_);reset();}
    Snapshot read() const {std::lock_guard<std::mutex> lock(mutex_);return snapshot_;}
};
} // namespace ms::harmony_v1::daily::recent
