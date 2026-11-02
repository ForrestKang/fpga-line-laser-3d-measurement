"""Strict PYNQ Stage B runtime adapter.

This module is deliberately safe to import on a workstation.  Static artifact
and HWH checks happen before a PYNQ import, an Overlay construction, or an
MMIO access.  The board implementation reuses the existing P2 transport and
controller; this file only owns the MFP1/BSG1 adapters, Stage B admission,
measured settling, and the display-facing result adapter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from numbers import Integral
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Mapping, Sequence

from .guard import GuardController
from .lifecycle import HardwareProcessLock
from .motor_control import MotorController
from .orchestrator import MotionSession
from .settle import (
    SettleConfig,
    SettleEstimate,
    brightness_normalized_difference,
    estimate_settle,
    phase_correlation_shift,
)
from .settle_protocols import (
    EXTENDED_SETTLE_PROTOCOL_ID,
    LEGACY_SETTLE_PROTOCOL_ID,
    SETTLE_PROTOCOL_IDS,
    get_settle_protocol,
    validate_settle_protocol,
)
from .types import CarrierMode, MotionConfig


PROJECT_ROOT = Path(__file__).resolve().parents[4]
FRAME_WIDTH = 752
FRAME_HEIGHT = 480
HDMI_WIDTH = 1280
HDMI_HEIGHT = 720
HDMI_STRIDE = HDMI_WIDTH * 3
TARGET_DEVICE = "xc7z020clg400-1"
OVERLAY_NAME = "structured_light_motion_profile_hdmi"
OVERLAY_MODE = "structured_light_profile_hdmi"
P2_OVERLAY_ADDRESSES = {
    "pipeline_control": 0x43C0_0000,
    "camera_diagnostics": 0x43C1_0000,
    "raw_dma": 0x4040_0000,
    "profile_dma": 0x4041_0000,
    "hdmi_vdma": 0x4042_0000,
    "hdmi_vtc": 0x43C2_0000,
    "hdmi_dynclk": 0x43C3_0000,
}
MOTION_ADDRESSES = {
    "motor_controller": 0x43C4_0000,
    "bsg1_guard": 0x43C5_0000,
}
ALL_ADDRESSES = {**P2_OVERLAY_ADDRESSES, **MOTION_ADDRESSES}
ADDRESS_RANGE = 0x1_0000
SOURCE_HASH_LENGTH = 64


class StageBRuntimeError(RuntimeError):
    """Base error for a rejected Stage B runtime operation."""


def prepare_camera_control(diag: Any, *, initialize: bool, timeout_s: float = 2.0,
                           sleep: Callable[[float], None] = time.sleep,
                           monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Admit configuration writes only after the camera clock/FIFO is ready.

    The retained RTL powers up with CAM_CTRL=1 (sensor held in reset), and
    rejects control writes while core_busy includes not-ready. P2Controller's
    constructor writes PROFILE_COUNT at 0x2c; on ARM Linux that SLVERR becomes
    SIGBUS. Therefore this ordering is required before constructing it.
    An attach never implicitly resets an existing capture.
    """
    if initialize:
        diag.write(0x3c, 1)
        sleep(0.001)
        diag.write(0x3c, 0)
        sleep(0.1)
    deadline = monotonic() + timeout_s
    samples = []
    while True:
        status = int(diag.read(0x08))
        samples.append(status)
        # Full ready additionally requires STEPS_PER_PROFILE=0, which is
        # written by P2Controller next. Before that write only the hardware
        # busy bit must be clear, otherwise admission would be circular.
        if not status & 4:
            return {'initialized':initialize,'status':status,'samples':samples,
                    'ready':bool(status & 1),'configuration_writes_allowed':True}
        if monotonic() >= deadline:
            raise RuntimeAdmissionError('camera data path is not ready; no control configuration write was issued')
        sleep(0.005)


class ArtifactContractError(StageBRuntimeError, ValueError):
    """An artifact, manifest, HWH, or source binding failed closed."""


class RuntimeAdmissionError(StageBRuntimeError, PermissionError):
    """The caller did not supply the explicit board-load admission evidence."""


class SettleBindingError(StageBRuntimeError, ValueError):
    """The saved measured settling configuration is absent or incompatible."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _integer(value: Any, name: str) -> int:
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactContractError(f"{name} must be an integer") from exc
    return parsed


def _manifest_value(record: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in record:
            return record[name]
    source = record.get("source")
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
    return None


def _address(record: Mapping[str, Any], name: str) -> int:
    addresses = record.get("addresses", record.get("address_map", {}))
    value = addresses.get(name) if isinstance(addresses, Mapping) else None
    if isinstance(value, Mapping):
        value = value.get("base", value.get("address"))
    return _integer(value, f"addresses.{name}")


def _resolve_manifest_file(manifest: Path, record: Mapping[str, Any], key: str) -> Path:
    files = record.get("files", record.get("artifacts", {}))
    entry = files.get(key) if isinstance(files, Mapping) else None
    value = entry.get("path") if isinstance(entry, Mapping) else entry
    if not isinstance(value, str) or not value:
        raise ArtifactContractError(f"manifest files.{key}.path is missing")
    path = Path(value)
    return path if path.is_absolute() else manifest.parent / path


def _resolve_contract_path(manifest: Path, record: Mapping[str, Any], explicit: str | Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    value = _manifest_value(record, "contract_file")
    if isinstance(value, str):
        path = Path(value)
        candidates.append(path if path.is_absolute() else manifest.parent / path)
    candidates.extend((
        manifest.parent / "p2_board.json",
        PROJECT_ROOT / "src" / "contracts" / "p2_board.json",
    ))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ArtifactContractError("manifest contract_file does not resolve to p2_board.json")


def _parse_hwh(path: Path) -> tuple[tuple[dict[str, Any], ...], dict[str, dict[str, list[str]]], dict[str, str]]:
    try:
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ArtifactContractError(f"cannot parse HWH {path}: {exc}") from exc

    ranges: list[dict[str, Any]] = []
    params: dict[str, dict[str, list[str]]] = {}
    systeminfo: dict[str, str] = {}
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].upper()
        attrs = {key.upper(): value for key, value in node.attrib.items()}
        if tag == "SYSTEMINFO" and not systeminfo:
            systeminfo = attrs
        if tag == "MEMRANGE" and "BASEVALUE" in attrs:
            try:
                item: dict[str, Any] = {
                    "base": int(attrs["BASEVALUE"], 0),
                    "instance": attrs.get("INSTANCE", ""),
                    "master": attrs.get("MASTERBUSINTERFACE", ""),
                    "slave": attrs.get("SLAVEBUSINTERFACE", ""),
                }
                if "HIGHVALUE" in attrs:
                    item["high"] = int(attrs["HIGHVALUE"], 0)
            except ValueError as exc:
                raise ArtifactContractError(f"HWH memory range is not an integer: {attrs}") from exc
            ranges.append(item)
        if tag == "MODULE":
            instance = attrs.get("INSTANCE", attrs.get("NAME", ""))
            if not instance:
                continue
            scoped = params.setdefault(instance, {})
            for child in node.iter():
                if child.tag.rsplit("}", 1)[-1].upper() != "PARAMETER":
                    continue
                child_attrs = {key.upper(): value for key, value in child.attrib.items()}
                name, value = child_attrs.get("NAME"), child_attrs.get("VALUE")
                if name is not None and value is not None:
                    scoped.setdefault(name, []).append(value)
    return tuple(ranges), params, systeminfo


def _single_parameter(params: Mapping[str, Mapping[str, Sequence[str]]], instance: str, name: str) -> int | None:
    values = params.get(instance, {}).get(name, ())
    if not values:
        return None
    parsed = tuple(_integer(value, f"HWH {instance}.{name}") for value in values)
    if len(set(parsed)) != 1:
        raise ArtifactContractError(f"HWH contains contradictory {instance}.{name} values")
    return parsed[0]


def _validate_source_hash(value: Any) -> None:
    values = (value,) if isinstance(value, str) else tuple(value.values()) if isinstance(value, Mapping) else ()
    if not values or any(
        not isinstance(item, str) or len(item) != SOURCE_HASH_LENGTH
        or any(char not in "0123456789abcdefABCDEF" for char in item)
        for item in values
    ):
        raise ArtifactContractError("manifest source_sha256 must contain valid SHA-256 values")


def _verify_motion_hwh(hwh_path: Path, record: Mapping[str, Any], *, raw_only: bool = False) -> dict[str, Any]:
    # R41 (runtime06): raw_only = the R40 b09+ Overlay without the legacy Profile path and
    # profile_dma. It requires PROFILE_ENABLE=0 and binds raw_dma only.
    ranges, params, systeminfo = _parse_hwh(hwh_path)
    if (
        systeminfo.get("DEVICE", "").lower(),
        systeminfo.get("PACKAGE", "").lower(),
        systeminfo.get("SPEEDGRADE", ""),
    ) != ("7z020", "clg400", "-1"):
        raise ArtifactContractError("HWH SYSTEMINFO does not target xc7z020clg400-1")
    binding_specs = {
        "pipeline_control": (("processing_pipeline", "S_AXI_CONTROL"),),
        "camera_diagnostics": (("processing_pipeline", "S_AXI_DIAG"),),
        "raw_dma": (("raw_dma", "S_AXI_LITE"),),
        "profile_dma": (("profile_dma", "S_AXI_LITE"),),
        "hdmi_vdma": (("hdmi_vdma", "S_AXI_LITE"),),
        "hdmi_vtc": (("hdmi_out_vtc_out", "ctrl"), ("hdmi_out/vtc_out", "ctrl")),
        "hdmi_dynclk": (("hdmi_out_axi_dynclk", "s00_axi"), ("hdmi_out/axi_dynclk", "s00_axi")),
        "motor_controller": (("motor_controller", "S_AXI"),),
        "bsg1_guard": (("bsg1_guard", "S_AXI"),),
    }
    if raw_only:
        del binding_specs["profile_dma"]
    checked: dict[str, Any] = {}
    for name, candidates in binding_specs.items():
        expected = ALL_ADDRESSES[name]
        if _address(record, name) != expected:
            raise ArtifactContractError(f"manifest address mismatch for {name}")
        matches = [
            item for item in ranges
            if int(item["base"]) == expected
            and str(item.get("master", "")).upper() == "M_AXI_GP0"
            and any(
                str(item.get("instance", "")).strip("/").upper() == instance.strip("/").upper()
                and str(item.get("slave", "")).upper() == slave.upper()
                for instance, slave in candidates
            )
        ]
        if not matches:
            raise ArtifactContractError(f"HWH exact M_AXI_GP0 binding missing for {name}")
        if any("high" in item and int(item["high"]) - int(item["base"]) + 1 != ADDRESS_RANGE for item in matches):
            raise ArtifactContractError(f"HWH range for {name} is not exactly 0x10000 bytes")
        checked[name] = {"base": expected, "range": ADDRESS_RANGE, "instance": matches[0]["instance"]}

    required_params = {
        ("processing_pipeline", "FRAME_WIDTH"): FRAME_WIDTH,
        ("processing_pipeline", "FRAME_HEIGHT"): FRAME_HEIGHT,
        ("processing_pipeline", "PROFILE_ENABLE"): 0 if raw_only else 1,
        ("motor_controller", "CLOCK_HZ"): 100_000_000,
        ("bsg1_guard", "CARRIER_MODE"): 0,
        ("bsg1_guard", "CLOCK_HZ"): 100_000_000,
        ("bsg1_guard", "HEARTBEAT_HALF_CYCLES"): 500_000,
        ("bsg1_guard", "LEASE_MIN_CYCLES"): 1_000_000,
        ("bsg1_guard", "LEASE_MAX_CYCLES"): 100_000_000,
    }
    for (instance, name), expected in required_params.items():
        actual = _single_parameter(params, instance, name)
        if actual != expected:
            raise ArtifactContractError(
                f"HWH {instance}.{name} must be readonly build value {expected}, got {actual!r}"
            )
    dma_params = {
        "C_INCLUDE_SG": 0, "C_INCLUDE_MM2S": 0, "C_INCLUDE_MM2S_DRE": 0,
        "C_INCLUDE_S2MM": 1, "C_INCLUDE_S2MM_DRE": 0,
        "C_M_AXI_S2MM_ADDR_WIDTH": 32, "C_M_AXI_S2MM_DATA_WIDTH": 64,
        "C_S_AXIS_S2MM_TDATA_WIDTH": 32, "C_SG_LENGTH_WIDTH": 20,
    }
    for instance in ("raw_dma",) if raw_only else ("raw_dma", "profile_dma"):
        for name, expected in dma_params.items():
            actual = _single_parameter(params, instance, name)
            if actual != expected:
                raise ArtifactContractError(f"HWH {instance}.{name}={actual!r}, expected {expected}")
    for name, expected in {
        "C_INCLUDE_SG": 0, "C_INCLUDE_MM2S": 1, "C_INCLUDE_S2MM": 0,
        "C_M_AXI_MM2S_DATA_WIDTH": 64, "C_M_AXIS_MM2S_TDATA_WIDTH": 32,
    }.items():
        actual = _single_parameter(params, "hdmi_vdma", name)
        if actual != expected:
            raise ArtifactContractError(f"HWH hdmi_vdma.{name}={actual!r}, expected {expected}")
    if _manifest_value(record, "carrier_mode", "mode") not in ("direct_wire", 0, "0"):
        raise ArtifactContractError("Stage B only admits the direct_wire carrier manifest")
    return {"ranges": checked, "readonly": {f"{instance}.{name}": value for (instance, name), value in required_params.items()}}


def verify_stage_b_artifacts(
    manifest_path: str | Path,
    *,
    bit_path: str | Path | None = None,
    hwh_path: str | Path | None = None,
    contract_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify the signed combined Overlay before any board access.

    The returned dictionary is evidence for callers and is also suitable for a
    JSON receipt.  Source hashes are validated for shape and provenance only;
    a newer host checkout is allowed to differ from the compiled RTL hash.
    """

    manifest = Path(manifest_path).resolve()
    if not manifest.is_file():
        raise ArtifactContractError(f"manifest file not found: {manifest}")
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactContractError(f"cannot read manifest {manifest}: {exc}") from exc
    if not isinstance(record, dict):
        raise ArtifactContractError("manifest must contain a JSON object")

    bit = Path(bit_path).resolve() if bit_path is not None else _resolve_manifest_file(manifest, record, "bit").resolve()
    hwh = Path(hwh_path).resolve() if hwh_path is not None else _resolve_manifest_file(manifest, record, "hwh").resolve()
    if not bit.is_file() or not hwh.is_file():
        raise ArtifactContractError("bitstream and HWH must both be present before load")
    if bit.parent != hwh.parent or bit.stem != hwh.stem:
        raise ArtifactContractError("bitstream and HWH must share one directory and base name")

    addresses = record.get("addresses", {}) if isinstance(record, dict) else {}
    raw_only = isinstance(addresses, dict) and "profile_dma" not in addresses
    expected_p2 = {k: v for k, v in P2_OVERLAY_ADDRESSES.items() if not (raw_only and k == "profile_dma")}
    from structured_light.p2.pynq_transport import verify_overlay_artifacts

    try:
        checked_record = verify_overlay_artifacts(bit, hwh, manifest, expected_addresses=expected_p2)
    except Exception as exc:
        if isinstance(exc, ArtifactContractError):
            raise
        raise ArtifactContractError(str(exc)) from exc
    record = checked_record

    overlay = _manifest_value(record, "overlay")
    mode = _manifest_value(record, "overlay_mode")
    if overlay not in (OVERLAY_NAME, OVERLAY_MODE):
        raise ArtifactContractError(f"manifest overlay must be {OVERLAY_NAME!r}")
    if mode != OVERLAY_MODE:
        raise ArtifactContractError(f"manifest overlay_mode must be {OVERLAY_MODE!r}")
    if _manifest_value(record, "preboard_passed") is not True:
        raise ArtifactContractError("manifest preboard_passed must be true before Overlay load")
    if str(_manifest_value(record, "part", "device")).lower() != TARGET_DEVICE:
        raise ArtifactContractError(f"manifest part/device must be {TARGET_DEVICE}")
    if _manifest_value(record, "carrier_mode", "mode") not in ("direct_wire", 0, "0"):
        raise ArtifactContractError("expansion_board requires a separate approved manifest")
    for name, expected in (("regmap_version", 0x10000), ("diagnostic_version", 0x10001), ("profile_format_version", 2)):
        if _integer(_manifest_value(record, name), name) != expected:
            raise ArtifactContractError(f"manifest {name} does not match the frozen contract")
    if _integer(_manifest_value(record, "frame_width"), "frame_width") != FRAME_WIDTH or _integer(_manifest_value(record, "frame_height"), "frame_height") != FRAME_HEIGHT:
        raise ArtifactContractError("Stage B requires a 752x480 frame")

    contract = _resolve_contract_path(manifest, record, contract_path)
    contract_sha = _manifest_value(record, "contract_sha256", "contract_sha")
    if not isinstance(contract_sha, str) or _sha256_file(contract).lower() != contract_sha.lower():
        raise ArtifactContractError("manifest contract_sha256 does not match p2_board.json")
    if Path(contract).name.lower() != "p2_board.json":
        raise ArtifactContractError("contract_file must be p2_board.json")
    _validate_source_hash(_manifest_value(record, "source_sha256", "source_combined_sha256", "source_hash"))
    motion_hwh = _verify_motion_hwh(hwh, record, raw_only=raw_only)
    return {
        "manifest": str(manifest),
        "bit": str(bit),
        "hwh": str(hwh),
        "contract": str(contract),
        "part": TARGET_DEVICE,
        "overlay": overlay,
        "overlay_mode": mode,
        "carrier_mode": "direct_wire",
        "frame_width": FRAME_WIDTH,
        "frame_height": FRAME_HEIGHT,
        "addresses": dict(ALL_ADDRESSES),
        "files": {
            "bit": {"sha256": _sha256_file(bit), "size": bit.stat().st_size},
            "hwh": {"sha256": _sha256_file(hwh), "size": hwh.stat().st_size},
            "manifest": {"sha256": _sha256_file(manifest), "size": manifest.stat().st_size},
        },
        "contract_sha256": _sha256_file(contract),
        "source_sha256": _manifest_value(record, "source_sha256", "source_combined_sha256", "source_hash"),
        "preboard_passed": True,
        "hwh_motion": motion_hwh,
    }


