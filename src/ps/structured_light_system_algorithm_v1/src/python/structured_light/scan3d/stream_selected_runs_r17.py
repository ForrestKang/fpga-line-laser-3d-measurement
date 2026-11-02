"""SLC4/URC1: original RPG1 facts plus explicit selected-run PL decisions.

Only the fixed conservative unique-eligible domain is admitted as primary.
TESTONLY modeling is an independent Raw oracle, never FPGA evidence.
"""
from dataclasses import dataclass, replace
from functools import cached_property, lru_cache
import hashlib
import json
from pathlib import Path
from time import perf_counter

import numpy as np

from ..p2.errors import ProtocolError, CapabilityError
from ..p2.contract import DIAG_OFFSETS, REG_STATUS, REG_PROFILE_ID, REG_ERROR_FLAGS
from ..p2.pynq_transport import verify_overlay_artifacts, parse_hwh_parameters, _parameter_int
from .stream_compact import SLC1_ABI, COMPACT_DMA_CONTRACT
from .stream_features import FEATURE_DMA_CONTRACT
from .stream_ridge import RidgePacket, RIDGE_DMA_CONTRACT, RPG1, SLC2_MAGIC, decode_slc2, model_slc2
from .stream_segments_r15 import (SegmentPynqTransport, SegmentPacket, SEGMENT_DMA_CONTRACT,
    SEGMENT_REASONS, RSG1, SLC3_MAGIC, segment_configuration_supported)
from .stream_capture import _FeatureDMAAdmission
from .segment_candidates_r15 import fixed_segment_policy

SLC4_MAGIC = 0x534c4334
URC1 = 0x55524331
SLC4_MAX_BYTES = 128 + 480*24
SELECTED_RUNS_DMA_CONTRACT = dict(SEGMENT_DMA_CONTRACT, format='SLC4',
    capability_magic=URC1, capability_offset=0x160, abi_offset=0x164,
    status_offset=0x168, fault_reset_offset=0x16c, mode_value=5,
    max_packet_bytes=SLC4_MAX_BYTES, prefilter_policy=URC1,
    max_candidates_per_row=8, selected_policy='single_or_unique_eligible_v1',
    eligible_count_saturation=15, saturated_competitor_policy='class_exactly_4')


def selected_configuration_supported(config):
    return (segment_configuration_supported(config)
        and getattr(config, 'threshold_mode', 'fixed') == 'fixed'
        and getattr(config, 'max_candidates_per_row', 8) == 8)


@dataclass(frozen=True)
class SelectedRunsPacket:
    payload: bytes
    ridge_packet: RidgePacket

    @property
    def request_id(self): return self.ridge_packet.request_id
    @property
    def epoch(self): return self.ridge_packet.epoch
    @property
    def configuration(self): return self.ridge_packet.configuration
    @property
    def header(self): return self.ridge_packet.header
    @property
    def columns(self): return self.ridge_packet.columns
    @property
    def ridge_flags(self): return self.ridge_packet.ridge_flags
    @property
    def vectors(self): return self.ridge_packet.vectors

    @cached_property
    def selected_vectors(self):
        return np.frombuffer(self.payload, '<u4', offset=128).reshape(-1, 6)[:, 4:6]

    @cached_property
    def decisions(self):
        return np.frombuffer(self.payload, '<u4', offset=128).reshape(-1, 6)[:, 3]

    @cached_property
    def selected_columns(self):
        """Immutable (Q12, class, ridge, present, first, last, class_or,
        eligible_count_saturate15, pure_saturated_present, overflow, supported).
        The original RPG1 columns remain available separately as .columns.
        """
        a, b = self.selected_vectors.T
        values = (a & 0x3fffff, (a >> 22) & 31, (a >> 27) & 7,
            (a & (1 << 30)) != 0, b & 1023, (b >> 10) & 1023,
            (b >> 20) & 31, (b >> 25) & 15, (b & (1 << 29)) != 0,
            (b & (1 << 30)) != 0, (b & (1 << 31)) != 0)
        return tuple(np.frombuffer(v.tobytes(), dtype=v.dtype) for v in values)

    @property
    def whole_frame_fallback(self): return bool(self.decisions[0] & 128)

    @cached_property
    def fallback_segment_packet(self):
        # Fallback only: bridge to the frozen R16 consumer, retaining every
        # original RPG1 byte. No selected sideband becomes an old SLC3 fact.
        h = np.frombuffer(self.payload, '<u4', count=32).copy()
        rows = np.frombuffer(self.payload, '<u4', offset=128).reshape(-1, 6)
        h[0], h[2], h[30] = SLC3_MAGIC, 32+4*len(rows), RSG1
        legacy = np.empty((len(rows), 4), '<u4')
        legacy[:, :3], legacy[:, 3] = rows[:, :3], 128
        return SegmentPacket(h.tobytes()+legacy.tobytes(), self.ridge_packet)


