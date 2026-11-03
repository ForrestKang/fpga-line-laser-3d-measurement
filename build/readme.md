# 用Vivado重新生成固件

## 需要的环境

安装Vivado 2025.2及XC7Z020器件支持，并使`vivado`可从终端调用；Python应可从同一终端运行。设计源码、约束、BD/IP重建脚本已经放在工程包中，生成缓存由本机工具创建。

## 执行步骤

1. 解压工程包，在含`src/`、`sim/`和`build/`的根目录打开终端。
2. 选择两个尚不存在的目录：一个存放恢复后的源码，一个存放本次Vivado工程和构建结果。
3. 执行：

```powershell
python -B build/reproduce.py --work ../scan3d_build_work --output ../scan3d_build_output --vivado vivado
```

`reproduce.py`先恢复源码，再调用`fpga/scripts/build_final.tcl`，依次重建工程、综合、布局布线、输出报告并检查实现门槛。默认双方向通道，不修改运行算法或提高时钟要求。

## 到哪里看结果

- `scan3d_build_work/`：恢复后的`fpga/`、`runtime/`、`firmware/`及验证依赖。
- `scan3d_build_output/reports/`：本次综合资源、实现资源、setup/hold、脉宽、DRC、CDC和功耗等报告。
- `scan3d_build_output/artifacts/final.bit`：通过实现检查后生成的bit文件；检查失败时保留错误和报告，不生成通过版本。
- 工程包`board/firmware/`：随包提供的配套bit、HWH和manifest，可按[板端说明](../board/readme.md)核对。

`build/evidence/`保留已有构建报告和前后对比数据，便于与新构建比较。完整构建使用本页的`reproduce.py`入口；它不依赖旧的route.dcp，不会给板卡加载固件。
