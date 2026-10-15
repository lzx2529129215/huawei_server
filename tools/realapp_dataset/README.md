# 真实应用 dd 占位采集

这是独立于旧供体实验的采集实现。占位方式为 `/dev/shm` 中单个、实际写入的零填充文件；不使用锁页、持续触碰、测试 HAP 或浏览器分配脚本。文件大小不是物理驻留量，结果标为 `realapp-dd-dataset-v1`，不得称为真实 8 GiB 设备数据。

## 当前可用状态

- 已实现设备预检、真实应用入口清单、6 类场景的 12 轮配对调度、素材生成、UI 操作校准/冻结入口、采集、恢复和训练导出。
- `config/realapp_dataset/suite.json` 是**场景草案**。未填实测 UI 操作和初态恢复步骤，不能作为冻结场景。程序会明确阻止使用未校准草案启动正式采集。
- 首批设备检查发现 15 个原生应用；coremail、小红书、飞书、微信、哔哩哔哩的入口尚待确认。卓易通停留在首次初始化页。部分原生应用也出现首次使用提示。没有自动注册、登录、发送消息或接受这些初始化条款。
- 小文件写入/恢复、设备看护恢复、内核日志时钟对齐已经实机验证。设备证据与当前剩余工作见 `results/realapp_dataset_20261014/`。没有将这些测试标为 12 轮训练数据。
- 完整 23,721 MiB dd 标定实际写入成功，随后观察到 HM_PANIC 重启；重新连通后取回部分日志，但尾部损坏，整轮无效，原因不能确认为 OOM。已恢复原二进制/配置对应的服务和控制状态；此后增加主机端周期证据副本。当前还没有通过完整高负载场景验收，不能无人看护直接批跑 12 轮。

## 使用

以下命令在项目根目录运行，HDC 和目标设备可以用入口顶层的 `--hdc`、`--target` 覆盖。

```powershell
python -X utf8 tools/realapp_dataset/run.py preflight --suite config/realapp_dataset/suite.json --output results/realapp_preflight
python -X utf8 tools/realapp_dataset/run.py fixtures --output results/realapp_materials
python -X utf8 tools/realapp_dataset/run.py inventory-ui --suite config/realapp_dataset/suite.json --output results/realapp_readiness
python -X utf8 tools/realapp_dataset/run.py smoke --output results/realapp_smoke
python -X utf8 tools/realapp_dataset/run.py smoke --watchdog-test --output results/realapp_watchdog
```

`preflight` 只读设备；`inventory-ui` 会打开应用检查入口，关闭本工具新启动且原先没有进程组的应用。`smoke` 会临时将有效 tmpfs 挂载上限改为 32 GiB、暂停服务回收、写入 8 MiB 文件，随后恢复。`calibrate-fill` 使用完整计算占位量并观察 60 秒，仅用于占位标定，不输出合格训练样本。

```powershell
python -X utf8 tools/realapp_dataset/run.py calibrate-fill --output results/realapp_fill_calibration
python -X utf8 tools/realapp_dataset/run.py calibrate --suite config/realapp_dataset/suite.json --output results/realapp_calibration
python -X utf8 tools/realapp_dataset/run.py collect --frozen results/realapp_calibration/frozen.json --output results/realapp_pilot
python -X utf8 tools/realapp_dataset/run.py export --runs results/realapp_pilot/01_office_observe results/realapp_pilot/02_office_apply --output results/realapp_export
```

每次使用新输出目录。`collect` 只接受实际校准生成的 `frozen.json`，不会自行降低负载、替换缺失应用、补出操作坐标或伪造成功结果。场景中途因真实内存失败结束时，尽可能继续 60 秒观察；普通失败不会升级为内存失败。

## 场景配置与校准约束

应用条目记录名称、真实 bundle/宿主、module、ability、运行形式。网页应用使用宿主 UID，采样按 UID 去重；业务应用名仍保留在操作日志中。

每个步骤的 `recipe` 必须提供：

