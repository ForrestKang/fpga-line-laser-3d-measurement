"""Stage profile of the offline full flow (same 240 saved packets).

Measures, per frame: raw inflate, strict PFO3 decode, measurement geometry,
preview geometry, MBR encode, writer admission. Reports p50/p95/p99 and totals.
Read-only over the saved run; no hardware.
"""
from __future__ import annotations
import argparse, json, struct, sys, time, zlib
from pathlib import Path
import numpy as np

RUNTIME = Path(__file__).resolve().parents[1]
CAND = RUNTIME.parent
for p in (RUNTIME, RUNTIME / 'structured_light_system_algorithm_v1/src/python',
          RUNTIME / 'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0, str(p))
sys.path.append(str(Path(r'D:/NUECSDC-FPGA/structured_light_system_geometry_v1/.venv/Lib/site-packages')))

from candidate.pfo3_r25 import decode, identity, HEADER_FIELDS  # noqa: E402
from candidate.storage_r21 import encode_profile  # noqa: E402

STAGES = ('inflate', 'decode', 'measure_geometry', 'preview_geometry', 'encode', 'admission')


def pct(values, q):
    return float(np.quantile(np.asarray(values, float), q)) if len(values) else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run-root', type=Path, required=True)
    p.add_argument('--target', required=True, choices=('T05', 'T06'))
    p.add_argument('--limit', type=int, default=240)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--reuse-frame', action='store_true',
                   help='decode once and reuse the immutable frame for both modes')
    p.add_argument('--instrument-fsync', action='store_true',
                   help='time and count every fsynced append in the writer sink')
    p.add_argument('--warmup', action='store_true',
                   help='run one throwaway frame before timing (first-call warmup)')
    a = p.parse_args()

    from tools.offline_scan_r26 import (SavedRun, ReplayAdmissionR26, OfflineBudget, SIM_FINGERPRINT,
                                        load_frozen_inputs, sha256_bytes, _PreviewNoHistory)
    from candidate.pfo3_processor_r25 import PLFinalProcessorR25
    from candidate.ps_pipeline_r22 import CloudSpool
    from candidate.storage_r21 import MultiBranchChunkWriter, encode_profile as encode
    from candidate.io_policy_r25 import DEFAULT_IO_POLICY

    run_ = SavedRun.load(a.run_root, a.target)
    frozen = load_frozen_inputs(CAND, a.target)
    cal_path = CAND / 'inputs' / a.target / 'calibration.json'
    out = Path(a.output)
    out.mkdir(parents=True, exist_ok=False)
    fsync_calls = []
    if a.instrument_fsync:
        import structured_light.scan3d.stream_writer as sw
        original_append = sw._append_fsynced

        def timed_append(path, data):
            t0 = time.perf_counter()
            try:
                return original_append(path, data)
            finally:
                fsync_calls.append((str(Path(path).name), time.perf_counter() - t0))
        sw._append_fsynced = timed_append
    budget = OfflineBudget()
    spool = CloudSpool(out / 'cloud.float64.bin', storage_budget=budget)
    writer = MultiBranchChunkWriter(out / 'records', capacity=DEFAULT_IO_POLICY.capacity,
                                    group_size=DEFAULT_IO_POLICY.group_size,
                                    max_delay_s=DEFAULT_IO_POLICY.max_delay_s,
                                    byte_capacity=DEFAULT_IO_POLICY.byte_capacity,
                                    raw_codec='zlib1', storage_budget=budget)
    measure = PLFinalProcessorR25.from_virtual_config(cal_path, max_cloud_points=a.limit * 7680)
    preview = PLFinalProcessorR25.from_virtual_config(cal_path, max_cloud_points=a.limit * 7680)
    measure._cloud = spool
    preview._cloud = _PreviewNoHistory()

    # read all raw/pfo3/primary bytes first so file IO is outside the loop
    from tools.offline_scan_r26 import read_run_payloads
    items = read_run_payloads(run_)[:a.limit]

    timings = {s: [] for s in STAGES}
    warmup_s = None
    if a.warmup and items:
        # Same idea as a pre-run warmup on the board: pay the first-call cost
        # (allocations, JIT, lazy imports) before the measured window starts.
        t0 = time.perf_counter()
        warm = PLFinalProcessorR25.from_virtual_config(cal_path, max_cloud_points=7680)
        warm._cloud = _PreviewNoHistory()
        warm_p = PLFinalProcessorR25.from_virtual_config(cal_path, max_cloud_points=7680)
        warm_p._cloud = _PreviewNoHistory()
        item = items[0]
        hdr = dict(zip(HEADER_FIELDS, struct.unpack_from('<64I', item['payload'])))
        ident = identity(hdr)
        ad = ReplayAdmissionR26(ident=ident, parameter_fingerprint=SIM_FINGERPRINT,
                                bit_sha256='0' * 64, stream_sha256=sha256_bytes(item['payload']),
                                input_raw_sha256=sha256_bytes(item['raw']),
                                primary_sha256=sha256_bytes(item['primary']), receipt={})
        row = frozen[0]
        meta = dict(row, request_id=0, capture_epoch=item['record']['capture_epoch'],
                    feature_config_revision=1, exposure_lines=60, exposure_revision=0,
                    motor_accepted_id=0, motor_epoch=0, emitted_command_count=0, fv_ticks=0,
                    exposure_start_ticks=0, exposure_end_ticks=0, replay_identity_tags=True,
                    physical_capture_timing_known=False, physical_motor_registration=False,
                    simulated_exposure_lines_assumed=60, geometry_evidence_type='SIMULATION_ONLY')
        g0 = np.frombuffer(item['raw'], np.uint8).reshape(480, 752)
        warm.process_final(g0, meta, payload=item['payload'], admission=ad, mode='measurement')
        warm_p.process_final(g0, meta, payload=item['payload'], admission=ad, mode='preview')
        warmup_s = time.perf_counter() - t0
    raw_fh = (Path(a.run_root) / f'{a.target}_01' / 'frames' / 'payloads.bin').open('rb')
    started = time.perf_counter()
    for index, item in enumerate(items):
        rec = item['record']
        # real board decode_hash cost: inflate the stored bytes + hash the full Raw
        t = time.perf_counter()
        raw_fh.seek(rec['raw_offset'])
        stored = raw_fh.read(rec['raw_stored_bytes'])
        raw = zlib.decompress(stored) if rec['raw_codec'] == 'zlib1' else stored
        sha256_bytes(raw)
        timings['inflate'].append(time.perf_counter() - t)
        t = time.perf_counter()
        hdr = dict(zip(HEADER_FIELDS, struct.unpack_from('<64I', item['payload'])))
        ident = identity(hdr)
        frame = decode(item['payload'], expected_identity=ident)
        timings['decode'].append(time.perf_counter() - t)
        ad = ReplayAdmissionR26(ident=ident, parameter_fingerprint=SIM_FINGERPRINT,
                                bit_sha256='0' * 64, stream_sha256=sha256_bytes(item['payload']),
                                input_raw_sha256=sha256_bytes(item['raw']),
                                primary_sha256=sha256_bytes(item['primary']), receipt={})
        if a.reuse_frame:
            # Same immutable reuse the DMA transport performs: decode once, then
            # both modes consume the identical validated frame.
            ad._validated_frame = frame
        row = frozen[index]
        metadata = dict(row, request_id=index, capture_epoch=rec['capture_epoch'],
                        feature_config_revision=1, exposure_lines=60, exposure_revision=0,
                        motor_accepted_id=0, motor_epoch=0, emitted_command_count=0, fv_ticks=0,
                        exposure_start_ticks=0, exposure_end_ticks=0, replay_identity_tags=True,
                        physical_capture_timing_known=False, physical_motor_registration=False,
                        simulated_exposure_lines_assumed=60, geometry_evidence_type='SIMULATION_ONLY')
        gray = np.frombuffer(item['raw'], np.uint8).reshape(480, 752)
        t = time.perf_counter()
        measured = measure.process_final(gray, metadata, payload=item['payload'], admission=ad,
                                         mode='measurement')
        timings['measure_geometry'].append(time.perf_counter() - t)
        t = time.perf_counter()
        prev = preview.process_final(gray, metadata, payload=item['payload'], admission=ad,
                                     mode='preview')
        timings['preview_geometry'].append(time.perf_counter() - t)
        summary = measured.summary()
        t = time.perf_counter()
        packet = encode(measured, summary=summary)
        timings['encode'].append(time.perf_counter() - t)
        record = dict(request_id=index, capture_epoch=rec['capture_epoch'], raw=item['raw'],
                      raw_sha256=sha256_bytes(item['raw']), profile_payload=packet,
                      source_pfo3_payload=item['payload'], source_pfo3_primary=item['primary'],
                      points_camera_mm=measured.delta_camera_mm, points_axis_mm=measured.delta_axis_mm,
                      summary=summary, motion=dict(source='SIMULATION_ONLY_angle',
                                                   angle_rad=float(row['angle_rad'])),
                      exposure=dict(source='SIMULATED_IMAGE_ASSUMPTION', lines=60, revision=0))
        t = time.perf_counter()
        writer.submit_wait(record, timeout_s=5.)
        timings['admission'].append(time.perf_counter() - t)
    raw_fh.close()
    receipt = writer.close(timeout_s=10.)
    total = time.perf_counter() - started
    spool.export_npy(out / 'cloud.npy')
    cloud = np.load(out / 'cloud.npy')
    spool.close()
    by_file = {}
    for name, dt in fsync_calls:
        by_file.setdefault(name, []).append(dt)
    fsync_doc = {name: dict(count=len(v), total_s=float(sum(v)),
                            p50_ms=1000 * pct(v, .5) or 0.0, p99_ms=1000 * pct(v, .99) or 0.0,
                            max_ms=1000 * max(v)) for name, v in by_file.items()}
    fsync_doc['calls_total'] = len(fsync_calls)
    fsync_doc['calls_per_frame'] = len(fsync_calls) / max(1, len(items))
    writer_accounting = getattr(writer, 'io_accounting', None)
    doc = dict(schema='R26_OFFLINE_PROFILE_V1', target=a.target, frames=len(items),
               writer_accounting=writer_accounting, warmup=bool(a.warmup), warmup_s=warmup_s,
               reuse_frame=bool(a.reuse_frame), total_s=total, points=int(len(cloud)),
               durable=bool(getattr(receipt, 'durable', False)),
               fsync=fsync_doc if a.instrument_fsync else None,
               stages={s: dict(p50_ms=1000 * pct(timings[s], .5) or 0.0,
                               p95_ms=1000 * pct(timings[s], .95) or 0.0,
                               p99_ms=1000 * pct(timings[s], .99) or 0.0,
                               max_ms=1000 * max(timings[s]) if timings[s] else 0.0,
                               total_s=float(sum(timings[s])))
                       for s in STAGES})
    (out / 'PROFILE.json').write_text(json.dumps(doc, indent=2) + '\n')
    print(json.dumps({k: doc[k] for k in ('frames', 'total_s', 'points', 'durable')}))
    if writer_accounting:
        print('writer_accounting', json.dumps(writer_accounting))
    for s in STAGES:
        d = doc['stages'][s]
        print('%-18s p50 %7.3f  p95 %7.3f  p99 %7.3f  max %7.3f  total %6.2f s' %
              (s, d['p50_ms'], d['p95_ms'], d['p99_ms'], d['max_ms'], d['total_s']))


if __name__ == '__main__':
    main()
