# P2 实板使用入口

先启动专用MicroSD上的官方PYNQ-Z2镜像，PYNQ-Z2 的上排 JP1 拨到 SD，下排 PLL/JTAG 不用于选择 SD。JTAG相机诊断与PYNQ Overlay不得交替覆盖同一次实验。记录串口115200启动日志、SSH/Jupyter访问及实际版本。本次直连实测板卡为192.168.2.99，电脑网口为192.168.2.100/24；不要操作另一块Type-C/JTAG Zynq7020。

下载脚本只下载和检验ZIP，不写SD：`python board/scripts/download_pynq_image.py`。本地SHA-256用于字节追踪，不冒充AMD发布的校验值。烧录时先核对SD容量/身份并备份，再按官方指南操作。

板上先运行：

```bash
python board/scripts/verify_p2_board.py --stage runtime --config board/configs/p2_manual_wiring.yaml
```

默认命令只读取运行环境，不加载bitstream，也不执行allocate；退出码2表示内存检查尚未完成。仅在外部PL接口全部未连接且已确认时，完整运行环境检查使用：

```bash
sudo env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 \
  board/scripts/verify_p2_board.py --stage runtime \
  --config board/configs/p2_manual_wiring.yaml \
  --initialize-official-base --external-ports-unconnected-required
```

`XILINX_XRT=/usr`来自本次实际镜像的`/etc/profile.d/xrt_setup.sh`。非登录SSH和sudo可能不保留它；其他镜像先读取自己的profile，不猜路径。官方base加载、allocate和读回必须在同一Python进程中完成。接好相机后，不运行带base参数的runtime命令；camera/raw/profile入口先校验、加载对应custom Overlay，再分配DMA缓冲。真实transport失败不会切到Mock。

2026-10-09已取得SD完整回读、COM3启动日志、SSH、Jupyter登录页及32KB DDR allocate/readback实测，证据在`board/results/p2/runtime/runtime_review.json`。当前runner的显式base流程也已在板上重跑通过。交互式notebook执行、相机与DMA采集仍未测试。没有相机供电/接线证据时不要启用capture。

正式主机软件证据为`sim/results/p2/check_host.json`：`passed=true`、`p2_host_verified=true`，162 PASS；对应`sim/results/p2/post_host_audit.xml`同为162项通过。审计已覆盖 DMA 完成后的 PL `scan_done`、buffer ownership、成对 bit/HWH 门禁和 runtime VERSION/CAPABILITIES readback。它不代表真实相机或 custom DMA 已通过。`camera_control_io.txt`另有单独相机复位、SYS不复位时的PCLK稳定重测仿真证据，仍不代表实物相机已通过。

当前 `check_preboard` 已实际返回 0，`p2_preboard_verified=true`。两套 final Overlay 均为 Vivado 2025.2、`xc7z020clg400-1` 实际产物，bit/HWH 同名同目录，manifest 为 `preboard_passed=true`；但 `hardware_confirmed=false`，相机 Pin1/接线、Gate A、Raw DMA 和 Profile DMA 仍未测试。CDC 逐路径签核和 DRC 保留项见 `report/p2_cdc_signoff.json` 与 `build/reports/p2/{camera_raw,structured_light_profile}/`，不要把原始 Critical 或 DRC findings 写成全零。

## 人工接线合同（候选，断电连接）

2026-10-09用户读取实物印字为 `ATK MT9V034 V1.5`，并表示接口与V1.4相同。本地现有原理图 `MT9V034_V1.4.pdf` 显示P1排针9×2；下表仍是以V1.4资料核对的候选接线，尚无V1.5原理图或实物逐针检查证据。排针方向必须对照实物pin1确认，不从商品正面照片推断。sensor DOUT2..9引出到8bit数据接口，未引出的DOUT0/1不补位。

| 模块排针 | 信号 | PYNQ接口 | FPGA |
|---:|---|---|---|
| 1 | GND | J7 GND | — |
| 2 | VCC3.3 | J7 3V3 | — |
| 3 | SCL | Arduino SCL | P15 |
| 4 | VSYNC/FV | AR9 | V18 |
| 5 | SDA | Arduino SDA | P16 |
| 6 | HSYNC/LV | AR8 | V17 |
| 7 | D2 → gray0 | AR0 | T14 |
| 8 | RST/RESET_BAR | AR10 | T16 |
| 9 | D4 → gray2 | AR2 | U13 |
| 10 | D3 → gray1 | AR1 | U12 |
| 11 | D6 → gray4 | AR4 | V15 |
| 12 | D5 → gray3 | AR3 | V13 |
| 13 | D8 → gray6 | AR6 | R16 |
| 14 | D7 → gray5 | AR5 | T15 |
| 15 | PCLK | PMODA pin7 | U18 |
| 16 | D9 → gray7 | AR7 | U17 |
| 17 | STB | AR11 | R17 |
| 18 | EXP | 本阶段不接 | — |

SDA/SCL开漏，资料上拉到模块3.3V。ID用7bit地址0x48读寄存器0x00，预期0x1324。读ID成功前不批量写模式寄存器。相机供电、RST/STB、空闲SCCB、PCLK、FV/LV极性、行/帧大小及数据变化都需实际留档。

