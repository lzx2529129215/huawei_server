# 场景 4：匿名冷页与干净文件页回收（短诊断）

> 场景4入口、配置和独立HAP已落包，单应用峰值与8应用daily冷底座校准已通过源绑定复核。2-case pilot已运行，但没有达到完整性或触发目标；校准和pilot均不代表回收服务收益或正式验收。

## 目标与边界

场景4关注一组匿名冷页与可重建的干净文件页，在需求增加时系统是否能回收/换出，并观察回收机会与真实启动失败、内核OOM、低内存通知之间的关系。负载hold期间只等待，不持续重新触碰；不调用MADV_PAGEOUT、不清系统缓存、不使用额外pressure HAP，也不改变系统内存策略。

本轮定位为短诊断，事件目标约20次分类事件；这不是正式门槛。popup和startup事件按各自类别统计；同一内核OOM导致启动未完成时可跨类别关联并分类双计，unique incident另报。真实可见popup须有窗口/mission、前景可见性和截图证据。达到目标数字本身不证明回收敏感性；没有事件或没有观察到实际回收也应如实报告，不得为了凑数提高至超过1.4倍物理内存。

正式扩展仍待后续配置：固定重复用例、独立3轮baseline/optimized比较、真实16GiB设备或厂商支持的物理降容，以及优化版本。scene4短诊断、32GiB预验证或单次校准均不能替代这些条件，也不应改变scene1、scene2、scene3的配置或历史结果。

## 当前负载预算

当前设备实测MemTotal为33,463,963,648B（约31.17GiB）；每次执行前仍应读取真实MemTotal并确认与配置匹配。当前负载如下，MiB按二进制MiB计算：

| 阶段 | 每个测试应用 | 8个应用合计 | 占31.17GiB的近似值 |
|---|---:|---:|---:|
| daily底座 | 2400MiB：1200MiB匿名 + 1200MiB文件映射 | 18.75GiB | 60.1% |
| peak目标 | 5184MiB：3984MiB新增匿名 + 保留1200MiB文件映射 | 40.5GiB | 129.9% |

这些是应用分配/触碰目标，不是RSS、PSS或同步驻留量。single-app 5184MiB峰值已实际触碰。8-app daily阶段应用报告总触碰18.75GiB，而独立进程快照的PSS+SwapPss约19.10GiB，低于该次MemTotal；这两个数是不同口径，PSS+SwapPss包含运行时额外占用，不能当作目标VMA驻留量。daily文件VMA的clean/RSS/PSS合计9.375GiB。pilot峰值增长约21.75GiB，仍须观察系统真实回收、换出及进程状态，不能仅凭分配数字预测结果。

当前pilot固定为2个case：popup保持60秒、startup观察15秒，各1次，共8个应用；不因事件总数接近20而提前停止。原先4-case重复结构尚未冻结。校准使用独立的single-app 5184MiB峰值和8-app 2400MiB daily冷底座，不用校准轮替代pilot。

## 已完成校准与证据限制

校准来源为`results/scene4_baseline_20261007_133550_s4_77f831f1a963/`，源绑定复核文件为`results/scene4_calibration_correction_20261007.json`，校正后摘要为`results/scene4_calibration_corrected_20261007.json`。源result和校正摘要SHA与复核记录一致；8个daily冷底座进程的maps/smaps/stat原始文件及single-app峰值原始文件SHA均通过复核。校正仅修复身份关联：旧逻辑查找不存在的journal字段`daily_ready.start_ticks`，改由journal PID/UID关联保存的直接proc start_ticks和同身份快照。原始`result.json`及原始校准状态仍保留`measurement_complete=false`，没有被改写；derived source-bound correction标记校准完整且失败项为空。

single-app a1实际触碰峰值5,435,817,984B（5184MiB），同一PID/UID/start_ticks在peak快照中有19个目标文件VMA，总size/RSS/PSS/Private_Clean均为1200MiB；daily阶段`MS_SYNC`成功并同步1200MiB。peak阶段只增加匿名区，因此不要求重复sync，`peak_sync_success=false`不代表文件映射同步失败。

