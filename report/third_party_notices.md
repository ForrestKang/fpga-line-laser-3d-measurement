# 第三方来源与许可

本说明用于定位已随源代码保留的许可文本，不替代原许可证。

| 组件 | 许可与位置 |
|---|---|
| Digilent axi_dynclk / rgb2dvi | 相关 VHDL 保留原版权及 BSD-3-Clause 条款，见 src/fpga/vendor/r31_baseline_copy/ip_repo/ 中原 HDL 头部 |
| Noto 字体子集 | src/ps/assets/fonts/ofl.txt，原来源与字形记录一并保留 |
| DejaVu 字体子集 | src/ps/assets/fonts/license_dejavu.txt |
| AMD Vivado/XPM/PS7 等工具提供内容 | 使用安装的 Vivado 2025.2 生成/解析，按 AMD 对应许可使用；本包没有携带工具安装包或许可证密钥 |

本仓库 `ForrestKang/fpga-line-laser-3d-measurement` 的 `license.md` 为MIT许可，明确适用于新编写的自有代码、测试和文档。本工程继承该既有文本，未另行选择许可证。
外部参考文件、厂商IP、BSD或OFL组件仍按原许可分发，不被上述MIT文本重新授权。
原系统依赖（PYNQ、NumPy、Pillow 等）不打包虚拟环境，按各自来源安装。
