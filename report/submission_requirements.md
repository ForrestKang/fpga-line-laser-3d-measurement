# 自主选题赛道提交核对

依据：用户提供的 AMD 选题指南，第 3.3 节，PDF 第 22–25 页（印刷页 19–22），以及 PDF 第 28 页“其它”。不套用智能体开发赛道或具身智能赛道的工程要求。

| 要求 | 本包位置 / 状态 |
|---|---|
| 完整实际采用的 RTL、PS、原生 C、约束与 BD | src/fpga、src/ps；未采用 HLS，不虚构 HLS 工程 |
| 仿真与验证结果 | sim、build/evidence、src/ps/evidence；历史结果与本次打包检查分开 |
| 综合、实现、资源、时钟与指标 | build/evidence/reports、board/firmware/admission_final.json |
| 上板运行脚本、输出与参考对照 | board、src/ps/r33、data；历史标定输出不能冒充当前精度 |
| 可复现脚本 | build/reproduce.py、build/restore_sources.py、原 scripts/build_final.tcl |
| 独立技能包及适用/失效条件 | skill/readme.md、skill/verify_bundle.py、skill/pynq_release_check.py |
| 设计报告、AI 协作和纠错记录 | report/design_report.docx、report/design_report.pdf、report/ai_collaboration_records.md |
| 纯英文小写目录和文件名 | 本包统一小写；恢复脚本在新工作目录恢复源码原始大小写，保持冻结源码字节不变 |
| 开源仓库与可再分发许可证 | 沿用ForrestKang/pynq-structured-light及既有MIT许可，保留第三方原许可；仓库仍为私有，公开访问和最终发布入口待完成 |
| 初级组/高级组 | 待团队按学历组成确认；两组提交内容相同 |
| 决赛英文单页海报 | 指南要求按 AMD 模板、不含队伍信息，A4 彩印带现场；已由既有AMD模板制作匿名英文单页PPTX及A4 PDF，见docs/media/，本地交付目录也保留 |

指南文字要求小写，而示意图写 README.md。本包遵从更严格的小写命名，使用 readme.md。
指南未在 3.3.5 工程目录里要求把完整开发历史、系统镜像、全部录像或聊天数据库塞进源码 ZIP，因此不随包带入。
完整原始聊天日志未作为本包材料导出；AI 文件提供真实可追溯节选及其来源性质，不冒充全部原始记录。

## 提交准备状态

工程目录、源码、必要 IP、约束、构建/验证入口、固件、报告和可复用 skill 已归集。
以下事项不得被“打包成功”掩盖：

- 原 runtime 回归 7 失败、1 跳过，以及固件与后续 runtime 的两项冻结哈希差异尚未修复。
- AI 协作文件为有来源性质说明的精选记录，未提供全量原始聊天导出。
- 仓库地址和既有MIT许可已核实；公开状态、实际发布入口和队伍组别仍需核对。指南对开源为“强烈要求”，本候选没有改变仓库可见性。
- 匿名英文单页海报已另行交付并收录docs/media/并收录docs/media/；现场打印及后台栏目仍需按实际通知确认。

只保留本次核对实际存在的材料，不以空白占位文件表示这些项目已经交付。
