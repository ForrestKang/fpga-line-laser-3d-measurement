# 板端文件与运行入口

`firmware/`包含配套bit、HWH、manifest和源码绑定记录。实时程序为[Live Runtime](../src/ps/r33/live.py)，回灌程序为[Replay Runtime](../src/ps/r33/replay.py)。

恢复工程后，在runtime目录查看运行参数：

```text
python -B -m r33.live --help
python -B -m r33.replay --help
```

`boot_reference/`保存启动服务和部署脚本，包含本装置使用的路径与配置；移植时按[项目复现步骤](../readme.md)与[技术报告](../report/design_report.pdf)核对设备、接线与参数。

`evidence/`保存板端回执及[来源清单](evidence/extraction_manifest.json)，`historical_outputs/`保存两个各200视角运行的回执与从数据BLOB恢复的量化点云。文件来源、格式和采集范围由清单记录。

软件与固件的对应关系见[配套记录](firmware/runtime_validation.json)，测试结果见[验证汇总](../report/release_validation.json)。