class MMIO32:
    """A 32-bit offset transport for MFP1/BSG1 register controllers."""

    def __init__(self, mmio: Any, *, base_address: int, address_range: int = ADDRESS_RANGE, name: str = "mmio") -> None:
        if not callable(getattr(mmio, "read", None)) or not callable(getattr(mmio, "write", None)):
            raise TypeError(f"{name} requires read(offset)/write(offset,value)")
        self.mmio = mmio
        self.base_address = int(base_address)
        self.address_range = int(address_range)
        self.name = name

    def _offset(self, offset: int) -> int:
        if type(offset) is not int or offset < 0 or offset >= self.address_range or offset & 3:
            raise ValueError(f"{self.name} offset must be an aligned 32-bit offset in its HWH window")
        return offset

    def read32(self, offset: int) -> int:
        return int(self.mmio.read(self._offset(offset))) & 0xFFFF_FFFF

    def write32(self, offset: int, value: int) -> None:
        self.mmio.write(self._offset(offset), int(value) & 0xFFFF_FFFF)

    def close(self) -> None:
        return None


def _overlay_mmio(overlay: Any, name: str, base: int, window: int = ADDRESS_RANGE) -> Any:
    supplied = getattr(overlay, "_p2_mmio", None)
    if isinstance(supplied, Mapping):
        value = supplied.get(name, supplied.get(base))
        if value is not None:
            return value
    try:
        from pynq import MMIO  # type: ignore
    except ImportError as exc:
        raise RuntimeAdmissionError(f"PYNQ MMIO is unavailable for {name}") from exc
    return MMIO(base, window)