完成接线确认后复制pending YAML为独立run配置，填写physical_revision、pin_mapping_confirmed、bitfile/hwh/manifest，再执行camera阶段。camera probe只读取ID/窗口/时钟，不自动宣布Gate A完整通过；Gate A资料不完整时入口以退出码2结束，不能以部分探测结果推进状态。

下一步先确认相机排针 Pin1 方向，再断电接线完成 Gate A。不要混用另一块板，也不要在本阶段加入电机算法。部署包 `build/toolchain/releases/p2_board_bundle.zip` 已真实生成，包含 67 个文件（最终压缩尺寸与 SHA-256 由打包命令输出），ZIP CRC 与每个 member SHA 回读通过；当前尚未部署到 PYNQ-Z2，也未加载 custom Overlay。解压后保持 bit/HWH 同名同目录，`p2_manual_wiring.yaml` 默认 pending，不允许直接 load。

Gate A全部测量记录通过后，配置填写hardware_confirmed、width/height、pclk_hz、fv_active/lv_active、frame_period_s、gate_a_evidence、gate_a_sha256及匹配bit/hwh路径。Gate A JSON须含`status=camera_input_verified`并附实际波形/测量文件路径和指纹。不要把模板改成PASS后代替测量。

```bash
python board/scripts/verify_p2_board.py --stage raw --config <confirmed_config>
python board/scripts/verify_p2_board.py --stage profile --config <confirmed_config>
```

两者默认1000次，失败立即保留并退出，不用重试掩盖错误。每次运行独立run_id。恢复和真实停钟另需20次故障操作及波形记录；普通批次成功不表示这些已测。

## Overlay 门禁与失败边界

`verify_p2_board.py` 在 `camera`、`raw`、`profile` 和 `recovery` 阶段加载
Overlay 之前，先读取 `bitfile`、`hwh` 和 manifest，并逐项核对：

- `xc7z020clg400-1`、regmap/diagnostic/profile 版本、contract SHA-256 和源码绑定；
- bitstream 与 HWH 必须位于同一目录且使用相同基名，保证 PYNQ `Overlay(bitfile)` 找到配套硬件描述；
- HWH 的 `SYSTEMINFO`、`FRAME_WIDTH/HEIGHT`、`PROFILE_ENABLE` 与确认配置；
- `M_AXI_GP0` 到 control、diagnostics、raw DMA（以及 Profile DMA）的精确地址和接口；
- 两路 DMA 的 simple S2MM、32-bit AXIS、64-bit memory、20-bit length 和 DRE/SG/MM2S 设置。

任何文件缺失、hash 不匹配、HWH 与 manifest 冲突或配置仍为 pending，都在
`Overlay(..., download=True)` 前失败。实板路径不会因为失败而自动切换到 Mock。
manifest 中的 `source_sha256` 是构建端源码 provenance；此入口检查其存在和 SHA-256
格式，但不会在只部署 Python、bit 和 HWH 的轻量包中声称重新计算了缺失的 RTL 源码。
完整发布包由 `build/scripts/p2/package_board.py` 生成，并包含 `src/rtl`、
`build/scripts/p2` 和 `board/constraints`，需要源码级复核时再运行对应的构建审计。
`raw` 批次只提交长度正确且 PL 已完成的原始帧；`profile` 批次还要求 Profile、诊断
快照、epoch、request ID 和同帧 Python/RTL 比对全部一致。屏幕上看到的 Mock/Replay
结果属于离线证据，不能替代 PYNQ-Z2、相机或测量精度实测。

Overlay 加载后、任何 START 之前，runner 还必须从实际 control/diagnostic MMIO 读回
runtime VERSION 与 CAPABILITIES，并核对 manifest、模式和帧几何；只读 HWH 或 manifest
不能代替 live readback。camera 阶段因此可能在 Overlay 已成功加载后仍以退出码2报告
`camera_input_verified` 未完成；raw/profile 不得绕过这个门禁。

入口自身采用有界等待和 `finally` 清理：即使 PynqTransport 或 P2Controller 构造失败，
已打开的 SessionRecorder 也会关闭；异常包、取消和恢复事件保留在 session 目录中。

## 离线 notebook

`board/notebooks/p2_capture.ipynb` 默认 `MODE = 'mock'`，展示同一灰度帧上的绿色
ROW 中心、质量表、逐字段比对、停止、保存和回放，不绘制三维点云。将 `MODE` 改成
`'real'` 只是显式选择真实路径；它要求 `CONFIG_PATH` 指向完成 Gate A 的确认 YAML，
先执行本脚本同样的 artifact/HWH/part/geometry/address/DMA 门禁，再构造 PYNQ
transport。没有确认配置或静态门禁失败时不会下载 bitstream。

参考： [官方镜像](https://www.pynq.io/index.html)、[Z2启动指南](https://pynq.readthedocs.io/en/v3.0.0/getting_started/pynq_z2_setup.html)、[DMA TLAST/长度](https://pynq.readthedocs.io/en/latest/pynq_libraries/dma.html)、[PG021实际接收长度](https://docs.amd.com/r/en-US/pg021_axi_dma/S2MM_LENGTH-S2MM-DMA-Buffer-Length-Register-Offset-58h)。
