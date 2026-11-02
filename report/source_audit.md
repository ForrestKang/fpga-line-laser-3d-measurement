# 源码与构建交付审计（2026-11-01）

审计源：`D:\NUECSDC-FPGA\FINAL_BOARD_PACKAGE_20261101`。旧包只读；未访问板卡、未加载 bit、未运行电机。新文件只写在本任务 `audit` 下。下列相对路径均相对于旧包，另有说明除外。

## 结论与已运行验证

- 主 PL 构建入口是 `2_source_and_build/scripts/build_final.tcl`，并非所有历史 `build_*.tcl`。命令为 `vivado -mode batch -source scripts/build_final.tcl -tclargs <不存在的新输出目录> 2 0 0 0 0`；入口明确限定 Vivado 2025.2、`xc7z020clg400-1`。
- `project_inputs.tcl` 中 39 项历史 RTL/XDC 文件路径全部存在；新主工程会替换其中部分 RTL。未在本次重跑 Vivado，因此只能确认静态依赖完整，不能声称本次重新综合/实现成功。
- 对 `2_source_and_build` 和 `1_board_image` 中全部 498 个 Python 文件运行 `ast.parse`：0 个语法错误。该检查没有导入或执行硬件模块。
- 实际重跑 `scripts/review_build_final.py --reports build_seedretry/reports --output <本任务>/audit/source_report_gate.json`，退出码 0，所有 gate=true；architecture=true，lane IDs=[0,1]、section_present=true。WNS=+0.230ns、WHS=+0.011ns、CDC critical=0、route errors=0。这是对已有报告的重新评估，不是一次新实现或实板验证。
- 检查 `ip_repo` 两个 IP 的 component.xml 文件引用：axi_dynclk 7 项均存在；rgb2dvi 19 项仅缺 `docs/rgb2dvi_v1_2.pdf`（文档附件），综合 HDL/XDC 均存在。
- `runtime_rig2b/r33/native/libassemble_r33.so` 与 `libmarkers_r34.so`：源码 SHA、二进制 SHA 均与各自 `.so.json` 回执匹配；marker 回执目标是 armv7l。未在 Windows 加载 ARM so，也未在本次编译 C。
- 已复制 42 个离线集成回归文件，总计 32,108,332 字节（约 30.62 MiB）到 `audit/sim_support`，逐文件源/目标 SHA256 一致。`SIM_SUPPORT_MAP.json` 记录精确来源、目标、字节数和 SHA256；未复制 r36_runtime、缓存或原始大数据集。

## 必须保留的 PL 源树

保持 `2_source_and_build` 当前相对结构（父代理可归档到 src/fpga，并通过恢复工具恢复原文件名）。建议优先保留以下小型目录整体，避免凭文件名错误删掉实际输入：

| 路径 | 保留依据 |
| --- | --- |
| `rtl/`、`baseline_rtl/` | 实际替换/新增的 top、camera、frontend、geometry、view、AE、section、button 及 direction RTL |
| `constraints/` | `r42_buttons.xdc`、`constrain_cdc_r40_b09.tcl` 是主入口实际使用；历史约束文件很小，可一并保留 |
| `bd/block_design_r40.tcl` | 主工程 BD 重建入口；含 PS7、AXI/DMA/VDMA、视频、时钟、HDMI IP 配置 |
| `scripts/build_final.tcl`、`create_project_r43.tcl` | 主构建与工程创建 |
| `scripts/freeze_compiled_sources.py` | 构建前绑定实际 synthesis inputs 的 SHA |
| `scripts/review_build_final.py`、`review_build_r43.py` | 主构建 fail-closed 报告门槛及其导入依赖 |
| `vendor/r31_baseline_copy/scripts/project_inputs.tcl` | 继承输入列表 |
| `vendor/r31_baseline_copy/ip_repo/` 整体 | Digilent rgb2dvi、axi_dynclk 的 component.xml、HDL、XDC、xgui 和自定义 TMDS 接口 XML |
| `vendor/r31_baseline_copy/source_tree/pl/rtl/` | 清单引用的 camera/cdc/algorithm/profile/motion/video 等基础 RTL |
| `vendor/r31_baseline_copy/source_tree/pl/integration/` | `p2_processing_bd.v` 等继承模块；部分 PFO3 文件会被新 RTL 替换，保留整目录成本很低 |
| `vendor/r31_baseline_copy/source_tree/pl/r25_next/rtl/` | RAM、include `.svh`，主入口将其设为 include_dirs |
| `vendor/r31_baseline_copy/source_tree/pl/baseline/constraints/` | `p2_pins.xdc`、`p2_timing.xdc`、`p2_hdmi_out.xdc`、`b_motion_pins.xdc` |
| `vendor/r31_baseline_copy/source_tree/pl/baseline/scripts/constrain_hdmi_clocks.tcl` | synth 后主构建显式 source |

