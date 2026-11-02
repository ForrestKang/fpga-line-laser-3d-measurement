# 硬件与接口

## 平台

| 部分 | 当前实现 |
|---|---|
| 开发板/器件 | PYNQ-Z2 / XC7Z020CLG400-1 |
| 图像输入 | MT9V034，752×480，8位灰度 |
| 照明 | 650nm线激光，独立光路 |
| 显示 | HDMI，1280×720界面 |
| 转台接口 | STEP、DIR、EN及守护信号 |
| 系统/视频域 | 100MHz / 125MHz |
| 工具版本 | Vivado 2025.2 |

仓库提供overlay及运行软件，不是完整可刷写SD系统镜像。顶层接口见[Board Top](../src/fpga/rtl/r42_board_top.sv)。

## AXI地址

地址来自[BD重建脚本](../src/fpga/bd/block_design_r40.tcl)。修改接口时应同步更新BD、驱动及测试。

| 功能 | 基地址 | 窗口 |
|---|---:|---:|
| PFO3 Frontend | 0x40000000 | 4KiB |
| Camera Control | 0x43C00000 | 64KiB |
| Camera Diagnostics | 0x43C10000 | 64KiB |
| Video Timing Controller | 0x43C20000 | 64KiB |
| Dynamic Video Clock | 0x43C30000 | 64KiB |
| Motor Controller | 0x43C40000 | 64KiB |
| Motion Guard | 0x43C50000 | 64KiB |
| Raw DMA | 0x40400000 | 64KiB |
| HDMI VDMA | 0x40420000 | 64KiB |
| PFO3 DMA | 0x40430000 | 64KiB |

Raw、视频和PFO3分别经HP0、HP1和HP2连接DDR；GP0用于寄存器控制。HP3当前未启用。

## Frontend内部窗口

以下偏移相对0x40000000，具体读写语义以[AXI Frontend](../src/fpga/rtl/r35_axi.sv)及驱动为准。

| 偏移/窗口 | 功能 |
|---|---|
| 0x000 / 0x004 / 0x008 | magic / ABI / feature |
| 0x010 | START / ABORT |
| 0x014–0x020 | busy、done、error、序列及输出长度 |
| 0x024–0x034 | live控制、包/错误计数、抓包与Raw复制序列 |
| 0x100–0x1FF | 帧配置及来源快照 |
| 0x200–0x2FF | 观测与质量摘要 |
| 0x300–0x35F | 几何参数、commit、epoch、SHA及缓存高水位 |
| 0x400–0x4FF | 点云视图 |
| 0x500–0x5FF | 测量统计 |
| 0x600–0x6FF | 按键 |
| 0x700–0x7FF | 截面测量 |

当前RTL的magic为0x57334650，ABI为0x00035000，feature为31。实际运行仍需核对固件与驱动合同，不能仅凭磁盘bit存在判断已加载的硬件。

几何参数使用0x300选择地址、0x304/0x308传送数据、0x30C写入或commit；0x310为epoch，0x340–0x35C为SHA。0x30C读回准入、错误及commit等待状态。参数更改会撤销旧准入，开始处理前须完成有效提交。

## 测量与截面

[Measurement Statistics驱动](../src/ps/r33/pl_measure.py)访问0x500窗口，将PL距离、坐标范围及分层半径统计转换为毫米。

[Section Measurement驱动](../src/ps/r33/pl_section.py)访问0x700窗口：

- magic为0x5345434D，版本1。
- 长度、位置及阈值使用Q16，基向量和斜率使用Q24。
- min_points范围为3–1024，段表最多16段。
- 提供主台阶、台阶列表、间隙、深度、噪声、参考段残差与斜率等输出。
- manifest声明正确的窗口、magic和版本后才创建MMIO；读取前后核对包计数，避免混用两个包的标量与表格。

ARM仍按行摘要识别可能由采集缺口形成的间隙，并保留用于绘图的截面投影；不能将绘图投影写成PL截面拟合。

## 按键

引脚来自[按键约束](../src/fpga/constraints/r42_buttons.xdc)，电气标准为LVCMOS33。

| 按键 | FPGA封装引脚 | 功能 |
|---|---|---|
| BTN0 | D19 | 扫描开始/停止请求 |
| BTN1 | D20 | 实时页 |
| BTN2 | L20 | 截面页 |
| BTN3 | L19 | 扫描页 |

[按键驱动](../src/ps/r33/buttons.py)用8位模计数计算新按键事件。BTN1–3直接选页，不依赖循环切换次数。

## 相机引脚依据

以下是[相机XDC](../src/fpga/vendor/r31_baseline_copy/source_tree/pl/baseline/constraints/p2_pins.xdc)中记录的FPGA封装引脚，不是相机排针序号。

| 信号 | FPGA封装引脚 |
|---|---|
| DATA[0..7] | T14、U12、U13、V13、V15、T15、R16、U17 |
| LV / FV / PCLK | V17 / V18 / U18 |
| RESET_N / STANDBY | T16 / R17 |
| SDA / SCL | P16 / P15 |

这些相机信号使用LVCMOS33。重新接线或制作转接板时，应将实际XDC、开发板原理图与相机针脚表逐项对应；不要按排针排列猜测。

## 部署与现场边界

[启动源码参考](../board/boot_reference/)用于理解原部署关系，其中含装置专用路径，不应直接覆盖另一块板的/boot。[板端说明](../board/readme.md)列出固件、启动与历史输出的范围。

本地授权和电机许可不随源码转移。首次静态验证与固件加载时断开电机12V；真实转动前再确认机械固定、线缆、供电及有效标定。本页提供接口依据，不构成自动加载、运动或激光操作授权。
