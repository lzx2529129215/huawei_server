# MemShift 0.1 — 用户态内存错峰回收 C++ 基础框架

## 结论与交付边界

本项目提供能够编译、常驻采样、计算回收预算并调用 `memory.reclaim` 的 C++17 参考实现。第一版采用短时 EWMA 趋势预测和后台应用的保守回收启发式，不需要训练数据或第三方机器学习库。后续可替换预测器和预算函数。

**10%是希望在目标用例中验证的异常事件下降目标，不是本项目已测出的收益，也不是代码能够保证的下限。** 当前已完成编译、策略单元测试及模拟文件系统联调；未在合同规定的16 GB目标机器和真实应用组合上完成性能验证。模拟测试中的算术数据不是性能结果。

本项目只处理“供给侧错峰”：把部分回收工作提前做。它不延迟应用启动、不串行化用户请求、不杀应用，不修改 `memory.high`、`memory.max`、全局 swappiness 或 swap 配置，也不清空全局缓存。

## 1. 文件与修改入口

| 文件 | 用途 |
|---|---|
| `src/memshiftd.cpp` | 常驻主循环、调度、单工作线程回收、CSV日志、异常退避 |
| `src/core.hpp` | 数据结构、`Predictor`接口、`EWMAPredictor`、`donorBudget()`及限速 |
| `src/io.hpp` | cgroup/proc采样、配置和运行时控制文件解析 |
| `src/memshiftctl.cpp` | C++运行时通知工具，原子更新前台保护、预留提示、暂停状态 |
| `config/example.ini` | 可调整参数和应用白名单，路径必须修改 |
| `config/control.example.conf` | 运行时控制文件示例 |
| `deploy/memshiftd.service` | 可选systemd常驻服务示例 |
| `tools/compare_runs.cpp` | 按相同用例序列配对比较异常事件计数 |
| `tests/test_policy.cpp` | 50项策略与解析检查 |
| `tests/smoke.py` | 模拟采样/通知/保护/统计工具联调，仅开发测试使用Python |
| `examples/measurements_template.csv` | 真实测试数据的空模板，不包含虚构收益 |

运行组件全部是C++。Python仅用于可选的开发联调测试；服务、通知工具和统计工具均不依赖Python。

## 2. 平台要求和不支持的情况

本版按Linux宿主机的标准cgroup v2布局编写：挂载点为 `/sys/fs/cgroup`，采样来自 `/proc`，每个被管理应用对应一个独立的 `domain` cgroup，目标组有 `memory.reclaim` 接口。要求C++17标准库、POSIX文件接口和线程支持。建议使用GCC 9及以上或具备相应C++17标准库的Clang。

是否支持目标鸿蒙PC，必须现场核实内核接口、权限、路径和工具链。不能据此假定所有鸿蒙版本都支持。普通Windows环境不能直接执行这套Linux接口。没有systemd的目标平台可由其自身服务管理器启动同一可执行程序，无需改成内核模块。

该版本针对宿主机物理内存不足，不支持用宿主机 `MemAvailable` 推断容器内部或某个 `memory.max` 配额的剩余额度。检测到挂载根的 `memory.max` 时，实际回收模式会拒绝启动。对cgroup子树配额、容器、NUMA局部不足和碎片问题，需要增加相应的作用域模型，不能直接套用当前预测。

应用路径只能是非根相对路径，禁止 `..`、重叠祖孙目标和符号链接路径；不允许把整个 `user.slice` / `system.slice` 当成应用来管理。守护进程不能放入回收目标组内。`--apply`执行时应使用root或具备所需文件访问权限的服务身份。配置、日志及控制文件所在目录应由管理员控制，不能放在其他用户可随意篡改的目录中。

**本程序不创建cgroup、不搬迁应用、不配置swap。** 应用应在大量分配内存之前就处于正确组内。Linux内存记账不会随进程后续迁移自动整体移动，因而不能先运行很久再仅移动主PID，并认为原有全部内存已经归入新组。[K1]