不能用 `**/build/**` 规则整体删 vendor 中的目录。历史清单在 `vendor/r31_baseline_copy/source_tree/pl/build/full_project_r25/` 下含这 3 个 HDL：

1. `p2_board_top.sv`：实际主工程仍加入。
2. `axis_bgrx_to_rgb24_bd_wrapper.v`：实际主工程仍加入。
3. `structured_light_motion_profile_hdmi_top.sv`：历史输入，`create_project_r43.tcl` 按 basename 将其替换成 `rtl/r42_board_top.sv`，因此严格说不是最终主工程编译必需。为保留原清单自洽与避免误删，建议这 3 个小文件一起保留。

`source_tree/pl/build/full_project_r25_v9/sl_profile_hdmi.gen/**`、其 `.srcs` BD 快照属于旧工程生成物，主流程由 `bd/block_design_r40.tcl` 重建并 `generate_target all`，可以从精简源码去除。Vivado 官方 IP 由 Vivado 2025.2 安装提供；不能把 vendor HDMI IP 当作 Vivado 自带 IP 删除。

## PS 实际运行源码和资源

实际链路为 `1_board_image/boot/boot.py` → 配置 → `boot_rig2b_install/demo_service.py` → `python -B -m r33.live`。配置中的 release/runtime 是板端符号链接布局，包中仅保存 `runtime_rig2b`；交付安装说明必须重建 `release/runtime` 或复制同等目录布局。

建议 PS 源码先保留 `1_board_image/runtime_rig2b` 下所有 `.py/.c/.h`、JSON 合同、字体和有效标定。整个 runtime 约 6.45 MiB，逐文件按历史版本裁剪收益很小，动态导入/ctypes/相对资源依赖却容易漏。目录中 `r33`、`candidate`、`reference_candidate`、`structured_light_system_algorithm_v1`、`structured_light_system_geometry_v1` 和根部 `geometry_reference_r35.py` 均保留；`virtual_scan`、`tools` 可作为离线工具保留。`evidence` 为历史 QA，可移到 evidence 而非源码主树；测试与 mock 不能当作现场测量证明。

- [Live Runtime](../src/ps/r33/live.py) 在运行时自行加入 ROOT、algorithm `src/python`、algorithm `board/scripts`、geometry `src/python` 到 sys.path，不能只交付主运行入口目录。
- 相机门槛实际导入 algorithm `board/scripts/verify_p2_board.py`；不要整体删所有 board/scripts。
- 原生库源码包括 `r33/native/{assemble_r33.c,markers_r34.c}`、`candidate/native/*.c`、algorithm `scan3d/native/{point_raster.c,cpu_fast_r20.c}`。
- `r33/build_native.py` 可显式构建 assembler；`tools/build_all_native_r26.py` 和 `tools/build_native_r22.py` 用于候选原生库，依赖 gcc、libm，assembler_r26 还依赖 zlib。现有 so 会使脚本拒绝覆盖，应在恢复后的新源码树执行构建，不覆盖冻结 so。
- marker 的 C 源码与回执齐全，但包中没有同等独立 marker 构建入口；可在新交付 wrapper 中依照 `.so.json` 的 gcc flags 生成该 so 及相应指纹回执。其缺失允许 NumPy fallback，不能把 fallback 性能说成 native 性能。
- `assets/fonts/` 整体保留：包括 Noto 子集、DejaVu 子集、`OFL.txt`、`LICENSE_DEJAVU.txt`、字体来源 JSON 与 glyph 清单。
- 未发现 requirements.txt/pyproject.toml/setup.py。板端需要 PYNQ Python 环境；源码直接使用 NumPy、Pillow，部分离线工具另需其他 Python 包。交付 README 应明确板端环境和 CPU 离线依赖，不能将“498 文件语法检查通过”写成全部依赖安装验证。

