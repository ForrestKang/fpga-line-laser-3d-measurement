# 最后一板：PL 截面运行时

本文件对应 `runtime/r33/pl_section.py` 和正式入口 `python -m r33.live`。驱动不加载 bit、不创建电机控制器，也不控制激光。本阶段 PC 回归不能替代真实板端验证。

## 后端与准入

部署 manifest 新增：

```json
"section_measure": {
  "present": true,
  "version": 1,
  "register_window": [1792, 2047],
  "magic": 1397048141
}
```

`magic` 即 `0x5345434D`。数字取自 RTL `SECM` 接口，不用运行时试探来判断硬件是否存在。没有 `section_measure.present` 时，默认使用原 ARM 截面实现，不创建截面 MMIO、不读写该窗口。显式 `--section-backend pl` 在缺块时启动失败；`--section-backend arm` 可明确选择浮点参考，驱动不静默回退。

驱动只通过前端基址的 `0x700–0x7FF` 窗口读写。每次建立驱动后读 magic；错误立即拒绝。manifest 的窗口、magic 或版本不支持也拒绝。`version` 未写时按 v1 兼容，但若写出其他版本不接受。现有按键、0x500 测量和点云视图 ABI 不变。

配置写入先关闭截面块；原点 Q16、两个单位向量 Q24 与 jump/break/level Q16、min_points 完整写入后再使能。参数非有限或超出位宽，在写寄存器前拒绝。硬件不支持图像行 ROI，所以 PL 后端不能配合 `--profile-rows`，应明确改用 ARM 后端；不会忽略行窗口。

## 每帧结果、窗口与显示

PL 的提交计数在标量和所有表项发布完毕后递增。正式循环一次提交一包，驱动等待恰好下一次计数；默认上限 2 秒。超时、跳号或读取过程中计数改变直接失败，避免使用上一帧结果。主标量、最多 15 条台阶、16 条分段、丢点和溢出状态都保留。1024 点容量或分段溢出时，数字不进入正式台阶/间隙判定。

相机预热的 PFO3 包也会触发截面求解。入口在启动采集线程和提交正式第一帧之前等待并消费该包，单独写入 `warmup_section.json` 和 `summary.warmup.section`，绑定 `input_frame_id=0`。预热结果不进入正式帧比较或测量云；正式第一帧继续要求精确下一包，不跳过计数、不放松身份检查。预热超时或跳号会直接失败并保留证据。

PL 处理的 grade 与配置一致：preview 使用 valid+preview 位；measurement 使用 valid+measurement 位。两类 XYZ 和采集保存原路径保持不变；此接口只消费 PL 相机坐标记录，不把预览点写入 measurement。

自动深度窗口使用**上一帧、当前 Z 窗口内有效点的 Z 第 5 百分位加 `auto_depth_mm`（默认 250 mm）**，取 Q16 向下舍入，作为下一包的 z_max。首包无限制；空窗口保留先前界限；显式 Z 窗口或 auto_depth=0 时不自动更新。配置与每帧证据记录当帧窗口和下一帧上界。

页面 B 的台阶、间隙、深度、噪声、level、间距、参考 RMS/斜率及台阶/分段表来自 PL。ARM 仅将当前 XYZ 投影到截面平面以绘图，显示用参考截距从投影点估计，不能作为新的测量值。参考残差数字直接取 PL RMS。间隙仍用候选行摘要补查：若两边界间超过半数行有局部候选却没有预览观测，则标记 `gap_unresolved`，不输出采集空洞尺寸。

## 对照和性能证据

PL 后端为每个完成帧额外执行一次 ARM 浮点参考，用同一 grade、同一量化 Z 窗口做对照，写 `section_compare.jsonl`：帧号、PL packet、有效性、丢点、原始硬件结果、两侧标量及差、两侧台阶/分段、参考 RMS/斜率、solver cycles、下一帧窗口。该参考是本轮验收诊断开销，不宣称已经关闭 ARM 对照。

原有 `ui_display_events.json` 逐次包含 `display_ms`、`ready_to_ack_ms`（相机完整帧就绪到 VDMA 确认）及新增 `input_to_ack_ms`（capture 开始到 VDMA 确认）。`summary.json` 的 `display_timing_ms` 提供各项 median/p95/maximum；页面 Pillow 重画耗时仍在 `ui.render_ms`。ACK 数量不等同于新模型更新率，静态相机也不能证明旋转模型性能。

`frames.json` 同时保存每帧 `capture_started_monotonic`、`capture_ready_monotonic` 和 `processed_monotonic`；显示事件保存 `ack_monotonic`，summary 保存 `running_start_monotonic`。板端 CPython 的这些性能时间戳与资源监测的 monotonic 位于同一单调时钟轴，可按实际帧范围重算活动期 CPU、处理速度、显示速度及画面年龄；不得将包含启动/门禁/退出的整程 CPU 冒充活动期负载。

运行示例（需使用实际已加载固件对应的 manifest，输出目录必须全新）：

```bash
cd /home/xilinx/scan3d_final_20261029
PYTHONPATH=runtime /usr/local/share/pynq-venv/bin/python3 -B -m r33.live \
  --manifest deployment/manifest.json \
  --intrinsics runtime/calibration_r41/camera_intrinsics_r41.json \
  --laser-plane runtime/calibration_r41/laser_plane_r41.json \
  --output data/static_section_new_run \
  --duration-s 60 --save-raw-every 1 --section-backend pl \
  --pages A,B,C --initial-page A --motor-power-disconnected
```

不传 `--allow-motor`。自动切页诊断可加 `--ui-cycle-test-s 10`，必须在报告中标记为自动测试，不能记作实体按键。该命令仅供主代理完成固件/设备准入后执行；本文没有执行板端操作。

## PC 检查

```powershell
python -I runtime/run_tests_final_section.py --report evidence/runtime_section_pc_<新编号>.json
```

新增检查覆盖：缺声明不构造 MMIO、magic/版本/窗口拒绝、单位换算、输入配置边界、固定点模型结果/台阶表、空帧、solver 超时、跳号、丢点、上一帧窗口、两 grade 隔离、采集空洞、页面 PL RMS，以及实际调用 `live.main` 的模型 MMIO 完整运行与逐帧对照落盘。继承按键、有限扫描状态机、页面、浮点截面和 PL 统计测试一同运行。
