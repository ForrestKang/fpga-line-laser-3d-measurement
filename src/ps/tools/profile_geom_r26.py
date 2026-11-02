"""Offline hierarchical profile of the saved-PL geometry path.

Runs the same saved actual-PL frames through the production processor API with
the hierarchical recorder either disabled or enabled. It supports the separate
(two geometry calls) and paired (one union geometry, optional parallel modes)
backends and writes a JSON report with per-stage exclusive times.

Usage:
    python runtime/tools/profile_geom_r26.py --limit 64 --backend paired
    python runtime/tools/profile_geom_r26.py --limit 64 --backend separate --timing both
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import struct
import sys
import time
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

from tools.offline_scan_r26 import (SavedRun, ReplayAdmissionR26, OfflineBudget, SIM_FINGERPRINT,
                                    load_frozen_inputs, sha256_bytes, _PreviewNoHistory, read_run_payloads)
from tools.stage_profile_r26 import install
from candidate.pfo3_processor_r25 import PLFinalProcessorR25
from candidate.pfo3_r25 import identity, HEADER_FIELDS, decode
from candidate.ps_pipeline_r22 import CloudSpool
from candidate.pfo3_geometry_pair_r25 import (PLGeometryPairProcessorR25,
                                              PreparedGeometryPairR25, PairedModeExecutor)

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


def run_once(args, out_dir, timed, backend):
    run_ = SavedRun.load(args.run_root, args.target)
    frozen = load_frozen_inputs(ROOT, args.target)
    calibration = ROOT / 'inputs' / args.target / 'calibration.json'
    items = read_run_payloads(run_)[:args.limit]
    if len(items) != args.limit:
        raise SystemExit('saved run shorter than requested limit')
    out_dir.mkdir(parents=True, exist_ok=False)
    spool = CloudSpool(out_dir / 'cloud.bin', storage_budget=OfflineBudget())
    if backend == 'paired':
        measure = PLGeometryPairProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680)
        preview = PLGeometryPairProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680)
    else:
        measure = PLFinalProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680)
        preview = PLFinalProcessorR25.from_virtual_config(calibration, max_cloud_points=args.limit * 7680)
    measure._cloud = spool
    preview._cloud = _PreviewNoHistory()
    rec, restore = install()
    rec.enabled = timed
    totals = {'measure': [], 'preview': [], 'pair_both': [], 'pair_build': []}
    executor = PairedModeExecutor() if backend == 'paired' else None
    try:
        for index, item in enumerate(items):
            meta = _meta(frozen, index, item['record']['capture_epoch'])
            frame, admission = _decode(item)
            gray = np.frombuffer(item['raw'], np.uint8).reshape(480, 752)
            if backend == 'paired':
                started = time.perf_counter()
                pair = PreparedGeometryPairR25(frame, measure.calibration)
                totals['pair_build'].append(time.perf_counter() - started)
                _, _, mode_timings = executor.process_timed(measure, preview, gray, meta, pair=pair,
                                                             payload=frame.payload, admission=admission)
                totals['measure'].append(mode_timings['measurement']['wall_s'])
                totals['preview'].append(mode_timings['preview']['wall_s'])
                totals['pair_both'].append(mode_timings['overlap_wall_s'])
            else:
                started = time.perf_counter()
                measure.process_final(gray, meta, payload=frame.payload, admission=admission,
                                      mode='measurement')
                totals['measure'].append(time.perf_counter() - started)
                started = time.perf_counter()
                preview.process_final(gray, meta, payload=frame.payload, admission=admission,
                                      mode='preview')
                totals['preview'].append(time.perf_counter() - started)
    finally:
        if executor is not None:
            executor.close()
        restore()
        spool.close()
    report = rec.report()
    summary = {}
    for name, values in totals.items():
        if values:
            summary[name] = dict(n=len(values), wall_sum=float(sum(values)),
                                 per_frame_ms=1000 * sum(values) / len(values))
    process_calls = [value[0] for value in rec.data.get('process_final', [])]
    return dict(frames=len(items), timed=timed, backend=backend, totals=summary,
                process_final_calls=len(process_calls),
                process_final_ms=(1000 * sum(process_calls) / len(process_calls) if process_calls else None),
                cloud_count=int(spool.count),
                cloud_last_write=dict(getattr(spool, 'last_append_timing', {})),
                stages=report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=64)
    parser.add_argument('--target', default='T05', choices=('T05', 'T06'))
    parser.add_argument('--run-root', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--backend', choices=('separate', 'paired'), default='separate')
    parser.add_argument('--timing', choices=('on', 'off', 'both'), default='both')
    parser.add_argument('--tag', default=None)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'profile')
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 240:
        raise SystemExit('limit must be 1..240')
    stamp = args.tag or time.strftime('%Y%m%d_%H%M%S') + '_' + args.backend
    base = args.output_dir / ('geom_r26_' + stamp)
    if base.exists():
        raise SystemExit('output exists: ' + str(base))
    runs = {}
    if args.timing in ('off', 'both'):
        runs['timing_off'] = run_once(args, base / 'timing_off', False, args.backend)
    if args.timing in ('on', 'both'):
        runs['timing_on'] = run_once(args, base / 'timing_on', True, args.backend)
    def outer_ms(run):
        totals = run['totals']
        if 'pair_both' in totals:
            return totals['pair_both']['per_frame_ms']
        return totals.get('measure', {}).get('per_frame_ms', 0.) + totals.get('preview', {}).get('per_frame_ms', 0.)
    doc = dict(schema='R26_GEOM_HIERARCHICAL_V2', target=args.target, limit=args.limit,
               backend=args.backend, **runs)
    if 'timing_off' in runs and 'timing_on' in runs:
        off = outer_ms(runs['timing_off']); on = outer_ms(runs['timing_on'])
        doc['timing_overhead_pct'] = None if not off or not on else 100 * (on / off - 1)
    (base / 'PROFILE.json').write_text(json.dumps(doc, indent=2) + '\n')
    print('dir:', base)
    for name, run in runs.items():
        value = run['process_final_ms']
        print(name, 'process_final_ms=%s' % ('unavailable' if value is None else '%.3f' % value),
              'outer_ms=%.3f' % outer_ms(run), run['totals'])
        for stage, row in run['stages'].items():
            print('  %-24s n=%5d wall=%10.2f exclusive=%10.2f' %
                  (stage, row['n'], row['wall_sum'], row['exclusive_wall_sum']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
