"""Strict separate/threaded-paired/serial-prepared geometry differential.

This tool deliberately replays a *saved actual-PL* run. It never connects to
the board and never manufactures a transport capability: the saved final frame
is decoded once and handed to the offline admission only to exercise the same
processor API. The comparison covers every frame's observations, acceptance,
reasons, camera/axis XYZ, quality and the complete measurement cloud. The
serial prepared path is the R26 candidate; the two-thread paired path is kept
only for the historical A/B.

Usage:
    python runtime/tools/equiv_geom_r26.py --limit 64 --tag pair64
    python runtime/tools/equiv_geom_r26.py --run-root D:/.../full240z --target T05

Exit code 0 only when every compared field and both cloud hashes are identical.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import numpy as np

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]
RUNTIME = ROOT / 'runtime'
for path in (RUNTIME, RUNTIME / 'structured_light_system_algorithm_v1/src/python',
             RUNTIME / 'structured_light_system_geometry_v1/src/python'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
for extra in (os.environ.get('R26_EXTRA_SITE'),
              r'D:/NUECSDC-FPGA/structured_light_system_geometry_v1/.venv/Lib/site-packages'):
    if extra and Path(extra).is_dir() and extra not in sys.path:
        sys.path.append(extra)

from tools.offline_scan_r26 import (SavedRun, ReplayAdmissionR26, SIM_FINGERPRINT,
                                    load_frozen_inputs, sha256_bytes, read_run_payloads)
from candidate.pfo3_processor_r25 import PLFinalProcessorR25
from candidate.pfo3_geometry_pair_r25 import (PLGeometryPairProcessorR25,
                                              PreparedGeometryPairR25, PairedModeExecutor)
from candidate.pfo3_r25 import identity, HEADER_FIELDS, decode
from candidate.storage_r21 import encode_profile

DEFAULT_RUN = Path(r'D:/Workspace/CodexWork/scan3d_pixel_frontend_20261021_25/evidence/board_runs/full240z')


def _meta(frozen, index, epoch):
    return dict(frozen[index], request_id=index, capture_epoch=epoch, feature_config_revision=1,
                exposure_lines=60, exposure_revision=0, motor_accepted_id=0, motor_epoch=0,
                emitted_command_count=0, fv_ticks=0, exposure_start_ticks=0, exposure_end_ticks=0,
                replay_identity_tags=True, physical_capture_timing_known=False,
                physical_motor_registration=False, simulated_exposure_lines_assumed=60,
                geometry_evidence_type='SIMULATION_ONLY')


def _decode(item):
    header = dict(zip(HEADER_FIELDS, struct.unpack_from('<64I', item['payload'])))
    ident = identity(header)
    frame = decode(item['payload'], expected_identity=ident, expected_fingerprint=SIM_FINGERPRINT)
    admission = ReplayAdmissionR26(ident=ident, parameter_fingerprint=SIM_FINGERPRINT,
        bit_sha256='0' * 64, stream_sha256=sha256_bytes(item['payload']),
        input_raw_sha256=sha256_bytes(item['raw']), primary_sha256=sha256_bytes(item['primary']), receipt={})
    admission._validated_frame = frame
    admission._validated_primary = item['primary']
    return frame, admission


def _payload_hash(frame):
    return hashlib.sha256(frame.payload).hexdigest()


def _quality_equal(a, b):
    if a.status != b.status or a.completeness != b.completeness:
        return False
    for name in ('median_intensity', 'median_width_px', 'saturation_fraction',
                 'ambiguous_fraction', 'saturated_rows'):
        if getattr(a, name) != getattr(b, name):
            return False
    return (a.valid_rows == b.valid_rows and a.expected_rows == b.expected_rows
            and a.rejection_counts == b.rejection_counts and a.raw_observed == b.raw_observed)


def _profile_equal(a, b):
    checks = {}
    for name in ('rows', 'center_q12', 'stripe_accepted', 'valid_points', 'branch_ids',
                 'points_camera_mm', 'points_axis_mm'):
        checks[name] = getattr(a, name).tobytes() == getattr(b, name).tobytes()
    checks['reasons'] = tuple(a.reasons) == tuple(b.reasons)
    checks['observation_reasons'] = tuple(a.observation_reasons) == tuple(b.observation_reasons)
    checks['quality_keys'] = tuple(a.observation_quality) == tuple(b.observation_quality)
    for key in a.observation_quality:
        if key in b.observation_quality:
            checks['quality/' + key] = a.observation_quality[key].tobytes() == b.observation_quality[key].tobytes()
    checks['observation_stats'] = json.dumps(a.observation_stats, sort_keys=True, default=str) == \
        json.dumps(b.observation_stats, sort_keys=True, default=str)
    checks['quality'] = _quality_equal(a.quality, b.quality)
    return checks


def _run_serial(frames, meta_rows, processors):
    measure, preview = processors
    out = []
    for index, (item, meta) in enumerate(zip(frames, meta_rows)):
        frame, admission = _decode(item)
        gray = np.frombuffer(item['raw'], np.uint8).reshape(480, 752)
        measured = measure.process_final(gray, meta, payload=frame.payload, admission=admission,
                                         mode='measurement')
        previewed = preview.process_final(gray, meta, payload=frame.payload, admission=admission,
                                          mode='preview')
        out.append(dict(frame=index, measured=measured, previewed=previewed,
                        profile_measure=encode_profile(measured), profile_preview=encode_profile(previewed),
                        payload_sha=_payload_hash(frame)))
    return out


def _run_paired(frames, meta_rows, processors):
    measure, preview = processors
    out = []
    with PairedModeExecutor() as executor:
        for index, (item, meta) in enumerate(zip(frames, meta_rows)):
            frame, admission = _decode(item)
            pair = PreparedGeometryPairR25(frame, measure.calibration)
            gray = np.frombuffer(item['raw'], np.uint8).reshape(480, 752)
            measured, previewed = executor.process(measure, preview, gray, meta, pair=pair,
                payload=frame.payload, admission=admission)
            out.append(dict(frame=index, measured=measured, previewed=previewed,
                            profile_measure=encode_profile(measured), profile_preview=encode_profile(previewed),
                            payload_sha=_payload_hash(frame)))
    return out


def _run_prepared_serial(frames, meta_rows, processors):
    """One shared camera mapping, then the two independent mode state machines."""
    measure, preview = processors
    out = []
    for index, (item, meta) in enumerate(zip(frames, meta_rows)):
        frame, admission = _decode(item)
        pair = PreparedGeometryPairR25(frame, measure.calibration)
        pair.assert_binding(frame, preview.calibration)
        gray = np.frombuffer(item['raw'], np.uint8).reshape(480, 752)
        measured = measure.process_prepared_final(
            gray, meta, pair=pair, payload=frame.payload, admission=admission,
            mode='measurement')
        previewed = preview.process_prepared_final(
            gray, meta, pair=pair, payload=frame.payload, admission=admission,
            mode='preview')
        out.append(dict(frame=index, measured=measured, previewed=previewed,
                        profile_measure=encode_profile(measured),
                        profile_preview=encode_profile(previewed),
                        payload_sha=_payload_hash(frame)))
    return out


def _cloud(processor):
    arrays = list(processor._cloud)
    if not arrays:
        return np.empty((0, 3), np.float64)
    return np.ascontiguousarray(np.concatenate(arrays), dtype=np.float64)


def compare(serial, paired, serial_processors, paired_processors):
    """Return a JSON-safe strict differential for profiles and both clouds."""
    mismatches = []
    frames = []
    for index, (a, b) in enumerate(zip(serial, paired)):
        row = dict(frame=index)
        for mode, key, profile_key in (('measurement', 'measured', 'profile_measure'),
                                       ('preview', 'previewed', 'profile_preview')):
            checks = _profile_equal(a[key], b[key])
            checks['encoded_profile'] = a[profile_key] == b[profile_key]
            row[mode] = checks
            if not all(checks.values()):
                mismatches.append(dict(frame=index, mode=mode,
                                       fields=[name for name, ok in checks.items() if not ok]))
        frames.append(row)
    clouds = {}
    for label, processors in (('serial', serial_processors), ('paired', paired_processors)):
        measured = _cloud(processors[0])
        previewed = _cloud(processors[1])
        clouds[label] = dict(measurement_points=int(len(measured)),
                             measurement_sha=hashlib.sha256(measured.tobytes()).hexdigest(),
                             measurement_bytes=measured.tobytes(),
                             preview_points=int(len(previewed)),
                             preview_sha=hashlib.sha256(previewed.tobytes()).hexdigest(),
                             preview_bytes=previewed.tobytes())
    cloud_equal = (clouds['serial']['measurement_bytes'] == clouds['paired']['measurement_bytes']
                   and clouds['serial']['preview_bytes'] == clouds['paired']['preview_bytes'])
    if not cloud_equal:
        fields = []
        if clouds['serial']['measurement_bytes'] != clouds['paired']['measurement_bytes']:
            fields.append('measurement_cloud')
        if clouds['serial']['preview_bytes'] != clouds['paired']['preview_bytes']:
            fields.append('preview_cloud')
        mismatches.append(dict(frame=None, mode='cloud', fields=fields))
    public_cloud = {label: {key: value for key, value in row.items() if not key.endswith('_bytes')}
                    for label, row in clouds.items()}
    return dict(all_equal=not mismatches, mismatches=mismatches[:32],
                mismatch_count=len(mismatches), frames=frames, cloud=public_cloud,
                clouds_byte_equal=cloud_equal)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--target', default='T05', choices=('T05', 'T06'))
    parser.add_argument('--limit', type=int, default=64)
    parser.add_argument('--tag', default='x')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'profile')
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 240:
        raise SystemExit('limit must be 1..240')
    run = SavedRun.load(args.run_root, args.target)
    frozen = load_frozen_inputs(ROOT, args.target)
    calibration = ROOT / 'inputs' / args.target / 'calibration.json'
    items = read_run_payloads(run)[:args.limit]
    meta_rows = [_meta(frozen, index, item['record']['capture_epoch']) for index, item in enumerate(items)]
    if len(items) != args.limit:
        raise SystemExit('saved run shorter than requested limit')
    out_dir = args.output_dir / ('equiv_r26_' + args.tag)
    if out_dir.exists():
        raise SystemExit('output exists: ' + str(out_dir))
    out_dir.mkdir(parents=True)

    serial_processors = (PLFinalProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680),
                         PLFinalProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680))
    paired_processors = (PLGeometryPairProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680),
                         PLGeometryPairProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680))
    prepared_processors = (PLGeometryPairProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680),
                           PLGeometryPairProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680))
    serial = _run_serial(items, meta_rows, serial_processors)
    paired = _run_paired(items, meta_rows, paired_processors)
    prepared = _run_prepared_serial(items, meta_rows, prepared_processors)
    paired_diff = compare(serial, paired, serial_processors, paired_processors)
    prepared_diff = compare(serial, prepared, serial_processors, prepared_processors)
    doc = dict(schema='R26_GEOM_PAIR_EQUIV_V2', target=args.target, limit=args.limit,
               run_root=str(args.run_root), evidence='saved actual-PL packet; offline API replay',
               all_equal=bool(paired_diff['all_equal'] and prepared_diff['all_equal']),
               mismatch_count=int(paired_diff['mismatch_count'] + prepared_diff['mismatch_count']),
               paired_thread=paired_diff, prepared_serial=prepared_diff,
               cloud=dict(thread=paired_diff.get('cloud'), prepared_serial=prepared_diff.get('cloud')),
               clouds_byte_equal=bool(paired_diff.get('clouds_byte_equal')
                                      and prepared_diff.get('clouds_byte_equal')))
    (out_dir / 'EQUIV.json').write_text(json.dumps(doc, indent=2) + '\n')
    print(json.dumps({key: doc[key] for key in ('all_equal', 'mismatch_count', 'cloud',
                                                'clouds_byte_equal')}, indent=2))
    return 0 if doc['all_equal'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