- `setup`：进入该步骤已核验初态的操作。
- `actions`：真实操作，支持 `click`、`text`、`key`、`swipe`、`assert`、`wait`；点击/输入/滑动使用现场 UI 属性选择器，限定所属应用，不接受未经核验的固定坐标。
- `postcondition.selector`：操作后的可见结果，必须唯一匹配。
- `materials`：相对于 `material_root` 的测试素材路径和 SHA-256；或明确的 `online_fixture`。
- `timeout_seconds`、`dwell_seconds`：步骤超时与停留时间。

顶层 `reset_steps` 用于每轮一致的应用初态：每项包含 `app`、`actions`、`postcondition`，只能关闭/复位测试内容，不能清空用户数据。没有真实 reset 配方不允许冻结。素材可设置顶层 `material_root`，复制至共享 Docs 下按清单哈希命名的独立目录；`text` 操作的 `material` 字段引用素材相对路径，执行时转换成设备文件选择器路径。

当前 Qt/Electron 应用可能只暴露窗口装饰节点；仅有启动成功不能当作完成导入/编辑。缺少操作节点时应补充经验证的应用自动化接口或视觉验证适配器，不能用等待时间代替后置条件。

## 内存与恢复协议

通过打开 `/dev/shm` 后读取目录句柄的 `mnt_id` 定位实际挂载，支持同名挂载叠加。占位量为 `floor((MemTotal − 8 GiB)/1 MiB)`；32 GiB 是 tmpfs 上限。核验 dd 返回码、文件长度和实际块数。

设备采样器每 250 ms 采集水位/开关，每秒采集进程组和系统指标，每 30 秒采集 smaps。只遍历配置应用的 cgroup 成员，不扫描全部进程。采样间隔超出 500 ms 时重置连续越线计数。仅在 actions/post 阶段连续两次 `MemAvailable < 0.8 GiB` 后请求一次 `disable`；记录 `readonly` 读回和 `reclaim_in` 状态。系统重新开启时保留变化，不反复写入。

设备端看护先于任何挂载/服务变更启动。30 分钟期限与主机清理共用恢复脚本，核对 boot ID、挂载编号、服务 PID/start ticks 和独占标记，只删除本轮文件，恢复原挂载容量、换出开关和控制文件。恢复失败阻止后续轮次。主机失联后可检查设备看护结果，或在同一次启动中运行：

```powershell
python -X utf8 tools/realapp_dataset/run.py recover --run results/realapp_pilot/01_office_observe
```

重启后默认拒绝使用旧 PID/挂载编号恢复。`recover --after-reboot --run <本轮目录>` 会重新核验独占标记、原服务二进制哈希/配置、原控制内容及新启动的挂载/换出状态；只有原服务未运行且证据全部匹配时才重新启动原服务。当前已有其他服务实例时不替换它。

应用初态恢复、UI 操作和系统换出开关均属于实验干预，记录在独立日志中。现有服务的配置和二进制不替换；仅通过原有 `paused` 控制切换观察/回收，原始控制内容保存后恢复。仅观察组若动作账本出现回收写入，不能作为合格对照组。

## 输出与标签

每轮保存原始系统/进程 JSONL、dd 与恢复记录、内核原始记录、操作日志、UI 观察、服务前后账本及差分、换出状态分段和事件分类。主机每轮还定期拉取 `checkpoints/` 副本；连接中断时保留最后一次成功副本，不能把部分记录当作完整覆盖。

`/dev/kmsg` 的序列号检查揭示丢记录；设备时钟标记对齐内核时间和 BOOTTIME，不直接假设相等。厂商 OOM 选择记录必须有相邻退出证据才确认；进程生命周期用于应用归属，无法归属时只保留系统级 OOM。系统策略杀和崩溃使用独立 hisysevent 来源；可见低内存文字单独记录，均不冒充 kernel OOM。

导出 `windows.jsonl` 与 `labels.csv`：过去 30 秒历史，未来 5/10/30 秒可用内存、最低水位和 OOM。内核覆盖缺失时负标签为未知，未来窗口不足则无效。进程 RSS 是逐进程求和的观测值，不解释为去重物理占用。同场景和配对两组必须在同一个训练/验证分组；12 轮仅作流程试采。