8-app daily阶段8/8身份连续、触碰和sync证据完整；每个目标文件有19个VMA、size/RSS/PSS/Private_Clean均为1200MiB，VMA device/inode与owned backing file的stat匹配。8 app touched合计20,132,659,200B（18.75GiB）；PSS合计20,480,265,216B，SwapPss合计30,679,040B，二者合计20,510,944,256B（约19.10GiB）。匿名PSS为10,328,328,192B（约9.62GiB），目标文件clean resident合计10,066,329,600B（9.375GiB）。结束时8个自有应用进程和对应文件均完成清理核验。

该次约20.8秒冷置窗口内，目标文件VMA的RSS/PSS/clean没有下降，pgsteal和scan计数也没有变化；因此校准只证明负载、同步文件页、目标VMA和容量基础成立，没有观察到该窗口内的文件页回收，也不据此评价系统服务是否有效。原始失败标记保留，校正依据和源哈希可追溯；原始运行测量时未记录runner源代码hash，因此不能声称复核文件绑定了测量时源码版本。

## 历史2-case pilot（非当前重复验证结果）

试测目录为`results/scene4_baseline_20261007_134527_s4_aa830362b043/`，逐项独立审阅见[scene4 pilot审阅报告](../../results/scene4_pilot_audit_20261007/report.md)，源绑定事件证据见`results/scene4_pilot_audit_20261007/source_review.json`。原始result记为OOM0、startup failure0、timeout4、native popup2；独立按kernel raw、PID/UID/start_ticks和journals复核后，派生分类为OOM4、关联startup failure4、timeout0、native popup2，分类事件合计10、unique6。OOM与同一incident的启动失败双分类但unique只计一次；没有policy kill或测试popup。分类合计低于18–24描述性目标，且触发类别不齐。原始run完整性标记保持false，不能视为qualified。

四个OOM都有case内kernel victim、快速进程退出、direct `/proc` 身份、daily/promote/peak journal和窗口marker证据；bundle前缀未适配导致原parser漏计，源复核只修正派生分类，不改raw。辅助OOM trace stats路径缺失，hisysevent另有解析错误，所以整体attempt/measurement完整性仍未成立。所有8个自有应用和16个case文件均完成清理核验。

8个app daily阶段的目标文件VMA均有1200MiB clean resident且sync成功；同身份逐app peak快照中VMA仍存在而RSS/PSS/clean降至0，匿名PSS与SwapPss增加。快照并非8 app同步峰值；MemAvailable已经包含可回收clean file页，不能把clean file另加到MemAvailable计算预算。该观察不能归因给特定厂商回收服务，也不能断言16GiB或优化版本效果。

## 固定4-case重复验证（新配置）

短诊断重复配置为`config/scenarios_scene4_cold_reclaim_repeat4.json`。固定执行顺序是popup×2（各hold 60秒），再startup×2（各hold 15秒）；每类第2个case将bundle顺序旋转1位。每case仍有8个scene4应用，daily 2400MiB（文件映射1200MiB+匿名1200MiB），peak 5184MiB（保持文件映射并加匿名），seed沿用20261005。完整跑完4 case，不因分类事件达到约20而提前停止。此次目标是获得约18–24个分类事件并观察启动失败、OOM和native/test popup；目标未命中、某类为0或采集不完整都照实报告，不将该短跑称为formal qualification。

本轮使用同参数、同HAP的逐应用5.184GiB peak与2.4GiB daily per-app校准作为容量参考。校准来源来自较早suite，suite hash不同；它不替代repeat4运行的source hash，也不作为验收资格门槛。每次仍核对当前配置DRAM、实际MemTotal、case参数和应用身份。

```powershell
Set-Location 'D:\Lzx\10.5华为pc项目\交付\内存场景复现包_20261006'
py -3 -X utf8 .
un.py --config .\config\scenarios_scene4_cold_reclaim_repeat4.json preflight --offline
py -3 -X utf8 .
un.py --config .\config\scenarios_scene4_cold_reclaim_repeat4.json run --scene 4 --phase baseline --profile development --rounds 1
```

结果进入新的`results\scene4_baseline_*`目录。报告应逐case保留startup/OOM/policy/native popup/test popup/unknown timeout/unique incident和观察窗口，区分原始统计与可复核的派生分类；确认OOM需要case内kernel victim、匹配PID/UID/start_ticks与进程退出，不用parser未匹配的0代替原文核查。测量完整性和辅助采集错误分别说明。目标VMA的clean RSS/PSS只有在同一存活身份、映射持续存在的前后快照中比较；每app peak采样不等于8 app同步峰值。该配置只供短诊断，baseline/optimized同配置可比较现象但未满足真实16GiB、固定正式轮次、baseline/optimized各3轮、场景2分类事件100和30%目标等正式条件。

