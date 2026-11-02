# 构建与部署

## 从源码构建

```powershell
python -B build/reproduce.py --work ../scan3d_build_work --output ../scan3d_build_output --vivado vivado
```

安装Vivado 2025.2与XC7Z020器件支持，并让命令可用。恢复器从`source_map.json`恢复原路径，再调用`fpga/scripts/build_final.tcl`。默认2路，四个dispatch/banking开关均为0；不静默升级IP。

`--work`及`--output`必须是新目录。历史`finalize_seedretry.tcl`依赖未随包提供的旧route.dcp，不能用它代替完整源码构建入口。

构建输出包括日志、综合与布局布线报告、资源、setup/hold、脉宽、DRC和CDC结果，以及配套bit/HWH和manifest。

## 固件与ARM组合检查

```powershell
python -B skill/pynq_release_check.py --manifest board/firmware/manifest.json --runtime-root src/ps
```

当前manifest绑定bit/HWH和运行程序，配套信息见[运行时记录](../board/firmware/runtime_validation.json)。部署前核对文件、原生库架构及设备ABI。

配套检查覆盖协议、寄存器接口和ARM原生库架构，测试结果见[验证汇总](../report/release_validation.json)。电脑与ARM分别构建对应架构的动态库。

## 板端安装前

1. 核对PYNQ-Z2身份、启动标识、硬件所有者、供电和实际接线。
2. 确认输出空间、备份和原版本回退路径。
3. 核对当前装置内参、光平面、转轴、实际转角与工作体积。
4. 检查ARM原生库架构和运行时依赖。
5. 完成固件与软件绑定检查后，按当前现场授权加载并读回ABI。

本仓库不带通用SD系统镜像。`board/boot_reference/`是装置专用的历史启动源码，包含旧自动演示配置；阅读它理解依赖，不要直接覆盖另一块板的`/boot`。

## 查询真实运行入口

在恢复后的runtime目录：

```powershell
cd ../scan3d_work/runtime
python -B -m r33.live --help
python -B -m r33.replay --help
```

`--help`用于查看当前源码支持的参数。实际采集、回灌、相机设置和运动均会改变硬件状态，不能当作只读文件检查。这里不提供绕过现场前置条件的一键自动转动命令。

部署后分别验证静态采集、真实PL回灌、HDMI、完整保存和受控运动。设备专用标定及历史运动许可不随仓库转移。
