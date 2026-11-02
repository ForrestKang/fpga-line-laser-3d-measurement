# 本次静态板测执行约定

范围到计划第 6.3 步：核验归档后清理非核心副本、通过离线/整板门槛后加载最终固件、60 秒相机与 A/B/C 页面测试。电机 12V 物理未接，不启用运动，不控制激光，不修改启动文件。

## 运行环境

板端命令使用 `/usr/local/share/pynq-venv/bin/python3`，并在该进程设置 `XILINX_XRT=/usr`。不能仅使用系统 Python，也不能假定非交互 SSH 会继承 XRT 环境。

本轮实际非硬件预检证明：缺少该变量时 PYNQ 报 `No Devices Found`；在同一 boot 设置后，可读取 PYNQ 固件元数据，记录指向现有 R43 p02。两次回执均保留：`evidence/board_loaded_identity04.json`（环境失败）、`evidence/board_loaded_identity05.json`（成功）。这不是新固件已加载的证明。

## 顺序与证据

1. 截面 195 包逐位一致、OOC WNS >= +0.5 ns、LUT <= 7000。
2. 最终源冻结后集成回归，整板实现满足计划时序/资源/CDC 门槛。
3. `scripts/package_final.py` 生成新部署目录，校验实际参与综合的源码 SHA。
4. 上传后以同一解释器验证原生库可加载；重新核对 machine、boot、所有者和空间。
5. 用户既有授权绑定本次 manifest 和 boot，执行一次显式加载；保存前后 ABI 与 bit/HWH SHA。
6. `board/static_camera60.py` 启动正式 `r33.live`，每 10 秒软件循环三页。软件切页不算实体按键验收。
7. 整目录下载运行与监测数据，逐文件 SHA 校验；`scripts/summarize_static_final.py` 从归档重新计算每 5 秒速度、逐页重画、显示年龄和 CPU。

## 结果边界

- 原图和 primary 保存覆盖已处理帧；采集最新帧队列的替换数量单独报告。
- 该静态入口的文件读回不能替代事务持久化/崩溃恢复验收。
- RGB 截图是 ARM 帧缓冲，不能证明最终 PL 覆盖层或物理显示器观感。
- 处理时刻是 PFO3 返回，不能混称包含保存和全部 UI 的整链完成时刻。
- 关闭需检查线程、owner 关闭和最新寄存器/DMA 读回。`capture_quiescent` 只指 PL 采集门与采集 DMA；不声称传感器断电或 SCCB 待机。
- 没有激光或没有有效 XYZ 时，仍可报告相机/切页/CPU，但不能据此声称完成物体测量。
