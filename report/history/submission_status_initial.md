# 证据边界与当前状态

本包整理于 2026-11-01，来源为 FINAL_BOARD_PACKAGE_20261101 的只读快照。此次整理不连接板卡、不加载固件、不运行相机或电机。

最终固件：final.bit SHA256 db396f4c3e5c8276bfe426702589542799d37df7c981243f6b455da43a79c840。
已有整板实现报告：2 路、系统 100 MHz、视频 125 MHz；WNS +0.230 ns、WHS +0.011 ns、CDC Critical 0。
资源：LUT 39,111 / 73.5169%，FF 50,902 / 47.8402%，BRAM 78.9286%，DSP 134 / 60.9091%。这些来自已保存实现报告，本次不声称重新布局布线。

## 必须注意

1. firmware/manifest.json 是历史冻结清单，保留原字节，其状态字段及 runtime_guards 对应当时快照；不得因包含 bit 就将 hardware_verified 改为 true。当前 runtime 是后续 rig2b 版本，两者部分 runtime_guards 已不同，按该 manifest 的严格加载器可能拒绝。不要跳过校验或直接改 hash 冒充同版；如需新发布，应重新生成并审查配套 manifest。
2. 历史板端回执与旧标定扫描输出仅作为已发生实验的证据。当前轴/光平面、角度与物体区域仍有独立核验缺口；T02 联合拟合结果不等于独立尺寸精度通过。
3. data/calibration 保存当前与历史参数及拟合来源，不代表可直接用于任何重新安装的装置。复现几何需核对装置和独立标定。
4. board/historical_outputs 明确是 2026-10-31 旧标定结果；不代表当前宇航员或任意物体已准确重建。
5. build 中历史脚本 finalize_seedretry.tcl 需要未随包提供的 route.dcp，且其路径是旧工作目录；它不是本包推荐的构建入口。请从源码使用 build/reproduce.py。
6. 原 boot 配置包含装置专用路径和自动启动策略，仅作源码参考，不得原样覆盖新板 /boot。历史用户电机许可及加载批准文件不随包分发。
7. 本次归档保留原已有失败报告，不将缺失的历史失败 payload、原聊天日志或未做的长测写为已补齐。

## 本次实际检查与未通过项

| 检查 | 结果 | 原始证据 |
|---|---|---|
| 原目录快照完整性 | 原清单 1,226 项大小、SHA 全部一致 | sim/results/original_snapshot_integrity.json |
| 收录的冻结 Python 源码语法 | 493 项，0 语法错误 | sim/results/python_syntax.json |
| 原整板报告门槛重评 | 通过；不等于本次重新实现 | report/source_report_gate.json |
| 空帧 RTL 集成冒烟 | PASS，协议/几何/累加器对照通过；仅 empty 用例 | sim/results/rtl_empty_result.json |
| 原 ARM 离线回归 | 69 项：61 通过、7 失败、1 跳过 | sim/results/pc_runtime_result.json、pc_console.log |

7 项失败属于 live/截面/停止与清理等集成测试，日志出现 `frame_decisions` 缺失。
本次保持原 runtime 和测试字节不变，不将其简单归咎于 Mock，也不宣称系统功能全部通过。
字体覆盖测试因原字体构建脚本不在测试期望的相邻目录而跳过；字体、许可及制作脚本本身均有保留。
固件 runtime_guards 中的 live.py、pl_section.py 两项与后续 runtime 不同，逐项哈希见 sim/results/runtime_guard_audit.json。

本次任务是审查并整理提交材料，没有扩大为修改业务算法、伪造加载清单或重做板端验收。
因此标为“自主赛道提交候选”；修复/核对上述配套问题后才能据实声明可直接部署的正式发布版。
