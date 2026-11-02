"""R26 serial prepared fast path for one immutable PFO3 frame.

This module is an opt-in implementation detail of
``PLGeometryPairProcessorR25.process_prepared_final``.  It does not decode,
hash-retain, or re-admit the PL wire frame: the caller must already hold the
transport-retained ``FinalFrame`` and its admission.  Every call still enters
the *calling processor's* lock and updates only that processor's state/cloud.
The frame-derived arrays and the one union camera mapping are immutable and
shared only as read-only data.

The R25 cold implementation remains in
``PLGeometryPairProcessorR25._process_prepared_final_cold`` and is used for
non-replay captures, custom extension points, non-flat metadata and any
calibration/axis mismatch.  The fast path never runs an image extractor, branch
linker, or Raw quality estimator.
"""
from __future__ import annotations

import os
import threading

import numpy as np

from structured_light.scan3d.continuous_timing import _finite, _uint
from structured_light.scan3d.multibranch_processor_r21 import MultiBranchProfile
from sl_geometry.geometry import unrotate_points
from .pfo3_processor_r25 import PLFinalProcessorR25, _forbidden, _immutable_image_digest
from .pfo3_geometry_pair_r25 import PLGeometryPairProcessorR25, _axis_key, _immutable
from .pfo3_r25 import PFO3Error
from .geometry_axis_r29 import unrotate_prepared

_FROZEN_UNROTATE = unrotate_points
_R29_GEOMETRY = os.environ.get('SCAN3D_R29_GEOMETRY', 'baseline')
if _R29_GEOMETRY not in ('baseline', 'shared'):
    raise ValueError('unknown R29 geometry backend')

_SCALAR_TYPES = (type(None), bool, int, float, str, bytes)
_MAPPING = (('cfg_version', 'feature_config_revision'),
            ('exposure_version', 'exposure_revision'),
            ('motor_id', 'motor_accepted_id'),
            ('motor_epoch', 'motor_epoch'),
            ('motor_steps', 'emitted_command_count'),
            ('FV_ticks', 'fv_ticks'),
            ('exposure_start', 'exposure_start_ticks'),
            ('exposure_end', 'exposure_end_ticks'))


def _flat_metadata(metadata):
    """The one-copy metadata optimization is valid only for immutable leaves."""
    return (type(metadata) is dict
            and all(type(value) in _SCALAR_TYPES for value in metadata.values()))


def _strong_owner(image):
    """Return the retained bytes owner only for an exact full immutable view.

    This mirrors ``pfo3_processor_r25._immutable_image_digest`` retention
    rules.  Any other ndarray is re-hashed on every call, so a mutable view can
    never make the fast path accept changed Raw bytes.
    """
    array = np.asarray(image)
    owner = array
    while isinstance(owner, np.ndarray) and owner.base is not None:
        owner = owner.base
    if (type(owner) is bytes and array.dtype == np.uint8 and array.shape == (480, 752)
            and array.flags.c_contiguous and len(owner) == array.nbytes
            and array.ctypes.data == np.frombuffer(owner, np.uint8).ctypes.data):
        return owner
    return None


def _fast_path_supported(processor):
    """Only the exact R25 pair implementation may take the shortcuts."""
    cls = type(processor)
    return (isinstance(processor, PLGeometryPairProcessorR25)
            and getattr(processor, 'replay', False) is True
            and cls._observations is PLGeometryPairProcessorR25._observations
            and cls._quality is PLGeometryPairProcessorR25._quality
            and cls._reconstruct_final_section is PLGeometryPairProcessorR25._reconstruct_final_section
            and cls._image_usable is PLFinalProcessorR25._image_usable
            and cls._geometry_quality_gate is PLFinalProcessorR25._geometry_quality_gate
            and cls.process_final is PLFinalProcessorR25.process_final
            and not any(name in processor.__dict__ for name in
                        ('_observations', '_quality', '_reconstruct_final_section',
                         '_image_usable', '_geometry_quality_gate', 'process_final')))


