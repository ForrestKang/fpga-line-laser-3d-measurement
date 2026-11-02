"""SLC3/RSG1 mode4: immutable PL segment decisions and explicit admission.

The TESTONLY producer is an offline oracle, never captured-FPGA evidence.
Successful primary consumption trusts admitted global segment decisions; local
wire consistency checks do not rerun PS association or width-span reductions.
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
from .stream_ridge import RidgePynqTransport, RidgePacket, RIDGE_DMA_CONTRACT, RPG1, SLC2_MAGIC, decode_slc2, model_slc2
from .stream_capture import _FeatureDMAAdmission

SLC3_MAGIC = 0x534c4333
RSG1 = 0x52534731
SLC3_MAX_BYTES = 128 + 480 * 16
SEGMENT_REASONS = (None, 'no_support', 'overflow', 'saturated_support',
    'roi_truncated', 'candidate_width', 'insufficient_contrast',
    'insufficient_segment_support', 'background_window_truncated',
    'ridge_contrast', 'ridge_shape', 'segment_width_span')
SEGMENT_DMA_CONTRACT = dict(enabled=True, format='SLC3', abi=SLC1_ABI,
    capability_magic=RSG1, capability_offset=0x150, abi_offset=0x154,
    status_offset=0x158, fault_reset_offset=0x15c, mode_offset=0x13c,
    mode_value=4, max_packet_bytes=SLC3_MAX_BYTES, prefilter_policy=RSG1,
    threshold=60, min_width=1, max_width=32, min_sum=30, min_peak=15,
    saturation_level=255, association_max_distance_px=3,
    association_max_row_gap=1, minimum_segment_rows=3,
    max_segment_width_span_px=12, min_contrast_dn=20, min_edge_drop_dn=8,
    center_policy='original_q12')


def segment_configuration_supported(config):
    return all(getattr(config, key) == value for key, value in (
        ('fixed_threshold', 60), ('min_width', 1), ('max_width', 32),
        ('min_sum', 30), ('min_peak', 15), ('saturation_level', 255)))


@dataclass(frozen=True)
class SegmentPacket:
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
    def decisions(self):
        return np.frombuffer(self.payload, '<u4', offset=128).reshape(-1, 4)[:, 3]

    @property
    def whole_frame_fallback(self):
        return bool(self.decisions[0] & 128)


def decode_slc3(payload, *, frame_width, frame_height, request_id=None, capture_epoch=None):
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProtocolError('SLC3 payload must be bytes-like')
    data = bytes(payload)
    args = (frame_width, frame_height, request_id, capture_epoch)
    if (type(frame_width) is int and type(frame_height) is int
            and all(value is None or type(value) is int for value in args[2:])):
        return _decode_slc3_cached(data, *args)
    return _decode_slc3_wire(data, *args)


@lru_cache(maxsize=2, typed=True)
def _decode_slc3_cached(data, frame_width, frame_height, request_id, capture_epoch):
    return _decode_slc3_wire(data, frame_width, frame_height, request_id, capture_epoch)


def _decode_slc3_wire(data, frame_width, frame_height, request_id, capture_epoch):
    if len(data) < 144 or len(data) > SLC3_MAX_BYTES or (len(data)-128) % 16:
        raise ProtocolError('SLC3 packet length mismatch')
    words = np.frombuffer(data, '<u4')
    h = words[:32]
    if (h[0] != SLC3_MAGIC or h[1] != SLC1_ABI or h[2] != len(words)
            or not 1 <= h[10] <= 480 or len(words) != 32+4*int(h[10])
            or h[30] != RSG1 or h[31]):
        raise ProtocolError('SLC3 magic/ABI/word count/policy mismatch')
    # The original three words and every NLQ1 check retain SLC2 admission.
    normalized = np.empty(32+3*int(h[10]), dtype='<u4')
    normalized[:32] = h
    normalized[0] = SLC2_MAGIC
    normalized[2] = len(normalized)
    normalized[30] = RPG1
    rows = words[32:].reshape(-1, 4)
    normalized[32:] = rows[:, :3].ravel()
    ridge = decode_slc2(normalized.tobytes(), frame_width=frame_width,
        frame_height=frame_height, request_id=request_id, capture_epoch=capture_epoch)
    if h[9]:
        raise ProtocolError('SLC3 reports a PL capture error')
    decision = rows[:, 3]
    if np.any(decision >> 8):
        raise ProtocolError('SLC3 reserved decision bits')
    fallback = (decision & 128) != 0
    if np.any(fallback):
        if not np.all(decision == 128):
            raise ProtocolError('SLC3 fallback must cover the whole frame')
        return SegmentPacket(data, ridge)
    if not segment_configuration_supported(ridge.configuration):
        raise ProtocolError('RSG1 successful decisions require the fixed configuration')
    center, flags, _, row_fallback, fault, first, last, count, *_ = ridge.columns
    if np.any(row_fallback | fault | (count > 1)):
        raise ProtocolError('SLC3 primary decisions require single-component nonfault rows')
    reason = (decision >> 2) & 15
    accepted = (decision & 1) != 0
    retained = (decision & 2) != 0
    if (np.any((decision & 64) == 0) or np.any(reason > 11)
            or np.any(accepted != (reason == 0))
            or np.any(retained != ((reason == 0) | (reason >= 8)))):
        raise ProtocolError('SLC3 decision masks/reason mismatch')
    # Class/ridge priority can be checked locally without rerunning segments.
    expected = np.zeros(len(reason), np.uint32)
    for bit, code in ((2, 6), (1, 5), (8, 4), (4, 3), (16, 2)):
        expected[(flags & bit) != 0] = code
    expected[count == 0] = 1
    base_reject = expected != 0
    if np.any(base_reject & (reason != expected)):
        raise ProtocolError('SLC3 local classification/reason mismatch')
    ridge_reason = np.full(len(reason), 10, np.uint32)
    ridge_reason[(ridge.ridge_flags & 4) != 0] = 0
    ridge_reason[(ridge.ridge_flags & 2) == 0] = 9
    ridge_reason[(ridge.ridge_flags & 1) == 0] = 8
    eligible = ~base_reject
    allowed = (reason == 7) | (reason == ridge_reason) | ((ridge_reason == 0) & (reason == 11))
    if np.any(eligible & ~allowed):
        raise ProtocolError('SLC3 local ridge/reason mismatch')
    return SegmentPacket(data, ridge)


def model_slc3(raw, config, *, request_id, capture_epoch, width=752, height=480,
               force_fallback=False):
    """TESTONLY modeled SLC3 using the unchanged independent R13 consumer.

    Unsupported hardware configurations and multi/fault records emit benign
    whole-frame fallback. No bitstream, DMA, or optical provenance is implied.
    """
    from .ridge_candidates import select_ridge_candidates
    from .segment_candidates_r15 import fixed_segment_policy
    wire = model_slc2(raw, config, request_id=request_id, capture_epoch=capture_epoch,
                     width=width, height=height)
    ridge = decode_slc2(wire, frame_width=width, frame_height=height)
    rows = np.frombuffer(wire, '<u4')[32:].reshape(-1, 3)
    output = np.empty((len(rows), 4), dtype='<u4')
    output[:, :3] = rows
    if (force_fallback or not segment_configuration_supported(config)
            or not segment_configuration_supported(ridge.configuration)
            or np.any(ridge.columns[3])):
        output[:, 3] = 128
    else:
        gray = np.frombuffer(raw, np.uint8).reshape(height, width)
        selected, _ = select_ridge_candidates(ridge, gray, config, fixed_segment_policy())
        y0, y1 = ridge.configuration.roi[1::2]
        reasons = np.asarray([SEGMENT_REASONS.index(reason) for reason in selected.reasons[y0:y1]], np.uint32)
        output[:, 3] = (64 | selected.accepted[y0:y1].astype(np.uint32)
            | (selected.retained[y0:y1].astype(np.uint32) << 1) | (reasons << 2))
    header = np.frombuffer(wire, '<u4', count=32).copy()
    header[0], header[2], header[30] = SLC3_MAGIC, 32+4*len(rows), RSG1
    result = header.tobytes()+output.tobytes()
    decode_slc3(result, frame_width=width, frame_height=height,
                request_id=request_id, capture_epoch=capture_epoch)
    return result


class SegmentPynqTransport(RidgePynqTransport):
    """Explicit manifest/artifact/capability-bound mode4; modes0..3 retained."""
    def _segment_idle_handoff_state(self):
        """Keep the original handoff guards, allowing sticky's ready=0 only.

        RSG1 intentionally deasserts the existing core-ready bit on a sticky
        fault. A reset-specific idle check must not require that bit to be 1.
        Buffers, DMA, recovery, busy/error and stable identity checks remain.
        """
        before_status = int(self._diag_mmio.read(0x158))
        if before_status != 2:
            return self._handoff_state()
        if (self._accepted_request_id is not None or self._accepted_epoch is not None
                or self._raw_buffer is not None or self._profile_buffer is not None
                or self.raw_dma.transfer_state is not None
                or (self.profile_dma is not None and self.profile_dma.transfer_state is not None)
                or self.recovery_required or self._recovery_token is not None):
            raise ProtocolError('RSG1 fault reset requires no in-flight buffers or recovery token')
        def read():
            return dict(status=int(self.mmio.read(REG_STATUS)),
                control_id=int(self.mmio.read(REG_PROFILE_ID)),
                control_error=int(self.mmio.read(REG_ERROR_FLAGS)),
                **{key:int(self._diag_mmio.read(DIAG_OFFSETS[key])) for key in
                   ('ready','request_id','epoch','error','row_count','frame_count')})
        state = read()
        if state != read() or int(self._diag_mmio.read(0x158)) != before_status:
            raise ProtocolError('RSG1 fault reset hardware snapshot changed while reading')
        if (state['status'] & 5 or state['ready'] & 5 or state['error']
                or state['control_error'] or state['request_id'] != state['control_id']):
            raise ProtocolError('RSG1 fault reset requires stable idle error-free matching hardware identity')
        return state

    def _verify_segment_contract(self, manifest_path, *, workers_joined):
        """Read-only complete artifact/manifest/capability proof, even faulted."""
        manifest = Path(manifest_path)
        record = json.loads(manifest.read_text(encoding='utf8'))
        for key, contract in (('row_feature_dma', FEATURE_DMA_CONTRACT),
                ('compact_policy_dma', COMPACT_DMA_CONTRACT),
                ('ridge_policy_dma', RIDGE_DMA_CONTRACT),
                ('segment_policy_dma', SEGMENT_DMA_CONTRACT)):
            declaration = record.get(key)
            if (not isinstance(declaration, dict) or declaration != contract
                    or any(type(declaration[k]) is not type(v) for k, v in contract.items())):
                raise CapabilityError('RSG1 manifest declaration mismatch: '+key)
        if workers_joined is not True:
            raise ProtocolError('RSG1 requires joined workers')
        verify_overlay_artifacts(self.bit_path, self.hwh_path, manifest_path)
        binding = tuple(hashlib.sha256(Path(p).read_bytes()).hexdigest()
                        for p in (self.bit_path, self.hwh_path))
        if binding != self._feature_artifact_binding:
            raise CapabilityError('RSG1 artifacts changed after admission')
        parameters = parse_hwh_parameters(self.hwh_path)
        if (any(_parameter_int(parameters, suffix) != 1 for suffix in
                ('.FEATURE_DMA_ENABLE', '.COMPACT_POLICY_ENABLE')) or self.profile_dma is None):
            raise CapabilityError('RSG1 HWH/DMA mismatch')
        # All capability/state failures occur before writes or mode receipts.
        for offset, expected in ((0x134, 0x52464431), (0x138, SLC1_ABI),
                (0x140, 0x52435131), (0x144, SLC1_ABI), (0x148, RPG1),
                (0x14c, SLC1_ABI), (0x150, RSG1), (0x154, SLC1_ABI)):
            if int(self._diag_mmio.read(offset)) != expected:
                raise CapabilityError('RSG1 dependency capability/ABI mismatch')
        self._segment_idle_handoff_state()
        receipt = _FeatureDMAAdmission(binding, hashlib.sha256(manifest.read_bytes()).hexdigest())
        return receipt

    def admit_segment_dma(self, manifest_path, *, workers_joined=True):
        receipt = self._verify_segment_contract(manifest_path, workers_joined=workers_joined)
        self._segment_capability_receipt = receipt
        self._segment_manifest_path = Path(manifest_path).resolve()
        self._require_segment_ready()
        self._segment_dma_receipt = receipt
        self._compact_dma_receipt = receipt
        self._ridge_dma_receipt = receipt
        self.set_feature_mode(4, workers_joined=True)
        return True

    def admit_ridge_dma(self, manifest_path, *, workers_joined=True):
        """R15 bench integration alias: admits mode4, never silently mode3."""
        return self.admit_segment_dma(manifest_path, workers_joined=workers_joined)

    def _require_segment_ready(self):
        if int(self._diag_mmio.read(0x158)) != 1:
            raise ProtocolError('RSG1 is busy or faulted; explicit idle reset required for sticky fault')

    @property
    def segment_policy_dma_enabled(self):
        proof = getattr(self, '_segment_dma_receipt', None)
        return (isinstance(proof, _FeatureDMAAdmission)
            and proof.artifact_binding == self._feature_artifact_binding
            and getattr(self, '_feature_dma_mode', 0) == 4)

    @property
    def ridge_policy_dma_enabled(self):
        return self.segment_policy_dma_enabled or super().ridge_policy_dma_enabled

    @property
    def compact_policy_dma_enabled(self):
        return self.segment_policy_dma_enabled or super().compact_policy_dma_enabled

    def set_feature_mode(self, mode, *, workers_joined):
        if mode != 4:
            return super().set_feature_mode(mode, workers_joined=workers_joined)
        if type(mode) is not int or workers_joined is not True:
            raise ProtocolError('RSG1 mode requires joined workers')
        proof = getattr(self, '_segment_dma_receipt', None)
        if (not isinstance(proof, _FeatureDMAAdmission)
                or proof.artifact_binding != self._feature_artifact_binding):
            raise CapabilityError('RSG1 mode is not artifact admitted')
        self._handoff_state()
        self._require_segment_ready()
        self._feature_mode_restore_required = True
        self._diag_mmio.write(0x13c, 4)
        if int(self._diag_mmio.read(0x13c)) != 4:
            raise ProtocolError('RSG1 mode write rejected')
        self._feature_dma_mode = 4

    def reset_segment_fault(self, *, workers_joined, manifest_path=None):
        """Operator/runtime explicit reset only; never called by admission/recovery."""
        path = manifest_path if manifest_path is not None else getattr(self, '_segment_manifest_path', None)
        if path is None:
            raise CapabilityError('RSG1 explicit reset requires its complete manifest path')
        proof = self._verify_segment_contract(path, workers_joined=workers_joined)
        status = int(self._diag_mmio.read(0x158))
        if status & ~7 or status & 4:
            raise ProtocolError('RSG1 reset requires idle engine')
        self._diag_mmio.write(0x15c, 1)
        self._require_segment_ready()
        self._segment_capability_receipt = proof
        self._segment_manifest_path = Path(path).resolve()

    def arm_profile(self, size):
        if not self.segment_policy_dma_enabled:
            return super().arm_profile(size)
        # Bypass only FeaturePynqTransport's fixed SLC1 allocation choice.
        from .stream_capture import FeaturePynqTransport
        return super(FeaturePynqTransport, self).arm_profile(SLC3_MAX_BYTES)

    def _profile_receive_limit(self):
        return SLC3_MAX_BYTES if self.segment_policy_dma_enabled else super()._profile_receive_limit()

    def start_capture(self, *, raw, profile):
        if self.segment_policy_dma_enabled:
            self._require_segment_ready()
        return super().start_capture(raw=raw, profile=profile)

    def _diagnostics_for_payload(self, profile, diagnostics):
        if not self.segment_policy_dma_enabled or profile is None:
            return super()._diagnostics_for_payload(profile, diagnostics)
        started = perf_counter()
        self._require_segment_ready()
        packet = decode_slc3(profile, frame_width=self.frame_width, frame_height=self.frame_height,
            request_id=self._accepted_request_id, capture_epoch=self._accepted_epoch)
        if (packet.configuration != self._feature_configuration or packet.request_id != diagnostics.request_id
                or packet.epoch != diagnostics.epoch or int(packet.header[9]) != diagnostics.error
                or diagnostics.row_count != self.frame_height):
            raise ProtocolError('SLC3 accepted configuration/diagnostic mismatch')
        self._received_segment_packet = packet
        self.total_feature_decode_s = getattr(self, 'total_feature_decode_s', 0.)+perf_counter()-started
        return replace(diagnostics, rows=())

    def claim_compact_packet(self, payload, *, request_id, capture_epoch):
        if not self.segment_policy_dma_enabled:
            return super().claim_compact_packet(payload, request_id=request_id, capture_epoch=capture_epoch)
        packet = getattr(self, '_received_segment_packet', None)
        self._received_segment_packet = None
        if (not isinstance(packet, SegmentPacket) or packet.payload != payload
                or packet.request_id != request_id or packet.epoch != capture_epoch):
            raise CapabilityError('SLC3 requires admitted same-capture receive evidence')
        return packet


def capture_features_segments(controller, *, timeout_s=2.0, require_motion_metadata=True, timing_s=None):
    """Mode4 finite capture with the original START/wait/recovery/generation lifecycle.

    Uses the transport's validated same-capture receipt inside DMA recovery.
    This R15 entry changes only SLC3 size/type admission; legacy capture stays
    in stream_capture.capture_features and is not monkey-patched globally.
    """
    from .stream_capture import FeatureCapture, read_motion_metadata
    from .payload_digest import payload_sha256
    from ..motion.lifecycle import GatedCaptureTransport
    bound = (controller.transport._transport if isinstance(controller.transport, GatedCaptureTransport)
             else controller.transport)
    if not isinstance(bound, SegmentPynqTransport) or not bound.segment_policy_dma_enabled:
        raise CapabilityError('SLC3 capture requires admitted SegmentPynqTransport mode4')
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
            raise ProtocolError('SLC3 capture Raw length mismatch')
        if payload is None or transfer.profile_length != len(payload) or not 0 < len(payload) <= SLC3_MAX_BYTES:
            raise ProtocolError('SLC3 capture Profile length mismatch')
        packet = controller.transport.claim_compact_packet(payload, request_id=handle.request_id,
                                                          capture_epoch=handle.epoch)
        if timing_s is not None:
            now = perf_counter(); timing_s['profile_decode_s'] = now-started; started = now
        diagnostics = transfer.diagnostics or controller.transport.read_diagnostics()
        if (diagnostics.request_id != handle.request_id or diagnostics.epoch != handle.epoch
                or diagnostics.row_count != controller.frame_height or diagnostics.error
                or packet.header[9]):
            raise ProtocolError('SLC3 capture diagnostic identity/state mismatch')
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
        controller._recover_after_processing_error('continuous_feature_processing_error', exc, handle)
        raise
    finally:
        controller._finish_inflight()


