# 鸿蒙 PC 应用内存场景复现包

这份目录包含可直接运行的测试程序、已签名的8个测试应用、场景配置、校准与冻结文件，以及与最终预验证结果对应的原始证据。请从本README所在目录运行命令；默认使用PowerShell。所有运行结果写入包内`results/`，已归档证据保存在`evidence/`。本说明不需要访问原项目目录。

## 先确认运行环境

需要Windows PowerShell、Python 3（仅使用标准库，无pip依赖）、随DevEco Studio安装的HDC，以及一台工程版本HarmonyOS PC。正式场景运行需要设备保持工程调试权限、HDC连接稳定、测试HAP版本与配置一致。不要更改设备内存策略或手动终止系统进程；测试只清理本项目自有应用和资源。

解压后打开PowerShell，设置工作目录并检查Python和包内主程序：

```powershell
$packageRoot = 'D:\Lzx\10.5华为pc项目\交付\内存场景复现包_20261006'
Set-Location -LiteralPath $packageRoot
if (Get-Command py -ErrorAction SilentlyContinue) { py -3 --version }
elseif (Get-Command python -ErrorAction SilentlyContinue) { python --version; Write-Host '若使用下方命令示例，请将py -3替换为python（并保留-X utf8）' }
else { throw '找不到Python 3；请安装Python 3并将python或py加入PATH' }
if (-not (Test-Path .\run.py)) { throw '当前目录不是复现包根目录' }
py -3 -X utf8 .\run.py --config .\config\scenarios_scene1_full48_pressure12.json preflight --offline --output .\validation\offline_preflight_scene1.json
py -3 -X utf8 .\run.py --config .\config\scenarios_qualification32.json preflight --offline --output .\validation\offline_preflight_scene2.json
```

离线预检只检查包内配置、脚本、HAP/pressure等资源闭包，不连接设备，也不会判断设备内存容量；成功结果应标明`device_checked=false`。若Python或包资源缺失会在开始场景前报错。可再对场景2配置运行同一检查，并将输出保存到`validation/offline_preflight_scene2.json`。真实设备连接与内存容量检查仍需执行下面的在线preflight。

复现包随附HDC和Windows USB依赖。优先检查并调用包内HDC（完整路径以`$packageRoot`为基准）：

```powershell
$device = '3QC0124C03000514'
$hdc = (Resolve-Path -LiteralPath '.\tools\hdc.exe').Path
if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $hdc) 'libusb_shared.dll'))) { throw 'HDC缺少同目录libusb_shared.dll' }
& $hdc list targets
& $hdc -t $device shell 'cat /proc/meminfo'
```

PowerShell调用带空格的exe路径时用`&`调用运算符。也可使用DevEco Studio安装的HDC：`D:\Lzx\deveco studio\dev eco\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe`。需要临时使用`hdc`短名称时，仅修改当前窗口的PATH：

```powershell
$env:Path = (Join-Path $packageRoot 'tools') + ';' + $env:Path
hdc -t $device list targets
```

这不会修改持久系统PATH。CMD中使用完整HDC路径并加双引号：

```bat
"D:\Lzx\10.5华为pc项目\交付\内存场景复现包_20261006\tools\hdc.exe" -t 3QC0124C03000514 list targets
```

`list targets`必须显示目标序列号。设备断开时先检查USB连接、工程调试权限和HDC状态；不要把空设备结果当成零故障测量。配置文件中的HDC路径由包内入口解析；若在另一台Windows电脑上运行，请在该电脑设置 `$hdc` 为它实际安装的HDC完整路径，并检查配置里的`hdc`字段与入口路径解析是否一致。

## 已签名测试HAP：直接安装与可选重建

首选直接使用包内随附的签名HAP，不需要重新签名或用户证书。它们按a1至a8命名，`bin/apps/manifest.json`记录bundle、SHA256及签名设备UDID。先检查8个文件，再选一种方式安装：

```powershell
$haps = Get-ChildItem .\bin\apps -Filter 'a*.hap' | Sort-Object Name
if ($haps.Count -ne 8) { throw "应有8个测试HAP，实际$($haps.Count)个" }
.\tools\install_apps.ps1 -Device $device
```

也可直接调用随包HDC逐个安装：