## 3. 第一版的策略

### 3.1 全局缺口和提前量

每秒采集 `MemAvailable`、`MemFree`、swap及内存/I/O PSI。`EWMAPredictor`对 `MemAvailable` 的有符号下降速度进行时间平滑，再预测默认5 s后的可用量。预测速度有上限，预测降幅不超过物理内存的1/4。这是经验外推，不是对未来用户操作的准确预测。

回收缺口取以下两项的较大值：

```text
available_gap = max(0, reserve + effective_launch_boost - predicted_available)
ready_gap     = max(0, ready_free - MemFree)
deficit       = max(available_gap, ready_gap)
```

`MemAvailable`已经包含部分可回收缓存，不能再把相同文件缓存完整加为“新增内存”。这里将其与真正空闲的 `MemFree` 分开控制，不进行重复相加。[K2]

默认希望保有1024 MiB的可用内存估计和256 MiB的即时空闲页池。任一缺口达到最小批次时启动一轮预回收；达到恢复条件后退出该轮。对于较晚才显现的启动事件，可以使用有有效期的额外预留提示；该提示只是暂时抬高目标余量，不是精确的未兑现分配需求模型。

### 3.2 应用候选预算

只允许显式设置 `reclaim=true` 的后台组参与。应用配置中的 `reclaim=false` 是永久排除，运行时前台名单是额外排除，不会把原本禁止的组重新打开。

候选量来自两次相邻采样中较小的 `inactive_file`、`inactive_anon`。文件候选量保守减去脏页和写回量；匿名候选量受全局swap余量及目标和祖先的 `memory.swap.max` 剩余额度约束，再乘匿名权重。候选预算乘以 `alpha` 和风险自适应系数，最后受到单次上限、全局缺口、分钟额度、单轮额度和保留量限制。

**inactive列表不是未来WSS，也不是“以后肯定不用”的页面集合。** 它只能帮助判断一个后台组是否值得尝试少量回收。`memory.reclaim`由内核选择页面，本程序没有指定具体虚拟地址。[K1]

配置的 `min_keep_mb`、`keep_ratio`、已读取的 `memory.min/low` 只用于限制本程序的请求预算，不能承诺实际驻留量绝对不越过该值。接口可能多回收或少回收，父子组保护也有相应作用域。[K1]

### 3.3 限速、保护和退避

默认新发现/重新出现的应用保护30 s；正在快速增长、出现较多重新访问或major fault、应用PSI较高的组不参与回收。控制文件缺失、损坏或含未知应用名时暂停额外回收，避免依照错误前台状态操作。

全局内存full PSI或I/O some PSI达到阈值时暂停主动回收，把严重压力处理留给原系统。本版不在高压力时无限增强回收。PSI使用内核给出的10 s平均值和相邻采样的total增量，以两者较大值做保护。[K3]

全局最多一个回收任务在执行，不堆积任务队列。每次工作线程执行前重新读取目标、控制状态和预算。回收结束后才重新计算冷却时间。`EAGAIN`、异常耗时、低回收计数增量等情况会降低强度并退避。`EINVAL`、权限错误和只读文件系统等错误会禁用该目标，直到重启或组重建。

回收后的计数变化可能受到同时发生的应用行为和内核回收影响；因此这些反馈只是保守风险信号，不代表严格的因果归因。

### 3.4 明确未实现的功能

未实现机器学习训练、逐页/区域访问监控、真实WSS标签构建、应用窗口自动识别、应用启动失败/弹窗打点、启动需求串行化、全系统预取、磁盘耐久度预算及生产级在线模型更新。

`memory.reclaim`写入可能阻塞，放到工作线程只是避免它直接阻塞采样主循环，不代表该系统调用具有可强制取消的超时。出现长时间写回等待时，关闭服务也可能等待内核返回；日志会提示，不保证停止延迟。

## 4. 编译

在目标Linux机器解压项目后：