def decode_slc4(payload, *, frame_width, frame_height, request_id=None, capture_epoch=None):
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProtocolError('SLC4 payload must be bytes-like')
    data = bytes(payload)
    if (type(frame_width) is not int or type(frame_height) is not int
            or any(v is not None and type(v) is not int for v in (request_id, capture_epoch))):
        raise ProtocolError('SLC4 identity and geometry must use exact integers')
    return _decode_slc4_cached(data, frame_width, frame_height, request_id, capture_epoch)


@lru_cache(maxsize=2, typed=True)
def _decode_slc4_cached(data, frame_width, frame_height, request_id, capture_epoch):
    if len(data) < 152 or len(data) > SLC4_MAX_BYTES or (len(data)-128) % 24:
        raise ProtocolError('SLC4 packet length mismatch')
    h = np.frombuffer(data, '<u4', count=32)
    if (h[0] != SLC4_MAGIC or h[1] != SLC1_ABI or h[2]*4 != len(data)
            or not 1 <= h[10] <= 480 or h[2] != 32+6*h[10]
            or h[30] != URC1 or h[31]):
        raise ProtocolError('SLC4 magic/ABI/word count/policy mismatch')
    rows = np.frombuffer(data, '<u4', offset=128).reshape(-1, 6)
    normalized = np.empty(32+3*len(rows), '<u4')
    normalized[:32] = h
    normalized[0], normalized[2], normalized[30] = SLC2_MAGIC, len(normalized), RPG1
    normalized[32:] = rows[:, :3].ravel()
    ridge = decode_slc2(normalized.tobytes(), frame_width=frame_width,
        frame_height=frame_height, request_id=request_id, capture_epoch=capture_epoch)
    if h[9]: raise ProtocolError('SLC4 reports a PL capture error')
    packet = SelectedRunsPacket(data, ridge)
    decision = packet.decisions
    if np.any(decision >> 8) or np.any(rows[:, 4] >> 31):
        raise ProtocolError('SLC4 reserved decision/selected bits')
    center, flags, _, _, fault, first, last, count, *_ = ridge.columns
    sc, sf, sr, present, ss, se, class_or, eligible, sat_comp, overflow, supported = packet.selected_columns
    x0, _, x1, _ = ridge.configuration.roi
    fixed = selected_configuration_supported(ridge.configuration)
    expected_supported = (~fault & ~overflow & ((count <= 1)
        | ((eligible == 1) & ~sat_comp))) if fixed else np.zeros(len(count), bool)
    expected_present = expected_supported & (count > 0)
    invalid = (overflow != (count > 8)) | (supported != expected_supported) | (present != expected_present)
    invalid |= eligible > np.minimum(count, 15)
    invalid |= (class_or == 0) & (eligible != np.minimum(count, 15))
    invalid |= (eligible == count) & (class_or != 0)
    invalid |= sat_comp & (((class_or & 4) == 0) | (eligible >= count))
    invalid |= (count == 0) & ((class_or != 0) | (eligible != 0) | sat_comp)
    single = (count == 1) & ~fault
    invalid |= single & ((class_or != flags) | (eligible != (flags == 0)) | (sat_comp != (flags == 4)))
    absent = ~present
    invalid |= absent & ((sc != 0) | (sf != 0) | (sr != 0) | (ss != 0) | (se != 0))
    invalid |= present & ((ss < x0) | (se >= x1) | (ss > se)
        | (sc < (ss.astype(np.int64) << 12)) | (sc > (se.astype(np.int64) << 12))
        | (((sr & 1) == 0) & (sr != 0)))
    invalid |= single & present & ((sc != center) | (sf != flags) | (sr != ridge.ridge_flags)
        | (ss != first) | (se != last))
    multi = present & (count > 1)
    invalid |= multi & ((sf != 0) | (ss < first) | (se > last)
        | (se-ss+1 > 32) | (ss == x0) | (se == x1-1))
    if np.any(invalid): raise ProtocolError('SLC4 selected/class/count/support mismatch')
    fallback = (decision & 128) != 0
    if np.any(fallback):
        if not np.all(decision == 128):
            raise ProtocolError('SLC4 fallback must cover the whole frame')
        return packet
    if not fixed or not np.all(supported):
        raise ProtocolError('URC1 primary requires fixed configuration and supported rows')
    reason, accepted, retained = (decision >> 2) & 15, (decision & 1) != 0, (decision & 2) != 0
    if (np.any((decision & 64) == 0) or np.any(reason > 11)
            or np.any(accepted != (reason == 0))
            or np.any(retained != ((reason == 0) | (reason >= 8)))):
        raise ProtocolError('SLC4 decision masks/reason mismatch')
    expected = np.zeros(len(reason), np.uint32)
    for bit, code in ((2, 6), (1, 5), (8, 4), (4, 3), (16, 2)):
        expected[(sf & bit) != 0] = code
    expected[~present] = 1
    base_reject = expected != 0
    if np.any(base_reject & (reason != expected)):
        raise ProtocolError('SLC4 local classification/reason mismatch')
    ridge_reason = np.full(len(reason), 10, np.uint32)
    ridge_reason[(sr & 4) != 0] = 0
    ridge_reason[(sr & 2) == 0] = 9
    ridge_reason[(sr & 1) == 0] = 8
    allowed = (reason == 7) | (reason == ridge_reason) | ((ridge_reason == 0) & (reason == 11))
    if np.any(~base_reject & ~allowed):
        raise ProtocolError('SLC4 local ridge/reason mismatch')
    return packet