## 最新四例实测（2026-10-07）

最新固定4-case运行目录为`results/scene4_baseline_20261007_140612_s4_7fddd67e7841/`，完整独立报告见[四例短诊断结果](../../results/scene4_quick_20261007/report.md)。实际事件为startup failure 8、kernel OOM 8、native popup 4、test popup/policy/普通timeout 0，分类合计20、unique 12。每例都是2个OOM、同事故关联2个startup failure和1个可见native popup。被source-bound核验过的截图确实显示前景“系统内存不足”TaskManager提示，并与窗口、mission和case时间匹配。source-bound文件计数与原始canonical events逐项一致，raw结果未更改。

原始全局execution、measurement、measurement evidence完整标志均为true，四例daily barrier、kernel dmesg窗口和popup观测完整；`acceptance_valid=false`。四例辅助tracefs per-CPU stats不可读，仍单列为辅助通道缺口。8/8应用无残留，32/32 owned backing pages verified absent。耗时按首case_begin至末case_end为803.51秒；主机从启动至result落盘约847.97秒，含审计目录最终收尾约863.60秒。与场景2历史11例相同case区间1816.87秒相比，case区间约短55.8%。

每例daily cold end时8个身份都存活，目标文件VMA合计9.375GiB RSS/PSS/Private_Clean且同步成功；同一存活进程的peak样本中目标VMA仍映射但RSS/PSS/Private_Clean为0。该现象与目标文件页回收相符；匿名页的PSS/SwapPss变化另见原始快照。逐app采样不同步，峰值需求40.5GiB不是同步驻留量。该结果是约20分类事件的32 GiB短诊断，不能证明HarmonyOS特定服务改善，也不构成16 GiB、优化版、30%或正式重复轮验收。

原始计数已为8/8/4；此前source-bound重放曾将事件重复追加到36，目前文件已修正为20/12，与raw一致。辅助trace统计缺失不得误写为OOM为零，也不应称所有辅助capture均完整。

## 需要保存的证据

文件映射必须记录创建/同步结果：对文件映射的msync需返回成功，日志记下映射身份、文件长度和校验/写入状态；持有期间应用进程和对应VMA应持续存在。若msync失败、映射消失或身份不匹配，该文件页不能作为“干净可回收页”证据。

每个case应尽量在同一主机单调时钟/设备uptime时间线上保存daily-ready、peak开始/到达、hold结束、kernel观察窗口起止、popup截图和cleanup时间。保存每个负载进程的PID、UID、start_ticks，并按UID/bundle关联，避免PID复用或把其他同UID进程混入。

进程内存快照应从smaps或smaps_rollup读取可用字段：RssAnon、RssFile、Pss_Anon、Pss_File、Private_Clean、Private_Dirty、Shared_Clean、Shared_Dirty、SwapPss，以及相关文件VMA的地址范围、权限、文件路径和Rss/Pss。字段缺失应记为unknown，不能填0。系统同一时间点记录MemAvailable、Cached、Inactive(anon/file)、Dirty、SwapFree以及可用的vmstat回收、swap和refault计数。

只有在文件msync成功、VMA仍映射且应用仍存活时，观察文件页Rss/clean页下降，并结合系统Cached/Inactive(file)、回收/换出计数变化，才可描述为“观察到与文件页回收一致的变化”；这仍是诊断关联，不单独证明所有变化由该策略造成。进程退出、force-stop或cleanup后的内存下降不能算作回收。匿名页要分别观察RSS与SwapPss/系统Swap变化，不把分配量当作驻留量。

PSS与SwapPss是比例/交换相关口径，不能改称“私有内存”或“物理RSS”；不同时间各应用峰值求和也不能当作同一时刻的同步驻留总量。匿名与文件的resident、proportional、swap和系统级指标应分列。

## 事件与结论口径

