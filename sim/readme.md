# 离线验证入口与证据

所有入口均不连接板卡、不执行 FPGA 加载、相机或电机动作。先恢复原文件名：

```text
python build/restore_sources.py --output <new_work>
python -m pip install -r sim/requirements_pc.txt
python sim/run_release_tests.py --work <new_work> --output <new_pc_results> --cc gcc
python sim/run_rtl_integration.py --work <new_work> --output <new_rtl_results> --iverilog <iverilog> --vvp <vvp> --cases empty
```

软件入口先用本机C编译器构建辅助库，再运行全部打包的runtime测试模块。依赖包括OpenCV、NumPy、Pillow、SciPy、fonttools。主机DLL/共享库只用于电脑测试，不是板端ARM库；具体运行版本和结果随输出保存。

RTL入口使用Icarus Verilog。包装器不传`--cases`时只跑空帧；完整19例需显式指定：

```text
python sim/run_rtl_integration.py --work <new_work> --output <new_matrix_results> --iverilog <iverilog> --vvp <vvp> --cases T05_000 T06_000 empty saturated noise slots17 mix_ovf_mid mix_ovf_edge live03_240 live02_180 T05_000:bin T06_000:bin empty:bin saturated:bin noise:bin slots17:bin mix_ovf_mid:bin live03_240:bin live02_180:bin
```

命令中的测试用例名是内部输入文件索引。各次RTL入口在恢复目录创建派生脚本，另一轮请使用新的恢复目录和输出路径。

截面单独使用182帧历史原图及13个合成输入，与整数模型逐位核对195包：

```text
python sim/run_section_rtl.py --work <new_work> --output <new_section_results> --iverilog <iverilog> --vvp <vvp> --workers 2
```

该入口先检查原图尺寸、包数和输入/源文件身份，再运行原截面验证脚本。`--validate-inputs-only`仅检查输入；不能当作RTL通过。离线配套检查可运行`python -B sim/test_release_pairing.py`，它不访问板卡。
路径适配只发生在恢复后的新工作目录，原验证脚本与断言保持在提交包内。
包内历史验证输入和冻结期望来自 sim/support，source_map.json 记录源文件与恢复位置。

运行时279项、配套11项、截面195包与整链19例测试结果见[验证汇总](../report/release_validation.json)。
