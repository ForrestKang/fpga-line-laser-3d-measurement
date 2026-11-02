"""Lazy PYNQ transport and bounded AXI DMA S2MM adapter.

Importing this module on a PC never imports ``pynq``.  The hardware class is
constructed only after bit/HWH/manifest checks pass.  The adapter polls DMA
status with a monotonic deadline and does not call PYNQ's unbounded wait/start/
stop helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any
import xml.etree.ElementTree as ET

from .capabilities import Capabilities
from .contract import (
    BoardContract,
    DIAGNOSTIC_VERSION, SUPPORTED_DIAGNOSTIC_VERSIONS, RAW_OVERLAY_MODES, PROFILE_OVERLAY_MODES,
    DIAG_OFFSETS,
    MAX_PROFILE_BYTES,
    MAX_SLP2_PACKET_BYTES,
    PROFILE_FORMAT_VERSION,
    REG_CONTROL,
    REG_ERROR_FLAGS,
    REGMAP_VERSION,
    REG_PROFILE_ID,
    REG_STATUS,
    REG_TIMESTAMP_LOW,
    REG_VERSION,
    TARGET_DEVICE,
    load_board_contract,
)
from .errors import BusyError, CapabilityError, ContractError, DMAError, ProtocolError, CaptureTimeout, VersionMismatch
from .mmio import MockMMIO
from .transport import CaptureTransport, TransportCapture
from .types import DiagnosticRow, DiagnosticsSnapshot


RUNTIME_CAPABILITY_BITS = {
    "raw": 1 << 0,
    "profile": 1 << 1,
    "row_diagnostics": 1 << 2,
    "camera_io": 1 << 3,
    "single_capture": 1 << 4,
}


# Vivado's generated HWH keeps the HDMI hierarchy in FULLNAME but exposes the
# flattened INSTANCE names in the address table.  The first alias in each
# tuple is the current combined profile+HDMI build.  The explicit frontend
# aliases are retained only for the older raw-HDMI handoff; this is an
# allow-list, not a suffix or substring match.
HDMI_HWH_BINDINGS = {
    "hdmi_vdma": (("hdmi_vdma", "S_AXI_LITE"),),
    "hdmi_vtc": (
        ("hdmi_out_vtc_out", "ctrl"),
        ("hdmi_out/vtc_out", "ctrl"),
        ("hdmi_out/frontend/vtc_out", "ctrl"),
    ),
    "hdmi_dynclk": (
        ("hdmi_out_axi_dynclk", "s00_axi"),
        ("hdmi_out/axi_dynclk", "s00_axi"),
        ("hdmi_out/frontend/axi_dynclk", "s00_axi"),
    ),
}


def _hwh_instance_matches(actual: Any, expected: str) -> bool:
    """Match one explicitly known HWH instance spelling."""

    actual_name = str(actual).strip("/").upper()
    expected_name = str(expected).strip("/").upper()
    return actual_name in {expected_name, expected_name.replace("/", "_")}


def sha256_file(path: str | Path) -> str:
    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hex(value: Any, name: str) -> int:
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as exc:
            raise ContractError(f"{name} is not an integer: {value!r}") from exc
    if type(value) is int:
        return value
    raise ContractError(f"{name} is missing or not an integer")


_R31_REVISION_INDEX_NAME = "DEPLOYMENT_REVISION_INDEX.json"
_R31_REVOKED_STATUS_TOKENS = ("SUPERSEDED", "DO_NOT_LOAD", "BLOCKED", "REVOKED", "NOT_ADMITTED")


def _is_r31_revision_manifest(record: dict[str, Any], manifest_path: Path) -> bool:
    """Identify R31 load manifests, including ones with edited identity fields."""

    source_revision = record.get("source_revision")
    status = record.get("status")
    if (
        (isinstance(source_revision, str) and source_revision.upper().startswith("R31_"))
        or (isinstance(status, str) and status.upper().startswith("R31_"))
        or ("critical_cdc_review" in record and "production_release_passed" in record)
    ):
        return True

    # Every R31 implementation manifest lives under its candidate's pl/deploy
    # tree. Keep older R24-R30 artifacts outside this hardware-load gate, while
    # preventing removal of the R31 marker fields from bypassing revision-index
    # validation.
    parts = tuple(part.casefold() for part in manifest_path.resolve().parts)
    return any(parts[index : index + 2] == ("pl", "deploy") for index in range(len(parts) - 1))


def _locate_r31_revision_index(manifest_path: Path) -> Path:
    """Find the package-local/current-project R31 revision index deterministically."""

    candidates = [manifest_path.parent / _R31_REVISION_INDEX_NAME]
    for ancestor in manifest_path.parents:
        candidates.append(ancestor / "evidence" / _R31_REVISION_INDEX_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ContractError(
        "R31 hardware load requires DEPLOYMENT_REVISION_INDEX.json; no current revision index found"
    )


def _revision_index_path(value: Any, index_path: Path, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"R31 revision index {field} is missing")
    path = Path(value)
    # Release bundles should use a relative path rooted at the index location.
    # Source-tree indexes may use an absolute path; it must still resolve to
    # the exact manifest presented to the loader.
    resolved = (path if path.is_absolute() else index_path.parent / path).resolve()
    root = _revision_index_root(index_path)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"R31 revision index {field} escapes its candidate root") from exc
    return resolved


def _revision_index_root(index_path: Path) -> Path:
    """Return the relocatable candidate root containing the revision index."""

    parent = index_path.parent.resolve()
    if parent.name.casefold() == "evidence":
        return parent.parent
    return parent


def _require_current_r31_revision(record: dict[str, Any], manifest_path: Path) -> None:
    """Fail closed unless this exact manifest is the currently admitted R31 release."""

    if not _is_r31_revision_manifest(record, manifest_path):
        return

    if record.get("preboard_passed") is not True:
        raise ContractError("R31 manifest preboard_passed must be true before Overlay load")
    status = record.get("status")
    if isinstance(status, str) and any(token in status.upper() for token in _R31_REVOKED_STATUS_TOKENS):
        raise ContractError("R31 manifest status is revoked, superseded, blocked, or not admitted")

    index_path = _locate_r31_revision_index(manifest_path)
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read R31 revision index {index_path}: {exc}") from exc
    if not isinstance(index, dict):
        raise ContractError("R31 revision index must contain a JSON object")
    if index.get("hardware_load_admitted") is not True:
        raise ContractError("current R31 revision index does not admit hardware loading")

    selected_path = _revision_index_path(index.get("current_manifest"), index_path, "current_manifest")
    manifest = manifest_path.resolve()
    if selected_path != manifest:
        raise ContractError("R31 manifest is not the current revision-index manifest")

    selected_sha = index.get("current_sha256")
    if not isinstance(selected_sha, str) or len(selected_sha) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in selected_sha
    ):
        raise ContractError("R31 revision index current_sha256 is invalid")
    actual_sha = sha256_file(manifest)
    if actual_sha.lower() != selected_sha.lower():
        raise ContractError("R31 manifest SHA-256 does not match the current revision index")

    records = index.get("records")
    if not isinstance(records, list):
        raise ContractError("R31 revision index records are missing or invalid")
    matching_records = []
    for item in records:
        if not isinstance(item, dict):
            raise ContractError("R31 revision index contains an invalid record")
        item_path = _revision_index_path(item.get("path"), index_path, "record path")
        if item_path == manifest:
            matching_records.append(item)
    if len(matching_records) != 1:
        raise ContractError("R31 current manifest identity is missing or ambiguous in revision index")

    current_record = matching_records[0]
    record_sha = current_record.get("sha256")
    if not isinstance(record_sha, str) or record_sha.lower() != actual_sha.lower():
        raise ContractError("R31 current manifest record SHA-256 does not match")
    record_status = current_record.get("status")
    if not isinstance(record_status, str) or any(
        token in record_status.upper() for token in _R31_REVOKED_STATUS_TOKENS
    ):
        raise ContractError("R31 current manifest record is revoked, superseded, blocked, or not admitted")
    if current_record.get("preboard_passed") is not True:
        raise ContractError("R31 current manifest record preboard_passed must be true")


def verify_overlay_artifacts(
    bit_path: str | Path,
    hwh_path: str | Path,
    manifest_path: str | Path,
    *,
    expected_addresses: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Verify actual bit/HWH bytes and the manifest before loading an overlay."""

    bit, hwh, manifest = Path(bit_path), Path(hwh_path), Path(manifest_path)
    for path, label in ((bit, "bitstream"), (hwh, "HWH"), (manifest, "manifest")):
        if not path.is_file():
            raise ContractError(f"{label} file not found: {path}")
    # PYNQ Overlay(bitfile) resolves the metadata sidecar from the bitstream
    # basename.  Validating an unrelated HWH would therefore create a false
    # contract check: the bytes checked here would differ from the HWH PYNQ
    # actually parses after the download.
    if bit.parent.resolve() != hwh.parent.resolve() or bit.stem != hwh.stem:
        raise ContractError(
            "bitstream and HWH must be in the same directory with the same base name"
        )
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read overlay manifest {manifest}: {exc}") from exc
    if not isinstance(record, dict):
        raise ContractError("overlay manifest must contain a JSON object")
    _require_current_r31_revision(record, manifest)
    files = record.get("files", record.get("artifacts", record))
    for path, key in ((bit, "bit"), (hwh, "hwh")):
        entry = files.get(key) if isinstance(files, dict) else None
        if isinstance(entry, dict):
            expected_sha = entry.get("sha256")
            expected_size = entry.get("size")
        else:
            expected_sha = record.get(f"{key}_sha256")
            expected_size = record.get(f"{key}_size")
        if not isinstance(expected_sha, str) or sha256_file(path).lower() != expected_sha.lower():
            raise ContractError(f"{key} SHA-256 does not match the overlay manifest")
        if expected_size is not None and int(expected_size) != path.stat().st_size:
            raise ContractError(f"{key} size does not match the overlay manifest")
    addresses = record.get("addresses", record.get("address_map", {}))
    if expected_addresses:
        for name, expected in expected_addresses.items():
            value = addresses.get(name) if isinstance(addresses, dict) else None
            if isinstance(value, dict):
                value = value.get("base", value.get("address"))
            if _hex(value, f"addresses.{name}") != expected:
                raise ContractError(f"HWH manifest address mismatch for {name}")
    return record


