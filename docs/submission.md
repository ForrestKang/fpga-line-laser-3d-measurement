# 参赛与交接材料

本轮按已提供的报名后台截图准备材料，报名后台尚未提交，仓库可见性保持不变。

## 后台字段与文件

| 字段 | 已知要求 | 本地准备位置 |
|---|---|---|
| 完整代码 | RAR，≤200MB | `review/source_code.rar`；包含完整工程、报告附件、AI记录和Skill |
| 作品视频 | MP4、H.264，时长<3分钟、≤300MB | `review/demo_video.mp4` |
| 技术报告 | PDF，≤50MB | `review/design_report.pdf` |
| 签署授权书 | PDF，≤50MB | 缺本队签署件，取得后单独放入审阅目录 |
| 海报（可选） | PDF或PPT，≤50MB | `review/finals/`；可编辑文件放`review/editable/` |
| 作品全貌及开发板角度照片 | JPG/PNG，每张≤10MB，最多4张 | `review/images/`，准备3张真实不同视角图片 |
| GitHub链接 | 原工程仓库地址 | https://github.com/ForrestKang/pynq-structured-light |
| 作品简介与关键词 | 简介、3个关键词 | `review/readme.md` |

上表中的`review/`指本次本地审阅目录，不是仓库内目录。待生成或待补齐的文件以实际文件和最终检查结果为准。AI记录和Skill随完整代码RAR提供，不另设AI或Skill压缩包。

## 工程内导航

| 内容 | 位置 |
|---|---|
| 作品总览与架构 | [首页](../readme.md)、[系统架构](architecture.md) |
| 报告源件 | [Word](../report/design_report.docx)、[PDF](../report/design_report.pdf) |
| PL、约束及BD | [FPGA源码](../src/fpga/)、[构建说明](build_and_deploy.md) |
| ARM与原生C | [运行软件](../src/ps/) |
| 固件与清单 | [Firmware](../board/firmware/) |
| 验证及失败记录 | [软件/RTL](../sim/readme.md)、[整板报告](../build/evidence/)、[验证状态](validation.md) |
| AI协作记录 | [两个真实案例](../report/ai_collaboration_records.md) |
| 可复用技能 | [Skill索引](../skill/readme.md)、[开发说明](../report/skill_development.md) |
| 复现与交接 | [复现步骤](../report/reproduction.md)、[发布检查单](../report/release_checklist.md) |
| 匿名英文海报 | [PPTX](media/poster_en_anonymous.pptx)、[A4 PDF](media/poster_en_anonymous_a4.pdf) |

## 尚需补齐

- 授权书必须是本队实际签署的文件；参考项目中的授权书不能代替。
- 仓库暂为私有，公开由所有者后续操作；公开后再验证未登录访问。
- 本轮材料仍待本地审阅，后台上传成功状态尚未核验。
- 工程状态按[本轮验证汇总](../report/release_validation.json)判定；旧失败及绑定记录保留，新软件配套独立归档。材料检查和电脑测试不替代板端或实物验收。