def model_slc4(raw, config, *, request_id, capture_epoch, width=752, height=480, force_fallback=False):
    """TESTONLY Raw oracle producer. Never represents captured FPGA output."""
    from ..stripe_v1.candidates import extract_candidates
    from ..stripe_v1.selection_v2 import _candidate_reasons
    from ..stripe_v1.selection_v4 import select_candidates_v4, ridge_integer_values
    if config.max_candidates_per_row != 8 or config.saturation_level != 255:
        raise ValueError('URC1 TESTONLY model requires max_candidates8 and DN255 hardware policy')
    gray = np.frombuffer(raw, np.uint8).reshape(height, width)
    original = model_slc2(raw, config, request_id=request_id, capture_epoch=capture_epoch,
                          width=width, height=height)
    ridge = decode_slc2(original, frame_width=width, frame_height=height)
    hardware_config = replace(config, saturation_level=255)
    source = extract_candidates(gray, hardware_config)
    x0, y0, x1, y1 = ridge.configuration.roi
    rows = np.zeros((y1-y0, 6), '<u4')
    rows[:, :3] = np.frombuffer(original, '<u4', offset=128).reshape(-1, 3)
    reason_bits = dict(candidate_width=1, insufficient_contrast=2, saturated_support=4,
                       roi_truncated=8, overflow=16)
    for index, row in enumerate(source.rows[y0:y1]):
        flags = [sum(reason_bits[r] for r in _candidate_reasons(c, hardware_config, x0, x1))
                 for c in row.candidates]
        count = len(flags)
        eligible = [i for i, f in enumerate(flags) if f == 0]
        sat = any(f == 4 for f in flags)
        class_or = int(np.bitwise_or.reduce(np.asarray(flags, np.uint32), initial=0))
        overflow = count > 8
        supported = (selected_configuration_supported(config) and not overflow
            and (count <= 1 or (len(eligible) == 1 and not sat)))
        chosen = row.candidates[0] if supported and count == 1 else (
            row.candidates[eligible[0]] if supported and count > 1 else None)
        if chosen is not None:
            f = flags[chosen.index]
            values = ridge_integer_values(gray[y0+index], chosen.start, chosen.end, x0, x1)
            rf = int(values.window_complete) | (int(values.window_complete and values.contrast8 >= 160) << 1)
            shape = (values.window_complete and chosen.width >= 3 and values.left_drop_dn >= 8
                and values.right_drop_dn >= 8 and int(gray[y0+index, chosen.start+1:chosen.end].max(initial=0))
                == int(gray[y0+index, chosen.start:chosen.end+1].max()))
            rf |= int(shape) << 2
            rows[index, 4] = chosen.center_q12 | (f << 22) | (rf << 27) | (1 << 30)
            rows[index, 5] = chosen.start | (chosen.end << 10)
        rows[index, 5] |= ((class_or << 20) | (min(len(eligible), 15) << 25)
            | (int(sat) << 29) | (int(overflow) << 30) | (int(supported) << 31))
    if force_fallback or not np.all((rows[:, 5] >> 31) != 0):
        rows[:, 3] = 128
    else:
        oracle = select_candidates_v4(gray, source, hardware_config, fixed_segment_policy())
        for index, row in enumerate(oracle.rows[y0:y1]):
            reason = SEGMENT_REASONS.index(row.reason)
            retained = row.candidate is not None
            rows[index, 3] = 64 | int(retained and row.reason is None) | (int(retained) << 1) | (reason << 2)
    h = np.frombuffer(original, '<u4', count=32).copy()
    h[0], h[2], h[30] = SLC4_MAGIC, 32+6*len(rows), URC1
    result = h.tobytes()+rows.tobytes()
    decode_slc4(result, frame_width=width, frame_height=height,
                request_id=request_id, capture_epoch=capture_epoch)
    return result


