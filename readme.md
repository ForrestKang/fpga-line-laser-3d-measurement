# 智能线激光三维测量系统

**Intelligent Line-Laser 3D Measurement System**

基于 **PYNQ-Z2** 的小型线激光三维测量装置：相机采集激光条纹，FPGA完成亚像素提取、定点几何和截面统计，屏幕显示当前截面与逐步形成的点云，ARM负责扫描编排、界面、拼接与完整数据保存。

[![简体中文](report/images/language_zh_active.svg)](readme.md) [![English](report/images/language_en_inactive.svg)](report/readme_en.md)

[电脑测试](#先在电脑运行) · [接线与接口](#接线与接口) · [构建与板端启动](#构建与板端启动) · [技术报告](report/design_report.pdf)

![系统硬件：开发板、相机、线激光与转台](report/images/system_photo.png)

## 作品做什么

面向小型零件、台阶和旋转外形的非接触截面检测与三维展示。单块开发板接入灰度相机、线激光、转台和HDMI显示器，提供实时观测、截面测量和整圈扫描三种界面。

- **完整PL算术通路**：共享窗口、方向精化、条纹与分支处理、Brown逆畸变、光平面求交、转轴变换、测量与点云显示。
- **测量与预览分开**：分别保留接受位、拒绝原因和XYZ，预览点不混入正式测量集合。
- **完整记录**：保存原图、硬件输出、观测、三维点、参数来源与错误状态，支持读回核查。
- **按键操作**：BTN1实时页、BTN2截面页、BTN3扫描页；BTN0在运行准入满足时发起开始/停止扫描请求。
- **可交接工程**：提供RTL、XDC、BD重建脚本、ARM源码、原生C、固件、测试、报告和恢复/构建入口。

## 三种界面

下面是程序直接生成的界面图，展示实时观测、截面测量和扫描信息。

### 实时观测 · BTN1

相机条纹、当前点云及处理状态在同一页显示。

![实时观测界面](report/images/live_page.png)

### 截面测量 · BTN2

显示截面轮廓、逐级台阶高度、噪声与质量趋势。

![截面测量界面](report/images/section_page.png)

### 整圈扫描 · BTN3

显示累积点云、角度进度、分层直径和扫描记录。

![整圈扫描界面](report/images/scan_page.png)

## 处理分工

![PL与ARM处理分工](report/images/system_architecture.svg)

| 处理侧 | 主要职责 |
|---|---|
| PL | 像素提取、分支与质量、定点几何、截面测量、点云叠加 |
| ARM | 参数与扫描编排、数据校验、扫描级拼接、页面组织、完整保存 |

正常几何坐标由PL产生，ARM解包时核对参数与帧身份。方向核默认两路；几何核复用DSP并采用有界状态机，并非每拍输出一个三维点。二维中心采用Q12，几何内部采用Q30，输出有符号Q16毫米坐标，Q30到Q16采用最近偶数舍入并检查溢出。参数通过epoch和SHA绑定，ARM核对标定、角度与帧身份后接收结果。

## 实测效果

| 项目 | 结果 |
|---|---|
| 图像处理 | FPGA随相机逐帧处理，约26.5帧/s |
| 整圈扫描 | 200个视角，约2.5分钟，约6.6万个测量点 |
| 台阶高差 | 卡尺值2、3、5 mm；截面读数2.00、2.90、4.96 mm |
| 阶梯圆柱 | 三段直径扫描值39.68、29.90、19.88 mm |
| 波纹花瓶 | 颈部卡尺值16.5 mm；扫描值16.23～16.55 mm |

阶梯圆柱参与过参数调整，其尺寸用于复核；花瓶为独立打印件对照。图像帧率、扫描时间、显示与持久化速度分别统计。标定方法、测量图像和尺寸对照见[技术报告](report/design_report.pdf)。

## 平台与实现结果

| 项目 | 配置 / 结果 |
|---|---|
| 开发板 / 器件 | PYNQ-Z2 / XC7Z020 |
| 相机输入 / 显示 | 752×480、8位灰度 / 1280×720 HDMI |
| 实现工具 | Vivado 2025.2 |
| 系统 / 视频域 | 100 / 125 MHz |
| LUT / FF | 39,111（73.52%）/ 50,902（47.84%）|
| BRAM / DSP | 78.93% / 134个（60.91%）|
| 布线后时序 | WNS +0.230 ns，WHS +0.011 ns |
| CDC Critical | 0 |

实现结果见[资源与时序报告](report/source_report_gate.json)。系统完成像素处理、三维重建、截面测量、HDMI显示与完整数据保存，逐项验证见[验证汇总](report/release_validation.json)。以上为随包历史记录，不代表每次源码重建都已完成同样的板端验证。

## 先在电脑运行

从仓库根目录执行。Python 3.10+可完成文件检查；软件回归另需本机C编译器和`sim/requirements_pc.txt`中的NumPy、Pillow、SciPy、fonttools、OpenCV。RTL测试使用Icarus Verilog（iverilog、vvp）；完整实现需要Vivado 2025.2和XC7Z020器件支持。依赖清单不是版本锁文件，复现时保留实际版本。

```powershell
git clone https://github.com/ForrestKang/pynq-structured-light.git
cd pynq-structured-light
python -B sim/tools/check_repository.py
python -B skill/verify_bundle.py .
python -B skill/test_pynq_release_check.py
python -B build/restore_sources.py --output ../scan3d_work
python -m pip install -r sim/requirements_pc.txt
python -B sim/run_release_tests.py --work ../scan3d_work --output ../scan3d_pc_results --cc gcc
python -B sim/test_release_pairing.py
```

恢复器依据`source_map.json`还原原始大小写和目录，生成`fpga/`、`runtime/`、`firmware/`及测试所需相邻路径。所有工作与输出目录应为新目录。电脑构建的动态库只用于电脑测试，板端须使用对应ARM架构的库。

```powershell
python -B sim/run_rtl_integration.py --work ../scan3d_work --output ../scan3d_rtl_results --iverilog iverilog --vvp vvp --cases empty
python -B sim/run_section_rtl.py --work ../scan3d_work --output ../scan3d_section_results --iverilog iverilog --vvp vvp --workers 2
```

截面测试使用182帧历史原图和13包合成输入，与定点模型逐位核对195包。整链入口默认只跑空帧，完整19例命令见[sim说明](sim/readme.md)；再次运行会派生脚本的RTL入口时使用新的恢复目录。这些命令不连接板卡。

## 接线与接口

相机为MT9V034，输入752×480、8位灰度；线激光为650 nm，转台使用STEP、DIR、EN及守护信号，HDMI输出1280×720。系统为overlay和运行软件，不含通用SD镜像。

相机引脚依据为[相机XDC](src/fpga/vendor/r31_baseline_copy/source_tree/pl/baseline/constraints/p2_pins.xdc)，下表是**FPGA封装引脚，不是相机排针序号**。接线时须与开发板原理图和相机针脚表逐项对应；电源和转台驱动接法以实际硬件资料为准，未确认项不得猜测。

| 信号 | FPGA封装引脚（LVCMOS33） |
|---|---|
| DATA[0..7] | T14、U12、U13、V13、V15、T15、R16、U17 |
| LV / FV / PCLK | V17 / V18 / U18 |
| RESET_N / STANDBY | T16 / R17 |
| SDA / SCL | P16 / P15 |
| BTN0 / BTN1 / BTN2 / BTN3 | D19 / D20 / L20 / L19 |

其他接口见[顶层RTL](src/fpga/rtl/board_top.sv)、[约束](src/fpga/constraints/)和[BD重建脚本](src/fpga/bd/block_design_r40.tcl)。GP0用于控制，HP0承载Raw DMA、HP1承载HDMI VDMA、HP2承载处理结果DMA，HP3未启用。系统域100 MHz、视频域125 MHz，相机PCLK经CDC进入系统域。

Frontend基址为`0x40000000`：当前magic=`0x57334650`、ABI=`0x00035000`、feature=`31`。`0x300`窗口提交几何参数并绑定epoch/SHA，`0x500`为测量统计，`0x600`为按键，`0x700`为截面测量。寄存器读写语义见[AXI源码](src/fpga/rtl/axi_packet_controller.sv)和[驱动](src/ps/r33/)。加载后须读回实际ABI；参数改变会撤销旧准入，完成有效提交后才能开始处理。

## 构建与板端启动

```powershell
python -B build/reproduce.py --work ../scan3d_build_work --output ../scan3d_build_output --vivado vivado
python -B skill/pynq_release_check.py --manifest board/firmware/manifest.json --runtime-root src/ps
```

完整构建从恢复后的`fpga/scripts/build_final.tcl`开始，默认双方向通道、四个dispatch/banking开关为0，不静默升级IP。输出含日志、资源、setup/hold、脉宽、DRC、CDC报告及bit/HWH和manifest。历史seedretry脚本依赖未提供的route.dcp，请使用上述完整入口。

板端需要PYNQ镜像及对应ARM/Python/PYNQ环境。先核对板卡身份、实际接线、当前内参/光平面/转轴/转角、原生库架构、磁盘空间和回退副本，再按[板端入口](board/readme.md)部署匹配的固件与runtime。`board/boot_reference/`含本装置的启动服务、旧路径与自动演示配置，移植时先修改设备配置，不直接覆盖另一块板的`/boot`。

在恢复后并部署到板端的runtime目录查看真实入口参数，再按当前装置配置启动：

```sh
python -B -m r33.live --help
python -B -m r33.replay --help
```

`live`用于相机实时运行，`replay`用于真实PL回灌；具体固件、数据与配置参数以`--help`为准。首次静态采集与加载时断开电机12 V，再依次验证ABI、静态采集、PL回灌、HDMI、完整保存和受控运动。真实运动前确认机械固定、线缆、供电和有效标定。

BTN1、BTN2、BTN3分别直接进入实时、截面和扫描页；BTN0发出开始/停止扫描请求，由状态机及运动准入决定是否执行。按键不替代固件、标定和运动准入。

## 数据与验证记录

每帧保存Raw灰度、primary硬件流、观测、measurement XYZ、preview XYZ、回执与metadata/trace，保留帧号、epoch、参数身份、角度、错误和耗时。正常质量取自PL摘要；`--diagnostic-raw-quality`显式启用Raw饱和诊断，不改变测量接受判据。文件布局见[存储实现](src/ps/r33/storage.py)。入队、处理完成和持久化分别计数，事务flush/fsync后才发布持久化确认；取消时保留已接受、失败和未完成范围。

| 随包验证内容 | 历史结果 |
|---|---|
| 完整运行时 | 279项通过，覆盖协议、几何、界面、存储与故障处理 |
| 入口与三页界面 | 71项通过，属于完整运行时测试范围 |
| 软件固件配套 | 11项通过，核对27份数据包、接口及原生库架构 |
| 通用PYNQ Skill | 4项离线复用测试通过，覆盖两个无关合成工程、驱动混用拒绝、目录迁移与独立调用 |
| 截面测量RTL | 195包与定点模型逐位一致 |
| 整链RTL | 19例全尺寸输入通过，覆盖空帧、噪声、饱和及候选溢出 |

逐项结果见[验证汇总](report/release_validation.json)及[固件配套记录](board/firmware/runtime_validation.json)。[板端历史数据](board/readme.md)包含回执与量化点云恢复件，其范围以来源清单为准，不等同于完整原始采集。

## 排障

| 现象 | 检查入口 |
|---|---|
| 恢复失败、字体或大小写不匹配 | 使用新的恢复目录，核对source_map和依赖 |
| 构建找不到工具或器件 | Vivado 2025.2、XC7Z020支持、PATH |
| 固件检查或ABI失败 | bit/HWH、manifest、驱动、实际MMIO与ARM库架构 |
| 相机ID/PCLK异常 | 供电、XDC与实际接线、复位、相机诊断寄存器 |
| HDMI黑屏或延迟 | 视频时钟、VDMA、显示缓冲与提交确认时间 |
| 内存不足或保存停顿 | 普通内存与CMA分别检查；记录分配结果、同步耗时、队列帧数/字节数和磁盘余量 |
| 点云环带、错层或台阶异常 | 光平面、转轴、实际转角、单位、参数来源及物体/参考台面/背景分离 |

定位时保留源码提交、固件/库身份、配置、完整命令、输入和原始错误日志。相机、激光或转台位置改变后复核标定。

## 仓库导航

| 目录 | 内容 |
|---|---|
| [src/](src/) | FPGA RTL、XDC、BD、ARM运行时、原生C、字体与必要IP |
| [sim/](sim/readme.md) | 离线/RTL测试、输入、结果及`sim/tools/`仓库校验工具 |
| [build/](build/) | 文件名恢复、完整构建入口及实现报告 |
| [board/](board/readme.md) | 固件、manifest、板端证据与启动参考 |
| [data/](data/) | 标定、输入与参考数据 |
| [skill/](skill/readme.md) | 可复用包校验、固件/runtime绑定检查 |
| [report/](report/design_report.pdf) | 技术报告、图像、验证与AI协作记录 |

[技术报告PDF](report/design_report.pdf) · [可编辑Word](report/design_report.docx) · [AI协作记录](report/ai_collaboration_records.md) · [可复用工具开发](report/skill_development.md)

## 许可证

自有新代码、测试和文档沿用仓库[MIT许可证](license.md)。厂商IP、外部参考代码及字体保留原许可，详见[第三方说明](report/third_party_notices.md)。

[中英文海报PPTX](report/images/poster.pptx)
