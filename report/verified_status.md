# 当前材料状态

本候选沿用冻结工程，新增提交说明、AI协作案例、复现步骤及离线发布检查工具。原始源码、固件、标定与失败证据保持原样；本轮没有运行板卡、相机或电机。

## 本轮完成

- 找回原仓库 `https://github.com/ForrestKang/pynq-structured-light`，核对已有上传回执和当前私有状态；继承仓库既有MIT许可证，保持私有；推送记录以仓库commit为准。
- 收录最新技术报告Word/PDF、四个有来源的AI协作案例、技能开发说明、从零复现步骤及发布检查单。
- 新增通用PYNQ发布检查器及17项测试：16项通过，1项因当前Windows环境不能建立链接而跳过。
- 实际检查固件文件匹配；8项runtime guard中6项匹配、2项不匹配，工具正确返回失败，未重写冻结清单。
- 在D盘全新临时目录成功执行 `build/restore_sources.py`，证明源码恢复入口可运行；未把这一项写成完整Vivado构建通过。
- 由既有AMD模板海报生成匿名英文单页副本，保留原始中英双页文件；视频独立交付。

## 保留的工程门槛

| 项目 | 当前结论 | 证据 |
|---|---|---|
| 原ARM业务回归 | 69项：61通过、7失败、1跳过，未在本轮修复或重跑 | `sim/results/pc_runtime_result.json` |
| 固件与软件配套 | [Live Runtime](../src/ps/r33/live.py)、[Section Measurement Driver](../src/ps/r33/pl_section.py) 两项guard不匹配 | `sim/results/pynq_release_check_actual.json` |
| RTL回归 | 归档中有空帧冒烟通过，不能代表完整输入回归 | `sim/results/rtl_empty_result.json` |
| 整板实现 | 读取已有报告，没有本轮重新实现 | `report/source_report_gate.json` |
| 仓库开放 | 仍为私有，公开及最终发布分支待完成 | `report/repository_and_license.md` |
| 新实物测量 | 本轮未执行 | 无新板端证据 |

既有整板报告：系统100MHz、视频125MHz；LUT 39,111（73.5169%），FF 50,902（47.8402%），BRAM 78.9286%，DSP 134（60.9091%），WNS +0.230ns、WHS +0.011ns、CDC Critical 0。以上是归档报告数据，不是本轮重测。

当前状态为“提交文档候选，工程准入待闭合”。历史逐项说明保存在 `report/history/submission_status_initial.md`。原冻结清单、旧标定限制、缺失历史数据及失败记录均继续保留。不能直接部署前绕过guard，也不能只改摘要把不配套的运行时写成已验证版本。
