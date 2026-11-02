# 视频时钟 125 MHz 构建结果

构建：`scripts/build_r43.tcl`，参数 `8 0 1 1 0`（8 路静态，banked），输出 `build/clk125`，日志 `build/clk125.log`。

| 时钟 | WNS (ns) | WHS (ns) |
| --- | --- | --- |
| clk_fpga_1 视频 125 MHz | +0.559 | +0.037 |
| clk_fpga_0 主时钟 100 MHz | +0.003 | +0.012 |
| HDMI 像素钟 | +1.901 | +0.091 |
| cam_pclk | +3.124 | +0.028 |

- 视频域裕量从 R43 的 +0.041 提到 +0.559，降频目的达到。
- 全局最差路径移到 pfo3_frontend：`support_ram/mem_reg_1` → `hp_p_reg` 分布式 RAM，9 级逻辑（4 级 CARRY4），数据路径 9.15 ns。
- 全局 WNS +0.003 ns，不满足 ≥ +0.15 ns 的目标。该 bit 不用于上板。
- 下一步：依据 `docs/lane_decision.md` 改回 2 路方向核以减轻前端布线压力；如仍不足，再对这条比较链插一级寄存器。