def select_selected_run_candidates(packet, gray, config, selection):
    """Consume admitted selected fields; primary never repeats PS segments."""
    from ..stripe_v1.stripe_fast import StripeRows
    from ..stripe_v1.selection_v4 import SelectionConfigV4
    if not isinstance(packet, SelectedRunsPacket):
        raise ValueError('URC1 requires an admitted SelectedRunsPacket')
    if packet.whole_frame_fallback:
        from .local_segments_r16 import select_local_segment_candidates
        return select_local_segment_candidates(packet.fallback_segment_packet, gray, config, selection)
    if (not isinstance(gray, np.ndarray) or gray.dtype != np.uint8
            or gray.shape != (packet.configuration.height, packet.configuration.width)):
        raise ValueError('SLC4/raw uint8 geometry mismatch')
    if (not selected_configuration_supported(config) or not packet.configuration.matches(config)
            or not isinstance(selection, SelectionConfigV4)
            or selection.legacy.selection != fixed_segment_policy().legacy.selection
            or (selection.min_contrast_dn, selection.min_edge_drop_dn,
                selection.max_segment_width_span_px) != (20, 8, 12)):
        raise ValueError('URC1 requires the fixed configuration and segment policy')
    height = packet.configuration.height
    _, y0, _, y1 = packet.configuration.roi
    decision = packet.decisions
    local_retained = (decision & 2) != 0
    center, _, _, _, first, last, *_ = packet.selected_columns
    centers, starts, ends = np.zeros(height, np.int64), np.full(height, -1, np.int64), np.full(height, -1, np.int64)
    accepted, retained, primary = np.zeros(height, bool), np.zeros(height, bool), np.zeros(height, bool)
    reasons = np.full(height, 'roi_excluded', dtype=object)
    centers[y0:y1] = np.where(local_retained, center, 0)
    starts[y0:y1] = np.where(local_retained, first.astype(np.int64), -1)
    ends[y0:y1] = np.where(local_retained, last.astype(np.int64), -1)
    accepted[y0:y1], retained[y0:y1], primary[y0:y1] = (decision & 1) != 0, local_retained, True
    reasons[y0:y1] = np.asarray(SEGMENT_REASONS, dtype=object)[(decision >> 2) & 15]
    return StripeRows(centers, starts, ends, accepted, retained, tuple(reasons.tolist())), primary


