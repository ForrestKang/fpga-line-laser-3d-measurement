# Seed-retry 合入、固化与 T02 旋转实测（2026-10-31）

## 合入内容

- RTL：从 `scan3d_t02_stripefix_20261031_01` 取 Codex 的同 run 种子重试。
  - `rtl/r35_row.sv`、`rtl/r35_frontend.sv` 原样采用（本工程这两个文件与 Codex 的基线相同）。
  - `rtl/r35_axi.sv` 加一行 `.seed_retry_mode(snapshot[33]==32'd3)`，保留实时流。
  - 旧文件在 `archive/pre_seedretry_20261031/`。
- Runtime：
  - `protocol.py`、`storage.py` 采用 Codex 版本；
  - `transport.py` 按 manifest 的 `pfo3.same_run_seed_retry` 选 policy 3；
  - 新增 `tests_seed_retry_contract.py`。
- `boot/demo_service.py`：manifest 开启 seed retry 时去掉 `--pfo3-bin2`（seed retry 只支持全宽）。
- 构建 `build/seedretry`：
  - 结果：WNS +0.230 ns，WHS +0.011 ns，TNS/THS 0，CDC Critical 0。
  - 流程内审查只因方向通道名正则（多了 `bounded_retry` 一层）误拒，已按 Codex 的同样修法修 `scripts/review_build_r43.py`。
  - 修正后全部门控通过，由已布线的 `route.dcp` 出 bit：`db396f4c…`。
- 测试：
  - 本地：runtime 76 OK，boot 17 OK；
  - 板上：seed retry 合同测试（ARM native）OK。

## 模式对比（同一新 bit，同一 T02 摆放）

| 项目 | 全宽 + seed retry | 2× binning 单种子 |
|---|---|---|
| 静态45 s：PL 帧率 | 26.4 fps | 52.9 fps |
| 静态45 s：每帧测量点中位 | 273 | 300 |
| 旋转一圈：测量点总数 | 51,560 | 65,052 |
| 旋转一圈：小段圆度 RMS | 0.17 mm | 0.08 mm |

binning 全面更好，所以开机固化用 binning（`deploy_seedretry_bin`）。seed retry 已在 bit 里，换回 `deploy_seedretry/firmware/manifest.json` 即可启用，不需要重新构建。

## 固化

- `/boot/boot.py` 未变（`319c51ce…`），config 改为 `f85d5934…`，原文件备份为 `/boot/boot.py.orig_20261029_103129_5da646b6`。
- 重启后（boot `c3db7643`）：
  - boot 日志确认加载 `deploy_seedretry_bin/firmware/final.bit`（sha `db396f4c…`），scan3d-demo 自动启动；
  - 运行状态 running、binning 开、实时流 0 错误、BTN0 就绪，axis 为 CALIBRATED_CHECKERBOARD，激光平面 d = −80.755；
  - 电机和守护均为禁用，error 0。
- 标定未改：内参、激光平面、转轴三文件哈希与之前固化版一致（3547 步/圈）。

## T02 旋转实测（本次摆放：小端朝下、大端朝上；200 视角、0 跳过、140 s）

| 段（自下而上） | 标称 | binning | seed retry | 段内自下而上 |
|---|---|---|---|---|
| 小段 | 20 | 19.54（−0.46） | 19.49（−0.51） | 不变 |
| 中段 | 30 | 30.65（+0.65） | 30.67（+0.67） | 30.07→31.23 |
| 大段 | 40 | 44.82（+4.82） | 44.70（+4.70） | 43.95→45.71 |

段高 17 / 17.5 / 17.5–20（标称 16），总高 52–54.5（标称 48）。

结论：
- 两种提取模式结果一致，所以误差不是 seed retry 或 binning 引起的，而是标定（转轴/尺寸）在这种摆放下不成立。
- 之前大端朝下时三段误差在 −0.10 到 −0.32 mm；这次大端朝上，越高、半径越大，读数越大。
- 这与宇航员扫描偏差的怀疑一致：T02 只在一种摆放下拟合的转轴，约束不住与高度相关的误差。
- 后续应把大端朝下和这次大端朝上两次扫描联合拟合转轴和比例，再用第三个物体验证。

## 数据

- 点云：`tmp/t02_seed/`、`tmp/t02_bin/`，对比 `tmp/t02_seedretry_compare.json`。
- 板端收据：`evidence/board/seedretry_*.json`、`post_seedretry_*.json`。
- 脚本：`scripts/package_seedretry.py`、`scripts/finalize_seedretry.tcl`，`board/seedretry_trial.py`、`seedretry_scan.py`、`seedretry_install.py`。