def _manifest_value(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record:
            return record[name]
    source = record.get("source")
    if isinstance(source, dict):
        for name in names:
            if name in source:
                return source[name]
    return None


def _validate_hardware_manifest(
    record: dict[str, Any],
    *,
    manifest_path: Path,
    contract: BoardContract,
    frame_width: int,
    frame_height: int,
    overlay_mode: str,
    require_source_binding: bool,
    contract_path: Path | None = None,
) -> None:
    """Fail closed before loading a bitstream built for another contract."""

    if require_source_binding and _manifest_value(record, "preboard_passed") is not True:
        raise VersionMismatch("overlay manifest preboard_passed must be true before Overlay load")

    device = _manifest_value(record, "device", "part")
    if device is not None and str(device).lower() != TARGET_DEVICE:
        raise VersionMismatch(f"overlay target {device!r} != {TARGET_DEVICE!r}")
    if require_source_binding and device is None:
        raise VersionMismatch("overlay manifest does not declare the target device")

    versions = (
        ("regmap_version", REGMAP_VERSION),
        ("diagnostic_version", DIAGNOSTIC_VERSION),
        ("profile_format_version", PROFILE_FORMAT_VERSION),
    )
    for name, expected in versions:
        value = _manifest_value(record, name)
        if value is None:
            if require_source_binding:
                raise VersionMismatch(f"overlay manifest is missing {name}")
            continue
        try:
            actual = int(value, 0) if isinstance(value, str) else int(value)
        except (TypeError, ValueError) as exc:
            raise VersionMismatch(f"overlay manifest {name} is not an integer") from exc
        if actual != expected and not (name == "diagnostic_version" and actual in SUPPORTED_DIAGNOSTIC_VERSIONS):
            raise VersionMismatch(f"overlay {name}={actual} != contract {expected}")

    width = _manifest_value(record, "frame_width")
    height = _manifest_value(record, "frame_height")
    frame = record.get("frame")
    if isinstance(frame, dict):
        width = frame.get("width", width)
        height = frame.get("height", height)
    for name, actual, expected in (("frame_width", width, frame_width), ("frame_height", height, frame_height)):
        if actual is None:
            if require_source_binding:
                raise VersionMismatch(f"overlay manifest is missing {name}")
            continue
        try:
            actual_int = int(actual, 0) if isinstance(actual, str) else int(actual)
        except (TypeError, ValueError) as exc:
            raise VersionMismatch(f"overlay manifest {name} is not an integer") from exc
        if actual_int != expected:
            raise VersionMismatch(f"overlay {name}={actual_int} != requested {expected}")

    declared_mode = _manifest_value(record, "overlay_mode", "overlay")
    if declared_mode is not None and str(declared_mode) not in {overlay_mode, overlay_mode.replace("structured_light_", "")}:
        raise VersionMismatch(f"overlay mode {declared_mode!r} does not match {overlay_mode!r}")
    if require_source_binding and declared_mode is None:
        raise VersionMismatch("overlay manifest does not declare overlay_mode")

    # The manifest must bind the generated artifacts to the current contract.
    contract_sha = _manifest_value(record, "contract_sha256", "contract_sha")
    if require_source_binding and not isinstance(contract_sha, str):
        raise VersionMismatch("overlay manifest is missing contract_sha256")
    if isinstance(contract_sha, str):
        contract_file_value = _manifest_value(record, "contract_file")
        candidates = [contract_path] if contract_path is not None else []
        if isinstance(contract_file_value, str):
            candidate = Path(contract_file_value)
            candidates.append(candidate if candidate.is_absolute() else manifest_path.parent / candidate)
        candidates.extend((
            manifest_path.parent / "p2_board.json",
            manifest_path.parent / "src/contracts/p2_board.json",
        ))
        for parent in Path(__file__).resolve().parents:
            candidates.append(parent / "src/contracts/p2_board.json")
        contract_file = next((item for item in candidates if item is not None and item.is_file()), None)
        if contract_file is None:
            raise VersionMismatch("manifest contract_sha256 cannot be checked: contract file is absent")
        if sha256_file(contract_file).lower() != contract_sha.lower():
            raise VersionMismatch("overlay contract_sha256 does not match the supplied contract file")

    source_sha = _manifest_value(record, "source_sha256", "source_combined_sha256", "source_hash")
    if require_source_binding and not isinstance(source_sha, (str, dict)):
        raise VersionMismatch("overlay manifest is missing source_sha256/source_combined_sha256")
    if isinstance(source_sha, str) and (len(source_sha) != 64 or any(char not in "0123456789abcdefABCDEF" for char in source_sha)):
        raise VersionMismatch("overlay source hash must be a 64-character SHA-256")
    if isinstance(source_sha, dict):
        values = tuple(source_sha.values())
        if not values or any(not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value) for value in values):
            raise VersionMismatch("overlay source_sha256 map contains an invalid SHA-256")


def validate_runtime_contract(
    control_mmio: Any,
    diagnostic_mmio: Any,
    *,
    overlay_mode: str,
) -> dict[str, int]:
    """Check the live control/diagnostic ABI after an Overlay is loaded.

    Static manifest/HWH checks cannot prove which logic is responding at the
    mapped addresses.  Camera bring-up and DMA capture share this readback so
    both paths reject a stale or mismatched bitstream before touching the
    sensor or allocating a DMA buffer.
    """

    if overlay_mode not in RAW_OVERLAY_MODES | PROFILE_OVERLAY_MODES:
        raise ContractError(f"unsupported overlay mode: {overlay_mode}")
    if not hasattr(control_mmio, "read") or not hasattr(diagnostic_mmio, "read"):
        raise ContractError("runtime contract check requires control and diagnostic MMIO")

    control_version = int(control_mmio.read(REG_VERSION)) & 0xFFFF_FFFF
    diagnostic_version = int(diagnostic_mmio.read(DIAG_OFFSETS["version"])) & 0xFFFF_FFFF
    if control_version != REGMAP_VERSION:
        raise VersionMismatch(
            f"runtime control VERSION=0x{control_version:08X} != contract 0x{REGMAP_VERSION:08X}"
        )
    if diagnostic_version not in SUPPORTED_DIAGNOSTIC_VERSIONS:
        raise VersionMismatch(
            f"runtime diagnostic VERSION=0x{diagnostic_version:08X} != contract 0x{DIAGNOSTIC_VERSION:08X}"
        )

    capability_word = int(diagnostic_mmio.read(DIAG_OFFSETS["capabilities"])) & 0xFFFF_FFFF
    required_names = ["raw", "camera_io", "single_capture"]
    if overlay_mode in PROFILE_OVERLAY_MODES:
        # Raw capture has no centroid/row snapshot hardware (CAPABILITIES=0x19).
        # Both row diagnostics and the Profile stream remain mandatory when
        # validating the paired raw/Profile Overlay (CAPABILITIES=0x1F).
        required_names.extend(["row_diagnostics", "profile"])
    missing = [
        name for name in required_names
        if not capability_word & RUNTIME_CAPABILITY_BITS[name]
    ]
    if missing:
        raise CapabilityError(
            "runtime diagnostic CAPABILITIES is missing: " + ", ".join(missing)
        )
    profile_advertised = bool(capability_word & RUNTIME_CAPABILITY_BITS["profile"])
    if profile_advertised != (overlay_mode in PROFILE_OVERLAY_MODES):
        raise CapabilityError(
            "runtime diagnostic PROFILE capability does not match overlay_mode"
        )
    return {
        "control_version": control_version,
        "diagnostic_version": diagnostic_version,
        "capabilities": capability_word,
    }


def parse_hwh_addresses(path: str | Path) -> dict[str, int]:
    """Extract module/interface base addresses from Vivado HWH layouts.

    Recent Vivado HWH files store address ranges as global ``MEMRANGE``
    elements with an ``INSTANCE`` attribute.  Walking every descendant from
    each ``MODULE`` first makes a parent module appear to own all child
    ranges, and can therefore return misleading zero or parent addresses for
    module-reference IP.  Prefer the explicit MEMRANGE ownership metadata and
    retain the small module-scoped fallback for older/minimal HWH fixtures
    that omit ``INSTANCE``.
    """

    source = Path(path)
    try:
        root = ET.fromstring(source.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ContractError(f"cannot parse HWH {source}: {exc}") from exc
    result: dict[str, int] = {}
    ranges_by_instance: dict[str, list[tuple[str, int]]] = {}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].upper() != "MEMRANGE":
            continue
        base = node.attrib.get("BASEVALUE") or node.attrib.get("basevalue")
        if base is None:
            continue
        try:
            base_value = int(base, 0)
        except ValueError:
            continue
        instance = node.attrib.get("INSTANCE") or node.attrib.get("instance") or ""
        interface = (
            node.attrib.get("SLAVEBUSINTERFACE")
            or node.attrib.get("slavebusinterface")
            or node.attrib.get("NAME")
            or node.attrib.get("name")
            or ""
        )
        if instance:
            ranges_by_instance.setdefault(instance, []).append((interface, base_value))
            if interface:
                result[f"{instance}.{interface}"] = base_value

    for instance, ranges in ranges_by_instance.items():
        if len(ranges) == 1:
            result[instance] = ranges[0][1]

    # Older compact fixtures often put MEMRANGE under MODULE and omit its
    # INSTANCE.  Recover that association without revisiting explicit ranges.
    for module in root.iter():
        if module.tag.rsplit("}", 1)[-1].upper() != "MODULE":
            continue
        instance = module.attrib.get("INSTANCE") or module.attrib.get("instance") or module.attrib.get("NAME")
        if not instance:
            continue
        fallback_ranges = []
        for child in module.iter():
            if child.tag.rsplit("}", 1)[-1].upper() != "MEMRANGE":
                continue
            if child.attrib.get("INSTANCE") or child.attrib.get("instance"):
                continue
            base = child.attrib.get("BASEVALUE") or child.attrib.get("basevalue")
            if base is None:
                continue
            try:
                fallback_ranges.append((
                    child.attrib.get("NAME") or child.attrib.get("name") or "",
                    int(base, 0),
                ))
            except ValueError:
                continue
        if len(fallback_ranges) == 1:
            result[instance] = fallback_ranges[0][1]
        for interface, base in fallback_ranges:
            if interface:
                result[f"{instance}.{interface}"] = base
    return result


