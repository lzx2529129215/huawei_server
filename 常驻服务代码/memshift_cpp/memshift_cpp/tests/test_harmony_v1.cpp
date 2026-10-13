#include "../src/final_authorization.hpp"
#include "../src/device_oracle_evidence.hpp"
#include "../src/harmony_v1.hpp"
#include <fstream>
#include <iterator>
#include <iostream>
#include <stdexcept>
#include <filesystem>
#include <cstdlib>
#include <fcntl.h>
#include <unistd.h>

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
std::vector<std::string> parseCsvRowForTest(const std::string& line) {
    std::vector<std::string> fields;
    std::string field;
    bool quoted=false;
    for(std::size_t i=0;i<line.size();++i) {
        const char c=line[i];
        if(quoted && c=='"' && i+1<line.size() && line[i+1]=='"') { field.push_back('"'); ++i; }
        else if(c=='"') quoted=!quoted;
        else if(c==',' && !quoted) { fields.push_back(field); field.clear(); }
        else field.push_back(c);
    }
    fields.push_back(field);
    return fields;
}
}

int main(int argc, char** argv) {
    using namespace ms;
    using namespace ms::harmony_v1;
    {
        OracleRssSample low;low.pid=7;low.uid=8;low.startTicks=9;low.rssKiB=700*1024;low.rssAnonKiB=74*1024;
        auto good=low;good.rssAnonKiB=256*1024;
        require(oraclePrecommitMaySkipDonor(oraclePipelineKeepFailure(low,true,128*MiB,512*MiB)),"verified low donor skips before reservation");
        require(oraclePipelineKeepFailure(good,true,128*MiB,512*MiB).empty(),"second qualified donor remains selectable");
        require(!oraclePrecommitMaySkipDonor(oraclePipelineKeepFailure({},true,128*MiB,512*MiB)),"missing sample remains blocked");
        require(!oraclePrecommitMaySkipDonor(oraclePipelineKeepFailure(low,false,128*MiB,512*MiB)),"unknown identity remains blocked");
        auto inconsistent=low;inconsistent.rssKiB=1;
        require(!oraclePrecommitMaySkipDonor(oraclePipelineKeepFailure(inconsistent,true,128*MiB,512*MiB)),"inconsistent fields remain blocked");
        for(const std::string failure:{"aa_not_background","proc_uid_changed","proc_start_ticks_changed","window_visible","aa_evidence_stale"})
            require(!oraclePrecommitMaySkipDonor(failure),"protection and freshness never become below-floor skips");
        unsigned selected=0;
        for(const auto& donor:{low,good}) {
            const auto failure=oraclePipelineKeepFailure(donor,true,128*MiB,512*MiB);
            if(oraclePrecommitMaySkipDonor(failure))continue;
            require(failure.empty(),"unknown must not proceed");++selected;
        }
        require(selected==1,"first low donor skips and second qualified donor remains selectable");
    }

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
        std::cout << "DEVICE_COLLECTOR_CHECK_BEGIN\n";
        ms::harmony_v1::testDeviceOracleEvidenceParsers();
        ms::harmony_v1::testDeviceOracleTargetGuard();
        std::cout << "DEVICE_COLLECTOR_CHECK_COMPLETE\n";
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

        const char* configuredTmp=std::getenv("TMPDIR");
        const std::filesystem::path tmpBase=(configuredTmp && *configuredTmp)?configuredTmp:"/tmp";
        auto telemetryDirTemplate=(tmpBase/"memshift-swap-telemetry-XXXXXX").string();
        std::vector<char> mutableTelemetryDir(telemetryDirTemplate.begin(),telemetryDirTemplate.end());
        mutableTelemetryDir.push_back('\0');
        const char* telemetryDir=::mkdtemp(mutableTelemetryDir.data());
        require(telemetryDir!=nullptr,"cannot create swap telemetry test directory");
        const std::filesystem::path telemetryPath(telemetryDir);
        const int telemetryDirFd=::open(telemetryDir,O_RDONLY|O_DIRECTORY|O_CLOEXEC);
        require(telemetryDirFd>=0,"cannot open swap telemetry test directory");
        const auto missingSwap=readOracleSwapTelemetryNode(telemetryDirFd,"memory.eswap_info");
        require(missingSwap.status=="missing" && missingSwap.errorNumber==ENOENT &&
            missingSwap.endedBoottime>=missingSwap.startedBoottime,
            "missing swap telemetry node must be explicit and timed");
        {
            std::ofstream valid(telemetryPath/"memory.eswap_info",std::ios::binary);
            valid<<"pages=12,\"class\"\nsource=vendor\n";
        }
        const auto validSwap=readOracleSwapTelemetryNode(telemetryDirFd,"memory.eswap_info");
        require(validSwap.status=="ok" && validSwap.raw=="pages=12,\"class\"\nsource=vendor\n",
            "swap telemetry must retain successful raw text without interpreting it");
        {
            std::ofstream large(telemetryPath/"memory.zswap_info",std::ios::binary);
            large<<std::string(4097,'x');
        }
        const auto truncatedSwap=readOracleSwapTelemetryNode(telemetryDirFd,"memory.zswap_info");
        require(truncatedSwap.status=="truncated" && truncatedSwap.raw.size()==4096 &&
            truncatedSwap.endedBoottime>=truncatedSwap.startedBoottime,
            "swap telemetry read must cap retained raw text at 4KiB");
        const std::vector<std::string> telemetryFields={"action,9","event\"x","case1","bundle","29610","20020188","8263788",
            "before_write","1.25","1.26","10","leaf_scope","apps/a1","memory.eswap_info","ok","0","alpha,\"beta\"\nsecond line"};
        const auto parsedTelemetryRow=parseCsvRowForTest(oracleSwapTelemetryCsvRow(telemetryFields));
        require(parsedTelemetryRow==telemetryFields && parsedTelemetryRow.front()=="action,9" &&
            parsedTelemetryRow.back().find('\n')!=std::string::npos,
            "telemetry CSV must preserve one action-associated row with quoted multiline raw text");
        {
            std::ofstream disk(telemetryPath/"hyperhold_io_info_file");
            disk<<"> /data/vendor:\n  - write_total_size: 285821665280\n";
            std::ofstream vendorStatFile(telemetryPath/"memory.stat");
            vendorStatFile<<"Anon: 1024 kB\nZram: 512 kB\nEswap: 128 kB\n";
        }
        require(readOracleSwapTelemetryNode(telemetryDirFd,"hyperhold_io_info_file").raw.find("285821665280")!=std::string::npos &&
            readOracleSwapTelemetryNode(telemetryDirFd,"memory.stat").raw.find("Eswap: 128 kB")!=std::string::npos,
            "file-swap and per-group state must retain raw vendor counters");
        bool rejectedControl=false;
        try { (void)readOracleSwapTelemetryNode(telemetryDirFd,"memory.force_swapout"); }
        catch(const std::runtime_error&) { rejectedControl=true; }
        require(rejectedControl,"telemetry must never open a swap control node");
        const auto diskPool=parseOracleDiskPool("Zram: 26192 kB\nEswap: 0 kB\nNc_Zram: 0 kB\n");
        const auto diskWork=oracleDiskWorkEstimate(100*MiB,1200*1024);
        require(!oracleDiskPoolMeetsMinimum(16*1024,MiB) &&
            !oracleDiskPoolMeetsMinimum(0,0) &&
            !oracleDiskPoolMeetsMinimum(MiB-1,MiB) &&
            oracleDiskPoolMeetsMinimum(MiB,MiB),
            "disk pool minimum must be checked independently of resident working-set reservation");
        require(oracleDiskPostwriteRecheckEligible(2,"peak_transition_os_snapshot","force_swapout","aa_evidence_stale") &&
            !oracleDiskPostwriteRecheckEligible(2,"peak_transition_os_snapshot","force_swapout","window_visible") &&
            !oracleDiskPostwriteRecheckEligible(2,"peak_transition_os_snapshot","force_swapout","proc_start_ticks_changed") &&
            !oracleDiskPostwriteRecheckEligible(3,"peak_transition_os_snapshot","force_swapout","aa_evidence_stale") &&
            !oracleDiskPostwriteRecheckEligible(2,"legacy","force_swapout","aa_evidence_stale"),
            "disk refresh only accepts freshness-only failures in the explicit self-workload scope");
        require(diskWork && *diskWork==1300*MiB &&
            oracleDiskReservation(*diskWork,1364*MiB,64*MiB,MiB)==1364*MiB &&
            oracleDiskReservation(*diskWork,1364*MiB-1,64*MiB,MiB)==0,
            "disk command must reserve resident anonymous memory plus existing pool and margin");
        require(!oracleDiskWorkEstimate(0,1200*1024) &&
            !oracleDiskWorkEstimate(MiB,std::numeric_limits<Bytes>::max()) &&
            !oracleDiskWorkEstimate(std::numeric_limits<Bytes>::max(),1),
            "empty pool or overflowing anonymous work estimate must not authorize disk submission");
        require(diskPool && diskPool->zramBytes==26192*1024ULL && diskPool->eswapBytes==0,
            "disk planning must use vendor pool units, not resident RSS");
        require(!parseOracleDiskPool("Zram: 1 kB\nEswap: 0 kB\n") &&
            !parseOracleDiskPool("Zram: 1 kB\nEswap: 0 kB\nNc_Zram: 1 kB\n") &&
            !parseOracleDiskPool("Zram: 1 kB\nZram: 2 kB\nEswap: 0 kB\nNc_Zram: 0 kB\n") &&
            !parseOracleDiskPool("Zram: 18446744073709551615 kB\nEswap: 0 kB\nNc_Zram: 0 kB\n"),
            "unknown, noncompressed, duplicate and overflowing disk pools must fail closed");
        require(oracleDiskReservation(128*MiB,192*MiB,64*MiB,MiB)==192*MiB &&
            oracleDiskReservation(128*MiB,192*MiB-1,64*MiB,MiB)==0 &&
            oracleDiskReservation(0,192*MiB,64*MiB,MiB)==0,
            "whole-pool disk commands must fit budget including margin; zero pool must not submit");
        require(oracleDiskReservation(150*MiB,192*MiB,0,MiB)==150*MiB &&
            oracleDiskReservation(193*MiB,192*MiB,0,MiB)==0,
            "pre-submit pool growth may consume already reserved margin, never exceed charged amount");
        ::close(telemetryDirFd);
        std::filesystem::remove_all(telemetryPath);

        const std::string oracleBaseConfig=
            "[daemon]\ncgroup_root=/sys/fs/cgroup\nproc_root=/proc\nlog_dir=./logs\nlock_file=./lock\n"
            "control_file=./control\noracle_mode=true\noracle_schedule_file=./schedule\noracle_ledger_file=./ledger\n"
            "oracle_global_cap_mb=4096\noracle_case_cap_mb=4096\noracle_app_cap_mb=2048\n"
            "[app io.memscene.scene4.workload.a1]\npath=apps/io.memscene.scene4.workload.a1_20020188\nreclaim=true\nmin_keep_mb=512\n"
            "[app io.memscene.scene4.workload.a2]\npath=apps/io.memscene.scene4.workload.a2_20020189\nreclaim=true\nmin_keep_mb=512\n";
        const auto oracleDefaults=parseConfig(oracleBaseConfig);
        require(oracleDiskBackendEnabledText("hyperhold enable reclaim_in enable\n"),"enabled vendor backend was rejected");
        for(const auto* state:{"hyperhold disable reclaim_in enable", "hyperhold enable reclaim_in disable",
            "", "enable", "hyperhold enable reclaim_in enable extra", "hyperhold unknown reclaim_in enable"})
            require(!oracleDiskBackendEnabledText(state),"disabled or unknown disk backend must refuse pipeline writes");
        require(oracleDefaults.options.harmonySmapsIntervalMs==0,"existing configurations must retain detailed sampling cadence");
        auto smapsConfig=oracleBaseConfig;
        smapsConfig.insert(smapsConfig.find("[app "),"harmony_smaps_interval_ms=30000\n");
        const auto smapsOptions=parseConfig(smapsConfig).options;
        require(smapsOptions.harmonySmapsIntervalMs==30000 && smapsOptions.intervalMs==oracleDefaults.options.intervalMs,
            "diagnostic cadence must not alter status/stat sampling cadence");
        for(const auto value:{"999","600001","-1"}) {
            auto invalid=oracleBaseConfig;
            invalid.insert(invalid.find("[app "),std::string("harmony_smaps_interval_ms=")+value+"\n");
            bool rejected=false;
            try { (void)parseConfig(invalid); } catch(const std::exception&) { rejected=true; }
            require(rejected,"invalid detailed sampling interval accepted");
        }
        require(!oracleDefaults.options.oracleDiskPrepareAnon,"disk preparation must default off");
        require(oracleDeviceDonorTotalKeepFloor(oracleDefaults.options,"io.memscene.scene4.workload.a1",128*MiB)==512*MiB,
            "legacy/non-device profile must retain its 512 MiB total-RSS floor");
        const auto deviceFloor=parseConfig(
            "[daemon]\ncgroup_root=/sys/fs/cgroup\nproc_root=/proc\nlog_dir=./logs\nlock_file=./lock\n"
            "control_file=./control\noracle_mode=true\noracle_proof_source=device\noracle_schedule_file=./schedule\n"
            "oracle_ledger_file=./ledger\noracle_profile=peak_transition_os_snapshot\noracle_backend=force_swapout\n"
            "oracle_disk_prepare_anon=true\noracle_cumulative_keep_mb=128\noracle_device_donor_total_keep_mb=128\n"
            "oracle_max_ratio=90\n"
            "[app io.memscene.scene4.workload.a1]\npath=apps/io.memscene.scene4.workload.a1_20020188\nreclaim=true\nmin_keep_mb=128\n"
            "[app io.memscene.scene4.workload.a2]\npath=apps/io.memscene.scene4.workload.a2_20020189\nreclaim=true\nmin_keep_mb=128\n");
        require(deviceFloor.options.oracleDeviceDonorTotalKeep==128*MiB && deviceFloor.options.oracleMaxRatio==90 &&
            oracleDeviceDonorTotalKeepFloor(deviceFloor.options,"io.memscene.scene4.workload.a1",128*MiB)==128*MiB &&
            oracleDeviceDonorTotalKeepFloor(deviceFloor.options,"io.memscene.scene4.workload.a14",128*MiB)==512*MiB &&
            oracleDeviceDonorTotalKeepFloor(deviceFloor.options,"io.memscene.scene4.workload.a17",128*MiB)==512*MiB &&
            oracleDeviceDonorTotalKeepFloor(deviceFloor.options,"io.memscene.scene4.workload.a1",600*MiB)==600*MiB &&
            oracleDeviceDonorTotalKeepFloor(oracleDefaults.options,"io.memscene.scene4.workload.a1",128*MiB)==512*MiB,
            "lower total-RSS floor must be restricted to fixed owned donors and preserve app-specific higher floors");
        bool nonDeviceLowFloorRejected=false;
        try { (void)parseConfig(
            "[daemon]\ncgroup_root=/sys/fs/cgroup\nproc_root=/proc\nlog_dir=./logs\nlock_file=./lock\n"
            "oracle_mode=true\noracle_schedule_file=./schedule\noracle_ledger_file=./ledger\n"
            "oracle_profile=peak_transition_os_snapshot\noracle_backend=force_swapout\noracle_disk_prepare_anon=true\n"
            "oracle_device_donor_total_keep_mb=128\n"
            "[app io.memscene.scene4.workload.a1]\npath=apps/io.memscene.scene4.workload.a1_20020188\nreclaim=true\nmin_keep_mb=128\n"); }
        catch(const std::exception&) { nonDeviceLowFloorRejected=true; }
        require(nonDeviceLowFloorRejected,"low device donor floor accepted without device proof");
        std::string fixedHighRatioConfig=
            "[daemon]\ncgroup_root=/sys/fs/cgroup\nproc_root=/proc\nlog_dir=./logs\nlock_file=./lock\n"
            "oracle_mode=true\noracle_proof_source=device\noracle_schedule_file=./schedule\noracle_ledger_file=./ledger\n"
            "oracle_profile=peak_transition_os_snapshot\noracle_backend=force_swapout\noracle_disk_prepare_anon=true\noracle_max_ratio=95\n";
        for(unsigned i=1;i<=16;++i) {
            const auto bundle="io.memscene.scene4.workload.a"+std::to_string(i);
            fixedHighRatioConfig+="[app "+bundle+"]\npath=apps/"+bundle+"_"+std::to_string(20020187+i)+
                "\nreclaim=true\nmin_keep_mb="+(i<=13?"128":"512")+"\n";
        }
        const auto fixedHighRatio=parseConfig(fixedHighRatioConfig);
        require(fixedHighRatio.options.oracleMaxRatio==95 && fixedHighRatio.apps.size()==16,
            "95 percent ratio must parse for the complete closed device app set");
        auto hostHighRatioConfig=fixedHighRatioConfig;
        const auto proofPos=hostHighRatioConfig.find("oracle_proof_source=device");
        hostHighRatioConfig.replace(proofPos,std::string("oracle_proof_source=device").size(),"oracle_proof_source=legacy");
        bool hostHighRatioRejected=false;
        try { (void)parseConfig(hostHighRatioConfig); }
        catch(const std::exception&) { hostHighRatioRejected=true; }
        require(hostHighRatioRejected,"max ratio above 90 escaped the fixed device-profile scope");
        OracleRssSample keepSample;
        keepSample.rssAnonKiB=256*1024; keepSample.rssKiB=1536*1024;
        require(oraclePipelineAnonKeep(keepSample,128*MiB,512*MiB)==128*MiB &&
            oraclePipelineKeepSatisfied(keepSample,128*MiB,512*MiB),
            "known non-anonymous resident pages must contribute to the total app keep floor");
        keepSample.rssKiB=384*1024;
        require(oraclePipelineAnonKeep(keepSample,128*MiB,512*MiB)==384*MiB &&
            !oraclePipelineKeepSatisfied(keepSample,128*MiB,512*MiB),
            "insufficient total resident memory must stop further preparation and disk stages");
        keepSample.rssKiB=1536*1024; keepSample.rssAnonKiB=127*1024;
        require(!oraclePipelineKeepSatisfied(keepSample,128*MiB,512*MiB),"anonymous floor cannot be replaced by file pages");
        keepSample.rssKiB.reset();
        require(!oraclePipelineAnonKeep(keepSample,128*MiB,512*MiB),"missing total RSS cannot permit a smaller anonymous keep");
        keepSample.rssKiB=126*1024;
        require(!oraclePipelineAnonKeep(keepSample,128*MiB,512*MiB),"inconsistent total and anonymous RSS must reject");
        auto diskPrepareConfig=oracleBaseConfig;
        diskPrepareConfig.insert(diskPrepareConfig.find("[app "),"oracle_disk_prepare_anon=true\n");
        require(parseConfig(diskPrepareConfig).options.oracleDiskPrepareAnon,"explicit disk preparation key ignored");
        require(oracleDiskPreparationComplete(true,"write_accepted_feedback_measured_positive_rss_observation",true,1,false),
            "verified positive preparation must permit the planned disk stage");
        require(!oracleDiskPreparationComplete(false,"write_accepted_feedback_measured_positive_rss_observation",true,1,false) &&
            !oracleDiskPreparationComplete(true,"write_failed_or_partial_no_retry",true,1,false) &&
            !oracleDiskPreparationComplete(true,"write_accepted_feedback_unknown",true,1,false) &&
            !oracleDiskPreparationComplete(true,"write_accepted_feedback_measured_positive_rss_observation",false,1,false) &&
            !oracleDiskPreparationComplete(true,"write_accepted_feedback_measured_positive_rss_observation",true,0,false) &&
            !oracleDiskPreparationComplete(true,"write_accepted_feedback_measured_positive_rss_observation",true,-1,false) &&
            !oracleDiskPreparationComplete(true,"write_accepted_feedback_measured_positive_rss_observation",true,1,true),
            "unknown/failed/identity-lost/nonpositive/late preparation must never authorize disk fallback");
        require(oracleDefaults.options.oracleBatch==512*MiB && oracleDefaults.options.oracleMaxRatio==30 &&
            oracleDefaults.options.oracleFeedbackDelayMs==0 &&
            oracleDefaults.options.oracleProfileActionIntervalMs==1000 &&
            oracleDefaults.options.oracleProfile=="legacy_two_donor" &&
            oracleDefaults.options.oracleBackend=="force_shrink_all" &&
            !oracleDefaults.options.oracleBackendExplicit &&
            oracleDefaults.options.oracleRatioSemantics=="resident_incremental" &&
            oracleDefaults.options.oracleCumulativeKeep==512*MiB,
            "oracle tuning defaults must preserve prior limits and immediate feedback");
        auto legacyNamedAppsConfig=oracleBaseConfig;
        const auto a1Section=legacyNamedAppsConfig.find("[app io.memscene.scene4.workload.a1]");
        const auto a2Section=legacyNamedAppsConfig.find("[app io.memscene.scene4.workload.a2]");
        require(a1Section!=std::string::npos && a2Section!=std::string::npos,"legacy config fixture missing app sections");
        legacyNamedAppsConfig.replace(a1Section,std::string("[app io.memscene.scene4.workload.a1]").size(),"[app donor_a]");
        const auto renamedA2=legacyNamedAppsConfig.find("[app io.memscene.scene4.workload.a2]");
        require(renamedA2!=std::string::npos,"legacy second app section fixture missing");
        legacyNamedAppsConfig.replace(renamedA2,std::string("[app io.memscene.scene4.workload.a2]").size(),"[app donor_b]");
        const auto legacyNamedApps=parseConfig(legacyNamedAppsConfig);
        require(legacyNamedApps.apps.size()==2 && legacyNamedApps.apps[0].name=="donor_a" &&
            legacyNamedApps.apps[1].name=="donor_b" &&
            oracleAppMatchesDonor("legacy_two_donor","donor_a","io.memscene.scene4.workload.a1_20020188",
                "io.memscene.scene4.workload.a1","20020188") &&
            oracleAppMatchesDonor("legacy_two_donor","donor_b","io.memscene.scene4.workload.a2_20020189",
                "io.memscene.scene4.workload.a2","20020189"),
            "legacy donor_a/donor_b config names no longer map by exact cgroup leaf");
        const auto oracleTuned=parseConfig(
            "[daemon]\ncgroup_root=/sys/fs/cgroup\nproc_root=/proc\nlog_dir=./logs\nlock_file=./lock\n"
            "control_file=./control\noracle_mode=true\noracle_schedule_file=./schedule\noracle_ledger_file=./ledger\n"
            "oracle_batch_mb=1792\noracle_max_ratio=90\noracle_feedback_delay_ms=5000\noracle_ratio_semantics=cumulative_anon_watermark\noracle_cumulative_keep_mb=128\n"
            "oracle_global_cap_mb=4096\noracle_case_cap_mb=4096\noracle_app_cap_mb=2048\n"
            "[app io.memscene.scene4.workload.a1]\npath=apps/io.memscene.scene4.workload.a1_20020188\nreclaim=true\nmin_keep_mb=512\n"
            "[app io.memscene.scene4.workload.a2]\npath=apps/io.memscene.scene4.workload.a2_20020189\nreclaim=true\nmin_keep_mb=512\n");
        require(oracleTuned.options.oracleBatch==1792*MiB && oracleTuned.options.oracleMaxRatio==90 &&
            oracleTuned.options.oracleFeedbackDelayMs==5000 &&
            oracleTuned.options.oracleRatioSemantics=="cumulative_anon_watermark" &&
            oracleTuned.options.oracleCumulativeKeep==128*MiB,
            "isolated oracle tuning keys did not parse as configured");
        auto peakProfileConfig=oracleBaseConfig;
        peakProfileConfig.insert(peakProfileConfig.find("[app "),
            "oracle_profile=peak_transition_os_snapshot\noracle_backend=proc_file\noracle_profile_action_interval_ms=1000\n");
        const auto peakProfile=parseConfig(peakProfileConfig);
        require(peakProfile.options.oracleProfile=="peak_transition_os_snapshot" &&
            peakProfile.options.oracleBackend=="proc_file" && peakProfile.options.oracleBackendExplicit &&
            peakProfile.options.oracleProfileActionIntervalMs==1000 && peakProfile.options.oracleProfilePollIntervalMs==200,
            "explicit profile/backend/independent feedback delay did not parse");
        auto peakWritebackConfig=oracleBaseConfig;
        peakWritebackConfig.insert(peakWritebackConfig.find("[app "),
            "oracle_profile=peak_transition_os_snapshot\noracle_backend=proc_file_writeback\noracle_profile_action_interval_ms=1000\noracle_profile_poll_interval_ms=150\n");
        const auto peakWriteback=parseConfig(peakWritebackConfig);
        require(peakWriteback.options.oracleProfile=="peak_transition_os_snapshot" &&
            peakWriteback.options.oracleBackend=="proc_file_writeback" && peakWriteback.options.oracleBackendExplicit &&
            peakWriteback.options.oracleProfilePollIntervalMs==150,
            "explicit writeback backend did not parse");
        auto badPollConfig=peakWritebackConfig;
        const auto pollPos=badPollConfig.find("oracle_profile_poll_interval_ms=150");
        badPollConfig.replace(pollPos,std::string("oracle_profile_poll_interval_ms=150").size(),"oracle_profile_poll_interval_ms=99");
        bool badPollRejected=false;
        try { (void)parseConfig(badPollConfig); } catch(const std::exception&) { badPollRejected=true; }
        require(badPollRejected,"oracle profile poll interval below 100ms was accepted");
        require(oracleScheduleCompatible("legacy_two_donor","force_shrink_all",1) &&
            oracleScheduleCompatible("peak_transition_os_snapshot","force_shrink_all",2) &&
            oracleScheduleCompatible("peak_transition_os_snapshot","proc_file",2) &&
            oracleScheduleCompatible("peak_transition_os_snapshot","proc_file_writeback",3) &&
            !oracleScheduleCompatible("legacy_two_donor","proc_file_writeback",3) &&
            !oracleScheduleCompatible("peak_transition_os_snapshot","proc_file_writeback",2) &&
            !oracleScheduleCompatible("peak_transition_os_snapshot","force_shrink_all",3),
            "oracle schema/backend compatibility did not preserve v1/v2 and isolate v3 writeback");
        require(oracleAppMatchesDonor("peak_transition_os_snapshot","io.memscene.scene4.workload.a1",
                "io.memscene.scene4.workload.a1_20020188","io.memscene.scene4.workload.a1","20020188") &&
            !oracleAppMatchesDonor("peak_transition_os_snapshot","donor_a",
                "io.memscene.scene4.workload.a1_20020188","io.memscene.scene4.workload.a1","20020188") &&
            !oracleAppMatchesDonor("legacy_two_donor","donor_a",
                "io.memscene.scene4.workload.a1_20020189","io.memscene.scene4.workload.a1","20020188"),
            "profile app name/path/UID matching rules changed");
        auto peakAnonConfig=oracleBaseConfig;
        peakAnonConfig.insert(peakAnonConfig.find("[app "),
            "oracle_profile=peak_transition_os_snapshot\noracle_backend=force_shrink_all\noracle_profile_action_interval_ms=1000\n");
        const auto peakAnon=parseConfig(peakAnonConfig);
        require(peakAnon.options.oracleProfile=="peak_transition_os_snapshot" &&
            peakAnon.options.oracleBackend=="force_shrink_all" && peakAnon.options.oracleBackendExplicit,
            "new profile did not permit explicit legacy anonymous backend selection");
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
        const auto ratio95=oracleCumulativeWatermarkForCap(2300*MiB,0,2200*MiB,95);
        require(ratio95.ratio==95 && ratio95.estimatedIncrementBytes<=2200*MiB &&
            oracleCumulativeWatermarkForCap(2300*MiB,0,2200*MiB,96).ratio==0,
            "95 percent watermark is permitted while values above it fail closed");
        require(oracleFreshnessFailure(true,10.0,{{"aa",7.0},{"window",9.0}})=="aa_evidence_stale" &&
            oracleFreshnessFailureRetryable("aa_evidence_stale") &&
            oracleFreshnessFailureRetryable("memory_current_telemetry_missing") &&
            !oracleFreshnessFailureRetryable("proc_start_ticks_changed") &&
            !oracleFreshnessFailureRetryable("window_focused") &&
            oracleFreshnessFailure(false,10.0,{{"aa",9.9}})=="identity_or_background_invariant_failed",
            "oracle freshness defer classification weakened an identity/background guard");
        const std::string lateEvidence="rejected:oracle_evidence_reject_after_pre_action_swap_telemetry:";
        for(const auto& reason:{"aa_evidence_stale","window_evidence_stale",
            "proc_evidence_stale","lifecycle_evidence_stale"}) {
            const std::string status=lateEvidence+reason;
            require(oracleUnsubmittedEvidenceMayDefer(status,false,false,false),
                "read-only pre-submit telemetry must not permanently halt a freshenable event");
            require(!oracleUnsubmittedEvidenceMayDefer(status,true,false,false) &&
                !oracleUnsubmittedEvidenceMayDefer(status,false,true,false) &&
                !oracleUnsubmittedEvidenceMayDefer(status,false,false,true),
                "submitted or deadline-overlapping work must never use no-write deferral");
        }
        for(const auto& reason:{"proc_start_ticks_changed","window_focused",
            "pipeline_process_total_or_anon_keep_unknown_or_below_floor","aa_telemetry_missing",
            "aa_timestamp_in_future","aa_evidence_stale:identity_changed"})
            require(!oracleUnsubmittedEvidenceMayDefer(lateEvidence+reason,false,false,false),
                "late no-write deferral weakened identity, protection or clock guards");
        require(oracleUnsubmittedEvidenceMayDefer("rejected:oracle_evidence_defer:aa_evidence_stale",false,false,false) &&
            !oracleUnsubmittedEvidenceMayDefer("rejected:oracle_evidence_defer:window_focused",false,false,false) &&
            !oracleUnsubmittedEvidenceMayDefer("write_failed_or_partial_no_retry:aa_evidence_stale",false,false,false) &&
            !oracleUnsubmittedEvidenceMayDefer("write_accepted_feedback_unknown_after_exception:aa_evidence_stale",false,false,false),
            "unknown writes or invariant failure must halt instead of deferring");
        auto invalidOracleConfig=oracleBaseConfig;
        invalidOracleConfig.insert(invalidOracleConfig.find("[app "),"oracle_max_ratio=96\n");
        bool invalidOracleRatioRejected=false;
        try { (void)parseConfig(invalidOracleConfig); }
        catch(const std::runtime_error&) { invalidOracleRatioRejected=true; }
        require(invalidOracleRatioRejected,"oracle ratio above the hard ceiling was accepted");
        auto invalidOracleKeep=oracleBaseConfig;
        invalidOracleKeep.insert(invalidOracleKeep.find("[app "),"oracle_cumulative_keep_mb=127\n");
        bool invalidOracleKeepRejected=false;
        try { (void)parseConfig(invalidOracleKeep); }
        catch(const std::runtime_error&) { invalidOracleKeepRejected=true; }
        require(invalidOracleKeepRejected,"oracle cumulative keep below its experiment floor was accepted");

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
            OracleRssSample{donorPid+1,donorUid,donorTicks,900,std::nullopt,std::nullopt,std::nullopt});
        require(!feedback.identityVerified,"different PID post sample was accepted");
        feedback=compareOracleRssSamples(donorPid,donorUid,donorTicks,before,
            OracleRssSample{donorPid,donorUid,donorTicks,18446744073709552ULL,std::nullopt,std::nullopt,std::nullopt});
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

        const std::string v2DonorFields="bundle|uid|pid|start_ticks|daily_ready|window_ready|daily_ready_boottime_s|background_since_s|aa_pid|aa_uid|aa_state|aa_observed_boottime_s|window_visible|window_focused|window_observed_boottime_s|proc_verified|proc_start_ticks|proc_observed_boottime_s|lifecycle_clear|lifecycle_observed_boottime_s|background_evidence_source";
        std::ostringstream v2Header; v2Header<<"schema_version|event_id|case_id|launch_boottime_s|planned_needed_bytes|donor_count";
        for(unsigned i=1;i<=8;++i) v2Header<<'|'<<prefixed("donor_"+std::to_string(i)+"_",v2DonorFields);
        auto v2Donor=[](unsigned i) {
            const auto uid=20020188+i, pid=101+i, ticks=1234+i;
            std::ostringstream d;
            d<<"io.memscene.scene4.workload.a"<<i<<'|'<<uid<<'|'<<pid<<'|'<<ticks
             <<"|true|true|900|910|"<<pid<<'|'<<uid<<"|BACKGROUND|998|false|false|998|true|"<<ticks<<"|998|true|998|os_snapshot";
            return d.str();
        };
        auto v2Schedule=[&](unsigned count) {
            std::ostringstream row; row<<"2|evt008|case008|1000|23353884672|"<<count;
            for(unsigned i=1;i<=count;++i) row<<'|'<<v2Donor(i);
            return v2Header.str()+"\n"+row.str()+"\n";
        };
        validateOracleScheduleText(v2Schedule(1));
        validateOracleScheduleText(v2Schedule(8));
        std::ostringstream v2Header16; v2Header16<<v2Header.str();
        for(unsigned i=9;i<=16;++i) v2Header16<<'|'<<prefixed("donor_"+std::to_string(i)+"_",v2DonorFields);
        auto v2Schedule16=[&](unsigned count) {
            auto text=v2Schedule(count);
            text.replace(0,v2Header.str().size(),v2Header16.str());
            return text;
        };
        validateOracleScheduleText(v2Schedule16(16));
        rejected=false;
        try { validateOracleScheduleText(v2Schedule16(17)); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"force_swapout schema2 accepted seventeen donors");

        rejected=false;
        auto v2BadTarget=v2Schedule(1); const auto allowlisted=v2BadTarget.find("workload.a1"); v2BadTarget.replace(allowlisted,std::string("workload.a1").size(),"workload.a17");
        try { validateOracleScheduleText(v2BadTarget); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"v2 schedule accepted target outside exact a1..a16 allowlist");
        rejected=false;
        auto v2Duplicate=v2Schedule(2); const auto secondPid=v2Duplicate.find("|103|1236|");
        require(secondPid!=std::string::npos,"v2 duplicate test fixture failed");
        v2Duplicate.replace(secondPid,std::string("|103|1236|").size(),"|102|1236|");
        try { validateOracleScheduleText(v2Duplicate); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"v2 schedule accepted duplicate PID identity");
        rejected=false;
        auto v2DuplicateUid=v2Schedule(2);
        const std::string donorTwoPrefix="io.memscene.scene4.workload.a2|20020190|103|1236";
        const auto donorTwoAt=v2DuplicateUid.find(donorTwoPrefix);
        require(donorTwoAt!=std::string::npos,"v2 duplicate UID fixture failed");
        v2DuplicateUid.replace(donorTwoAt,donorTwoPrefix.size(),"io.memscene.scene4.workload.a2|20020189|103|1236");
        const auto donorTwoAaUid=v2DuplicateUid.find("|103|20020190|BACKGROUND");
        require(donorTwoAaUid!=std::string::npos,"v2 duplicate UID AA fixture failed");
        v2DuplicateUid.replace(donorTwoAaUid,std::string("|103|20020190|BACKGROUND").size(),"|103|20020189|BACKGROUND");
        try { validateOracleScheduleText(v2DuplicateUid); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"v2 schedule accepted duplicate UID identity");
        rejected=false;
        auto v2Heartbeat=v2Schedule(1); const auto osSource=v2Heartbeat.rfind("os_snapshot"); v2Heartbeat.replace(osSource,11,"hap_heartbeat");
        try { validateOracleScheduleText(v2Heartbeat); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"v2 profile accepted fabricated legacy heartbeat evidence");

        std::ostringstream v3Header; v3Header<<"schema_version|event_id|case_id|launch_boottime_s|planned_needed_bytes|donor_count";
        for(unsigned i=1;i<=8;++i) v3Header<<'|'<<prefixed("donor_"+std::to_string(i)+"_",v2DonorFields+"|writeback_file_path|writeback_file_dev|writeback_file_ino|writeback_file_size");
        auto v3Donor=[&](unsigned i) {
            std::ostringstream d; d<<v2Donor(i)<<"|memscene_case008.pages|66662|"<<(164292+i)<<"|1258291200"; return d.str();
        };
        auto v3Schedule=[&](unsigned count) {
            std::ostringstream row; row<<"3|evt008|case008|1000|23353884672|"<<count;
            for(unsigned i=1;i<=count;++i) row<<'|'<<v3Donor(i);
            return v3Header.str()+"\n"+row.str()+"\n";
        };
        validateOracleScheduleText(v3Schedule(1));
        validateOracleScheduleText(v3Schedule(8));
        std::ostringstream v3Header16; v3Header16<<v3Header.str();
        for(unsigned i=9;i<=16;++i) v3Header16<<'|'<<prefixed("donor_"+std::to_string(i)+"_",v2DonorFields+"|writeback_file_path|writeback_file_dev|writeback_file_ino|writeback_file_size");
        auto v3Schedule16=[&](unsigned count) {
            auto text=v3Schedule(count);
            text.replace(0,v3Header.str().size(),v3Header16.str());
            return text;
        };
        validateOracleScheduleText(v3Schedule16(16));
        rejected=false;
        try { validateOracleScheduleText(v3Schedule16(17)); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"schema3 accepted seventeen donors");
        auto v3Bad17=v3Schedule16(1);
        v3Bad17.replace(v3Bad17.find("workload.a1"),std::string("workload.a1").size(),"workload.a17");
        rejected=false;
        try { validateOracleScheduleText(v3Bad17); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"schema3 accepted a17 outside allowlist");

        rejected=false;
        auto v3WrongCase=v3Schedule(1); const auto goodFile=v3WrongCase.find("memscene_case008.pages");
        require(goodFile!=std::string::npos,"v3 test file fixture missing");
        v3WrongCase.replace(goodFile,std::string("memscene_case008.pages").size(),"memscene_other.pages");
        try { validateOracleScheduleText(v3WrongCase); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"v3 schedule accepted a file outside the exact case filesDir name");
        rejected=false;
        auto v3DuplicateFile=v3Schedule(2); const auto secondFileIdentity=v3DuplicateFile.find("memscene_case008.pages|66662|164294|");
        require(secondFileIdentity!=std::string::npos,"v3 duplicate file identity fixture missing");
        v3DuplicateFile.replace(secondFileIdentity,std::string("memscene_case008.pages|66662|164294|").size(),"memscene_case008.pages|66662|164293|");
        try { validateOracleScheduleText(v3DuplicateFile); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"v3 schedule accepted duplicate file dev/inode identity");
        rejected=false;
        auto v3Heartbeat=v3Schedule(1); const auto v3OsSource=v3Heartbeat.rfind("os_snapshot"); v3Heartbeat.replace(v3OsSource,11,"hap_heartbe");
        try { validateOracleScheduleText(v3Heartbeat); } catch(const std::runtime_error&) { rejected=true; }
        require(rejected,"v3 writeback profile accepted fabricated heartbeat evidence");

        require(oracleProcFileReservation(100*MiB,300*MiB,128*MiB,300*MiB,200*MiB,200*MiB,150*MiB,120*MiB,64*MiB,8*MiB)==0,
            "whole-process file estimate exceeding shared app room was partially reserved");
        require(oracleProcFileReservation(100*MiB,300*MiB,128*MiB,300*MiB,200*MiB,200*MiB,180*MiB,180*MiB,64*MiB,8*MiB)==100*MiB,
            "whole-process file estimate fitting shared limits was not fully reserved");
        require(oracleProcFileReservation(100*MiB,550*MiB,512*MiB,300*MiB,200*MiB,200*MiB,180*MiB,180*MiB,64*MiB,8*MiB)==0,
            "file reservation allowed estimated retained RSS below keep reserve");
        require(oracleProcFileReservation(7*MiB,300*MiB,128*MiB,300*MiB,200*MiB,200*MiB,180*MiB,180*MiB,64*MiB,8*MiB)==0,
            "sub-minimum whole-process file estimate was reserved");
        const Bytes writebackReserved=oracleProcFileWritebackReservation(900*MiB,1200*MiB,1800*MiB,512*MiB,1800*MiB,1500*MiB,1500*MiB,1500*MiB,1500*MiB,64*MiB,8*MiB);
        require(writebackReserved==1264*MiB &&
            oracleProcFileWritebackReservation(900*MiB,1600*MiB,1800*MiB,512*MiB,1500*MiB,1500*MiB,1500*MiB,1500*MiB,1500*MiB,64*MiB,8*MiB)==0,
            "writeback reservation must charge whole work estimate plus the safety margin");
        require(oracleProcFileWritebackReservation(900*MiB,1200*MiB,1800*MiB,512*MiB,1800*MiB,1263*MiB,1500*MiB,1500*MiB,1500*MiB,64*MiB,8*MiB)==0 &&
            oracleProcFileWritebackReservation(900*MiB,1200*MiB,1800*MiB,512*MiB,1800*MiB,1264*MiB,1264*MiB,1264*MiB,1264*MiB,64*MiB,8*MiB)==1264*MiB,
            "writeback estimate plus margin must fit every shared cap before reservation");
        require(oracleProcFileWritebackEstimateFits(1239624,1200*MiB,writebackReserved) &&
            !oracleProcFileWritebackEstimateFits(1239624,1200*MiB,1200*MiB) &&
            !oracleProcFileWritebackEstimateFits(1264*1024+1,1200*MiB,writebackReserved) &&
            !oracleProcFileWritebackEstimateFits(1239624,1265*MiB,writebackReserved),
            "writeback RSS growth inside the charged margin must fit while estimates beyond reservation still stop");
        require(oracleProcFileWritebackSystemGapFits(1264*MiB,writebackReserved) &&
            !oracleProcFileWritebackSystemGapFits(1264*MiB-1,writebackReserved),
            "writeback system-gap gate must compare against the margin-inclusive reservation");
        require(oracleProfileTargetMaySubmit(false,true) && !oracleProfileTargetMaySubmit(true,true) &&
            !oracleProfileTargetMaySubmit(false,false),"profile target retry/identity guard semantics changed");
        {
            const std::vector<unsigned> rotation={0,1,2,3};
            const std::vector<bool> zramReady={true,true,true,true};
            const std::vector<bool> prepared={false,false,true,false};
            const std::vector<std::optional<bool>> prepRoom={true,false,false,std::nullopt};
            const auto attempts=oracleDiskPriorityAttempts(rotation,zramReady,prepared,prepRoom);
            require(attempts.size()==5 &&
                attempts[0].donorIndex==1 && attempts[0].priorityDiskOnly &&
                attempts[1].donorIndex==2 && attempts[1].priorityDiskOnly &&
                attempts[2].donorIndex==0 && !attempts[2].priorityDiskOnly &&
                attempts[3].donorIndex==1 && !attempts[3].priorityDiskOnly &&
                attempts[4].donorIndex==3 && !attempts[4].priorityDiskOnly,
                "only low-room or already-prepared zram donors get priority; bounded fallback preserves other prep opportunities");
            const auto noPool=oracleDiskPriorityAttempts(rotation,std::vector<bool>(4,false),prepared,prepRoom);
            require(noPool.size()==4 && noPool.front().donorIndex==0 && !noPool.front().priorityDiskOnly,
                "empty pool preserves original single round-robin pass");
            const auto noRoomProof=oracleDiskPriorityAttempts(rotation,zramReady,
                std::vector<bool>(4,false),std::vector<std::optional<bool>>(4));
            require(noRoomProof.size()==4 && std::none_of(noRoomProof.begin(),noRoomProof.end(),
                [](const OracleDiskPriorityAttempt& attempt){return attempt.priorityDiskOnly;}),
                "unknown fresh anon room never authorizes priority disk-only");
            OracleRssSample high;high.rssAnonKiB=600*1024;high.rssKiB=620*1024;
            OracleRssSample low;low.rssAnonKiB=177*1024;low.rssKiB=190*1024;
            const auto highRoom=oracleDiskPriorityPrepRoomAvailable(high,128*MiB,128*MiB,64*MiB,64*MiB);
            const auto lowRoom=oracleDiskPriorityPrepRoomAvailable(low,128*MiB,128*MiB,64*MiB,64*MiB);
            OracleRssSample unknown;unknown.rssAnonKiB=177*1024;
            require(highRoom && *highRoom && lowRoom && !*lowRoom &&
                !oracleDiskPriorityPrepRoomAvailable(unknown,128*MiB,128*MiB,64*MiB,64*MiB) &&
                !oracleDiskPriorityPrepRoomAvailable(low,128*MiB,128*MiB,0,64*MiB),
                "prep-room classifier requires fresh consistent RSSAnon/RSS and keep+margin+batch capacity");
        }
        {
            const auto schema=3U;
            const std::string profile="peak_transition_os_snapshot",backend="proc_file_writeback";
            bool a1Selected=false,a2Selected=false,a3Selected=false;
            const bool a1AlreadySubmitted=true;
            const bool a2Cooldown=true;
            const bool a3IdentityComplete=true;
            if(oracleProfileTargetMaySubmit(a1AlreadySubmitted,true)) a1Selected=true;
            if(oracleProfileCooldownMayContinue(schema,profile,backend,true)) {
                // The scheduler skips the cooled donor with continue, then evaluates a3.
            } else a2Selected=true;
            if(oracleProfileTargetMaySubmit(false,a3IdentityComplete)) a3Selected=true;
            require(!a1Selected && !a2Selected && a3Selected,
                "schema3 feedback cooldown must exclude only the affected donor and leave a3 selectable");
            require(!oracleProfileCooldownMayContinue(2,profile,backend,true) &&
                !oracleProfileCooldownMayContinue(3,"legacy_two_donor",backend,true) &&
                !oracleProfileCooldownMayContinue(3,profile,"force_shrink_all",true) &&
                !oracleProfileCooldownMayContinue(3,profile,backend,false),
                "donor-local cooldown escaped schema3 writeback scope or accepted unknown identity");
            require(oracleProfileDonorCooldownScope(3,profile,backend) &&
                !oracleProfileDonorCooldownScope(2,profile,backend) &&
                !oracleProfileDonorCooldownScope(3,profile,"force_shrink_all") &&
                a2Cooldown && !oracleProfileCooldownMayContinue(3,profile,backend,false),
                "unknown identity must remain a global event-stop condition, not a donor skip");
        }
        {
            using Retry=OraclePrecommitMemberRetryAction;
            const bool scoped=oracleProfileDonorCooldownScope(3,"peak_transition_os_snapshot","proc_file_writeback");
            const auto anomaly=oraclePrecommitMemberRetryAction(scoped,true,true,0,0,29610,false,0,10.0,20.0);
            require(anomaly==Retry::Wait && oraclePrecommitMemberWaitWindowOpen(false,0,10.0,20.0),
                "schema3 writeback cgroup count anomaly must enter bounded read-only precommit wait");
            require(oraclePrecommitMemberRetryAction(scoped,true,true,1,29610,29610,true,10.0,11.0,20.0)==Retry::Recovered,
                "exact original PID recovery inside the bound must permit full preflight revalidation");
            require(oraclePrecommitMemberRetryAction(scoped,true,true,1,29610,29610,true,10.0,12.0,20.0)==Retry::Stop &&
                !oraclePrecommitMemberWaitWindowOpen(true,10.0,12.0,20.0),
                "recovery at the exact two-second boundary must remain stopped");
            require(oraclePrecommitMemberRetryAction(scoped,true,true,1,29610,29610,true,10.0,10.9,10.9)==Retry::Stop,
                "earlier event deadline must bound member retry");
            require(oraclePrecommitMemberRetryAction(scoped,true,true,1,29611,29610,true,10.0,10.5,20.0)==Retry::Stop &&
                oraclePrecommitMemberRetryAction(scoped,false,true,0,0,29610,false,0,10.0,20.0)==Retry::Stop &&
                oraclePrecommitMemberRetryAction(scoped,true,false,0,0,29610,false,0,10.0,20.0)==Retry::Stop,
                "wrong PID, unreadable member source, or malformed member row must hard stop");
            require(oraclePrecommitMemberRetryAction(scoped,true,true,2,29610,29610,false,0,10.0,20.0)==Retry::Wait,
                "multiple listed members may only wait for exact recovery and are never accepted");
            require(oraclePrecommitMemberRetryAction(false,true,true,0,0,29610,false,0,10.0,20.0)==Retry::NotApplicable,
                "legacy or out-of-scope profile behavior changed");
            unsigned charges=0,writes=0;
            if(anomaly==Retry::Recovered || anomaly==Retry::NotApplicable) { ++charges; ++writes; }
            require(charges==0 && writes==0,
                "precommit member wait must have no reservation charge or reclaim write side effect");
        }
        require(oracleWritebackMayProceed(true,true,true,true) &&
            !oracleWritebackMayProceed(false,true,true,true) && !oracleWritebackMayProceed(true,false,true,true) &&
            !oracleWritebackMayProceed(true,true,false,true) && !oracleWritebackMayProceed(true,true,true,false),
            "writeback-to-proc transition must require successful sync, deadline, identity, and budget gates");
        require(oracleLoopIntervalMs(true,true,200,1000)==200 &&
            oracleLoopIntervalMs(true,false,200,1000)==1000 && oracleLoopIntervalMs(false,true,200,1000)==1000,
            "fast oracle poll changed cadence outside an active peak profile event");
        require(!oracleProfileRunsFullObservation(true,true,false) &&
            oracleProfileRunsFullObservation(true,true,true) && oracleProfileRunsFullObservation(true,false,false) &&
            oracleProfileRunsFullObservation(false,true,false),
            "fast oracle poll repeated full observation/smaps sampling or suppressed a due/normal observation");
        require(oracleFastPollWindow(true,70.0,100.0) &&
            !oracleFastPollWindow(false,70.0,100.0) &&
            oracleFastPollWindow(false,85.0,100.0) &&
            !oracleFastPollWindow(true,100.0,100.0) &&
            !oracleFastPollWindow(true,64.9,100.0),
            "device full lead fast scheduling and legacy/deadline boundaries");
        require(oracleMayReserveAction(false,10.0,9.0,1.0,11.0) &&
            !oracleMayReserveAction(true,10.0,9.0,1.0,11.0) &&
            !oracleMayReserveAction(false,10.0,9.1,1.0,11.0) &&
            !oracleMayReserveAction(false,11.0,9.0,1.0,11.0),
            "oracle reservation ignored single-worker, minimum action spacing, or launch deadline");
        // Simulate a slow schedule/preflight pass: the reservation timestamp
        // is the persisted reservation completion, not the stale poll start.
        require(!oracleMayReserveAction(false,11.1,10.2,1.0,12.0) &&
            oracleMayReserveAction(false,11.21,10.2,1.0,12.0),
            "slow preflight caused actual reservation spacing to be measured from stale poll time");
        require(oracleProcFileWriteComplete(1,1) && !oracleProcFileWriteComplete(0,1) &&
            !oracleProcFileWriteComplete(-1,1),"proc_file write return semantics accepted partial/error results");
        require(oracleProcFileFeedbackMayContinue(true,1000,999) &&
            !oracleProcFileFeedbackMayContinue(true,1000,1000) &&
            !oracleProcFileFeedbackMayContinue(true,999,1000) &&
            !oracleProcFileFeedbackMayContinue(false,1000,900) &&
            !oracleProcFileFeedbackMayContinue(true,1000,std::nullopt),
            "file feedback incorrectly accepted nonpositive, unknown, or incomplete identity observations");
        require(oracleAnonPostwriteRecheckEligible(2,"peak_transition_os_snapshot","force_shrink_all","aa_evidence_stale") &&
            oracleAnonPostwriteRecheckEligible(2,"peak_transition_os_snapshot","force_shrink_all","window_evidence_stale") &&
            !oracleAnonPostwriteRecheckEligible(1,"peak_transition_os_snapshot","force_shrink_all","aa_evidence_stale") &&
            !oracleAnonPostwriteRecheckEligible(2,"legacy_two_donor","force_shrink_all","aa_evidence_stale") &&
            !oracleAnonPostwriteRecheckEligible(2,"peak_transition_os_snapshot","proc_file","aa_evidence_stale") &&
            !oracleAnonPostwriteRecheckEligible(2,"peak_transition_os_snapshot","force_shrink_all","aa_telemetry_missing") &&
            !oracleAnonPostwriteRecheckEligible(2,"peak_transition_os_snapshot","force_shrink_all","window_visible") &&
            !oracleAnonPostwriteRecheckEligible(2,"peak_transition_os_snapshot","force_shrink_all","proc_start_ticks_changed"),
            "anonymous postwrite refresh only accepts stale evidence in schema2 profile");
        require(oracleWritebackPostwriteRecheckEligible(3,"peak_transition_os_snapshot","proc_file_writeback","aa_evidence_stale") &&
            oracleWritebackPostwriteRecheckEligible(3,"peak_transition_os_snapshot","proc_file_writeback","window_evidence_stale") &&
            oracleWritebackPostwriteRecheckEligible(3,"peak_transition_os_snapshot","proc_file_writeback","proc_evidence_stale") &&
            oracleWritebackPostwriteRecheckEligible(3,"peak_transition_os_snapshot","proc_file_writeback","lifecycle_evidence_stale") &&
            !oracleWritebackPostwriteRecheckEligible(2,"peak_transition_os_snapshot","proc_file_writeback","aa_evidence_stale") &&
            !oracleWritebackPostwriteRecheckEligible(3,"legacy","proc_file_writeback","aa_evidence_stale") &&
            !oracleWritebackPostwriteRecheckEligible(3,"peak_transition_os_snapshot","proc_file","aa_evidence_stale") &&
            !oracleWritebackPostwriteRecheckEligible(3,"peak_transition_os_snapshot","proc_file_writeback","aa_telemetry_missing") &&
            !oracleWritebackPostwriteRecheckEligible(3,"peak_transition_os_snapshot","proc_file_writeback","proc_start_ticks_changed") &&
            !oracleWritebackPostwriteRecheckEligible(3,"peak_transition_os_snapshot","proc_file_writeback","window_visible"),
            "postwrite recheck eligibility widened beyond schema3 evidence timestamp staleness");
        require(oracleWritebackPreProcRecheckMayResume(3,"peak_transition_os_snapshot","proc_file_writeback",
                "aa_evidence_stale","",true,true,true,true,true,true) &&
            oracleWritebackPreProcRecheckMayResume(3,"peak_transition_os_snapshot","proc_file_writeback",
                "lifecycle_evidence_stale","",true,true,true,true,true,true),
            "pre-proc refresh may resume after exact freshness-only evidence recovers");
        require(!oracleWritebackPreProcRecheckMayResume(3,"peak_transition_os_snapshot","proc_file_writeback",
                "proc_start_ticks_changed","",true,true,true,true,true,true) &&
            !oracleWritebackPreProcRecheckMayResume(3,"peak_transition_os_snapshot","proc_file_writeback",
                "aa_evidence_stale","window_visible",true,true,true,true,true,true),
            "pre-proc refresh must reject hard identity/background failures");
        require(!oracleWritebackPreProcRecheckMayResume(3,"peak_transition_os_snapshot","proc_file_writeback",
                "aa_evidence_stale","",true,true,true,true,true,false) &&
            !oracleWritebackPreProcRecheckMayResume(3,"peak_transition_os_snapshot","proc_file_writeback",
                "aa_evidence_stale","",true,true,true,true,false,true) &&
            !oracleWritebackPreProcRecheckMayResume(3,"peak_transition_os_snapshot","proc_file_writeback",
                "aa_evidence_stale","",true,true,false,true,true,true),
            "pre-proc refresh must preserve deadline, existing reservation and file identity gates");
        require(!oracleWritebackPreProcRecheckMayResume(2,"peak_transition_os_snapshot","proc_file_writeback",
                "aa_evidence_stale","",true,true,true,true,true,true) &&
            !oracleWritebackPreProcRecheckMayResume(3,"legacy_two_donor","proc_file_writeback",
                "aa_evidence_stale","",true,true,true,true,true,true) &&
            !oracleWritebackPreProcRecheckMayResume(3,"peak_transition_os_snapshot","proc_file",
                "aa_evidence_stale","",true,true,true,true,true,true),
            "pre-proc freshness refresh must not alter legacy, v2, or proc_file behavior");
        require(oracleWritebackPostwriteRecheckRemaining(10.0,10.0,20.0)==2.0 &&
            oracleWritebackPostwriteRecheckRemaining(10.0,10.0,11.25)==1.25 &&
            oracleWritebackPostwriteRecheckRemaining(10.0,11.25,20.0)==0.75 &&
            oracleWritebackPostwriteRecheckRemaining(10.0,12.0,20.0)==0.0 &&
            oracleWritebackPostwriteRecheckRemaining(10.0,10.5,10.4)==0.0,
            "postwrite recheck wait exceeded its two-second cap or event deadline");
        constexpr Bytes recheckKeep=512*MiB;
        require(oracleProcFileRecheckFeedbackMayContinue(true,true,1200,1100,700000,recheckKeep) &&
            !oracleProcFileRecheckFeedbackMayContinue(false,true,1200,1100,700000,recheckKeep) &&
            !oracleProcFileRecheckFeedbackMayContinue(true,false,1200,1100,700000,recheckKeep) &&
            !oracleProcFileRecheckFeedbackMayContinue(true,true,1200,1200,700000,recheckKeep) &&
            !oracleProcFileRecheckFeedbackMayContinue(true,true,1200,1100,524000,recheckKeep) &&
            !oracleProcFileRecheckFeedbackMayContinue(true,true,1200,1100,std::nullopt,recheckKeep),
            "postwrite recheck accepted unknown, nonpositive, or below-keep file feedback");
        require(!oracleMayReserveAction(true,10.0,0.0,1.0,20.0),
            "an in-flight worker recheck must continue to block a second action");
        std::cout<<"PREPARATION_WORK_CHECK_BEGIN\n";
        require(oraclePreparationWorkMode(true,true,"force_shrink_all") &&
            !oraclePreparationWorkMode(false,true,"force_shrink_all") &&
            !oraclePreparationWorkMode(true,false,"force_shrink_all") &&
            !oraclePreparationWorkMode(true,true,"force_swapout"),"new work mode escaped device preparation scope");
        OracleRssSample workSample; workSample.pid=1; workSample.uid=1; workSample.startTicks=1;
        workSample.rssAnonKiB=1024*1024; workSample.rssKiB=1600*1024; workSample.vmSwapKiB=32*1024;
        const Bytes wholeWork=1632*MiB;
        require(oraclePreparationWorkEstimate(workSample)==wholeWork &&
            oraclePreparationWorkReservation(workSample,wholeWork+64*MiB,64*MiB)==wholeWork+64*MiB &&
            oraclePreparationWorkReservation(workSample,wholeWork+64*MiB-1,64*MiB)==0,
            "whole work reservation exact room boundary failed");
        auto unknownWork=workSample; unknownWork.rssKiB.reset();
        require(!oraclePreparationWorkEstimate(unknownWork),"missing VmRSS accepted");
        unknownWork=workSample; unknownWork.vmSwapKiB.reset();
        require(!oraclePreparationWorkEstimate(unknownWork),"missing VmSwap accepted");
        unknownWork=workSample; unknownWork.rssKiB=std::numeric_limits<Bytes>::max()/1024+1;
        require(!oraclePreparationWorkEstimate(unknownWork),"VmRSS KiB overflow accepted");
        unknownWork=workSample; unknownWork.vmSwapKiB=std::numeric_limits<Bytes>::max()/1024+1;
        require(!oraclePreparationWorkEstimate(unknownWork),"VmSwap KiB overflow accepted");
        unknownWork=workSample; unknownWork.rssKiB=std::numeric_limits<Bytes>::max()/1024;
        unknownWork.vmSwapKiB=std::numeric_limits<Bytes>::max()/1024;
        require(!oraclePreparationWorkEstimate(unknownWork),"whole work addition overflow accepted");
        auto grewWork=workSample; grewWork.rssKiB=*grewWork.rssKiB+1;
        require(oraclePreparationWorkReservation(grewWork,wholeWork,0)==0,
            "worker work growth did not reject before write gate");
        require(oraclePreparationFinalRatioAllowed(workSample,300*MiB,30,128*MiB,512*MiB),"valid fixed ratio rejected");
        auto depletedWork=workSample; depletedWork.rssAnonKiB=140*1024; depletedWork.rssKiB=700*1024;
        require(!oraclePreparationFinalRatioAllowed(depletedWork,300*MiB,30,128*MiB,512*MiB),"final ratio ignored fresh keep+margin");
        require(!oraclePreparationFinalRatioAllowed(unknownWork,300*MiB,30,128*MiB,512*MiB),"unknown final ratio accepted");
        require(oraclePreparationFeedbackWithinReservation(true,wholeWork,wholeWork) &&
            !oraclePreparationFeedbackWithinReservation(true,wholeWork+1,wholeWork) &&
            oraclePreparationFeedbackWithinReservation(false,wholeWork+64*MiB,wholeWork) &&
            !oraclePreparationFeedbackWithinReservation(false,wholeWork+64*MiB+1,wholeWork),"feedback reservation or legacy boundary changed");
        require(!oracleDiskPreparationComplete(true,"write_failed_or_partial_no_retry",true,1,false) &&
            !oracleDiskPreparationComplete(true,"write_accepted_feedback_unknown",false,1,false),
            "partial or unknown preparation accepted");
        require(!oraclePipelineKeepSatisfied(depletedWork,128*MiB,800*MiB),"total floor failure accepted");
        std::cout<<"PREPARATION_WORK_CHECK_COMPLETE\n";
        std::cout<<"DEVICE_COOLDOWN_SELECTION_CHECK_BEGIN\n";
        using Selection=OracleDeviceCooldownSelection;
        const auto select=[](bool cooling,bool identity,const std::string& failure) {
            return oracleDeviceCooldownSelection(true,2,true,"force_swapout",cooling,identity,failure,false);
        };
        require(select(true,true,"")==Selection::SkipDonor && select(false,true,"")==Selection::Available,
            "cooled a3 did not skip to available a4 selection");
        const std::vector<Selection> allCooled{select(true,true,""),select(true,true,"")};
        require(std::none_of(allCooled.begin(),allCooled.end(),[](Selection value){return value==Selection::Available;}),
            "all cooled donors became available");
        require(select(true,false,"")==Selection::HaltEvent && select(true,true,"uid_mismatch")==Selection::HaltEvent &&
            select(true,true,"aa_not_background")==Selection::HaltEvent &&
            select(true,true,"aa_evidence_stale")==Selection::WaitForProof,
            "cooldown skip bypassed identity/foreground/freshness protection");
        require(oracleDeviceCooldownSelection(true,2,true,"force_shrink_all",true,true,"",true)==Selection::HaltEvent,
            "runtime protected cooldown donor bypassed original halt");
        require(oracleDeviceCooldownSelection(false,2,true,"force_swapout",true,true,"",false)==Selection::LegacyBlocked &&
            oracleDeviceCooldownSelection(true,1,true,"force_swapout",true,true,"",false)==Selection::LegacyBlocked &&
            oracleDeviceCooldownSelection(true,2,false,"force_swapout",true,true,"",false)==Selection::LegacyBlocked &&
            oracleDeviceCooldownSelection(true,2,true,"proc_file_writeback",true,true,"",false)==Selection::LegacyBlocked,
            "cooldown scope changed other execution paths");
        std::cout<<"DEVICE_COOLDOWN_SELECTION_CHECK_COMPLETE\n";
        using Auth=FinalAuthorizationDisposition;
        require(finalAuthorizationDisposition(true,true,"",false,false)==Auth::Allow,"fresh final authorization rejected");
        require(finalAuthorizationDisposition(true,true,"aa_evidence_stale",false,false)==Auth::DeferFreshness &&
            oracleUnsubmittedEvidenceMayDefer("rejected:oracle_evidence_defer:aa_evidence_stale:age_s=2.50134032",false,false,false),
            "final stale did not enter actual no-write result-defer branch");
        require(finalAuthorizationDisposition(true,true,"",false,false)==Auth::Allow,"new fresh evidence did not allow normal checks");
        require(finalAuthorizationDisposition(true,false,"aa_evidence_stale",false,false)==Auth::Reject &&
            finalAuthorizationDisposition(false,true,"aa_evidence_stale",false,false)==Auth::Reject,
            "identity/control failure bypassed by stale");
        require(finalAuthorizationDisposition(true,true,"aa_telemetry_missing",false,false)==Auth::Reject &&
            finalAuthorizationDisposition(true,true,"aa_timestamp_in_future",false,false)==Auth::Reject,
            "unknown time treated as stale");
        require(finalAuthorizationDisposition(true,true,"aa_evidence_stale",true,false)==Auth::Reject &&
            finalAuthorizationDisposition(true,true,"aa_evidence_stale",false,true)==Auth::Reject &&
            !oracleUnsubmittedEvidenceMayDefer("write_accepted_feedback_unknown_after_exception:aa_evidence_stale",true,false,false),
            "submitted unknown effect permitted retry");
        std::cout<<"FINAL_AUTH_STALE_RESULT_ROUTING_CHECK_COMPLETE\n";
        std::cout << "harmony-v1 profile checks passed\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "harmony-v1 profile check failed: " << e.what() << '\n';
        return 1;
    }
}
