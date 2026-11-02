# 自主选题赛道提交核对

工程内容依据用户提供的AMD选题指南第3.3节；实际上传格式和大小按本轮报名后台截图执行。报名后台尚未提交。

## 后台上传要求

| 项目 | 格式与限制 | 当前准备状态 |
|---|---|---|
| 完整代码 | RAR，≤200MB | 从本工程生成`source_code.rar`；不用ZIP替代后台要求 |
| 演示视频 | MP4、H.264，<3分钟、≤300MB | 使用已选定完整视频，发布前复核编码、时长和大小 |
| 技术报告 | PDF，≤50MB | 报告完善后导出`design_report.pdf`；Word保留供编辑 |
| 签署授权书 | PDF，≤50MB | 缺本队签署件；参考工程签署件不能用于本队 |
| 海报（可选） | PDF或PPT，≤50MB | 匿名英文单页副本已准备；提交格式按后台选择 |
| 作品全貌与板卡重要角度照片 | JPG/PNG，每张≤10MB，最多4张 | 本地准备3张真实照片/既有实际帧，不重复裁图凑数 |
| 仓库链接 | GitHub地址 | 沿用`ForrestKang/pynq-structured-light`；公开待所有者操作 |
| 文字字段 | 作品简介、3关键词 | 集中写入本地审阅目录的`readme.md` |

当前后台格式已明确，不再将其列为未知。上传是否成功、报名组别及最终回执仍须在实际提交时核对。

## 完整代码内应有的内容

| 内容 | 本工程位置 |
|---|---|
| 实际采用的RTL、PS、原生C、约束与BD | `src/fpga/`、`src/ps/`；没有采用HLS，不虚构HLS工程 |
| 仿真与验证结果 | `sim/`、`build/evidence/`、`src/ps/evidence/` |
| 综合、实现、资源与时钟 | `build/evidence/reports/`、`board/firmware/admission_final.json` |
| 上板脚本、输出及参考 | `board/`、`src/ps/`、`data/` |
| 可复现构建 | `build/reproduce.py`、`build/restore_sources.py`及原Tcl入口 |
| 可复用Skill | `skill/`，含说明、适用/失效条件、工具及自测 |
| 报告与AI协作 | `report/design_report.docx`、`report/design_report.pdf`、[AI记录](ai_collaboration_records.md) |
| 开源许可 | 继承既有MIT，第三方保持原许可，见[许可说明](third_party_notices.md) |

工程采用英文小写存档路径；恢复工具按来源映射恢复原始大小写。Skill和AI记录纳入同一份代码RAR，不生成独立压缩包。完整录像、整卡镜像、全部历史采集和聊天数据库不放入代码提交件。

## 当前审核状态

- 报名后台尚未提交。
- 本队签署授权书未提供，不能以参考项目文件充当。
- 原仓库地址已确认，当前私有；所有者完成公开后再检查匿名访问。
- 工程状态以[本轮验证汇总](release_validation.json)及独立软件绑定记录为准；旧失败继续保留。材料完整性检查不替代板端或实物验收。
- 本地文件位置和待补项以审阅目录`readme.md`及[发布检查单](release_checklist.md)为准。
