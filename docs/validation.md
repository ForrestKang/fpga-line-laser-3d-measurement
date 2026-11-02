# 测试与测量结果

## 软件与RTL测试

| 项目 | 结果 | 记录 |
|---|---|---|
| 完整运行时 | 279项通过，零失败、零跳过 | [运行时回归](../report/verification/runtime_complete.json) |
| 入口与三页界面 | 71项通过 | [界面回归](../report/verification/runtime_original_scope.json) |
| 软件固件配套 | 11项通过，覆盖27份数据包 | [配套测试](../report/verification/pairing_test_summary.json) |
| 截面测量RTL | 195包逐位一致，包括182帧采集数据和13组合成输入 | [截面结果](../report/verification/section_rtl.json) |
| 整链RTL | 19例全尺寸输入通过 | [整链结果](../report/verification/full_rtl.json) |

71项入口与界面检查属于完整运行时测试范围，单独执行用于复核。验证汇总见[release_validation.json](../report/release_validation.json)。

## 整板实现

| 项目 | 实现结果 |
|---|---:|
| LUT | 39,111，73.52% |
| FF | 50,902，47.84% |
| BRAM | 78.93% |
| DSP | 134，60.91% |
| WNS / WHS | +0.230 ns / +0.011 ns |
| CDC Critical | 0 |
| 系统 / 视频时钟 | 100 / 125 MHz |

数据对应仓库提供的固件；完整报告见[实现记录](../build/evidence/)与[资源时序汇总](../report/source_report_gate.json)。

## 实物测量

系统展示样件为台阶块、阶梯圆柱和波纹花瓶。台阶块相邻高差为2、3、5 mm，截面界面读数为2.00、2.90、4.96 mm。阶梯圆柱下、中、上段直径为40、30、20 mm；报告中的扫描值分别为39.68、29.90、19.88 mm。花瓶颈部卡尺读数为16.5 mm，扫描结果为16.23～16.55 mm。

阶梯圆柱在调试中参与过参数调整，其尺寸结果用于复核；花瓶作为独立打印件进行尺寸对照。具体场景、标定过程和图像见[技术报告](../report/design_report.pdf)。

PL逐帧图像处理约26.5帧/s；整圈扫描示例采集200个视角，用时约2.5分钟，获得约6.6万个测量点。图像处理速度、转台扫描时间、显示更新和持久化速度分别统计。

## 复现测试

在仓库根目录执行：

```text
python -B tools/check_repository.py
python -B skill/verify_bundle.py .
python -B build/restore_sources.py --output ../scan3d_work
python -m pip install -r sim/requirements_pc.txt
python -B sim/run_release_tests.py --work ../scan3d_work --output ../scan3d_pc_results --cc gcc
python -B sim/test_release_pairing.py
python -B sim/run_section_rtl.py --work ../scan3d_work --output ../scan3d_section_results --iverilog iverilog --vvp vvp --workers 2
```

完整19例RTL命令见[仿真说明](../sim/readme.md)，完整Vivado工程构建见[构建部署](build_and_deploy.md)。

测试核对候选与亚像素中心、分支和拒绝原因、几何参数身份、Q16坐标、截面统计及持久化边界，并覆盖包损坏、重复或缺失记录、缓冲复用、取消、写盘失败和进程退出。