def compare_selected_runs_raw(packet, gray):
    """QA only: independently re-extract Raw and compare all six row words.

    A received benign whole-frame fallback is modeled as fallback too; this
    does not turn its candidate sideband into valid PL final decisions.
    """
    from ..stripe_v1.types import StripeConfig
    if not isinstance(packet, SelectedRunsPacket):
        raise ValueError('URC1 Raw comparison requires an admitted packet')
    cfg = packet.configuration
    if (not isinstance(gray, np.ndarray) or gray.dtype != np.uint8
            or gray.shape != (cfg.height, cfg.width)):
        raise ValueError('SLC4/raw uint8 geometry mismatch')
    config = StripeConfig(fixed_threshold=cfg.fixed_threshold, roi=cfg.roi,
        min_width=cfg.min_width, max_width=cfg.max_width, min_sum=cfg.min_sum,
        min_peak=cfg.min_peak, saturation_level=255, max_candidates_per_row=8)
    expected = model_slc4(gray.tobytes(), config, request_id=packet.request_id,
        capture_epoch=packet.epoch, width=cfg.width, height=cfg.height,
        force_fallback=packet.whole_frame_fallback)
    a = np.frombuffer(packet.payload, '<u4', offset=128).reshape(-1, 6)
    b = np.frombuffer(expected, '<u4', offset=128).reshape(-1, 6)
    mismatch = np.flatnonzero(np.any(a != b, axis=1))
    return dict(passed=not len(mismatch), checked_rows=len(a),
        mismatching_source_rows=(mismatch+cfg.roi[1]).tolist(),
        original_fact_mismatch_rows=(np.flatnonzero(np.any(a[:, :3] != b[:, :3], axis=1))+cfg.roi[1]).tolist(),
        selected_fact_mismatch_rows=(np.flatnonzero(np.any(a[:, 4:] != b[:, 4:], axis=1))+cfg.roi[1]).tolist(),
        final_decision_mismatch_rows=(np.flatnonzero(a[:, 3] != b[:, 3])+cfg.roi[1]).tolist(),
        comparison_scope='independent Raw original facts, selected fields and final decisions; QA only')


