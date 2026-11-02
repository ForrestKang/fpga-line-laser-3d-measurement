"""Explicit real-board acceptance runner; never substitutes Mock for hardware."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/python'))
import yaml


def _config_value(config: dict, *names: str):
    for name in names:
        if name in config and config[name] is not None:
            return config[name]
    return None


def _manifest_value(record: dict, *names: str):
    for name in names:
        if name in record:
            return record[name]
    source = record.get('source')
    if isinstance(source, dict):
        for name in names:
            if name in source:
                return source[name]
    return None


def _integer(value, name: str) -> int:
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be an integer') from exc


def _hwh_ranges(path: Path) -> tuple[dict, ...]:
    try:
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f'cannot parse HWH {path}: {exc}') from exc
    result = []
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1].upper() != 'MEMRANGE':
            continue
        attrs = {key.upper(): value for key, value in node.attrib.items()}
        if 'BASEVALUE' not in attrs:
            continue
        result.append({
            'base': _integer(attrs['BASEVALUE'], 'HWH MEMRANGE BASEVALUE'),
            'instance': attrs.get('INSTANCE', ''),
            'master': attrs.get('MASTERBUSINTERFACE', ''),
            'slave': attrs.get('SLAVEBUSINTERFACE', ''),
        })
    return tuple(result)


def _hwh_systeminfo(path: Path) -> dict[str, str]:
    try:
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f'cannot parse HWH {path}: {exc}') from exc
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1].upper() == 'SYSTEMINFO':
            return {key.upper(): value for key, value in node.attrib.items()}
    raise ValueError('HWH is missing SYSTEMINFO')


def _hwh_parameter(parameters: dict[str, str], suffix: str, *, instance: str | None = None) -> int | None:
    suffix = suffix.upper()
    values = []
    for key, value in parameters.items():
        upper = key.upper()
        if instance is not None and not upper.startswith(instance.upper() + '.'):
            continue
        if upper.endswith('.' + suffix) or upper == suffix:
            values.append(_integer(value, f'HWH parameter {key}'))
    if not values:
        return None
    if len(set(values)) != 1:
        raise ValueError(f'HWH contains contradictory values for {suffix}: {values!r}')
    return values[0]


def _artifact_path(config: dict, name: str) -> Path:
    value = config.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f'confirmed config must provide {name}')
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def validate_overlay_contract(config: dict, overlay_mode: str) -> dict:
    """Validate all static artifact/HWH/contract bindings before Overlay().

    This intentionally duplicates no bitstream loading.  It verifies the
    manifest bytes, target part, frozen contract, exact PS address/interface
    bindings, HWH geometry/mode parameters, and DMA configuration first.
    """
    if overlay_mode not in {'camera_raw', 'camera_raw_monitor', 'camera_raw_hdmi', 'structured_light_profile', 'structured_light_profile_hdmi'}:
        raise ValueError(f'unsupported overlay mode: {overlay_mode}')
    bitfile = _artifact_path(config, 'bitfile')
    hwh = _artifact_path(config, 'hwh')
    manifest = _artifact_path(config, 'manifest')
    contract_path = ROOT / 'src' / 'contracts' / 'p2_board.json'
    sys.path.insert(0, str(ROOT / 'src/python'))
    from structured_light.p2.contract import (
        DIAGNOSTIC_VERSION, SUPPORTED_DIAGNOSTIC_VERSIONS, PROFILE_FORMAT_VERSION, REGMAP_VERSION,
        TARGET_DEVICE, load_board_contract,
    )
    from structured_light.p2.pynq_transport import (
        parse_hwh_parameters, sha256_file, verify_overlay_artifacts,
    )
    contract = load_board_contract(contract_path)
    expected_addresses = {
        'pipeline_control': contract.control_base,
        'camera_diagnostics': contract.diagnostics_base,
        'raw_dma': contract.raw_dma_base,
    }
    if overlay_mode in {'structured_light_profile', 'structured_light_profile_hdmi'}:
        expected_addresses['profile_dma'] = contract.profile_dma_base
    if overlay_mode in {'camera_raw_hdmi', 'structured_light_profile_hdmi'}:
        expected_addresses.update(hdmi_vdma=0x40420000 if overlay_mode == 'structured_light_profile_hdmi' else 0x40410000, hdmi_vtc=0x43c20000,
                                  hdmi_dynclk=0x43c30000)
    record = verify_overlay_artifacts(
        bitfile, hwh, manifest, expected_addresses=expected_addresses,
    )
    if _manifest_value(record, 'preboard_passed') is not True:
        raise ValueError('manifest preboard_passed must be true before Overlay load')

    device = _manifest_value(record, 'device', 'part')
    if str(device).lower() != TARGET_DEVICE:
        raise ValueError(f'manifest target {device!r} != {TARGET_DEVICE!r}')
    for name, expected in (
        ('regmap_version', REGMAP_VERSION),
        ('diagnostic_version', DIAGNOSTIC_VERSION),
        ('profile_format_version', PROFILE_FORMAT_VERSION),
    ):
        value = _manifest_value(record, name)
        if value is None or (_integer(value, f'manifest {name}') != expected and not (name == 'diagnostic_version' and _integer(value, name) in SUPPORTED_DIAGNOSTIC_VERSIONS)):
            raise ValueError(f'manifest {name} does not match frozen contract')
    declared_mode = _manifest_value(record, 'overlay_mode', 'overlay')
    allowed_modes = {overlay_mode, overlay_mode.replace('structured_light_', '')}
    if str(declared_mode) not in allowed_modes:
        raise ValueError(f'manifest overlay_mode {declared_mode!r} != {overlay_mode!r}')
    contract_sha = _manifest_value(record, 'contract_sha256', 'contract_sha')
    if not isinstance(contract_sha, str) or sha256_file(contract_path).lower() != contract_sha.lower():
        raise ValueError('manifest contract_sha256 does not match src/contracts/p2_board.json')
    source_sha = _manifest_value(record, 'source_sha256', 'source_combined_sha256', 'source_hash')
    if not isinstance(source_sha, (str, dict)):
        raise ValueError('manifest is missing source_sha256/source_combined_sha256')
    source_values = (source_sha,) if isinstance(source_sha, str) else tuple(source_sha.values())
    if not source_values or any(
        not isinstance(value, str) or len(value) != 64
        or any(char not in '0123456789abcdefABCDEF' for char in value)
        for value in source_values
    ):
        raise ValueError('manifest source_sha256 must contain valid SHA-256 values')

    frame = record.get('frame') if isinstance(record.get('frame'), dict) else {}
    manifest_width = _manifest_value(record, 'frame_width') or frame.get('width')
    manifest_height = _manifest_value(record, 'frame_height') or frame.get('height')
    if manifest_width is None or manifest_height is None:
        raise ValueError('manifest must declare frame_width and frame_height')
    manifest_width, manifest_height = _integer(manifest_width, 'manifest frame_width'), _integer(manifest_height, 'manifest frame_height')
    if (manifest_width, manifest_height) not in {(640, 480), (752, 480)}:
        raise ValueError('manifest geometry must be 640/752 x 480')

    info = _hwh_systeminfo(hwh)
    if (info.get('DEVICE', '').lower(), info.get('PACKAGE', '').lower(), info.get('SPEEDGRADE', '')) != ('7z020', 'clg400', '-1'):
        raise ValueError(f'HWH SYSTEMINFO does not target xc7z020clg400-1: {info!r}')
    parameters = parse_hwh_parameters(hwh)
    hwh_width = _hwh_parameter(parameters, 'FRAME_WIDTH')
    hwh_height = _hwh_parameter(parameters, 'FRAME_HEIGHT')
    profile_enable = _hwh_parameter(parameters, 'PROFILE_ENABLE')
    if hwh_width is None or hwh_height is None or profile_enable is None:
        raise ValueError('HWH must declare FRAME_WIDTH, FRAME_HEIGHT and PROFILE_ENABLE')
    if (hwh_width, hwh_height) != (manifest_width, manifest_height):
        raise ValueError('HWH geometry disagrees with manifest geometry')
    if bool(profile_enable) != (overlay_mode in {'structured_light_profile', 'structured_light_profile_hdmi'}):
        raise ValueError('HWH PROFILE_ENABLE disagrees with requested overlay mode')
    config_width = _config_value(config, 'width', 'frame_width')
    config_height = _config_value(config, 'height', 'frame_height')
    if config_width is not None and _integer(config_width, 'config width') != hwh_width:
        raise ValueError('confirmed config width disagrees with HWH')
    if config_height is not None and _integer(config_height, 'config height') != hwh_height:
        raise ValueError('confirmed config height disagrees with HWH')

    bindings = {
        'pipeline_control': ('processing_pipeline', 'S_AXI_CONTROL'),
        'camera_diagnostics': ('processing_pipeline', 'S_AXI_DIAG'),
        'raw_dma': ('raw_dma', 'S_AXI_LITE'),
    }
    if overlay_mode in {'structured_light_profile', 'structured_light_profile_hdmi'}:
        bindings['profile_dma'] = ('profile_dma', 'S_AXI_LITE')
    if overlay_mode in {'camera_raw_hdmi', 'structured_light_profile_hdmi'}:
        video_hierarchy = ('hdmi_out' if overlay_mode == 'structured_light_profile_hdmi'
                           else 'hdmi_out/frontend')
        bindings.update(hdmi_vdma=('hdmi_vdma', 'S_AXI_LITE'),
                        hdmi_vtc=(video_hierarchy + '/vtc_out', 'ctrl'),
                        hdmi_dynclk=(video_hierarchy + '/axi_dynclk', 's00_axi'))
    ranges = _hwh_ranges(hwh)
    if overlay_mode in {'camera_raw_hdmi', 'structured_light_profile_hdmi'}:
        from structured_light.p2.video_reset_contract import verify_video_reset_handoff
        verify_video_reset_handoff(hwh)
    for name, address in expected_addresses.items():
        instance, slave = bindings[name]
        if not any(
            item['base'] == address and item['instance'].upper() in {
                instance.upper(), instance.replace('/', '_').upper()}
            and item['slave'].upper() == slave.upper() and item['master'].upper() == 'M_AXI_GP0'
            for item in ranges
        ):
            raise ValueError(f'HWH M_AXI_GP0 binding mismatch for {name}')

    dma_instances = ['raw_dma'] + (['profile_dma'] if overlay_mode in {'structured_light_profile', 'structured_light_profile_hdmi'} else [])
    dma_expected = {
        'C_INCLUDE_SG': 0, 'C_INCLUDE_MM2S': 0, 'C_INCLUDE_MM2S_DRE': 0,
        'C_INCLUDE_S2MM': 1, 'C_INCLUDE_S2MM_DRE': 0,
        'C_M_AXI_S2MM_ADDR_WIDTH': 32, 'C_M_AXI_S2MM_DATA_WIDTH': 64,
        'C_S_AXIS_S2MM_TDATA_WIDTH': 32, 'C_SG_LENGTH_WIDTH': 20,
    }
    for instance in dma_instances:
        for suffix, expected in dma_expected.items():
            actual = _hwh_parameter(parameters, suffix, instance=instance)
            if actual is None or actual != expected:
                raise ValueError(f'HWH {instance} {suffix}={actual!r}, expected {expected}')
    if overlay_mode in {'camera_raw_hdmi', 'structured_light_profile_hdmi'}:
        for suffix, expected in {'C_INCLUDE_SG': 0, 'C_INCLUDE_MM2S': 1,
                                  'C_INCLUDE_S2MM': 0, 'C_M_AXI_MM2S_DATA_WIDTH': 64,
                                  'C_M_AXIS_MM2S_TDATA_WIDTH': 32}.items():
            actual = _hwh_parameter(parameters, suffix, instance='hdmi_vdma')
            if actual != expected:
                raise ValueError(f'HWH HDMI VDMA {suffix}={actual!r}, expected {expected}')

    return {
        'bitfile': str(bitfile), 'hwh': str(hwh), 'manifest': str(manifest),
        'part': TARGET_DEVICE, 'overlay_mode': overlay_mode,
        'frame_width': hwh_width, 'frame_height': hwh_height,
        'profile_enable': profile_enable, 'addresses': expected_addresses,
        # The build manifest binds this bit/HWH pair to its source tree.  The
        # board acceptance path validates the binding's presence and SHA-256
        # shape, but does not claim to re-hash RTL that was not deployed with
        # the runtime package.
        'source_binding': {
            'manifest_present': True,
            'local_source_hash_verified': False,
            'scope': 'build_manifest_provenance_only',
        },
        'bit_sha256': sha256_file(bitfile), 'hwh_sha256': sha256_file(hwh),
        'manifest_sha256': sha256_file(manifest), 'contract_sha256': sha256_file(contract_path),
    }


def _close_capture_resources(controller, transport, recorder) -> None:
    errors = []
    if controller is not None:
        try:
            controller.close()
        except Exception as exc:
            errors.append(exc)
    elif transport is not None:
        try:
            transport.close()
        except Exception as exc:
            errors.append(exc)
    try:
        recorder.close()
    except Exception as exc:
        errors.append(exc)
    # Preserve the original board/transport exception from a surrounding
    # try/finally; only surface cleanup failure when no primary error exists.
    if errors and sys.exc_info()[0] is None:
        raise RuntimeError('board resource cleanup failed: ' + '; '.join(str(e) for e in errors)) from errors[0]

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


GRAY_STAGES = {'signals', 'first-frame', 'hdmi', 'scene-a', 'scene-b', 'scene-aprime'}
GRAY_GATE_A_MODES = {'camera_raw_monitor', 'camera_raw_hdmi'}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_sha256(value, label: str) -> str:
    if (not isinstance(value, str) or len(value) != 64 or
            any(char not in '0123456789abcdef' for char in value)):
        raise ValueError(f'{label} must be a lowercase SHA-256 digest')
    return value


def _check_gray_target_identity(config: dict) -> dict:
    contract_path = ROOT / 'board/configs/p2_target_identity.json'
    try:
        contract = json.loads(contract_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'cannot load packaged PYNQ-Z2 identity contract: {exc}') from exc
    if contract.get('schema_version') != 1 or contract.get('target') != 'PYNQ-Z2':
        raise ValueError('unsupported packaged target identity contract')
    if contract.get('user_confirmed_board') != 'PYNQ-Z2':
        raise ValueError('target identity lacks explicit PYNQ-Z2 confirmation')
    contract_sha = _sha256_file(contract_path)
    if config.get('identity_contract_sha256') != contract_sha:
        raise ValueError('board identity contract hash does not match packaged contract')
    if config.get('user_confirmed_board') != 'PYNQ-Z2':
        raise ValueError('deployment config does not preserve user-confirmed PYNQ-Z2 identity')
    host_key = _require_sha256(config.get('ssh_host_key_sha256'), 'SSH host-key fingerprint')
    if host_key != contract.get('ssh_host_key_sha256'):
        raise ValueError('SSH host-key fingerprint does not match the pinned target contract')

    expected = contract.get('machine_identity')
    actual = config.get('board_identity')
    required = ('hostname', 'machine', 'device_tree_model', 'device_tree_compatible')
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        raise ValueError('exact board identity readback is missing')
    for field in required:
        if field not in expected or field not in actual or actual[field] != expected[field]:
            raise ValueError(f'board identity mismatch for {field}')
    capture = config.get('board_identity_capture')
    if (not isinstance(capture, dict) or not isinstance(capture.get('boot_id'), str) or
            not capture['boot_id'] or not isinstance(capture.get('captured_utc'), str)):
        raise ValueError('board identity capture metadata is missing')
    # boot_id is deliberately recorded for provenance, never pinned: it changes
    # after a legitimate reboot. The generic Zynq compatible is compared as
    # observed but is not treated as a unique PYNQ-Z2 identifier.
    return {'target': 'PYNQ-Z2', 'hostname': actual['hostname'],
            'machine': actual['machine'], 'device_tree_model': actual['device_tree_model'],
            'device_tree_compatible': actual['device_tree_compatible'],
            'boot_id': capture['boot_id'], 'ssh_host_key_sha256': host_key,
            'identity_contract_sha256': contract_sha}


def _check_gray_gate_a(config: dict, config_path: Path) -> dict:
    if config.get('hardware_confirmed') is not True:
        raise ValueError('hardware_pending: first-frame/HDMI requires measured Gate-A evidence')
    if (config.get('width'), config.get('height')) not in {(640, 480), (752, 480)}:
        raise ValueError('Gate-A geometry must be measured as 640/752 x 480')
    for field in ('fv_active', 'lv_active'):
        if isinstance(config.get(field), bool) or config.get(field) not in (0, 1):
            raise ValueError(f'measured {field} polarity is required')
    pclk = config.get('pclk_hz')
    if isinstance(pclk, bool) or not isinstance(pclk, int) or pclk <= 0:
        raise ValueError('measured nonzero PCLK is required')
    period = config.get('frame_period_s')
    if (isinstance(period, bool) or not isinstance(period, (float, int)) or
            not math.isfinite(period) or period <= 0):
        raise ValueError('measured frame_period_s is required')

    evidence_value = config.get('gate_a_evidence')
    if not isinstance(evidence_value, str) or not evidence_value:
        raise ValueError('hashed Gate-A signal evidence path is required')
    evidence_path = Path(evidence_value)
    if not evidence_path.is_absolute():
        evidence_path = config_path.parent / evidence_path
    try:
        evidence_path = evidence_path.resolve(strict=True)
        evidence_bytes = evidence_path.read_bytes()
    except OSError as exc:
        raise ValueError(f'Gate-A signal evidence is unavailable: {exc}') from exc
    expected_sha = _require_sha256(config.get('gate_a_sha256'), 'Gate-A evidence fingerprint')
    if hashlib.sha256(evidence_bytes).hexdigest() != expected_sha:
        raise ValueError('Gate-A signal evidence SHA-256 mismatch')
    try:
        gate = json.loads(evidence_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError('Gate-A signal evidence is not valid JSON') from exc
    if (not isinstance(gate, dict) or gate.get('status') != 'camera_signal_verified' or
            gate.get('camera_signal_verified') is not True):
        raise ValueError('Gate-A evidence is not a passed camera_signal_verified result')
    for field in ('width', 'height', 'fv_active', 'lv_active', 'pclk_hz'):
        value = gate.get(field)
        configured = config.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or configured != value:
            raise ValueError(f'run config {field} differs from hashed Gate-A evidence')
    gate_period = gate.get('frame_period_s')
    if (isinstance(gate_period, bool) or not isinstance(gate_period, (int, float)) or
            not math.isfinite(gate_period) or
            not math.isclose(float(period), float(gate_period), rel_tol=1e-12, abs_tol=1e-12)):
        raise ValueError('run config frame_period_s differs from hashed Gate-A evidence')

    mode = config.get('gate_a_source_overlay_mode')
    if mode not in GRAY_GATE_A_MODES or gate.get('overlay_mode') != mode:
        raise ValueError('Gate-A source must be a locally verified monitor-capable release')
    if config.get('gate_a_source_preboard_passed') is not True:
        raise ValueError('Gate-A source release was not preboard-approved')
    gate_artifacts = gate.get('artifact_sha256')
    config_artifacts = config.get('gate_a_source_artifact_sha256')
    if not isinstance(gate_artifacts, dict) or not isinstance(config_artifacts, dict):
        raise ValueError('Gate-A source artifact provenance is missing')
    if set(gate_artifacts) != {'bit', 'hwh', 'manifest'} or set(config_artifacts) != set(gate_artifacts):
        raise ValueError('Gate-A source artifact provenance fields are incomplete')
    normalized = {key: _require_sha256(value, f'Gate-A source {key} SHA-256')
                  for key, value in gate_artifacts.items()}
    if normalized != config_artifacts:
        raise ValueError('Gate-A source artifact fingerprints differ from hashed evidence')
    return {'gate_a_status': gate['status'], 'gate_a_sha256': expected_sha,
            'gate_a_source_overlay_mode': mode,
            'gate_a_source_artifact_sha256': normalized,
            'gate_a_evidence_path': str(evidence_path)}


def checked_config(path: Path, stage: str) -> dict:
    value = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Board config must be a mapping')
    if value.get('board') != 'pynq_z2' or value.get('part') != 'xc7z020clg400-1':
        raise ValueError('Wrong board/target')
    if stage == 'runtime':
        return value
    if value.get('pin_mapping_confirmed') is not True or not value.get('physical_revision'):
        raise ValueError('Manual wiring and physical module revision must be confirmed first')
    if stage in GRAY_STAGES:
        _check_gray_target_identity(value)
        if stage in {'first-frame', 'hdmi', 'scene-a', 'scene-b', 'scene-aprime'}:
            _check_gray_gate_a(value, path)
        return value
    if stage not in {'camera', 'signals'}:
        if value.get('hardware_confirmed') is not True:
            raise ValueError('hardware_pending/preboard config cannot enable capture')
        if (value.get('width'), value.get('height')) not in {(640, 480), (752, 480)}:
            raise ValueError('Only measured 640/752 x 480 modes supported')
        if value.get('fv_active') not in {0, 1} or value.get('lv_active') not in {0, 1}:
            raise ValueError('Measured FV/LV polarities required')
        if not isinstance(value.get('pclk_hz'), int) or value['pclk_hz'] <= 0:
            raise ValueError('Measured nonzero PCLK required')
        evidence = Path(value['gate_a_evidence'])
        if not evidence.is_absolute():
            evidence = path.parent / evidence
        gate = json.loads(evidence.read_text(encoding='utf-8'))
        if gate.get('status') not in {'camera_input_verified', 'camera_signal_verified'}:
            raise ValueError('Gate A evidence has not passed')
        expected = value.get('gate_a_sha256')
        if not expected or hashlib.sha256(evidence.read_bytes()).hexdigest() != expected:
            raise ValueError('Gate A evidence hash missing/mismatched')
    return value

def _require_pynq_arm() -> None:
    if platform.machine().lower() not in {'armv7l', 'armv8l'}:
        raise RuntimeError('PYNQ-Z2 acceptance must run on the actual ARM board')


def runtime_environment_probe() -> dict:
    """Collect board/PYNQ metadata without touching PL or allocating DDR.

    Importing PYNQ is intentionally kept separate from ``allocate`` and from
    Overlay download. A remote shell does not inherit the Overlay metadata
    that PYNQ's DMA allocator may expect, so capture stages must load their
    signed custom Overlay before any buffer allocation.
    """

    _require_pynq_arm()
    import pynq
    import numpy as np
    tree = Path('/proc/device-tree/model')
    return {'python': sys.version, 'pynq': getattr(pynq, '__version__', 'unknown'),
            'numpy': np.__version__, 'kernel': platform.release(),
            'device_tree_model': tree.read_bytes().rstrip(b'\0').decode() if tree.exists() else None,
            'meminfo': Path('/proc/meminfo').read_text(),
            'allocate_bytes': None, 'allocate_physical_address': None,
            'allocate_readback': 'not_tested', 'allocate_status': 'not_tested',
            'official_base_loaded': False, 'official_base_status': 'not_requested',
            'base_initialization': {
                'official_base_loaded': False,
                'status': 'not_requested',
                'reason': 'explicit runtime base flags are required',
            },
            'XILINX_XRT': os.environ.get('XILINX_XRT'),
            'package_import': __import__('structured_light').__file__,
            'boot_serial_log': 'not_tested', 'ssh': 'not_tested', 'jupyter': 'not_tested',
            'status': 'runtime_environment_checked_allocate_not_tested'}


def _official_base_bit_path() -> Path:
    """Resolve the official PYNQ base bitstream from the installed package."""

    import pynq

    package_root = Path(pynq.__file__).resolve().parent
    candidate = package_root / 'overlays' / 'base' / 'base.bit'
    if not candidate.is_file():
        raise FileNotFoundError(
            f'official PYNQ base bitstream was not found under the installed package: {candidate}'
        )
    return candidate


def initialize_official_base(*, external_ports_unconnected_required: bool) -> dict:
    """Load the official base only after explicit empty-port confirmation."""

    _require_pynq_arm()
    if external_ports_unconnected_required is not True:
        raise ValueError(
            '--initialize-official-base requires '
            '--external-ports-unconnected-required'
        )
    bitfile = _official_base_bit_path()
    from pynq import Overlay

    Overlay(str(bitfile), download=True)
    return {
        'official_base_loaded': True,
        'official_base_status': 'loaded_with_explicit_empty_ports_confirmation',
        'base_initialization': {
            'official_base_loaded': True,
            'status': 'loaded_with_explicit_empty_ports_confirmation',
            'external_ports_unconnected_required': True,
        },
        'official_base_bitfile': str(bitfile),
        'official_base_sha256': hashlib.sha256(bitfile.read_bytes()).hexdigest(),
        'external_ports_unconnected_required': True,
    }


def runtime_allocate_probe() -> dict:
    """Run the separate DDR allocate/readback check after base initialization."""

    _require_pynq_arm()
    import numpy as np
    from pynq import allocate
    pattern = np.arange(8192, dtype=np.uint32) ^ np.uint32(0xA55AA55A)
    buffer = allocate(shape=pattern.shape, dtype=np.uint32)
    try:
        buffer[:] = pattern
        buffer.flush()
        buffer.invalidate()
        if not np.array_equal(buffer, pattern):
            raise RuntimeError('allocate/readback mismatch')
        address = int(buffer.physical_address)
    finally:
        buffer.freebuffer()
    return {
        'allocate_bytes': pattern.nbytes,
        'allocate_physical_address': address,
        'allocate_readback': 'pass',
        'allocate_status': 'passed',
    }


def validate_runtime_options(*, initialize_official_base_flag: bool,
                             external_ports_unconnected_required: bool) -> None:
    """Require both runtime base-initialization confirmations together."""

    if initialize_official_base_flag != external_ports_unconnected_required:
        raise ValueError(
            '--initialize-official-base and '
            '--external-ports-unconnected-required must be supplied together'
        )


def runtime_probe(*, initialize_official_base_flag: bool = False,
                  external_ports_unconnected_required: bool = False) -> dict:
    """Run runtime checks with explicit, opt-in official-base loading."""

    validate_runtime_options(
        initialize_official_base_flag=initialize_official_base_flag,
        external_ports_unconnected_required=external_ports_unconnected_required,
    )
    result = runtime_environment_probe()
    if not initialize_official_base_flag:
        result['missing_checks'] = ['allocate']
        result['status'] = 'runtime_environment_checked_allocate_not_tested'
        return result
    result.update(initialize_official_base(
        external_ports_unconnected_required=external_ports_unconnected_required,
    ))
    result.update(runtime_allocate_probe())
    result['status'] = 'runtime_python_allocate_passed'
    return result

def release_camera(diag):
    """Explicit sensor reset timing; these host delays are not XDC proof.

    MT9V034 Rev G requires RESET_BAR >=750 ns and a 10-SYSCLK delay
    after release. Use 1 ms asserted and 100 ms released before SCCB.
    This runs only during the user's explicit real-board stage.
    """
    diag.write(0x3c, 1)  # RESET_BAR low, STANDBY low.
    time.sleep(0.001)
    diag.write(0x3c, 0)
    time.sleep(0.1)


def camera_probe(config, output):
    validation = validate_overlay_contract(config, 'camera_raw')
    from pynq import MMIO, Overlay
    from structured_light.p2.contract import load_board_contract
    from structured_light.p2.pynq_transport import validate_runtime_contract
    contract = load_board_contract()
    # Use the path that just passed the static contract gate.  A relative
    # path in a copied config must not be resolved against an arbitrary cwd.
    overlay = Overlay(validation['bitfile'], download=True)
    control = MMIO(contract.control_base, contract.window)
    diag = MMIO(contract.diagnostics_base, contract.window)
    runtime_contract = validate_runtime_contract(
        control, diag, overlay_mode='camera_raw',
    )
    release_camera(diag)
    diag.write(0x24, int(config.get('sccb_address', 0x48)))
    def read_sensor(register):
        diag.write(0x28, register)
        diag.write(0x34, 1)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            status = int(diag.read(0x38))
            if status & 0xc:
                raise RuntimeError(f'SCCB NACK/timeout: reg={register:#x}, status={status:#x}')
            if status & 2 and not status & 1:
                return int(diag.read(0x30))
            time.sleep(0.001)
        raise TimeoutError(f'SCCB read timed out: {register:#x}')
    chip = read_sensor(0)
    write_json(output / 'camera_id.json', {'chip_id': chip, 'expected': 0x1324})
    if chip != 0x1324:
        raise RuntimeError(f'Unexpected sensor ID: {chip:#x}')
    registers = {f'{r:02x}': read_sensor(r) for r in (1, 2, 3, 4, 5, 6, 7, 0x0d, 0xb6)}
    samples = []
    for _ in range(3):
        time.sleep(1.1)
        samples.append({'pclk_hz': int(diag.read(0x0c)), 'pixels': int(diag.read(0x18)),
                        'lines': int(diag.read(0x1c)), 'frame_counter': int(diag.read(0x20))})
    if not all(x['pclk_hz'] > 0 for x in samples):
        raise RuntimeError('PCLK is zero; retain evidence and stop before capture')
    # ID/PCLK alone do not prove data variation, polarity or electrical safety.
    return {'chip_id': chip, 'registers': registers, 'samples': samples,
            'status': 'camera_probe_observed', 'camera_input_verified': False,
            'artifact_validation': validation, 'runtime_contract': runtime_contract,
            'pending': ['measured module supply/reset/standby', 'waveform confirms FV/LV polarity',
                        'data variation and measured line/frame geometry', 'frame period',
                        'actual input timing budget'], 'overlay': str(overlay.bitfile_name)}

def capture_batch(config, args, output):
    from structured_light.p2.controller import P2Controller
    from structured_light.p2.pynq_transport import PynqTransport
    from structured_light.p2.session import SessionRecorder
    mode = 'camera_raw' if args.stage == 'raw' else 'structured_light_profile'
    validation = validate_overlay_contract(config, mode)
    write_json(output / 'artifact_validation.json', validation)
    manifest = json.loads(Path(validation['manifest']).read_text(encoding='utf-8'))
    for key, expected in [('frame_width', config['width']), ('frame_height', config['height'])]:
        if manifest.get(key) != expected:
            raise ValueError(f'Overlay mismatch: {key}')
    # FV/LV polarities are required by checked_config and are verified against
    # measured camera evidence.  They are optional in an artifact manifest
    # because the bitstream does not encode the sensor's measured polarity;
    # compare them only when the manifest explicitly records them.
    for key in ('fv_active', 'lv_active'):
        if key in manifest and manifest[key] != config[key]:
            raise ValueError(f'Overlay mismatch: {key}')
    recorder = SessionRecorder(output, metadata={'evidence_tier': 'real_board', 'config': config})
    transport = None
    controller = None
    try:
        transport = PynqTransport(validation['bitfile'], validation['hwh'], validation['manifest'],
                                  frame_width=config['width'], frame_height=config['height'], overlay_mode=mode)
        controller = P2Controller(transport, recorder=recorder)
        release_camera(transport._diag_mmio)
        controller.set_threshold(int(config.get('threshold', 30)))
        controller.configure_roi(*config.get('roi', [0, 0, config['width'], config['height']]))
        deadline = max(1.0, 3 * float(config['frame_period_s']) + 0.1)
        controller.mmio.write(0x50, int(deadline * 100000000))
        controller.default_timeout_s = deadline + 1
        start = time.monotonic()
        previous = None
        for index in range(args.count):
            if args.stage == 'raw':
                frame = controller.capture_raw()
                (output / f'raw_{index:06d}.bin').write_bytes(frame.pixels)
                recorder.event('raw_accepted', index=index, bytes=len(frame.pixels), sha256=hashlib.sha256(frame.pixels).hexdigest())
            else:
                result = controller.capture_pair()
                if result.comparison is None or not result.comparison.passed:
                    raise AssertionError(f'Same-frame mismatch request {result.request_id}')
                identity = (result.epoch, result.request_id)
                if previous is not None and identity <= previous:
                    raise AssertionError(f'Repeated/out-of-order identity {identity}')
                previous = identity
                controller.drain_results()
        return {'requests': args.count, 'elapsed_s': time.monotonic() - start,
                'algorithm_max_error_lsb': 0 if args.stage == 'profile' else None,
                'artifact_validation': validation,
                'status': f'{args.stage}_batch_passed', 'acceptance_requires': 1000,
                'recovery_trials': 'not_tested', 'clock_pause': 'not_tested'}
    finally:
        _close_capture_resources(controller, transport, recorder)


def recovery_trials(config, args, output):
    """Real STOP and output-backpressure trials; no clock-stop claim."""
    from structured_light.p2.controller import P2Controller
    from structured_light.p2.pynq_transport import PynqTransport
    from structured_light.p2.session import SessionRecorder
    from structured_light.p2.errors import CaptureTimeout, DMAError
    validation = validate_overlay_contract(config, 'structured_light_profile')
    write_json(output / 'artifact_validation.json', validation)
    recorder = SessionRecorder(output, metadata={'evidence_tier': 'real_board', 'config': config})
    transport = None
    controller = None
    trials = []
    try:
        transport = PynqTransport(validation['bitfile'], validation['hwh'], validation['manifest'],
                                  frame_width=config['width'], frame_height=config['height'])
        controller = P2Controller(transport, recorder=recorder)
        transport._diag_mmio.write(0x3c, 0)
        controller.set_threshold(int(config.get('threshold', 30)))
        controller.configure_roi(*config.get('roi', [0, 0, config['width'], config['height']]))
        deadline = max(1.0, 3 * float(config['frame_period_s']) + 0.1)
        controller.mmio.write(0x50, int(deadline * 100000000))
        for index in range(args.count):
            handle = controller.capture_single()
            kind = 'stop' if index % 2 == 0 else 'output_backpressure'
            if kind == 'stop':
                controller.stop()
            else:
                # Deliberately stop the profile receiver while the PL request
                # is active.  The bounded transport reset must clean both paths.
                transport.profile_dma.stop(timeout_s=0.5)
                try:
                    controller.recv_profile(timeout_s=deadline + 1)
                except (CaptureTimeout, DMAError) as exc:
                    recorder.event('expected_recovery_fault', index=index, error=str(exc))
                else:
                    raise AssertionError('Fault-injected receiver unexpectedly submitted a Profile')
            after = controller.capture_pair(timeout_s=deadline + 1)
            if not after.comparison.passed or after.epoch <= handle.epoch:
                raise AssertionError('Recovery did not isolate the cancelled transaction')
            controller.drain_results()
            trials.append({'index': index, 'kind': kind, 'cancelled_id': handle.request_id,
                           'before_epoch': handle.epoch, 'after_epoch': after.epoch,
                           'recovered_id': after.request_id, 'passed': True,
                           'recovery_registers': transport.last_recovery})
            write_json(output / 'recovery_trials.json', trials)
        return {'status': 'recovery_batch_passed', 'trials': len(trials),
                'artifact_validation': validation,
                'acceptance_requires': 20, 'clock_pause': 'not_tested',
                'physical_sensor_reset': 'not_tested'}
    finally:
        _close_capture_resources(controller, transport, recorder)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['runtime', 'camera', 'signals', 'first-frame', 'hdmi', 'scene-a', 'scene-b', 'scene-aprime', 'raw', 'profile', 'recovery'], required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--count', type=int, default=1000)
    parser.add_argument(
        '--initialize-official-base', action='store_true',
        help='runtime stage only: explicitly load PYNQ official base.bit before allocate',
    )
    parser.add_argument(
        '--external-ports-unconnected-required', action='store_true',
        help='confirm runtime stage external PL ports are not connected before base.bit load',
    )
    parser.add_argument('--output', type=Path, default=ROOT / 'board/results/p2')
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error('count must be positive')
    if args.stage != 'runtime' and (args.initialize_official_base or args.external_ports_unconnected_required):
        parser.error('official base initialization flags are valid only with --stage runtime')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    output = args.output / run_id
    output.mkdir(parents=True, exist_ok=False)
    result = {'run_id': run_id, 'stage': args.stage, 'status': 'failed', 'evidence_tier': 'real_board',
              'started_utc': datetime.now(timezone.utc).isoformat(), 'no_automatic_retries': True}
    code = 1
    try:
        config = checked_config(args.config, args.stage)
        write_json(output / 'config.json', config)
        if args.stage == 'runtime':
            result['runtime'] = runtime_probe(
                initialize_official_base_flag=args.initialize_official_base,
                external_ports_unconnected_required=args.external_ports_unconnected_required,
            )
            result['status'] = result['runtime']['status']
            # A passive runtime report is useful evidence, but it is not the
            # allocate gate.  Keep the default invocation visibly incomplete
            # so automation cannot mistake it for a full runtime PASS.
            code = 0 if result['status'] == 'runtime_python_allocate_passed' else 2
        else:
            # This is deliberately passive.  Custom camera/raw/profile paths
            # perform their artifact gate and load the matching custom Overlay
            # before PynqTransport allocates any DMA buffers.
            result['runtime'] = runtime_environment_probe()
        if args.stage == 'camera':
            result.update(camera_probe(config, output))
            # ID/PCLK observation is intentionally not Gate A.  Keep a
            # distinct nonzero result until the user supplies measured
            # FV/LV, geometry, polarity and data-variation evidence.
            code = 0 if result.get('camera_input_verified') is True else 2
        elif args.stage in {'signals', 'first-frame', 'hdmi', 'scene-a', 'scene-b', 'scene-aprime'}:
            from gray_capture_board import run_gray_stage
            result.update(run_gray_stage(config, args, output))
            code = 0 if result.get('stage_passed') else 2
        elif args.stage in {'raw', 'profile'}:
            result.update(capture_batch(config, args, output))
            code = 0
        elif args.stage == 'recovery':
            result.update(recovery_trials(config, args, output))
            code = 0
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        result['error'] = str(exc)
    finally:
        result['completed_utc'] = datetime.now(timezone.utc).isoformat()
        write_json(output / 'summary.json', result)
        print(json.dumps(result, ensure_ascii=False, default=str))
    return code

if __name__ == '__main__':
    raise SystemExit(main())
