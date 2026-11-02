# 离线验证入口与证据

所有入口均不连接板卡、不执行 FPGA 加载、相机或电机动作。先恢复原文件名：

```text
python build/restore_sources.py --output <new_work>
python sim/run_runtime_tests.py --work <new_work> --output <new_pc_results>
python sim/run_rtl_integration.py --work <new_work> --output <new_rtl_results> --iverilog <iverilog> --vvp <vvp> --cases empty
```

第三条使用 Icarus Verilog。未指定 --cases 时保留原脚本默认用例；需较长仿真时间。
路径适配只发生在恢复后的新工作目录，原验证脚本与断言保持在提交包内。
包内历史验证输入和冻结期望来自 sim/support，source_map.json 记录原路径、SHA 与恢复位置。

本次实跑：原 ARM 回归 69 项中 7 失败、1 跳过；空帧 RTL 冒烟 PASS。
完整原有 RTL 用例本次未全跑；历史报告不是本轮重新执行结果。
运行结果、控制台日志和固件/runtime 配套差异都在 results/。