```bash
cd memshift_cpp
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
ctest --test-dir build --output-on-failure
```

不使用CMake也可以编译三个运行组件：

```bash
mkdir -p build
g++ -std=c++17 -O2 -Wall -Wextra -pthread src/memshiftd.cpp -o build/memshiftd
g++ -std=c++17 -O2 -Wall -Wextra src/memshiftctl.cpp -o build/memshiftctl
g++ -std=c++17 -O2 -Wall -Wextra tools/compare_runs.cpp -o build/compare_runs
```

编译产物为 `memshiftd`、`memshiftctl`、`compare_runs`。项目包只分发源码和说明，不依赖本次编译机器上的二进制兼容性。

## 5. 从检查到实际运行

### 5.1 先确认接口

以下均为读取，不触发回收：

```bash
stat -fc %T /sys/fs/cgroup
cat /sys/fs/cgroup/cgroup.controllers
cat /proc/meminfo | grep -E 'MemTotal|MemAvailable|MemFree|SwapTotal|SwapFree'
cat /proc/pressure/memory
cat /proc/pressure/io
```

文件系统类型应为 `cgroup2fs`。还应确认目标应用组下有 `memory.current`、`memory.stat`、`memory.min`、`memory.low`、`memory.events`、`cgroup.events` 和 `memory.reclaim`。`memory.reclaim`是写接口，**不要使用cat读取它来判断支持情况**，可以使用 `ls -l` 检查文件，再使用程序的 `--check` 检查可写性。[K1]

`require_psi=true`是默认保护要求。目标平台缺少全局PSI时应优先核实内核配置；确实需要做兼容实验可设置 `require_psi=false`，但这会失去缺失部分的全局压力保护，不能认为安全性和原配置相同。

`swappiness=-1`会省略扩展字段，以免接口回移版本不支持。设置其他值后若出现 `EINVAL`，不要盲目重试或自动改用更激进语义，应恢复为 `-1`并核实接口。`--check`只检查文件和读取权限等，不通过写入验证扩展参数。

### 5.2 查找实际应用组并修改配置

```bash
# 将12345替换为实际应用PID。
cat /proc/12345/cgroup
```

例如返回：

```text
0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-example.scope
```

则配置使用去掉开头斜杠的路径：

```ini
[app background_a]
path=user.slice/user-1000.slice/user@1000.service/app.slice/app-example.scope
reclaim=true
min_keep_mb=512
```

配置可以包含暂时不存在的应用组，后续出现时会重新采样并进入启动保护。`--check`会对缺失组返回非零，常驻模式则跳过并定期重试。程序不会自动发现不同的新路径，应用重启路径发生变化时需更新配置并重启服务。

先复制配置：

```bash
cp config/example.ini config/local.ini
mkdir -p runtime
./build/memshiftctl --file ./runtime/control.conf --init --protect editor
```

随后编辑 `config/local.ini` 中所有 `[app ...]` 路径。`editor`等名字是人为设定的逻辑名，不是根据进程名称推断的。如删除或重命名示例应用，要同步修改控制文件中的保护名单，否则程序会安全暂停。

### 5.3 检查与只观察

```bash
sudo ./build/memshiftd --config config/local.ini --check --apply
sudo ./build/memshiftd --config config/local.ini --observe
```

第一条即使带有 `--apply`，只要有 `--check`就不会执行回收写入。第二条持续计算和记日志，但不会写 `memory.reclaim`。观察模式的 `observe_only`记录、`would_reclaim`决策均不是实际回收结果。

从项目根目录启动，确保示例中的相对日志、锁和控制文件路径正确。后台应用至少经过30 s启动保护后，才可能成为候选。保护期是从本程序首次观察到该组开始计算的，并非读取应用历史运行时长。

看到的执行原因应与实际场景一致，特别检查是否误把前台/正在启动的应用列为回收对象。使用Ctrl+C停止观察，再进入实际模式。默认共享锁会阻止同一配置下重复运行两个实例。

### 5.4 开启实际回收

