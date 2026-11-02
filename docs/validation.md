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

上表是原交接包的历史记录。本轮已复现并修复旧Transport Mock缺少`frame_decisions`和记录计数的合同问题，且为故障测试补上注入帧、次数和错误原因检查，防止提前异常误判为通过。字体测试改为定位随包构建脚本并执行实际字形覆盖；旧参考测试分别绑定对应的图像协议解码器和黄金几何模型。原失败日志保留，新结果另立记录。

## 本轮修复与实跑

| 范围 | 已确认内容 | 证据 |
|---|---|---|
| 默认质量路径 | 正常帧只读PL行摘要和帧决定位；Raw饱和统计仅显式诊断，不改变接受判据 | [Live Runtime](../src/ps/r33/live.py)、[诊断测试](../src/ps/r33/tests_quality_diagnostics.py) |
| 严格存储兼容 | 按primary头部显式选择图像观测或几何观测严格解码器，支持水平双像素策略 | [Storage](../src/ps/r33/storage.py)、最终验证汇总 |
| 软件与固件配套 | 11项离线配套测试、27个归档primary子例通过 | [配套测试入口](../sim/test_release_pairing.py)、[验证汇总](../report/release_validation.json) |
| 截面RTL | 182帧历史原图与13个合成输入，共195包逐位对照通过 | [截面复现入口](../sim/run_section_rtl.py)、验证汇总 |
| 完整runtime与整板RTL矩阵 | 最终计数、逐例结果和失败清单以原始回执为准 | [验证汇总](../report/release_validation.json) |

手动初始化曝光probe保持原独立逻辑；`--diagnostic-raw-quality`不控制曝光或测量接受。45份非生成硬件构建输入、bit/HWH未变；本轮没有重新布局布线、上板或新增实物精度验证。

## 本地材料与Skill检查

| 项目 | 结果 | 证据 |
|---|---|---|
| 发布检查器自测 | 17项：16通过、1环境跳过 | [Skill测试](../sim/results/pynq_release_check_tests.json) |
| 原交接包固件配套 | bit/HWH匹配，当时两项runtime guard不匹配 | [原始发布检查](../sim/results/pynq_release_check_actual.json) |
| 原逐项runtime guard | 当时的Live Runtime、Section Measurement Driver有差异 | [历史Guard清单](../sim/results/runtime_guard_audit.json) |

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

原检查器返回FAIL真实反映了当时的两项软件差异。[旧manifest](../board/firmware/history/manifest_before_repair.json)与硬件源码冻结记录保留；本轮软件回归及配套检查另记于[运行时验证记录](../board/firmware/runtime_validation.json)，由当前manifest引用。[工具说明](../skill/pynq_release_check.md)给出输入格式、错误与退出码。

按[复现说明](../report/reproduction.md)恢复源码后，使用新的结果目录执行软件及RTL检查：

```text
python -m pip install -r sim/requirements_pc.txt
python -B sim/run_release_tests.py --work <restored_work> --output <new_pc_results> --cc gcc
python -B sim/run_section_rtl.py --work <restored_work> --output <new_section_results> --iverilog iverilog --vvp vvp --workers 2
python -B sim/run_rtl_integration.py --work <restored_work> --output <new_rtl_results> --iverilog iverilog --vvp vvp --cases empty
```

完整软件入口包含本机C库编译及所有runtime测试，依赖OpenCV、fonttools等声明依赖；主机库不替代ARM库。空帧只是集成冒烟，完整19例矩阵需显式传入[仿真说明](../sim/readme.md)列出的`--cases`；保存本次命令、配置及结果。

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

1. 检查本轮完整runtime回归及RTL矩阵的最终原始回执，不把历史失败计为当前状态。
2. 审查当前固件/runtime组合和独立验证记录，保留原清单及失败记录。
3. 需要重建时执行完整Vivado实现；本轮RTL仿真不替代重新布局布线。
4. 固定源码、库、固件及配置后进行板端完整链路验证。
5. 取得独立光学和尺寸证据后，再声明对应装置的实物精度。

当前候选状态与提交清单见[已验证状态](../report/verified_status.md)和[发布检查单](../report/release_checklist.md)。

## 本次仓库交付检查

本轮完成新首页与文档链接检查、Python语法检查、发布工具自测、源码恢复和Git文件集合核对。结果汇总见[repository_delivery_checks.json](../sim/results/repository_delivery_checks.json)。这些检查均在电脑执行，不替代上表保留的业务、RTL、整板或实物门槛。