## 固件、启动和敏感文件

板端 firmware 文件夹内 `final.bit`、`final.hwh`、`manifest.json`、`p2_board.json` 必须配套保留在可运行附件；`ADMISSION_FINAL.json`、`COMPILED_SOURCES_SHA256.json`、`SOURCE_FREEZE.json` 应保留作来源/哈希证据。实际提供的是 overlay 与 Python 启动文件，不是完整可刷写 SD 系统镜像，不应改名声称完整操作系统镜像。

以下是用户/设备授权记录，不能当普通源码提交；仅报告路径，不复述具体身份或声明值：

- `1_board_image/boot_autostart_btn0/motor_standing.json`
- `1_board_image/boot_rig2b_install/authority.json`
- `1_board_image/boot_rig2b_install/{install_check.json,install_receipt.json,EXPECTED.json}` 属安装审计/机器绑定记录，普通提交建议排除或审核脱敏后独立留存。
- `1_board_image/boot/scan3d_boot_config.json` 引用旧路径及 motor standing 的哈希，提交演示安装配置应重新生成安全示例；不要将旧授权文件迁移到另一台设备。

本次按私钥标记、常见凭据字段和文件名检查，未发现私钥文件/私钥块。host 工具中存在 password 参数/环境变量读取和 SSH 调用，不能因出现 password 单词就判定硬编码秘密；已查看形态为参数传递和环境读取，未打印秘密值。该扫描不是完整信息泄露认证。

`boot_rig2b_install` 没有 `restore_boot.py`，README 此处描述不精确。恢复脚本实际存在 `1_board_image/boot_autostart_btn0/restore_boot.py` 及 `2_source_and_build/boot/restore_boot.py`。最小启动源码可保留 `2_source_and_build/boot`（5 个 Python）及当前 `boot_rig2b_install/{boot_loader.py,demo_service.py,install_boot.py}`，并在新安装 wrapper 明确使用哪一份。

## 已确认便携性问题

1. `scripts/finalize_seedretry.tcl` 固定读取 `<root>/build/seedretry/artifacts/route.dcp`；旧包为 `build_seedretry` 且没有任何 route.dcp。它是历史辅助脚本，现包不能依该入口直接恢复 bit。主入口 `build_final.tcl` 可完整重新实现。
2. `scripts/build_r43.ps1:18` 固定 `D:\Vavido\2025.2\Vivado\bin\vivado.bat`；使用 PATH 形式主 Tcl 入口或在新 wrapper 显式接收 Vivado 路径。
3. `scripts/integration_verify_final.py` 固定旧工作区、Icarus 路径，而且原包缺 `<root>/runtime` 与 `<root>/tests/integration_axi_final_tb.sv`。回归依赖已按下节补齐，仍需 wrapper 设置恢复布局和修改路径绑定；没有重跑该 RTL 回归。
4. `build_page_fonts.py`、freeze/package/lane sweep、旧 integration/unit/timing 脚本中仍有 C:/D: 或 /home/xilinx 绝对路径。它们是历史制作/实验工具，不应在 README 宣称所有脚本可直接独立运行。源码可原样归档，交付入口只公开经适配的主流程。
5. `install_boot.py` 默认 release04、deploy release02 的旧板端位置；必须通过 `--release --deploy --data` 显式参数指定新布局。README 安装示例漏掉必需 `--output` 参数，需补上新回执路径。

## 补齐离线集成回归的最小清单与路径改点

已整理的 `audit/sim_support` 结构：

