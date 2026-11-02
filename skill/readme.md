# 可复用的 PYNQ 工程提交与归档技能

## 1. 包完整性校验
使用：`python skill/verify_bundle.py <工程根目录>`。仅使用 Python 标准库，无需 PYNQ、网络、管理员或板卡。检查 manifest 中每个文件的存在、大小与 SHA256，并拒绝未登记文件、路径穿越、符号链接；不执行任何硬件动作。
适用：比赛交接包、PYNQ overlay/ARM 软件分发、归档复制后验证。验证效果见 sim/results/checker_validation.json（8 项正确/损坏/缺失/额外文件/路径与重复条目测试）。
失效条件：manifest 本身来自不可信方、硬盘读错、文件验证后再次变化。它不是数字签名，也不证明 bit 与源码行为等价。

## 2. 从失败中提炼的归档规则
先整目录复制，再逐文件对照大小和 SHA，按格式读取；完成前不删原数据。将回执、原图、观测、点云、preview 和失败输入看成同一运行证据，而不是只备份几个 JSON。空 Python 包文件合法；空日志需由格式合同解释；预期非空 payload 为零字节则失败。
此规则来自 R26 回收只带回 JSON 导致历史 payload 缺失的观察；本包不声称那些缺失数据已经恢复。

## 3. 通用提示词模板
“先只读核对目标、版本、输入与输出合同；冻结原始目录；给每个新产物记录来源和 SHA；把 PC 模拟、RTL、整板实现、板测和实物精度分别记录。失败保留原始输入与日志。验收通过后再整理提交，不把微基准速度当作整链性能。”
适用：PYNQ/FPGA 工程审查与交接。板卡加载、相机和运动等实际动作另核对当前授权，模板不授予硬件权限。

## 4. Overlay与运行时绑定检查（新增可执行技能）

用途：在发布PYNQ工程前，检查固件文件及其绑定的ARM运行时是否属于同一组交付文件。完整说明见 `skill/pynq_release_check.md`，开发来源见 `report/skill_development.md`。

```powershell
python -B skill/pynq_release_check.py --manifest board/firmware/manifest.json --runtime-root src/ps
python -B skill/test_pynq_release_check.py
```

该工具只读、不连接板卡，不自动修清单。17项测试中16项通过，链接逃逸用例因环境限制跳过；实际候选中识别出两项runtime guard不匹配并返回失败。测试结果见 `sim/results/pynq_release_check_tests.json`，实际检查见 `sim/results/pynq_release_check_actual.json`。文件检查不能替代RTL、整板、板端或实物精度验证。

## Git仓库中的包校验

包校验器仅排除根目录的Git本地元数据（.git目录或worktree的.git文件）；额外payload仍报未登记，已登记内容损坏仍报失败，不把日志或原始数据统一排除。对应新增5项故障/检出测试：`python -B skill/test_verify_bundle.py`。
