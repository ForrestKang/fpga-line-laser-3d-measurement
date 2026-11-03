# R42 runtime12 软件与离线验证说明

本目录是 runtime11 的独立候选。新增按键页切换、有限步进扫描、物体坐标拼接和 PC Mock 回归；原相机、PFO3、定点轮廓和 PL 测量入口保留。此说明和测试均不是实际运动或光学精度验收。

## 默认安全状态

- 未带 `--allow-motor` 时，不创建电机/守护 MMIO，不发送运动命令。BTN0 仅显示拒绝原因。
- 只有 manifest 声明按钮块，才创建按钮 MMIO；错误 magic 拒绝启动。
- 按键配置：`--pages A,B,C --button-pages A,B,C --initial-page A`；A 实时 PL 点云、B 定点轮廓、C 扫描物体坐标云。只启用一页时切页键均无动作。
- 切页只更新 PL CTRL.OVERLAY，不发 CLEAR，不改变 ingest/preview/auto/freeze 位。
- Pillow 仅在主循环；图约 3 次/秒，卡片及文字约 1 次/秒；显示线程只处理 numpy。

## 扫描与准入

扫描前需要 `--allow-motor --motor-authority <本次运行授权JSON>`。文件必须由已获用户现场许可的调度方提供，禁止软件推断 12V 状态或自动生成许可。校验在运动 MMIO 创建前执行。

manifest 需同时包含：

```json
{"motor_controller":{"present":true,"base_address":1136918528},
 "motor_scan":{"present":true,"pulses_per_revolution":3200,
               "motor_base":1136918528,"guard_base":1136984064}}
```

即 motor=0x43c40000、guard=0x43c50000。按钮合同：

```json
{"buttons":{"present":true,"register_window":[1536,1791],"magic":1379152450}}
```

授权字段：`machine_id`、`boot_id`、`run_id`（必须等于 output 目录名）、`allow_motor=true`、`motor_12v_connected=true`、非空 `user_statement`。任何不一致拒绝；重启后旧授权失效。现场目前12V未接，不能使用含电源已接声明的授权。

BTN0 单次按下启动，一次运行最多一圈；扫描中再次按下 STOP；停止、失败或完成后不得自动重启。

- `--scan-views 200`：每位置16脉冲；或320：每位置10脉冲。总量3200。
- 实际 MotorController.move_pulses 在唯一运动线程中执行；独立守护心跳；主循环持续采集、轮询按钮和显示。
- 完成运动的那帧不接收；至少再丢弃2个稳定帧，且满足 `--scan-settle-s`（默认0.1秒），再接受有效几何帧。
- `--scan-deadline-s` 默认600秒（允许1..1800秒）；绝对期限触发 FAILED 并停止/关闭，不假装一圈完成。扫描期间演示 `--duration-s` 不截断扫描；停止后恢复演示期限。
- 参数使用既有 B 阶段有限运动值：375000周期、187500高电平、10000000建立时间、18秒单次期限；驱动异常先停止，不自动清故障或重试。
- STOP/close 校验 motor enable_n 和 guard enable_pin 已禁能；超时/无法确认写入错误，不宣称安全关闭完成。

## 坐标与保存

PL 几何角度固定0；PS 复用现有 TurntableModel 的逆转轴变换，不修改几何公式。每帧记录 motor_id / motor_epoch / motor_steps、角度和接收状态。运动过程及稳定过程帧不并入拼接云。

保存 `xyz_camera_mm.npy`、扫描接受帧 `xyz_object_mm.npy`（Nx4，第四列1=measurement/0=preview）、`scan_frames.json`、`frames.json`、`turntable_preview.json`、状态及汇总。暂定点保持独立接受标记，不用于正式尺寸。本版轴仍是 PROVISIONAL_NOT_CALIBRATED，页C和导出明确标注，不能据此宣称毫米尺寸精度。

## PC 验证

```powershell
& 'D:/NUECSDC-FPGA/structured_light_system_algorithm_v1/.venv/Scripts/python.exe' -I -B -X utf8 ./run_tests_r42.py --report ./evidence/<新编号>.json
```

覆盖真实主循环而非孤立演示：完整200位置、期限、BTN0 STOP、相机/PFO3故障、默认禁运动；另以真实 MotorController/GuardController 配仓库 MockTransport 验证正常与卡住中止。历史 MockGuard 的 enable_pin 极性与物理 active-low EN 不同，仅测试夹具修正极性；生产门禁不放宽。

未验证：真实按钮、真实相机与运动并发、12V使能与停止、转轴标定、实际360°模型、性能。必须按任务书硬件授权点逐项进行，不可由 Mock 结果替代。