```text
tests/integration_axi_final_tb.sv
tests/xpm_stubs_sim.v
verification/board/r37_r41f_live03/raw_000240.gray8
verification/board/r37_r41f_live02/raw_000180.gray8
verification/board/r37_r41g_live02/raw_000015.gray8
verification/calibrations/{T05,T06}/calibration.json
verification/r33/direction_tests/reference.py
verification/r33/window_tests/reference.py
verification/sim/*.hex                          # 6 个有限样例
verification/R39_reference/*_primary.bin        # 8 个已冻结期望
verification/R41_14_reference/*_primary.bin     # 19 个已冻结期望
```

`direction_tests/reference.py` 动态从相邻 `window_tests/reference.py` 导入 features；已同时复制。没有它仅复制 direction oracle 会在执行时失败。r36_runtime 未复制，`geometry_reference_r35.py` 使用主包 runtime 的现有文件即可。raw 第 3 张在默认 19 case 之外的 liveg02_015 分支使用，也已有限复制。

主要原路径：测试台来自 `D:\Workspace\CodexWork\scan3d_claude_continue_20261031_01\tests\integration_axi_final_tb.sv`；XPM mock 与 verification 来自 `D:\Workspace\CodexWork\scan3d_r42_buttons_boot_20261029_01`；第 3 张 raw 来自 `D:\Workspace\CodexWork\scan3d_r37_cdc_threshold_20261026_01\stageA13\boardprep21_session_board31\board\r37_r41g_live02\raw_000015.gray8`。精确每文件来源见 `SIM_SUPPORT_MAP.json`。

对恢复后的 `scripts/integration_verify_final.py` 可在新 wrapper/适配副本中修改以下路径绑定（保持原始源码归档不变，保持断言/门槛不变）：

- 第 25 行 VERIFY：指向新 `vendor/verification`（或本 sim_support/verification）。
- 第 26-28 行 对应实现 和 geo 导入：先设置 `<root>/runtime` 到 sys.path，直接从该运行源码导入 `geometry_reference_r35`；去掉对旧 对应实现/r36_runtime 的路径回退依赖。
- 第 29 行 RUNTIME：提供 `<root>/runtime` 到主包 PS 恢复树的目录布局/链接。
- 第 43 行 liveg02_015：改为相对 `r37_r41g_live02/raw_000015.gray8`，仍与 BOARD 拼接。
- 第 52 行 IVER：改为配置参数/环境变量或 `shutil.which('iverilog')` 与 `shutil.which('vvp')`；脚本后续使用 `IVER/'iverilog.exe'` 与 `IVER/'vvp.exe'`，若支持 Linux 需同时适配可执行文件名。
- 第 59 行 xpm_stubs_sim.v：改为 `<root>/tests/xpm_stubs_sim.v`，同一行测试台也必须在 tests 下。
- 第 63 行 TEMP/TMP：改为新任务输出/临时目录，而非固定旧目录。第 22-23 行 ROOT/OUT 的既有相对入口保留。

补齐后的静态验收应检查所有本地源/fixture 的存在、SHA 与来源表一致、所有 Python 可 parse、IP XML file refs、源码恢复后路径大小写完全一致；再单独运行 CPU 模拟/RTL 仿真并记录其真实结果。不能用补齐成功或哈希通过代替 RTL 仿真通过。

## 可排除生成物和精简建议

从竞赛源码主树排除 `__pycache__`、`.pyc`、旧 `.gen`、旧 `.srcs` BD 快照、构建日志、`.jou`、`.log`、`.bak`、重复打包脚本输出、旧部署/安装回执和现用授权。`build_seedretry/reports` 约 3.92 MiB，可只将 timing/utilization/cdc/drc/route/admission 和源码哈希证据放验证附件；其他报告是参考，不是重新构建必需。

保留原源码逐字节归档是稳妥策略。若存档路径全部小写，`source_map.json + restore_sources.py` 必须恢复原始大小写（特别是 回放测试件的原始目录名、历史参考结果目录、`.svh` 引用及 IP XML 引用），再在恢复目录构建。实体名/模块名不应改变；单纯存档文件名改变不应传播成 RTL 标识符改写。
