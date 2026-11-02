# 从零复现说明

## 1. 准备环境

工程平台为PYNQ-Z2（XC7Z020），相机MT9V034，HDMI输出，步进驱动接口。源码实现采用SystemVerilog/Verilog、ARM Python和原生C；没有独立HLS工程。

电脑离线检查使用Python 3.10及以上；业务测试按 `sim/requirements_pc.txt` 安装依赖。完整硬件构建使用Vivado 2025.2及XC7Z020器件支持。板端使用对应PYNQ Linux/Python环境，Windows原生库不能直接复制到ARM运行。

所有示例从解压后的工程根目录执行。示例输出路径必须是新目录，不覆盖已有材料。

## 2. 核对提交文件

```text
python -B skill/verify_bundle.py .
```

该步骤只检查文件集合和内容完整性，不证明运行速度或实物精度。

## 3. 恢复原工程文件名和相邻目录

提交仓库使用英文小写文件名，原源码中的大小写及相对路径通过 `source_map.json` 恢复：

```text
python -B build/restore_sources.py --output D:/Workspace/CodexTemp/scan3d_reproduce/work
```

Linux下换成可写的新绝对路径。程序会先检查源码，再创建 `fpga/`、`runtime/` 等工作目录；不会连接板卡。

## 4. 离线软件与RTL检查

```text
python -m pip install -r sim/requirements_pc.txt
python -B sim/run_release_tests.py --work D:/Workspace/CodexTemp/scan3d_reproduce/work --output D:/Workspace/CodexTemp/scan3d_reproduce/pc_results --cc gcc
python -B sim/run_section_rtl.py --work D:/Workspace/CodexTemp/scan3d_reproduce/work --output D:/Workspace/CodexTemp/scan3d_reproduce/section_results --iverilog iverilog --vvp vvp --workers 2
python -B sim/run_rtl_integration.py --work D:/Workspace/CodexTemp/scan3d_reproduce/work --output D:/Workspace/CodexTemp/scan3d_reproduce/rtl_results --iverilog iverilog --vvp vvp --cases empty
```

完整软件入口需要本机C编译器，依赖包括OpenCV，用于图像和显示测试。主机编译库不能复制给ARM运行。截面入口使用182帧历史原图与13个合成输入，完整核对195包；RTL入口均依赖Icarus Verilog。`empty`只代表空帧冒烟，完整19例的显式参数见[仿真说明](../sim/readme.md)。

本轮已修复旧Mock合同、故障注入检查、字体查找和参考测试入口，并修复严格日志读回的协议分派与水平双像素策略支持。默认质量仅用PL摘要，Raw统计改为显式诊断；原七处失败及绑定差异留作历史。最终完整软件与RTL结果见[验证汇总](release_validation.json)，不与Skill自测混合计数。

## 5. 从源码构建整板固件

```text
python -B build/reproduce.py --work D:/Workspace/CodexTemp/scan3d_rebuild/work --output D:/Workspace/CodexTemp/scan3d_rebuild/output --vivado vivado
```

这是另一组新目录，不能复用第3步已存在的work目录。脚本恢复原源码并运行原 `build_final.tcl`，默认双通道，系统100MHz、视频125MHz。它只构建，不下载bit。

检查构建日志、DRC、CDC和布线后时序，再核对bit/HWH与清单。保存的历史整板报告为WNS +0.230ns、WHS +0.011ns、CDC Critical 0；本候选不把这些数字写成今天重新布局布线的结果。

## 6. 板端准备

先读[板端说明](../board/readme.md)。核对板卡身份、当前程序所有者、供电与接线、可用空间和输出目录，检查软件与固件配套。当前manifest通过`runtime_validation_file`引用[新软件验证记录](../board/firmware/runtime_validation.json)；[旧manifest](../board/firmware/history/manifest_before_repair.json)和`source_freeze.json`保留来源，不以替换历史摘要绕过准入。11项离线配套测试及27个归档primary子例通过不等于已在当前板卡加载。

`board/boot_reference/`用于说明旧部署关系，包含装置相关路径，不是复制到新板就能直接覆盖的安装包。新板先完成无运动的静态图像/回放验证，再评估开机服务。

## 7. 启动与操作关系

恢复后的实时入口是 [Live Runtime](../src/ps/r33/live.py)，真实PL回放入口是 [Replay Runtime](../src/ps/r33/replay.py)。先在对应运行环境查看参数：

```text
python -B -m r33.live --help
python -B -m r33.replay --help
```

导入路径应指向恢复后的 `runtime/`。真实运行需要有效的相机内参、光平面、转轴和当前设备配置，不把另一套装置的标定直接当作通用参数。

三页分别为实时观测、截面测量、整圈扫描。BTN1–3切页，BTN0启动/停止扫描。程序启动成功、页面显示成功、数据完成保存和尺寸验证通过是不同状态。

## 8. 结果核对

检查接受帧范围、源身份、measurement/preview分离、保存完成状态、失败范围，以及点云和测量结果是否与参考对应。模拟数据、板上回放和真实光学测量分开记录。

静态台阶用相邻台面的高差核对；旋转件另核转轴和实际转角。不要把背景到物体的距离当作小台阶的高度，也不利用被测件标称尺寸同时调参并证明精度。

## 9. 故障处理

- 相机无响应：先留日志并核对接线/供电/当前所有者，不循环加载或转动碰运气。
- 固件软件不匹配：停止该发布组合，核对版本来源，不重写摘要掩盖差异。
- 保存不足或同步超时：停止接收新任务，保留已完成和失败范围，不把入队当持久化。
- 取消与进程退出：保留输出、错误状态和最后接受帧；收尾完成后才确认释放资源。

完成每一步都应留下实际命令与结果。某一步没有执行，就写未执行，不用后一步的截图代替。