class SelectedRunsPynqTransport(SegmentPynqTransport):
    """Explicit manifest/artifact/capability-bound mode5; modes0..4 retained."""
    def _selected_runs_idle_handoff_state(self):
        """Keep the original handoff guards, allowing sticky's ready=0 only.

        URC1 intentionally deasserts the existing core-ready bit on a sticky
        fault. A reset-specific idle check must not require that bit to be 1.
        Buffers, DMA, recovery, busy/error and stable identity checks remain.
        """
        before_status = int(self._diag_mmio.read(0x168))
        if before_status != 2:
            return self._handoff_state()
        if (self._accepted_request_id is not None or self._accepted_epoch is not None
                or self._raw_buffer is not None or self._profile_buffer is not None
                or self.raw_dma.transfer_state is not None
                or (self.profile_dma is not None and self.profile_dma.transfer_state is not None)
                or self.recovery_required or self._recovery_token is not None):
            raise ProtocolError('URC1 fault reset requires no in-flight buffers or recovery token')
        def read():
            return dict(status=int(self.mmio.read(REG_STATUS)),
                control_id=int(self.mmio.read(REG_PROFILE_ID)),
                control_error=int(self.mmio.read(REG_ERROR_FLAGS)),
                **{key:int(self._diag_mmio.read(DIAG_OFFSETS[key])) for key in
                   ('ready','request_id','epoch','error','row_count','frame_count')})
        state = read()
        if state != read() or int(self._diag_mmio.read(0x168)) != before_status:
            raise ProtocolError('URC1 fault reset hardware snapshot changed while reading')
        if (state['status'] & 5 or state['ready'] & 5 or state['error']
                or state['control_error'] or state['request_id'] != state['control_id']):
            raise ProtocolError('URC1 fault reset requires stable idle error-free matching hardware identity')
        return state

    def _verify_selected_runs_contract(self, manifest_path, *, workers_joined):
        """Read-only complete artifact/manifest/capability proof, even faulted."""
        manifest = Path(manifest_path)
        record = json.loads(manifest.read_text(encoding='utf8'))
        for key, contract in (('row_feature_dma', FEATURE_DMA_CONTRACT),
                ('compact_policy_dma', COMPACT_DMA_CONTRACT),
                ('ridge_policy_dma', RIDGE_DMA_CONTRACT),
                ('segment_policy_dma', SEGMENT_DMA_CONTRACT),
                ('selected_runs_policy_dma', SELECTED_RUNS_DMA_CONTRACT)):
            declaration = record.get(key)
            if (not isinstance(declaration, dict) or declaration != contract
                    or any(type(declaration[k]) is not type(v) for k, v in contract.items())):
                raise CapabilityError('URC1 manifest declaration mismatch: '+key)
        if workers_joined is not True:
            raise ProtocolError('URC1 requires joined workers')
        verify_overlay_artifacts(self.bit_path, self.hwh_path, manifest_path)
        binding = tuple(hashlib.sha256(Path(p).read_bytes()).hexdigest()
                        for p in (self.bit_path, self.hwh_path))
        if binding != self._feature_artifact_binding:
            raise CapabilityError('URC1 artifacts changed after admission')
        parameters = parse_hwh_parameters(self.hwh_path)
        if (any(_parameter_int(parameters, suffix) != 1 for suffix in
                ('.FEATURE_DMA_ENABLE', '.COMPACT_POLICY_ENABLE')) or self.profile_dma is None):
            raise CapabilityError('URC1 HWH/DMA mismatch')
        # All capability/state failures occur before writes or mode receipts.
        for offset, expected in ((0x134, 0x52464431), (0x138, SLC1_ABI),
                (0x140, 0x52435131), (0x144, SLC1_ABI), (0x148, RPG1),
                (0x14c, SLC1_ABI), (0x150, RSG1), (0x154, SLC1_ABI),
                (0x160, URC1), (0x164, SLC1_ABI)):
            if int(self._diag_mmio.read(offset)) != expected:
                raise CapabilityError('URC1 dependency capability/ABI mismatch')
        self._selected_runs_idle_handoff_state()
        receipt = _FeatureDMAAdmission(binding, hashlib.sha256(manifest.read_bytes()).hexdigest())
        return receipt

    def admit_selected_runs_dma(self, manifest_path, *, workers_joined=True):
        receipt = self._verify_selected_runs_contract(manifest_path, workers_joined=workers_joined)
        self._selected_runs_capability_receipt = receipt
        self._selected_runs_manifest_path = Path(manifest_path).resolve()
        self._require_selected_runs_ready()
        self._selected_runs_dma_receipt = receipt
        self._compact_dma_receipt = receipt
        self._ridge_dma_receipt = receipt
        self.set_feature_mode(5, workers_joined=True)
        return True

    def admit_ridge_dma(self, manifest_path, *, workers_joined=True):
        """R15 bench integration alias: admits mode5, never silently mode3."""
        return self.admit_selected_runs_dma(manifest_path, workers_joined=workers_joined)

    def _require_selected_runs_ready(self):
        if int(self._diag_mmio.read(0x168)) != 1:
            raise ProtocolError('URC1 is busy or faulted; explicit idle reset required for sticky fault')

    @property
    def selected_runs_policy_dma_enabled(self):
        proof = getattr(self, '_selected_runs_dma_receipt', None)
        return (isinstance(proof, _FeatureDMAAdmission)
            and proof.artifact_binding == self._feature_artifact_binding
            and getattr(self, '_feature_dma_mode', 0) == 5)

    @property
    def ridge_policy_dma_enabled(self):
        return self.selected_runs_policy_dma_enabled or super().ridge_policy_dma_enabled

    @property
    def compact_policy_dma_enabled(self):
        return self.selected_runs_policy_dma_enabled or super().compact_policy_dma_enabled

    def set_feature_mode(self, mode, *, workers_joined):
        if mode != 5:
            return super().set_feature_mode(mode, workers_joined=workers_joined)
        if type(mode) is not int or workers_joined is not True:
            raise ProtocolError('URC1 mode requires joined workers')
        proof = getattr(self, '_selected_runs_dma_receipt', None)
        if (not isinstance(proof, _FeatureDMAAdmission)
                or proof.artifact_binding != self._feature_artifact_binding):
            raise CapabilityError('URC1 mode is not artifact admitted')
        self._handoff_state()
        self._require_selected_runs_ready()
        self._feature_mode_restore_required = True
        self._diag_mmio.write(0x13c, 5)
        if int(self._diag_mmio.read(0x13c)) != 5:
            raise ProtocolError('URC1 mode write rejected')
        self._feature_dma_mode = 5

    def reset_selected_runs_fault(self, *, workers_joined, manifest_path=None):
        """Operator/runtime explicit reset only; never called by admission/recovery."""
        path = manifest_path if manifest_path is not None else getattr(self, '_selected_runs_manifest_path', None)
        if path is None:
            raise CapabilityError('URC1 explicit reset requires its complete manifest path')
        proof = self._verify_selected_runs_contract(path, workers_joined=workers_joined)
        status = int(self._diag_mmio.read(0x168))
        if status & ~7 or status & 4:
            raise ProtocolError('URC1 reset requires idle engine')
        self._diag_mmio.write(0x16c, 1)
        self._require_selected_runs_ready()
        self._selected_runs_capability_receipt = proof
        self._selected_runs_manifest_path = Path(path).resolve()

    def arm_profile(self, size):
        if not self.selected_runs_policy_dma_enabled:
            return super().arm_profile(size)
        # Bypass only FeaturePynqTransport's fixed SLC1 allocation choice.
        from .stream_capture import FeaturePynqTransport
        return super(FeaturePynqTransport, self).arm_profile(SLC4_MAX_BYTES)

    def _profile_receive_limit(self):
        return SLC4_MAX_BYTES if self.selected_runs_policy_dma_enabled else super()._profile_receive_limit()

    def start_capture(self, *, raw, profile):
        if self.selected_runs_policy_dma_enabled:
            self._require_selected_runs_ready()
            self._received_selected_runs_packet = None
        return super().start_capture(raw=raw, profile=profile)

    def _diagnostics_for_payload(self, profile, diagnostics):
        if not self.selected_runs_policy_dma_enabled or profile is None:
            return super()._diagnostics_for_payload(profile, diagnostics)
        self._received_selected_runs_packet = None
        started = perf_counter()
        self._require_selected_runs_ready()
        packet = decode_slc4(profile, frame_width=self.frame_width, frame_height=self.frame_height,
            request_id=self._accepted_request_id, capture_epoch=self._accepted_epoch)
        if (packet.configuration != self._feature_configuration or packet.request_id != diagnostics.request_id
                or packet.epoch != diagnostics.epoch or int(packet.header[9]) != diagnostics.error
                or diagnostics.row_count != self.frame_height):
            raise ProtocolError('SLC4 accepted configuration/diagnostic mismatch')
        self._received_selected_runs_packet = packet
        self.total_feature_decode_s = getattr(self, 'total_feature_decode_s', 0.)+perf_counter()-started
        return replace(diagnostics, rows=())

    def claim_compact_packet(self, payload, *, request_id, capture_epoch):
        if not self.selected_runs_policy_dma_enabled:
            return super().claim_compact_packet(payload, request_id=request_id, capture_epoch=capture_epoch)
        packet = getattr(self, '_received_selected_runs_packet', None)
        self._received_selected_runs_packet = None
        if (not isinstance(packet, SelectedRunsPacket) or packet.payload != payload
                or packet.request_id != request_id or packet.epoch != capture_epoch):
            raise CapabilityError('SLC4 requires admitted same-capture receive evidence')
        return packet


