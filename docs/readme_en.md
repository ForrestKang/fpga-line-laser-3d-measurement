# Intelligent Line-Laser 3D Measurement System

**A compact PYNQ-Z2 instrument for non-contact profile measurement and rotary 3D scanning.**

[![简体中文](media/language_zh_inactive.svg)](../readme.md) [![English](media/language_en_active.svg)](readme_en.md)

[Quick start](getting_started.md) · [Architecture](architecture.md) · [Hardware](hardware.md) · [Build and deployment](build_and_deploy.md) · [Validation](validation.md)

![System hardware: PYNQ-Z2, line laser and turntable](media/system_photo.png)

## What the system does

An MT9V034 camera observes a line laser on the object. The FPGA extracts the stripe centre, reconstructs 3D coordinates and computes section measurements. The display shows the current profile and the point cloud as the scan grows. The ARM runtime coordinates acquisition and rotation, manages the interface, stitches scans and saves complete records.

The prototype targets small parts, stepped surfaces and rotary shapes. It combines the camera, laser, turntable and HDMI display around one PYNQ-Z2 board.

- **An integrated PL processing path.** Shared-window filtering, directional centre refinement, stripe extraction, branch linking, Brown-model inverse distortion, laser-plane intersection and axis transformation run in programmable logic.
- **Hardware measurement and rendering.** The Measurement Statistics, Section Measurement and Cloud Renderer modules consume the reconstructed points.
- **Separate measurement and preview results.** Each set retains its acceptance flags, rejection reasons and XYZ records. Preview points do not enter the formal measurement set automatically.
- **Traceable data.** Raw images, hardware streams, observations, 3D points, parameter identity and error states remain available for readback and diagnosis.
- **A complete engineering handoff.** This repository includes RTL, constraints, Block Design reconstruction, ARM and native C sources, firmware, tests, reports and restoration/build entry points.

## Three operating pages

These are interface images generated directly by the page renderer from archived data.

### Live view · BTN1

Camera stripe, point cloud, processing state and quality in one view.

![Live view](media/live_page.png)

### Section measurement · BTN2

Section profile, individual step heights, noise and quality trends.

![Section measurement](media/section_page.png)

### Rotary scan · BTN3

Accumulated cloud, angular progress, diameter profile and scan records.

![Rotary scan](media/scan_page.png)

## Hardware/software partition

![Pixel processing, geometry, ARM runtime and display partition](media/system_architecture.svg)

| Programmable logic | ARM processing system |
|---|---|
| Camera Capture and CDC FIFO | Device ownership and configuration |
| Shared Window Filter and Direction Refinement | Frame/parameter identity validation and record decoding |
| Stripe Extraction and Branch Linking | Scan control and angle coordination |
| Fixed-point Geometry and Point Cache | Scan-level stitching and cloud management |
| Measurement Statistics and Section Measurement | Interface composition, storage and export |
| Cloud Renderer and video overlay | Quality decisions and result management |

The normal path receives XYZ from the PL geometry engine. ARM validates and decodes these records instead of repeating the ray, distortion and rotation calculations. The default build uses **two direction lanes**. The geometry engine shares DSP arithmetic through a bounded state machine, with one point in flight at a time.

Image centres use Q12 encoding; geometry uses Q30 internally and emits signed Q16 coordinates. These formats describe numerical representation, not physical measurement accuracy. See [Architecture](architecture.md) for frame identity, rounding, clock domains and DMA connections.

## Platform and implementation

| Item | Configuration / archived result |
|---|---|
| Board / FPGA | PYNQ-Z2 / XC7Z020 |
| Camera input | 752 × 480, 8-bit grayscale |
| User interface | 1280 × 720 HDMI |
| Implementation tool | Vivado 2025.2 |
| System / video domain | 100 / 125 MHz |
| LUTs | 39,111 · 73.52% |
| Flip-flops | 50,902 · 47.84% |
| Block RAM | 78.93% |
| DSP slices | 134 · 60.91% |
| Post-route timing | WNS +0.230 ns · WHS +0.011 ns |
| Critical CDC findings | 0 in the saved report |

The figures come from the [archived implementation report review](../report/source_report_gate.json). This documentation update did not rerun implementation or board measurements. PL processing, complete scan throughput, display updates and durable storage are reported separately; see [Results and measurement definitions](results.md).

## Quick start without a board

Python 3.10 or later is sufficient for repository and utility checks. These commands do not connect to hardware or require Vivado:

```sh
git clone https://github.com/ForrestKang/pynq-structured-light.git
cd pynq-structured-light
python -B tools/check_repository.py
python -B skill/verify_bundle.py .
python -B skill/test_pynq_release_check.py
```

Restore the original source filenames and directory layout, then run the offline software tests:

```sh
python -B build/restore_sources.py --output ../scan3d_work
python -m pip install -r sim/requirements_pc.txt
python -B sim/run_release_tests.py --work ../scan3d_work --output ../scan3d_pc_results --cc gcc
```

The complete runtime suite requires a host C compiler and the declared Python dependencies, including OpenCV for image and display tests. Host-built libraries are PC test helpers, not ARM binaries. Output directories must not already exist. The restoration map preserves the original names and case-sensitive paths. Read [Getting started](getting_started.md) for the full section and integration RTL commands.

## Rebuild and deployment

Install Vivado 2025.2 with XC7Z020 support, then use a separate pair of new directories:

```sh
python -B build/reproduce.py --work ../scan3d_build_work --output ../scan3d_build_output --vivado vivado
```

The entry point restores the sources and invokes the complete saved Tcl flow. It does not load a board. Review timing, resources, DRC, CDC and firmware/runtime binding before deployment. The repaired software has a separate [runtime validation record](../board/firmware/runtime_validation.json); the [earlier manifest](../board/firmware/history/manifest_before_repair.json) and hardware source-freeze record are retained.

Read [Build and deployment](build_and_deploy.md) and [Hardware interfaces](hardware.md) before operating the board. Boot files are device-specific references. Calibration, wiring, laser control and motion authority do not transfer automatically to another setup.

## Current delivery status

| Scope | Status |
|---|---|
| Sources, interfaces and restoration/build entry points | Collected; the source layout can be restored into a new directory |
| Repository integrity and release-check utilities | Checked; repository CI covers this offline scope |
| Software repair | Complete protocol Mock, verified fault injection, PL-only default quality, strict versioned journal readback |
| Complete runtime regression | Final execution counts and outcomes are in the [validation summary](../report/release_validation.json) |
| Firmware/runtime pairing | 11 offline pairing tests and 27 archived primary packet cases passed; repaired runtime binding recorded separately |
| Section RTL | 195 packets passed bitwise comparison: 182 historical camera frames plus 13 synthetic inputs |
| Integration RTL, implementation and board operation | Matrix results are in the validation summary; no new place-and-route or board run in this repair |

Default frame quality uses PL summaries. Raw saturation comparison requires `--diagnostic-raw-quality` and does not change acceptance decisions; the existing initial-exposure probe remains independent. Strict journal readback explicitly selects the image-only or geometry-bearing decoder and supports the horizontal-pair policy. All 45 non-generated hardware build inputs and the bit/HWH remain unchanged. [Known issues](known_issues.md) separates PC verification from the board and physical checks still required.

## Repository guide

| Directory | Contents |
|---|---|
| [docs/](readme.md) | Architecture, interfaces, setup, operation, validation and troubleshooting |
| [src/fpga/](../src/fpga) | RTL, XDC, BD Tcl, models, original build scripts and required IP |
| [src/ps/](../src/ps) | ARM runtime, native C and libraries, fonts and tests |
| [build/](../build) | Source restoration, full build entry point and archived implementation reports |
| [sim/](../sim/readme.md) | Offline tests, RTL entry points, fixtures and results |
| [board/](../board/readme.md) | bit/HWH, frozen manifest, board evidence and boot references |
| [data/](../data) | Calibration, test input and reference data |
| [skill/](../skill/readme.md) | Reusable package and firmware/runtime binding checks |
| [report/](../report/readme.md) | [Design report PDF](../report/design_report.pdf), [editable Word report](../report/design_report.docx), AI collaboration and submission records |
| [tools/](../tools) | Repository structure, link and syntax checks |

See [Operation and data](operation.md) for page controls, measurement/preview records and completion states. See [Submission materials](submission.md) for the report, reusable Skill, AI collaboration cases and anonymous English poster. Large raw videos and full SD-card images stay outside the source repository.

## Contributing and support

Follow [Contributing](../contributing.md) when changing code or interfaces. Preserve frozen inputs and failure evidence, use new output directories, and report the actual checks performed. Include the commit, configuration, firmware identity, reproduction command and original error when reporting a problem. [Troubleshooting](troubleshooting.md) covers common build, camera, display, storage and geometry issues.

## License

Original project code, tests and documentation retain the repository's [MIT license](../license.md). Vendor IP, external reference code and fonts retain their original licenses; see [Third-party notices](../report/third_party_notices.md).