class PreparedFastContextR26:
    """Per-frame Raw/axis cache shared by the two serial mode calls.

    The context is attached to one ``PreparedGeometryPairR25`` and never
    reused across frames.  Union axis arrays are built lazily because the angle
    arrives with the frame metadata, not with the pair constructor.  A lock
    also keeps the legacy threaded PairedModeExecutor correct if it is used.
    """

    def __init__(self, pair, capture, admission):
        self.pair = pair
        self._capture = capture
        self._admission = admission
        image = np.asarray(capture)
        if image.shape != (480, 752) or image.dtype != np.uint8:
            raise ValueError('replay requires 752x480 uint8 gray')
        self._strong = _strong_owner(image)
        self._input_raw_sha = admission.input_raw_sha256
        raw_sha = _immutable_image_digest(image)
        if raw_sha != self._input_raw_sha:
            raise PFO3Error('geometry/display Raw differs from actual PL input Raw')
        self._raw_sha = raw_sha
        self._axis_lock = threading.Lock()
        self._axis_key = None
        self._union_axis = None
        self._mode_geometry = {}
        self._r29_shared = _R29_GEOMETRY == 'shared'

    def matches(self, capture, admission):
        return (capture is self._capture and admission is self._admission
                and admission.input_raw_sha256 == self._input_raw_sha)

    def verify_capture(self, capture):
        image = np.asarray(capture)
        if image.shape != (480, 752) or image.dtype != np.uint8:
            raise ValueError('replay requires 752x480 uint8 gray')
        if self._admission.input_raw_sha256 != self._input_raw_sha:
            raise PFO3Error('geometry/display Raw differs from actual PL input Raw')
        if (self._strong is not None and capture is self._capture
                and _strong_owner(image) is self._strong):
            # ndarray identity alone does not bind its data/strides. Retain the
            # exact immutable bytes owner and recheck the full-view layout.
            return
        if _immutable_image_digest(image) != self._input_raw_sha:
            raise PFO3Error('geometry/display Raw differs from actual PL input Raw')

    def union_axis(self, calibration, angle):
        """Rotate the single union-valid camera set once per axis/angle key."""
        key = (_axis_key(calibration), float(angle).hex())
        with self._axis_lock:
            if self._axis_key != key:
                points = self.pair.section_points
                axis = np.full_like(points, np.nan)
                mask = self.pair.fast_union_valid
                if mask.any():
                    point = calibration.axis_point
                    if self._r29_shared and unrotate_points is _FROZEN_UNROTATE:
                        unrotated = unrotate_prepared(points[mask], key[0], angle)
                    else:
                        unrotated = unrotate_points(points[mask], point,
                                                    calibration.axis_direction, angle)
                    axis[mask] = (unrotated - np.asarray(point)) @ calibration.basis_camera
                # Both mode profiles may share this result. A bytes owner
                # prevents a consumer from re-enabling writes on one profile
                # and corrupting its sibling or a later encoded record.
                self._union_axis = _immutable(axis)
                self._axis_key = key
                self._mode_geometry.clear()
            return self._union_axis

    def mode_geometry(self, mode, union_axis, valid):
        """Share only permanently immutable arrays for identical valid masks.

        Keep each processor's cloud list/count/revision independent. Cache
        entries are discarded with every axis/angle change; the array identity
        check also prevents a concurrent caller using another rotation's block.
        """
        covers = self.pair.mode_covers_union(mode)
        all_valid = self.pair.mode_valid_counts(mode)[0] == len(valid)
        with self._axis_lock:
            if union_axis is not self._union_axis:
                axis = union_axis if covers else _immutable(np.where(valid[:, None], union_axis, np.nan))
                return axis, axis if all_valid else _immutable(axis[valid])
            key = 'union' if covers else mode
            value = self._mode_geometry.get(key)
            if value is None:
                axis = union_axis if covers else _immutable(np.where(valid[:, None], union_axis, np.nan))
                value = axis, axis if all_valid else _immutable(axis[valid])
                self._mode_geometry[key] = value
            return value


def _check_mapping(metadata, admission):
    captured = metadata
    if any(captured.get(key) != admission.identity[field] for field, key in _MAPPING):
        raise PFO3Error('captured configuration/exposure/motion differs from PL identity')


