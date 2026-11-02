# 智能线激光三维测量系统

**Intelligent Line-Laser 3D Measurement System**

基于 **PYNQ-Z2** 的小型线激光三维测量装置：相机采集激光条纹，FPGA完成亚像素提取、定点几何和截面统计，屏幕显示当前截面与逐步形成的点云，ARM负责扫描编排、界面、拼接与完整数据保存。

[![简体中文](docs/media/language_zh_active.svg)](readme.md) [![English](docs/media/language_en_inactive.svg)](docs/readme_en.md)

[快速开始](docs/getting_started.md) · [系统架构](docs/architecture.md) · [硬件接口](docs/hardware.md) · [构建与部署](docs/build_and_deploy.md) · [验证结果](docs/validation.md)

![系统硬件：开发板、相机、线激光与转台](docs/media/system_photo.png)

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

![实时观测界面](docs/media/live_page.png)

### 截面测量 · BTN2

显示截面轮廓、逐级台阶高度、噪声与质量趋势。

![截面测量界面](docs/media/section_page.png)

### 整圈扫描 · BTN3

显示累积点云、角度进度、分层直径和扫描记录。

![整圈扫描界面](docs/media/scan_page.png)

## 处理分工

![PL与ARM处理分工](docs/media/system_architecture.svg)

| 处理侧 | 主要职责 |
|---|---|
| PL | 像素提取、分支与质量、定点几何、截面测量、点云叠加 |
| ARM | 参数与扫描编排、数据校验、扫描级拼接、页面组织、完整保存 |

正常几何坐标由PL产生，ARM解包时核对参数与帧身份。方向核默认两路；几何核复用DSP并采用有界状态机，并非每拍输出一个三维点。详细模块、位宽和时钟域见[系统架构](docs/architecture.md)。

## 实测效果

| 项目 | 结果 |
|---|---|
| 图像处理 | FPGA随相机逐帧处理，约26.5帧/s |
| 整圈扫描 | 200个视角，约2.5分钟，约6.6万个测量点 |
| 台阶高差 | 卡尺值2、3、5 mm；截面读数2.00、2.90、4.96 mm |
| 阶梯圆柱 | 三段直径扫描值39.68、29.90、19.88 mm |
| 波纹花瓶 | 颈部卡尺值16.5 mm；扫描值16.23～16.55 mm |

标定方法、测量图像和尺寸对照见[技术报告](report/design_report.pdf)。

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

实现结果见[资源与时序报告](report/source_report_gate.json)。系统完成像素处理、三维重建、截面测量、HDMI显示与完整数据保存，处理速度和实物测量结果见[验证结果](docs/validation.md)。

## 先在电脑运行

Python 3.10及以上；先检查包和工具，不需要板卡或Vivado：

```powershell
git clone https://github.com/ForrestKang/pynq-structured-light.git
cd pynq-structured-light
python -B tools/check_repository.py
python -B skill/verify_bundle.py .
python -B skill/test_pynq_release_check.py
```

接着恢复原工程大小写和目录：

```powershell
python -B build/restore_sources.py --output ../scan3d_work
python -m pip install -r sim/requirements_pc.txt
python -B sim/run_release_tests.py --work ../scan3d_work --output ../scan3d_pc_results --cc gcc
```

完整软件回归需要本机C编译器；依赖包括OpenCV，用于真实图像和显示测试。构建出的主机动态库仅用于电脑验证，不能复制到ARM运行。输出目录必须尚不存在。截面195包与整板RTL矩阵的复现命令见[快速开始](docs/getting_started.md)。

## 工程内容与测试结果

| 内容 | 结果 |
|---|---|
| 完整工程 | RTL、XDC、BD、ARM运行程序、原生C、bit/HWH、字体及构建脚本齐全 |
| 运行时测试 | 279项通过，覆盖协议、几何、界面、存储与故障处理 |
| 入口与三页界面 | 71项回归通过，包含按键、截面和扫描流程 |
| 软件固件配套 | 11项测试通过，核对27份输出数据包、接口及原生库架构 |
| 截面测量RTL | 195包与定点模型逐位一致 |
| 整链RTL | 19例全尺寸输入通过，覆盖空帧、噪声、饱和及候选溢出 |

测试记录见[验证汇总](report/release_validation.json)，软件与固件对应关系见[配套记录](board/firmware/runtime_validation.json)。

## 仓库导航

| 目录 | 内容 |
|---|---|
| [docs/](docs/) | 快速开始、系统架构、硬件接口、部署、操作与排障 |
| [src/fpga/](src/fpga) | RTL、XDC、BD、模型、原构建脚本与必要IP |
| [src/ps/](src/ps) | ARM运行时、原生库与C源码、字体和测试 |
| [build/](build) | 原文件名恢复、一键构建和既有实现报告 |
| [sim/](sim/readme.md) | 离线回归、RTL入口、测试输入与结果 |
| [board/](board/readme.md) | bit/HWH、冻结manifest、板端证据、启动参考 |
| [data/](data) | 标定、输入与参考数据 |
| [skill/](skill/readme.md) | 可复用包校验、固件/runtime绑定检查 |
| [report/](report/readme.md) | [技术报告PDF](report/design_report.pdf)、[Word](report/design_report.docx)及AI协作记录 |
| [tools/](tools) | 仓库链接、语法及材料自检 |

技术报告见[报告目录](report/readme.md)，代码修改规范见[Contributing](contributing.md)。

## 许可证

自有新代码、测试和文档沿用仓库[MIT许可证](license.md)。厂商IP、外部参考代码及字体保留原许可，详见[第三方说明](report/third_party_notices.md)。
