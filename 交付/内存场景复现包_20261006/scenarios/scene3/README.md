# 场景3：两例快速预验证

从交付包根目录执行。它在场景2引擎上运行两个固定case，用于较短的分类事件预验证：popup（8个负载应用daily各256 MiB，peak 5120 MiB，hold 60秒）和startup（daily各256 MiB，peak 12288 MiB，hold 0秒），固定seed为20261005。完整执行两例，不会在达到某个事件数后提前停止。

“约20次”表示启动失败、真实kernel OOM、可见低内存popup三类分类事件的描述性总量目标18–24；三类各至少1个真实事件。事件按类别计数，所以同一PID/start_ticks事故可以同时贡献startup failure与OOM两个分类事件；unique incidents另报。被系统模态遮挡的测试弹窗、超出case时间窗的截图、普通启动超时都不能冒充确认事件。只有case内截图/UI与身份、kernel evidence完整时才纳入本轮预验证判断。

## 运行

在包根PowerShell中先做设备只读预检，再运行：

```powershell
py -3 -X utf8 .\run.py --config .\config\scenarios_scene3_quick.json preflight --output .\results\preflight_scene3.json
py -3 -X utf8 .\run.py --config .\config\scenarios_scene3_quick.json run --scene 3 --phase baseline --profile development --rounds 1
```

`--config`是顶层选项，必须放在`run`、`preflight`等子命令之前。确保设备在线、工程调试权限可用、包内8个签名HAP已安装。不要在设备上加压其他负载或修改系统内存策略。

输出目录为`results/scene3_baseline_YYYYMMDD_HHMMSS_s3_<id>/`。查看`result.json`中的`scene=3`、scope=`short_diagnostic_prevalidation`、`scene3_target`和`primary_evidence_complete`，并逐case核`case_measurement_complete`、kernel/UID/PID/start_ticks、时间窗内UI截图、owned app/page cleanup。场景3不走`qualification`、`freeze`、`acceptance`或formal compare门槛；18–24是描述性范围，不是验收阈值。未知或缺失的数据不得当成0。

## UI审计

将下面占位目录替换为新run实际目录。审计读取result、截图和UI证据，写一个hash/source-bound、可移动的`scene3_ui_audit.json`；`--verify`会重新核验源关系：

```powershell
$result = '.\results\scene3_baseline_实际运行目录\result.json'
$audit = '.\results\scene3_baseline_实际运行目录\scene3_ui_audit.json'
py -3 -X utf8 .\tools\audit_scene2_ui.py --result $result --output $audit --relative-source
py -3 -X utf8 .\tools\audit_scene2_ui.py --verify $audit
```

同配置的旧完整case仅作时长参照：popup外层case约185秒，startup约138秒，两例串行用时约6分钟；同一11-case旧run的首case_begin至末case_end约30分17秒。旧计数经UI审计后为popup case 7类事件、startup case 13类事件，合计20（unique 11），不是本次scene3成绩。场景2暂停run中c0001不完整，不用于历史基准或scene3计数。

本包已完成一次实际运行`results/scene3_baseline_20261007_121853_s3_e07404295377/`：popup case分类合计7（startup失败3、OOM3、native popup1），startup case分类合计13（startup失败6、OOM6、native popup1），合计20类事件、11 unique incidents；test popup、timeout均为0。source-bound UI audit verify成功，两张截图都证明TaskManager低内存弹窗在case内可见，16组journal配对及两case BEGIN/END marker齐全，owned cleanup读回完成。首case_begin至末case_end耗时314.68秒；详细报告见`../../results/scene3_quick_20261007/report.md`。

场景3沿用随包`results/calibration_qualification32.json`中8个应用的5120/12288 MiB负载测量作为参考；audit核得源文件SHA一致，但该校准suite hash不等于scene3，且不作为scene3/正式验收门槛。当前运行只是32 GiB短时诊断预验证，不能替代场景2固定11例资格门槛（baseline至少100分类事件），更不能替代真实16 GiB设备上的正式验收。
