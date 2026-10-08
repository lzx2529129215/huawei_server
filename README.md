# 鸿蒙 PC 应用内存场景

本项目用已安装的真实应用和自有测试应用执行两类内存场景，保存设备环境、系统事件、应用日志、内核跟踪和结果报告。正式验收要求真实设备、真实事件与完整证据；脚本不会伪造系统通知或改写系统内存策略。

## 先看清楚设备和配置

当前 `config/scenarios.json` 对应 32 GiB 设备。`config/scenarios16.json` 是 16 GiB 参数模板，**不代表当前设备已经变成16 GiB，也不能靠修改 `dram_gib` 冒充物理容量**。当前记录的设备是 32 GiB HarmonyOS PC（serial `3QC0124C03000514`）。16 GiB正式验收必须连接厂商支持的真实16 GiB设备或使用厂商认可的真实降容方式；预检会按 `/proc/meminfo` 实测值核对，验收运行也会因容量不匹配而失败。

16 GiB模板给8个测试应用各分配并触碰3072 MiB，共24 GiB（DRAM的150%）；场景1另行启动8192 MiB pressure，这部分**不计入**24 GiB自有应用触碰量。压力分配和Hyperhold写入、应用Eswap增量都需要设备实际观测。场景2的daily/peak/档位只是重新校准的起始参数：单个应用峰值不得超过DRAM，所有应用daily总量不得超过DRAM，独立测得的peak总量须至少达到DRAM的120%。模板取 daily 256 MiB、peak 4096 MiB、40个用例周期；实际值须由校准和日志确认，不能仅凭配置宣称达标。

项目根目录示例：`D:\Lzx\10.5华为pc项目`。以下项目命令均在 PowerShell 中从项目根目录执行。Python 需可通过 `py -3` 调用；runner和构建工具使用配置/参数里的HDC完整路径，因此无需把HDC加入系统PATH。

直接在命令行检查设备时，CMD与PowerShell的调用语法不同。CMD中给可执行文件路径加双引号：

```bat
"D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe" -t 3QC0124C03000514 shell "cat /proc/meminfo"
```

PowerShell中用调用运算符`&`：

```powershell
& 'D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe' -t 3QC0124C03000514 shell 'cat /proc/meminfo'
```

如需在当前CMD窗口临时使用`hdc`短名称：

```bat
set "PATH=D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\toolchains;%PATH%"
hdc -t 3QC0124C03000514 shell "cat /proc/meminfo"
```

如需在当前PowerShell窗口临时使用`hdc`短名称：

```powershell
$env:Path = 'D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\toolchains;' + $env:Path
hdc -t 3QC0124C03000514 shell 'cat /proc/meminfo'
```

上面的PATH改动只对执行它的当前窗口有效；新窗口会恢复原值。项目无需持久修改系统PATH。

**参数位置很重要：** `--config` 是顶层选项，必须放在 `preflight`、`run`、`calibrate`、`freeze`、`compare` 子命令之前。例如：

```powershell
py -3 .\run.py --config .\config\scenarios16.json preflight
```

不要写成 `py -3 .\run.py preflight --config ...`。不指定 `--config` 时使用32 GiB默认配置。

## 一次性准备

先只读检查目标设备和六个真实应用、八个测试应用是否安装：

```powershell
py -3 .\run.py --config .\config\scenarios.json preflight --output .\results\preflight.json
```

检查 `results\preflight.json` 和终端输出里的 `MemTotal`、`SwapTotal`、`reported_dram_gib` 与安装状态。预检退出码为0表示列出的应用已安装；容量资格仍要结合正式运行检查。16 GiB准备时改用 `scenarios16.json`，但如果实测容量不是16 GiB附近，不要开始正式验收。

构建并安装八个自有测试HAP（使用项目配置的DevEco Studio和设备序列号）：

```powershell
py -3 .\tools\build_apps.py --studio 'D:\Lzx\deveco studio\dev eco\DevEco Studio' --target 3QC0124C03000514 --count 8 --install
```