def _admit_replay_fast(processor, capture, meta):
    """Same checks as MultiBranchProcessor._admit_replay without deepcopy."""
    image = np.asarray(capture)
    if image.shape != (480, 752) or image.dtype != np.uint8:
        raise ValueError('replay requires 752x480 uint8 gray')
    request = meta.get('request_id', meta.get('frame_id'))
    _uint('request_id', request)
    _uint('capture_epoch', meta.get('capture_epoch'), 16)
    if meta.get('frame_id', request) != request:
        raise ValueError('mixed frame/request identity')
    timestamp = _finite('timestamp_s', meta.get('timestamp_s'))
    _finite('angle_rad', meta.get('angle_rad'))
    if meta.get('calibration_id', processor.calibration_id) != processor.calibration_id:
        raise ValueError('frame/calibration identity mismatch')
    _uint('exposure_revision', meta.get('exposure_revision'))
    if (type(meta.get('exposure_lines')) is not int
            or meta.get('exposure_lines') != processor.exposure_lines
            or meta.get('exposure_revision') != processor.exposure_revision):
        raise ValueError('stale or missing replay exposure metadata')
    if processor._capture_epoch is not None and meta['capture_epoch'] != processor._capture_epoch:
        raise ValueError('capture epoch changed; construct a new processor')
    if processor._last_request is not None and request <= processor._last_request:
        raise ValueError('capture request repeated or out of order')
    if processor._last_time is not None and timestamp <= processor._last_time:
        raise ValueError('replay timestamps must strictly advance')
    return request, meta['capture_epoch'], timestamp


