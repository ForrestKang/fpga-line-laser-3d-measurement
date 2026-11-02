---
name: pynq-overlay-check
description: Offline checks for PYNQ overlay files and their host drivers, including mixed releases, missing or changed files, unsafe manifest paths and independently reusable package checks. Use for file pairing and handoff verification; file checks do not load an overlay or validate live hardware.
---

# PYNQ Overlay 与驱动配套检查

识别用户指定的清单、主机程序目录及输出位置。先用已有文件完成检查，文件任务不要求板型、寄存器地址或在线设备；存在多个候选时按来源确认目标，不任取首个文件。

本目录工具只依赖Python 3.10及以上的标准库，可直接调用：

```text
python -B <本目录>/pynq_release_check.py --manifest <清单> --runtime-root <主机程序目录>
python -B <本目录>/verify_bundle.py <待检查的工程根目录>
```

检查结果指出哪个文件、哪项声明不匹配；只发现摘要差异时，不推断具体硬件故障。

Overlay清单格式和退出码见[pynq_release_check.md](pynq_release_check.md)。需要保存检查器JSON时使用`--output <新文件>`，保留原输入及旧报告；检查失败不能通过自动重算清单摘要转成通过。

返回匹配结果、具体问题、实际命令和输出范围。现有脚本验证文件内容和配套声明；设备型号兼容、实际加载、接口行为及算法精度使用目标工程自己的验证。

用户进一步要求DMA诊断、黄金参考比对或性能分析时，按需读取[readme.md](readme.md)中对应任务模板，确认驱动与工具能力，再使用目标工程既有入口；不要把文件校验器当作通用DMA或仿真执行器。

脚本自测入口为`test_pynq_release_check.py`、`test_verify_bundle.py`和`test_portability.py`。这套技能不自动连接硬件、不写寄存器、不加载固件。