```powershell
foreach ($hap in $haps) {
    & $hdc -t $device install -r $hap.FullName
    if ($LASTEXITCODE -ne 0) { throw "HAP安装失败：$($hap.Name)" }
}
```

若设备拒绝随附签名，先确认HAP manifest中的设备UDID与当前设备一致。需要在当前设备重新构建时才使用可选流程：

```powershell
py -3 -X utf8 .\tools\build_apps.py --studio 'D:\Lzx\deveco studio\dev eco\DevEco Studio' --target $device --count 8 --install
```

该脚本用本机OpenHarmony SDK debug签名并按设备UDID签发，不需要用户私钥。它会用ASCII临时路径构建，避开中文路径下Hvigor缓存问题。若包内未带`bin/pressure`，可从源码重建：

```powershell
.\tools\build_pressure.ps1 -Studio 'D:\Lzx\deveco studio\dev eco\DevEco Studio'
```

## 只读预检

运行前先确认真实容量、系统换出容量、HAP安装状态和配置：

```powershell
py -3 -X utf8 .\run.py --config .\config\scenarios_scene1_full48_pressure12.json preflight --output .\results\preflight_scene1.json
py -3 -X utf8 .\run.py --config .\config\scenarios_qualification32.json preflight --output .\results\preflight_scene2.json
```

包内`scenarios/scene1/run.ps1`和`scenarios/scene2/run.ps1`会检查可用Python，优先使用`python`，否则尝试`py -3`，两者都缺失时明确报错。`--config`是顶层参数，必须放在子命令`preflight`、`run`、`calibrate`、`freeze`或`compare`之前。预检输出查看`MemTotal`、`SwapTotal`、设备serial、应用安装与pressure程序状态。当前设备实测约32 GiB；`config/scenarios16.json`只是参数模板，不能把配置值改为16 GiB来伪装物理容量。正式16 GiB验收需要真实16 GiB设备或厂商认可的真实降容，并在该设备重新校准、资格验证和冻结。

## 场景1：48 GiB自有负载、应用切换和复用缺页

包内快捷入口默认读取已验证配置：`.\scenarios\scene1\run.ps1 -Profile development -Phase baseline -Rounds 1`。显式等价命令见下方。配置`config/scenarios_scene1_full48_pressure12.json`中8个测试应用各分配6 GiB，其中每个应用75%为file-backed、25%为anonymous；8个应用需在腾讯会议→浏览器→海泰浏览器→文件管理器→WPS→应用市场→腾讯会议→浏览器序列外准备并保持存活。合计48 GiB实际映射且触碰（anon约12 GiB、file约36 GiB）。pressure为12 GiB，另列、不计入48 GiB；pressure须clean stop后再建立fresh reuse baseline。序列共16个launch/swipe动作。执行一轮development预验证：

```powershell
py -3 -X utf8 .\run.py --config .\config\scenarios_scene1_full48_pressure12.json run --scene 1 --phase baseline --profile development --rounds 1
```

本包归档的154410轮证明48 GiB触碰、12 GiB pressure clean stop、系统disk write增加1,435,009,024 bytes，压力释放后fresh baseline以及backing pages清理读回。Meet/Browser的Eswap为0是诊断值，不是单独无效条件；系统disk swap与各真实应用Eswap按来源分开记录。该轮端点fault诊断合计14,873（minor 13,758、major 1,115），**不是有效的完整应用家族主指标**：整轮measurement不完整，前段trace只覆盖9/16 marker，sched fork采集能力仍未知。本包证据未证明reuse测量窗口内所有真实应用进程及其后代都被覆盖；缺失的fork事件不能按零处理，也不能将root进程计数提升为完整应用家族总量。不得用该轮计算20%正式改进；正式比较要求baseline与优化版各10个有效、同设备、同配置、覆盖完整的独立轮次。

## 场景2：固定11-case资格集、审计与32 GiB预验证冻结

包内快捷入口`.\scenarios\scene2\run.ps1 -Profile qualification -Phase baseline -Subcase combined -Rounds 1`默认使用已验证的`config/scenarios_qualification32.json`。此配置为popup 1例（5 GiB/60秒）、startup 9例（12 GiB/0秒）、OOM 1例（12 GiB/15秒），daily为256 MiB，校准各应用×5120/12288 MiB档位独立测量15秒。固定11例按定义全部执行，不达到100就提前停止。baseline资格门槛为启动失败、kernel OOM、可见低内存popup三类均有证据，三类分类事件总和≥100；`unique_incidents`另列，不取代分类总和。daily每应用就绪后才统一promote peak；peak超过设备容量、启动普通超时、后台被遮挡的popup均不能冒充已确认事件。