def process_prepared_final_fast(processor, capture, metadata, *, pair, payload,
                                admission, mode='measurement'):
    """Exact serial replay equivalent of the R25 prepared path, minus copies.

    No partial fast-path result is ever published: all checks happen before
    state mutation, each processor lock is held for its own call, and the
    method raises instead of guessing when its binding assumptions fail.
    """
    if (not _fast_path_supported(processor) or not _flat_metadata(metadata)
            or not pair.axis_matches(processor.calibration)):
        return processor._process_prepared_final_cold(capture, metadata, pair=pair,
            payload=payload, admission=admission, mode=mode)
    frame = pair.frame
    admission.validate()
    if admission.fixture_only:
        raise PFO3Error('prepared processor requires actual retained transport frame')
    if admission._validated_frame is not frame:
        raise PFO3Error('pair belongs to a different decoded frame')
    if payload is not frame.payload:
        raise PFO3Error('pair payload is not retained transport archive')
    if frame.raw_required:
        raise PFO3Error('whole-frame fallback cannot use geometry pair')
    if mode not in ('measurement', 'preview'):
        raise PFO3Error('unknown mode')
    context = pair.fast_context(capture, admission)
    context.verify_capture(capture)
    _check_mapping(metadata, admission)
    quality = pair.quality(mode)
    fast = pair.fast_mode(mode)
    # _fast_path_supported() pins _image_usable to the PL policy, whose only
    # input is the retained frame's per-mode decision bit.  Comparing the
    # pair's eager answer with the frame here keeps a changed policy fail-closed
    # without mutating processor state before the lock.
    usable = frame.frame_usable(mode)
    if usable is not fast.usable:
        return processor._process_prepared_final_cold(capture, metadata, pair=pair,
            payload=payload, admission=admission, mode=mode)
    with processor._lock:
        if getattr(processor, '_r25_pair', None) is not None:
            raise PFO3Error('nested pair processing')
        if getattr(processor, '_r25_policy_mode', mode) != mode:
            raise PFO3Error('measurement and preview require separate processor/cloud instances')
        processor._r25_policy_mode = mode
        processor._r25_pair = pair
        processor._r25_frame = frame
        processor._r25_mode = mode
        processor.extractor = _forbidden
        try:
            # Same point in the R25 sequence: the frame/request binding is
            # checked after the mode policy/frame fields are installed, so a
            # rejected request leaves the same fail-closed policy state.
            if (metadata.get('request_id') != frame.header['frame_id']
                    or metadata.get('capture_epoch') != frame.header['epoch']):
                raise PFO3Error('geometry request differs from PL frame')
            # Preserve the exact R25 insertion order: process_final's pre-update
            # fields, then the replay admission fields, then final status fields.
            meta = dict(metadata)
            meta.update(processing_backend='PL_FINAL_V3',
                pfo3_algorithm_revision=frame.header['algorithm_revision'],
                pfo3_mode=mode, ARM_image_algorithm_calls=0,
                pfo3_stream_sha256=admission.stream_sha256,
                pfo3_bit_sha256=admission.bit_sha256)
            request, epoch, timestamp = _admit_replay_fast(processor, capture, meta)
            meta.update(request_id=request, evidence_type='SIMULATION_ONLY',
                        measurement_eligible=False)
            meta.update(pfo3_mode=mode, ARM_image_algorithm_calls=0, PL_success=True,
                pfo3_algorithm_revision=frame.header['algorithm_revision'],
                processing_backend='PL_FINAL_V3',
                pfo3_stream_sha256=admission.stream_sha256,
                pfo3_bit_sha256=admission.bit_sha256,
                pfo3_input_raw_sha256=admission.input_raw_sha256,
                pfo3_source=frame.header['source'],
                pfo3_frame_usable=frame.frame_usable(mode),
                pfo3_dimensions_allowed=(mode == 'measurement'))
            angle = float(meta['angle_rad'])
            valid = fast.valid_pre if usable else fast.valid_gate
            reasons = fast.reasons_pre if usable else fast.reasons_gate
            counts = fast.counts_pre if usable else fast.counts_gate
            valid_count, valid_row_count = pair.mode_valid_counts(mode)
            enough_rows = valid_row_count >= processor.min_valid_rows
            profile_valid = usable and enough_rows
            # Same pre-append capacity gate as the replay branch in
            # MultiBranchProcessor.process; the cloud is never partially grown.
            if processor._point_count + valid_count > processor.max_cloud_points:
                raise BufferError('simulation point cloud capacity exceeded')
            union_axis = context.union_axis(processor.calibration, angle)
            camera = pair.mode_camera_points(mode)
            if context._r29_shared:
                axis_points, block = context.mode_geometry(mode, union_axis, valid)
            elif pair.mode_covers_union(mode):
                axis_points = union_axis
            else:
                axis_points = np.where(valid[:, None], union_axis, np.nan)
                axis_points.setflags(write=False)
            if valid_count:
                if not context._r29_shared:
                    block = axis_points[valid]
                    block.setflags(write=False)
                processor._cloud.append(block)
                processor._point_count += valid_count
                processor._revision += 1
            processor._capture_epoch = epoch
            processor._last_request = request
            processor._last_time = timestamp
            processor.scan_angles_rad.append(angle)
            return MultiBranchProfile(
                request_id=request, capture_epoch=epoch, mode=processor.mode,
                rows=pair.fast_rows, center_q12=pair.fast_centers,
                stripe_accepted=fast.accepted, points_camera_mm=camera,
                points_axis_mm=axis_points, valid_points=valid, reasons=reasons,
                rejection_counts=dict(counts), quality=quality, angle_rad=angle,
                angle_source='assumed_simulation_replay', exposure_mid_ticks=None,
                registered=False, profile_valid=bool(profile_valid and not processor.replay),
                valid=False, fused=False, cloud_revision=processor._revision,
                pl_primary_rows=0, raw_fallback_rows=pair.raw_fallback_rows,
                pl_mismatch_rows=(),
                registration_reason='SIMULATION_ONLY_not_physical_registration',
                branch_ids=fast.branch_ids,
                observation_quality=dict(pair.observation_quality),
                observation_reasons=fast.observation_reasons,
                observation_stats=dict(fast.stats_items), frame_metadata=meta,
                simulation_geometry_valid=bool(processor.replay and enough_rows),
                simulation_registered=True, reference_basis=processor.reference_basis)
        finally:
            processor._r25_frame = None
            processor._r25_pair = None
