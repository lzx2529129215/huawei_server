#include "../src/harmony_v1.hpp"
#include <fstream>
#include <iterator>
#include <iostream>
#include <stdexcept>

namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
std::string procStatText(std::uint64_t pid,std::uint64_t startTicks) {
    const std::vector<std::string> fields={"S","1","2","3","4","5","6","123","8","7",
        "10","11","12","13","14","15","16","17","18",std::to_string(startTicks)};
    std::string text=std::to_string(pid)+" (scene4 donor)";
    for(const auto& field:fields) text+=" "+field;
    return text;
}
std::string statusText(std::uint64_t pid,std::uint64_t uid,std::uint64_t rssAnonKiB) {
    return "Name:\tdonor\nPid:\t"+std::to_string(pid)+"\nUid:\t"+std::to_string(uid)+" "+std::to_string(uid)+" "+
        std::to_string(uid)+" "+std::to_string(uid)+"\nRssAnon:\t"+
        std::to_string(rssAnonKiB)+" kB\nVmSwap:\t0 kB\n";
}
}

int main(int argc, char** argv) {
    using namespace ms;
    using namespace ms::harmony_v1;
    try {
        if (argc == 3 && std::string(argv[1]) == "--validate-oracle-schedule") {
            std::ifstream input(argv[2], std::ios::binary);
            if (!input) throw std::runtime_error("cannot open oracle schedule input");
            const std::string text{std::istreambuf_iterator<char>(input),
                                   std::istreambuf_iterator<char>()};
            validateOracleScheduleText(text);
            std::cout << "oracle schedule parser accepted input\n";
            return 0;
        }
        if (argc != 1) throw std::runtime_error("usage: test_harmony_v1 [--validate-oracle-schedule path]");
        const auto stat = parseVendorStat(
            "inactive_anon 34190\n"
            "inactive_file 842395648\n"
            "Anon: 139168 kB\n"
            "File: 0 kB\n"
            "sync_compress: software compress/decompress\n"
            "Zram: 105752 kB\n"
            "Eswap: 7 kB\n");
        require(stat.anonKiB && *stat.anonKiB == 139168, "Anon unit parsing failed");
        require(stat.zramKiB && *stat.zramKiB == 105752, "Zram line parser lost alignment");
        require(stat.eswapKiB && *stat.eswapKiB == 7, "Eswap unit parsing failed");
        require(!parseVendorStat("Anon: 123\nZram: nope kB\n").anonKiB, "unitless Anon must stay unknown");

        const std::string oracleBaseConfig=
            "[daemon]\ncgroup_root=/sys/fs/cgroup\nproc_root=/proc\nlog_dir=./logs\nlock_file=./lock\n"
            "control_file=./control\noracle_mode=true\noracle_schedule_file=./schedule\noracle_ledger_file=./ledger\n"
            "oracle_global_cap_mb=4096\noracle_case_cap_mb=4096\noracle_app_cap_mb=2048\n"
            "[app io.memscene.scene4.workload.a1]\npath=apps/io.memscene.scene4.workload.a1_20020188\nreclaim=true\nmin_keep_mb=512\n"
            "[app io.memscene.scene4.workload.a2]\npath=apps/io.memscene.scene4.workload.a2_20020189\nreclaim=true\nmin_keep_mb=512\n";
        const auto oracleDefaults=parseConfig(oracleBaseConfig);
        require(oracleDefaults.options.oracleBatch==512*MiB && oracleDefaults.options.oracleMaxRatio==30 &&
            oracleDefaults.options.oracleFeedbackDelayMs==0 &&
            oracleDefaults.options.oracleRatioSemantics=="resident_incremental",
            "oracle tuning defaults must preserve prior limits and immediate feedback");
        const auto oracleTuned=parseConfig(
            "[daemon]\ncgroup_root=/sys/fs/cgroup\nproc_root=/proc\nlog_dir=./logs\nlock_file=./lock\n"
            "control_file=./control\noracle_mode=true\noracle_schedule_file=./schedule\noracle_ledger_file=./ledger\n"
            "oracle_batch_mb=1792\noracle_max_ratio=90\noracle_feedback_delay_ms=5000\noracle_ratio_semantics=cumulative_anon_watermark\n"
            "oracle_global_cap_mb=4096\noracle_case_cap_mb=4096\noracle_app_cap_mb=2048\n"
            "[app io.memscene.scene4.workload.a1]\npath=apps/io.memscene.scene4.workload.a1_20020188\nreclaim=true\nmin_keep_mb=512\n"
            "[app io.memscene.scene4.workload.a2]\npath=apps/io.memscene.scene4.workload.a2_20020189\nreclaim=true\nmin_keep_mb=512\n");
        require(oracleTuned.options.oracleBatch==1792*MiB && oracleTuned.options.oracleMaxRatio==90 &&
            oracleTuned.options.oracleFeedbackDelayMs==5000 &&
            oracleTuned.options.oracleRatioSemantics=="cumulative_anon_watermark",
            "isolated oracle tuning keys did not parse as configured");
        require(oracleRatioForCap(448*MiB,2332590080,30)==20 &&
            oracleRatioForCap(1216*MiB,2332590080,50)==50 &&
            oracleRatioForCap(1648*MiB,2332590080,70)==70,
            "oracle ratio did not reflect the configured cap and ceiling");
        require(oracleRatioCanAdvance(0,70) && oracleRatioCanAdvance(50,70) &&
            !oracleRatioCanAdvance(70,70) && !oracleRatioCanAdvance(70,50) &&
            !oracleRatioCanAdvance(50,0),
            "oracle must only advance ratio after positive verified feedback");
        const auto watermark=oracleCumulativeWatermarkForCap(2300*MiB,900*MiB,1724*MiB,90);
        require(watermark.ratio==82 && watermark.totalAnonProxyBytes==3200*MiB &&
            watermark.currentVmSwapBytes==900*MiB && watermark.estimatedIncrementBytes<=1724*MiB &&
            watermark.estimatedRetainedBytes>=576*MiB,
            "cumulative watermark plan must fit the capped estimated increment and keep reserve");
        const auto alreadyAtWatermark=oracleCumulativeWatermarkForCap(950*MiB,2240*MiB,1500*MiB,70);
        require(alreadyAtWatermark.ratio==0 && alreadyAtWatermark.estimatedIncrementBytes==0,
            "a reached/lower cumulative target must not create another action");
        const auto zeroSwap=oracleCumulativeWatermarkForCap(2300*MiB,0,1000*MiB,90);
        require(zeroSwap.ratio>0 && zeroSwap.estimatedIncrementBytes<=1000*MiB,
            "zero current swap still needs a cap-bounded watermark estimate");
        require(oracleFreshnessFailure(true,10.0,{{"aa",7.0},{"window",9.0}})=="aa_evidence_stale" &&
            oracleFreshnessFailureRetryable("aa_evidence_stale") &&
            oracleFreshnessFailureRetryable("memory_current_telemetry_missing") &&
            !oracleFreshnessFailureRetryable("proc_start_ticks_changed") &&
            !oracleFreshnessFailureRetryable("window_focused") &&
            oracleFreshnessFailure(false,10.0,{{"aa",9.9}})=="identity_or_background_invariant_failed",
            "oracle freshness defer classification weakened an identity/background guard");
        auto invalidOracleConfig=oracleBaseConfig;
        invalidOracleConfig.insert(invalidOracleConfig.find("[app "),"oracle_max_ratio=91\n");
        bool invalidOracleRatioRejected=false;
        try { (void)parseConfig(invalidOracleConfig); }
        catch(const std::runtime_error&) { invalidOracleRatioRejected=true; }
        require(invalidOracleRatioRejected,"oracle ratio above the hard ceiling was accepted");

        const auto procMemory=parseProcMemory(
            "VmRSS: 1200 kB\nVmSwap: 80 kB\nRssAnon: 700 kB\nRssFile: 500 kB\n",
            "Pss: 1000 kB\nPss_Anon: 650 kB\nPss_File: 350 kB\n"
            "Private_Clean: 10 kB\nPrivate_Dirty: 15 kB\nSwap: 20 kB\n");
        require(procMemory.rssKiB && *procMemory.rssKiB==1200, "proc status RSS parser failed");
        require(procMemory.vmSwapKiB && *procMemory.vmSwapKiB==80, "proc status VmSwap parser failed");
        require(procMemory.pssKiB && *procMemory.pssKiB==1000, "smaps_rollup PSS parser failed");
        require(procMemory.privateKiB && *procMemory.privateKiB==25, "smaps private-page parser failed");
        require(procMemory.smapsSwapKiB && *procMemory.smapsSwapKiB==20, "smaps swap parser failed");

        std::vector<std::string> procFields={"S","1","2","3","4","5","6","123","8","7",
            "10","11","12","13","14","15","16","17","18","987"};
        std::string procStat="4242 (name with ) inside)";
        for(const auto& field:procFields) procStat+=" "+field;
        const auto parsedProcStat=parseProcStat(4242,procStat);
        require(parsedProcStat.pid==4242 && parsedProcStat.startTicks==987, "proc stat identity parser failed");
        require(parsedProcStat.minflt==123 && parsedProcStat.majflt==7, "proc stat fault parser failed");
        bool wrongProcStatPidRejected=false;
        try { (void)parseProcStat(4242,procStatText(4243,987)); }
        catch(const std::runtime_error&) { wrongProcStatPidRejected=true; }
        require(wrongProcStatPidRejected,"proc stat text with a different leading PID was accepted");

        const std::uint64_t donorPid=4242, donorUid=20020188, donorTicks=987;
        const std::string donorLeaf="apps/io.memscene.scene4.workload.a1_20020188";
        const std::string donorCgroup="4:memory:/"+donorLeaf+"\n";
        auto makeSample=[&](std::uint64_t samplePid,std::uint64_t sampleUid,
                            std::uint64_t ticks,std::uint64_t anonKiB) {
            const auto stat=procStatText(samplePid,ticks);
            return parseOracleRssSample(samplePid,sampleUid,ticks,donorLeaf,stat,
                statusText(samplePid,sampleUid,anonKiB),donorCgroup,stat);
        };
        const auto before=makeSample(donorPid,donorUid,donorTicks,1024);
        const auto smallDrop=makeSample(donorPid,donorUid,donorTicks,1023);
        require(before && smallDrop,"same-identity bracketed RSS samples rejected");
        auto feedback=compareOracleRssSamples(donorPid,donorUid,donorTicks,before,smallDrop);
        require(feedback.identityVerified && feedback.positiveDrop && feedback.signedDeltaKiB==1 &&
            feedback.dropBytes==1024,"small same-identity RSS decrease was clipped");
        require(oracleFeedbackMayContinue(
            "write_accepted_feedback_measured_positive_rss_observation",
            feedback.identityVerified,feedback.signedDeltaKiB,true),
            "verified positive same-identity RSS observation did not pass continuation gate");
        const auto equal=makeSample(donorPid,donorUid,donorTicks,1024);
        feedback=compareOracleRssSamples(donorPid,donorUid,donorTicks,before,equal);
        require(feedback.identityVerified && !feedback.positiveDrop && feedback.signedDeltaKiB==0 &&
            feedback.dropBytes==0,"zero RSS delta was not represented as zero");
        require(!oracleFeedbackMayContinue("write_accepted_feedback_nonpositive_halt",
            feedback.identityVerified,feedback.signedDeltaKiB,true),
            "zero RSS feedback must halt further event actions");
        const auto growth=makeSample(donorPid,donorUid,donorTicks,1029);
        feedback=compareOracleRssSamples(donorPid,donorUid,donorTicks,before,growth);
        require(feedback.identityVerified && !feedback.positiveDrop && feedback.signedDeltaKiB==-5 &&
            feedback.dropBytes==0,"RSS growth was not represented with a signed delta");
        require(!oracleFeedbackMayContinue("write_accepted_feedback_nonpositive_halt",
            feedback.identityVerified,feedback.signedDeltaKiB,true),
            "RSS growth feedback must halt further event actions");
        require(!parseOracleRssSample(donorPid,donorUid,donorTicks,donorLeaf,
            procStatText(donorPid,donorTicks),statusText(donorPid,donorUid+1,1024),donorCgroup,
            procStatText(donorPid,donorTicks)),"mismatched donor UID accepted");
        require(!parseOracleRssSample(donorPid,donorUid,donorTicks,donorLeaf,
            procStatText(donorPid,donorTicks),statusText(donorPid,donorUid,1024),donorCgroup,
            procStatText(donorPid,donorTicks+1)),"PID replacement between stat brackets accepted");
        require(!parseOracleRssSample(donorPid,donorUid,donorTicks,donorLeaf,
            procStatText(donorPid+1,donorTicks),statusText(donorPid,donorUid,1024),donorCgroup,
            procStatText(donorPid,donorTicks)),"wrong PID in pre-sample stat text accepted");
        require(!parseOracleRssSample(donorPid,donorUid,donorTicks,donorLeaf,
            procStatText(donorPid,donorTicks),statusText(donorPid,donorUid,1024),donorCgroup,
            procStatText(donorPid+1,donorTicks)),"wrong PID in post-sample stat text accepted");
        require(!parseOracleRssSample(donorPid,donorUid,donorTicks,donorLeaf,
            procStatText(donorPid,donorTicks),statusText(donorPid+1,donorUid,1024),donorCgroup,
            procStatText(donorPid,donorTicks)),"wrong PID in proc status accepted");
        require(!parseOracleRssSample(donorPid,donorUid,donorTicks,donorLeaf,
            procStatText(donorPid,donorTicks),statusText(donorPid,donorUid,1024),"4:memory:/apps/other\n",
            procStatText(donorPid,donorTicks)),"foreign memory cgroup membership accepted");
        feedback=compareOracleRssSamples(donorPid,donorUid,donorTicks,before,std::nullopt);
        require(!feedback.identityVerified && !feedback.positiveDrop,
            "missing post-write RSS sample was accepted as measured feedback");
        require(!oracleFeedbackMayContinue("write_accepted_feedback_unknown",
            feedback.identityVerified,feedback.signedDeltaKiB,false),
            "missing post-write sample must halt further event actions");
        feedback=compareOracleRssSamples(donorPid,donorUid,donorTicks,before,
            OracleRssSample{donorPid+1,donorUid,donorTicks,900,std::nullopt});
        require(!feedback.identityVerified,"different PID post sample was accepted");
        feedback=compareOracleRssSamples(donorPid,donorUid,donorTicks,before,
            OracleRssSample{donorPid,donorUid,donorTicks,18446744073709552ULL,std::nullopt});
        require(!feedback.identityVerified && !feedback.positiveDrop,
            "RSS delta that would overflow byte conversion was accepted");
        validateOracleActionLedgerHeader(oracleActionLedgerHeaderV2());
        bool ledgerRejected=false;
        try { validateOracleActionLedgerHeader("action_id,event_id,case_id,donor_bundle,pid,uid,start_ticks,trigger_boottime_s,planned_needed_bytes,remaining_bytes,input_bytes,requested_ratio,nominal_bytes,write_length,errno,elapsed_ms,post_rssanon_kib,observed_drop_bytes,status,deadline_overlap,background_evidence_source,aa_observed_boottime_s,window_observed_boottime_s,proc_observed_boottime_s,lifecycle_observed_boottime_s"); } catch(const std::runtime_error&) { ledgerRejected=true; }
        require(ledgerRejected,"legacy ledger header accepted for feedback-v2 append");
        validateOracleActionTimingLedgerHeaderV3(oracleActionTimingLedgerHeaderV3());
        bool timingLedgerRejected=false;
        try { validateOracleActionTimingLedgerHeaderV3(oracleActionLedgerHeaderV2()); }
        catch(const std::runtime_error&) { timingLedgerRejected=true; }
        require(timingLedgerRejected,"feedback V2 ledger header accepted for timing V3 append");
        require(!oracleWriteCrossedDeadline(10.0,10.0,10.0),"write ending exactly at deadline counted as crossing");
        require(oracleWriteCrossedDeadline(9.9,10.1,10.0),"write crossing deadline was not identified");
        require(!oracleWriteCrossedDeadline(10.1,10.2,10.0),"write wholly after deadline counted as crossing");
        require(!oracleFeedbackCrossedDeadline(10.0,10.0),"feedback ending exactly at deadline counted as crossing");
        require(oracleFeedbackCrossedDeadline(10.1,10.0),"feedback completing after deadline was not identified");
        bool invalidTimingRejected=false;
        try { (void)oracleWriteCrossedDeadline(2.0,1.0,3.0); }
        catch(const std::runtime_error&) { invalidTimingRejected=true; }
        require(invalidTimingRejected,"inverted syscall timing was accepted");

        require(hasMemoryController("4:memory:/\n"), "legacy memory hierarchy was not recognized");
        require(hasMemoryController("2:cpu,cpuacct:/x\n4:memory:/x\n"), "controller list was not parsed");
        require(!hasMemoryController("0::/\n"), "unified v2 membership must not imply the memory controller");

        const auto mounts = parseMounts("none /dev/memcg cgroup rw,relatime 0 0\n"
                                        "none /a\\040b cgroup rw 0 0\n"
                                        "cgroup2 /sys/fs/cgroup cgroup2 rw 0 0\n");
        require(mounts.size() == 3, "mount table parsing failed");
        require(mounts[1].mountPoint == "/a b", "mount escape decoding failed");
        require(mounts[2].fsType == "cgroup2", "cgroup2 fs type was lost");

        validateBackendRequest("linux-v2", false);
        validateBackendRequest("harmony-v1", false);
        validateBackendRequest("harmony-v1-ratio-probe", false);
        validateBackendRequest("harmony-v1-ratio-probe", true);
        bool rejected = false;
        try { validateBackendRequest("harmony-v1", true); } catch (const std::runtime_error&) { rejected = true; }
        require(rejected, "harmony-v1 apply must be rejected");
        rejected = false;
        try { validateBackendRequest("unknown", false); } catch (const std::runtime_error&) { rejected = true; }
        require(rejected, "unknown backend must be rejected");
        require(ratioForBudget(0,128*MiB)==0, "zero budget must not issue ratio");
        require(ratioForBudget(1*MiB,128*MiB)==0, "fractional percent must floor to zero");
        require(ratioForBudget(3*MiB,128*MiB)==2, "ratio conversion must floor");
        require(ratioForBudget(64*MiB,128*MiB)==20, "ratio conversion must cap at 20 percent");
        require(safeRelative("100/com.huawei.hmos.browser_20020064"), "valid device app path rejected");
        require(!safeRelative("../system"), "parent traversal app path accepted");
        const std::string donorFields="bundle|uid|pid|start_ticks|cold_ready|window_ready|cold_ready_boottime_s|background_since_s|last_heartbeat_s|aa_pid|aa_uid|aa_state|aa_observed_boottime_s|window_visible|window_focused|window_observed_boottime_s|proc_verified|proc_start_ticks|proc_observed_boottime_s|lifecycle_clear|lifecycle_observed_boottime_s|background_evidence_source";
        const auto prefixed=[](const std::string& prefix,const std::string& fields) { std::istringstream names(fields); std::ostringstream result; std::string name; bool first=true; while(std::getline(names,name,'|')) { if(!first) result<<'|'; result<<prefix<<name; first=false; } return result.str(); };
        const std::string scheduleHeader="event_id|case_id|launch_boottime_s|planned_needed_bytes|"+prefixed("donor_a_",donorFields)+"|"+prefixed("donor_b_",donorFields)+"\n";
        const std::string donorA="io.memscene.scene4.workload.a1|20020188|101|1234|true|true|900|910|999|101|20020188|BACKGROUND|998|false|false|998|true|1234|998|true|998|os_snapshot";
        const std::string donorB="io.memscene.scene4.workload.a2|20020189|102|1235|true|true|901|911|999|102|20020189|BACKGROUND|998|false|false|998|true|1235|998|true|998|os_snapshot";
        const std::string validSchedule="evt001|case001|1000|40265318400|"+donorA+"|"+donorB+"\n";
        validateOracleScheduleText(scheduleHeader+validSchedule);
        rejected=false;
        try { validateOracleScheduleText(scheduleHeader+std::string("evt001|case001|1000|40265318400|io.memscene.scene4.workload.a1|20020188|101|1234|True|true|900|910|999|101|20020188|BACKGROUND|998|false|false|998|true|1234|998|true|998|os_snapshot|"+donorB+"\n")); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"oracle schedule accepted noncanonical boolean");
        rejected=false;
        try { validateOracleScheduleText(scheduleHeader+validSchedule+validSchedule); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"oracle schedule accepted duplicate request rows");
        std::cout << "harmony-v1 profile checks passed\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "harmony-v1 profile check failed: " << e.what() << '\n';
        return 1;
    }
}