在目标设备重新校准并运行资格轮的命令如下（会执行设备场景，可能高负载；需先完成工程准备和预检）：

```powershell
py -3 -X utf8 .\run.py --config .\config\scenarios_qualification32.json calibrate --scene 2 --phase baseline --profile qualification --subcase combined --rounds 1 --calibration-output .\results\calibration_qualification32.json
py -3 -X utf8 .\run.py --config .\config\scenarios_qualification32.json run --scene 2 --phase baseline --profile qualification --subcase combined --rounds 1
```

运行结果会在`results/scene2_baseline_*`下生成。每轮检查`execution_complete`、`measurement_complete`、`qualification_eligible`、`qualification_reasons`、各case的daily/terminal evidence、`counts.total_events`与`unique_incidents`。保存原始结果路径后，执行hash-bound UI审计并核对：

```powershell
$result = '.\results\scene2_baseline_实际运行目录\result.json'
$audit = '.\results\scene2_baseline_实际运行目录\qualification_ui_audit.json'
py -3 -X utf8 .\tools\audit_scene2_ui.py --result $result --output $audit --relative-source
py -3 -X utf8 .\tools\audit_scene2_ui.py --verify $audit
```

审计把截图、UI树、mission时间和raw源hash绑定。只有截图证明测试应用弹窗实际可见才计该类事件；被TaskManager模态遮挡的AlertDialog会从审计计数移除，但raw不回写。当前随包180114审计计数为startup63+OOM63+native popup11=137类事件、74 unique，原始结果计138/75（含1条被遮挡的test popup），raw `measurement_complete=false`保持不变。审计逐case确认11例、88/88对journals以及必要kernel/UI证据完整，并生成`qualification_eligible=true`、scope=`prevalidation_32gb`。为了可重定位，包内audit使用相对`source_result`并随原始result/逐case证据重建；audit/frozen文件自身哈希可能与原工作区版本不同，但source hashes绑定包内raw原件，且package manifest校验所有文件。

随包已冻结文件`config/frozen_scene2_prevalidation32.json`只属于32 GiB预验证。以下命令可根据随包证据重新验证并重建到另一个输出文件；不会执行场景：

```powershell
$result = '.\evidence\scene2_qualification_180114\result.json'
$audit = '.\evidence\scene2_qualification_180114\qualification_ui_audit.json'
py -3 -X utf8 .\tools\audit_scene2_ui.py --verify $audit
py -3 -X utf8 .\run.py --config .\config\scenarios_qualification32.json freeze --baseline $result --calibration .\results\calibration_qualification32.json --audit $audit --output .\results\frozen_scene2_prevalidation32_rebuilt.json
```

冻结的case/suite hash、设备、DRAM和校准均需一致。32 GiB预验证冻结文件不能用于16 GiB acceptance。

## 场景3：两例快速预验证

场景3从场景2复用两个已测的压力档，执行一例popup（8个应用各daily 256 MiB后进入5 GiB peak，hold 60秒）和一例startup（各daily 256 MiB后进入12 GiB peak，hold 0秒）。入口使用独立配置`config/scenarios_scene3_quick.json`，固定seed为20261005。它完整执行两例，不会在计数到20时提前停止；“约20次”是三类分类事件总数的描述性目标区间18–24，另要求启动失败、真实kernel OOM和可见低内存popup三类各至少出现1次。每类事件按类别计数，因此同一PID/start_ticks事故同时有startup失败和OOM证据时计入两个类别，`unique_incidents`另行报告。测试应用popup只有在窗口内截图/UI证据证明实际可见时才计数；被系统模态遮挡的请求不算可见popup。

本快速场景不属于场景2固定11例资格集，不要求≥100分类事件，不生成qualification/frozen/acceptance资格，也不能替代32 GiB资格基线或16 GiB正式验收。场景3引用随包既有`results/calibration_qualification32.json`中的8应用×5/12 GiB逐档测量作为负载能力参考；该校准配置的suite hash与scene3不同，且标记`used_as_acceptance_gate=false`，不伪称scene3单独完成了校准。当前32 GiB设备的运行结果只作短时诊断预验证。

