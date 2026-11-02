# 最终版：智能线激光三维测量系统

**整理日期：** 2026-10-30
**工作目录：** `D:\Workspace\CodexWork\scan3d_final_20261029`

本目录已合并另外两个目录的修改：

- `scan3d_final_timingfix_20261030_01`：前端时序修复、截面测量接入、整板实现；
- `scan3d_final_runtimefix_20261030_01`：Runtime04，Raw 缓冲复用、流水、页面重画提速。

被覆盖的旧文件备份在 `archive/pre_merge_20261030/`。

## 组成

| 部分 | 内容 | 身份 |
| --- | --- | --- |
| 固件 | `release/firmware/final.bit`、`final.hwh`、`manifest.json` | bit `228b4551…`，HWH `65626b16…`，manifest `5e58f7e3…`；与板上 `release02/deploy/final` 一致 |
| 软件 | `runtime/`，与板上 `release04/runtime` 逐文件相同 | `release/RUNTIME_MANIFEST.json`（`a8eae9aa…`） |
| 开机 | `boot/boot_loader.py`（装为 `/boot/boot.py`）、`demo_service.py`、`install_boot.py`、`restore_boot.py` | 见 `evidence/board/` 回执 |

源码一致性：`rtl/`、`scripts/`、`tests/` 与 timingfix 目录无差异；`runtime/` 与 Runtime04 部署包无差异。

## 固件（整板实现）

| 项目 | 结果 |
| --- | --- |
| 配置 | 2 路方向核，系统 100 MHz，视频 125 MHz；PFO3 + 合并模式 + PL 自动曝光 + PL 测量（0x500）+ 按键（0x600）+ 截面测量（0x700）+ 点云视图 + 电机控制 / 守护 |
| 时序 | WNS +0.343 ns，WHS +0.015 ns，TNS / THS / TPWS 均为 0，Critical CDC 0，布线错误 0 |
| 资源 | LUT 72.88%，FF 47.10%，BRAM 78.93%，DSP 60.91%；均未超过 80% |
| 截面测量 | 定点模型与浮点版在 182 帧实拍上一致；RTL 与模型在 195 个包上逐位一致；集成回归 19 + 4 项通过 |

## PC 回归（合并后）

| 测试套件 | 结果 |
| --- | --- |
| `run_tests_r42.py` | 57 项通过 |
| `run_tests_final_section.py` | 68 项通过 |
| CMA 复用、退出、页面重画、页面 | 23 项通过 |
| `boot/tests_boot.py` | 9 项通过 |

## 板上实测（Runtime04，同一固件，60 s，不接电机）

| 指标 | 串行 | 流水 |
| --- | ---: | ---: |
| 处理速度（帧/秒） | 1.65 | 2.04 |
| 画面年龄 p95（ms） | 711 | 967 |
| A/B/C 页重画 p50（ms） | 248 / 183 / 83 | 253 / 192 / 91 |
| 双核 CPU 占用 | 48.6% | 57.7% |

关于 PS 端占用：CPU 占用从早先 R43 回灌的约 95% 降到约 58%，但这是在每秒只处理约 2 帧的情况下测得的，不是同等吞吐下的节省。

- 慢的主要原因：当前实拍场景每帧约一万条诊断候选，primary 包约 0.95 MB，ARM 端解包和校验耗时；再加上页面重画约 0.1–0.25 s。
- 10 帧/秒、画面年龄 ≤ 0.5 s 的实时目标**未达到**。
- 当前场景的测量级 XYZ 为 0，不能据此声明三维吞吐或毫米精度。

## 开机自动演示

- `/boot/boot.py`（即 `boot_loader.py`）的流程：
  - 核对 manifest、bit、HWH 的哈希，通过后加载固件；
  - 再用 `systemd-run` 启动临时单元 `scan3d-demo`；
  - 任何核对失败都改为加载 PYNQ base，不启动演示。
- `demo_service.py`：
  - 循环运行 `r33.live`，每轮 30 min；参数为流水模式、截面用 PL 后端、三页、初始实时页，不保存原图；
  - 早期失败按 10 s、20 s…最多 300 s 退避重试；
  - 只保留最近 3 轮演示目录；剩余空间低于 2.3 GB 时不启动，低于 2.06 GB 时请求当前轮停止。
- **电机**：开机演示永不带 `--allow-motor`，BTN0 只显示未授权。12V 授权必须每次由用户现场确认，不能预先写进开机流程。
- **停止演示**：`sudo systemctl stop scan3d-demo`。
- **恢复原 boot.py**：用安装回执中的备份路径和 SHA 运行 `restore_boot.py --apply`。

## 固化验收（2026-10-30）

| 步骤 | 结果 | 回执 |
| --- | --- | --- |
| 30 min 稳定性测试 | 两轮各约 15.6 min，均正常退出；中途自动重启正常；帧率 2.38 / 2.54，CPU 59–60%，最低可用内存 278 MiB | `evidence/board/demo_soak_30min.json` |
| 安装 | `/boot/boot.py` = `cdd421e1…`，配置 `cb58bc18…`；原文件备份 `/boot/boot.py.orig_20261028_090201_3ecac088`（`b2653ef8…`） | `evidence/board/install_apply.json`、`BOOT_INSTALL_AUTHORITY.json` |
| 重启验证 | 新 boot_id `80aa76fb…`。系统日志依次记录 SCAN3D_BOOT `loaded`（final.bit）和 `demo_started`；`scan3d-demo` 为 active，重启次数 0；live 运行中，实时页已出帧 | `evidence/board/post_reboot_check.json`、`LOAD_AUTHORITY_REBOOT.json` |

**BTN0 直接扫描（2026-10-30 用户改定）**：12V 独立供电，用户给出常设许可（`evidence/board/MOTOR_STANDING_PERMISSION.json`，原话已记录）。

- 开机配置固定了这份许可文件的哈希，演示服务据此为每一轮生成本轮电机授权。
- 按一次 BTN0 扫一圈（有界，3200 步，期限 1200 s），扫描中再按一次停止；演示自身从不主动转动电机。
- 扫描结束 90 s 后自动开新一轮，之后 BTN0 可再次扫描。
- 安装后 `/boot/boot.py` = `81bd9418…`，配置 `8b638c62…`；上一版备份 `/boot/boot.py.orig_20261028_093119_80aa76fb`。
- 已重启演示单元：live 带 `--allow-motor` 运行，电机控制和守护初始化无错误，状态为 “BTN0 ready”。我没有发出任何运动命令。
- 上一轮日志记录了 5 次实体按键切页（BTN1/2/3），来源为 physical_button。

板上系统时钟比实际日期慢约 2 天（显示 2026-10-28），演示目录名按板上时间生成。

HDMI 画面和物理按键切页需要现场目视确认。

## 未完成或未证明

- 实时性能（10 帧/秒）。
- 历史流水 SIGSEGV 的根因。
- 长时间稳定性：本轮先做 30 min 测试，见 `evidence/board/demo_soak_30min.json`。
- 物理按键切页现场确认，HDMI 实拍画面。
- 转轴标定、实物尺寸精度。
- 电机扫描：需要 12V 和现场批准。
