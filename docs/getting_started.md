# 快速开始

先在电脑完成文件和软件检查，再进入RTL与硬件构建。下面命令从仓库根目录执行，不会连接或操作板卡。

## 1. 工具与依赖

| 用途 | 依赖 |
|---|---|
| 仓库与发布检查 | Python 3.10+，标准库 |
| ARM离线回归 | `sim/requirements_pc.txt`：NumPy、Pillow、SciPy、fonttools、OpenCV；本机C编译器 |
| RTL集成 | Icarus Verilog（iverilog、vvp）及原脚本依赖 |
| 完整工程实现 | Vivado 2025.2，XC7Z020器件支持 |
| 真正板端运行 | PYNQ镜像与对应ARM/Python/PYNQ环境 |

PC依赖清单不是环境锁文件。记录实际Python、库和工具版本；不要把Windows构建的动态库复制给ARM使用。

## 2. 最小自检

```powershell
python -B tools/check_repository.py
python -B skill/verify_bundle.py .
python -B skill/test_pynq_release_check.py
```

第一项检查文档链接、必要文件与Python语法；第二项检查发布包文件内容；第三项检查通用发布工具的故障拒绝行为。它们都不证明业务回归或硬件已通过。

## 3. 恢复原文件名

提交目录统一英文小写，HDL、字体及脚本原名通过来源映射恢复：

```powershell
python -B build/restore_sources.py --output ../scan3d_work
```

新目录内包含`fpga/`、`runtime/`、`firmware/`等；集成脚本需要的相邻目录也由恢复器创建。输出必须不存在，避免覆盖已有工程。

## 4. 软件测试

```powershell
python -m venv ../scan3d_venv
# 激活自己的虚拟环境后：
python -m pip install -r sim/requirements_pc.txt
python -B sim/run_release_tests.py --work ../scan3d_work --output ../scan3d_pc_results --cc gcc
```

此入口编译本机原生辅助库并执行所有打包的runtime测试模块，包含存储、协议、故障、页面和几何黄金模型；OpenCV用于图像及显示测试。`--cc`可指定编译器路径。主机库不等于ARM库，也不证明板端执行。最终结果见[验证汇总](../report/release_validation.json)，原失败证据保留在`sim/results/`。

## 5. RTL冒烟与完整构建

```powershell
python -B sim/run_rtl_integration.py --work ../scan3d_work --output ../scan3d_rtl_results --iverilog iverilog --vvp vvp --cases empty
python -B sim/run_section_rtl.py --work ../scan3d_work --output ../scan3d_section_results --iverilog iverilog --vvp vvp --workers 2
python -B build/reproduce.py --work ../scan3d_build_work --output ../scan3d_build_output --vivado vivado
```

截面入口使用182帧随包历史原图和13包合成输入，核对195包寄存器与表字段；它不连接相机。完整19例整板RTL矩阵的显式参数见[sim说明](../sim/readme.md)，该包装器不指定`--cases`时只跑空帧。构建使用另一组新目录；本轮没有重新布局布线。完整实现及板端步骤见[构建与部署](build_and_deploy.md)。
