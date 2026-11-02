# 当前材料状态

本候选在原交接包的新副本中修复电脑回归、严格存储兼容和正常质量路径。45份非生成硬件构建输入、bit/HWH、旧源码冻结记录和历史失败保留；本轮没有重新布局布线，没有运行板卡、相机或电机。

## 本轮完成

- 找回原仓库 `https://github.com/ForrestKang/pynq-structured-light`，核对已有上传回执和当前私有状态；继承仓库既有MIT许可证，保持私有；推送记录以仓库commit为准。
- 收录技术报告Word/PDF、两个真实AI协作案例、技能开发说明、从零复现步骤及发布检查单。
- 新增通用PYNQ发布检查器及17项测试：16项通过，1项因当前Windows环境不能建立链接而跳过。
- 原交接包两项runtime guard差异保留在历史清单和回执；当前软件组合另立[运行时验证记录](../board/firmware/runtime_validation.json)，由当前manifest引用。
- 修复旧Transport Mock缺少新协议字段及故障测试提前失败的问题，补充注入帧、次数、精确原因检查和实际字体覆盖。
- 默认帧质量只读取PL摘要；Raw饱和统计需显式`--diagnostic-raw-quality`，不改变接受判据，手动初始化曝光probe独立保留。
- 严格保存读回按primary版本选择对应严格解码器并支持水平双像素策略；旧参考测试明确绑定图像协议和黄金几何入口。
- 已确认11项离线配套测试、27个归档primary子例通过；截面RTL以182帧历史原图和13个合成输入完成195包逐位对照。
- 在D盘全新临时目录成功执行 `build/restore_sources.py`，证明源码恢复入口可运行；未把这一项写成完整Vivado构建通过。
- 由既有AMD模板海报生成匿名英文单页副本，保留原始中英双页文件；视频独立交付。

## 保留的工程门槛

| 项目 | 当前结论 | 证据 |
|---|---|---|
| 完整runtime回归 | 使用全部打包测试及主机原生辅助库，最终计数和结果见回执 | [验证汇总](release_validation.json) |
| 固件与软件配套 | 11项离线测试、27个归档primary子例通过，新软件绑定独立记录 | [验证汇总](release_validation.json)、[运行时记录](../board/firmware/runtime_validation.json) |
| RTL回归 | 截面195包逐位一致；整板矩阵最终逐例结果见回执 | [验证汇总](release_validation.json) |
| 整板实现 | 读取已有报告，没有本轮重新实现 | `report/source_report_gate.json` |
| 仓库开放 | 仍为私有，公开及最终发布分支待完成 | `report/repository_and_license.md` |
| 新实物测量 | 本轮未执行 | 无新板端证据 |

既有整板报告：系统100MHz、视频125MHz；LUT 39,111（73.5169%），FF 50,902（47.8402%），BRAM 78.9286%，DSP 134（60.9091%），WNS +0.230ns、WHS +0.011ns、CDC Critical 0。以上是归档报告数据，不是本轮重测。

当前状态按[本轮最终验证汇总](release_validation.json)逐项判定，不预填正在运行的完整runtime和整板矩阵结果。历史逐项说明见[原交接状态](history/submission_status_initial.md)及[旧manifest](../board/firmware/history/manifest_before_repair.json)。软件修复和电脑测试不替代当前装置的板端性能或实物精度验收。报名后台尚未提交。