在相同HDC、测试HAP和设备条件下先做只读预检，再运行一次完整的两例场景：

```powershell
py -3 -X utf8 .\run.py --config .\config\scenarios_scene3_quick.json preflight --output .\results\preflight_scene3.json
py -3 -X utf8 .\run.py --config .\config\scenarios_scene3_quick.json run --scene 3 --phase baseline --profile development --rounds 1
```

输出写入`results/scene3_baseline_YYYYMMDD_HHMMSS_s3_<id>/`，场景字段为`scene=3`、scope为`short_diagnostic_prevalidation`。检查两个case的`case_measurement_complete`、实际case计数、UI截图和source-bound evidence；总结果中的`scene3_target.target_met`表示本场景描述性目标是否同时满足，`primary_evidence_complete`表示主要证据是否完整。将实际结果路径代入下列命令，生成与result/raw截图绑定的可重定位UI审计，再执行verify：

```powershell
$result = '.\results\scene3_baseline_实际运行目录\result.json'
$audit = '.\results\scene3_baseline_实际运行目录\scene3_ui_audit.json'
py -3 -X utf8 .\tools\audit_scene2_ui.py --result $result --output $audit --relative-source
py -3 -X utf8 .\tools\audit_scene2_ui.py --verify $audit
```

本包已完成一次场景3实测，结果目录为`results/scene3_baseline_20261007_121853_s3_e07404295377/`。审计后分类为startup失败9、kernel OOM 9、可见native popup 2，合计20类事件、11个unique incidents；test popup、普通timeout和memory-policy kill均为0。两case均有完整primary evidence，audit verify成功，描述性`scene3_target.target_met=true`；该结果仍为`short_diagnostic_prevalidation`，不属于qualification/freeze或formal acceptance。首case_begin至末case_end为314.68秒（5分14.68秒），场景2固定11例历史相同标记区间为1816.87秒（30分16.87秒），场景3短约82.7%；runner整条命令主机墙钟约363秒（约6分03秒）。详细事实、时间和限制见`results/scene3_quick_20261007/report.md`。

## 场景4：冷匿名页与干净文件页短诊断

场景4使用独立HAP和配置`config/scenarios_scene4_cold_reclaim_repeat4.json`，固定跑popup×2（hold 60秒）再startup×2（hold 15秒）；每类第二个case轮换bundle顺序。每app daily 2400MiB（匿名1200+文件映射1200MiB），peak 5184MiB。完整跑完4 case，不因接近20次分类事件提前停止。它用于诊断干净文件页与匿名冷页的观测，不属于场景2资格集，也不能替代16 GiB、三轮baseline/optimized、100事件或30%正式门槛。

对应的逐app同参数校准已用于容量参考，因suite hash不同不作为该repeat4配置的资格门槛。场景4步骤、指标解释、结果和限制见[场景4说明](scenarios/scene4/README.md)。先做离线包资源预检，再执行固定4例：

```powershell
py -3 -X utf8 .\run.py --config .\config\scenarios_scene4_cold_reclaim_repeat4.json preflight --offline
py -3 -X utf8 .\run.py --config .\config\scenarios_scene4_cold_reclaim_repeat4.json run --scene 4 --phase baseline --profile development --rounds 1
```

完整性要求、事件分类和文件页/匿名页口径以场景4说明及对应source-bound报告为准；有缺失的辅助观测仍须单独列出，不能用事件存在掩盖整轮coverage状态。

## 场景4最新四例实测结果（2026-10-07）

固定popup×2（hold 60秒）、startup×2（hold 15秒）完整运行。原始canonical events与当前source-bound复核一致：启动失败8、kernel OOM 8、原生可见低内存弹窗4、test popup/policy/timeout均为0；分类合计20、unique incidents 12。每例都是2个OOM、同事故关联2条startup failure和1个native popup。四张窗口内截图及TaskManager mission证据确认系统弹窗可见；32/32 owned pages清理读回不存在，8/8自有应用无残留进程。原始结果SHA为`84c1000feda60dac5444ebf12c057a043b67cc6c8693f80fd15c107028b9deb6`。完整逐项核对见[场景4四例短诊断报告](results/scene4_quick_20261007/report.md)。

