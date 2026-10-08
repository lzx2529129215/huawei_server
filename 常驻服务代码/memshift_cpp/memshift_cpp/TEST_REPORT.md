# MemShift 0.1 本次代码验证记录

## 已验证

| 检查 | 环境/范围 | 结果 |
|---|---|---|
| Release编译 | Linux x86_64, GCC 14.2.0, C++17 | 通过 |
| 第二编译器检查 | Clang 17.0.0, C++17 | 通过 |
| 编译警告检查 | -Wall -Wextra -Wpedantic -Wconversion -Wshadow | 最终构建无warning/error输出 |
| 策略/解析单元测试 | tests/test_policy.cpp，50项检查 | 全部通过 |
| 模拟联调 | tests/smoke.py，模拟proc/cgroup文件 | 通过 |
| UBSan构建和测试 | GCC Debug, -fsanitize=undefined | 两项CTest通过，已覆盖路径未报告UBSan错误 |
| 真实接口只读采样 | 当前执行环境的/proc及cgroup命名空间根 | 内存和cgroup统计解析通过 |

最终三种构建(GCC Release、Clang Release、GCC UBSan)均执行CTest，结果均为2/2通过。模拟联调覆盖：后台候选请求计划、运行时前台保护、冷却、暂停、损坏控制文件安全关闭、通知原子合并、禁止fixture实际回收、禁止互斥模式混用、统计工具配对检查、零基线处理。

其中的“50项”指策略测试程序中的50个检查，不表示50个真实应用或50个性能用例。

## 未验证

没有执行真实的 `memory.reclaim` 写入，没有在16 GB合同目标机上运行实际应用组合，也没有测得10%或其他百分比的性能/异常率收益。当前执行环境未提供完整的全局 `/proc/pressure/memory`、`/proc/pressure/io` 接口，并非本版要求的宿主机验证环境。

实际回收工作线程中的内核执行、EAGAIN/权限/延迟行为仍需在目标机器做接口联调；模拟观察模式没有覆盖真实内核回收分支。编译、单元测试和UBSan通过不构成“没有错误”或“达到合同指标”的证明。

`tests/smoke.py`中的10与9是统计函数的人工算术测试值，只用于验证百分比计算，不是实际基线和优化数据。项目中的正式测量模板保持为空。

## 重现本地检查

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
ctest --test-dir build --output-on-failure
./build/test_policy
```

当Python3不可用时，CMake只注册C++策略测试；服务、通知工具、统计工具仍可正常编译，运行时不依赖Python。
