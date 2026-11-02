"""Offline formal scanning flow over a saved actual-PL board run.

Only the transport is substituted: saved DDR-replay packets (raw + PFO3 +
primary + trace) are re-admitted by ReplayAdmissionR26, which verifies them
against the frozen run's own recorded hashes. Everything downstream is the
production code path: PLFinalProcessorR25 geometry/quality, MBR persistence,
exposure controller, rescan decision, feature discovery.

No PYNQ import, no camera, no motor, no HDMI device, no FPGA load.
"""
from __future__ import annotations
import argparse, hashlib, json, os, sys, time, zlib
from dataclasses import dataclass
from pathlib import Path
import numpy as np

RUNTIME = Path(__file__).resolve().parents[1]
CAND = RUNTIME.parent
for p in (RUNTIME, RUNTIME / 'structured_light_system_algorithm_v1/src/python',
          RUNTIME / 'structured_light_system_geometry_v1/src/python'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from candidate.pfo3_r25 import decode, identity, PFO3Error, HEADER_FIELDS  # noqa: E402
import struct  # noqa: E402

SIM_FINGERPRINT = 'bec4a5e36ccc36be6da6945979984ff630654b0cf6add3a094e88ee99e72c399'


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def ensure_cv2():
    """structured_light.scan3d imports OpenCV through the turntable module.

    The offline geometry path never calls a checkerboard routine, but the
    package import still needs the module. Record exactly which one was used.
    """
    try:
        import cv2  # noqa: F401
        return 'present'
    except ModuleNotFoundError:
        pass
    for extra in (Path(r'D:/NUECSDC-FPGA/structured_light_system_geometry_v1/.venv/Lib/site-packages'),):
        if (extra / 'cv2').is_dir():
            sys.path.append(str(extra))
            try:
                import cv2  # noqa: F401
                return 'borrowed:' + str(extra)
            except Exception:
                sys.path.remove(str(extra))
    raise SystemExit('OpenCV unavailable; structured_light.scan3d cannot be imported offline')


class OfflineBudget:
    """Bounds offline bytes; it is not board capacity admission."""
    def __init__(self, limit=1 << 40):
        self.limit = limit
        self.used = 0
    def reserve(self, n):
        if self.used + n > self.limit:
            raise BufferError('offline byte bound reached')
        return n
    def commit(self, token, n):
        self.used += n


@dataclass
class SavedRun:
    root: Path
    target: str
    records: list
    trace: list
    manifest: dict

    @classmethod
    def load(cls, root, target):
        root = Path(root)
        base = root / f'{target}_01'
        # A prior RESULT or a list of frame rows does not establish a closed,
        # committed source. Exhaust the strict reader (including final EOF) so
        # failed writers, torn commits and uncommitted tails cannot be replayed
        # as a successful archived run.
        from candidate.storage_r21 import iter_records
        verified_frames=sum(1 for row in iter_records(base/'frames') if row['kind']=='frame')
        records = [json.loads(l) for l in (base / 'frames' / 'frames.jsonl').read_text().splitlines()]
        frames = [r for r in records if r['kind'] == 'frame']
        trace = [json.loads(l) for l in (base / 'frames_trace.jsonl').read_text().splitlines()]
        manifest = json.loads((root / 'DONE.json').read_text())
        if not frames:
            raise SystemExit('no saved frames')
        if verified_frames != len(frames):
            raise ValueError('strict saved frame count mismatch')
        return cls(root=root, target=target, records=frames, trace=trace, manifest=manifest)


class ReplayAdmissionR26:
    """Offline re-admission of an already completed actual-PL packet.

    It is deliberately NOT a PLFrameAdmission: that type requires a live
    transport capability token. This one verifies the saved evidence chain and
    records that no PL execution happened in this process.
    """
    fixture_only = False
    hardware_completed = False
    evidence_class = 'SAVED_ACTUAL_PL_PACKET_OFFLINE_REPLAY'
    _validated_frame = None
    _validated_primary = None
    _pair_seal = None

    @property
    def primary_sha256(self):
        return self._primary_sha256

    def __init__(self, *, ident, parameter_fingerprint, bit_sha256, stream_sha256,
                 input_raw_sha256, primary_sha256, receipt):
        self.identity = ident
        self.parameter_fingerprint = parameter_fingerprint
        self.bit_sha256 = bit_sha256
        self.stream_sha256 = stream_sha256
        self.input_raw_sha256 = input_raw_sha256
        self._primary_sha256 = primary_sha256
        self.receipt = receipt

    def validate(self):
        for value in (self.parameter_fingerprint, self.bit_sha256, self.stream_sha256,
                      self.input_raw_sha256, self._primary_sha256):
            if not isinstance(value, str) or len(value) != 64:
                raise PFO3Error('saved-packet hash missing')

    def persistence_frame(self, record):
        self.validate()
        return None

    def provenance(self):
        return dict(evidence_class=self.evidence_class,
                    PL_execution_in_this_process=False,
                    saved_transport_receipt=self.receipt)


def load_frozen_inputs(cand, target):
    p = cand / 'inputs' / target / 'frames.jsonl'
    rows = [json.loads(l) for l in p.read_text().splitlines()]
    return {r['frame_id']: r for r in rows}


def read_run_payloads(run):
    """Decode raw/PFO3/primary for every saved frame and verify recorded hashes."""
    base = run.root / f'{run.target}_01'
    out = []
    with (base / 'frames' / 'payloads.bin').open('rb') as fh:
        for rec in run.records:
            fh.seek(rec['raw_offset'])
            stored = fh.read(rec['raw_stored_bytes'])
            if rec['raw_codec'] in ('zlib1','zlib'):
                decoder=zlib.decompressobj()
                raw=decoder.decompress(stored,752*480+1)
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise ValueError('invalid bounded saved Raw compression')
            elif rec['raw_codec']=='none':raw=stored
            else:raise ValueError('unsupported saved Raw codec')
            if len(raw) != rec['raw_bytes'] or sha256_bytes(raw) != rec['raw_sha256']:
                raise SystemExit('saved Raw hash mismatch')
            fh.seek(rec['raw_stored_bytes'] and rec['raw_offset'])
            e = rec['source_payloads']['source_pfo3_payload']
            fh.seek(e['offset'])
            payload = fh.read(e['bytes'])
            if sha256_bytes(payload) != e['sha256']:
                raise SystemExit('saved PFO3 payload hash mismatch')
            p = rec['source_payloads']['source_pfo3_primary']
            fh.seek(p['offset'])
            primary = fh.read(p['bytes'])
            if sha256_bytes(primary) != p['sha256']:
                raise SystemExit('saved primary hash mismatch')
            out.append(dict(record=rec, raw=raw, payload=payload, primary=primary))
    return out


def build_app(cand, target, calibration):
    from candidate.pfo3_processor_r25 import PLFinalProcessorR25
    from candidate.ps_pipeline_r22 import CloudSpool
    from candidate.storage_r21 import MultiBranchChunkWriter
    from candidate.io_policy_r25 import DEFAULT_IO_POLICY
    from structured_light.scan3d.continuous_exposure import ExposureController
    from structured_light.scan3d.stream_exposure import ExposureCoordinator
    return (PLFinalProcessorR25, CloudSpool, MultiBranchChunkWriter, DEFAULT_IO_POLICY,
            ExposureController, ExposureCoordinator)


class AppendSpool:
    """Append-only float64 XYZ spool with an fsynced index (preview persistence)."""

    def __init__(self, path):
        self.path = Path(path)
        self.stream = self.path.open('xb')
        self.offset = 0
        self.entries = []

    def append(self, points):
        array = np.ascontiguousarray(points, dtype='<f8')
        data = array.tobytes()
        entry = dict(offset=self.offset, bytes=len(data), count=int(len(array)),
                     sha256=sha256_bytes(data))
        self.stream.write(data)
        self.offset += len(data)
        self.entries.append(entry)
        return entry

    def close(self):
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()


class _PreviewNoHistory:
    def append(self, points):
        if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
            raise ValueError('finite preview XYZ required')


def render_pngs(cloud, measurement_frames, output, angles):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover
        return dict(rendered=False, error=type(exc).__name__ + ': ' + str(exc))
    out = Path(output) / 'display'
    out.mkdir(parents=True, exist_ok=True)
    written = []
    if len(cloud):
        fig = plt.figure(figsize=(12, 4), dpi=110)
        for i, (a, b) in enumerate(((0, 1), (0, 2), (1, 2))):
            ax = fig.add_subplot(1, 3, i + 1)
            ax.scatter(cloud[:, a], cloud[:, b], s=0.15, c=cloud[:, 2] if a != 2 else cloud[:, 0],
                       cmap='viridis', linewidths=0)
            ax.set_aspect('equal'); ax.set_xlabel('XYZ'[a] + ' mm'); ax.set_ylabel('XYZ'[b] + ' mm')
            ax.set_title(f'measurement {len(cloud)} pts')
        fig.tight_layout(); p = out / 'cloud_views.png'; fig.savefig(p); plt.close(fig)
        written.append(p.name)
    for item in measurement_frames[:3]:
        k, gray, centers = item['frame'], item['gray'], item['centers']
        fig = plt.figure(figsize=(6, 4), dpi=110)
        ax = fig.add_subplot(1, 1, 1)
        ax.imshow(gray, cmap='gray', vmin=0, vmax=255)
        if len(centers):
            ax.scatter(centers[:, 0], centers[:, 1], s=1, c='r')
        ax.set_title(f'frame {k} angle {angles[k]:.4f} rad, {len(centers)} accepted centres')
        fig.tight_layout(); p = out / f'frame_{k:03d}_overlay.png'; fig.savefig(p); plt.close(fig)
        written.append(p.name)
    return dict(rendered=True, files=written, backend='matplotlib_offscreen',
                hdmi_device_accessed=False)


def run(args):
    cand = CAND
    run_ = SavedRun.load(args.run_root, args.target)
    frozen = load_frozen_inputs(cand, args.target)
    cal_path = cand / 'inputs' / args.target / 'calibration.json'
    calibration = json.loads(cal_path.read_text())
    if calibration.get('evidence_type') != 'SIMULATION_ONLY':
        raise SystemExit('offline replay requires the frozen SIMULATION_ONLY geometry')

    man = json.loads((cand / 'runtime/r25_v9_prep/manifest.json').read_text())
    bit_sha = man['artifacts']['files']['bit']['sha256'] if 'artifacts' in man else None
    if bit_sha is None:
        for key in ('bit_sha256', 'bit'):
            if key in man:
                bit_sha = man[key] if isinstance(man[key], str) else man[key].get('sha256')
    if not isinstance(bit_sha, str) or len(bit_sha) != 64:
        bit_sha = '1c18690bd1c2ecf5d92caf8e8a3199217593aad8e72b37c7c74b1a43a3deedf8'

    cv2_source = ensure_cv2()
    PLFinalProcessorR25, CloudSpool, MultiBranchChunkWriter, IO, ExposureController, ExposureCoordinator = \
        build_app(cand, args.target, calibration)

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    budget = OfflineBudget(limit=args.byte_budget)
    spool = CloudSpool(out / 'cloud.float64.bin', storage_budget=budget)
    writer = MultiBranchChunkWriter(out / 'records', capacity=IO.capacity, group_size=IO.group_size,
                                    max_delay_s=IO.max_delay_s, byte_capacity=IO.byte_capacity,
                                    raw_codec='zlib1', storage_budget=budget)
    preview_spool = None if args.no_preview_persist else AppendSpool(out / 'preview.float64.bin')
    measure = PLFinalProcessorR25.from_virtual_config(cal_path, max_cloud_points=max(1, args.limit) * 7680)
    preview = PLFinalProcessorR25.from_virtual_config(cal_path, max_cloud_points=max(1, args.limit) * 7680)
    measure._cloud = spool
    preview._cloud = _PreviewNoHistory()

    ctrl = ExposureController(line_values=(30, 60, 120), initial_lines=60, hysteresis_frames=3)
    angles = []
    frames_out = []
    pending = read_run_payloads(run_)[:args.limit]
    gate = (args.gate_sha256 or '').lower()
    if gate:
        want = sha256_bytes(cal_path.read_bytes())
        if want != gate:
            raise SystemExit('geometry config changed: ' + want)

    started = time.perf_counter()
    exposure_events = []
    preview_index = []
    effective_after = None
    per_frame = []
    for index, item in enumerate(pending):
        rec = item['record']
        raw = item['raw']
        pfo3 = item['payload']
        primary = item['primary']
        hdr = dict(zip(HEADER_FIELDS, struct.unpack_from('<64I', pfo3)))
        ident = identity(hdr)
        epoch = rec['capture_epoch']
        expect = dict(frame_id=index, epoch=epoch, cfg_version=1, exposure_version=0, motor_id=0,
                      motor_epoch=0, motor_steps=0, FV_ticks=0, exposure_start=0, exposure_end=0, source=2)
        if ident != expect:
            raise SystemExit(f'saved identity differs at frame {index}: {ident}')
        if rec['request_id'] != index:
            raise SystemExit('saved record identity mismatch')
        frozen_row = frozen.get(index)
        if frozen_row is None or frozen_row['raw_sha256'] != rec['raw_sha256']:
            raise SystemExit('saved raw differs from frozen input manifest')
        trace_row = run_.trace[index]
        receipt = trace_row.get('transport_receipt') or {}
        ad = ReplayAdmissionR26(ident=ident, parameter_fingerprint=SIM_FINGERPRINT, bit_sha256=bit_sha,
                                stream_sha256=sha256_bytes(pfo3), input_raw_sha256=sha256_bytes(raw),
                                primary_sha256=sha256_bytes(primary), receipt=receipt)
        ad.validate()
        metadata = dict(frozen_row, request_id=index, capture_epoch=epoch, feature_config_revision=1,
                        exposure_lines=60, exposure_revision=0, motor_accepted_id=0, motor_epoch=0,
                        emitted_command_count=0, fv_ticks=0, exposure_start_ticks=0, exposure_end_ticks=0,
                        replay_identity_tags=True, physical_capture_timing_known=False,
                        physical_motor_registration=False, simulated_exposure_lines_assumed=60,
                        geometry_evidence_type='SIMULATION_ONLY')
        gray = np.frombuffer(raw, np.uint8).reshape(480, 752)
        t0 = time.perf_counter()
        measured = measure.process_final(gray, metadata, payload=pfo3, admission=ad, mode='measurement')
        t1 = time.perf_counter()
        prev = preview.process_final(gray, metadata, payload=pfo3, admission=ad, mode='preview')
        t2 = time.perf_counter()
        # processing_backend stays PL_FINAL_V3: these observations really were
        # produced by the PL final frontend. The replay flags state that the
        # execution happened in the recorded board run, not in this process.
        measured.frame_metadata.update(
            offline_replay=True, PL_execution_in_this_process=False,
            saved_run=str(args.run_root), saved_transport_receipt=receipt,
            evidence_class=ReplayAdmissionR26.evidence_class)
        summary = measured.summary()
        usable = bool(measured.simulation_geometry_valid and int(measured.valid_points.sum()) > 0)
        summary.update(source='offline_saved_pl_packet', angle_rad=float(frozen_row['angle_rad']),
                       measurement_frame_usable=measured.simulation_geometry_valid,
                       preview_frame_usable=True, physical_accuracy_verified=False,
                       PL_packet_origin='ACTUAL_PL_BOARD_RUN', PL_execution_this_process=False)
        packet = writer_encode(measured, summary)
        record = dict(request_id=index, capture_epoch=epoch, raw=raw, raw_sha256=sha256_bytes(raw),
                      profile_payload=packet, source_pfo3_payload=pfo3, source_pfo3_primary=primary,
                      points_camera_mm=measured.delta_camera_mm,
                      points_axis_mm=measured.delta_axis_mm,
                      summary=summary,
                      motion=dict(source='SIMULATION_ONLY_angle',
                                  angle_rad=float(frozen_row['angle_rad']), motor_command=False),
                      exposure=dict(source='SIMULATED_IMAGE_ASSUMPTION', lines=ctrl.lines,
                                    revision=ctrl.revision, effect_on_image='NOT_SIMULATED'))
        t3 = time.perf_counter()
        writer.submit_wait(record, timeout_s=5.)
        t4 = time.perf_counter()
        quality_status = measured.quality.status
        op = ctrl.propose(quality_status)
        if op is not None:
            ctrl.acknowledge(op.revision, accepted=True, effective_after_request_id=index)
            effective_after = index
            exposure_events.append(dict(revision=op.revision, lines=op.lines,
                                        effective_after_request_id=index, trigger=quality_status))
            writer.submit_event(dict(event='exposure_simulated', revision=op.revision,
                                     lines=op.lines, effective_after_request_id=index,
                                     effect_on_image='NOT_SIMULATED'))
        if preview_spool is not None:
            entry = preview_spool.append(prev.delta_axis_mm)
            preview_index.append(dict(frame=index, frame_id=index,
                                      angle_rad=float(frozen_row['angle_rad']), **entry))
        angles.append(float(frozen_row['angle_rad']))
        per_frame.append(dict(frame=index, angle=float(frozen_row['angle_rad']),
                              valid_points=int(measured.valid_points.sum()),
                              preview_points=len(prev.delta_axis_mm),
                              quality=quality_status, usable=usable,
                              exposure_revision=ctrl.revision,
                              geometry_wall_s=t1 - t0, preview_wall_s=t2 - t1,
                              persist_wall_s=t4 - t3))
        if index < 3:
            accepted = measured.profile('measurement', qualified_only=True) if measured.profile_valid else None
            centers = np.empty((0, 2))
            if accepted is not None and len(accepted.rows):
                centers = np.column_stack((accepted.center_q12 / 4096.0, accepted.rows))
            frames_out.append(dict(frame=index, gray=gray, centers=centers))
    close_receipt = writer.close(timeout_s=10.)
    if preview_spool is not None:
        preview_spool.close()
        with (out / 'preview_index.jsonl').open('x', encoding='utf-8') as f:
            for row in preview_index:
                f.write(json.dumps(row, sort_keys=True) + '\n')
            f.flush(); os.fsync(f.fileno())
    elapsed = time.perf_counter() - started
    spool.export_npy(out / 'cloud.npy')
    cloud = np.load(out / 'cloud.npy')
    spool.close()

    from virtual_scan.feature_task_r26 import discover_features_bounded
    discovery_started = time.perf_counter()
    features, feature_audit = discover_features_bounded(cloud, calibration_id=calibration['calibration_id'],
                                 scan_angles_rad=angles, policy=None,
                                 version=getattr(args,'feature_policy','v4'),
                                 workers=getattr(args,'feature_workers',2), deadline_s=120.)
    discovery_s = time.perf_counter() - discovery_started

    display = render_pngs(cloud, frames_out, out, angles)
    quality_counts = {}
    for f in per_frame:
        quality_counts[f['quality']] = quality_counts.get(f['quality'], 0) + 1
    low = [f for f in per_frame if f['quality'] != 'good' or f['valid_points'] < args.min_points]
    rescan_bins = sorted({int(round(f['angle'] * 180 / np.pi / args.rescan_bin_deg) * args.rescan_bin_deg)
                          for f in low})
    rescan = dict(bin_deg=args.rescan_bin_deg, bins=rescan_bins, actions=len(rescan_bins),
                  budget_turns=1, executed=False,
                  reason='no sector below the quality threshold' if not rescan_bins
                  else 'bounded second turn')
    if rescan_bins and args.rescan_simulate:
        # The frozen source can never add observations. The second turn is run
        # anyway so budget, state transitions and non-duplication are exercised;
        # its points are reported separately and never merged into measurement.
        second = PLFinalProcessorR25.from_virtual_config(cal_path, max_cloud_points=args.limit * 7680)
        second._cloud = _PreviewNoHistory()
        chosen = [f for f in per_frame
                  if int(round(f['angle'] * 180 / np.pi / args.rescan_bin_deg) * args.rescan_bin_deg) in rescan_bins]
        observations = 0
        for f in chosen:
            item = pending[f['frame']]
            hdr = dict(zip(HEADER_FIELDS, struct.unpack_from('<64I', item['payload'])))
            ad2 = ReplayAdmissionR26(ident=identity(hdr), parameter_fingerprint=SIM_FINGERPRINT,
                                     bit_sha256=bit_sha, stream_sha256=sha256_bytes(item['payload']),
                                     input_raw_sha256=sha256_bytes(item['raw']),
                                     primary_sha256=sha256_bytes(item['primary']), receipt={})
            meta2 = dict(frozen[f['frame']], request_id=f['frame'],
                         capture_epoch=item['record']['capture_epoch'], feature_config_revision=1,
                         exposure_lines=ctrl.lines, exposure_revision=ctrl.revision,
                         motor_accepted_id=0, motor_epoch=0, emitted_command_count=0, fv_ticks=0,
                         exposure_start_ticks=0, exposure_end_ticks=0, replay_identity_tags=True,
                         physical_capture_timing_known=False, physical_motor_registration=False,
                         simulated_exposure_lines_assumed=60, geometry_evidence_type='SIMULATION_ONLY')
            gray2 = np.frombuffer(item['raw'], np.uint8).reshape(480, 752)
            res2 = second.process_final(gray2, meta2, payload=item['payload'], admission=ad2, mode='preview')
            observations += len(res2.delta_axis_mm)
        rescan.update(executed=True, mode='SIMULATED_FROZEN_SOURCE', frames=len(chosen),
                      observations=observations, merged_into_measurement=False,
                      note='same frozen packets; proves budget/state machine only, adds no data')
    report = dict(
        schema='R26_OFFLINE_FULL_FLOW_V1', target=args.target, frames=len(per_frame),
        resolved_config=dict(source='offline_saved_packet',limit=args.limit,repeats=1,
            geometry_backend='separate',raw_codec='zlib1',hdmi=False,
            feature_policy=getattr(args,'feature_policy','v4'),
            feature_workers_requested=getattr(args,'feature_workers',2)),
        source=dict(run_root=str(args.run_root), bit_sha256=bit_sha,
                    calibration_id=calibration['calibration_id'],
                    calibration_sha256=sha256_bytes(cal_path.read_bytes()),
                    geometry_evidence_type='SIMULATION_ONLY',
                    packet_origin='ACTUAL_PL_BOARD_RUN', PL_execution_this_process=False),
        measurement=dict(points=int(len(cloud)), valid_frames=sum(1 for f in per_frame if f['usable']),
                         empty_frames=sum(1 for f in per_frame if not f['usable']),
                         writer=dict(frames=close_receipt.written if hasattr(close_receipt, 'written') else None,
                                     durable=bool(getattr(close_receipt, 'durable', False))),
                         writer_accounting=getattr(writer, 'io_accounting', None)),
        quality=dict(counts=quality_counts, low_quality_frames=len(low),
                     min_points_per_frame=args.min_points),
        exposure=dict(final_lines=ctrl.lines, final_revision=ctrl.revision,
                      history=ctrl.history, events=exposure_events,
                      effect_on_image='NOT_SIMULATED_saved_frames_fixed'),
        rescan=rescan,
        preview=dict(persisted=preview_spool is not None,
                     points=int(sum(r['count'] for r in preview_index)),
                     frames=len(preview_index),
                     bytes=int(preview_spool.offset) if preview_spool is not None else 0,
                     index='preview_index.jsonl', spool='preview.float64.bin',
                     note='preview derived XYZ persisted separately; never merged into measurement'),
        features=features, feature_discovery_s=discovery_s,
        display=display,
        timing=dict(total_s=elapsed,
                    geometry_p50=float(np.median([f['geometry_wall_s'] for f in per_frame])),
                    persist_p50=float(np.median([f['persist_wall_s'] for f in per_frame]))),
        physical_accuracy_verified=False, feature_discovery_included=True,
        hdmi_device_accessed=False, camera_commands=0, motor_commands=0, FPGA_loads=0,
        cv2_source=cv2_source)
    write_new(out / 'RESULT.json', report)
    write_new(out / 'features.json', dict(features=features,
                                          calibration_id=calibration['calibration_id']))
    return report


def writer_encode(measured, summary):
    from candidate.storage_r21 import encode_profile
    return encode_profile(measured, summary=summary)


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, indent=2, allow_nan=False, default=str)
        f.flush(); os.fsync(f.fileno())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-root', type=Path, required=True)
    p.add_argument('--target', choices=('T05', 'T06'), required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--limit', type=int, default=24)
    p.add_argument('--feature-policy',choices=('v4','v5','v6'),default='v6')
    p.add_argument('--feature-workers',type=int,choices=(1,2),default=2)
    p.add_argument('--min-points', type=int, default=50)
    p.add_argument('--rescan-bin-deg', type=int, default=5)
    p.add_argument('--rescan-simulate', action=argparse.BooleanOptionalAction, default=True)
    p.add_argument('--no-preview-persist', action='store_true', default=False)
    p.add_argument('--byte-budget', type=int, default=1 << 40,
                   help='offline byte budget; a small value injects storage exhaustion')
    p.add_argument('--gate-sha256')
    a = p.parse_args(argv)
    try:
        r = run(a)
    except BaseException as exc:
        # Bounded stop: preserve partial evidence and never retry automatically.
        try:
            Path(a.output).mkdir(parents=True, exist_ok=True)
            write_new(Path(a.output) / 'ERROR.json',
                      dict(schema='R26_OFFLINE_FULL_FLOW_ERROR_V1',
                           error=type(exc).__name__ + ': ' + str(exc),
                           partial_evidence_preserved=True, automatic_retry=False,
                           hardware_accessed=False))
        except BaseException:
            pass
        raise
    print(json.dumps({k: v for k, v in r.items() if k not in ('features', 'exposure', 'display')},
                     default=str)[:2000])


if __name__ == '__main__':
    main()