def parse_hwh_bases(path: str | Path) -> tuple[int, ...]:
    """Return every MEMRANGE base, including multiple AXI slaves per IP."""

    source = Path(path)
    try:
        root = ET.fromstring(source.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ContractError(f"cannot parse HWH {source}: {exc}") from exc
    bases: list[int] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].upper() != "MEMRANGE":
            continue
        value = node.attrib.get("BASEVALUE") or node.attrib.get("basevalue")
        if value is not None:
            try:
                bases.append(int(value, 0))
            except ValueError:
                pass
    return tuple(bases)


def parse_hwh_parameters(path: str | Path) -> dict[str, str]:
    """Extract Vivado module parameters for frame/mode consistency checks."""

    source = Path(path)
    try:
        root = ET.fromstring(source.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ContractError(f"cannot parse HWH {source}: {exc}") from exc
    result: dict[str, str] = {}
    for module in root.iter():
        if module.tag.rsplit("}", 1)[-1].upper() != "MODULE":
            continue
        instance = module.attrib.get("INSTANCE") or module.attrib.get("instance") or module.attrib.get("NAME") or ""
        for child in module.iter():
            if child.tag.rsplit("}", 1)[-1].upper() != "PARAMETER":
                continue
            name = child.attrib.get("NAME") or child.attrib.get("name")
            value = child.attrib.get("VALUE") or child.attrib.get("value")
            if name and value is not None:
                key = f"{instance}.{name}" if instance else name
                result[key] = value
    return result


def _parameter_int(parameters: dict[str, str], suffix: str) -> int | None:
    candidates = [value for key, value in parameters.items() if key.upper().endswith(suffix.upper())]
    if not candidates:
        return None
    try:
        values = tuple(int(value, 0) for value in candidates)
    except ValueError as exc:
        raise ContractError(f"HWH parameter {suffix} is not an integer: {candidates!r}") from exc
    if len(set(values)) != 1:
        raise VersionMismatch(f"HWH contains contradictory values for {suffix}: {values!r}")
    return values[0]


def _parse_hwh_memory_ranges(path: str | Path) -> tuple[dict[str, Any], ...]:
    """Read address ranges with their bus/interface ownership metadata."""

    source = Path(path)
    try:
        root = ET.fromstring(source.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ContractError(f"cannot parse HWH {source}: {exc}") from exc
    ranges: list[dict[str, Any]] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].upper() != "MEMRANGE":
            continue
        base = node.attrib.get("BASEVALUE") or node.attrib.get("basevalue")
        high = node.attrib.get("HIGHVALUE") or node.attrib.get("highvalue")
        if base is None:
            continue
        try:
            item: dict[str, Any] = {
                "base": int(base, 0),
                "instance": node.attrib.get("INSTANCE") or node.attrib.get("instance") or "",
                "master": node.attrib.get("MASTERBUSINTERFACE") or node.attrib.get("masterbusinterface") or "",
                "slave": node.attrib.get("SLAVEBUSINTERFACE") or node.attrib.get("slavebusinterface") or "",
            }
            if high is not None:
                item["high"] = int(high, 0)
        except ValueError as exc:
            raise ContractError(f"HWH memory range is not an integer: {node.attrib}") from exc
        ranges.append(item)
    return tuple(ranges)


def _buffer_capacity_bytes(buffer: Any) -> int:
    """Return a byte capacity for a PYNQ allocation, failing closed.

    ``pynq.allocate`` returns an ndarray-like object with ``nbytes``.  Tests
    and board-side adapters sometimes expose a ``capacity`` property instead.
    A DMA length must never be accepted merely because a buffer happens to be
    sliceable: without a byte capacity we cannot prove that the S2MM write is
    contained in the allocation.
    """

    for name in ("nbytes", "capacity"):
        value = getattr(buffer, name, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            if value < 0:
                raise DMAError(f"DMA buffer {name} must be nonnegative")
            return value
        if value is not None:
            raise DMAError(f"DMA buffer {name} must be an integer byte capacity")
    # A small fake/adapter may only implement ``__len__``.  Treat that value
    # as bytes explicitly; a real PYNQ ndarray is handled by nbytes above.
    try:
        value = len(buffer)
    except (TypeError, AttributeError) as exc:
        raise DMAError("DMA buffer does not expose a byte capacity (nbytes/capacity/len)") from exc
    if type(value) is not int or value < 0:
        raise DMAError("DMA buffer len() is not a nonnegative integer byte capacity")
    return value


def _release_unowned_buffer(buffer: Any, name: str) -> None:
    """Release an allocation which was never handed to the DMA channel.

    ``BoundedS2MM.arm`` performs capacity/address/cache validation before it
    publishes ``transfer_state``.  In that window no DMA can own the buffer,
    so the transport must free it directly instead of dropping the reference
    after a no-op ``cleanup_buffer`` call.  Invalidate failures are retained,
    but do not prevent the final free attempt.
    """

    errors: list[Exception] = []
    invalidate = getattr(buffer, "invalidate", None)
    if callable(invalidate):
        try:
            invalidate()
        except Exception as exc:
            errors.append(exc)
    released = False
    for method_name in ("freebuffer", "free"):
        method = getattr(buffer, method_name, None)
        if callable(method):
            try:
                method()
            except Exception as exc:
                errors.append(exc)
            else:
                released = True
            break
    if not released:
        errors.append(RuntimeError("buffer has no freebuffer/free method"))
    if errors:
        raise DMAError(
            f"unowned DMA buffer cleanup failed for {name}: "
            + "; ".join(str(item) for item in errors)
        ) from errors[0]


@dataclass(slots=True)
class DmaTransfer:
    buffer: Any
    requested_length: int
    destination_address: int
    actual_length: int = 0
    armed: bool = False
    completed: bool = False


class BoundedS2MM:
    """Direct-MMIO simple-mode S2MM driver with bounded control paths.

    The default PYNQ DMA object is deliberately not used: accessing its
    ``transfer/start/wait/stop`` methods would make recovery depend on an
    unbounded driver loop.  AXI DMA S2MM offsets are relative to ``0x30``:
    DMACR ``+0x00``, DMASR ``+0x04``, destination address ``+0x18`` and
    length ``+0x28``.
    """

    def __init__(self, dma_mmio: Any, *, name: str = "s2mm", channel_offset: int = 0x30) -> None:
        if not hasattr(dma_mmio, "read") or not hasattr(dma_mmio, "write"):
            raise DMAError(f"{name} requires a direct MMIO object")
        self.mmio = dma_mmio
        self.name = name
        self.channel_offset = channel_offset
        self.control_offset = channel_offset
        self.status_offset = channel_offset + 0x04
        self.destination_low_offset = channel_offset + 0x18
        self.destination_high_offset = channel_offset + 0x1C
        self.length_offset = channel_offset + 0x28
        self.transfer_state: DmaTransfer | None = None
        # A buffer may be released only after a completed transfer or after a
        # channel reset has been observed.  Keeping this state separate from
        # ``transfer_state`` prevents a failed recovery from silently freeing
        # memory which the DMA could still be writing.
        self._reset_confirmed = False
        self._safe_to_release = False
        # Keep a structured receipt for the bounded stop operation.  Board
        # runners persist this alongside the PL/DMA snapshot when a recovery
        # is injected; it is deliberately independent of transfer_state so a
        # failed stop cannot be mistaken for a released buffer.
        self.last_stop_evidence: dict[str, Any] = {}

    def _rd(self, offset: int) -> int:
        return int(self.mmio.read(offset)) & 0xFFFF_FFFF

    def _wr(self, offset: int, value: int) -> None:
        self.mmio.write(offset, int(value) & 0xFFFF_FFFF)

    def arm(self, buffer: Any, length: int) -> None:
        if type(length) is not int or not 0 < length < (1 << 20):
            raise DMAError("DMA transfer length must be in 1..2^20-1")
        if self.transfer_state is not None:
            raise DMAError(f"{self.name} still owns an unreleased buffer")
        capacity = _buffer_capacity_bytes(buffer)
        if capacity < length:
            raise DMAError(
                f"{self.name} buffer capacity {capacity} bytes is smaller than requested {length} bytes"
            )
        destination = getattr(buffer, "device_address", None)
        if destination is None:
            destination = getattr(buffer, "physical_address", None)
        if type(destination) is not int or not 0 <= destination <= 0xFFFF_FFFF_FFFF_FFFF:
            raise DMAError(f"{self.name} buffer has no uint64 device_address")
        if destination & 0x7:
            raise DMAError(f"{self.name} destination address 0x{destination:X} is not 8-byte aligned")
        invalidate = getattr(buffer, "invalidate", None)
        if callable(invalidate):
            invalidate()
        # Publish ownership before the MMIO sequence.  If a register access
        # fails, the caller can still perform a bounded recovery and release
        # the allocation rather than losing the only reference to it.
        self.transfer_state = DmaTransfer(buffer=buffer, requested_length=length, destination_address=destination)
        self._reset_confirmed = False
        self._safe_to_release = False
        try:
            # AXI DMA simple-mode startup: reset, wait for reset to clear and
            # the channel to be halted, clear latched IOC/error bits, then
            # raise RS and wait for the channel to leave Halted.  Only after
            # that sequence are address and length registers written.
            self._wr(self.control_offset, 0x4)
            reset_deadline = time.monotonic() + 0.5
            while time.monotonic() < reset_deadline:
                control = self._rd(self.control_offset)
                status = self._rd(self.status_offset)
                if not (control & 0x4) and (status & 0x1):
                    break
                time.sleep(0.001)
            else:
                raise CaptureTimeout(f"{self.name} reset did not complete before arm")
            # Until RS is asserted the channel is known halted and its
            # allocation is safe to release if the subsequent startup write
            # fails.  The running-transfer path below clears this guard.
            self._reset_confirmed = True
            self._safe_to_release = True
            status = self._rd(self.status_offset)
            clear_mask = status & (0x70 | (1 << 12))
            if clear_mask:
                self._wr(self.status_offset, clear_mask)
            self._reset_confirmed = False
            self._safe_to_release = False
            self._wr(self.control_offset, 0x1)  # RS=1
            run_deadline = time.monotonic() + 0.5
            while time.monotonic() < run_deadline:
                if not (self._rd(self.status_offset) & 0x1):
                    break
                time.sleep(0.001)
            else:
                raise CaptureTimeout(f"{self.name} did not leave Halted before arm")
            self._wr(self.destination_low_offset, destination & 0xFFFF_FFFF)
            self._wr(self.destination_high_offset, (destination >> 32) & 0xFFFF_FFFF)
            # The length write starts the simple-mode transfer and is the last
            # register write before the camera stream is released.
            self._wr(self.length_offset, length)
        except Exception as exc:
            raise DMAError(f"{self.name} MMIO arm failed: {exc}") from exc
        self.transfer_state.armed = True

    def poll(self) -> bool:
        if self.transfer_state is None:
            return False
        if self.transfer_state.completed:
            return True
        if not self.transfer_state.armed:
            return False
        status = self._rd(self.status_offset)
        # DMA error bits: internal/slave/decode. IOC (bit 12) or idle (bit 1)
        # marks completion in simple mode.  The length register is the actual
        # S2MM completion length exposed by the P2 contract.
        if status & 0x70:
            raise DMAError(f"{self.name} reported DMA error status 0x{status:08X}")
        if not (status & (1 << 12) or status & (1 << 1)):
            return False
        actual = self._rd(self.length_offset)
        # The length register is the only accepted completion length.  Do not
        # substitute the programmed capacity: doing so can silently publish a
        # short frame with stale bytes at the end of the buffer.
        if actual == 0:
            raise DMAError(f"{self.name} completed with zero S2MM_LENGTH")
        if actual < 0 or actual > self.transfer_state.requested_length:
            raise DMAError(f"{self.name} reported invalid S2MM_LENGTH {actual}")
        self.transfer_state.actual_length = actual
        # IOC/idle is the hardware completion boundary.  The buffer is now
        # safe to release even if the later PL diagnostic publication fails;
        # stop() still resets the channel before the normal cleanup path.
        self.transfer_state.armed = False
        self.transfer_state.completed = True
        self._safe_to_release = True
        return True

    def wait(self, timeout_s: float) -> int:
        if type(timeout_s) not in (int, float) or timeout_s <= 0:
            raise DMAError("DMA timeout must be positive")
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            if self.poll():
                assert self.transfer_state is not None
                return self.transfer_state.actual_length
            time.sleep(min(0.001, max(0.0001, deadline - time.monotonic())))
        raise CaptureTimeout(f"{self.name} did not complete within {timeout_s:.3f}s")

    def stop(self, timeout_s: float = 0.5) -> None:
        if type(timeout_s) not in (int, float) or timeout_s <= 0:
            raise DMAError("DMA stop timeout must be positive")
        # ``timeout_s`` is a per-phase budget.  A channel may take the whole
        # halt budget and still become halted as a consequence of the soft
        # reset; that is a confirmed recovery and is safe to release.
        phase_timeout = float(timeout_s)
        halt_deadline = time.monotonic() + phase_timeout
        self._reset_confirmed = False
        self._wr(self.control_offset, 0x0)
        # Halt bit is status bit 0.  If the stream is already broken, do not
        # wait forever: issue the bounded soft reset and poll its completion.
        halted = False
        halt_status = 0
        while time.monotonic() < halt_deadline:
            halt_status = self._rd(self.status_offset)
            if halt_status & 0x1:
                halted = True
                break
            time.sleep(0.001)
        halt_timed_out = not halted
        self._wr(self.control_offset, 0x4)
        reset_complete = False
        reset_deadline = time.monotonic() + phase_timeout
        reset_status = 0
        reset_control = 0
        while time.monotonic() < reset_deadline:
            # DMACR reset is self-clearing.  Also require the channel to be
            # halted so a reset that never reaches the data path is rejected.
            reset_control = self._rd(self.control_offset)
            reset_status = self._rd(self.status_offset)
            if not (reset_control & 0x4) and (reset_status & 0x1):
                reset_complete = True
                break
            time.sleep(0.001)
        self.last_stop_evidence = {
            "halt_observed": halted,
            "halt_phase_timed_out": halt_timed_out,
            "halt_status": halt_status,
            "reset_observed": reset_complete,
            "reset_control": reset_control,
            "reset_status": reset_status,
            "phase_timeout_s": phase_timeout,
            # A reset that self-cleared with HALTED set is sufficient even
            # when the first RS=0 phase did not observe HALTED in time.
            "safe_to_release": bool(reset_complete),
        }
        if not reset_complete:
            raise CaptureTimeout(f"{self.name} reset did not complete within {timeout_s:.3f}s")
        if self.transfer_state is not None:
            self.transfer_state.armed = False
            self.transfer_state.completed = False
        self._reset_confirmed = True
        self._safe_to_release = True

    @property
    def reset_confirmed(self) -> bool:
        """Whether the last stop/reset observed a halted DMA channel."""

        return self._reset_confirmed

    def cleanup_buffer(self, *, retain: bool = False) -> None:
        if self.transfer_state is None:
            return
        if not self._safe_to_release:
            raise DMAError(f"{self.name} buffer cannot be released before DMA halt/reset")
        buffer = self.transfer_state.buffer
        methods = ("invalidate",) if retain else ("invalidate", "freebuffer", "free")
        for method_name in methods:
            method = getattr(buffer, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception as exc:
                    raise DMAError(f"buffer {method_name} failed for {self.name}: {exc}") from exc
                if method_name != "invalidate":
                    break
        if retain:
            if getattr(self, "idle_buffer", None) is not None:
                raise DMAError(f"{self.name} already retains an idle allocation")
            self.idle_buffer = buffer
        self.transfer_state = None
        self._reset_confirmed = False
        self._safe_to_release = False

    def release_idle_buffer(self) -> None:
        """Retired buffers were detached only after IOC or confirmed reset."""
        buffer = getattr(self, "idle_buffer", None)
        if buffer is not None:
            if self.transfer_state is not None:
                raise DMAError(f"{self.name} still has an active transfer")
            _release_unowned_buffer(buffer, self.name)
            self.idle_buffer = None


class _PynqRegisterMMIO:
    """Offset adapter for a PYNQ MMIO object."""

    def __init__(self, mmio: Any, *, base: int, frame_width: int, frame_height: int, contract: BoardContract) -> None:
        self._raw = mmio
        self.contract = contract
        self.frame_width, self.frame_height = frame_width, frame_height
        self.epoch = 0
        self._request_id = None
        self._busy = False

    def read(self, offset: int) -> int:
        value = int(self._raw.read(offset))
        if offset == REG_STATUS:
            self._busy = bool(value & 1)
        if offset == REG_PROFILE_ID:
            self._request_id = value & 0xFFFF_FFFF
        if offset == REG_TIMESTAMP_LOW:
            self._timestamp_low = value
        return value & 0xFFFF_FFFF

    def write(self, offset: int, value: int, wstrb: int = 0xF, **_: Any) -> None:
        if wstrb != 0xF:
            # PYNQ MMIO writes a complete word; P2 hardware tests can use a
            # board-side WSTRB adapter later, but never silently drop lanes.
            raise ContractError("PYNQ MMIO adapter currently accepts full-word writes only")
        value = int(value) & 0xFFFF_FFFF
        self._raw.write(offset, value)
        if offset == REG_CONTROL:
            if value & 0x1:
                self._busy = True
            elif value & 0x2:
                self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def request_id(self) -> int | None:
        return self._request_id


class PynqTransport(CaptureTransport):
    """Real PYNQ Overlay transport, loaded only after artifact verification."""

    def __init__(
        self,
        bit_path: str | Path,
        hwh_path: str | Path,
        manifest_path: str | Path,
        *,
        frame_width: int,
        frame_height: int,
        expected_addresses: dict[str, int] | None = None,
        overlay: Any | None = None,
        contract: BoardContract | None = None,
        overlay_mode: str = "structured_light_profile",
        contract_path: str | Path | None = None,
        strict_manifest: bool = True,
        transition_timeout_s: float = 0.5,
    ) -> None:
        if overlay_mode not in RAW_OVERLAY_MODES | PROFILE_OVERLAY_MODES:
            raise ContractError("overlay_mode must be camera_raw or structured_light_profile")
        if type(frame_width) is not int or type(frame_height) is not int or (frame_width, frame_height) not in {(640, 480), (752, 480)}:
            raise ContractError("PYNQ hardware frame mode must be 640x480 or 752x480")
        if type(transition_timeout_s) not in (int, float) or transition_timeout_s <= 0:
            raise ContractError("transition_timeout_s must be positive")
        self.contract = contract or load_board_contract()
        expected = expected_addresses or {
            "pipeline_control": self.contract.control_base,
            "camera_diagnostics": self.contract.diagnostics_base,
            "raw_dma": self.contract.raw_dma_base,
            **({"profile_dma": self.contract.profile_dma_base}
               if overlay_mode in PROFILE_OVERLAY_MODES else {}),
            **({"hdmi_vdma": 0x40420000, "hdmi_vtc": 0x43c20000, "hdmi_dynclk": 0x43c30000}
               if overlay_mode == "structured_light_profile_hdmi" else {}),
        }
        manifest_record = verify_overlay_artifacts(bit_path, hwh_path, manifest_path, expected_addresses=expected)
        _validate_hardware_manifest(
            manifest_record,
            manifest_path=Path(manifest_path), contract=self.contract,
            frame_width=frame_width, frame_height=frame_height,
            overlay_mode=overlay_mode, require_source_binding=strict_manifest,
            contract_path=Path(contract_path) if contract_path is not None else None,
        )
        hwh_bases = parse_hwh_bases(hwh_path)
        if not hwh_bases:
            raise ContractError("HWH contains no AXI memory ranges")
        hwh_ranges = _parse_hwh_memory_ranges(hwh_path)
        if strict_manifest:
            bindings = {
                "pipeline_control": (("processing_pipeline", "S_AXI_CONTROL"),),
                "camera_diagnostics": (("processing_pipeline", "S_AXI_DIAG"),),
                "raw_dma": (("raw_dma", "S_AXI_LITE"),),
                **({"profile_dma": (("profile_dma", "S_AXI_LITE"),)}
                   if overlay_mode in PROFILE_OVERLAY_MODES else {}),
                **({name: HDMI_HWH_BINDINGS[name] for name in HDMI_HWH_BINDINGS}
                   if overlay_mode == "structured_light_profile_hdmi" else {}),
            }
            missing_bindings = []
            for name, address in expected.items():
                candidates = bindings[name]
                matches = [item for item in hwh_ranges
                           if any(
                               _hwh_instance_matches(item["instance"], instance)
                               and str(item["slave"]).upper() == slave.upper()
                               for instance, slave in candidates
                           )
                           and str(item["master"]).upper() == "M_AXI_GP0"
                           and item["base"] == address]
                if not matches:
                    missing_bindings.append(name)
            if missing_bindings:
                raise ContractError(
                    "HWH PS M_AXI_GP0 address/interface mismatch for: "
                    + ", ".join(missing_bindings)
                )
        else:
            missing_hwh = [name for name, address in expected.items() if address not in hwh_bases]
            if missing_hwh:
                raise ContractError(f"HWH address mismatch for: {', '.join(missing_hwh)}")
        hwh_parameters = parse_hwh_parameters(hwh_path)
        if strict_manifest:
            # The generated HWH is the hardware truth for dimensions and the
            # raw/profile build mode.  A manifest alone must not make a
            # contradictory or incomplete HWH loadable.  The current P2
            # camera_raw handoff contains all three parameters; keep this
            # requirement for the future profile handoff as well.
            missing_parameters = [
                name for name in ("FRAME_WIDTH", "FRAME_HEIGHT", "PROFILE_ENABLE")
                if _parameter_int(hwh_parameters, f".{name}") is None
            ]
            if missing_parameters:
                raise VersionMismatch(
                    "HWH is missing required P2 mode parameters: " + ", ".join(missing_parameters)
                )
            dma_parameters = {
                ".C_INCLUDE_SG": 0,
                ".C_INCLUDE_MM2S": 0,
                ".C_INCLUDE_MM2S_DRE": 0,
                ".C_INCLUDE_S2MM": 1,
                ".C_INCLUDE_S2MM_DRE": 0,
                ".C_M_AXI_S2MM_ADDR_WIDTH": 32,
                ".C_M_AXI_S2MM_DATA_WIDTH": 64,
                ".C_S_AXIS_S2MM_TDATA_WIDTH": 32,
                ".C_SG_LENGTH_WIDTH": 20,
            }
            dma_instances = ("raw_dma", "profile_dma") if overlay_mode in PROFILE_OVERLAY_MODES else ("raw_dma",)
            for dma_instance in dma_instances:
                for suffix, expected_value in dma_parameters.items():
                    scoped = {key: value for key, value in hwh_parameters.items()
                              if key.split(".", 1)[0] == dma_instance}
                    declared = _parameter_int(scoped, suffix)
                    if declared is None:
                        raise VersionMismatch(
                            f"HWH {dma_instance} is missing required DMA parameter {suffix[1:]}"
                        )
                    if declared != expected_value:
                        raise VersionMismatch(
                            f"HWH {dma_instance} {suffix[1:]}={declared} != contract {expected_value}"
                        )
        for suffix, actual, label in (
            (".FRAME_WIDTH", frame_width, "FRAME_WIDTH"),
            (".FRAME_HEIGHT", frame_height, "FRAME_HEIGHT"),
        ):
            declared = _parameter_int(hwh_parameters, suffix)
            if declared is not None and declared != actual:
                raise VersionMismatch(f"HWH {label}={declared} != requested {actual}")
        profile_enable = _parameter_int(hwh_parameters, ".PROFILE_ENABLE")
        if profile_enable is not None and bool(profile_enable) != (overlay_mode in PROFILE_OVERLAY_MODES):
            raise VersionMismatch("HWH PROFILE_ENABLE does not match overlay_mode")
        self.bit_path, self.hwh_path = Path(bit_path), Path(hwh_path)
        self.overlay_mode = overlay_mode
        if overlay is None:
            try:
                from pynq import Overlay  # type: ignore
            except ImportError as exc:
                raise CapabilityError("PYNQ is not installed; use Mock/Replay on the PC") from exc
            self.overlay = Overlay(str(self.bit_path), download=True)
        else:
            self.overlay = overlay
        self.frame_width, self.frame_height = frame_width, frame_height
        self.transition_timeout_s = float(transition_timeout_s)
        self.capabilities = Capabilities(
            raw_dma=True, profile_dma=overlay_mode in PROFILE_OVERLAY_MODES,
            camera_sccb=True, camera_diagnostics=True,
            motion=False, hardware=True, overlay_name=overlay_mode, source="pynq",
        )
        try:
            from pynq import MMIO  # type: ignore
        except ImportError:
            MMIO = None

        def direct_mmio(name: str, base: int) -> Any:
            # Tests may provide an explicit map.  On hardware use PYNQ's raw
            # MMIO constructor; this avoids constructing the default DMA
            # driver, whose transfer/start/stop paths are unbounded.
            supplied = getattr(self.overlay, "_p2_mmio", None)
            if isinstance(supplied, dict):
                value = supplied.get(name, supplied.get(base))
                if value is not None:
                    return value
            if MMIO is None:
                raise CapabilityError("PYNQ MMIO is unavailable")
            return MMIO(base, self.contract.window)

        self._control_raw_mmio = direct_mmio("pipeline_control", self.contract.control_base)
        self._diag_mmio = direct_mmio("camera_diagnostics", self.contract.diagnostics_base)
        raw_dma_mmio = direct_mmio("raw_dma", self.contract.raw_dma_base)
        self.mmio = _PynqRegisterMMIO(
            self._control_raw_mmio, base=self.contract.control_base,
            frame_width=frame_width, frame_height=frame_height, contract=self.contract,
        )
        self.raw_dma = BoundedS2MM(raw_dma_mmio, name="raw_dma")
        if overlay_mode in PROFILE_OVERLAY_MODES:
            profile_dma_mmio = direct_mmio("profile_dma", self.contract.profile_dma_base)
            self.profile_dma: BoundedS2MM | None = BoundedS2MM(profile_dma_mmio, name="profile_dma")
        else:
            self.profile_dma = None
        self.runtime_contract = validate_runtime_contract(
            self.mmio, self._diag_mmio, overlay_mode=overlay_mode,
        )
        self._raw_buffer = None
        self._profile_buffer = None
        self._want_raw = False
        self._want_profile = False
        self._accepted_request_id: int | None = None
        self._accepted_epoch: int | None = None
        # ``_accepted_*`` describes the one request currently owned by the
        # transport.  The last accepted identity is generation state and must
        # survive receive/STOP cleanup; otherwise a skipped or duplicated
        # request ID could be paired with a fresh DMA buffer.
        self._last_accepted_id: int | None = None
        self._last_accepted_epoch: int | None = None
        # A cancelled request advances ``next_epoch`` in the PL, but the
        # exported epoch changes only when the next START is accepted.  This
        # one-shot token is granted only by a verified local recovery.
        self._recovery_token: dict[str, int | str] | None = None
        self._recovery_required = False
        # Last recovery snapshot is intentionally kept in memory so a board
        # runner can persist the exact failure without guessing which DMA or
        # PL state caused it.
        self.last_recovery: dict[str, Any] = {}

    @property
    def recovery_required(self) -> bool:
        """Whether a failed/ambiguous recovery blocks the next START."""

        return bool(getattr(self, "_recovery_required", False))

    @property
    def last_accepted_id(self) -> int | None:
        """Most recent accepted request ID, retained across cleanup."""

        return getattr(self, "_last_accepted_id", None)

    @property
    def last_accepted_epoch(self) -> int | None:
        """Epoch belonging to :attr:`last_accepted_id`."""

        return getattr(self, "_last_accepted_epoch", None)

    def resume_generation(self, request_id: int, epoch: int) -> None:
        """R41 runtime07: continue the request/epoch generation of an Overlay that stayed
        loaded across processes. Only before this transport's first START, on an idle core."""
        if self._accepted_request_id is not None or getattr(self, "_last_accepted_id", None) is not None:
            raise ProtocolError("resume_generation only before the first START of this transport")
        if int(self.mmio.read(REG_STATUS)) & 1:
            raise BusyError("cannot resume a generation while the pipeline is busy")
        self._last_accepted_id = int(request_id) & 0xFFFF_FFFF
        self._last_accepted_epoch = int(epoch) & 0xFFFF
        self._resume_epoch_step_once = True

    @staticmethod
    def _next_request_id(value: int) -> int:
        return (int(value) + 1) & 0xFFFF_FFFF

    @staticmethod
    def _next_epoch(value: int) -> int:
        return (int(value) + 1) & 0xFFFF

    def _expected_start_identity(self, before_id: int, before_epoch: int) -> tuple[int, int, dict[str, int | str] | None]:
        """Return the only identity accepted for the next START.

        The PL keeps the current epoch visible after cancellation and exposes
        the incremented ``next_epoch`` only on the following accepted START.
        A recovery token therefore changes the expected epoch exactly once;
        it is never inferred from a board read alone.
        """

        last_id = getattr(self, "_last_accepted_id", None)
        last_epoch = getattr(self, "_last_accepted_epoch", None)
        token = getattr(self, "_recovery_token", None)
        if token is not None:
            if last_id is None or last_epoch is None:
                raise ProtocolError("recovery epoch token has no accepted generation")
            if int(token.get("base_request_id", -1)) != last_id:
                raise ProtocolError("recovery token request ID no longer matches the accepted generation")
            if int(token.get("base_epoch", -1)) != last_epoch:
                raise ProtocolError("recovery token epoch no longer matches the accepted generation")
            if before_id != last_id or before_epoch != last_epoch:
                raise ProtocolError("board identity changed before the one-shot recovery START")
            return (
                self._next_request_id(last_id),
                self._next_epoch(last_epoch),
                token,
            )

        expected_id = 0 if last_id is None else self._next_request_id(last_id)
        expected_epoch = before_epoch if last_epoch is None else last_epoch
        if last_epoch is not None and before_epoch != last_epoch:
            raise ProtocolError("pipeline epoch changed outside a verified recovery")
        if last_id is not None and before_id != last_id:
            raise ProtocolError("pipeline request ID changed outside a verified START")
        return expected_id, expected_epoch, None

    def _read_cancel_evidence(self) -> dict[str, Any]:
        """Read the post-abort diagnostic reason without inventing one."""

        diag_error = int(self._diag_mmio.read(DIAG_OFFSETS["error"])) & 0xFFFF_FFFF
        stop_bit = bool(diag_error & (1 << 6))
        timeout_bit = bool(diag_error & (1 << 0))
        if timeout_bit:
            reason = "timeout"
        elif stop_bit:
            reason = "stop"
        elif diag_error:
            reason = "fault"
        else:
            reason = None
        return {
            "diag_error": diag_error,
            "stop_bit": stop_bit,
            "timeout_bit": timeout_bit,
            "reason": reason,
            "valid": bool(diag_error),
        }

    def stream_requirements(self, *, raw: bool, profile: bool) -> tuple[bool, bool]:
        if self.overlay_mode in PROFILE_OVERLAY_MODES:
            # The full pipeline always drives both TLASTs.  Arming only one
            # channel would either deadlock the pipeline or overflow its
            # unbackpressured raw/profile FIFO.
            return True, True
        return raw, False

    def _allocate(self, size: int) -> Any:
        try:
            from pynq import allocate  # type: ignore
            import numpy as np  # type: ignore
        except ImportError as exc:
            raise CapabilityError("PYNQ and NumPy are required on the board") from exc
        return allocate(shape=(size,), dtype=np.uint8)

    def enable_raw_buffer_reuse(self) -> None:
        """Opt-in one-frame CMA pool; call before starting the capture worker."""
        if self.raw_dma.transfer_state is not None or self._raw_buffer is not None:
            raise BusyError("enable capture buffer reuse only while idle")
        self._reuse_raw_buffer = True
        self.raw_buffer_pool_stats = dict(allocations=0, reuses=0, capacity_bytes=0)

    def arm_raw(self, size: int) -> None:
        cached = getattr(self.raw_dma, "idle_buffer", None)
        if cached is not None:
            if _buffer_capacity_bytes(cached) != size:
                raise DMAError("persistent raw buffer size changed")
            self.raw_dma.idle_buffer = None
        self._raw_buffer = cached if cached is not None else self._allocate(size)
        stats = getattr(self, "raw_buffer_pool_stats", None)
        if stats is not None:
            stats["allocations" if cached is None else "reuses"] += 1
            stats["capacity_bytes"] = _buffer_capacity_bytes(self._raw_buffer)
        try:
            self.raw_dma.arm(self._raw_buffer, size)
        except Exception as exc:
            # Validation/cache failures happen before BoundedS2MM publishes
            # transfer_state.  No DMA owns the allocation in that case, so
            # release it directly; never hide it behind a no-op cleanup.
            if self.raw_dma.transfer_state is None:
                try:
                    _release_unowned_buffer(self._raw_buffer, "raw_dma")
                except Exception as cleanup_exc:
                    raise DMAError(
                        f"raw DMA arm failed and unowned buffer cleanup failed: {cleanup_exc}"
                    ) from exc
                self._raw_buffer = None
                raise
            try:
                self.raw_dma.stop(timeout_s=self.transition_timeout_s)
                self.raw_dma.cleanup_buffer()
            except Exception as recovery_exc:
                raise DMAError(f"raw DMA arm failed and buffer recovery failed: {recovery_exc}") from exc
            self._raw_buffer = None
            raise

    def arm_profile(self, size: int) -> None:
        if self.profile_dma is None:
            raise CapabilityError("camera_raw overlay has no Profile S2MM channel")
        self._profile_buffer = self._allocate(size)
        try:
            self.profile_dma.arm(self._profile_buffer, size)
        except Exception as exc:
            if self.profile_dma.transfer_state is None:
                try:
                    _release_unowned_buffer(self._profile_buffer, "profile_dma")
                except Exception as cleanup_exc:
                    raise DMAError(
                        f"profile DMA arm failed and unowned buffer cleanup failed: {cleanup_exc}"
                    ) from exc
                self._profile_buffer = None
                raise
            try:
                self.profile_dma.stop(timeout_s=self.transition_timeout_s)
                self.profile_dma.cleanup_buffer()
            except Exception as recovery_exc:
                raise DMAError(f"profile DMA arm failed and buffer recovery failed: {recovery_exc}") from exc
            self._profile_buffer = None
            raise

    def start_capture(self, *, raw: bool, profile: bool) -> int:
        if not raw and not profile:
            raise DMAError("at least one S2MM stream must be armed before START")
        if getattr(self, "_recovery_required", False):
            raise DMAError("PYNQ transport requires a confirmed bounded recovery before START")
        if (raw and self._raw_buffer is None) or (profile and self._profile_buffer is None):
            raise DMAError("both requested DMA buffers must be armed before START")
        before_id = int(self.mmio.read(REG_PROFILE_ID))
        before_epoch = int(self._diag_mmio.read(DIAG_OFFSETS["epoch"])) & 0xFFFF
        try:
            expected_id, expected_epoch, recovery_token = self._expected_start_identity(
                before_id, before_epoch,
            )
        except ProtocolError:
            # A board-side generation jump before START is itself an
            # unreconciled recovery condition.  Do not let a caller retry
            # indefinitely against an unknown DMA/PL generation.
            self._recovery_required = True
            raise
        if self.mmio.read(REG_STATUS) & 1:
            raise BusyError("PYNQ pipeline is still busy")
        self._want_raw, self._want_profile = raw, profile
        self.mmio.write(REG_CONTROL, 1)
        deadline = time.monotonic() + self.transition_timeout_s
        accepted_id: int | None = None
        accepted_epoch: int | None = None
        while time.monotonic() < deadline:
            status = int(self.mmio.read(REG_STATUS))
            current_id = int(self.mmio.read(REG_PROFILE_ID))
            diag_id = int(self._diag_mmio.read(DIAG_OFFSETS["request_id"])) & 0xFFFF_FFFF
            diag_epoch = int(self._diag_mmio.read(DIAG_OFFSETS["epoch"])) & 0xFFFF
            changed = current_id != before_id or diag_id != before_id
            # The first request may legitimately use ID zero.  Thereafter an
            # unchanged ID is rejected, preventing a stale packet from being
            # associated with a new START.
            first_request_busy = self._accepted_request_id is None and bool(status & 1)
            id_consistent = diag_id == current_id or (current_id == 0 and diag_id == 0)
            if (bool(status & 1) and changed and id_consistent) or (first_request_busy and id_consistent):
                accepted_id = current_id
                accepted_epoch = diag_epoch
                break
            time.sleep(0.001)
        if accepted_id is None:
            self._want_raw = self._want_profile = False
            raise CaptureTimeout("pipeline did not acknowledge START within the bounded deadline")
        if accepted_id != expected_id:
            # The PL has accepted a request, but it is not the next request in
            # this transport generation.  Keep the DMA ownership fail-closed;
            # a caller must explicitly run the bounded recovery path before a
            # later START can be attempted.
            self._recovery_required = True
            self._want_raw = self._want_profile = False
            raise ProtocolError(
                f"pipeline request ID {accepted_id} is not the expected next ID {expected_id}"
            )
        # R41 runtime07: after resume_generation() the first START may carry the PL's pending
        # next_epoch (a request cancelled by the previous process); exactly one step is allowed.
        allowed_epochs = {expected_epoch}
        if getattr(self, "_resume_epoch_step_once", False):
            allowed_epochs.add(self._next_epoch(expected_epoch))
            self._resume_epoch_step_once = False
        if accepted_epoch not in allowed_epochs:
            self._recovery_required = True
            self._want_raw = self._want_profile = False
            raise ProtocolError(
                f"pipeline epoch {accepted_epoch} is not the expected epoch {expected_epoch}"
            )
        self._accepted_request_id = accepted_id
        self._accepted_epoch = accepted_epoch
        self._last_accepted_id = accepted_id
        self._last_accepted_epoch = accepted_epoch
        # Consume the recovery token only after the PL accepted exactly the
        # identity it was issued for.  A timeout before acceptance leaves the
        # token available for one bounded retry; an accepted but mismatched
        # identity is fail-closed above.
        if recovery_token is not None:
            self._recovery_token = None
        self._recovery_required = False
        self.mmio.epoch = accepted_epoch
        self.mmio._request_id = accepted_id
        return accepted_id

    def poll(self) -> bool:
        return (not self._want_raw or self.raw_dma.poll()) and (not self._want_profile or self.profile_dma.poll())

    def _profile_receive_limit(self) -> int:
        """Subclasses may admit a different, explicitly negotiated wire format."""
        return MAX_SLP2_PACKET_BYTES

    def _diagnostics_for_payload(self, profile: bytes | None,
                                 diagnostics: DiagnosticsSnapshot) -> DiagnosticsSnapshot:
        """Validate subclass payloads inside the existing DMA recovery boundary."""
        return diagnostics

    def receive(self) -> TransportCapture:
        try:
            if not self.poll():
                raise DMAError("PYNQ DMA transfer is incomplete")
            raw_len = self.raw_dma.transfer_state.actual_length if self._want_raw and self.raw_dma.transfer_state else 0
            profile_len = self.profile_dma.transfer_state.actual_length if self._want_profile and self.profile_dma.transfer_state else 0
            expected_raw = self.frame_width * self.frame_height
            if self._want_raw and raw_len != expected_raw:
                raise ProtocolError(f"raw S2MM actual length {raw_len} != expected frame length {expected_raw}")
            if self._want_profile and not 0 < profile_len <= self._profile_receive_limit():
                raise ProtocolError(f"Profile S2MM actual length {profile_len} is outside the SLP2 limit")
            # S2MM writes DDR behind the CPU cache.  Invalidate before touching
            # the bytes, then invalidate again in cleanup before releasing the
            # allocation so both the read and lifecycle paths are explicit.
            for buffer in (self._raw_buffer if self._want_raw else None, self._profile_buffer if self._want_profile else None):
                invalidate = getattr(buffer, "invalidate", None)
                if callable(invalidate):
                    invalidate()
            raw = bytes(memoryview(self._raw_buffer)[:raw_len]) if self._want_raw and self._raw_buffer is not None else None
            profile = bytes(memoryview(self._profile_buffer)[:profile_len]) if self._want_profile and self._profile_buffer is not None else None
            if self._want_raw and (raw is None or len(raw) != raw_len):
                raise ProtocolError("raw S2MM buffer contents are shorter than the reported transfer length")
            if self._want_profile and (profile is None or len(profile) != profile_len):
                raise ProtocolError("Profile S2MM buffer contents are shorter than the reported transfer length")
            # Diagnostic publication is a separate post-TLAST transaction.  It
            # can fail after both DMA channels have completed, so it must be
            # inside the same cleanup/recovery boundary as buffer release.
            # The raw-only overlay still publishes a PL completion/diagnostic
            # transaction; accepting DMA TLAST alone would allow a late PL
            # error or a stale diagnostic snapshot to be reported as success.
            diagnostics = self._wait_published(require_rows=self._want_profile)
            diagnostics = self._diagnostics_for_payload(profile, diagnostics)
            if self._want_raw:
                self.raw_dma.cleanup_buffer(retain=bool(getattr(self, "_reuse_raw_buffer", False)))
                self._raw_buffer = None
            if self._want_profile:
                self.profile_dma.cleanup_buffer()
                self._profile_buffer = None
        except Exception as exc:
            # Never publish a partial pair.  A diagnostic or cleanup failure
            # leaves the transport in an explicit recovery path; stop() keeps
            # any DMA buffer whose channel did not really halt.
            try:
                self.stop()
            except Exception as recovery_exc:
                raise DMAError(f"PYNQ receive failed and recovery failed: {recovery_exc}") from exc
            raise
        else:
            self._raw_buffer = None
            self._profile_buffer = None
            self._want_raw = self._want_profile = False
            self._accepted_request_id = None
            self._accepted_epoch = None
            return TransportCapture(raw, profile, diagnostics, raw_len, profile_len)

    def _wait_published(self, *, require_rows: bool = True) -> DiagnosticsSnapshot:
        """Wait for the post-TLAST diagnostic snapshot to become stable."""

        expected_id = self._accepted_request_id
        expected_epoch = self._accepted_epoch
        if expected_id is None or expected_epoch is None:
            raise ProtocolError("no accepted request identity is available for diagnostics")
        deadline = time.monotonic() + self.transition_timeout_s
        while time.monotonic() < deadline:
            control_status = int(self.mmio.read(REG_STATUS)) & 0xFFFF_FFFF
            legacy_errors = int(self.mmio.read(REG_ERROR_FLAGS)) & 0xFFFF_FFFF
            request_before = int(self._diag_mmio.read(DIAG_OFFSETS["request_id"])) & 0xFFFF_FFFF
            epoch_before = int(self._diag_mmio.read(DIAG_OFFSETS["epoch"])) & 0xFFFF
            ready_word = int(self._diag_mmio.read(DIAG_OFFSETS["ready"])) & 0xFFFF_FFFF
            ready = bool(ready_word & 1) and not bool(ready_word & 0x4)
            row_count = int(self._diag_mmio.read(DIAG_OFFSETS["row_count"])) & 0xFFFF_FFFF
            capture_errors = int(self._diag_mmio.read(DIAG_OFFSETS["error"])) & 0xFFFF_FFFF
            if legacy_errors or capture_errors:
                raise ProtocolError(
                    f"PL capture reported errors (STATUS=0x{control_status:08X}, "
                    f"ERROR_FLAGS=0x{legacy_errors:08X}, DIAG_ERROR=0x{capture_errors:08X})"
                )
            if row_count > self.frame_height:
                raise ProtocolError(f"diagnostic row_count {row_count} exceeds configured frame height {self.frame_height}")
            # STATUS bit 1 is the sticky PL completion latch.  DMA TLAST is
            # necessary but not sufficient: accepting STATUS=0 here could
            # publish a stale diagnostic snapshot after an early/false DMA
            # completion, especially for the first request whose ID is zero.
            status_complete = (
                (control_status & 0x1) == 0 and
                (control_status & 0x2) != 0 and
                (control_status & 0x4) == 0
            )
            rows_complete = (not require_rows) or row_count == self.frame_height
            if (
                request_before == expected_id and epoch_before == expected_epoch
                and ready and status_complete and rows_complete
            ):
                snapshot = self.read_diagnostics()
                request_after = int(self._diag_mmio.read(DIAG_OFFSETS["request_id"])) & 0xFFFF_FFFF
                epoch_after = int(self._diag_mmio.read(DIAG_OFFSETS["epoch"])) & 0xFFFF
                row_after = int(self._diag_mmio.read(DIAG_OFFSETS["row_count"])) & 0xFFFF_FFFF
                status_after = int(self.mmio.read(REG_STATUS)) & 0xFFFF_FFFF
                legacy_after = int(self.mmio.read(REG_ERROR_FLAGS)) & 0xFFFF_FFFF
                capture_after = int(self._diag_mmio.read(DIAG_OFFSETS["error"])) & 0xFFFF_FFFF
                if (
                    request_after == request_before == snapshot.request_id == expected_id
                    and epoch_after == epoch_before == snapshot.epoch == expected_epoch
                    and row_after == row_count
                    and (not require_rows or row_after == self.frame_height)
                    and (status_after & 0x1) == 0
                    and (status_after & 0x2) != 0
                    and (status_after & 0x4) == 0
                    and not legacy_after and not capture_after
                ):
                    return snapshot
            time.sleep(0.001)
        raise CaptureTimeout("diagnostic snapshot was not published within the bounded deadline")

    def read_diagnostics(self) -> DiagnosticsSnapshot:
        """Read the published diagnostic snapshot after both DMA TLASTs."""

        def rd(offset: int) -> int:
            return int(self._diag_mmio.read(offset)) & 0xFFFF_FFFF

        row_count = rd(DIAG_OFFSETS["row_count"])
        if row_count > self.frame_height:
            raise ProtocolError(f"diagnostic row_count {row_count} exceeds configured frame height {self.frame_height}")
        if rd(DIAG_OFFSETS["width"]) != self.frame_width or rd(DIAG_OFFSETS["height"]) != self.frame_height:
            raise ProtocolError("diagnostic frame dimensions do not match the configured hardware mode")
        rows: list[DiagnosticRow] = []
        capability_word = rd(DIAG_OFFSETS["capabilities"])
        # Raw capture publishes its observed row count, but has no row BRAM.
        # Reading that unsupported window returns AXI SLVERR (SIGBUS on ARM).
        table_count = row_count if capability_word & RUNTIME_CAPABILITY_BITS["row_diagnostics"] else 0
        base = DIAG_OFFSETS["row_table"]
        for index in range(table_count):
            values = [rd(base + index * 0x40 + word * 4) for word in range(10)]
            packed = values[7]
            rows.append(DiagnosticRow(
                row=values[0], xc_fixed=values[1], s0=values[2], s1=values[3],
                peak=values[4], width=values[5], candidate_count=values[6],
                flags=(packed >> 8) & 0x7FFF, quality=packed & 0xFF,
                valid=bool((packed >> 23) & 1), diagnostics=values[8], profile_id=values[9],
            ))
        snapshot = DiagnosticsSnapshot(
            version=rd(DIAG_OFFSETS["version"]), capabilities=capability_word,
            ready=bool(rd(DIAG_OFFSETS["ready"]) & 1), pclk_hz=rd(DIAG_OFFSETS["pclk_hz"]),
            width=rd(DIAG_OFFSETS["width"]), height=rd(DIAG_OFFSETS["height"]),
            last_line=rd(DIAG_OFFSETS["last_line"]), last_frame=rd(DIAG_OFFSETS["last_frame"]),
            frame_count=rd(DIAG_OFFSETS["frame_count"]),
            camera_device_address=rd(DIAG_OFFSETS["camera_device_address"]),
            camera_register_address=rd(DIAG_OFFSETS["camera_register_address"]),
            camera_write_data=rd(DIAG_OFFSETS["camera_write_data"]), camera_read_data=rd(DIAG_OFFSETS["camera_read_data"]),
            camera_command=rd(DIAG_OFFSETS["camera_command"]), camera_status=rd(DIAG_OFFSETS["camera_status"]),
            camera_control=rd(DIAG_OFFSETS["camera_control"]), abort=rd(DIAG_OFFSETS["abort"]),
            epoch=rd(DIAG_OFFSETS["epoch"]), request_id=rd(DIAG_OFFSETS["request_id"]),
            row_count=row_count, error=rd(DIAG_OFFSETS["error"]), rows=tuple(rows),
        )
        # Keep the transport-side status snapshot aligned with the published
        # diagnostic epoch and request ID for Controller.get_status.
        self.mmio.epoch = snapshot.epoch & 0xFFFF
        self.mmio._request_id = snapshot.request_id
        return snapshot

    def _save_recovery_state(self, reason: str) -> None:
        """Capture PL/DMA state before issuing an abort or STOP command."""

        snapshot: dict[str, Any] = {
            "reason": reason,
            "time_monotonic": time.monotonic(),
            "inflight_request_id": getattr(self, "_accepted_request_id", None),
            "inflight_epoch": getattr(self, "_accepted_epoch", None),
            "last_accepted_id": getattr(self, "_last_accepted_id", None),
            "last_accepted_epoch": getattr(self, "_last_accepted_epoch", None),
            "recovery_required_before": bool(getattr(self, "_recovery_required", False)),
        }
        try:
            snapshot["control_status"] = int(self.mmio.read(REG_STATUS)) & 0xFFFF_FFFF
        except Exception as exc:
            snapshot["control_status_error"] = str(exc)
        for name, dma in (("raw", self.raw_dma), ("profile", self.profile_dma)):
            if dma is None:
                continue
            item: dict[str, Any] = {}
            try:
                item["status"] = dma._rd(dma.status_offset)
                item["control"] = dma._rd(dma.control_offset)
                item["length"] = dma._rd(dma.length_offset)
            except Exception as exc:
                item["read_error"] = str(exc)
            state = dma.transfer_state
            if state is not None:
                item.update({
                    "requested_length": state.requested_length,
                    "actual_length": state.actual_length,
                    "armed": state.armed,
                })
            snapshot[f"dma_{name}"] = item
        try:
            snapshot["diag_epoch"] = int(self._diag_mmio.read(DIAG_OFFSETS["epoch"])) & 0xFFFF
            snapshot["diag_request_id"] = int(self._diag_mmio.read(DIAG_OFFSETS["request_id"])) & 0xFFFF_FFFF
            snapshot["diag_ready"] = int(self._diag_mmio.read(DIAG_OFFSETS["ready"])) & 0xFFFF_FFFF
            snapshot["diag_error"] = int(self._diag_mmio.read(DIAG_OFFSETS["error"])) & 0xFFFF_FFFF
            snapshot["cancel_evidence_pre"] = self._read_cancel_evidence()
        except Exception as exc:
            snapshot["diag_read_error"] = str(exc)
        self.last_recovery = snapshot

    def _wait_ready_barrier(self) -> None:
        """Wait until system/camera domains are idle and diagnostics are ready."""

        deadline = time.monotonic() + self.transition_timeout_s
        last_status = last_ready = 0
        while time.monotonic() < deadline:
            last_status = int(self.mmio.read(REG_STATUS)) & 0xFFFF_FFFF
            last_ready = int(self._diag_mmio.read(DIAG_OFFSETS["ready"])) & 0xFFFF_FFFF
            # STATUS bit 0 is capture busy and bit 2 is packer busy.  The
            # diagnostic READY word exposes camera-domain reset_busy at bit 2.
            if (last_status & 0x5) == 0 and (last_ready & 0x1) and not (last_ready & 0x4):
                self.mmio.epoch = int(self._diag_mmio.read(DIAG_OFFSETS["epoch"])) & 0xFFFF
                self.mmio._request_id = int(self._diag_mmio.read(DIAG_OFFSETS["request_id"])) & 0xFFFF_FFFF
                return
            time.sleep(0.001)
        raise CaptureTimeout(
            f"PYNQ reset barrier did not become ready within {self.transition_timeout_s:.3f}s "
            f"(status=0x{last_status:08X}, ready=0x{last_ready:08X})"
        )

    def stop(self, *, _reason: str = "stop") -> None:
        """Abort PL first, then stop/reset DMA and release only safe buffers."""

        self._save_recovery_state(_reason)
        recovery_snapshot = self.last_recovery
        inflight_id = recovery_snapshot.get("inflight_request_id")
        inflight_epoch = recovery_snapshot.get("inflight_epoch")
        # A STOP can race the PL's transition to idle.  The identity captured
        # above is therefore the authority for this recovery attempt; the
        # pre-stop STATUS bit is only evidence, never a prerequisite.
        errors: list[Exception] = []
        # STOP must reach the acquisition state machine before any DMA reset;
        # otherwise a live camera stream can write into a buffer while the
        # host is already trying to release it.
        try:
            self.mmio.write(REG_CONTROL, 2)
        except Exception as exc:
            errors.append(exc)

        for name, dma in (("raw", self.raw_dma), ("profile", self.profile_dma)):
            if dma is None:
                continue
            stopped = False
            try:
                dma.stop(timeout_s=self.transition_timeout_s)
                stopped = dma.reset_confirmed
            except Exception as exc:
                errors.append(exc)
            if stopped:
                try:
                    dma.cleanup_buffer()
                except Exception as exc:
                    errors.append(exc)
                else:
                    if name == "raw":
                        self._raw_buffer = None
                    else:
                        self._profile_buffer = None

        barrier_error: Exception | None = None
        try:
            self._wait_ready_barrier()
        except Exception as exc:
            barrier_error = exc
            errors.append(exc)

        try:
            cancel_evidence = self._read_cancel_evidence()
        except Exception as exc:
            cancel_evidence = {"valid": False, "read_error": str(exc), "reason": None}
            errors.append(exc)
        recovery_snapshot["cancel_evidence_post"] = cancel_evidence
        recovery_snapshot["stop_evidence"] = {
            name: getattr(dma, "last_stop_evidence", {})
            for name, dma in (("raw", self.raw_dma), ("profile", self.profile_dma))
            if dma is not None
        }
        recovery_snapshot["recovery_barrier_passed"] = barrier_error is None

        # A failed channel recovery retains its transfer_state and host buffer
        # for a later bounded retry.  Clear request intent only after all
        # channels have either been safely released or were never armed.
        all_released = (
            self._raw_buffer is None and self._profile_buffer is None
            and self.raw_dma.transfer_state is None
            and (self.profile_dma is None or self.profile_dma.transfer_state is None)
        )
        recovery_identity_matches = (
            inflight_id is not None and inflight_epoch is not None and
            recovery_snapshot.get("diag_request_id") == inflight_id and
            recovery_snapshot.get("diag_epoch") == inflight_epoch
        )
        # The identity is checked against the post-barrier registers as well;
        # a board that published another generation while the DMA was being
        # stopped cannot grant this transport an epoch token.
        try:
            post_id = int(self._diag_mmio.read(DIAG_OFFSETS["request_id"])) & 0xFFFF_FFFF
            post_epoch = int(self._diag_mmio.read(DIAG_OFFSETS["epoch"])) & 0xFFFF
            recovery_snapshot["diag_request_id_post"] = post_id
            recovery_snapshot["diag_epoch_post"] = post_epoch
            recovery_identity_matches = recovery_identity_matches and post_id == inflight_id and post_epoch == inflight_epoch
        except Exception as exc:
            recovery_identity_matches = False
            recovery_snapshot["diag_post_read_error"] = str(exc)

        cancellation_verified = bool(cancel_evidence.get("valid")) and recovery_identity_matches
        barrier_verified = not errors
        if all_released and barrier_verified and cancellation_verified:
            # Only this path creates the one-shot +1 epoch token.  In
            # particular, an explicit stop on an idle transport cannot mint a
            # token, and a repeated stop after this cleanup has no in-flight
            # identity to match.
            self._recovery_token = {
                "base_request_id": int(inflight_id) & 0xFFFF_FFFF,
                "base_epoch": int(inflight_epoch) & 0xFFFF,
                "expected_epoch": self._next_epoch(int(inflight_epoch)),
                "reason": str(cancel_evidence.get("reason") or _reason),
            }
            recovery_snapshot["recovery_token_granted"] = True
            self._recovery_required = False
        elif (
            all_released and barrier_verified and inflight_id is None
            and not recovery_snapshot.get("recovery_required_before", False)
        ):
            # Idempotent STOP/close after a completed transfer.
            recovery_snapshot["recovery_token_granted"] = False
            self._recovery_required = False
        else:
            recovery_snapshot["recovery_token_granted"] = False
            # Any ambiguous cancellation, failed barrier, or unconfirmed DMA
            # ownership blocks START until a later stop call proves recovery.
            self._recovery_required = True

        if all_released and not errors:
            self._want_raw = self._want_profile = False
            self._accepted_request_id = None
            self._accepted_epoch = None
        else:
            self._want_raw = self._want_profile = False

        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise DMAError("PYNQ recovery failed: " + "; ".join(str(item) for item in errors)) from errors[0]

    def reset(self, *, reason: str = "reset") -> None:
        # Keep the reason in the recovery evidence while using the same
        # bounded STOP/DMA/reset barrier as an explicit user stop.
        self.stop(_reason=reason)
        self._wait_ready_barrier()

    def close(self) -> None:
        if (
            self._raw_buffer is not None or self._profile_buffer is not None
            or self.raw_dma.transfer_state is not None
            or (self.profile_dma is not None and self.profile_dma.transfer_state is not None)
        ):
            self.reset(reason="close")
        self.raw_dma.release_idle_buffer()
        if self.profile_dma is not None:
            self.profile_dma.release_idle_buffer()



__all__ = [
    "sha256_file", "verify_overlay_artifacts", "validate_runtime_contract",
    "parse_hwh_addresses", "parse_hwh_bases",
    "parse_hwh_parameters", "DmaTransfer",
    "BoundedS2MM", "PynqTransport",
]
