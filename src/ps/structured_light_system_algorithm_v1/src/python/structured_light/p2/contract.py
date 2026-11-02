"""P2 board addresses, register semantics, and packet-size constants.

The values mirror ``src/contracts/p2_board.json``.  The machine-readable
contract is required at runtime; constants in this module are only the typed
baseline used by the test fixture and are checked against that file whenever
it is loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .errors import ContractError

from ..generated import (
    REG_CONTROL, REG_CURRENT_ANGLE, REG_ERROR_FLAGS, REG_FIFO_LEVEL, REG_IRQ_ENABLE,
    REG_IRQ_STATUS, REG_MECH_STEPS_HIGH, REG_MECH_STEPS_LOW, REG_PROFILE_COUNT,
    REG_PROFILE_ID, REG_ROI_END, REG_ROI_START, REG_STATUS, REG_THRESHOLD,
    REG_TIMESTAMP_HIGH, REG_TIMESTAMP_LOW, REG_VERSION, REG_MIN_WIDTH, REG_MAX_WIDTH,
    REG_MIN_SUM, REG_MIN_PEAK, REG_STEPS_PER_PROFILE, REG_STEPS_PER_REV, REG_MICROSTEPS,
    REG_GEAR_NUM, REG_GEAR_DEN, REG_TIMEOUT_CYCLES, REG_STEP_PERIOD, REG_SETTLE_CYCLES,
    REG_LASER_DELAY, REG_TRIGGER_WIDTH,
)


CORE_BASE = 0x43C0_0000
DIAGNOSTICS_BASE = 0x43C1_0000
RAW_DMA_BASE = 0x4040_0000
PROFILE_DMA_BASE = 0x4041_0000
IP_WINDOW = 0x1_0000
MAX_PROFILE_BYTES = 8192
MAX_SLP2_PACKET_BYTES = (8 + 3 * 480) * 4
SYSTEM_CLOCK_HZ = 100_000_000
REGMAP_VERSION = 0x0001_0000
DIAGNOSTIC_VERSION = 0x0001_0000
SUPPORTED_DIAGNOSTIC_VERSIONS = (0x0001_0000, 0x0001_0001)
PROFILE_OVERLAY_MODES = {"structured_light_profile", "structured_light_profile_hdmi"}
RAW_OVERLAY_MODES = {"camera_raw", "camera_raw_monitor", "camera_raw_hdmi"}
PROFILE_FORMAT_VERSION = 2
TARGET_DEVICE = "xc7z020clg400-1"

DIAG_OFFSETS = {
    "version": 0x00, "capabilities": 0x04, "ready": 0x08, "pclk_hz": 0x0C,
    "width": 0x10, "height": 0x14, "last_line": 0x18, "last_frame": 0x1C,
    "frame_count": 0x20, "camera_device_address": 0x24, "camera_register_address": 0x28,
    "camera_write_data": 0x2C, "camera_read_data": 0x30, "camera_command": 0x34,
    "camera_status": 0x38, "camera_control": 0x3C, "abort": 0x40, "epoch": 0x44,
    "request_id": 0x48, "row_count": 0x4C, "error": 0x50, "w1c": 0x54,
    "row_table": 0x1000,
}

REGISTER_NAMES = {
    REG_CONTROL: "CONTROL", REG_STATUS: "STATUS", REG_THRESHOLD: "THRESHOLD",
    REG_ROI_START: "ROI_START", REG_ROI_END: "ROI_END", REG_MIN_WIDTH: "MIN_WIDTH",
    REG_MAX_WIDTH: "MAX_WIDTH", REG_STEPS_PER_PROFILE: "STEPS_PER_PROFILE",
    REG_SETTLE_CYCLES: "SETTLE_CYCLES", REG_LASER_DELAY: "LASER_DELAY",
    REG_TRIGGER_WIDTH: "TRIGGER_WIDTH", REG_PROFILE_COUNT: "PROFILE_COUNT",
    REG_ERROR_FLAGS: "ERROR_FLAGS", REG_CURRENT_ANGLE: "CURRENT_ANGLE",
    REG_TIMESTAMP_LOW: "TIMESTAMP_LOW", REG_TIMESTAMP_HIGH: "TIMESTAMP_HIGH",
    REG_STEPS_PER_REV: "STEPS_PER_REV", REG_MICROSTEPS: "MICROSTEPS",
    REG_GEAR_NUM: "GEAR_NUM", REG_GEAR_DEN: "GEAR_DEN", REG_TIMEOUT_CYCLES: "TIMEOUT_CYCLES",
    REG_STEP_PERIOD: "STEP_PERIOD", REG_MIN_SUM: "MIN_SUM", REG_MIN_PEAK: "MIN_PEAK",
    REG_PROFILE_ID: "PROFILE_ID", REG_IRQ_STATUS: "IRQ_STATUS", REG_IRQ_ENABLE: "IRQ_ENABLE",
    REG_MECH_STEPS_LOW: "MECH_STEPS_LOW", REG_MECH_STEPS_HIGH: "MECH_STEPS_HIGH",
    REG_FIFO_LEVEL: "FIFO_LEVEL", REG_VERSION: "VERSION",
}

REGISTER_RESETS = {
    REG_CONTROL: 0, REG_THRESHOLD: 30, REG_ROI_START: 0, REG_ROI_END: (480 << 16) | 640,
    REG_MIN_WIDTH: 1, REG_MAX_WIDTH: 32, REG_STEPS_PER_PROFILE: 1, REG_SETTLE_CYCLES: 1000,
    REG_LASER_DELAY: 100, REG_TRIGGER_WIDTH: 10, REG_PROFILE_COUNT: 1,
    REG_STEPS_PER_REV: 200, REG_MICROSTEPS: 16, REG_GEAR_NUM: 1, REG_GEAR_DEN: 1,
    REG_TIMEOUT_CYCLES: 50_000_000, REG_STEP_PERIOD: 1000, REG_MIN_SUM: 30, REG_MIN_PEAK: 15,
    REG_IRQ_ENABLE: 7, REG_VERSION: 0x0001_0000,
}

READ_ONLY_REGS = {
    REG_STATUS, REG_ERROR_FLAGS, REG_CURRENT_ANGLE, REG_TIMESTAMP_LOW, REG_TIMESTAMP_HIGH,
    REG_PROFILE_ID, REG_MECH_STEPS_LOW, REG_MECH_STEPS_HIGH, REG_FIFO_LEVEL, REG_VERSION,
}
CONFIG_REGS = set(REGISTER_NAMES) - READ_ONLY_REGS - {REG_CONTROL, REG_IRQ_STATUS}
IRQ_MASK = 0x7


@dataclass(frozen=True, slots=True)
class BoardContract:
    control_base: int = CORE_BASE
    diagnostics_base: int = DIAGNOSTICS_BASE
    raw_dma_base: int = RAW_DMA_BASE
    profile_dma_base: int = PROFILE_DMA_BASE
    window: int = IP_WINDOW

    def as_dict(self) -> dict[str, int]:
        return {
            "control_base": self.control_base,
            "diagnostics_base": self.diagnostics_base,
            "raw_dma_base": self.raw_dma_base,
            "profile_dma_base": self.profile_dma_base,
            "window": self.window,
        }


def _find_project_root() -> Path | None:
    for candidate in (Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parents[4]):
        if (candidate / "src/contracts/interface.json").is_file():
            return candidate
    return None


def load_board_contract(path: str | Path | None = None) -> BoardContract:
    source = Path(path) if path is not None else ((_find_project_root() or Path.cwd()) / "src/contracts/p2_board.json")
    if not source.is_file():
        raise ContractError(f"P2 board contract not found: {source}")
    try:
        raw: Any = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read P2 board contract {source}: {exc}") from exc
    # Accept either the concise ``addresses`` mapping or the direct mapping.
    addresses = raw.get("address_map", raw.get("addresses", raw)) if isinstance(raw, dict) else {}
    aliases = {
        "control_base": ("control", "pipeline_control"),
        "diagnostics_base": ("diagnostics", "camera_diagnostics"),
        "raw_dma_base": ("raw_dma",),
        "profile_dma_base": ("profile_dma",),
    }
    values: dict[str, int] = {}
    for field, names in aliases.items():
        value = next((addresses.get(name) for name in names if isinstance(addresses, dict) and name in addresses), None)
        if isinstance(value, dict):
            value = value.get("base", value.get("address"))
        if isinstance(value, str):
            value = int(value, 0)
        if type(value) is not int:
            raise ContractError(f"P2 board contract is missing integer {field}")
        values[field] = value
    window = addresses.get("window", raw.get("window", IP_WINDOW)) if isinstance(addresses, dict) else IP_WINDOW
    if isinstance(addresses, dict) and "window" not in addresses:
        ranges = []
        for value in addresses.values():
            if isinstance(value, dict) and value.get("range") is not None:
                ranges.append(value.get("range"))
        if ranges and all(type(item) is int for item in ranges):
            window = ranges[0]
    if isinstance(window, str):
        window = int(window, 0)
    if type(window) is not int or window <= 0:
        raise ContractError("P2 board contract window must be a positive integer")
    contract = BoardContract(**values, window=window)
    expected = BoardContract()
    if contract != expected:
        raise ContractError(f"P2 board address contract differs from frozen constants: {contract} != {expected}")
    return contract


__all__ = [
    "CORE_BASE", "DIAGNOSTICS_BASE", "RAW_DMA_BASE", "PROFILE_DMA_BASE", "IP_WINDOW",
    "MAX_PROFILE_BYTES", "MAX_SLP2_PACKET_BYTES", "SYSTEM_CLOCK_HZ", "REGMAP_VERSION",
    "DIAGNOSTIC_VERSION", "PROFILE_FORMAT_VERSION", "TARGET_DEVICE", "DIAG_OFFSETS",
    "REGISTER_NAMES", "REGISTER_RESETS", "READ_ONLY_REGS", "CONFIG_REGS", "IRQ_MASK",
    "BoardContract", "load_board_contract",
]