```powershell
python -m unittest discover -s tools/realapp_dataset -p test_*.py -v
```

本地测试覆盖阈值、采样缺口、重新开启分段、PID 复用、内核时间偏移、缺失退出证据、部分 dd 写入、断连恢复、看护过期、服务账本轮转和标签泄漏。

崩溃后的原始 JSONL 不修改。`audit --run <目录>` 保存文件哈希、损坏偏移及可读取前缀，前缀只用于诊断，不提升为合格训练记录。
# 2026-10-14 追查补充

当前设备的大文件恢复故障尚未解决。持久化内核日志已关联到删除本轮 fill 的 rm PID，在 fscache_get_entries / unlink 路径发生内核异常（HM_PANIC_SYSMGR）。`config/realapp_dataset/incidents.json` 记录此故障，超过 8 MiB 的占位及正式采集会被阻断；修改设备前的阻断已实机验证。8 MiB 是已测试的小文件诊断规模，不是推定的安全上限。需要保存修复/替代清理验证证据后再解决此记录。

桌面快捷方式检查的证据在 `results/realapp_dataset_20261014/shortcut_inventory/`：当前 user 桌面、应用中心两页及桌面文件夹均未发现 5 个待确认入口。尚不能冻结真实场景或声称完成 12 轮数据集。本地测试已增加到 26 项。

## 2026-10-15：复用用户占位的探索试采

用户补装后，20 个应用入口均已核查；新增 5 个入口的记录在 `results/realapp_dataset_20261015/app_recheck/`。入口存在不代表登录、素材导入和操作后置条件已经校准。

`python -u -m tools.realapp_dataset.manual --output <新目录>` 支持有人监督的真实应用探索：保留现有 apply 服务策略，60 秒基线、最长 20 分钟操作、60 秒结束观察。该入口只复用用户已有的 `/dev/shm/tmp/fill`、`fill1`、`fill2`，不会分配、删除或截断占位文件，也不会改变挂载容量；仍有服务和换出状态恢复看护。不要把该入口的探索结果直接作为冻结场景的正式训练样本。

首轮位于 `results/realapp_dataset_20261015/manual20_apply01/`：20 GiB 文件保留且前后 stat 相同，恢复验证通过。WPS 打开并翻阅 241 页测试报告，哔哩哔哩播放公开视频；图像导入受黑色弹窗阻塞，抖音停在登录提示。最低 MemAvailable 为 1.77734375 GiB，未到 0.8 GiB 阈值，未请求关闭换出，确认 OOM 为 0。原始记录和质量报告完整保留，但序列尚未冻结及配对，`valid_for_training=false`。

此设备内核日志偶尔乱序交付相邻序号。本轮 406030 条原始记录序号唯一且连续；解析时按序号排序后检查真实缺口、重复和时钟标记，原始文件不修改。修正前的派生结果保留为 `.before_sequence_fix`；乱序、真实缺口和重复记录均有测试覆盖，目前 30 项测试通过。

第二轮 `manual22_apply01` 核实用户占位实际合计 22 GiB（8+8+1+4+1）；手动入口改为记录全部 `fill*`，仍只读复用。基线可用内存约 1.31 GiB，操作期间最低 0.5224609375 GiB。完成抖音播放及切换、剪映导入视频和 84.9 MB 图片并操作 4K 导出、CAD 打开 DXF。系统低内存提示单独分类，完整内核证据未发现 OOM。阈值触发一次关闭请求并读回 readonly，约 113 秒后系统重新开启；未反复抢写。20 分钟操作和结束观察完成，服务、开关恢复核验通过，5 个用户占位文件 stat 前后相同；仍为未冻结、未配对的探索记录。

本轮另发现内核日志中存在非 UTF-8 的二进制诊断文本。解析器现在对这类行使用可逆的 Latin-1 字节映射，保留全部记录与原始文件，并记录 `binary_text_records`；格式损坏或截断仍失败。32 项测试通过。原派生错误保留在 `result.before_binary_reader_fix.json`，重新分析结果及限制见 `quality_report.json`。

