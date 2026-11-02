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
python -B sim/run_runtime_tests.py --work D:/Workspace/CodexTemp/scan3d_reproduce/work --output D:/Workspace/CodexTemp/scan3d_reproduce/pc_results
python -B sim/run_rtl_integration.py --work D:/Workspace/CodexTemp/scan3d_reproduce/work --output D:/Workspace/CodexTemp/scan3d_reproduce/rtl_results --iverilog iverilog --vvp vvp --cases empty
```

第二条依赖Icarus Verilog。`empty`只是空帧集成冒烟，不代表完整实拍集通过。需要完整用例时先读 `sim/readme.md` 和原测试入口支持的参数。

继承的业务回归记录有7失败、1跳过；详见 `verified_status.md`。本说明没有把它们改成通过。新增Skill的自测与这些业务测试分别计数。

## 5. 从源码构建整板固件

```text
python -B build/reproduce.py --work D:/Workspace/CodexTemp/scan3d_rebuild/work --output D:/Workspace/CodexTemp/scan3d_rebuild/output --vivado vivado
```

这是另一组新目录，不能复用第3步已存在的work目录。脚本恢复原源码并运行原 `build_final.tcl`，默认双通道，系统100MHz、视频125MHz。它只构建，不下载bit。

检查构建日志、DRC、CDC和布线后时序，再核对bit/HWH与清单。保存的历史整板报告为WNS +0.230ns、WHS +0.011ns、CDC Critical 0；本候选不把这些数字写成今天重新布局布线的结果。

## 6. 板端准备

先读 `board/readme.md`。核对板卡身份、当前程序所有者、供电与接线、可用空间和输出目录。用只读方式检查软件与固件清单是否配套。当前候选清单内两项runtime guard不匹配，必须先解决发布一致性，不能跳过加载器校验继续演示。

`board/boot_reference/`用于说明旧部署关系，包含装置相关路径，不是复制到新板就能直接覆盖的安装包。新板先完成无运动的静态图像/回放验证，再评估开机服务。

## 7. 启动与操作关系

恢复后的实时入口是 `runtime/r33/live.py`，真实PL回放入口是 `runtime/r33/replay.py`。先在对应运行环境查看参数：

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