四例raw `execution_complete`、`measurement_complete`、`measurement_evidence_complete`均为true；唯一已知覆盖缺口是辅助tracefs per-CPU OOM stats不可读，该缺口单列，dmesg窗口和8条victim身份/退出证据完整。本轮不是正式验收：实机MemTotal约31.17 GiB，`acceptance_valid=false`，未提供优化版本，未进行16 GiB、正式重复轮次或30%对照。配置peak请求总和40.5 GiB是需求量而非同时驻留量。文件VMA clean页驻留下降与回收状态一致，但该诊断不能证明特定系统服务改善。

## 正式比较入口（仅在16 GiB及优化版本就绪后）

目前没有用户提供的优化版本，也没有完成16 GiB正式baseline/optimized轮次。下列仅为CLI格式示例，需将占位结果和audit路径替换成同设备、同16 GiB冻结配置产生的六份独立acceptance运行文件。每个audit与同位置result一一对应：

```powershell
$baseline = @('.\results\scene2_baseline_1\result.json', '.\results\scene2_baseline_2\result.json', '.\results\scene2_baseline_3\result.json')
$baselineAudits = @('.\results\scene2_baseline_1\scene2_ui_audit.json', '.\results\scene2_baseline_2\scene2_ui_audit.json', '.\results\scene2_baseline_3\scene2_ui_audit.json')
$optimized = @('.\results\scene2_optimized_1\result.json', '.\results\scene2_optimized_2\result.json', '.\results\scene2_optimized_3\result.json')
$optimizedAudits = @('.\results\scene2_optimized_1\scene2_ui_audit.json', '.\results\scene2_optimized_2\scene2_ui_audit.json', '.\results\scene2_optimized_3\scene2_ui_audit.json')
$compareArgs = @('--config', '.\config\scenarios16.json', 'compare', '--baseline') + $baseline + @('--baseline-audit') + $baselineAudits + @('--optimized') + $optimized + @('--optimized-audit') + $optimizedAudits + @('--output', '.\results\scene2_comparison.json')
py -3 -X utf8 .\run.py @compareArgs
```

baseline与optimized各需3个独立有效轮次，正式compare按hash-bound audit中的`counts_after`汇总并记录audit哈希；原始result不会被重写。优化版某类事件为0时仍可有效，但采集必须完整。目标为三类合计均值下降至少30%；本包不包含优化版，不宣称任何正式目标达成。

## 交付包完整性检查

包根`PACKAGE_MANIFEST.json`列出当前交付文件SHA256。加入场景3说明与结果后，新清单替代了初版；初版原样保存在`PACKAGE_MANIFEST_20261006_pre_scene3.json`作历史参考。完成解压后可在包根只读验证当前清单列出的文件：

```powershell
py -3 -X utf8 .\tools\package_manifest.py --verify
```

验证通过表示文件与随包manifest一致；不要用`--write`覆盖基准清单。独立的`preflight --offline`用于核验程序引用的资源闭包，manifest验证用于核验实际包文件哈希，两项都成功后再连设备。

## 证据目录与常见状态

- `evidence/scene1_full48_pressure12_154410/`：场景1完整原始预验证证据及报告。
- `evidence/scene2_qualification_180114/`：场景2资格轮raw、UI audit和全部必要证据。
- `evidence/scene2_calibration_163801/`：场景2逐应用、逐峰值档校准证据。
- `results/`：新执行生成的结果，含`result.json`、`report.md`、`events.csv/json`、`case_results.json`、journals、trace、kernel markers和UI截图。
- `config/`：两场景已验证32 GiB配置、16 GiB模板与预验证冻结文件。

`execution_complete`只说明动作完成；`measurement_complete`表示该轮必要证据完整；`qualification_eligible`是资格门槛；`acceptance_valid`和compare结果才决定正式验收。遇到HDC超时、缺失identity、缺少case窗口marker或trace窗口不完整时应保留原证据并判该轮不完整，不把未知计为0。停止时仅清理本项目自有测试应用与本轮资源。不要删除真实应用数据、改系统回收策略或清理其他进程。

