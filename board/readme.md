# 上板资料

firmware/ 包含最终保存的 bit/HWH 和原 manifest；通过 build/restore_sources.py 恢复时保留 manifest 所引用的原始文件大小写。
src/ps/r33/live.py 为实时主入口，replay.py 为真实 PL 回灌入口；具体可用参数以 `python -m r33.live --help` / `python -m r33.replay --help` 为准（在已还原并配置好导入路径的环境）。
boot_reference/ 是实际 boot/service 源码快照，仅供理解部署关系，不是自动安装指令。旧绝对路径、装置专用标定及清理策略需要逐项复核；没有打包历史授权 proof、motor_standing.json 或 authority.json。
historical_outputs/ 是旧标定下的结果，不代表当前测量精度。源固件 manifest 的 runtime_guards 与 rig2b 后续软件存在漂移，详见 report/verified_status.md；本次不修改原 SHA 来掩盖漂移。


## 有限的实测证据子集

evidence/ 的五份 JSON 是原保存板端回执的脱敏提取件，原来源和 SHA、删去字段在 extraction_manifest.json 中。
包含双采样模式合同试验、两个各200视角的历史运行和已量化点云；没有本轮重新上板。
NPY 是从回执 BLOB 还原的量化点云，不是板端原始 NPY 的逐字节副本，也不包含全部原图。
原回执中“完成/已标定”字符串保留为历史结果，不能覆盖当前独立光学精度尚未通过的事实。
本包没有声称整套历史原始采集数据齐全，也不带传输主机信息、设备权限或自动运动批准。
