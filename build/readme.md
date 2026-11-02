# 构建与报告

优先使用 reproduce.py。它先校验并恢复冻结源码，再调用 Vivado 2025.2 原始 build_final.tcl。输出目录必须不存在，防止覆盖历史构建。
evidence/ 为已有实现报告/日志；其中 source freeze 内的绝对路径属于原构建记录，source_map.json 提供打包来源映射。
不随包带 route.dcp、所有 IP 生成缓存和历史版本，因此原 finalize_seedretry.tcl 只能作为历史脚本审阅，不能作为本包的重建捷径。
