# 场景 1 独立复现包

本包只提供场景 1 的配置、命令入口和运行资产；采集器底层复用共享模块。本包不提供场景 2–4 的入口、配置、HAP 或数据，也不含旧设备原始结果或签名私钥。包含场景 1 所需的 8 个测试负载 HAP、压力程序和 HDC/运行库。请在 Windows PowerShell 中从解压后的包根目录操作；命令示例默认当前目录就是含 `run.py` 的目录。

## 先了解本包能验证什么

场景 1 会启动 8 个测试负载应用，每个应用触碰配置的内存量，然后按固定顺序重访腾讯会议和浏览器。当前 32GiB 配置为每个测试应用 6144MiB，共 48GiB（匿名页 12GiB、文件后备页 36GiB）；随后另建 12288MiB 压力负载。额外压力不计入 48GiB，也不能代替“8 个应用同时保持已分配并触碰的背景负载”。

针对 16GiB 或其他容量，必须先按真实设备容量生成配置并做低风险预检、安装和校准。生成器只给出容量预算与压力起始值，不证明该设备已能承受该负载。16GiB 示例配置是候选起点，未在真实 16GiB 设备上验证。不要仅修改名义 DRAM 字段来冒充设备容量；实际 MemTotal、分配结果和系统限制均须与目标设备相符。

系统 vmstat 的匿名/文件页缺页、refault 与 major/minor 计数属于系统范围；腾讯会议和浏览器的进程缺页计数是另一项、另一窗口的指标，不能把两者混加。当前采集未记录每应用 `memory.current`，也未完整覆盖短命子进程；已报告的应用进程端点统计不能解释为完整进程家族流量。即使单轮动作完整，也不等于正式全家族验收。

## 环境要求

- Windows 10/11、PowerShell、Python 3（仅使用 Python 标准库；不需要 pip 安装第三方包）。建议先用 `py -3` 启动器；若没有它，可将命令中的 `py -3` 换成 `python`。
- HarmonyOS PC 工程设备、可用 USB/调试连接，且设备需提供工程 root/HDC 权限以访问私有 journal、`/proc`、dmesg 与 trace 控制；HDC 与 `libusb_shared.dll` 随包提供。无需手动修改系统 PATH。包内 native HAP 与压力程序为 HarmonyOS ARM64；其他设备架构需用对应 SDK 重建。
- 设备要预先安装并可启动这六个真实应用。预检使用包内记录的 bundle、模块和 Ability：

  | 应用 | Bundle | Module / Ability |
  |---|---|---|
  | 腾讯会议 | `com.tencent.meeting.app` | `application` / `NXHostUIAbility` |
  | 浏览器 | `com.huawei.hmos.browser` | `entry` / `MainAbility` |
  | 海泰浏览器 | `com.haitai.htbrowser` | `pc_entry` / `EntryAbility` |
  | 文件管理器 | `com.huawei.hmos.filemanager` | `pc` / `MainAbility` |
  | WPS | `cn.wps.office.hap` | `prome` / `PromeAbility` |
  | 应用市场 | `com.huawei.hmsapp.appgallery` | `entry` / `MainAbility` |
- 包内 8 个测试 HAP 使用当前构建时的 OpenHarmony SDK debug 签名，profile 可能绑定原设备 UDID；它们可能无法安装到另一台设备。若安装被签名或 UDID 拒绝，请按“换设备时重建与签名”在目标调试设备上重新生成签名；不要复制或索取原构建机私钥。组织要求的 vendor 授权 profile 需走组织批准的构建流程。

## 解压、检查 HDC 与设备

解压 ZIP 后在 PowerShell 进入目录，例如：

```powershell
Set-Location 'D:\你的目录\场景1独立复现包_20261007'
```

先做不连接设备的包资源检查，再查看 HDC 能否看到目标设备：

```powershell
py -3 -X utf8 .\run.py --config .\config\scene1_full48_pressure12.json preflight --offline
.\tools\hdc.exe list targets
```

`--offline` 只检查包内文件，不检查设备、实际 MemTotal 或容量。`list targets` 应显示唯一目标设备。如果连接多台设备，应在后续命令显式传入目标序列号；不要依赖错误的默认设备。

如果设备是 32GiB，使用下列命令检查真实应用和当前安装状态。16GiB/其他容量设备应先按下一节生成对应配置，再将此命令中的配置路径换成生成文件。它会连接设备并写入包内 `validation`：

```powershell
py -3 -X utf8 .\run.py --config .\config\scene1_full48_pressure12.json --device '设备序列号' preflight --output .\validation\preflight.json
```

先按下一节安装 8 个测试 HAP，再运行设备预检。检查报告中的六个真实应用与测试 HAP 已安装、MemTotal 与配置相符、工程权限与 swap 可用。设备内核可能缺少部分 vmstat 匿名/文件 counter；报告会将缺字段标为缺失/null，不把它当作 0。只有预检满足运行条件后才继续。

## 生成容量配置

例如为真实 16GiB 设备生成每应用 3072MiB（8 个共 24GiB）的候选配置：

```powershell
py -3 -X utf8 .\tools\make_scene1_config.py --dram-gib 16 --output .\config\scene1_generated_16GiB_pressure6GiB.json --device '设备序列号'
```

