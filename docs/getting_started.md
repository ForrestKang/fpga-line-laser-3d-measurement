# 快速开始

先在电脑完成文件和软件检查，再进入RTL与硬件构建。下面命令从仓库根目录执行，不会连接或操作板卡。

## 1. 工具与依赖

| 用途 | 依赖 |
|---|---|
| 仓库与发布检查 | Python 3.10+，标准库 |
| ARM离线回归 | `sim/requirements_pc.txt`：NumPy、Pillow、SciPy、fonttools |
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
python -B sim/run_runtime_tests.py --work ../scan3d_work --output ../scan3d_pc_results
```

历史结果为69项中61通过、7失败、1跳过，详见[已知问题](known_issues.md)。保留新结果，不覆盖`sim/results/`中的旧证据。

## 5. RTL冒烟与完整构建

```powershell
python -B sim/run_rtl_integration.py --work ../scan3d_work --output ../scan3d_rtl_results --iverilog iverilog --vvp vvp --cases empty
python -B build/reproduce.py --work ../scan3d_build_work --output ../scan3d_build_output --vivado vivado
```

构建使用另一组新目录。`empty`只代表空帧冒烟；不指定用例前先阅读[sim说明](../sim/readme.md)。完整实现及板端步骤见[构建与部署](build_and_deploy.md)。
