#pragma once
namespace ms::harmony_v1 {
// Wake scheduling only: all worker, identity, freshness and reservation guards
// remain in the existing action path. Past boundaries never cause a spin.
inline double oracleAlignedPollWaitSeconds(bool deviceActive, double normalWait,
    double now, double lastSubmit, double minInterval, double launch) {
    if (!deviceActive || !(normalWait > 0 && normalWait < 1e100) ||
        !(now > 0 && now < 1e100) || !(lastSubmit > 0 && lastSubmit <= now) ||
        !(minInterval > 0 && minInterval < 1e100) || !(launch > now && launch < 1e100))
        return normalWait;
    const double untilGate = minInterval - (now - lastSubmit);
    if (!(untilGate > 0) || untilGate >= normalWait) return normalWait;
    // At most one additional near-boundary wake; never sub-millisecond polling.
    const double boundedWait = untilGate < 0.001 ? 0.001 : untilGate;
    return boundedWait < normalWait ? boundedWait : normalWait;
}
}
