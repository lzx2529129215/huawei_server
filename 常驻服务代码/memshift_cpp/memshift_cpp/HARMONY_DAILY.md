# Harmony 日常服务首版

## 当前设备版本：049（2026-10-12 22时）

样机已串行更新并留驻 SHA256 `0492305ac3f80c3cab1330d67e5c5dd8c65a418b42963db7ca75291975b47e7e` 的主工程构建；设备路径为 `/data/local/tmp/memshift_daily_lead_049_20261012_2200/memshiftd`，配置 SHA256 `816bc39ce0f639ee1e7420b7ddadd5a89c73c3cc4cd5fb59dc3287a7a63e0708`，配置文件在同目录。该配置相对 e9c 只改变独占日志目录，仍为 `oracle_mode=false`、`daily_async=true`、`daily_disk_mode=off`、四办公应用/keep512/小额度；不会自行读取未来计划或打开测试用的大额度和磁盘回收。共享控制文件字节未变。实际 PID 会变化，操作前须重新发现。

90秒被动留驻门禁通过：候选是唯一实例，账本只有表头、没有动作，RSS 约 6.46→6.55 MiB；同一 boot ID、Enforcing、进程可执行文件/完整 argv/二进制和配置哈希已在切换后独立核对。回退来源是下述 e9c 程序和配置，仍原样保留。准备、可逆切换脚本和原始核查证据在项目根 `results/daily_lead_rollout_20261012_2200/`。此空闲观察不是办公OOM收益证明。

三对冻结场景4内部对照使用同一049二进制，但实验配置另行启用了设备登记的100%已知未来事件、13个自有供体、较大额度及 `force_swapout`。观察合计 A 44→B 0，见项目根 `results/scene4_measurement_closure_20261012/three_pair_combined_report.md`。这个结果不能外推到当前办公配置；原始运行器的追踪采集失败及原生弹窗独立核查边界见报告。

## 前一代 e9c 日常版本

样机于2026-10-12曾串行更新并留驻 **e9c** 日常版本；该轮90.27秒被动观察为0动作，不是办公OOM收益证明。它不读取未来T或host供体身份计划，`daily_owned_os_snapshot`仍默认false，`daily_disk_mode=off`，四办公应用/keep512/小额度与原版相同。

- e9c 二进制 SHA256：`e9c67d6ff5f3e5b2e46a9df5073466dd72b289a85b33fa309ff6c2879c958139`。
- e9c 部署INI SHA256：`26ccfaa4b2790c2b623c6adf4fcf55f1e9526a0982bbd09ab952c08dbb9f7a61`。
- e9c 设备程序：`/data/local/tmp/memshift_daily_office_e9c_20261012_0408/memshiftd`。
- e9c 设备配置：同目录 `resident_daily_office_candidate.ini`；日志同目录 `logs/harmony-v1-ratio/`。
- 共享原 `resident_control.conf` / `resident.lock` 路径保持下述full_chain入口，control字节未改。原2e4/aa75设备文件完整保留作回退。
- 源码合入/独立构建：项目根 `results/daily_owned_qualification_20261012_0253/delivery_main_20261012_040432/`；已复现审核r3位级e9c，12目标编译链接通过。
- 实际串行更新、90秒自身CPU/RSS原始读数、0动作账本、独立身份/控制核验、回退步骤：项目根 `results/daily_office_e9c_rollout_20261012_0408/`。核验时PID38016/start10021778；后续操作必须fresh发现，不得盲用此历史PID。

仓库 `config/harmony_v1_daily_device.ini` 仍保持aa75原字节；e9c 部署26cc只改变独占log_dir，没有其它策略差异。e9c/26cc现在是049的回退来源，不能直接复用期待旧2e4或cae原版的切换脚本。无开机自启，无办公/自有应用启动，无压力、系统白名单/评分/swap/SELinux变更。

