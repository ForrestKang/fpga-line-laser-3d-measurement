# PYNQ Overlay 发布文件准入检查

`pynq_release_check.py` 是一个只读、离线的发布文件检查器。它把 overlay 清单中的文件记录，与清单旁边的实际文件，以及 `runtime_guards` 指定的运行时代码逐项核对。

## 适用场景

在交付 PYNQ overlay 前，可以用它发现以下问题：

- `.bit` 或 `.hwh` 缺失、大小不符或 SHA-256 不符；
- overlay 清单把运行时代码绑定到旧版本，而交付目录中的代码已不同；
- 清单路径是绝对路径、带驱动器路径、包含 `..`，或通过符号链接 / junction 逃出指定根目录；
- manifest 格式错误，或者没有声明 runtime guard。

检查器不绑定特定开发板、工程版本、寄存器地址或 overlay 文件名。清单的 `files` 字段使用“角色名 → `{path, size, sha256}`”映射；文件路径相对 manifest 所在目录。`runtime_guards` 使用“运行时代码相对路径 → SHA-256”映射，路径相对 `--runtime-root`。

## 用法

在工程目录执行：

```powershell
python skill/pynq_release_check.py `
  --manifest board/firmware/manifest.json `
  --runtime-root src/ps
```

退出码为 `0` 表示清单声明的文件绑定全部通过，`1` 表示发现清单或文件问题，`2` 表示无法创建指定报告文件。JSON 报告同时输出到标准输出。

需要保存报告时，明确指定一个尚不存在的文件：

```powershell
python skill/pynq_release_check.py `
  --manifest board/firmware/manifest.json `
  --runtime-root src/ps `
  --output sim/results/release_check.json
```

`--output` 使用独占新建模式。目标已存在时拒绝写入，不覆盖已有证据。输出目录必须预先存在。

## 已验证效果

测试使用 Python 标准库 `unittest`，覆盖有效 overlay、bit 大小与摘要错误、HWH 缺失、运行时代码摘要错误和缺失、运行时根目录缺失、绝对路径和路径穿越、Windows 驱动器路径、反斜杠路径、重复文件路径、无 guard 字段、损坏 JSON、非对象 JSON 和报告文件防覆盖。

在 Windows 当前执行环境中，符号链接创建权限不可用；相应安全用例被标为 skipped，并记录原因。路径解析仍在普通路径和恶意路径用例中执行。若在允许建立符号链接 / junction 的环境中运行，该逃逸用例会实际验证拒绝行为。

## 失败如何触发

- 任一 overlay 文件不存在、不是普通文件、大小不符、摘要不符或读取期间发生变化：该项失败，整体不通过。
- 任一 runtime guard 缺失、路径不安全、无法读取或摘要不符：该项失败，整体不通过。
- 缺少或为空的 `runtime_guards`：标为 `coverage: uncovered` 并判整体失败，不会被当成“无须核对”。
- 清单无法读取、JSON 损坏、字段类型错误：报告结构化错误，不抛出未处理异常，也不改变清单或目标文件。
- `--output` 已存在：返回码 `2`，拒绝覆盖。

## 当前候选中的实际发现

对 `board/firmware/manifest.json` 与 `src/ps` 执行后，overlay 文件 `final.bit` 和 `final.hwh` 的大小及 SHA-256 均与清单一致；8 个运行时 guard 中 6 个匹配，[Live Runtime](../src/ps/r33/live.py) 与 [Section Measurement Driver](../src/ps/r33/pl_section.py) 的实际 SHA-256 与 manifest 声明不一致。检查器如实返回整体失败。此结果说明当前清单与当前软件目录不能作为同一组已绑定发布文件通过准入；它没有修改或重写任何一方的摘要。

## 失效条件与证据边界

检查针对运行时实际读取到的文件。若检查时另一个进程并发替换文件，无法提供对后续发布时刻的原子保证；报告会检测普通文件大小、修改时间或文件身份在读取期间的变化，但正式发布仍应在冻结目录上运行。

文件绑定通过只说明这些路径、长度和字节摘要彼此匹配。它**不能证明** overlay 已加载、FPGA 正在运行该设计、AXI 行为正确、图像处理结果正确，也不能证明毫米尺寸精度。真实 PL 执行、RTL/实现验证、板端回放及实物精度必须分别取得证据。
