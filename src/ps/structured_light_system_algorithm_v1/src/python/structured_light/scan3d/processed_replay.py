"""Saved real Raw -> modeled SLC1 -> production processor replay inputs.

The compact model runs only during preparation. Its bytes are software model
output, never captured RTL/DMA evidence. Historical rotating-preview frames
have no CSM1 metadata, calibrated axis or validated exposure effects. Their
time-estimated angles belong to an explicitly assumed display preview only.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from ..gray_reference import reference_rows
from ..p2.types import DiagnosticsSnapshot
from ..stripe_v1.types import StripeConfig
from .pipeline import FRAME_HEIGHT, FRAME_WIDTH
from .stream_capture import FeatureCapture
from .stream_compact import decode_slc1


def _median2(values):
    ordered = sorted(values)
    return ordered[(len(ordered)-1)//2] + ordered[len(ordered)//2] if ordered else 0


def model_slc1(raw: bytes, stripe_config: StripeConfig, *, request_id: int,
               capture_epoch: int, width: int = FRAME_WIDTH,
               height: int = FRAME_HEIGHT) -> bytes:
    """Model published SLC1 row arithmetic from Raw, with no hardware access.

    Legacy row statistics come from the production integer Golden Model.
    PSF1 preclassification and NLQ1 histograms follow the retained R5 compact
    oracle; software selection still verifies candidates against these Raw
    bytes. The result is decoded before returning to reject unsupported wire
    limits instead of truncating values into a seemingly valid packet.
    """
    stripe_config.__post_init__()
    if stripe_config.threshold_mode != 'fixed':
        raise ValueError('modeled SLC1 requires fixed threshold configuration')
    if (type(request_id) is not int or not 0 <= request_id < 1 << 32
            or type(capture_epoch) is not int or not 0 <= capture_epoch < 1 << 16):
        raise ValueError('modeled SLC1 requires uint32 request and uint16 epoch')
    if (type(width) is not int or type(height) is not int
            or not 1 <= width <= FRAME_WIDTH or not 1 <= height <= FRAME_HEIGHT
            or not isinstance(raw, bytes) or len(raw) != width * height):
        raise ValueError('modeled SLC1 requires complete immutable gray8 bytes')
    x0, y0, x1, y1 = stripe_config.roi or (0, 0, width, height)
    if (x1 > width or y1 > height or stripe_config.max_width > 1023
            or stripe_config.min_sum >= 1 << 18):
        raise ValueError('modeled SLC1 configuration exceeds wire limits')
    reference = reference_rows(raw, width=width, height=height,
        threshold=stripe_config.fixed_threshold, roi=(x0, y0, x1, y1),
        min_width=stripe_config.min_width, max_width=stripe_config.max_width,
        min_sum=stripe_config.min_sum, min_peak=stripe_config.min_peak)
    gray = np.frombuffer(raw, np.uint8).reshape(height, width)
    vectors = []
    for y in range(y0, y1):
        row = reference[y]
        first, last = row.support_start or 0, row.support_end or 0
        saturated = bool(row.candidate_count and np.any(gray[y, x0:x1] == 255))
        fault = bool(row.flags & 0x30)
        fallback = row.candidate_count > 1 or fault
        center = ((row.s1 << 12)//row.s0
                  if row.s0 and row.candidate_count == 1 and not fault else 0)
        flags = 0
        if row.candidate_count == 1 and not fault:
            flags = (int(row.width < stripe_config.min_width or row.width > stripe_config.max_width)
                | (int(row.s0 < max(1, stripe_config.min_sum)
                       or row.peak < max(1, stripe_config.min_peak)) << 1)
                | (int(saturated) << 2) | (int(first == x0 or last == x1-1) << 3)
                | (int(row.s0 > (1 << 18)-1 or row.s1 > (1 << 27)-1
                       or center > (1 << 22)-1) << 4))
        vectors.append([center | flags << 22 | int(row.valid) << 27
                        | int(fallback) << 28 | int(fault) << 29,
                        first | last << 10 | row.candidate_count << 20,
                        row.width | int(gray[y, x0:x1].max()) << 10
                        | row.peak << 18 | int(saturated) << 26])
    roi = gray[y0:y1, x0:x1]
    legacy = reference[y0:y1]
    valid = [row for row in legacy if row.valid]
    saturated_pixels = int(np.count_nonzero(roi >= 250))
    saturated_rows = int(np.count_nonzero(np.any(roi >= 250, axis=1)))
    multiple = sum(row.candidate_count > 1 for row in legacy)
    raw_median = _median2([int(row.max()) for row in roi])
    peak_median = _median2([row.peak for row in valid])
    width_median = _median2([row.width for row in valid])
    max_width = max((row.width for row in valid), default=0)
    n = y1-y0
    status = (1 if saturated_pixels*100 > roi.size or saturated_rows*20 > n else
        2 if multiple*20 > n or max_width > 32 else 3 if not valid and raw_median < 120 else
        4 if len(valid)*5 < n*4 else 3 if peak_median < 120 else 0)
    primary = sum((row[0] & (31 << 22)) == 0 and not row[0] & (1 << 28)
                  and (row[1] >> 20) & 1023 == 1 for row in vectors)
    header = [0x534c4331, 0x10000, 32+3*n, request_id, capture_epoch, 0, 0, 0,
        len(valid), 0, n, width, height, stripe_config.fixed_threshold,
        x0 | x1 << 10, y0 | y1 << 10,
        stripe_config.min_width | stripe_config.max_width << 10,
        stripe_config.min_sum, stripe_config.min_peak | 255 << 8, 0x4e4c5131,
        n, saturated_pixels, saturated_rows, multiple, raw_median, peak_median,
        width_median, max_width, status, primary, 0x50534631, 0]
    payload = np.asarray(header + [word for row in vectors for word in row], dtype='<u4').tobytes()
    return decode_slc1(payload, frame_width=width, frame_height=height,
        request_id=request_id, capture_epoch=capture_epoch).payload


@dataclass(frozen=True)
class ReplayEntry:
    raw: bytes
    payload: bytes
    request_id: int
    epoch: int
    raw_sha256: str
    source_profile_sha256: str | None
    source: str
    source_metadata_json: str
    stripe_config: StripeConfig
    angle_rad: float
    angle_source: str = 'capture_time_estimate'

    @property
    def provenance(self):
        return dict(source='saved_real_raw_modeled_slc1', raw_source=self.source,
            raw_sha256=self.raw_sha256, modeled_slc1_sha256=hashlib.sha256(self.payload).hexdigest(),
            source_profile_sha256=self.source_profile_sha256,
            source_profile_bytes_available=False, modeled_slc1=True, rtl_dma_captured=False,
            request_id=self.request_id, capture_epoch=self.epoch,
            stripe_config_fingerprint=self.stripe_config.fingerprint,
            angle_rad=self.angle_rad, angle_source=self.angle_source,
            axis_validated=False, timing_validated=False, effect_known=False,
            measurement_eligible=False, observation_only=True,
            physical_accuracy_verified=False,
            source_metadata=json.loads(self.source_metadata_json))

    def capture(self) -> FeatureCapture:
        """Decode cached bytes; derived diagnostics are not hardware readback.

        No flags-only dictionary is passed as CSM1: historical frames have no
        such metadata. Call process with feature_config_fingerprint explicitly.
        Observation eligibility stays false in provenance, regardless of a
        static profile's per-section geometric ``valid`` result.
        """
        packet = decode_slc1(self.payload, frame_width=FRAME_WIDTH, frame_height=FRAME_HEIGHT,
            request_id=self.request_id, capture_epoch=self.epoch)
        values = {field.name: 0 for field in fields(DiagnosticsSnapshot) if field.name != 'rows'}
        values.update(width=FRAME_WIDTH, height=FRAME_HEIGHT, epoch=self.epoch,
            request_id=self.request_id, row_count=FRAME_HEIGHT, ready=True)
        return FeatureCapture(self.request_id, self.epoch, self.raw, self.raw_sha256,
            None, DiagnosticsSnapshot(**values), None, profile_payload=self.payload,
            compact_packet=packet)


@dataclass(frozen=True)
class PreparedReplay:
    entries: tuple[ReplayEntry, ...]
    stripe_config: StripeConfig
    preparation_s: float
    source_index: str
    source_index_sha256: str
    steps_per_rev: int | None = None
    motor_worker_s: float | None = None

    @property
    def provenance(self):
        sources = sorted({entry.angle_source for entry in self.entries})
        return dict(source='saved_real_raw_modeled_slc1', frames=len(self.entries),
            source_index=self.source_index, source_index_sha256=self.source_index_sha256,
            preparation_s=self.preparation_s, preparation_excluded_from_steady_state_fps=True,
            steps_per_rev=self.steps_per_rev, motor_worker_s=self.motor_worker_s,
            angle_source=sources[0] if len(sources) == 1 else 'mixed_unvalidated',
            angle_sources=sources, axis_validated=False,
            timing_validated=False, effect_known=False,
            measurement_eligible=False, observation_only=True,
            physical_accuracy_verified=False, modeled_slc1=True, rtl_dma_captured=False)


def prepare_rotating_replay(case_dir, stripe_config: StripeConfig, *, steps_per_rev=3200,
                            motor_worker_s=12.137351508001302) -> PreparedReplay:
    """Hash-check historical real frames and prepare one modeled compact cache.

    Angles retain the legacy rounded capture-time estimate. Neither that
    estimate nor repeated playback creates new frame IDs or motion evidence.
    Construct a fresh static processor for each full replay of these entries.
    """
    started = time.perf_counter()
    if (type(steps_per_rev) is not int or steps_per_rev <= 0
            or type(motor_worker_s) not in (int, float)
            or not math.isfinite(motor_worker_s) or motor_worker_s <= 0):
        raise ValueError('finite positive replay duration and pulse scale required')
    case_dir = Path(case_dir).resolve()
    index = case_dir/'evidence_02/frames.jsonl'
    index_bytes = index.read_bytes()
    records = [json.loads(line) for line in index_bytes.splitlines() if line.strip()]
    rotating = [row for row in records if str(row.get('stage', '')).startswith('ROTATING')]
    if not rotating:
        raise ValueError('no rotating-stage frames in saved index')
    start = float(rotating[0]['capture_started_s'])
    entries = []
    previous_id = previous_started = None
    epoch = None
    for record in rotating:
        request, current_epoch = record['request_id'], record['epoch']
        begin, end = float(record['capture_started_s']), float(record['capture_completed_s'])
        if (not math.isfinite(begin) or not math.isfinite(end) or end < begin
                or previous_started is not None and begin <= previous_started):
            raise ValueError('saved capture times must be finite and advance')
        if previous_id is not None and request <= previous_id:
            raise ValueError('saved request IDs must advance without rewriting')
        if epoch is not None and epoch != current_epoch:
            raise ValueError('saved capture epoch changed')
        frame_index = record['index']
        if type(frame_index) is not int or frame_index < 0:
            raise ValueError('invalid saved frame index')
        source = case_dir/'evidence_02/frames'/f'frame_{frame_index:04d}.gray'
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != record['raw_sha256']:
            raise ValueError('saved Raw SHA-256 mismatch: ' + str(source))
        payload = model_slc1(raw, stripe_config, request_id=request, capture_epoch=current_epoch)
        steps = round(steps_per_rev*((begin+end)/2-start)/motor_worker_s)
        entries.append(ReplayEntry(raw, payload, request, current_epoch, digest,
            record.get('profile_sha256'), str(source),
            json.dumps(record, sort_keys=True, allow_nan=False), stripe_config,
            steps/steps_per_rev*math.tau))
        previous_id, previous_started, epoch = request, begin, current_epoch
    return PreparedReplay(tuple(entries), stripe_config, time.perf_counter()-started,
        str(index), hashlib.sha256(index_bytes).hexdigest(), steps_per_rev, float(motor_worker_s))


def prepare_manifest_replay(manifest_path, stripe_config: StripeConfig) -> PreparedReplay:
    """Prepare recorded real Raw paths and declared, unvalidated display angles.

    The JSON is a record list, or an object with a ``records`` list. Every
    record requires raw_file, raw_sha256, request_id, capture_epoch, angle_rad,
    angle_source and source. Only raw_file is read, relative to and confined
    within the manifest directory. ``source`` is retained as provenance text;
    it never grants filesystem access. Original exposure flags stay in the
    source metadata while this replay's measurement eligibility stays false.
    """
    started = time.perf_counter()
    manifest_path = Path(manifest_path).resolve()
    data = manifest_path.read_bytes()
    payload = json.loads(data)
    records = payload.get('records') if isinstance(payload, dict) else payload
    if not isinstance(records, list) or not records:
        raise ValueError('saved replay manifest requires a nonempty record list')
    entries = []
    previous_id = epoch = None
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('saved replay record must be an object')
        request, current_epoch = record['request_id'], record['capture_epoch']
        if (type(request) is not int or not 0 <= request < 1 << 32
                or type(current_epoch) is not int or not 0 <= current_epoch < 1 << 16):
            raise ValueError('saved replay requires uint32 request and uint16 epoch')
        if previous_id is not None and request <= previous_id:
            raise ValueError('saved request IDs must advance without rewriting')
        if epoch is not None and epoch != current_epoch:
            raise ValueError('saved capture epoch changed')
        angle = record['angle_rad']
        if type(angle) not in (int, float) or not math.isfinite(angle):
            raise ValueError('saved replay angle must be finite')
        if any(not isinstance(record[key], str) or not record[key].strip()
               for key in ('angle_source', 'source', 'raw_file')):
            raise ValueError('saved replay source, angle source and raw file are required')
        relative = Path(record['raw_file'])
        source_path = (manifest_path.parent/relative).resolve()
        if relative.is_absolute() or not source_path.is_relative_to(manifest_path.parent):
            raise ValueError('saved Raw path must stay inside manifest directory')
        raw = source_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != record['raw_sha256']:
            raise ValueError('saved Raw SHA-256 mismatch: ' + str(source_path))
        wire = model_slc1(raw, stripe_config, request_id=request, capture_epoch=current_epoch)
        entries.append(ReplayEntry(raw, wire, request, current_epoch, digest,
            record.get('profile_sha256'), record['source'],
            json.dumps(record, sort_keys=True, allow_nan=False), stripe_config,
            float(angle), record['angle_source']))
        previous_id, epoch = request, current_epoch
    return PreparedReplay(tuple(entries), stripe_config, time.perf_counter()-started,
        str(manifest_path), hashlib.sha256(data).hexdigest())


__all__ = ['ReplayEntry', 'PreparedReplay', 'model_slc1',
           'prepare_rotating_replay', 'prepare_manifest_replay']
