# 最终部署与 60 秒静态运行一致性审查

审查日期：2026-10-30。范围：`scripts/package_final.py`、`board/load_final.py`、`board/static_camera60.py`、`scripts/summarize_static_final.py`，以及用于核对参数和清理行为的当前 `runtime/r33/live.py`、原生库加载器。此次只读源码和本地文件，没有连接板卡、加载固件或运行相机；没有修改上述源码。

## 结论

按下述路径和运行环境调用，当前源码中未发现必然阻断正常 60 秒静态运行的 CLI 或截面 ABI 冲突。仍需真实板端核验：提取后的 ARM 原生库可加载、实际 PL ABI、相机信号准入和最后停止读回。静态审查不能替代这些结果。

**必须正确区分 loader 与相机输出目录。** Loader 使用固定的父目录 `data`；相机包装器使用随发布包位置变化的 `release01/data`。另外，源码未自行设置 `XILINX_XRT`，调用端必须显式提供本机已核验所需的 `XILINX_XRT=/usr` 和 PYNQ venv。不能直接用普通系统 Python 代表该环境。

| 项目 | 当前行为与影响 | 是否阻断正常运行 |
| --- | --- | --- |
| Loader 输出路径 | `board/load_final.py:12,83–88` 固定限制在 `/home/xilinx/scan3d_final_20261029/data`。给 `release01/data` 会在加载前报 `outside new candidate` | 命令写错会阻断；按本文调用可避免 |
| 静态输出路径 | `board/static_camera60.py:12,47` 的 ROOT 跟随脚本。从 release01 执行时只接受 `release01/data/<新编号>` | 命令写错会阻断；与 loader 不是同一路径规则 |
| XRT / venv | 包装器继承环境并使用 `sys.executable` 启动 live；不自动设置 XRT | 缺少本机必要环境可能直接失败；须在外层明确设置 |
| 已有 PL 准入 | Loader 在 `Overlay(download=True)` 前要求原硬件读出 ABI `(0x57334650,0x35000,31)` 且不 busy | 空 PL、不同 ABI 或 busy 会拒绝；不是“任何重启状态都能恢复”的加载器 |
| ARM 原生库 | 打包复制 `.so`，但打包校验与加载 bit 均不等于板端已成功 `ctypes.CDLL` | 损坏、架构或依赖不兼容会使后续 live 失败；应先做非硬件导入测试 |
| 180 秒 | 包装器到时创建 `stop.request`，不是杀进程或硬截止 | 正常 60 秒不受影响；硬件/内核阻塞时不保证 180 秒内退出 |

## 正确的部署位置与调用关系

本地 `board/deploy_final` 是目录内容；按安全提取工具放入板端 `/home/xilinx/scan3d_final_20261029/release01` 后应为：

```text
/home/xilinx/scan3d_final_20261029/
  data/<新的加载回执编号>/
  transfer01/                         # ZIP、传输清单、提取回执
  release01/
    board/load_final.py
    board/static_camera60.py
    deploy/final/final.bit
    deploy/final/final.hwh
    deploy/final/manifest.json
    deploy/final/p2_board.json
    runtime/...
    data/<新的静态运行编号>/
    data/<新的静态运行编号>_monitor/
```

加载 proof 可以放在固定父 ROOT 之内，但必须绑定当次 `machine_id`、`boot_id`、最终 manifest SHA 和**加载回执目录末级名称**。manifest 路径、runtime guards、bit/HWH 的同目录同 basename 校验彼此匹配；live 的传输准入还会要求当前 `PL.bitfile_name` 指向同一份 release01 bit。

下列是供主代理填入真实 proof、boot 和新编号的调用模板，不是本次实际执行记录：

```bash
sudo env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 -B \
  /home/xilinx/scan3d_final_20261029/release01/board/load_final.py \
  --manifest /home/xilinx/scan3d_final_20261029/release01/deploy/final/manifest.json \
  --proof /home/xilinx/scan3d_final_20261029/<本次proof>.json \
  --output /home/xilinx/scan3d_final_20261029/data/<新的加载编号>

sudo env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 -B \
  /home/xilinx/scan3d_final_20261029/release01/board/static_camera60.py \
  --expected-boot <刚核实的boot_id> \
  --output /home/xilinx/scan3d_final_20261029/release01/data/<新的静态运行编号>
```

不要先创建 static 运行目录，live 会以排他方式创建。包装器另建 `_monitor` 兄弟目录，里面有 `camera.log`、`RESOURCE_MONITOR.json`。调用端传输超时不能被当作远端已退出的证据。

## 打包与原生库

`package_final.py` 要求实际构建报告通过、综合源 SHA 不变、bit/HWH 存在，然后创建全新的 `board/deploy_final`。既有目录不会覆盖。bit、HWH、P2 合同、manifest、runtime 和两个板端脚本的相对结构正确；manifest guards 包含当前 live、transport、scope、display、section 和 page_state。

目前本地确有 `runtime/r33/native/libassemble_r33.so`（7,360 B）和 `libmarkers_r34.so`（11,880 B），ELF class=1、e_machine=40，即 ARM32。复制整棵 runtime 会保留二者及 marker 的 `.so.json` 构建回执。这只证明本地内容/架构标识，不证明板端动态链接成功。

提取后的建议非硬件检查为：在 PYNQ venv 中把 release01/runtime 放入 PYTHONPATH，调用 `r33.assemble_native.load_native()`，检查 `r33_body_abi()==0x330001` 并记录返回的库 SHA；再调用 `r33.markers_native_r34.backend_status()`，记录 available、machine、SHA。这些函数只加载文件和 C 库，不访问 MMIO。assembler 缺失时正常路径会拒绝，不静默回退；marker 为 auto，但存在而构建回执不匹配会抛异常，因此必须一起检查。不要以“两份文件在”替代该板端检查。

