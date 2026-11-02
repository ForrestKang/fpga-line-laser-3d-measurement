# Overlay 与运行程序配套检查

`pynq_release_check.py` 只读检查固件清单和主机驱动，适用于采用 bit/HWH 交付的 PYNQ 工程。脚本不绑定板卡型号、固定目录、寄存器地址或算法。

## 清单合同

- `files`：角色名到文件记录的映射。角色名由工程自定，常见为 `bit` 和 `hwh`；每项包含 `path`、`size`、`sha256`。
- `runtime_guards`：驱动相对路径到 SHA-256 的映射，至少有一项。
- 固件路径相对清单所在目录；驱动路径相对 `--runtime-root`。
- 路径使用 `/`，不能含绝对路径、驱动器前缀、`..` 或逃出根目录的链接。

先从构建记录确认固件与驱动确实配套，再生成冻结清单。例如在自己的工程目录运行：

```python
from pathlib import Path
import hashlib
import json

overlay = Path('filter_overlay')
runtime = Path('filter_runtime')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

manifest = {
    'files': {
        role: {'path': name, 'size': (overlay / name).stat().st_size,
               'sha256': sha(overlay / name)}
        for role, name in [('bit', 'filter.bit'), ('hwh', 'filter.hwh')]
    },
    'runtime_guards': {'filter_driver.py': sha(runtime / 'filter_driver.py')},
}
with (overlay / 'manifest.json').open('x', encoding='utf-8') as stream:
    json.dump(manifest, stream, indent=2)
```

文件名和驱动数量由自己的工程确定。示例生成文件记录，不替代软硬件功能配套验证，也不会覆盖已有清单。

## 调用与返回值

从技能目录执行：

```powershell
python -B pynq_release_check.py --manifest ../filter_overlay/manifest.json --runtime-root ../filter_runtime
```

标准输出为 JSON，包含 `passed`、`firmware_files`、`runtime_guards` 和 `errors`。退出码 `0` 表示全部声明项匹配，`1` 表示输入或文件错误，`2` 表示报告文件创建失败。

可选 `--output ../results/release_check.json` 保存报告；父目录须已存在，目标文件须尚不存在。脚本拒绝覆盖旧报告。

## 已验证的拒绝行为

测试覆盖 bit 大小和摘要错误、HWH 缺失、驱动变化或缺失、路径穿越、绝对路径、重复文件路径、损坏 JSON、非对象清单、没有驱动绑定及报告防覆盖。符号链接或 junction 逃逸用例仅在测试环境支持创建链接时运行，跳过原因会写入结果。

`test_portability.py` 另外检查两个无关合成工程、驱动交叉混用、目录迁移、独立复制调用和只读行为。它验证文件检查器的可移植性，不进行真实板级验证。

## 故障定位与边界

摘要不一致时，先查驱动是否来自同一次构建、文件是否传输完整，再恢复正确文件；缺少 `runtime_guards` 时判为未覆盖并失败，不静默忽略。原工程曾出现固件文件一致、两个关键驱动不一致的情况，这正是加入联合检查的原因。

脚本检测读取时可见的文件变化，不保证检查后文件不会再变。清单必须可信，输入应冻结；检查不会连接设备、加载固件或修改目标文件。