```bash
sudo ./build/memshiftd --config config/local.ini --apply
```

该进程以普通用户态进程方式持续运行。终端方式可Ctrl+C退出，也可由系统服务管理器托管。只有显式的 `--apply`允许写回收接口；同时指定 `--observe`和 `--apply`会报错。

### 5.5 前台保护、启动预留提示与暂停

另开终端，在同一项目目录执行：

```bash
# 把background_a切入前台，同时保留editor的保护。
sudo ./build/memshiftctl --file ./runtime/control.conf --protect background_a,editor

# 保持其他字段不变，临时多预留1024 MiB，30 s后该提示自动失效。
sudo ./build/memshiftctl --file ./runtime/control.conf --boost-mb 1024 --ttl 30

# 暂停后续主动回收；已经进入内核的单次请求不能在此保证撤销。
sudo ./build/memshiftctl --file ./runtime/control.conf --pause true

# 恢复调度。
sudo ./build/memshiftctl --file ./runtime/control.conf --pause false
```

`memshiftctl`以加锁、临时文件写入、原子替换方式更新控制文件；未指定的字段保留。`--protect`替换整个运行时保护名单，并不是追加一个名字。使用 `--protect none`会清空运行时名单，但不会改变配置中 `reclaim=false`的永久保护。

程序不会自动识别当前窗口。动态前台保护和启动提示应由真实的窗口/应用生命周期事件调用上述工具，或使用同样的文件协议接入。提示不是必需条件；没有提示时仍按趋势和基础预留目标调度。

**不要为了让优化版好看而只在优化组额外等待30 s、提前获知未来测试脚本，或延迟应用启动。** 需要考察提前提示时，提示应来自两版均可获得的真实事件，并保持相同用户操作序列和评价起点。只在分配已经开始时收到通知，不能视为已经获得充分的预回收时间。

## 6. 默认参数与第一轮调整

| 参数 | 默认 | 含义 |
|---|---:|---|
| `reserve_mb` | 1024 MiB | 目标可用内存估计 |
| `ready_free_mb` | 256 MiB | 即时空闲页目标，不与可回收缓存重复相加 |
| `horizon_sec` | 5 s | 全局可用内存趋势预测时长，可改至30/60，但误差风险更大 |
| `alpha` | 0.20 | 候选量的初始预算系数，不是直接回收20%的全部内存 |
| `batch_mb` | 64 MiB | 单个请求上限，不是实际回收量保证 |
| `max_per_minute_mb` | 1024 MiB | 全局滚动60 s请求额度 |
| `app_per_minute_mb` | 256 MiB | 单应用滚动60 s请求额度 |
| `episode_fraction` | 0.20 | 单轮累计请求上限，基于该轮开始尝试时的应用用量 |
| `keep_ratio` | 0.70 | 每次计算预算时保留当前用量的比例约束 |
| `startup_grace_sec` | 30 s | 新出现/重建组的保护时间 |
| `cooldown_sec` | 3 s | 同一应用成功/普通请求后的最小冷却时间 |
| `backoff_sec` | 30 s | 风险/异常反馈的退避时间 |
| `swappiness` | -1 | 省略扩展字段，使用内核现有策略 |

第一轮不建议同时修改很多参数。先看日志确认是否有“候选不足”“提示太晚”“执行速度不足”或“反复回访”问题。仅在已有充分后台候选、实际回收正常、且未出现体验退化的情况下，再分别试验 `reserve_mb=1536` 或 `app_per_minute_mb=512`、`alpha=0.25`。这些是待验证的调参选择，不是已测出的最优参数。

`allow_anon_hint=false`只影响预算中是否计入匿名候选，不会把内核执行器变成严格的“仅文件页回收器”。参数字段必须按内核实际语义理解，不能把启发式统计当作选页控制。[K1]

## 7. 日志如何判断