## 60 秒配置和截面 ABI

包装器参数均存在于当前 live CLI，PC `python -B -m r33.live --help` 返回 0。当前实际配置是：60 秒、保存每个已处理帧 Raw、PL section、A/B/C 三页、初始 A、每 10 秒软件切页、pipeline 默认开启、页面重画默认 1 秒。B 在页面集合内会建立 ProfileConfig，因此 `--section-backend pl` 不会因缺 `--profile-mode` 而被忽略。

本次没有 `--allow-motor`、电机 authority、激光控制或自动加载选项。live 会按现有相机驱动做复位释放、SCCB/PCLK/FV/LV 准入与采集，因此它仍是实际相机操作，不能描述为只读测试。当前没有显式启用 PL 自动曝光或额外手动曝光流程。

manifest 要求 `section_measure.present=true`、窗口 0x700–0x7FF、magic 0x5345434D、version=1。硬件 0x704 是**控制寄存器**，复位值 2（use_preview=1、enable=0），不是版本寄存器。Loader 的比较正确；不能改成读 1。live 预热帧的 section 结果先单独读取记录，再启动正式 capture，保持正式首帧身份检查。

空间准入是当前文件系统可用字节至少 6,459,228,160，不是 GiB。运行中低于 2 GiB+64 MiB 会请求停止，保留已在途数据所需余量。由于逐帧 SD 写入和驱动调用仍可能阻塞，该软停止不是无限停顿情况下的保证。

## 停止、清理和可证明范围

live 在期限或 stop.request 后停止接收，给 capture/display 线程各最多 5 秒 join。线程残留会写 `threads_alive_after_join`、错误并返回 1；cleanup 异常也会使 phase=failed，不能保留 stopped。

最终 `summary.json` 中：

- `owner_close_results` 表示 close 是否实际调用、是否成功；仅有 success 不独立证明停机。
- `camera_stop_evidence` 保存已有 recovery/DMA stop 证据，以及 close 后新读出的 status/ready；`capture_quiescent=true` 还要求 capture/DMA 的 stop/reset 证据满足条件。
- `hardware_quiescent` 还要求无残留线程、无 cleanup 失败、PFO3 close 成功。
- `sensor_stream_stopped=false` 明确表示没有证明传感器进入停止输出/待机；当前证据范围是 PL capture gate 与 capture DMA。
- `automatic_retry=false`，无法确认停机时不得自动再试。文件描述符锁会随清理或进程退出释放，不能宣称一个已经退出的进程仍持有锁保护硬件。

因此正常完成报告应检查 phase、退出码、线程、cleanup、capture/hardware quiescent 和读回证据，不能只看“调用了 stop”或 wrapper 退出。包装器 RESOURCE_MONITOR 直接返回 child 退出码，归档后汇总会呈现 shutdown 字段。

## 指标真实性与报告边界

`summarize_static_final.py` 读取归档的运行目录与 `_monitor`，输出必须位于二者之外且不能覆盖已有文件。

| 字段/检查 | 可以说明 | 不能据此说明 |
| --- | --- | --- |
| processed_hz | 从 formal capture 开始计算完整、不重叠 5 秒窗口，事件为 PFO3 返回时间 | 不是 ARM 截面/UI/保存完成率，也不是持久化或非空 XYZ 速率 |
| new display Hz | 同一窗口里不同正式 frame ID 的 VDMA ACK，重复显示同帧不计 | 不是每次都有新三维点；激光关闭时也可能是正常新原图 |
| capture/ready→ACK | 同一 monotonic 轴上的画面年龄 P50/P95/max | 不是曝光中点到物理显示器出光的延迟 |
| UI render_ms | 全局及 A/B/C 已携带到显示事件的重画耗时 | 被替换快照、缓存复用没有对应重画样本；不是所有候选重画的完整总体 |
| CPU/内存 | 两核总容量 100% 的整板 busy、iowait；活动区间重叠加权与采样可用内存 | 不是进程 CPU，采样最低值不是连续内存下界保证 |
| Raw/primary | 每个已处理帧 Raw=752×480 且 stats SHA 相同；primary 长度匹配并另算 SHA | 没有对 primary 做正式语义解码；不证明 fsync/崩溃恢复或所有相机帧都保存 |
| 页面/快照 | 区分软件循环与 BTN 计数；核对 ARM RGB 回读尺寸、SHA及 framebuffer_equal | 软件切页不是实体按键验证；ARM RGB 不包含最终 PL HDMI 叠加层 |

`source_checks_ok=true` 是证据文件相互一致，不是所有验收指标通过。脚本没有把完整 ABC、hardware_quiescent、10 Hz、5 Hz、半秒年龄、128 MiB 或持久化都作为总门槛。报告需单独列出实际值和对应判定。0 XYZ 本身不会使相机/切页/CPU 汇总失败；相机信号和保存错误才应独立归因，不能把激光未开当作相机故障。

## 本次实际检查

- 四个指定脚本 AST 解析通过。
- 当前 live CLI 帮助实际执行返回 0，静态包装器使用的选项均存在。
- 本地两份 `.so` 的文件长度、ELF class 和 machine 已读回检查。
- 已核对包路径、loader/static 的不同输出规则、runtime 配置、清理代码和汇总公式。
- 未执行打包（它会生成排他产物）、板端 CDLL 检查、上传、加载、相机或 HDMI。以上相关实际结果由主代理执行后提供。