生成器按每应用至少占名义 DRAM 的 150% / 8 计算背景负载，并按 64MiB 向上取整；背景默认按 75% 文件后备、25% 匿名页构成。压力负载单独配置，不计入该 150% 条件。16GiB 及其他非32GiB配置默认的6144MiB压力值仅为待观察的起始值，并非已校准安全值；运行器不会因事件不足自动提高压力。若使用本包明确标注的 32GiB 已测配置，应保留每应用 6144MiB 与 12288MiB 压力设置。

生成时可用 `--device` 把目标序列号写入配置；若省略，配置设备为空，运行命令须用根级 `--device` 指定唯一目标。若需覆盖 HDC 可执行文件路径，可在运行命令根级使用 `--hdc 'C:\本机工具\hdc.exe'`；通常直接使用包内 HDC。`--config`、`--device`、`--hdc` 都是根级选项，必须放在子命令 `preflight` / `run` 之前。运行工具不含 `--scene` 参数，因为它是场景 1 专用入口。

## 安装测试 HAP 并复核应用

自动选择唯一在线设备安装：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_scene1_apps.ps1
```

若有多台在线设备，明确指定序列号：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_scene1_apps.ps1 -Device '设备序列号'
```

安装后再次运行设备预检。真实应用应已由用户/设备镜像提供；此安装脚本只处理包内 8 个测试 HAP。

## 运行一轮 baseline

先确认所选配置中的设备序列号、真实 DRAM、每应用负载和压力值，然后运行一轮开发配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\scenarios\scene1\run.ps1 -Config .\config\scene1_full48_pressure12.json -Device '设备序列号' -Phase baseline -Rounds 1
```

若使用刚生成的 16GiB 候选配置，将路径替换为 `.\config\scene1_generated_16GiB_pressure6GiB.json`。命令完成后，在 `results\scene1_baseline_<时间>_s1_<编号>\` 查看 `result.json`、报告、事件和采样记录。具体文件以本轮结果目录为准。保留原结果，不要手工改写统计字段。

要停止正在运行的进程，在启动该轮的终端按 `Ctrl+C`，等待程序执行收尾。采集器只清理本轮拥有的测试应用和压力进程；结束后查看结果中的清理状态。不要用全局清理命令杀真实应用或改系统内存策略。

## baseline 与 optimized 对照

先由产品/系统侧提供优化版本，并按相同设备、相同生成配置、相同应用版本和相同动作分别运行 baseline 与 optimized。先做一轮确认流程，再执行目标各 10 轮：本包 CLI 按 `--rounds 10` 固定重复，不提供独立 `acceptance` profile。

```powershell
powershell -ExecutionPolicy Bypass -File .\scenarios\scene1\run.ps1 -Config .\config\scene1_full48_pressure12.json -Device '设备序列号' -Phase baseline -Rounds 10
py -3 -X utf8 .\run.py --config .\config\scene1_full48_pressure12.json --device '设备序列号' run --phase optimized --rounds 10
```

两个阶段必须使用同一配置及负载定义。不要把不同设备、不同 DRAM 配置或不同动作的结果拼成一组比较。`--rounds 10` 表示最多执行十轮；若任一轮结果无效，程序会停止后续轮次。十轮目标不是达标结论；完整性、设备容量门槛、证据覆盖和优化版本本身仍须审核。若设备不是经校准的 32GiB 场景，先生成并验证其专属配置，之后两阶段都使用该文件。

## 换设备时重建与签名

目标设备不能安装包内签名 HAP 时，使用你本机安装的 DevEco Studio/OpenHarmony SDK 为目标调试设备重新构建。不要把私钥、密码或个人证书复制到交付 ZIP。可通过以下命令查看实际参数：

```powershell
py -3 -X utf8 .\tools\build_apps.py --help
```

例如用本机 SDK 为目标调试设备生成 debug 签名 HAP：`py -3 -X utf8 .\tools\build_apps.py --studio 'C:\DevEco Studio' --count 8 --target '设备序列号' --source .\apps\workload\workload --bundle-prefix io.memscene.workload.a --output-dir .\bin\apps --signing-dir .\build\signing`。`--signing-dir` 是本机生成的 debug 签名材料输出目录，不是导入证书的参数。若组织要求自有 vendor 授权 profile，当前脚本不支持导入该证书，请改用组织批准的构建流程；新目标设备的签名和安装权限仍由设备/厂商策略决定。构建完成后核对 8 个 HAP 的 bundle 名称与配置一致，再用包内安装脚本指定目标序列号安装。SDK 路径因机器不同而异，本包不要求使用原构建机的绝对路径。

压力程序如需从源码重建，也需要本机 DevEco SDK/Native 工具链；正常运行使用包内 `bin\pressure`，不需要安装 SDK。

## 结果解释与边界

“所有 8 个测试应用都分配并触碰了目标量”“额外压力进程已退出”“重访窗口有进程缺页计数”“系统范围出现磁盘换出”是不同证据，分别查看结果中的负载、pressure、reuse 与 system counter 字段。应用 Eswap 为零不能单独证明系统没有磁盘换出；同样，系统换出不能证明指定真实应用已被换出。

只有证据完整且计数窗口匹配时才使用相应指标。系统 fault/refault 是系统范围快照差值；应用 fault 是重访窗口中已识别进程集合的端点差。当前包不采集每应用 `memory.current`，短命子进程的覆盖也尚未证实。跨设备运行应把这些作为已知限制，不能将缺失值当作零或宣称正式验收通过。