def _power_off_confirmed(value: str | Path | Mapping[str, Any] | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        return (
            value.get("physical_power_off_confirmed") is True
            or value.get("power_off_confirmed") is True
            or value.get("power_off_confirmed_for_load") is True
        )
    if value is None:
        return False
    path = Path(value)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeAdmissionError(f"cannot read power-off confirmation {path}: {exc}") from exc
    return _power_off_confirmed(record)


def _loaded_overlay_receipt(value: str | Path | Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        raise RuntimeAdmissionError("attach requires a loaded-Overlay receipt")
    record = _read_confirmation(value, label="loaded Overlay receipt")
    if record.get("loaded") is not True and record.get("overlay_loaded") is not True:
        raise RuntimeAdmissionError("loaded Overlay receipt does not confirm an active Overlay")
    return record


def _current_pl_identity(overlay: Any, *, expected_bit: Path) -> dict[str, Any]:
    """Read active PL identity without downloading or touching MMIO."""

    active_path = getattr(overlay, "bitfile_name", None)
    pl = None
    try:
        from pynq import PL  # type: ignore
    except ImportError:
        PL = None
    if PL is not None:
        active_path = getattr(PL, "bitfile_name", active_path)
        pl = PL
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    boot_id = None
    if boot_id_path.is_file():
        try:
            boot_id = boot_id_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            boot_id = None
    if not active_path:
        embedded = getattr(overlay, "loaded_receipt", getattr(overlay, "bit_loaded_receipt", None))
        if isinstance(embedded, Mapping):
            digest = embedded.get("bit_sha256")
            expected_digest = _sha256_file(expected_bit)
            if isinstance(digest, str) and digest.lower() == expected_digest.lower():
                return {
                    "active_bitfile": embedded.get("active_bitfile"),
                    "bit_sha256": digest,
                    "expected_bit_sha256": expected_digest,
                    "pynq_pl_available": False,
                    "boot_id": boot_id,
                    "embedded_receipt": dict(embedded),
                }
        raise RuntimeAdmissionError("cannot attach: current PL bitfile identity is unavailable")
    active = Path(active_path)
    if not active.is_file():
        raise RuntimeAdmissionError(f"cannot attach: active PL bitfile is not readable: {active}")
    digest = _sha256_file(active)
    expected_digest = _sha256_file(expected_bit)
    if digest.lower() != expected_digest.lower():
        raise RuntimeAdmissionError("attach refused: active PL bitstream fingerprint differs from the signed artifact")
    return {
        "active_bitfile": str(active),
        "bit_sha256": digest,
        "expected_bit_sha256": expected_digest,
        "pynq_pl_available": pl is not None,
        "boot_id": boot_id,
    }


def verify_loaded_overlay_receipt(
    receipt: str | Path | Mapping[str, Any],
    *,
    artifacts: Mapping[str, Any],
    overlay: Any,
) -> dict[str, Any]:
    """Bind an attach receipt to the current PL and signed artifacts."""

    record = _loaded_overlay_receipt(receipt)
    if not record.get("boot_identity"):
        raise RuntimeAdmissionError("loaded Overlay receipt must include current boot_identity")
    identity = _current_pl_identity(overlay, expected_bit=Path(str(artifacts["bit"])))
    expected = artifacts["files"]
    for field, actual in (
        ("bit_sha256", expected["bit"]["sha256"]),
        ("hwh_sha256", expected["hwh"]["sha256"]),
        ("manifest_sha256", expected["manifest"]["sha256"]),
    ):
        declared = record.get(field)
        if not isinstance(declared, str) or declared.lower() != str(actual).lower():
            raise RuntimeAdmissionError(f"loaded Overlay receipt {field} does not match signed artifacts")
    if record.get("overlay_mode", OVERLAY_MODE) != OVERLAY_MODE:
        raise RuntimeAdmissionError("loaded Overlay receipt overlay_mode mismatch")
    embedded = identity.get("embedded_receipt")
    if isinstance(embedded, Mapping) and record.get("boot_identity") is not None:
        if embedded.get("boot_identity") != record.get("boot_identity"):
            raise RuntimeAdmissionError("loaded Overlay receipt boot identity differs from injected Overlay receipt")
    actual_boot = identity.get("boot_id")
    if identity.get("pynq_pl_available") and not actual_boot:
        raise RuntimeAdmissionError("attach refused: current Linux boot_id is unavailable")
    declared_boot = record.get("boot_identity")
    if actual_boot:
        declared_boot_id = declared_boot.get("boot_id") if isinstance(declared_boot, Mapping) else declared_boot
        if declared_boot_id != actual_boot:
            raise RuntimeAdmissionError("loaded Overlay receipt boot_id does not match the running Linux boot")
    identity["receipt"] = record
    return identity


def _validate_motion_config(config: MotionConfig) -> None:
    if not 1 <= config.positions <= 10:
        raise RuntimeAdmissionError("real Stage B motion permits at most ten finite positions")
    if config.pulses_per_position not in {80, 320} or abs(float(config.rpm) - 5.0) > 1e-9:
        raise RuntimeAdmissionError("real Stage B motion requires 80 or 320 pulses at 5 RPM")
    fixed = {
        "period_cycles": 375_000,
        "high_cycles": 187_500,
        "dir_setup_cycles": 10_000_000,
        "enable_setup_cycles": 10_000_000,
    }
    for name, expected in fixed.items():
        if int(getattr(config, name)) != expected:
            raise RuntimeAdmissionError(f"real Stage B motion does not permit overriding {name}")


def _motion_confirmation(value: str | Path | Mapping[str, Any] | None) -> dict[str, Any]:
    """Require a fresh operator confirmation before any real motor ARM."""

    if value is None:
        raise RuntimeAdmissionError(
            "real motion requires a fresh confirmation of 12V ON, stationary target, hand clear, and finite scope"
        )
    record = _read_confirmation(value, label="motion confirmation")
    fresh = record.get("fresh_motion_confirmed") is True or record.get("fresh") is True
    power_on = record.get("physical_power_on_confirmed") is True or record.get("power_on_confirmed") is True
    required = {
        "stationary": record.get("stationary") is True,
        "hand_clear": record.get("hand_clear") is True,
        "target_fixed": record.get("target_fixed") is True,
        "finite_scope": record.get("finite_scope") is True,
    }
    if not fresh or not power_on or not all(required.values()):
        raise RuntimeAdmissionError(
            "motion confirmation must be fresh and assert power_on, stationary, hand_clear, target_fixed, finite_scope"
        )
    return record


def _driver_readonly_proof(value: str | Path | Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate read-only driver state before host ARM is possible."""

    if value is None:
        raise RuntimeAdmissionError("real motion requires a read-only driver proof before ARM")
    record = _read_confirmation(value, label="driver proof")
    proof = record.get("driver_read_only_proof", record.get("driver_proof", record))
    if not isinstance(proof, Mapping):
        raise RuntimeAdmissionError("driver proof must contain a mapping")
    mode = proof.get("mode", proof.get("control_mode"))
    en = proof.get("en", proof.get("enable"))
    substeps = proof.get("microsteps", proof.get("substeps"))
    bus_voltage = proof.get("bus_voltage_v", proof.get("bus_v"))
    speed = proof.get("speed_rpm", proof.get("speed"))
    enabled = proof.get("enabled")
    if mode != 3 or en not in (0, False) or substeps != 16:
        raise RuntimeAdmissionError("driver proof must read mode=3, EN=0, microsteps=16")
    try:
        voltage = float(bus_voltage)
        speed_value = float(speed)
    except (TypeError, ValueError) as exc:
        raise RuntimeAdmissionError("driver proof must include numeric bus voltage and speed") from exc
    if not math.isfinite(voltage) or not 12.0 <= voltage <= 32.0:
        raise RuntimeAdmissionError("driver proof bus voltage must be in 12..32 V")
    if not math.isfinite(speed_value) or abs(speed_value) > 1e-9 or enabled not in (False, 0):
        raise RuntimeAdmissionError("driver proof must show speed=0 and disabled state before ARM")
    return dict(record)


def _read_confirmation(value: str | Path | Mapping[str, Any], *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    path = Path(value)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeAdmissionError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(record, dict):
        raise RuntimeAdmissionError(f"{label} must contain a JSON object")
    return record


def _json_load(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SettleBindingError(f"cannot read settle config {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise SettleBindingError("settle config must contain a JSON object")
    return value


def _settle_hash(config: SettleConfig, *, target: str, installation: str, roi: tuple[int, int, int, int], mode: str, profile: str) -> str:
    return config.bound_hash(target=target, installation=installation, roi=roi, mode=mode, profile=profile)


def _validated_roi(
    roi: Any,
    *,
    name: str,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    if not isinstance(roi, (tuple, list)) or len(roi) != 4:
        raise RuntimeAdmissionError(f"{name} must contain exactly four integer coordinates")
    if any(isinstance(value, bool) or not isinstance(value, Integral) for value in roi):
        raise RuntimeAdmissionError(f"{name} coordinates must be integers; booleans and fractions are invalid")
    values = tuple(int(value) for value in roi)
    x0, y0, x1, y1 = values
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise RuntimeAdmissionError(f"{name} must satisfy 0<=x0<x1<={width} and 0<=y0<y1<={height}")
    return values


def _resolve_settle_roi(
    roi: tuple[int, int, int, int],
    settle_roi: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int]:
    """Resolve the old ``roi`` alias and the explicit settle ROI."""

    full_frame_roi = (0, 0, FRAME_WIDTH, FRAME_HEIGHT)
    legacy_roi = _validated_roi(roi, name="roi", width=FRAME_WIDTH, height=FRAME_HEIGHT)
    if settle_roi is None:
        return legacy_roi
    explicit_roi = _validated_roi(settle_roi, name="settle_roi", width=FRAME_WIDTH, height=FRAME_HEIGHT)
    if legacy_roi != full_frame_roi and legacy_roi != explicit_roi:
        raise RuntimeAdmissionError("roi and settle_roi specify conflicting settle regions")
    return explicit_roi


def _configure_capture_configuration(
    controller: Any,
    *,
    stripe_roi: tuple[int, int, int, int],
    settle_roi: tuple[int, int, int, int],
    threshold: int,
    capture_timeout_s: float,
    capture_phase_timeout_s: float,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    """Configure the P2 stripe region and freeze its measured readback."""

    try:
        frame_width = int(controller.frame_width)
        frame_height = int(controller.frame_height)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeAdmissionError("P2 controller frame geometry is unavailable for ROI validation") from exc
    requested_stripe_roi = _validated_roi(
        stripe_roi, name="stripe_roi", width=frame_width, height=frame_height,
    )
    requested_settle_roi = _validated_roi(
        settle_roi, name="settle_roi", width=frame_width, height=frame_height,
    )
    if isinstance(threshold, bool) or not isinstance(threshold, Integral):
        raise RuntimeAdmissionError("threshold must be an integer in [0,255]")
    threshold = int(threshold)
    controller.configure_roi(*requested_stripe_roi)
    controller.set_threshold(threshold)
    readback_roi = tuple(int(value) for value in controller._roi())
    centroid = {str(key): int(value) for key, value in controller._centroid_parameters().items()}
    if readback_roi != requested_stripe_roi or centroid.get("threshold") != threshold:
        raise RuntimeAdmissionError("P2 stripe ROI or threshold readback differs from the requested configuration")
    return {
        "schema_version": 1,
        "stripe_roi": {"requested": list(requested_stripe_roi), "readback": list(readback_roi)},
        "settle_roi": list(requested_settle_roi),
        "threshold_requested": threshold,
        "centroid_parameters_readback": centroid,
        "profile_codec": "SLP2",
        "dma_contract": {
            "raw_timeout_s": float(capture_timeout_s),
            "capture_phase_timeout_s": float(capture_phase_timeout_s),
        },
        "overlay_files": dict(artifacts),
    }


def require_settle_binding(
    path: str | Path | None,
    *,
    target: str,
    installation: str,
    roi: tuple[int, int, int, int],
    mode: str,
    profile: str,
    config: SettleConfig | None = None,
) -> dict[str, Any]:
    """Load a measured settling receipt bound to the current installation."""

    if path is None:
        raise SettleBindingError("real motion requires a saved settle configuration")
    value = _json_load(path)
    cfg = config or SettleConfig()
    protocol = cfg.protocol
    binding_protocol_id = value.get("protocol_id", LEGACY_SETTLE_PROTOCOL_ID)
    if not isinstance(binding_protocol_id, str):
        raise SettleBindingError("settle config protocol_id is malformed")
    if binding_protocol_id != protocol.protocol_id:
        raise SettleBindingError("settle config protocol_id does not match the current protocol")
    descriptor = value.get("settle_protocol")
    if descriptor is None:
        if binding_protocol_id != LEGACY_SETTLE_PROTOCOL_ID:
            raise SettleBindingError("v2 settle config requires a canonical protocol descriptor")
    else:
        try:
            validate_settle_protocol(descriptor, protocol_id=binding_protocol_id)
        except (TypeError, ValueError) as exc:
            raise SettleBindingError(f"settle config protocol descriptor is not canonical: {exc}") from exc
    schedule_value = value.get("schedule_ms")
    if schedule_value is None:
        if binding_protocol_id != LEGACY_SETTLE_PROTOCOL_ID:
            raise SettleBindingError("v2 settle config requires its canonical schedule")
    else:
        try:
            schedule_tuple = tuple(schedule_value)
        except TypeError as exc:
            raise SettleBindingError("settle config schedule is malformed") from exc
        if schedule_tuple != protocol.schedule_ms:
            raise SettleBindingError("settle config schedule does not match the current protocol")
    expected_hash = _settle_hash(cfg, target=target, installation=installation, roi=roi, mode=mode, profile=profile)
    if value.get("schema_version") != 1 or value.get("config_hash") != expected_hash:
        raise SettleBindingError("settle config hash/schema does not match target, installation, ROI, mode, and profile")
    if (value.get("target"), value.get("installation"), value.get("mode"), value.get("profile")) != (target, installation, mode, profile):
        raise SettleBindingError("settle config is bound to another target or installation")
    if tuple(value.get("roi", ())) != tuple(roi):
        raise SettleBindingError("settle config ROI does not match the current camera ROI")
    accepted = value.get("accepted_wait_s")
    if (
        isinstance(accepted, bool)
        or not isinstance(accepted, (int, float))
        or not math.isfinite(float(accepted))
        or not 0 < float(accepted) <= 120.0
    ):
        raise SettleBindingError("settle config accepted_wait_s must be a finite measured wait within the 120 s task budget")
    if value.get("selected_scheduled_ms") not in protocol.schedule_ms:
        raise SettleBindingError("settle config selected_scheduled_ms is not in the planned schedule")
    frame_period = value.get("frame_period_s")
    if (
        isinstance(frame_period, bool)
        or not isinstance(frame_period, (int, float))
        or not math.isfinite(float(frame_period))
        or float(frame_period) <= 0
    ):
        raise SettleBindingError("settle config lacks a measured positive frame period")
    if value.get("positions") != 10 or value.get("pulses_per_position") != 320 or abs(float(value.get("rpm", 0)) - 5.0) > 1e-9:
        raise SettleBindingError("settle config does not describe the formal 10x320 at 5 RPM sequence")
    points = value.get("points")
    if not isinstance(points, list) or len(points) != 10 or any(not isinstance(item, Mapping) or item.get("settled") is not True for item in points):
        raise SettleBindingError("settle config does not contain ten accepted measured points")
    return value


def _settle_noise_thresholds(
    baseline: Sequence[Any], *, roi: tuple[int, int, int, int], config: SettleConfig,
) -> tuple[float, float, float, float]:
    """Derive noise thresholds from baseline pairs only."""

    if len(baseline) < config.baseline_frames:
        raise SettleBindingError("settle baseline must contain twenty measured raw frames")
    values = list(baseline[: config.baseline_frames])
    import numpy as np

    phase_values = [
        math.hypot(*phase_correlation_shift(values[index - 1][roi[1]:roi[3], roi[0]:roi[2]], values[index][roi[1]:roi[3], roi[0]:roi[2]]))
        for index in range(1, len(values))
    ]
    diff_values = [
        brightness_normalized_difference(
            values[index - 1][roi[1]:roi[3], roi[0]:roi[2]],
            values[index][roi[1]:roi[3], roi[0]:roi[2]],
        )
        for index in range(1, len(values))
    ]
    if not phase_values or not diff_values:
        raise SettleBindingError("settle baseline must contain pairwise noise samples")
    phase_p99 = float(np.quantile(np.asarray(phase_values), config.p99_quantile))
    diff_p99 = float(np.quantile(np.asarray(diff_values), config.p99_quantile))
    phase_threshold = max(config.max_phase_shift_px, config.phase_multiplier * phase_p99)
    diff_threshold = config.diff_multiplier * diff_p99 + config.diff_offset_dn
    return phase_p99, diff_p99, phase_threshold, diff_threshold


def _settle_texture_ok(frame: Any, config: SettleConfig) -> bool:
    import numpy as np

    values = np.asarray(frame, dtype=np.float64)
    if values.ndim != 2 or values.size == 0 or float(values.std()) < config.min_texture_std:
        return False
    gradient_x = np.diff(values, axis=1)
    gradient_y = np.diff(values, axis=0)
    energy = float((np.mean(np.square(gradient_x)) + np.mean(np.square(gradient_y))) / 2.0)
    return energy >= config.min_gradient_energy


def select_settle_wait(
    baseline: Sequence[Any], candidates: Sequence[Any], *,
    actual_elapsed_s: Sequence[float], roi: tuple[int, int, int, int],
    config: SettleConfig, frame_period_s: float,
    baseline_thresholds: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    """Select the earliest stable wait against a same-pose late reference.

    Baseline frames determine noise thresholds only.  Pose identity comes from
    the measured 2.5 s / 3.0 s late pair, so a moving or pre-move baseline can
    never make an early frame appear settled.
    """

    protocol = config.protocol
    schedule_ms = protocol.schedule_ms
    if len(candidates) != len(schedule_ms) or len(actual_elapsed_s) != len(candidates):
        raise SettleBindingError("settle candidates must cover all seven scheduled samples")
    if isinstance(frame_period_s, bool) or not math.isfinite(float(frame_period_s)) or frame_period_s <= 0:
        raise SettleBindingError("a measured positive frame period is required for settling")
    for index, candidate in enumerate(candidates):
        region = candidate[roi[1]:roi[3], roi[0]:roi[2]]
        if not _settle_texture_ok(region, config):
            scheduled_ms = schedule_ms[index]
            if index >= len(candidates) - 2:
                raise SettleBindingError(
                    f"late reference sample at {scheduled_ms} ms has insufficient settle ROI texture"
                )
            raise SettleBindingError(
                f"post-stop sample at {scheduled_ms} ms has insufficient settle ROI texture"
            )
    if baseline_thresholds is None:
        phase_p99, diff_p99, phase_threshold, diff_threshold = _settle_noise_thresholds(
            baseline, roi=roi, config=config,
        )
    else:
        if len(baseline_thresholds) != 4 or any(
            not math.isfinite(float(value)) for value in baseline_thresholds
        ):
            raise SettleBindingError("cached settle baseline thresholds are malformed")
        phase_p99, diff_p99, phase_threshold, diff_threshold = (
            float(value) for value in baseline_thresholds
        )
    late_two_point = candidates[-2]
    late_three_point = candidates[-1]
    late_phase = math.hypot(*phase_correlation_shift(
        late_two_point[roi[1]:roi[3], roi[0]:roi[2]],
        late_three_point[roi[1]:roi[3], roi[0]:roi[2]],
    ))
    late_diff = brightness_normalized_difference(
        late_two_point[roi[1]:roi[3], roi[0]:roi[2]],
        late_three_point[roi[1]:roi[3], roi[0]:roi[2]],
    )
    if late_phase > phase_threshold or late_diff > diff_threshold:
        late_a, late_b = protocol.late_reference_ms
        raise SettleBindingError(
            f"late {late_a / 1000.0:.1f} s and {late_b / 1000.0:.1f} s references are not stable"
        )
    candidate_metrics: list[dict[str, float]] = []
    for candidate in candidates:
        phase = math.hypot(*phase_correlation_shift(
            candidate[roi[1]:roi[3], roi[0]:roi[2]],
            late_three_point[roi[1]:roi[3], roi[0]:roi[2]],
        ))
        difference = brightness_normalized_difference(
            candidate[roi[1]:roi[3], roi[0]:roi[2]],
            late_three_point[roi[1]:roi[3], roi[0]:roi[2]],
        )
        candidate_metrics.append({"phase_shift_px": phase, "difference_dn": difference})
    selected_index: int | None = None
    for index, _ in enumerate(candidates):
        tail = candidate_metrics[index:]
        if all(
            item["phase_shift_px"] <= phase_threshold and item["difference_dn"] <= diff_threshold
            for item in tail
        ):
            selected_index = index
            break
    if selected_index is None:
        raise SettleBindingError("no planned settle wait has stable subsequent candidate frames")
    selected_wait_s = float(actual_elapsed_s[selected_index]) + float(frame_period_s)
    estimate = SettleEstimate(
        settled=True, elapsed_s=selected_wait_s, samples=len(baseline) + len(candidates),
        baseline_phase_p99=phase_p99, baseline_diff_p99=diff_p99,
        phase_shift_px=candidate_metrics[selected_index]["phase_shift_px"],
        difference_dn=candidate_metrics[selected_index]["difference_dn"],
        phase_threshold_px=phase_threshold, difference_threshold_dn=diff_threshold,
        reason="stable_same_pose", config_hash="", roi=tuple(roi),
    )
    return {
        "selected_index": selected_index,
        "selected_scheduled_ms": schedule_ms[selected_index],
        "accepted_wait_s": selected_wait_s,
        "frame_period_s": float(frame_period_s),
        "baseline_phase_p99": phase_p99,
        "baseline_diff_p99": diff_p99,
        "phase_threshold_px": phase_threshold,
        "difference_threshold_dn": diff_threshold,
        "late_pair": {"phase_shift_px": late_phase, "difference_dn": late_diff},
        "candidate_metrics": candidate_metrics,
        "estimate": estimate,
    }


def _p2_capture_identity(controller: Any) -> dict[str, int]:
    mmio = getattr(controller, "mmio", None)
    if mmio is None:
        raise StageBRuntimeError("P2 controller does not expose capture request_id/epoch evidence")
    values: dict[str, int | None] = {}
    for output_name, source_name in (("camera_request_id", "request_id"), ("camera_epoch", "epoch")):
        value = getattr(mmio, source_name, None)
        if value is None or isinstance(value, bool) or not isinstance(value, Integral):
            raise StageBRuntimeError(f"P2 controller {source_name} evidence is unavailable or malformed")
        try:
            values[output_name] = int(value)
        except (TypeError, ValueError):
            raise StageBRuntimeError(f"P2 controller {source_name} is not a readable integer")
    return values


class MeasuredSettleSampler:
    """Use one pre-motion baseline and seven auditable post-stop raw frames."""

    def __init__(
        self,
        controller: Any,
        *,
        baseline_frames: Sequence[Any],
        baseline_capture_records: Sequence[Mapping[str, Any]],
        output: str | Path,
        roi: tuple[int, int, int, int],
        config: SettleConfig,
        config_hash: str,
        accepted_wait_s: float | None = None,
        frame_period_s: float | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        guard: Any | None = None,
        expected_pulses: int = 320,
        capture_timeout_s: float = 2.0,
        persistence_timeout_s: float = 6.0,
    ) -> None:
        self.controller = controller
        roi_request = roi
        self.config = config
        self.config_hash = str(config_hash)
        if accepted_wait_s is not None and isinstance(accepted_wait_s, bool):
            raise SettleBindingError("accepted_wait_s must be positive")
        try:
            self.accepted_wait_s = None if accepted_wait_s is None else float(accepted_wait_s)
        except (TypeError, ValueError) as exc:
            raise SettleBindingError("accepted_wait_s must be positive") from exc
        if frame_period_s is not None and isinstance(frame_period_s, bool):
            raise SettleBindingError("MeasuredSettleSampler requires an observed positive camera frame period")
        try:
            self.frame_period_s = None if frame_period_s is None else float(frame_period_s)
        except (TypeError, ValueError) as exc:
            raise SettleBindingError("MeasuredSettleSampler requires an observed positive camera frame period") from exc
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.guard = guard
        self.expected_pulses = int(expected_pulses)
        self.capture_timeout_s = float(capture_timeout_s)
        self.persistence_timeout_s = float(persistence_timeout_s)
        self.protocol = config.protocol
        self.schedule_ms = self.protocol.schedule_ms
        self.settle_deadline_s = (
            max(self.schedule_ms) / 1000.0
            + len(self.schedule_ms) * self.capture_timeout_s
            + self.persistence_timeout_s
        )
        self.last_observation: dict[str, Any] | None = None
        self.observations: list[dict[str, Any]] = []
        self.last_persisted_capture: dict[str, int] | None = None
        self._pending_stop: dict[str, Any] | None = None
        if config.baseline_frames != 20:
            raise SettleBindingError("measured settling requires exactly twenty baseline frames")
        if self.accepted_wait_s is not None and self.accepted_wait_s <= 0:
            raise SettleBindingError("accepted_wait_s must be positive")
        if self.frame_period_s is None or not math.isfinite(self.frame_period_s) or self.frame_period_s <= 0:
            raise SettleBindingError("MeasuredSettleSampler requires an observed positive camera frame period")
        if self.expected_pulses <= 0:
            raise SettleBindingError("expected_pulses must be positive")
        if (
            not math.isfinite(self.capture_timeout_s) or self.capture_timeout_s <= 0
            or not math.isfinite(self.persistence_timeout_s) or self.persistence_timeout_s <= 0
        ):
            raise SettleBindingError("settle sample capture and persistence budgets must be finite and positive")
        self.baseline_frames = [self._as_gray8(frame) for frame in baseline_frames]
        if len(self.baseline_frames) != 20:
            raise SettleBindingError("MeasuredSettleSampler requires exactly 20 pre-captured baseline frames")
        if len(baseline_capture_records) != 20:
            raise SettleBindingError("MeasuredSettleSampler requires 20 baseline capture timing records")
        shape = self.baseline_frames[0].shape
        if any(frame.shape != shape for frame in self.baseline_frames):
            raise SettleBindingError("baseline frames do not share one full-frame geometry")
        height, width = shape
        try:
            self.roi = _validated_roi(roi_request, name="settle ROI", width=width, height=height)
        except RuntimeAdmissionError as exc:
            raise SettleBindingError(str(exc)) from exc
        x0, y0, x1, y1 = self.roi
        self.frame_shape = (height, width)
        self.baseline_capture_records = [self._validate_baseline_record(item, index) for index, item in enumerate(baseline_capture_records)]

        self.output = Path(output)
        if self.output.exists():
            raise StageBRuntimeError(f"settle sample output must be unique and unused: {self.output}")
        try:
            self.output.mkdir(parents=True, exist_ok=False)
            self.baseline_dir = self.output / "baseline"
            self.baseline_dir.mkdir()
            self._write_json_exclusive(self.output / "sampler_config.json", {
                "schema_version": 1,
                "config_hash": self.config_hash,
                "settle_roi": list(self.roi),
                "frame_period_s": self.frame_period_s,
                "expected_pulses": self.expected_pulses,
                "capture_timeout_s": self.capture_timeout_s,
                "persistence_timeout_s": self.persistence_timeout_s,
                "settle_deadline_s": self.settle_deadline_s,
                "schedule_ms": list(self.schedule_ms),
                "settle_protocol": self.protocol.as_dict(),
                "settle_config": asdict(self.config),
                "baseline_capture_source": "MotionSession.run_static(raw=True)",
            })
            self.baseline_artifacts = self._persist_baseline()
        except StageBRuntimeError:
            raise
        except Exception as exc:
            raise StageBRuntimeError(f"failed to persist measured settle baseline: {exc}") from exc

        if not self._texture_ok(self.baseline_frames[0][y0:y1, x0:x1]):
            raise SettleBindingError("settle baseline ROI has insufficient texture")
        # Baseline noise is immutable for this sampler. Compute the exact
        # threshold tuple once before motion starts; each position reuses the
        # values without changing the estimator arithmetic or thresholds.
        self._baseline_thresholds = _settle_noise_thresholds(
            self.baseline_frames, roi=self.roi, config=self.config,
        )

    @staticmethod
    def _as_gray8(frame: Any):
        import numpy as np

        pixels = getattr(frame, "pixels", None)
        if isinstance(pixels, (bytes, bytearray, memoryview)):
            width = int(getattr(frame, "width", 0))
            height = int(getattr(frame, "height", 0))
            data = np.frombuffer(bytes(pixels), dtype=np.uint8)
            if width <= 0 or height <= 0 or data.size != width * height:
                raise SettleBindingError("precaptured baseline is not a complete GrayFrame")
            return data.reshape(height, width).copy()
        values = np.asarray(frame)
        if values.ndim != 2 or values.size == 0:
            raise SettleBindingError("precaptured baseline frames must be two-dimensional grayscale")
        if values.dtype != np.uint8:
            if values.dtype.kind not in "ui" or int(values.min()) < 0 or int(values.max()) > 255:
                raise SettleBindingError("precaptured baseline pixels must fit gray8")
            values = values.astype(np.uint8)
        return np.ascontiguousarray(values).copy()

    @staticmethod
    def _validate_baseline_record(item: Mapping[str, Any], index: int) -> dict[str, Any]:
        record = dict(item)
        try:
            started = float(record["capture_started_monotonic_s"])
            completed = float(record["capture_completed_monotonic_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SettleBindingError(f"baseline timing record {index} is incomplete") from exc
        if not math.isfinite(started) or not math.isfinite(completed) or completed < started:
            raise SettleBindingError(f"baseline timing record {index} has invalid monotonic timestamps")
        record["capture_started_monotonic_s"] = started
        record["capture_completed_monotonic_s"] = completed
        for field in ("camera_request_id", "camera_epoch"):
            value = record.get(field)
            if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
                raise SettleBindingError(f"baseline timing record {index} lacks valid P2 {field} evidence")
            record[field] = int(value)
        return record

    @staticmethod
    def _texture_ok(frame: Any, config: SettleConfig | None = None) -> bool:
        return _settle_texture_ok(frame, config or SettleConfig())

    @staticmethod
    def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
        import os

        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _hash_bytes(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def _persist_frame(self, directory: Path, stem: str, frame: Any) -> dict[str, Any]:
        import io
        import os
        import numpy as np

        values = self._as_gray8(frame)
        if tuple(values.shape) != self.frame_shape:
            raise StageBRuntimeError("settle sample geometry changed from its pre-motion baseline")
        raw_bytes = values.tobytes(order="C")
        raw_path = directory / f"{stem}.gray8.bin"
        png_path = directory / f"{stem}.png"
        try:
            with raw_path.open("xb") as stream:
                stream.write(raw_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            from PIL import Image

            image_buffer = io.BytesIO()
            # ARM evidence capture favors bounded CPU time over PNG size.
            # Decoder equality below still verifies every original pixel.
            Image.fromarray(values).save(image_buffer, format="PNG", compress_level=0)
            png_bytes = image_buffer.getvalue()
            with Image.open(io.BytesIO(png_bytes)) as decoded_image:
                decoded = np.asarray(decoded_image.convert("L"), dtype=np.uint8)
            if not np.array_equal(decoded, values):
                raise StageBRuntimeError("saved PNG does not decode to the original gray8 pixels")
            with png_path.open("xb") as stream:
                stream.write(png_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        except StageBRuntimeError:
            raise
        except Exception as exc:
            raise StageBRuntimeError(f"failed to save settle image pair {stem}: {exc}") from exc
        return {
            "raw_file": raw_path.relative_to(self.output).as_posix(),
            "raw_sha256": self._hash_bytes(raw_bytes),
            "raw_bytes": len(raw_bytes),
            "png_file": png_path.relative_to(self.output).as_posix(),
            "png_sha256": self._hash_bytes(png_bytes),
            "png_bytes": len(png_bytes),
        }

    def _persist_baseline(self) -> list[dict[str, Any]]:
        import os

        artifacts: list[dict[str, Any]] = []
        jsonl_path = self.baseline_dir / "baseline.jsonl"
        with jsonl_path.open("x", encoding="utf-8", newline="\n") as stream:
            for index, (frame, timing) in enumerate(zip(self.baseline_frames, self.baseline_capture_records, strict=True)):
                files = self._persist_frame(self.baseline_dir, f"baseline_{index:02d}", frame)
                record = {"frame_index": index, **timing, **files}
                artifacts.append(record)
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        return artifacts

    def _capture(self) -> Any:
        capture_raw = getattr(self.controller, "capture_raw", None)
        if not callable(capture_raw):
            raise StageBRuntimeError("real settling requires P2Controller.capture_raw")
        frame = capture_raw()
        values = self._as_gray8(frame)
        if tuple(values.shape) != self.frame_shape:
            raise StageBRuntimeError("capture_raw returned a frame with unexpected geometry")
        return values

    def mark_motor_stopped(self, position_index: int, *, monotonic_s: float, motor_receipt: Mapping[str, Any]) -> None:
        if type(position_index) is not int or position_index < 0:
            raise SettleBindingError("stop marker position_index must be a non-negative integer")
        if self._pending_stop is not None:
            raise SettleBindingError("a stop marker is already pending for another sampler call")
        try:
            stopped_at = float(monotonic_s)
        except (TypeError, ValueError) as exc:
            raise SettleBindingError("stop marker time must be a finite host monotonic timestamp") from exc
        if not math.isfinite(stopped_at) or stopped_at > self.monotonic():
            raise SettleBindingError("stop marker time must be finite and no later than the current host monotonic time")
        if not isinstance(motor_receipt, Mapping):
            raise SettleBindingError("stop marker requires the validated motor receipt")
        receipt = dict(motor_receipt)
        if receipt.get("done") is not True or receipt.get("busy") is not False:
            raise SettleBindingError("stop marker receipt does not show clean MFP1 DONE and idle")
        if receipt.get("fault") is True or receipt.get("cancelled") is True:
            raise SettleBindingError("stop marker receipt contains a motor fault or cancellation")
        try:
            emitted = int(receipt.get("emitted", -1))
        except (TypeError, ValueError) as exc:
            raise SettleBindingError("stop marker receipt has no valid emitted pulse count") from exc
        if emitted != self.expected_pulses or int(receipt.get("error", 0)) != 0:
            raise SettleBindingError("stop marker receipt pulse count or error code is invalid")
        final_status = receipt.get("stop_readback", receipt.get("final_status", receipt.get("cleanup_status")))
        if not isinstance(final_status, Mapping):
            raise SettleBindingError("stop marker requires a final motor disable readback")
        if isinstance(final_status.get("status"), Mapping):
            final_status = final_status["status"]
        if final_status.get("busy") is not False or final_status.get("step") is not False or final_status.get("enable_n") is not True:
            raise SettleBindingError("stop marker readback must show idle, STEP low, and EN high")
        if int(final_status.get("error", 0)) != 0:
            raise SettleBindingError("stop marker readback has a nonzero motor error code")
        external_guard_readback: dict[str, Any] | None = None
        if self.protocol.protocol_id == EXTENDED_SETTLE_PROTOCOL_ID:
            status_reader = getattr(self.guard, "status", None)
            if not callable(status_reader):
                raise SettleBindingError("v2 settling requires external BSG1 guard readback")
            try:
                external_guard_readback = dict(status_reader())
            except Exception as exc:
                raise SettleBindingError(f"v2 external BSG1 guard readback failed: {exc}") from exc
            required_state = {
                "active": True,
                "fault": False,
                "error": 0,
                "motor_busy": False,
                "step_pin": False,
                "enable_pin": False,
            }
            for name, expected in required_state.items():
                actual = external_guard_readback.get(name)
                invalid = (
                    type(actual) is not bool or actual is not expected
                    if isinstance(expected, bool)
                    else isinstance(actual, bool) or actual != expected
                )
                if invalid:
                    if name == "enable_pin":
                        raise SettleBindingError("v2 external BSG1 readback must keep external EN low")
                    raise SettleBindingError(
                        f"v2 external BSG1 readback has unsafe {name}={actual!r}; expected {expected!r}"
                    )
        try:
            receipt_copy = json.loads(json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str))
        except (TypeError, ValueError) as exc:
            raise SettleBindingError("motor receipt is not persistable JSON evidence") from exc
        self._pending_stop = {
            "position_index": position_index,
            "stop_confirmed_monotonic_s": stopped_at,
            "marked_at_monotonic_s": float(self.monotonic()),
            "motor_receipt": receipt_copy,
        }
        if external_guard_readback is not None:
            self._pending_stop["external_guard_readback"] = json.loads(
                json.dumps(external_guard_readback, ensure_ascii=False, sort_keys=True, default=str)
            )

    def __call__(self, position_index: int) -> Sequence[Any]:
        if type(position_index) is not int or position_index < 0:
            raise SettleBindingError("sampler position_index must be a non-negative integer")
        if self._pending_stop is None:
            raise SettleBindingError("MeasuredSettleSampler requires a matching motor stop marker before sampling")
        if self._pending_stop["position_index"] != position_index:
            raise SettleBindingError("sampler position does not match the pending motor stop marker")
        stop = self._pending_stop
        self._pending_stop = None
        position_dir = self.output / f"position_{position_index:03d}"
        try:
            position_dir.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise SettleBindingError(f"settle position output already exists: {position_dir}") from exc
        self._write_json_exclusive(position_dir / "stop.json", stop)
        samples_jsonl = position_dir / "samples.jsonl"
        samples: list[Any] = []
        records: list[dict[str, Any]] = []
        result_record: dict[str, Any] = {
            "position_index": position_index,
            "settled": False,
            "stop_confirmed_monotonic_s": stop["stop_confirmed_monotonic_s"],
            "motor_receipt": stop["motor_receipt"],
            "baseline_manifest": "baseline/baseline.jsonl",
            "baseline_frame_count": 20,
            "settle_roi": list(self.roi),
            "sample_schedule_ms": list(self.schedule_ms),
            "settle_protocol": self.protocol.as_dict(),
            "samples": records,
        }
        if "external_guard_readback" in stop:
            result_record["external_guard_readback"] = stop["external_guard_readback"]
        try:
            import os

            with samples_jsonl.open("x", encoding="utf-8", newline="\n") as stream:
                for index, scheduled_ms in enumerate(self.schedule_ms):
                    deadline = float(stop["stop_confirmed_monotonic_s"]) + scheduled_ms / 1000.0
                    while self.monotonic() < deadline:
                        self.sleep(min(0.005, max(0.0, deadline - self.monotonic())))
                    triggered_at = float(self.monotonic())
                    frame = self._capture()
                    completed_at = float(self.monotonic())
                    camera_identity = _p2_capture_identity(self.controller)
                    files = self._persist_frame(position_dir, f"sample_{index:02d}", frame)
                    samples.append(frame)
                    record = {
                        "sample_index": index,
                        "scheduled_ms": scheduled_ms,
                        **camera_identity,
                        "planned_trigger_monotonic_s": deadline,
                        "actual_triggered_monotonic_s": triggered_at,
                        "actual_completed_monotonic_s": completed_at,
                        "actual_elapsed_s": triggered_at - float(stop["stop_confirmed_monotonic_s"]),
                        "capture_duration_s": completed_at - triggered_at,
                        **files,
                    }
                    records.append(record)
                    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    # Generation bookkeeping is independent of the later
                    # settle decision. Publish only after image + record are
                    # durable, including the last pair of a rejected settle.
                    self.last_persisted_capture = {
                        "request_id": camera_identity["camera_request_id"],
                        "epoch": camera_identity["camera_epoch"],
                    }
            selection = select_settle_wait(
                self.baseline_frames, samples,
                actual_elapsed_s=[item["actual_elapsed_s"] for item in records],
                roi=self.roi, config=self.config, frame_period_s=float(self.frame_period_s),
                baseline_thresholds=self._baseline_thresholds,
            )
            if self.accepted_wait_s is not None:
                if abs(selection["accepted_wait_s"] - self.accepted_wait_s) > max(float(self.frame_period_s), 1e-6):
                    raise SettleBindingError(
                        f"measured wait {selection['accepted_wait_s']:.6f}s differs from saved wait {self.accepted_wait_s:.6f}s"
                    )
                accepted_wait_s = self.accepted_wait_s
            else:
                accepted_wait_s = float(selection["accepted_wait_s"])
            estimate = selection["estimate"]
            accepted_estimate = SettleEstimate(
                settled=True, elapsed_s=accepted_wait_s, samples=estimate.samples,
                baseline_phase_p99=estimate.baseline_phase_p99,
                baseline_diff_p99=estimate.baseline_diff_p99,
                phase_shift_px=estimate.phase_shift_px, difference_dn=estimate.difference_dn,
                phase_threshold_px=estimate.phase_threshold_px,
                difference_threshold_dn=estimate.difference_threshold_dn,
                reason=estimate.reason, config_hash=self.config_hash, roi=estimate.roi,
            )
            result_record.update({
                "settled": True,
                "accepted_wait_s": accepted_wait_s,
                "selected_scheduled_ms": selection["selected_scheduled_ms"],
                "frame_period_s": self.frame_period_s,
                "estimate": accepted_estimate.as_dict(),
                "late_pair": selection["late_pair"],
                "candidate_metrics": selection["candidate_metrics"],
                "baseline_phase_p99": selection["baseline_phase_p99"],
                "baseline_diff_p99": selection["baseline_diff_p99"],
                "phase_threshold_px": selection["phase_threshold_px"],
                "difference_threshold_dn": selection["difference_threshold_dn"],
            })
            self._write_json_exclusive(position_dir / "samples.json", result_record)
            self.last_observation = dict(result_record)
            self.observations.append(dict(result_record))
            return MeasuredSettleSamples(samples, accepted_estimate)
        except Exception as exc:
            result_record.update({
                "passed": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            if not (position_dir / "samples.json").exists():
                try:
                    self._write_json_exclusive(position_dir / "samples.json", result_record)
                except Exception as save_exc:
                    raise StageBRuntimeError(f"failed to save settle position failure record: {save_exc}") from save_exc
            raise


def _estimate_from_mapping(value: Mapping[str, Any]) -> SettleEstimate:
    return SettleEstimate(
        settled=bool(value["settled"]), elapsed_s=float(value["elapsed_s"]),
        samples=int(value["samples"]), baseline_phase_p99=float(value["baseline_phase_p99"]),
        baseline_diff_p99=float(value["baseline_diff_p99"]),
        phase_shift_px=None if value.get("phase_shift_px") is None else float(value["phase_shift_px"]),
        difference_dn=None if value.get("difference_dn") is None else float(value["difference_dn"]),
        phase_threshold_px=float(value["phase_threshold_px"]),
        difference_threshold_dn=float(value["difference_threshold_dn"]), reason=str(value["reason"]),
        config_hash=str(value["config_hash"]), roi=tuple(int(item) for item in value["roi"]),
    )


class MeasuredSettleSamples(list):
    """Full diagnostic frames carrying the same-pose measured estimate."""

    def __init__(self, frames: Sequence[Any], estimate: SettleEstimate) -> None:
        super().__init__(frames)
        self.settle_estimate = estimate


class CachedSettleSamples(list):
    """One real post-wait raw frame plus the cached diagnostic estimate.

    The list contains only the newly captured frame; no repeated synthetic
    frames are manufactured.  An updated MotionSession can consume the
    ``settle_estimate`` attribute directly while older callers still receive
    an ordinary sequence and fail closed if they demand the old 20-frame
    estimator path.
    """

    def __init__(self, frame: Any, estimate: SettleEstimate) -> None:
        super().__init__([frame])
        self.settle_estimate = estimate


class SavedSettleSampler:
    """Use one measured wait and one real frame per formal position."""

    def __init__(self, controller: Any, binding: Mapping[str, Any], *, sleep: Callable[[float], None] | None = None, monotonic: Callable[[], float] | None = None) -> None:
        self.controller = controller
        self.binding = dict(binding)
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic
        binding_protocol_id = self.binding.get("protocol_id", LEGACY_SETTLE_PROTOCOL_ID)
        if not isinstance(binding_protocol_id, str):
            raise SettleBindingError("saved settle binding protocol_id is malformed")
        descriptor = self.binding.get("settle_protocol")
        if descriptor is None:
            if binding_protocol_id != LEGACY_SETTLE_PROTOCOL_ID:
                raise SettleBindingError("v2 saved settle binding requires a canonical protocol descriptor")
            self.protocol = get_settle_protocol(LEGACY_SETTLE_PROTOCOL_ID)
        else:
            try:
                self.protocol = validate_settle_protocol(descriptor, protocol_id=binding_protocol_id)
            except (TypeError, ValueError) as exc:
                raise SettleBindingError(f"saved settle binding protocol descriptor is not canonical: {exc}") from exc
        schedule_value = self.binding.get("schedule_ms")
        if schedule_value is not None:
            try:
                schedule_tuple = tuple(schedule_value)
            except TypeError as exc:
                raise SettleBindingError("saved settle binding schedule is malformed") from exc
            if schedule_tuple != self.protocol.schedule_ms:
                raise SettleBindingError("saved settle binding schedule does not match its protocol")
        if schedule_value is None and self.protocol.protocol_id != LEGACY_SETTLE_PROTOCOL_ID:
            raise SettleBindingError("v2 saved settle binding requires its canonical schedule")
        self.accepted_wait_s = float(binding["accepted_wait_s"])
        self.frame_period_s = float(binding.get("frame_period_s", 0.0))
        if not math.isfinite(self.accepted_wait_s) or self.accepted_wait_s <= 0:
            raise SettleBindingError("saved settle wait must be finite and positive")
        if not math.isfinite(self.frame_period_s) or self.frame_period_s <= 0:
            raise SettleBindingError("saved settle binding lacks a measured frame period")
        self.last_observation: dict[str, Any] | None = None
        self.last_estimate: SettleEstimate | None = None

    def _cached_estimate(self, position_index: int) -> SettleEstimate:
        points = self.binding.get("points", ())
        point = next((item for item in points if int(item.get("position_index", -1)) == int(position_index)), None)
        if not isinstance(point, Mapping) or not isinstance(point.get("estimate"), Mapping):
            raise SettleBindingError(f"saved settle binding lacks position {position_index} estimate")
        estimate = _estimate_from_mapping(point["estimate"])
        return SettleEstimate(
            settled=True, elapsed_s=self.accepted_wait_s, samples=estimate.samples,
            baseline_phase_p99=estimate.baseline_phase_p99, baseline_diff_p99=estimate.baseline_diff_p99,
            phase_shift_px=estimate.phase_shift_px, difference_dn=estimate.difference_dn,
            phase_threshold_px=estimate.phase_threshold_px, difference_threshold_dn=estimate.difference_threshold_dn,
            reason="cached_measured_settle", config_hash=estimate.config_hash, roi=estimate.roi,
        )

    def __call__(self, position_index: int) -> CachedSettleSamples:
        started = self.monotonic()
        deadline = started + self.accepted_wait_s
        while self.monotonic() < deadline:
            self.sleep(min(0.005, max(0.0, deadline - self.monotonic())))
        capture_raw = getattr(self.controller, "capture_raw", None)
        if not callable(capture_raw):
            raise StageBRuntimeError("formal settling requires P2Controller.capture_raw")
        frame = capture_raw()
        estimate = self._cached_estimate(position_index)
        self.last_estimate = estimate
        self.last_observation = {
            "position_index": int(position_index),
            "actual_elapsed_s": max(0.0, self.monotonic() - started),
            "estimate": estimate.as_dict(),
            "cached": True,
            "settle_protocol": self.protocol.as_dict(),
        }
        return CachedSettleSamples(frame, estimate)

    def return_settle_estimate(self, position_index: int) -> SettleEstimate:
        estimate = self._cached_estimate(position_index)
        self.last_estimate = estimate
        return estimate


def return_settle_estimate(sampler: Any) -> SettleEstimate | None:
    """Return the most recent measured estimate from a settle sampler."""

    observation = getattr(sampler, "last_observation", None)
    if not isinstance(observation, Mapping):
        return None
    estimate = observation.get("estimate")
    if not isinstance(estimate, Mapping):
        return None
    return SettleEstimate(
        settled=bool(estimate["settled"]), elapsed_s=float(estimate["elapsed_s"]),
        samples=int(estimate["samples"]), baseline_phase_p99=float(estimate["baseline_phase_p99"]),
        baseline_diff_p99=float(estimate["baseline_diff_p99"]),
        phase_shift_px=None if estimate.get("phase_shift_px") is None else float(estimate["phase_shift_px"]),
        difference_dn=None if estimate.get("difference_dn") is None else float(estimate["difference_dn"]),
        phase_threshold_px=float(estimate["phase_threshold_px"]),
        difference_threshold_dn=float(estimate["difference_threshold_dn"]), reason=str(estimate["reason"]),
        config_hash=str(estimate["config_hash"]), roi=tuple(int(item) for item in estimate["roi"]),
    )


class StageBHDMIAdapter:
    """Render one CaptureResult into a canonical 1280x720 BGR canvas."""

    def __init__(self, display: Any | None = None, *, no_display: bool = False) -> None:
        self.display = display
        self.no_display = bool(no_display)
        self.configured = False
        self.last_frame_bgr: bytes | None = None
        self.last_receipt: dict[str, Any] = {}
        self._next_position = 0

    @staticmethod
    def _capture_metrics(result: Any) -> dict[str, Any]:
        diagnostics = getattr(result, "diagnostics", None)
        rows = tuple(getattr(diagnostics, "rows", ()) or ())
        valid_rows = sum(bool(getattr(row, "valid", False)) for row in rows)
        valid_single_rows = sum(
            bool(getattr(row, "valid", False)) and int(getattr(row, "candidate_count", 0)) == 1
            for row in rows
        )
        comparison = getattr(result, "comparison", None)
        comparison_passed = None if comparison is None else bool(getattr(comparison, "passed", False))
        comparison_checked = None if comparison is None else int(getattr(comparison, "checked_rows", 0))
        mismatches = tuple(getattr(comparison, "mismatches", ()) or ()) if comparison is not None else ()
        comparison_mismatch_count = None if comparison is None else int(
            getattr(comparison, "mismatch_count", len(mismatches))
        )
        comparison_fields = None if comparison is None else list(getattr(comparison, "compared_fields", ()) or ())
        max_xc_error_lsb: int | None = None
        xc_errors = [
            abs(int(getattr(item, "expected")) - int(getattr(item, "actual")))
            for item in mismatches
            if getattr(item, "field", None) == "xc_fixed"
        ]
        if xc_errors:
            max_xc_error_lsb = max(xc_errors)
        elif comparison_passed is True and comparison_checked is not None and comparison_checked > 0:
            max_xc_error_lsb = 0
        metadata = getattr(result, "metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        metrics: dict[str, Any] = {
            "diagnostic_row_count": len(rows),
            "valid_rows": valid_rows,
            "valid_single_candidate_rows": valid_single_rows,
            "comparison_passed": comparison_passed,
            "comparison_checked_rows": comparison_checked,
            "comparison_mismatch_count": comparison_mismatch_count,
            "comparison_fields": comparison_fields,
            "max_xc_error_lsb": max_xc_error_lsb,
        }
        for name in ("nominal_motor_steps", "nominal_motor_steps_calibrated", "nominal_motor_rpm", "motor_request_id"):
            if name in metadata:
                metrics[name] = metadata[name]
        return metrics

    def prewarm(self) -> None:
        """Warm image/font imports before a motion task owns BSG1."""

        import numpy  # noqa: F401
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (8, 8))
        ImageDraw.Draw(image).text((0, 0), "", font=ImageFont.load_default())

    @staticmethod
    def build_canvas(result: Any, *, position_index: int = 0, error: str | None = None) -> bytes:
        import numpy as np
        from PIL import Image, ImageDraw

        frame = getattr(result, "raw_frame", None)
        raw = bytes(getattr(result, "raw_bytes", getattr(frame, "pixels", b"")))
        width, height = int(getattr(frame, "width", FRAME_WIDTH)), int(getattr(frame, "height", FRAME_HEIGHT))
        if (width, height) != (FRAME_WIDTH, FRAME_HEIGHT) or len(raw) != FRAME_WIDTH * FRAME_HEIGHT:
            raise ValueError("Stage B HDMI canvas requires the complete 752x480 gray frame")
        canvas = np.zeros((HDMI_HEIGHT, HDMI_WIDTH, 3), dtype=np.uint8)
        canvas[:, :] = (16, 24, 36)  # BGR background
        x0, y0 = (HDMI_WIDTH - width) // 2, (HDMI_HEIGHT - height) // 2
        gray = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
        canvas[y0:y0 + height, x0:x0 + width, :] = gray[:, :, None]
        rows = getattr(getattr(result, "diagnostics", None), "rows", ())
        for row in rows:
            if not bool(getattr(row, "valid", False)) or int(getattr(row, "candidate_count", 0)) != 1:
                continue
            x = int(round(int(getattr(row, "xc_fixed", 0)) / float(1 << 12)))
            y = int(getattr(row, "row", -1))
            if not (0 <= x < width and 0 <= y < height):
                continue
            cx, cy = x0 + x, y0 + y
            canvas[max(y0, cy - 1):min(HDMI_HEIGHT, cy + 2), max(x0, cx - 1):min(HDMI_WIDTH, cx + 2)] = (0, 255, 0)
        image = Image.fromarray(canvas[:, :, ::-1])
        draw = ImageDraw.Draw(image)
        request_id = int(getattr(result, "request_id", 0))
        metrics = StageBHDMIAdapter._capture_metrics(result)
        angle = int(getattr(result, "angle_raw", 0))
        comparison = getattr(result, "comparison", None)
        detail = error or ("comparison_failed" if comparison is not None and not bool(getattr(comparison, "passed", False)) else "none")
        draw.text((24, 16), f"STAGE B | request={request_id} | position={int(position_index)}", fill=(255, 255, 255))
        draw.text((24, 38), f"angle_raw={angle} | angle_calibrated=False | error={detail}", fill=(255, 224, 128))
        draw.text(
            (24, 60),
            f"valid_rows={metrics['valid_rows']}/{metrics['diagnostic_row_count']} | "
            f"single_candidate_rows={metrics['valid_single_candidate_rows']} | "
            f"max_xc_error_lsb={metrics['max_xc_error_lsb']}",
            fill=(180, 240, 180),
        )
        if "nominal_motor_steps" in metrics:
            draw.text(
                (24, 82),
                f"nominal_motor_steps={metrics['nominal_motor_steps']} | calibrated=False",
                fill=(255, 224, 128),
            )
        return np.asarray(image, dtype=np.uint8)[:, :, ::-1].tobytes()

    def show_capture(self, result: Any, *, position_index: int | None = None, error: str | None = None) -> dict[str, Any]:
        if position_index is None:
            position_index = self._next_position
        self._next_position = int(position_index) + 1
        metrics = self._capture_metrics(result)
        receipt = {
            "request_id": int(getattr(result, "request_id", 0)),
            "camera_epoch": int(getattr(result, "epoch", 0)),
            "position_index": int(position_index),
            "angle_raw": int(getattr(result, "angle_raw", 0)),
            "angle_calibrated": bool(getattr(result, "angle_valid", False)),
            "canvas_width": HDMI_WIDTH,
            "canvas_height": HDMI_HEIGHT,
            "raw_width": FRAME_WIDTH,
            "raw_height": FRAME_HEIGHT,
            "valid_rows": metrics["valid_rows"],
            "valid_single_candidate_rows": metrics["valid_single_candidate_rows"],
            # Keep the historical spelling for existing consumers.
            "valid_single_candidate_centres": metrics["valid_single_candidate_rows"],
            "diagnostic_row_count": metrics["diagnostic_row_count"],
            "comparison_passed": metrics["comparison_passed"],
            "comparison_checked_rows": metrics["comparison_checked_rows"],
            "comparison_mismatch_count": metrics["comparison_mismatch_count"],
            "comparison_fields": metrics["comparison_fields"],
            "max_xc_error_lsb": metrics["max_xc_error_lsb"],
            "error": error,
        }
        for name in ("nominal_motor_steps", "nominal_motor_steps_calibrated", "nominal_motor_rpm", "motor_request_id"):
            if name in metrics:
                receipt[name] = metrics[name]
        previous = self.last_frame_bgr
        try:
            candidate = None
            if self.no_display or self.display is None:
                receipt["display_deferred"] = True
            else:
                candidate = self.build_canvas(result, position_index=position_index, error=error)
                configure = getattr(self.display, "configure", None)
                if not self.configured and callable(configure):
                    configure()
                    self.configured = True
                show = getattr(self.display, "show", None)
                if not callable(show):
                    raise StageBRuntimeError("HDMI output lacks bounded show(raw_bgr)")
                display_receipt = show(candidate)
                receipt["display"] = dict(display_receipt) if isinstance(display_receipt, Mapping) else {"receipt": display_receipt}
        except Exception:
            self.last_frame_bgr = previous
            self.last_receipt = {**receipt, "display_failed": True, "last_frame_retained": previous is not None}
            raise
        if candidate is not None:
            self.last_frame_bgr = candidate
        self.last_receipt = receipt
        return dict(receipt)

    def close(self) -> None:
        if self.display is not None:
            close = getattr(self.display, "close", None)
            if callable(close):
                close()


class PynqStageBRuntime:
    """Owned PYNQ objects and the current MotionSession."""

    def __init__(
        self,
        *,
        artifacts: Mapping[str, Any],
        manifest_record: Mapping[str, Any],
        overlay: Any,
        transport: Any,
        controller: Any,
        guard: GuardController,
        motor: MotorController,
        session: MotionSession,
        display: StageBHDMIAdapter,
        settle_binding: Mapping[str, Any] | None,
        settle_sampler: Any | None,
        motion_confirmation: Mapping[str, Any] | None,
        driver_proof: Mapping[str, Any] | None,
        hardware_lock: HardwareProcessLock,
        runtime_lock_owned: bool,
        capture_configuration: Mapping[str, Any],
        output: Path,
    ) -> None:
        self.artifacts = dict(artifacts)
        self.manifest_record = dict(manifest_record)
        self.overlay = overlay
        self.transport = transport
        self.controller = controller
        self.guard = guard
        self.motor = motor
        self.session = session
        self.display = display
        self.settle_binding = dict(settle_binding) if settle_binding is not None else None
        self.settle_sampler = settle_sampler
        self.motion_confirmation = dict(motion_confirmation) if motion_confirmation is not None else None
        self.driver_proof = dict(driver_proof) if driver_proof is not None else None
        self.hardware_lock = hardware_lock
        self.runtime_lock_owned = bool(runtime_lock_owned)
        self.frame_period_s: float | None = None
        self.capture_configuration = json.loads(json.dumps(dict(capture_configuration), sort_keys=True, default=str))
        self.output = output
        self.closed = False

    def signals(self) -> dict[str, Any]:
        """Run the accepted reset/SCCB/independent-monitor camera routines."""

        scripts = PROJECT_ROOT / "board" / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from gray_capture_board import verify_camera_control
        from verify_p2_board import release_camera
        from structured_light.p2.camera_evidence import assess_signals, run_monitor

        self.output.mkdir(parents=True, exist_ok=True)
        if (self.output / "signals.json").exists():
            raise StageBRuntimeError(f"refusing to overwrite existing Stage B receipt {self.output / 'signals.json'}")
        try:
            diag = self.transport._diag_mmio
            release_camera(diag)
            camera = verify_camera_control(diag, self.output)
            monitor = run_monitor(diag)
            pclk = min(int(value) for value in camera.get("pclk_hz", ()) if int(value) > 0)
            signals = assess_signals(monitor, width=FRAME_WIDTH, height=FRAME_HEIGHT, pclk_hz=pclk, frames=16)
            if not signals.get("camera_signal_verified"):
                raise StageBRuntimeError("camera signal monitor did not pass")
            self.frame_period_s = float(signals["frame_period_s"])
            payload = {
                "stage": "signals", "camera_control": camera, "monitor": monitor,
                "signals": signals, "artifacts": self.artifacts["files"], "passed": True,
            }
        except Exception as exc:
            self._write_json_no_overwrite("signals.json", {
                "stage": "signals", "passed": False,
                "error_type": type(exc).__name__, "error": str(exc),
                "artifacts": self.artifacts["files"],
            })
            raise
        self._write_json_no_overwrite("signals.json", payload)
        return payload

    def _write_json_no_overwrite(self, name: str, payload: Mapping[str, Any]) -> Path:
        self.output.mkdir(parents=True, exist_ok=True)
        path = self.output / name
        if path.exists():
            raise StageBRuntimeError(f"refusing to overwrite existing Stage B receipt {path}")
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")
            stream.flush()
            import os
            os.fsync(stream.fileno())
        return path

    def _write_jsonl_no_overwrite(self, name: str, values: Sequence[Mapping[str, Any]]) -> Path:
        self.output.mkdir(parents=True, exist_ok=True)
        path = self.output / name
        if path.exists():
            raise StageBRuntimeError(f"refusing to overwrite existing Stage B receipt {path}")
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            for value in values:
                stream.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str) + "\n")
            stream.flush()
            import os
            os.fsync(stream.fileno())
        return path

    def _publish_motion_evidence(self) -> dict[str, Any]:
        """Promote the exact owner/session outputs to the runtime evidence root."""

        task = getattr(self.session, "last_task_receipt", None)
        if not isinstance(task, Mapping):
            raise StageBRuntimeError("MotionSession did not publish a task receipt")
        task_copy = json.loads(json.dumps(dict(task), default=str))
        task_path = self._write_json_no_overwrite("task_receipt.json", task_copy)
        config = getattr(self.session, "config", None)
        if config is None:
            raise StageBRuntimeError("MotionSession has no motion configuration for evidence")
        motion_config = {
            "schema_version": 1,
            "positions": int(task.get("positions_requested", getattr(config, "positions", 0))),
            "pulses_per_position": int(getattr(config, "pulses_per_position")),
            "rpm": float(getattr(config, "rpm")),
            "direction": int(getattr(config, "direction")),
            "period_cycles": int(getattr(config, "period_cycles")),
            "high_cycles": int(getattr(config, "high_cycles")),
            "dir_setup_cycles": int(getattr(config, "dir_setup_cycles")),
            "enable_setup_cycles": int(getattr(config, "enable_setup_cycles")),
            "motor_lease_cycles": int(getattr(config, "motor_lease_cycles")),
            "motor_timeout_cycles": int(getattr(config, "motor_timeout_cycles")),
            "motor_timeout_s": float(getattr(config, "motor_timeout_s")),
            "guard_lease_cycles": int(getattr(config, "guard_lease_cycles")),
            "guard_timeout_ms": int(getattr(config, "guard_timeout_ms")),
            "capture_timeout_s": float(getattr(config, "capture_timeout_s")),
            "capture_phase_timeout_s": float(getattr(config, "capture_phase_timeout_s")),
            "overlay_files": json.loads(json.dumps(self.artifacts.get("files", {}), default=str)),
        }
        config_path = self._write_json_no_overwrite("motion_config.json", motion_config)
        trace = task.get("motion_start_trace", ())
        if not isinstance(trace, (list, tuple)) or not all(isinstance(item, Mapping) for item in trace):
            raise StageBRuntimeError("MotionSession task receipt has no valid motor START trace")
        events_path = self._write_jsonl_no_overwrite("motion_events.jsonl", trace)
        return {
            "task_receipt": str(task_path),
            "motion_config": str(config_path),
            "motion_events": str(events_path),
        }

    @staticmethod
    def _capture_payload(result: Any, *, index: int, mode: str) -> dict[str, Any]:
        if mode == "raw":
            frame = result
            return {
                "index": index, "mode": mode, "request_id": None, "epoch": None,
                "raw_actual_length": len(getattr(frame, "pixels", b"")),
                "raw_sha256": getattr(frame, "sha256", None),
                "profile_actual_length": None, "comparison_passed": None,
            }
        comparison = getattr(result, "comparison", None)
        return {
            "index": index, "mode": mode, "request_id": getattr(result, "request_id", None),
            "epoch": getattr(result, "epoch", None), "raw_actual_length": len(getattr(result, "raw_bytes", b"")),
            "raw_sha256": getattr(result, "input_sha256", None),
            "profile_actual_length": len(getattr(result, "profile_payload", b"")),
            "profile_sha256": hashlib.sha256(getattr(result, "profile_payload", b"")).hexdigest(),
            "comparison_passed": None if comparison is None else bool(getattr(comparison, "passed", False)),
        }

    def first_frame(self) -> Any:
        self.output.mkdir(parents=True, exist_ok=True)
        if (self.output / "first_frame.json").exists():
            raise StageBRuntimeError(f"refusing to overwrite existing Stage B receipt {self.output / 'first_frame.json'}")
        try:
            result = self.session.run_static(raw=False)
        except Exception as exc:
            self._write_json_no_overwrite("first_frame.json", {
                "stage": "first-frame", "passed": False,
                "error_type": type(exc).__name__, "error": str(exc),
                "artifacts": self.artifacts["files"],
            })
            raise
        payload = self._capture_payload(result, index=0, mode="profile")
        self._write_json_no_overwrite("first_frame.json", {"stage": "first-frame", "passed": True, "artifacts": self.artifacts["files"], **payload})
        return result

    def static(self, *, count: int = 1000, mode: str = "profile") -> dict[str, Any]:
        if mode not in {"raw", "profile"}:
            raise ValueError("static mode must be raw or profile")
        if type(count) is not int or not 1 <= count <= 1000:
            raise ValueError("static count must be in 1..1000")
        records: list[dict[str, Any]] = []
        records_path = self.output / f"{mode}_static_records.jsonl"
        summary_name = f"{mode}_static_summary.json"
        if records_path.exists() or (self.output / summary_name).exists():
            raise StageBRuntimeError(f"refusing to overwrite existing static output for {mode}")
        self.output.mkdir(parents=True, exist_ok=True)
        stream = records_path.open("x", encoding="utf-8", newline="\n")
        for index in range(count):
            started = time.monotonic()
            try:
                result = self.session.run_static(raw=(mode == "raw"))
                record = {
                    **self._capture_payload(result, index=index, mode=mode),
                    "elapsed_s": time.monotonic() - started, "passed": True,
                }
                if mode == "raw":
                    mmio = getattr(getattr(self.controller, "transport", None), "mmio", None)
                    record["request_id"] = getattr(mmio, "request_id", None)
                    record["epoch"] = getattr(mmio, "epoch", None)
                if record["raw_actual_length"] != FRAME_WIDTH * FRAME_HEIGHT:
                    raise StageBRuntimeError("P2 actual raw length does not match the manifest contract")
                if mode == "profile" and not 0 < int(record["profile_actual_length"] or 0) <= 5792:
                    raise StageBRuntimeError("P2 actual Profile length is outside the SLP2 limit")
                if mode == "profile" and record["comparison_passed"] is not True:
                    raise StageBRuntimeError("P2 same-frame comparison failed")
                if mode == "raw":
                    raw_path = self.output / f"raw_static_{index:04d}.gray8.bin"
                    if raw_path.exists():
                        raise StageBRuntimeError(f"refusing to overwrite existing raw capture {raw_path}")
                    with raw_path.open("xb") as raw_stream:
                        raw_stream.write(bytes(getattr(result, "pixels", b"")))
                        raw_stream.flush()
                        import os
                        os.fsync(raw_stream.fileno())
                    record["raw_file"] = str(raw_path)
                records.append(record)
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                stream.flush()
            except Exception as exc:
                failure = {
                    "index": index, "mode": mode, "passed": False,
                    "error_type": type(exc).__name__, "error": str(exc),
                    "elapsed_s": time.monotonic() - started,
                }
                records.append(failure)
                stream.write(json.dumps(failure, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                stream.flush()
                import os
                os.fsync(stream.fileno())
                stream.close()
                summary = {
                    "stage": f"{mode}-static", "count_requested": count,
                    "count_completed": index, "passed": False,
                    "records_path": str(records_path), "failure": failure,
                    "manifest_sha256": self.artifacts["files"]["manifest"]["sha256"],
                }
                self._write_json_no_overwrite(summary_name, summary)
                raise
        import os
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        summary = {
            "stage": f"{mode}-static", "count_requested": count, "count_completed": count,
            "passed": True, "records_path": str(records_path),
            "manifest_sha256": self.artifacts["files"]["manifest"]["sha256"],
        }
        self._write_json_no_overwrite(summary_name, summary)
        return summary

    def settle_diagnostic(self, *, sampler: Callable[[int], Sequence[Any]] | None = None, positions: int = 10) -> dict[str, Any]:
        if self.motion_confirmation is None or self.driver_proof is None:
            raise RuntimeAdmissionError("settle diagnostic requires fresh motion confirmation and driver proof")
        if positions != 10:
            raise ValueError("formal Stage B settle diagnostic requires ten positions")
        if sampler is not None or self.settle_sampler is not None:
            raise SettleBindingError("settle_diagnostic owns signal admission and pre-motion baseline preparation")
        if self.frame_period_s is None:
            self.signals()
        if self.frame_period_s is None or not math.isfinite(float(self.frame_period_s)) or float(self.frame_period_s) <= 0:
            raise RuntimeAdmissionError("settle diagnostic requires a measured positive camera frame period")

        baseline: list[Any] = []
        baseline_records: list[dict[str, float]] = []
        for index in range(20):
            started_at = time.monotonic()
            frame = self.session.run_static(raw=True)
            completed_at = time.monotonic()
            pixels = getattr(frame, "pixels", None)
            if (
                not isinstance(pixels, (bytes, bytearray, memoryview))
                or int(getattr(frame, "width", 0)) != FRAME_WIDTH
                or int(getattr(frame, "height", 0)) != FRAME_HEIGHT
                or len(pixels) != FRAME_WIDTH * FRAME_HEIGHT
            ):
                raise StageBRuntimeError(f"pre-motion baseline frame {index} violates the 752x480 gray8 contract")
            baseline.append(frame)
            baseline_records.append({
                "capture_started_monotonic_s": started_at,
                "capture_completed_monotonic_s": completed_at,
                **_p2_capture_identity(self.controller),
            })

        callback = MeasuredSettleSampler(
            self.controller,
            baseline_frames=baseline,
            baseline_capture_records=baseline_records,
            output=self.output / "settle_samples",
            roi=self.session.settle_roi,
            config=self.session.settle_config,
            config_hash=self.session.settle_config_hash,
            frame_period_s=self.frame_period_s,
            guard=self.guard if self.session.settle_config.protocol_id == EXTENDED_SETTLE_PROTOCOL_ID else None,
            expected_pulses=self.session.config.pulses_per_position,
            capture_timeout_s=self.session.config.capture_timeout_s,
            persistence_timeout_s=self.session.config.capture_phase_timeout_s,
        )
        self.settle_sampler = callback
        self.session.run_sequence(positions=10, settle_sampler=callback)
        points = list(callback.observations)
        if len(points) != 10:
            raise SettleBindingError("real settle diagnostic did not publish ten measured positions")
        if not all(isinstance(item.get("estimate"), Mapping) and item["estimate"].get("settled") is True for item in points):
            raise SettleBindingError("all ten positions must pass the selected same-pose settle reference")
        accepted_wait_s = max(float(item["accepted_wait_s"]) for item in points)
        selected_ms = max(int(item["selected_scheduled_ms"]) for item in points)
        binding = {
            "schema_version": 1,
            "target": self.session.target,
            "installation": self.session.installation,
            "mode": self.session.carrier_mode,
            "profile": self.session.profile,
            "roi": list(self.session.settle_roi),
            "config_hash": self.session.settle_config_hash,
            "accepted_wait_s": accepted_wait_s,
            "selected_scheduled_ms": selected_ms,
            "frame_period_s": float(self.frame_period_s),
            "positions": 10,
            "pulses_per_position": self.session.config.pulses_per_position,
            "rpm": 5.0,
            "points": points,
            "protocol_id": self.session.settle_config.protocol_id,
            "settle_protocol": self.session.settle_config.protocol.as_dict(),
            "schedule_ms": list(self.session.settle_config.protocol.schedule_ms),
            "baseline_manifest": "settle_samples/baseline/baseline.jsonl",
            "capture_configuration": getattr(self, "capture_configuration", {}),
            "hardware_evidence": "pynq_measured_raw",
        }
        path = self.output / "settle_config.json"
        self._write_json_no_overwrite(path.name, binding)
        self.settle_binding = binding
        return {"stage": "settle", "path": str(path), **binding}

    def _require_motion_settle(self) -> None:
        if self.motion_confirmation is None or self.driver_proof is None:
            raise RuntimeAdmissionError("formal motion requires fresh motion confirmation and driver proof")
        if self.settle_binding is None:
            raise SettleBindingError("formal motion requires a valid saved settle config")

    def single(self) -> Any:
        self._require_motion_settle()
        result = self.session.run_single(settle_sampler=self.settle_sampler)
        self._publish_motion_evidence()
        return result

    def sequence(self) -> tuple[Any, ...]:
        self._require_motion_settle()
        results = self.session.run_sequence(positions=10, settle_sampler=self.settle_sampler)
        self._publish_motion_evidence()
        return results

    def recovery(self, runner: Callable[..., Any] | None = None) -> Any:
        if runner is None:
            raise RuntimeAdmissionError("recovery requires an explicit compatible existing P2 runner")
        return runner(self)

    def _save_capture_handoff(self) -> dict[str, Any]:
        """Publish a clean checkpoint while the hardware lock is still held."""
        exporter = getattr(self.transport, "export_capture_checkpoint", None)
        if not callable(exporter):
            return {"capture_handoff": None, "handoff_reason": "transport has no handoff support"}
        try:
            checkpoint = exporter()
        except Exception as exc:
            # Closing an aborted session remains possible; it grants no new
            # process the right to guess the hidden next_epoch value.
            return {"capture_handoff": None, "handoff_reason": str(exc)}
        identity = _current_pl_identity(self.overlay, expected_bit=Path(self.artifacts["bit"]))
        if not identity.get("boot_id"):
            return {"capture_handoff": None, "handoff_reason": "current boot identity unavailable"}
        payload = {
            "loaded": True, "overlay_mode": OVERLAY_MODE,
            "active_bitfile": identity.get("active_bitfile"),
            "boot_identity": {"boot_id": identity["boot_id"]},
            **{f"{k}_sha256": self.artifacts["files"][k]["sha256"] for k in ("bit", "hwh", "manifest")},
            "capture_checkpoint": checkpoint,
            "handoff_scope": "clean capture generation only; not motion authorization",
        }
        path = self._write_json_no_overwrite("capture_handoff.json", payload)
        return {"capture_handoff": str(path)}

    def close(self) -> dict[str, Any]:
        if self.closed:
            return {"closed": True, "already_closed": True}
        errors: list[str] = []
        # MotionSession owns the bounded order for an active task.  On an idle
        # session revoke once before its normal component close path.
        if not bool(getattr(self.session, "_guard_held", False)):
            try:
                self.guard.revoke(deadline_s=self.session.config.cleanup_timeout_s)
            except Exception as exc:
                errors.append(f"guard.revoke: {type(exc).__name__}: {exc}")
        session_result: Mapping[str, Any] | None = None
        try:
            value = self.session.close()
            if isinstance(value, Mapping):
                session_result = value
        except Exception as exc:
            errors.append(f"session.close: {type(exc).__name__}: {exc}")
        if isinstance(getattr(self.session, "last_task_receipt", None), Mapping) and not (self.output / "task_receipt.json").exists():
            try:
                self._publish_motion_evidence()
            except Exception as exc:
                errors.append(f"motion evidence promotion: {type(exc).__name__}: {exc}")
        if isinstance(session_result, Mapping) and session_result.get("closed") is False:
            # A stuck worker may still own DMA/HDMI memory.  Keep the runtime
            # open and report the bounded failure; callers must not free it.
            return {
                "closed": False,
                "worker_unconfirmed": bool(session_result.get("worker_unconfirmed", True)),
                "cleanup": session_result.get("cleanup"),
                "errors": errors,
            }
        if errors:
            raise StageBRuntimeError("; ".join(errors))
        # No process may claim hardware between the final snapshot and its
        # durable receipt. A receipt failure still releases the clean owner.
        try:
            handoff = self._save_capture_handoff()
        except Exception:
            if self.runtime_lock_owned:
                self.hardware_lock.release()
                self.runtime_lock_owned = False
            self.closed = True
            raise
        if self.runtime_lock_owned:
            try:
                self.hardware_lock.release()
            except Exception as exc:
                raise StageBRuntimeError(f"hardware owner lock release failed: {exc}") from exc
            self.runtime_lock_owned = False
        close_receipt = {
            "schema_version": 1,
            "closed": True,
            "capture_handoff": handoff.get("capture_handoff"),
            "handoff": dict(handoff),
            "cleanup": dict(getattr(self.session, "last_cleanup", {}) or {}),
            "task_receipt": str(self.output / "task_receipt.json") if (self.output / "task_receipt.json").is_file() else None,
        }
        self._write_json_no_overwrite("close_receipt.json", close_receipt)
        self.closed = True
        return {"closed": True, **handoff}


def _create_stage_b_runtime_locked(
    manifest_path: str | Path,
    *,
    stage: str | None = None,
    load: bool = False,
    attach: bool = False,
    power_off_confirmation: str | Path | Mapping[str, Any] | bool | None = None,
    loaded_overlay_receipt: str | Path | Mapping[str, Any] | None = None,
    motion_confirmation: str | Path | Mapping[str, Any] | None = None,
    driver_proof: str | Path | Mapping[str, Any] | None = None,
    overlay: Any | None = None,
    output: str | Path | None = None,
    settle_config_path: str | Path | None = None,
    settle_protocol: str = LEGACY_SETTLE_PROTOCOL_ID,
    target: str = "pynq_z2",
    installation: str = "stage_b_candidate",
    roi: tuple[int, int, int, int] = (0, 0, FRAME_WIDTH, FRAME_HEIGHT),
    settle_roi: tuple[int, int, int, int] | None = None,
    stripe_roi: tuple[int, int, int, int] = (0, 0, FRAME_WIDTH, FRAME_HEIGHT),
    threshold: int = 30,
    config: MotionConfig | None = None,
    no_display: bool = False,
    hardware_lock: HardwareProcessLock,
    runtime_lock_owned: bool,
) -> PynqStageBRuntime:
    """Create one real Stage B runtime after static admission checks."""

    artifacts = verify_stage_b_artifacts(manifest_path)
    resolved_settle_roi = _resolve_settle_roi(roi, settle_roi)
    resolved_stripe_roi = tuple(stripe_roi)
    if load and attach:
        raise RuntimeAdmissionError("--load and --attach are mutually exclusive")
    if stage in {"settle", "single", "sequence"}:
        if load or not attach:
            raise RuntimeAdmissionError("formal motion stages require attach=True and never download an Overlay")
        if motion_confirmation is None or driver_proof is None:
            raise RuntimeAdmissionError("formal motion stages require fresh motion and driver confirmations")
    if stage in {"signals", "first-frame", "static", "raw-static", "profile-static"} and not load and not attach:
        raise RuntimeAdmissionError("signals/first-frame/static require load=True or attach=True")
    if overlay is None and not load and not attach:
        raise RuntimeAdmissionError("explicit load=True or attach=True is required when no Overlay is injected")
    if load and not _power_off_confirmed(power_off_confirmation):
        raise RuntimeAdmissionError("Overlay load requires current physical_power_off_confirmed input")
    confirmed_motion = _motion_confirmation(motion_confirmation) if motion_confirmation is not None else None
    confirmed_driver = _driver_readonly_proof(driver_proof) if driver_proof is not None else None
    manifest = _json_load(manifest_path)
    # Validate the protocol and derive its settle config before importing any
    # PYNQ/P2 modules or issuing a board command.  A v2 default factory config
    # also uses the bounded ten-millisecond host poll requested for this path.
    settle_cfg = SettleConfig(protocol_id=settle_protocol)
    motion_config = config or MotionConfig(
        host_poll_max_s=0.010 if settle_cfg.protocol_id == EXTENDED_SETTLE_PROTOCOL_ID else 0.025,
    )
    _validate_motion_config(motion_config)
    from structured_light.p2.contract import load_board_contract
    from structured_light.p2.controller import P2Controller
    from .capture_handoff import HandoffPynqTransport as PynqTransport
    from structured_light.p2.session import SessionRecorder

    if attach and overlay is None:
        try:
            from pynq import Overlay  # type: ignore
        except ImportError as exc:
            raise RuntimeAdmissionError("PYNQ is required for --attach") from exc
        overlay = Overlay(artifacts["bit"], download=False)
    if attach:
        verify_loaded_overlay_receipt(loaded_overlay_receipt, artifacts=artifacts, overlay=overlay)

    contract = load_board_contract(artifacts["contract"])
    transport = PynqTransport(
        artifacts["bit"], artifacts["hwh"], artifacts["manifest"],
        frame_width=FRAME_WIDTH, frame_height=FRAME_HEIGHT,
        expected_addresses=dict(P2_OVERLAY_ADDRESSES), overlay=overlay,
        contract=contract, overlay_mode=OVERLAY_MODE,
        contract_path=artifacts["contract"], strict_manifest=True,
    )
    # A new Python process does not imply a freshly reset PL. Restore the
    # exact completed generation before configuration writes or DMA arming.
    if attach:
        record = _loaded_overlay_receipt(loaded_overlay_receipt)
        transport.restore_capture_checkpoint(record.get("capture_checkpoint"))
    motor_mmio = MMIO32(_overlay_mmio(transport.overlay, "motor_controller", MOTION_ADDRESSES["motor_controller"], contract.window), base_address=MOTION_ADDRESSES["motor_controller"], name="MFP1")
    guard_mmio = MMIO32(_overlay_mmio(transport.overlay, "bsg1_guard", MOTION_ADDRESSES["bsg1_guard"], contract.window), base_address=MOTION_ADDRESSES["bsg1_guard"], name="BSG1")
    guard = GuardController(guard_mmio, carrier_mode=CarrierMode.DIRECT_WIRE, manifest=manifest)
    motor = MotorController(motor_mmio, poll_interval_s=motion_config.host_poll_max_s)
    # These read-only ABI checks are deliberately after artifact/PL identity
    # admission and before any camera or motion command is issued.
    guard_identity = guard.validate_identity()
    motor_identity = motor.validate_identity()
    root = Path(output) if output is not None else Path(manifest_path).resolve().parent / "stage_b_runtime"
    if root.exists() and any(root.iterdir()):
        raise StageBRuntimeError(f"refusing to overwrite existing Stage B output directory {root}")
    camera_admission = prepare_camera_control(transport._diag_mmio, initialize=load)
    recorder = SessionRecorder(root / "sessions", metadata={
        "stage": "B", "hardware": "real_pynq_z2", "overlay": OVERLAY_NAME,
        "overlay_mode": OVERLAY_MODE, "artifacts": artifacts["files"],
        "guard_identity": guard_identity, "motor_identity": motor_identity,
        "camera_ready_before_configuration": camera_admission,
    })
    controller = P2Controller(transport, recorder=recorder, session_id=recorder.session_id,
                              default_timeout_s=max(2.0, motion_config.capture_timeout_s))
    if int(transport._diag_mmio.read(0x08)) & 5 != 1:
        raise RuntimeAdmissionError('static defaults did not establish full camera ready state')
    capture_configuration = _configure_capture_configuration(
        controller,
        stripe_roi=resolved_stripe_roi,
        settle_roi=resolved_settle_roi,
        threshold=threshold,
        capture_timeout_s=motion_config.capture_timeout_s,
        capture_phase_timeout_s=motion_config.capture_phase_timeout_s,
        artifacts=artifacts["files"],
    )
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "capture_config.json"
    try:
        with config_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(capture_configuration, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            import os
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise StageBRuntimeError(f"refusing to overwrite capture configuration evidence {config_path}") from exc
    recorder.event("capture_configuration", **capture_configuration)
    display_output = None
    if not no_display:
        from structured_light.p2.hdmi_output import create_output

        display_output = create_output(transport.overlay, artifacts["manifest"], background_owner=False)
    display = StageBHDMIAdapter(display_output, no_display=no_display)
    # Import PIL/NumPy/font resources before MotionSession can ARM BSG1. The
    # no-display path remains a metrics-only fast path for settle diagnostics.
    display.prewarm()
    binding = None
    if settle_config_path is not None:
        binding = require_settle_binding(
            settle_config_path, target=target, installation=installation,
            roi=resolved_settle_roi, mode=CarrierMode.DIRECT_WIRE.value, profile="SLP2", config=settle_cfg,
        )
    settle_sampler = None
    if binding is not None:
        settle_sampler = SavedSettleSampler(controller, binding)
    session = MotionSession(
        guard, motor, controller, display=display, output=root / "records",
        config=motion_config, settle_config=settle_cfg, settle_roi=resolved_settle_roi,
        target=target, installation=installation, carrier_mode=CarrierMode.DIRECT_WIRE,
        profile="SLP2", mode="pynq", hardware_lock=hardware_lock,
    )
    return PynqStageBRuntime(
        artifacts=artifacts, manifest_record=manifest, overlay=transport.overlay,
        transport=transport, controller=controller, guard=guard, motor=motor,
        session=session, display=display, settle_binding=binding,
        settle_sampler=settle_sampler, motion_confirmation=confirmed_motion,
        driver_proof=confirmed_driver, hardware_lock=hardware_lock,
        runtime_lock_owned=runtime_lock_owned, capture_configuration=capture_configuration, output=root,
    )


def create_stage_b_runtime(
    manifest_path: str | Path,
    *,
    stage: str | None = None,
    load: bool = False,
    attach: bool = False,
    power_off_confirmation: str | Path | Mapping[str, Any] | bool | None = None,
    loaded_overlay_receipt: str | Path | Mapping[str, Any] | None = None,
    motion_confirmation: str | Path | Mapping[str, Any] | None = None,
    driver_proof: str | Path | Mapping[str, Any] | None = None,
    overlay: Any | None = None,
    output: str | Path | None = None,
    settle_config_path: str | Path | None = None,
    settle_protocol: str = LEGACY_SETTLE_PROTOCOL_ID,
    target: str = "pynq_z2",
    installation: str = "stage_b_candidate",
    roi: tuple[int, int, int, int] = (0, 0, FRAME_WIDTH, FRAME_HEIGHT),
    settle_roi: tuple[int, int, int, int] | None = None,
    stripe_roi: tuple[int, int, int, int] = (0, 0, FRAME_WIDTH, FRAME_HEIGHT),
    threshold: int = 30,
    config: MotionConfig | None = None,
    no_display: bool = False,
    hardware_lock: HardwareProcessLock | None = None,
    hardware_lock_path: str | Path | None = None,
) -> PynqStageBRuntime:
    """Acquire the shared preview lock before any Overlay load or attach."""

    # Reject a mistyped protocol before acquiring the hardware owner lock or
    # entering the locked factory where PYNQ imports are permitted.
    get_settle_protocol(settle_protocol)
    planned_output = Path(output) if output is not None else Path(manifest_path).resolve().parent / "stage_b_runtime"
    if planned_output.exists() and any(planned_output.iterdir()):
        raise StageBRuntimeError(f"refusing to overwrite existing Stage B output directory {planned_output}")
    lock = hardware_lock or HardwareProcessLock(
        hardware_lock_path or HardwareProcessLock.DEFAULT_PATH,
        label="stage_b_runtime",
    )
    was_acquired = bool(lock.acquired)
    lock.acquire()
    runtime_owned = not was_acquired
    try:
        return _create_stage_b_runtime_locked(
            manifest_path, stage=stage, load=load, attach=attach,
            power_off_confirmation=power_off_confirmation,
            loaded_overlay_receipt=loaded_overlay_receipt,
            motion_confirmation=motion_confirmation, driver_proof=driver_proof,
            overlay=overlay, output=output, settle_config_path=settle_config_path,
            settle_protocol=settle_protocol,
            target=target, installation=installation, roi=roi, settle_roi=settle_roi,
            stripe_roi=stripe_roi, threshold=threshold, config=config,
            no_display=no_display, hardware_lock=lock,
            runtime_lock_owned=runtime_owned,
        )
    except Exception:
        # The runtime did not become the owner, so initialization failure must
        # release only the lock acquired by this context.  A pre-acquired lock
        # remains owned by its original context.
        if runtime_owned:
            lock.release()
        raise


# Descriptive aliases used by board notebooks and integration callers.
build_stage_b_runtime = create_stage_b_runtime
create_pynq_runtime = create_stage_b_runtime
create_stage_b_adapter = create_stage_b_runtime
verify_overlay_contract = verify_stage_b_artifacts
verify_b_overlay = verify_stage_b_artifacts
verifyOverlayArtifacts = verify_stage_b_artifacts
returnSettleEstimate = return_settle_estimate
StageBRuntime = PynqStageBRuntime
PynqStageBAdapter = PynqStageBRuntime
PynqRuntime = PynqStageBRuntime
PynqMMIOTransport = MMIO32


__all__ = [
    "ALL_ADDRESSES", "ArtifactContractError", "CachedSettleSamples", "MeasuredSettleSamples", "MMIO32", "MeasuredSettleSampler",
    "MOTION_ADDRESSES", "OVERLAY_MODE", "OVERLAY_NAME", "P2_OVERLAY_ADDRESSES",
    "PynqMMIOTransport", "PynqRuntime", "PynqStageBRuntime", "RuntimeAdmissionError",
    "SETTLE_PROTOCOL_IDS", "get_settle_protocol",
    "SavedSettleSampler", "SettleBindingError", "StageBHDMIAdapter", "StageBRuntime", "StageBRuntimeError",
    "build_stage_b_runtime", "create_pynq_runtime", "create_stage_b_adapter", "create_stage_b_runtime",
    "verify_b_overlay", "verify_overlay_contract", "verifyOverlayArtifacts", "returnSettleEstimate",
    "require_settle_binding", "return_settle_estimate", "select_settle_wait", "verify_loaded_overlay_receipt", "verify_stage_b_artifacts",
]