| 文件 | 重点 |
|---|---|
| `system.csv` | 可用/空闲内存、预测、PSI、缺口、预留提示、是否提交请求 |
| `apps.csv` | cgroup用量分项、缺页/重新访问计数、OOM查杀进程数、候选预算、跳过原因 |
| `reclaim.csv` | 请求量、实际提交量、write耗时、errno、前后计数/用量变化 |
| `events.csv` | 组重建、暂停、异常、工作线程耗时警告 |

默认每个CSV达到32 MiB后只保留当前文件和一个 `.1`轮转文件。正式实验应逐轮归档，避免覆盖需要保留的早期记录。日志大小阈值会继续生效，不会无限占用磁盘。

常见跳过原因包括 `protected`、`startup_grace`、`growing_app`、`refault_backoff`、`io_pressure`、`memory_full_pressure`、`budget_too_small`、`cooldown`、`worker_busy` 和 `control_invalid`。

`reclaim.csv`中：

- `planned_bytes`是调度层预算；`submitted_bytes`是实际用于写请求的目标容量。两者都不是成功释放的物理字节数。
- `status=ok`表示write接口接受该次请求，仍不等于系统净增加了同等物理容量；`partial_or_unmet`对应EAGAIN，可能已经发生部分回收，代码不立即重试。[K1]
- `current_change`是回收期间应用组用量的有符号变化；`pgsteal_delta`是同一时间段的回收计数变化，可能混入并发内核行为。`pgsteal_proactive_delta`仅在内核提供对应计数器时填写。
- `mem_available_change`、`mem_free_change`都是整机同时段观测值，不是严格的动作归因。使用zswap/zram时，压缩数据本身仍会占RAM；本版记录可获得的zswap量，但不完整核算每个zram设备的内存与元数据，不能把应用驻留下降等同于整机净释放。[K4][K5]

`apps.csv`的 `oom_kill`来自 `memory.events`，表示该组中的OOM查杀进程数量，不是整个合同的三类异常总数。一组OOM可能终止多个进程；启动失败和低内存弹窗也必须独立打点。[K1]

## 8. 怎样验证“10%提升”

### 8.1 先确认原问题确实是本版能够作用的类型

至少需要存在一定的后台可回收内存、足够提前量，以及实际可用的回收/换出通道。对于所有内存都在高峰期间立即使用、没有足够swap、仅受应用cgroup硬配额限制、回收比启动慢很多或基线已经几乎无异常的用例，不应假设本版一定有效。

第一步确认真实模式中出现有效请求及相关内存变化；第二步确认前台启动时延、I/O停顿和重新访问没有不可接受的退化；第三步才看异常次数下降。

### 8.2 固定A/B流程

冻结机器、系统镜像、swap/压缩配置、应用版本、操作序列、计时边界和异常计数口径。优化前后采用相同用例序列和数量，重复运行并逐轮归档。原系统作为基线；可以额外增加同等日志采样但不回收的观察组，以分析监控开销。

不要把启动“变成不失败但无限等待”视为成功。预先固定启动成功判据与超时阈值，并同时记录启动时延。若基线异常数为0，无法计算具有意义的异常下降百分比。

合同第二场景的最终指标是启动失败、低内存弹窗、OOM查杀的总次数下降30%；优化前累计异常事件达到规定次数后固定用例，优化后执行相同用例序列及数量，前后重复3次取平均。这里10%仅是用户当前提出的基础版阶段性实验目标，不替代合同验收要求。合同依据为用户提供协议第4页的“内存错峰调度”行。

### 8.3 记录并比较真实测试数据

将测试端数据填入两个CSV，字段如下：

```text
run_id,sequence,case_id,launch_fail,lowmem_popup,oom_events,launch_ms
```

每一行对应某轮中的一个固定用例。`run_id`在两版中均使用对应轮次，如1、2、3；`sequence`是轮内操作序号；`case_id`为相同应用组合/步骤的稳定标识。三类异常填本用例内真实事件次数，不要直接把累计cgroup计数抄过来。`launch_ms`填写按预定成功边界测到的启动耗时；失败时可以留空，成功时必须填写。

