# 板端证据安全子集：清单与边界

来源目录：D:\Workspace\CodexWork\scan3d_final_20261029\evidence\board
输出目录：D:\Workspace\CodexWork\scan3d_competition_submission_20261101_01\audit\board_evidence_subset

只读读取原始收据；没有复制原始 JSON、安装授权文件或其他授权文件。下列同名 JSON 是仅含选定 stdout/result 行的脱敏提取件，原始文件可用列出的源 SHA-256 在来源目录核对。

## 收据

| 输出文件 | 原始来源 SHA-256 | 删除的原始字段 |
|---|---|---|
| seedretry_trial01.json | 342d3bd08665c6ed541a280cb2af7b0460a771ed9fe53428262633f470270047 | action, hostkey, started_ns, automatic_retry, machine_id, boot_id, command |
| seedretry_scan_seed.json | 02c2bdbf851c0aa75ff1d649dd72134aedb97feabb616fbbb197314ac1d2d26b | action, hostkey, started_ns, automatic_retry, machine_id, boot_id, command |
| seedretry_scan_bin.json | 6233d07ba900bfec50b8335434b5f7efd643e434dc10674c39cab9a28c98c174 | action, hostkey, started_ns, automatic_retry, machine_id, boot_id, command |
| seedretry_collect_seed.json | 58b520f82bb13e6d9e78a11f37f43bb841039cbe88ce29392c15afa8e600adb2 | action, hostkey, started_ns, automatic_retry, machine_id, boot_id, command |
| seedretry_collect_bin.json | 82c6acc7de49b3cb7e66f05e2b9c0ba817d71b2cef162a0de1589280dc3c7355 | action, hostkey, started_ns, automatic_retry, machine_id, boot_id, command |

五份来源收据都含 hostkey、machine_id、boot_id 身份/传输字段；这些字段及 command 等非结果字段均已省略，未输出其值。字段名扫描未发现 license、licence、copyright、authorization、authority、approval、permit 字段。安全提取 JSON 内也没有保留敏感 stdout 行；逐份字段名和跳过行类型见 EXTRACTION_MANIFEST.json。

## 从 BLOB 提取的数组

两个 collect 收据的 BLOB 按候选目录中的 tmp/decode_collect.py 格式解码：base64 后 zlib 解压；前 6*N 字节是 N×3 小端 int16 XYZ（按 0.1 mm 量化），后 N 字节是 int8 measurement flag。结果数组写为独立 NPY，再以 np.load(..., allow_pickle=False) 读回验证。

| 模式 | 输出 | shape / dtype | 读取验证 |
|---|---|---|---|
| seed | seedretry_collect_seed_object_cloud_mm.npy | [70902, 4] / float64 | 有限值通过; measured rows=51560; flag values=[0, 1] |
| seed | seedretry_collect_seed_views.npy | [200, 2] / int64 | 有限值通过 |
| bin | seedretry_collect_bin_object_cloud_mm.npy | [77183, 4] / float64 | 有限值通过; measured rows=65052; flag values=[0, 1] |
| bin | seedretry_collect_bin_views.npy | [200, 2] / int64 | 有限值通过 |

点云是 Nx4：XYZ（mm）与 measurement flag；views 是 200x2 的电机步数/每视角点数结果表。提取的 seed/bin 点云各有 51,560 / 65,052 个 measurement flag 为 1 的行。这是记录内的点数统计，不表示这些点均达到精度指标。

## 边界

- 原始 receipt JSON 未复制；保留的脱敏 JSON 写有原始源 SHA-256、删除字段名、passed/wall_s 结果字段和筛选后的 stdout 结果行。完整提取元数据及新文件 SHA-256 见 EXTRACTION_MANIFEST.json。
- BLOB 还原的是量化后的物体坐标 Nx4 点云及 views 元数据，不是 board 端原始 NPY 文件逐字节副本。收据中的 FILE 行只记录 xyz_object_mm.npy / xyz_camera_mm.npy 的板端大小；xyz_camera_mm.npy 文件内容不在 BLOB 中，未重建。
- seedretry_trial01 / seed / bin 结果只证明对应记录中的合同测试、运行和采集状态；本任务未重跑回归、连接板卡或重做计量。
- 未复制 seedretry_install.json、authority.json 或其他授权证明/安装授权文件。