e9c 的90.27秒同代际观测：CPU累计112ticks（1.24072ticks/秒），CLK_TCK未匹配验证，CPU百分比未知；VmRSS6636→6652KiB，VmHWM6660→6704KiB，Threads2。只代表有限空闲区间，不外推压力开销。下面2e4/aa75块是更早历史/回退来源，不能作为当前运行版本。

## 前一代2e4/aa75历史与回退设备路径

- 已验证二进制 SHA256：`2e4cd6a392b8c8241e61457b8f5438953d9722654d151534c64ee28fc3ad19f5`。
- 当前配置 SHA256：`aa75f221efa62c4ef1e5a81ad987993279d13974c1847d7eac1f6fc84638e102`。
- 构建与源码快照：项目根 `results/daily_scene4_nonoracle_plan_20261011_2130/revision_known_no_write_20261011_2217/`。
- 留驻与回退证据：项目根 `results/daily_office_rollout_20261011_2255/`。
- 设备程序：`/data/local/tmp/memshift_daily_office_20261011_2255/memshiftd`。
- 设备配置：同目录 `resident_daily_office_candidate.ini`。
- 共享控制：`/data/local/tmp/memshift_validation_20261007/full_chain/resident_control.conf`。
- 共享锁：同目录 `resident.lock`；当前日志独占于新服务目录的 `logs/harmony-v1-ratio/`。

仓库 `config/harmony_v1_daily_device.ini` 是前一代 aa75 配置的逐字节副本，**只绑定这台样机的四个包/UID/leaf 路径**，不是通用设备配置。说明只写在本文件，保持 INI 原字节。PID 会随串行恢复变化，操作前必须重新查代际，不能盲用历史 PID。

## 当前执行链与保护

实际系统采样 → EWMA/当前物理 Free 与 reserve 缺口 → 四应用白名单/keep/后台资格 → 写前预扣共享及每应用分钟额度 → 单 worker → 写前 fresh 身份、control、缺口、swap 与保留量复核 → 一次 `memory.force_shrink_anon` 比例写 → 反馈及 action 账本。

普通办公资格仍要求 group `memory.app_score>0`、每成员 `oom_score_adj>0`、成员集合/start_ticks/cgroup 一致；这是原评分保护，不是 AA/WMS 前台证明。真实写前保留量至少 512MiB，最大比例 20。一个 action 有 reservation/completion 两行，不能计为两次写入。失败/未知保持预扣，接受但反馈未知会停止后续 admission；不自动补写或切第二 backend。单 worker 退出 join 可能等待阻塞的内核写，不能声称可强制取消。

当前小额度：batch16MiB、全局64/每应用32MiB每分钟、keep512MiB、readyFree256MiB/reserve1024MiB、horizon5秒，详细 smaps 每30秒而 status/stat 仍按1秒采样。`daily_disk_mode=off`、whole-work=0。Harmony PSI 语义未验证，只作诊断；`require_psi=true` 在此后端不支持。

## 观察与 apply

不传 `--apply` 是观察模式；`--apply` 才允许在全部资格与预算门禁通过后提交回收。`daily_async=true` 不保证会有动作：空闲无缺口、组不存在、前台/评分保护或保留量不足时应保持不写。控制文件继续使用原入口，暂停或保护立即作为后续动作门禁；本说明不提供自动启动或部署命令。

前一代只读健康证据在项目根 `results/daily_office_health_20261012_0120/`：约78秒，原代际/哈希/控制/Enforcing不变，16测试应用无残留，真实0动作。WPS/appgallery组不存在及浏览器保护属于正常排除，不需启动应用使日志变绿。

## 构建与测试边界

`tools/build_harmony.ps1` 与 CMake 的 daemon/test_harmony 链接源一致，包含 `device_oracle_evidence.cpp`；日常 executor、disk fake 及只读 provider 工具也有构建目标。可用 `-OutputDir <绝对新目录>` 输出独立产物；省略时保持原 `build-ohos-aarch64`，相对路径按工程根解析。