```bash
cp examples/measurements_template.csv baseline.csv
cp examples/measurements_template.csv optimized.csv
# 由真实测试工具填入数据后运行，而不是先编造数值。
./build/compare_runs baseline.csv optimized.csv
```

工具强制检查 `(run_id, sequence)`配对和 `case_id`一致，再计算：

```text
event_reduction = (baseline_events - optimized_events) / baseline_events * 100%
```

它同时输出各轮事件数、三类分项及成功用例启动P95。样本不足时会提示，但它不会验证两轮机器环境是否一致，不执行统计显著性检验，也不会将简单计数差异宣称为因果证据。成功用例P95只作描述，成功集合改变时存在幸存者偏差。

## 9. systemd托管示例

以下是在支持systemd的Linux测试机上的可选部署步骤。没有systemd时跳过这一节，由目标平台现有服务管理器常驻启动。

先完成前面的命令行检查和实际小规模试验，再安装；不要在尚未修改路径时直接开机自启。

```bash
sudo install -d -m 0755 /opt/memshift/bin /etc/memshift
sudo install -m 0755 build/memshiftd build/memshiftctl build/compare_runs /opt/memshift/bin/
sudo install -m 0640 config/local.ini /etc/memshift/memshift.ini
sudo install -m 0640 runtime/control.conf /etc/memshift/control.conf
```

把 `/etc/memshift/memshift.ini`中的三个目录字段及控制路径改成：

```ini
log_dir=/var/log/memshift
lock_file=/run/memshift/daemon.lock
control_file=/etc/memshift/control.conf
```

应用逻辑名和控制文件中的保护名单必须一致。暂停字段如仍为true则保持暂停。然后：

```bash
sudo install -m 0644 deploy/memshiftd.service /etc/systemd/system/memshiftd.service
sudo systemctl daemon-reload
sudo systemctl start memshiftd
sudo systemctl status memshiftd
sudo journalctl -u memshiftd -f
```

确认稳定后再由管理员决定是否执行 `systemctl enable memshiftd`。停止用 `systemctl stop memshiftd`。服务化后控制命令应使用 `/etc/memshift/control.conf`，而非项目目录下的相对路径。

服务文件使用 `Type=simple`，不要求程序自行fork脱离终端。不要额外把cgroupfs设为只读，否则回收写入会失败。

## 10. 后续接机器学习的最小改动路线

第一处替换 `src/core.hpp`中的 `Predictor`：使用全局/应用历史特征预测未来缺口、峰值时间和可信范围，保留 `Forecast`输出适配当前调度逻辑。当前5 s线性趋势模型可以作为消融对照，而不是删去后无法比较。

第二处替换 `donorBudget()`中 `cold`候选量的计算：将后台inactive启发式替换为“当前驻留候选集合中，在高峰期间预计不会复用的保守容量”。不要直接用未来pagefault次数乘页大小替代这一容量。

第三处改进风险反馈：加入明确的动作历史、延迟重新访问窗口、启动时延、压缩/换出净收益和执行时间模型；保留现有保护名单、限速、单轮预算、校验及观察模式。逐步替换一个模块并做A/B与消融，才能区分收益来源。

## 11. 接口资料

以上策略阈值为本实现的工程初值，不是内核文档给出的推荐值。以下资料仅支持接口和计数语义，不证明性能收益：

```text
[K1] Linux kernel — Control Group v2
https://docs.kernel.org/admin-guide/cgroup-v2.html

[K2] Linux kernel — The /proc Filesystem, meminfo
https://docs.kernel.org/filesystems/proc.html

[K3] Linux kernel — PSI - Pressure Stall Information
https://docs.kernel.org/accounting/psi.html

[K4] Linux kernel — zswap
https://docs.kernel.org/admin-guide/mm/zswap.html

[K5] Linux kernel — zram: Compressed RAM-based block devices
https://docs.kernel.org/admin-guide/blockdev/zram.html
```