只构建、不安装时去掉 `--install`。构建结果在 `bin\apps\a1.hap` 至 `a8.hap`，清单为 `bin\apps\manifest.json`。工具将构建缓存放在ASCII临时目录，避开Hvigor对中文项目路径的限制；签名使用本机OpenHarmony SDK debug签名并绑定设备UDID，不需要用户私钥。随后编译原生pressure程序：

```powershell
.\tools\build_pressure.ps1 -Studio 'D:\Lzx\deveco studio\dev eco\DevEco Studio'
```

产物为 `bin\pressure`。安装测试HAP和构建pressure属于准备步骤；`run.py`的场景运行会检查应用与pressure产物。构建或签名失败时先处理对应错误，不要把缺失HAP的运行结果当作有效数据。

## 先做流程预验证

Smoke会大幅缩小负载，只用于检查端到端流程，不可提升为验收证据：

```powershell
py -3 .\run.py --config .\config\scenarios.json run --scene 1 --phase baseline --profile smoke
py -3 .\run.py --config .\config\scenarios.json run --scene 2 --phase baseline --profile smoke --subcase combined
```

正式跑高负载前，建议用development逐项观察。Development默认每条命令一轮，场景2可以选 `startup`、`oom`、`popup` 或 `combined`：

```powershell
py -3 .\run.py --config .\config\scenarios.json run --scene 1 --phase baseline --profile development
py -3 .\run.py --config .\config\scenarios.json run --scene 2 --phase baseline --profile development --subcase startup
py -3 .\run.py --config .\config\scenarios.json run --scene 2 --phase baseline --profile development --subcase oom
py -3 .\run.py --config .\config\scenarios.json run --scene 2 --phase baseline --profile development --subcase popup
py -3 .\run.py --config .\config\scenarios.json run --scene 2 --phase baseline --profile development --subcase combined
```

`startup`、`oom`、`popup`分别运行各自触发策略；`combined`按 popup、startup、oom 顺序运行。启动失败只计有 ENOMEM 或能关联到真实内存杀原因的失败；普通就绪超时会单列为 `startup_timeout`。`oom_kill`要求内核 `oom:mark_victim` PID能与本轮实际观测到的进程身份、存活时间关联。native低内存弹窗要求有可见UI归属；测试应用弹窗还须有真实LOW/CRITICAL回调、弹窗请求，以及同一case截图中测试应用为focused owner的可见证据；被系统弹窗遮挡的后台AlertDialog窗口不计作可见弹窗。policy kill会单独记作 `memory_policy_kill`，不会冒充kernel OOM。无事件的小负载预验证只说明流程跑通。

## 场景1：六个真实应用切换、pressure和复用缺页

固定顺序是腾讯会议→浏览器→海泰浏览器→文件管理器→WPS→应用市场→腾讯会议→浏览器。前六次启动和末尾两次重访，各自记录一次launch和一次swipe，共16个action。八个测试应用在真实应用序列外启动并等待ready；正式验收要求它们实际触碰的私有负载合计不少于DRAM的150%，pressure不计入。pressure退出后再测两次重访进程的minor/major faults；`reuse_faults`是这两次重访主指标，完整窗口的进程计数只作为first-sample下界诊断，不能当成全窗口精确总量。匿名/文件缺页另读内核vmstat字段；若字段缺失保留unknown。系统swap写计数与各真实app Eswap按各自来源分别记录、解读；单个目标app Eswap为0时报告其诊断值，不单独判无效，也不能将测试app增量计给真实app。

