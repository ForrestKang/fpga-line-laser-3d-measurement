# 验证与结果

验证分为文件完整性、软件测试、RTL、整板实现、板端运行和实物尺寸。每层结果单独记录，读回完整性不替代算法或实物精度。

## 已保存的工程结果

| 项目 | 结果 | 证据 |
|---|---|---|
| 原始快照校验 | 1226项匹配 | [原快照校验](../sim/results/original_snapshot_integrity.json) |
| 冻结Python语法 | 493项，0错误 | [语法记录](../sim/results/python_syntax.json) |
| 原ARM回归 | 69项：61通过、7失败、1跳过 | [业务回归](../sim/results/pc_runtime_result.json) |
| RTL集成冒烟 | 空帧PASS | [RTL空帧结果](../sim/results/rtl_empty_result.json) |
| 原整板报告重评 | gate通过；没有重新实现 | [报告门槛重评](../report/source_report_gate.json) |

7项业务失败涉及实时主入口、PL截面对照、停止、截止时间和清理，日志包括`frame_decisions`缺失。当前证据不足以将这些失败简单归因于Mock。字体测试因制作脚本不在测试预期目录而跳过。

## 本地材料与Skill检查

| 项目 | 结果 | 证据 |
|---|---|---|
| 发布检查器自测 | 17项：16通过、1环境跳过 | [Skill测试](../sim/results/pynq_release_check_tests.json) |
| 当前固件配套 | bit/HWH匹配，8项runtime guard中2项不匹配 | [实际发布检查](../sim/results/pynq_release_check_actual.json) |
| 逐项runtime guard | `live.py`、`pl_section.py`不匹配 | [Guard清单](../sim/results/runtime_guard_audit.json) |

Skill的链接逃逸用例因当前Windows环境无法创建相应链接而跳过。其余测试验证了有效包、错误摘要、缺失文件、路径规则、损坏清单和输出防覆盖。

这些是归档中已有的本地材料结果；仓库完善后的新增CI和文档检查应使用独立回执，不覆盖原记录，也不计为新的板测。

## 归档整板结果

| 资源/时序 | 保存的结果 |
|---|---:|
| LUT | 39111，73.5169% |
| FF | 50902，47.8402% |
| BRAM | 78.9286% |
| DSP | 134，60.9091% |
| WNS | +0.230ns |
| WHS | +0.011ns |
| CDC Critical | 0 |
| 系统 / 视频域 | 100 / 125MHz |

这些数字来自保存的实现报告，本次仓库整理没有重新运行Vivado或板卡。资源报告和整板门槛见[实现证据](../build/evidence/)及[报告重评](../report/source_report_gate.json)。

## 可执行检查

在仓库根目录执行文件与Skill检查：

```text
python -B skill/verify_bundle.py .
python -B skill/pynq_release_check.py --manifest board/firmware/manifest.json --runtime-root src/ps
python -B skill/test_pynq_release_check.py
```

发布检查器当前返回FAIL是对两项runtime guard差异的真实发现。保留原manifest，不靠重写摘要把不配套的固件/runtime组合改成“通过”。[工具说明](../skill/pynq_release_check.md)给出输入格式、错误与退出码。

按[复现说明](../report/reproduction.md)恢复源码后，使用新的结果目录执行软件及RTL检查：

```text
python -B sim/run_runtime_tests.py --work <restored_work> --output <new_pc_results>
python -B sim/run_rtl_integration.py --work <restored_work> --output <new_rtl_results> --iverilog iverilog --vvp vvp --cases empty
```

空帧只是集成冒烟。需要完整用例时阅读[仿真说明](../sim/readme.md)和实际入口参数，保存本次命令、配置及结果。

完整整板构建：

```text
python -B build/reproduce.py --work <new_build_work> --output <new_build_output> --vivado vivado
```

构建work必须是尚不存在的新目录，不能复用先前的恢复目录。[构建入口](../build/reproduce.py)要求Vivado 2025.2及XC7Z020器件支持，并运行保存的完整Tcl流程。

## 正确性检查重点

- 图像候选、Q12中心、分支、接受位、拒绝原因和帧质量。
- 几何epoch/SHA、candidate ID、measurement/preview集合。
- Q16 XYZ、几何拒绝、范围与溢出。
- DMA长度、身份、错误footer及完整记录。
- 截面标量、段表、包计数及ARM参考对照。
- 缓冲所有权、跨帧污染、取消、消费者退出及持久化边界。

性能重构逐字段比较冻结输出；算法更改采用独立真值评分，不通过改期望或删困难输入制造通过。

## 性能口径

分别测量PL周期、DMA等待、ARM封装、显示提交、显示确认及持久化时间。重叠运行的阶段不能直接相加作为整链耗时。

板测同时公布有效XYZ、包含新观测的画面更新、持久化速度、画面年龄、CPU、内存、队列峰值，以及冷启动、排空和结束分析时间。显示ACK总数不能直接替代显示帧率，重复旧画面不计新模型更新。

## 实物测量

模拟真值只用于独立评分，不进入条纹提取或拟合。实际尺寸依赖相机、光平面、转轴及转角标定。参与选择标定方案的样件结果属于复核记录，不能同时充当独立精度验证。

[历史板端输出](../board/historical_outputs/)和[实测证据说明](../board/readme.md)应按原来源解释。部分回执附带的是还原后的量化点云，并非完整原图或原始NPY副本；具体见[提取清单](../board/evidence/extraction_manifest.json)。

## 当前工程发布门槛

1. 定位并解决或明确归属7项业务回归失败。
2. 确定真实对应的固件/runtime组合，审查新配套清单并验证，保留原记录。
3. 完成所需完整RTL用例与可复现整板构建。
4. 固定源码、库、固件及配置后进行板端完整链路验证。
5. 取得独立光学和尺寸证据后，再声明对应装置的实物精度。

当前候选状态与提交清单见[已验证状态](../report/verified_status.md)和[发布检查单](../report/release_checklist.md)。

## 本次仓库交付检查

本轮完成新首页与文档链接检查、Python语法检查、发布工具自测、源码恢复和Git文件集合核对。结果汇总见[repository_delivery_checks.json](../sim/results/repository_delivery_checks.json)。这些检查均在电脑执行，不替代上表保留的业务、RTL、整板或实物门槛。