这是 OHOS AArch64 交叉构建，不能直接把 ELF 在 Windows 主机运行或把编译称测试通过。`test_daily_action_executor` / `test_daily_disk_backend` 为 fake-only fixture；`device_daily_evidence_probe --fake` 仅纯解析，省略 `--fake` 会调用真实只读 provider。设备执行应另行审核授权，本构建脚本不执行这些程序。新构建若哈希不同，不能称与冻结 2e4 位级相同，也不会自动替换当前程序。

## 范围与串行替换原则

三对非 oracle 自有应用实验的全部确认 OOM 从30降至21（30%），有一对持平。实验仍含18GiB水位、40GiB额度、13自有 singleton 磁盘白名单和固定负载；不是普通办公收益、逐轮稳定改善或完整异常指标验收。普通办公磁盘接口仍关闭；`force_swapout` 的整组命令没有已证明的物理字节硬上限，不能直接推广。

这里没有配置开机自启。任何后续串行替换都须 fresh 核验 boot/Enforcing、唯一旧实例 PID+start_ticks/exe/完整argv及文件哈希，保存原配置/控制及回退来源；精确 TERM 并确认旧实例退出后才启动一例新服务，沿用共享锁/控制。身份、退出或恢复未知时不得再启第二例。退出 join 阻塞不得用强杀冒充已恢复。前一代原 binary/config/control 字节保留作回退；本节记录当时的文档/构建阶段，实际后续切换见顶部e9c运行证据。


## 2026-10-12：自有对象资格与主动磁盘开关分离

已将审核后的 r3 五文件增量接入主工程，未提交 Git；接入当时尚未部署，随后已按顶部回执串行更新至e9c/26cc。新选项 `daily_owned_os_snapshot` 默认 false；旧 `daily_disk_mode=owned_apply` 的资格行为兼容保留。显式 opt-in 只对非 oracle、非 probe、daily_async 的精确 a1–a13 singleton 自有 leaf 生效，匿名动作仍需 fresh AA/WMS/proc 证明。alias 与 bundle 的 control 保护独立于成员数/资格成功；多成员或身份不明也不丢失 bundle 保护。普通办公继续原 score/adj 资格与至少512MiB保留量。

磁盘排队与 worker 均须显式 `daily_disk_mode=owned_apply`；off 在节点 open 前拒绝磁盘后端，即使 whole-work 非零也不生效。资格 opt-in 本身不打开磁盘。两个阶段继续共享预扣预算、单 worker、比例/保留量保护和 unknown 全局 halt；不读取未来 T 或 host 供体身份计划。此接入不降低 keep、不改变额度、采样或调度节奏。

r3 的两个 fake-only ELF 已在先前独立回执中 exit0/PASS，当前构建只复现产物，不重复设备测试。有效 mixed 探索例全部确认 OOM=5（5测试/0其他），主窗/full-active覆盖完整；anon-only 例16应用未建立 daily_ready、无T/无峰值，OOM比较不可用，不能写成0或归因于关盘。一次1MiB启动诊断仅 aa 接受，无进程/日志；既有同trace日志出现 SampleManager enabled / UIAbility not in white list，属于具体资格排除线索，尚无唯一根因或崩溃结论，未修改该系统策略。

mixed/anon-only 的16GiB固定水位、40GiB全局/8GiB每应用额度、owned磁盘白名单与冻结负载仍是实验条件，不是日常默认。13供体是1536MiB匿名、file0；只有三个目标的日常负载含1280MiB匿名+256MiB文件，目标峰值8192MiB。仓库 aa75 办公模板仍 readyFree256MiB、batch16MiB、64/32MiB分钟额度、keep512MiB、diskoff，原字节未改变，普通办公收益未证明。

当前日常匿名接口按整数百分比写入，KeepSlack=128KiB：16MiB batch 的 safeCap 仅15.875MiB，2/4GiB匿名分母的1%约20.48/40.96MiB，会得到ratio0而跳过；1GiB分母可容纳1%约10.24MiB，512MiB分母则被keep512保护。故当前小批次不能覆盖任意大组，不以此自动扩大额度。
