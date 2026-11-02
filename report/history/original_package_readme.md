# 智能线激光三维测量系统：最终上板资源包（2026-11-01）

本目录未压缩。`1_board_image` 是从 PYNQ-Z2 板上逐文件下载的实际运行版本，每个文件的大小和 SHA256 都与板端核对过。其余目录是本机的源码、构建报告、标定和文档。全部文件的 SHA256 见 `MANIFEST_SHA256.json`。

## 板上运行链路（开机即运行）

开机后由 `/boot/boot.py` 读取 `/boot/scan3d_boot_config.json`，依次：

1. 加载 FPGA bit `deploy_seedretry_bin/firmware/final.bit`。
2. 以 systemd 单元 `scan3d-demo` 启动 `boot_rig2b_install/demo_service.py`。
3. 演示服务用 `release_rig2b`（即 `runtime_rig2b`）运行 `r33.live`，提供实时流、HDMI 三页界面、BTN1–3 翻页、BTN0 一次一圈扫描。

| 板上位置 | 本包位置 | 关键哈希 |
|---|---|---|
| `/boot/boot.py` | `1_board_image/boot/boot.py` | 319c51ce… |
| `/boot/scan3d_boot_config.json` | `1_board_image/boot/scan3d_boot_config.json` | cd0eb793… |
| `…/deploy_seedretry_bin/firmware/` | `1_board_image/deploy_seedretry_bin/firmware/` | bit db396f4c…，manifest f68532fd… |
| `…/btn0_full_scan_20261030_01/runtime_rig2b/` | `1_board_image/runtime_rig2b/` | live.py c856bed8…，pl_section.py 06562fc0… |
| `…/boot_rig2b_install/` | `1_board_image/boot_rig2b_install/` | demo_service.py 060d4812… |
| `…/boot_autostart_btn0/motor_standing.json` | `1_board_image/boot_autostart_btn0/` | 15e572d6…（BTN0 常设电机许可） |

板上根目录为 `/home/xilinx/scan3d_final_timingfix_20261030_01`（上表的"…"）。`release_rig2b/runtime` 是指向 `runtime_rig2b` 的符号链接。

## 版本内容

### FPGA

- 双通道、125 MHz 视频、PL 截面测量。
- 相机直通 PFO3 实时流。
- 同 run 种子重试（policy 3，仅全宽可用）。
- 时序：WNS +0.230 ns，WHS +0.011 ns，CDC Critical 0。
- 演示用 2× binning 单种子：同摆放 T02 对比中，测量点 65k 对 52k，PL 帧率 52.9 对 26.4 fps。

### 标定（`runtime_rig2b/calibration_r41/`，存档 `3_calibration/install_rig2_20261101/`）

- 2026-11-01 装置变动后按 T02 重拟合，并用 T01 台阶块独立核对。
- 激光平面：比例 1.004，法向变 2.0°，d = −81.117。
- 转轴：位置移 9.65 mm，倾斜 5.4°；每圈 3547 步。
- T02 三段误差在 ±0.07 mm 以内。
- T01 实测：台阶 2.21 / 3.09 / 4.99 mm（设计 2 / 3 / 5），总高 48.76（设计 48）。

### 运行时修复

实时模式下 PL 截面窗口与本帧不一致时，这一帧改由 ARM 计算，不再中断整次运行。

## 目录

| 目录 | 内容 |
|---|---|
| `1_board_image/` | 板上实际运行文件（上表） |
| `2_source_and_build/` | RTL、BD、约束、构建脚本、vendor 依赖、boot 安装脚本、板端脚本 |
|  | `build_seedretry/`：时序与利用率报告、准入记录、构建日志 |
| `3_calibration/` | 现用标定 `install_rig2_20261101`；历史标定 `install_t02_rotation` / `install_t02` / 棋盘格 `install` |
|  | `validation/`：T01、T02 核对结果和拟合文件 |
| `4_docs/` | 本轮报告 `REPORT_SEEDRETRY_20261031.md`、`REPORT_JOINT_CAL_20261031.md`，设计文档 `docs/` |
| `5_scan_results_20261031_previous_calibration/` | 10-31 用旧标定做的扫描模型与精度记录（装置变动前） |

## 重新构建与安装

### 构建 bit

在 `2_source_and_build` 中运行：

```
vivado -mode batch -source scripts/build_final.tcl -tclargs <新输出目录> 2 0 0 0 0
```

- 需要 Vivado 2025.2。
- 若流程内审查误拒，参照 `scripts/finalize_seedretry.tcl`，从已布线 dcp 出 bit。

### 安装到板

板上以 sudo 运行：

```
install_boot.py --mode auto_demo --deploy <firmware> --release <release> --live-stream --motor-standing <motor_standing.json> --authority <authority.json> --apply
```

- `install_boot.py` 在 `1_board_image/boot_rig2b_install/`。
- 安装前会先备份原 `/boot/boot.py`。
- 恢复用 `restore_boot.py`。

## 当前状态提示（2026-11-01）

- 板子约 11 小时前断电重启过（boot 7f2c43fc）。bit 已按 /boot 正常加载，但相机 SCCB 无响应（"SCCB reg 0x0 failed"），所以演示没有运行。
- 以往的处理方法：断电、重新插好相机排线、再上电。不要带电插拔。
- 本包内容与此无关，/boot 配置未变。