复用测量期间，runner在hitrace启动后尝试启用`sched_process_fork`与`sched_process_exit`，保存enable/tracing状态readback；随后建立fresh reuse baseline并写入`reuse_begin`，结束时再写`reuse_end`。唯一session marker与设备uptime用来对齐trace时间窗。主hitrace完成并导出最终trace文件后，runner在主机解析marker和fork/exit行；不会从活动trace stdout过滤读取。child必须在ending snapshot有匹配PID/start_ticks身份；仅在中间采样出现、但ending时已退出或身份不匹配，会记录counter gap并使测量不完整。若已知child能力探测缺少fork行，即使enable读回为1，也不能将目标child计数缺口当作0或声称生命周期覆盖完整。runner优先使用独立tracefs实例；回退到全局事件时会保存并恢复原enable值。当前设备探测的trace_clock为boot/mono、tracing_on保持1，主hitrace marker和若干exit事件可在最终trace中看到；但tracefs直读为空，且已知shell child未产生可见`sched_process_fork`行，设备fork事件捕获能力仍未知；本地finalized-trace解析器没有按fork过滤，而OpenHarmony官方[hitrace文档](https://github.com/openharmony/docs/blob/master/en/application-dev/dfx/hitrace.md)只说明`sched`为内核调度tag，未说明此版本单个fork tracepoint是否采集。探测中Hitrace缓冲配置从294912 KiB临时变为65536 KiB后恢复；overrun统计未知。Scene1新采集还分别记录anon/file缺页类别、VIRT/RSS/smaps_rollup与逐个测试app touched bytes；不完整或缺失的vmstat值保留unknown，owned background pages文件清理需有readback。

Development单轮示例：

```powershell
py -3 .\run.py --config .\config\scenarios.json run --scene 1 --phase baseline --profile development
```

正式基线和优化版各跑10轮（需实际16 GiB设备；优化版应是用户提供的优化HAP/代码版本）：

```powershell
$scene1BaselineStart = Get-Date
py -3 .\run.py --config .\config\scenarios16.json run --scene 1 --phase baseline --profile acceptance --rounds 10
$baseline = @(Get-ChildItem .\results -Directory -Filter 'scene1_baseline_*' | Where-Object { $_.CreationTime -ge $scene1BaselineStart } | ForEach-Object { Join-Path $_.FullName 'result.json' })
if ($baseline.Count -ne 10) { throw "本次基线结果应为10份，实际$($baseline.Count)份；请先核对输出目录" }

$scene1OptimizedStart = Get-Date
py -3 .\run.py --config .\config\scenarios16.json run --scene 1 --phase optimized --profile acceptance --rounds 10
$optimized = @(Get-ChildItem .\results -Directory -Filter 'scene1_optimized_*' | Where-Object { $_.CreationTime -ge $scene1OptimizedStart } | ForEach-Object { Join-Path $_.FullName 'result.json' })
if ($optimized.Count -ne 10) { throw "本次优化版结果应为10份，实际$($optimized.Count)份；请先核对输出目录" }
$compareArgs = @('--config', '.\config\scenarios16.json', 'compare', '--baseline') + $baseline + @('--optimized') + $optimized + @('--output', '.\results\scene1_comparison.json')
py -3 .\run.py @compareArgs
```

上面的筛选只收集记录开始时间之后新建的结果目录。先记录baseline的开始时间、完成baseline并保存结果数组，再执行优化版；这些代码块请在同一个PowerShell窗口依次运行。`compare`要求每版本恰好10个有效acceptance结果、设备/容量/测试应用身份及工作负载一致。场景1目标为复用缺页均值下降至少20%，挑战目标30%。

## 场景2：触发分类、峰值校准与冻结用例

所有应用先进入daily-ready屏障，之后才统一晋升peak；发送peak命令前会再次确认daily-ready日志中的PID、UID和进程start time仍与设备上的存活进程一致。正式场景2只接受实测16 GiB设备。Baseline需真实复现启动失败、kernel OOM和低内存弹窗三类，固定用例集分类事件数合计至少100；事件按来源去重，同一incident跨类别保留关联。普通超时单列为未确认事件，policy kill单独记录且不能冒充kernel OOM。

每个测试应用在每个case中的终态可由两类证据完成：完整成功路径（peak开始、达到peak并进入ready），或来源确认的内存失败（例如peak阶段ENOMEM、已归属到该进程的kernel OOM或内存策略杀）。真实失败终态不需要再伪造peak-ready事件；普通超时、采样缺失或身份无法归属仍属未确认，不能计作完整测量。结果中的`outcome`会标为`success`、`confirmed_memory_failure`或`unconfirmed`。Baseline仍必须满足三类事件和分类事件数合计至少100；`unique_incidents`另行报告，同一incident跨类别分别计入所属分类。优化版允许某类计数为0，但其各case仍须成功完成或有可归属的真实内存失败终态，且证据采集完整。

32 GiB设备当前已有一份**预验证冻结**。原始资格运行的`result.json`仍保留runner的`measurement_complete=false`和原始计数138/75；另有离线、源文件哈希绑定的UI审计，核对11个case、88/88对journals、内核与UI证据，并剔除1条被TaskManager遮挡的测试弹窗，审计计数为137类事件/74 unique。审计验证成功后冻结文件记录`prevalidation_32gb`范围；这不把raw结果改写成`measurement_complete=true`，也不构成16 GiB正式资格。

可用下列命令复核审计并重建32 GiB预验证冻结（`--config`仍位于子命令前）：

```powershell
$qualRun = '.\results\scene2_baseline_20261006_180114_s2_00d7aec837f3'
py -3 -X utf8 .\tools\audit_scene2_ui.py --result (Join-Path $qualRun 'result.json')
py -3 -X utf8 .\tools\audit_scene2_ui.py --verify (Join-Path $qualRun 'qualification_ui_audit.json')
py -3 -X utf8 .\run.py --config .\config\scenarios.json freeze --baseline (Join-Path $qualRun 'result.json') --calibration .\results\calibration_qualification32.json --audit (Join-Path $qualRun 'qualification_ui_audit.json') --output .\config\frozen_scene2_prevalidation32.json
```

当前已保存的冻结文件为`config\frozen_scene2_prevalidation32.json`，scope为`prevalidation_32gb`、DRAM为32 GiB、包含11个固定case；suite hash为`e7eece713001e722180fa6ccfb51078487b4e9dea0b95bd096426f33f28f7759`。审计source hash指向原始`result.json`，校准`checks.valid=true`且覆盖实际使用的5120/12288 MiB档。该冻结文件不能用于16 GiB验收。

先在与设备容量匹配的配置、同一版本上做单应用校准（八个应用各自独立测量）。资格阶段与正式acceptance分开：qualification只用于检查场景、校准并生成固定用例，不计入正式baseline轮次。qualification要求`dram_gib`与实测容量匹配（当前支持16/32 GiB配置）、固定combined用例完整、每case daily屏障完成、三类事件均出现，且启动失败、OOM、低内存弹窗的分类事件数合计至少100。`unique_incidents`另行报告；同一incident跨类别的归属保留，不用unique数量代替分类事件合计门槛。32 GiB qualification/freeze仅是当前设备的预验证工件，不能用于16 GiB正式验收；切换到真实16 GiB设备后必须重新校准、qualification并冻结。下面命令演示16 GiB正式前置流程：

```powershell
py -3 .\run.py --config .\config\scenarios16.json calibrate --scene 2 --phase baseline --profile qualification --rounds 1 --calibration-output .\config\calibration16.json
```

核对校准的每应用daily/peak实测值、单应用peak不超过16 GiB、daily总量不超过16 GiB、peak总量至少19.2 GiB。校准档位是逐应用独立测量，不是声称所有应用能同时达到峰值；用例实跑也必须记录ready/peak或可归属的真实内存失败终态证据。

然后执行一轮qualification baseline，构造当前配置定义的完整combined用例集。此轮必须满足上述资格门槛，输出`qualification_eligible: true`；它不是正式baseline轮次，也不要求`acceptance_valid`：

```powershell
$qualificationStart = Get-Date
py -3 .\run.py --config .\config\scenarios16.json run --scene 2 --phase baseline --profile qualification --subcase combined --rounds 1
```

```powershell
$qualificationRun = Get-ChildItem .\results -Directory -Filter 'scene2_baseline_*' | Where-Object { $_.CreationTime -ge $qualificationStart } | Sort-Object CreationTime -Descending | Select-Object -First 1
if (-not $qualificationRun) { throw "没有找到本次qualification结果目录" }
$qualificationResult = Join-Path $qualificationRun.FullName 'result.json'
```

核对该轮`qualification_eligible`、`qualification_reasons`、`counts`、`unique_incidents`和`cases_hash`，并核对同配置校准的`checks.valid`。通过后冻结：

```powershell
py -3 .\run.py --config .\config\scenarios16.json freeze --baseline $qualificationResult --calibration .\config\calibration16.json --output .\config\frozen_scene2_16.json
```

冻结后再跑正式acceptance baseline三轮，且只收集本次新产生的三个独立结果：

```powershell
$scene2BaselineStart = Get-Date
py -3 .\run.py --config .\config\scenarios16.json run --scene 2 --phase baseline --profile acceptance --subcase combined --rounds 3 --frozen .\config\frozen_scene2_16.json
```

每轮检查 `result.json`：`status`须为`valid`，三类覆盖齐全，分类事件数合计`counts.total_events >= 100`，`measurement_complete`与`acceptance_valid`为true，且case hash一致。将本次三份正式基线的结果路径收集起来，并确认数量：

```powershell
$baseline = @(Get-ChildItem .\results -Directory -Filter 'scene2_baseline_*' | Where-Object { $_.CreationTime -ge $scene2BaselineStart } | ForEach-Object { Join-Path $_.FullName 'result.json' })
if ($baseline.Count -ne 3) { throw "本次基线结果应为3份，实际$($baseline.Count)份；请先核对输出目录" }
```

然后用同一冻结文件在优化版上跑3轮，确保两版用例集完全一致。优化版允许真实事件类别为0，但每个case仍须测量完整且每应用均有完整成功或来源确认的内存失败终态：

```powershell
$scene2OptimizedStart = Get-Date
py -3 .\run.py --config .\config\scenarios16.json run --scene 2 --phase optimized --profile acceptance --subcase combined --rounds 3 --frozen .\config\frozen_scene2_16.json
$optimized = @(Get-ChildItem .\results -Directory -Filter 'scene2_optimized_*' | Where-Object { $_.CreationTime -ge $scene2OptimizedStart } | ForEach-Object { Join-Path $_.FullName 'result.json' })
if ($optimized.Count -ne 3) { throw "本次优化版结果应为3份，实际$($optimized.Count)份；请先核对输出目录" }
$baselineAudits = foreach ($r in $baseline) {
    $a = Join-Path (Split-Path -Parent $r) 'scene2_ui_audit.json'
    py -3 -X utf8 .\tools\audit_scene2_ui.py --result $r --output $a
    if ($LASTEXITCODE -ne 0) { throw "baseline UI audit失败：$r" }
    $a
}
$optimizedAudits = foreach ($r in $optimized) {
    $a = Join-Path (Split-Path -Parent $r) 'scene2_ui_audit.json'
    py -3 -X utf8 .\tools\audit_scene2_ui.py --result $r --output $a
    if ($LASTEXITCODE -ne 0) { throw "optimized UI audit失败：$r" }
    $a
}
$compareArgs = @('--config', '.\config\scenarios16.json', 'compare', '--baseline') + $baseline + @('--optimized') + $optimized + @('--baseline-audit') + $baselineAudits + @('--optimized-audit') + $optimizedAudits + @('--output', '.\results\scene2_comparison.json')
py -3 .\run.py @compareArgs
```

场景2的变量命令代码块也请在同一个PowerShell窗口按顺序执行。对比要求每版本3个有效正式轮次、相同设备/容量/应用身份和相同冻结case hash；目标为分类事件数均值下降至少30%。Qualification资格结果不计入这3轮。若qualification轮测量不完整、三类缺任何一类、分类事件合计不足100、校准不达组合容量或固定用例不匹配，则不能冻结；正式轮次不完整或证据不足也不能通过对比。若当前只做32 GiB流程预验证，使用`scenarios.json`重复校准、qualification和freeze，并将冻结文件明确命名为32 GiB预验证文件；该文件及其轮次不得送入16 GiB正式compare。

## 结果目录与状态

每轮会在 `results\scene1_*` 或 `results\scene2_*` 创建独立目录。常见文件包括：

- `result.json`：机器可读结论、环境、计数、用例哈希、完整性与验收标记。
- `report.md`、`events.csv`、`events.json`：摘要和归类后的事件。
- `environment.json`、`config.json`、`progress.json`：设备实测、该轮配置与执行标记。
- `trace.ftrace`及trace日志：hitrace原始跟踪；场景2另存`kernel_trace`与`system_events`。
- `journals`：测试应用逐进程分配/通知记录；`ui`和PNG截图：UI树与弹窗可见证据。
- `samples.jsonl`、`actions.json`、`pressure.log`：内存采样、场景1动作、pressure分配记录；场景2另有`cases.json`和`case_results.json`。

`incomplete`表示运行尚未正常收尾；`invalid`表示执行或证据检查失败；`prevalidated`表示流程完成、当前结果仍不具备正式验收资格（例如smoke、32 GiB上的场景2）；只有满足配置、容量、证据和轮数条件才会出现`valid`。`execution_complete`只说明动作执行完成，不等价于`measurement_complete`、`acceptance_valid`或对比目标达成。当前本地框架由实现代理报告48项测试、compileall和CLI help检查通过；这些检查不证明目标设备的trace开关可用或高负载验收达标。请以JSON中的字段及`compare`输出为准，不要只看进程退出码或控制台的“结果”路径。

退出与清理：runner在正常完成或捕获异常时会尝试清理本轮自有测试应用、pressure进程、远端临时文件、跟踪状态和本轮创建的应用页面文件；只操作本项目自有测试应用与本轮资源。不要手动清理系统策略、真实应用数据或其他用户进程。中断供电/强制关闭导致未执行finally时，先保留结果目录和设备日志，确认仅有本项目自有残留后再处理。

## 最近设备预验证结果（2026-10-06）

场景2固定11-case资格轮为[180114](results/scene2_baseline_20261006_180114_s2_00d7aec837f3/result.json)。原始计数是startup failure 63、kernel OOM 63、native弹窗11、测试弹窗1，合计138、unique75；原始文件仍为`measurement_complete=false`。源文件哈希绑定的[离线UI审计](results/scene2_baseline_20261006_180114_s2_00d7aec837f3/qualification_ui_audit.json)确认唯一测试弹窗被TaskManager模态遮挡，移除该事件后分类计数为137、unique74；11个case核心测量、88/88对journals和内核/弹窗证据完整，audit `qualification_eligible=true`，scope为`prevalidation_32gb`。审计的`--verify`已返回`verified=true`，并实际生成[32 GiB预验证冻结文件](config/frozen_scene2_prevalidation32.json)。冻结与派生审计不改写原始结果，也不代表16 GiB正式验收。

本地SDK Hiperf做过一次无压力小探针：父子进程各mmap并逐页触碰4 MiB，`hiperf stat -e sw-page-faults -p <parentPID> -i 1000 -d 12`最终计数639、coverage 100%、退出码0。观测值在父进程触碰后先升至270，在fork子进程触碰后升至635，说明该设备本次默认继承采集观察到了这个新fork的子进程缺页活动。此样本只证明一个直接子进程；尚未验证已有线程、foundation独立启动的同UID进程或完整真实应用家族，也未证明覆盖了场景1精确复用窗口。单次探针不能替代场景1真实应用窗口；Hiperf可用合并的`sw-page-faults`事件，但单独`sw-page-faults-min`和`sw-page-faults-maj`不支持，minor/major仍由进程计数诊断。探针命令、父/子PID、stat快照和清理读回保存在`results\hiperf_sw_fault_probe_20261006\`；三项设备临时文件已读回确认不存在。该结果不解决场景1当前fork trace覆盖未知问题。

## 当前已知证据边界

现有32 GiB设备上的scene1 smoke完成16个动作，后台总触碰量只有1 GiB。后续145101开发轮触碰48 GiB并clean stop 8 GiB pressure，但trace无效；152756轮同样触碰48 GiB、clean stop 12 GiB pressure，reuse trace缺marker。最新154410轮再次由8个应用同时触碰48 GiB（anon12+file36），12 GiB pressure在reuse baseline前clean stop；8个owned进程和8个backing pages均有清理读回。系统swap写增量为1,435,009,024 bytes且`disk_swap.confirmed=true`，Meet/Browser Eswap均0，文件管理器9,281,536 bytes；测试a1/a2/a3约1.52 GiB分别列出，不记到真实应用名下。Meet/Browser Eswap为0仅作诊断。reuse窗口的独立trace覆盖全部6/6 markers，时钟漂移约-34 ms；Meet 4,990（minor 4,647、major343）、Browser 9,883（minor9,111、major772），合计14,873（minor13,758、major1,115），且两app端点计数valid。整轮仍`measurement_complete=false`：前段trace只有9/16 markers，缺sequence_begin和stage1–3；生命周期trace见1,798 exit但0 fork，fork覆盖能力仍unknown，不能把未观察fork解释为无child。主应用计数与reuse窗口可作预验证诊断，不能进入正式20%比较。

场景2三类固定用例的155919 development pilot完整执行了popup 5 GiB/60秒、startup 12 GiB/0秒及OOM 12 GiB/90秒。原始counts是startup failure9、OOM9、native popup1、test popup1、timeout6；独立截图审计确认test popup被TaskManager模态遮挡且取图标记`case_attribution_allowed=false`，因此派生可见类别合计为19、unique10，原始结果不回写。六个timeout来自OOM类12 GiB/90秒case，六个测试进程停在peak分配中，未达peak/ready，也没有匹配内存故障；该case另外缺kernel dmesg END marker并记录HDC采样超时，导致`measurement_complete=false`。这个pilot不能用于qualification/freeze，也不足以直接定最终重复次数。

此前未通过的固定11-case qualification尝试为[170544运行](results/scene2_baseline_20261006_170544_s2_1d4d2d3973c5/result.json)。原始runner分类计数为startup_failure 57、OOM 57、native popup 9、test popup 1，合计124；`unique_incidents=67`，另外的`startup_timeout=8`不计入三类事件合计。逐张检查的10个事件截图均有可见UI证据：9个是case内新建TaskManager LowMemoryAbility窗口及系统弹窗，1个是a6的CRITICAL回调后、自有测试弹窗在focused窗口中显示且未被遮挡。事件证据的截图完成时间均落在各自测量窗口内。该轮仍不是合格baseline：c0006在daily-ready后缺少a6的可用进程身份，runner未放行daily barrier、没有发送peak命令，8个attempt均未确认；`startup_timeout=8`是该准备阶段缺口的诊断字段，不能解释为真实启动失败或内存杀。另有10个实际测量case的Hitrace均只有measurement-end、缺measurement-begin，c0006未进入测量且没有trace；因此全局`measurement_evidence_complete=false`、`measurement_complete=false`、`qualification_eligible=false`，拒绝freeze。不要根据缺失的身份查询结果倒推出a6进程已经退出，也不要因分类计数达到124而把该轮提升为有效资格结果。