- 启动失败只在真实启动尝试有对应证据时计入；普通超时、预备阶段中止和cleanup不计为OOM。
- 内核OOM需由内核记录的victim身份、UID/PID/start_ticks、时间窗内进程退出等证据确认。关联的启动失败可以同incident跨类别记录，unique incident单独去重。
- 低内存提示需区分系统原生可见弹窗与测试应用实际收到LOW/CRITICAL回调后的提示；被前景系统窗口遮挡的后台请求不算可见popup。
- 采集不完整、身份缺口、窗口marker缺失、文件映射/同步失败应分别列明。unknown不能报告为零。
- 若回收充分、没有真实故障或未达到约20分类事件，照实报告；不提升超过候选上限来追事件数。

## 当前可用入口与交付文件

scene4配置为 `config/scenarios_scene4_cold_reclaim.json`；正式构建的8个独立测试HAP为 `bin/scene4_apps/a1.hap` 至 `a8.hap`，包名为 `io.memscene.scene4.workload.a1` 至 `.a8`。源代码位于 `apps/scene4_workload/`。构建来源和逐HAP SHA记录在 `results/scene4_build/`、`bin/scene4_apps/manifest.json`；source tree SHA记录为 `57f54b565855b1dccf6c98a016ed5bb618219d450b6b331c520f90511f302997`。这些HAP使用本设备UDID对应的OpenHarmony SDK debug签名；复现不要求用户重签。

在包根运行以下命令。离线检查不连接设备，也不验证物理内存或安装状态：

```powershell
Set-Location 'D:\Lzx\10.5华为pc项目\交付\内存场景复现包_20261006'
py -3 -X utf8 .\run.py --config .\config\scenarios_scene4_cold_reclaim.json preflight --offline
```

scene4 HAP 位于独立目录。包内通用 `tools\install_apps.ps1` 用于原有 `bin\apps`；scene4请单独执行下列安装脚本。它从scene4 manifest读取HAP并逐个验SHA、安装后读取bundle UID，结果写入 `results\scene4_install\install_record.json`；只作用于scene4新包名：

```powershell
py -NoProfile -ExecutionPolicy Bypass -File .\tools\install_scene4_apps.ps1 -Device 3QC0124C03000514
```

先运行获准的候选校准：单个应用校准5184MiB峰值，然后8个应用并发保持2400MiB daily底座并保存短时冷置前后快照。校准是容量和证据准备，不是服务改善结论：

```powershell
py -3 -X utf8 .\run.py --config .\config\scenarios_scene4_cold_reclaim.json calibrate --scene 4 --profile development --calibration-output .\results\scene4_calibration_candidate.json
```

需要负责人审核校准结果及pilot预算后，再运行固定的两个case。该校准/早期pilot配置为popup与startup各1次，仅作历史短诊断，不作为当前固定重复配置：

```powershell
py -3 -X utf8 .\run.py --config .\config\scenarios_scene4_cold_reclaim_repeat4.json run --scene 4 --phase baseline --profile development --rounds 1
```

结果写入新的 `results\scene4_calibrate_*` 或 `results\scene4_baseline_*` 目录。校准/运行报告应检查各case的daily barrier、PID/UID/start_ticks、`file_sync_proofs`、`reclaim_diagnostics`、`scene4_target`、窗口内事件和owned-app/page cleanup。采集器同时保存目标case backing file对应VMA的`/proc/PID/smaps`字段（Rss/Pss/clean/dirty/swap）及进程级`smaps_rollup`。两类数据应分开解释：目标VMA可以给出该映射自身驻留变化；rollup中的RssFile/Pss_File是整个进程文件映射汇总，不能把它变化归因到单个VMA。字段缺失或unknown不得当作0。场景4当前没有独立的通用freeze/compare入口；最新四例的source-bound分类复核文件已随运行保存，但不得套用scene2资格冻结或将本轮写成正式结果。

本配置的当前用例为popup 60秒、startup 15秒，daily 2400MiB（1200MiB文件映射+1200MiB匿名），peak 5184MiB（保留1200MiB文件映射并增加匿名负载），每类各1次、OOM类0次，固定种子20261005。设备连接后仍须以实际MemTotal确认场景预算约束。CLI虽允许同配置的`baseline`和`optimized` phase，但scene4只作敏感性诊断，不会生成正式优化比较或达标结论。正式固定重复轮次、三轮baseline/optimized比较和真实16GiB验收仍待后续实现及独立环境确认；校准已完成；2-case pilot派生核验得到10类事件/6个unique（OOM4、关联启动失败4、native popup2），低于目标；原始run仍不完整，见`results/scene4_pilot_audit_20261007/report.md`。目前没有优化服务对照结果。