def capture_features_selected_runs(controller, *, timeout_s=2.0, require_motion_metadata=True, timing_s=None):
    """Mode4 finite capture with the original START/wait/recovery/generation lifecycle.

    Uses the transport's validated same-capture receipt inside DMA recovery.
    This R15 entry changes only SLC4 size/type admission; legacy capture stays
    in stream_capture.capture_features and is not monkey-patched globally.
    """
    from .stream_capture import FeatureCapture, read_motion_metadata
    from .payload_digest import payload_sha256
    from ..motion.lifecycle import GatedCaptureTransport
    bound = (controller.transport._transport if isinstance(controller.transport, GatedCaptureTransport)
             else controller.transport)
    if not isinstance(bound, SelectedRunsPynqTransport) or not bound.selected_runs_policy_dma_enabled:
        raise CapabilityError('SLC4 capture requires admitted SelectedRunsPynqTransport mode5')
    started = perf_counter() if timing_s is not None else 0.
    diagnostic_before = getattr(controller.transport, 'total_read_diagnostics_s', 0.)
    decode_before = getattr(controller.transport, 'total_feature_decode_s', 0.)
    row_reads_before = getattr(controller.transport, 'total_row_table_full_reads', 0)
    handle = controller._arm_and_start(raw=True, profile=True)
    if timing_s is not None:
        now = perf_counter(); timing_s['arm_start_s'] = now-started; started = now
    transfer = controller._wait_for_transfer(timeout_s)
    if timing_s is not None:
        now = perf_counter(); timing_s['wait_transfer_inclusive_s'] = now-started; started = now
        timing_s['diagnostic_read_nested_s'] = getattr(controller.transport, 'total_read_diagnostics_s', 0.)-diagnostic_before
        timing_s['feature_packet_decode_nested_s'] = getattr(controller.transport, 'total_feature_decode_s', 0.)-decode_before
    try:
        raw, payload = transfer.raw, transfer.profile
        if (raw is None or len(raw) != controller.frame_width*controller.frame_height
                or transfer.raw_length != len(raw)):
            raise ProtocolError('SLC4 capture Raw length mismatch')
        if payload is None or transfer.profile_length != len(payload) or not 0 < len(payload) <= SLC4_MAX_BYTES:
            raise ProtocolError('SLC4 capture Profile length mismatch')
        packet = controller.transport.claim_compact_packet(payload, request_id=handle.request_id,
                                                          capture_epoch=handle.epoch)
        if timing_s is not None:
            now = perf_counter(); timing_s['profile_decode_s'] = now-started; started = now
        diagnostics = transfer.diagnostics or controller.transport.read_diagnostics()
        if (diagnostics.request_id != handle.request_id or diagnostics.epoch != handle.epoch
                or diagnostics.row_count != controller.frame_height or diagnostics.error
                or packet.header[9]):
            raise ProtocolError('SLC4 capture diagnostic identity/state mismatch')
        if timing_s is not None:
            now = perf_counter(); timing_s['diagnostic_read_s'] = now-started; started = now
        metadata = read_motion_metadata(controller.transport._diag_mmio,
            request_id=handle.request_id, capture_epoch=handle.epoch, required=True,
            capability_confirmed=getattr(controller.transport, 'frame_motion_metadata_supported', False)) if require_motion_metadata else None
        if timing_s is not None:
            now = perf_counter(); timing_s['motion_metadata_s'] = now-started; started = now
        digest = payload_sha256(raw)
        if timing_s is not None:
            timing_s['raw_sha256_s'] = perf_counter()-started
        return FeatureCapture(handle.request_id, handle.epoch, raw, digest, None, diagnostics, metadata,
            profile_payload=bytes(payload), timing_s=timing_s, compact_packet=packet,
            row_table_full_reads=getattr(controller.transport, 'total_row_table_full_reads', 0)-row_reads_before)
    except Exception as exc:
        bound._received_selected_runs_packet = None
        controller._recover_after_processing_error('continuous_feature_processing_error', exc, handle)
        raise
    finally:
        controller._finish_inflight()


