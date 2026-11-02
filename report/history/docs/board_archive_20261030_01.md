# 最后一板：只读归档与可回收清单

本次仅通过固定 SSH host key、固定源网口及 machine/boot 身份读取板卡，写入 D 盘新归档。没有删除板端文件，没有创建板端硬件锁，没有加载 FPGA，没有访问 MMIO、运行相机或发送电机命令。

- 最终清单：`archive/board_space_20261030_01/ARCHIVE_MANIFEST_FINAL.json`
- 清单 SHA-256：`dd7bbe973186b69515a20a114f5462d541cb4a4009a933b32f3308c2255bec47`
- 15 个成功运行，204 个原文件；逻辑大小 643,454,474 B。
- 基于 `st_blocks`、设备号/inode、nlink 的保守可释放空间：644,087,808 B。
- 全部目标没有 symlink 或文件 hardlink；目录 nlink 不当作文件 hardlink。
- 最后可用空间：5,981,945,856 B。若这些目录全部回收，预计可用空间 6,626,033,664 B；该值是估算，实际删除后仍须重新读取文件系统可用空间和执行容量准入。
- machine：`c82cf5bcd3a14b95935d65bfb529d72d`
- 前后 boot：`3ecac088-69c8-4399-a110-9121631984b2`

每个 eligible 目录均已完成：完整 lstat 清单、远端逐文件 SHA、下载或复制经重新核对的 D 盘副本、本地大小与 SHA、JSON/JSONL/NPY/文本格式检查、全帧 journal 严格协议读回、measurement/preview 导出点云与 journal 数据 SHA 对照，以及归档后远端整目录清单重核。空文件只允许已定义的事件日志，不把空 payload 当成功。

原 `formal35_T06_rep1` 没有成功 RESULT，因此排除，不允许据此清单回收。R33 正式运行初次使用当前 R35 协议 reader 时被拒绝，原失败回执留在 `ARCHIVE_MANIFEST.json`。随后使用该历史工程原样保留的 R33 reader 完成严格读回，并记录 reader 路径和 SHA；没有降低协议或完整性标准。R33 额外两个成功短测用于补足空间，均按同样流程核验。

删除由主代理按最终清单另行执行。本报告不能作为任何失败记录、代码、固件、系统目录、旧包或清单外对象的删除依据。
