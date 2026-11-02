"""Generation-checked feature capture; no hardware imports until construction.

Fast reads address only the ten published row words, never the reserved AXI
holes. Raw/Profile share one START. Reference comparison is an explicit QA
operation and is never silently labelled passing when it was not run.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from contextlib import contextmanager
from functools import lru_cache
import hashlib
from time import perf_counter
from typing import Any
from pathlib import Path
import json
import numpy as np
from ..p2.contract import DIAG_OFFSETS,MAX_PROFILE_BYTES,MAX_SLP2_PACKET_BYTES
from ..p2.errors import ProtocolError,CapabilityError
from ..p2.types import DiagnosticRow,DiagnosticsSnapshot
from ..p2.pynq_transport import RUNTIME_CAPABILITY_BITS
from ..p2.pynq_transport import verify_overlay_artifacts, parse_hwh_parameters, _parameter_int
from ..p2.contract import (REG_ROI_START, REG_ROI_END, REG_THRESHOLD, REG_MIN_WIDTH,
                           REG_MAX_WIDTH, REG_MIN_SUM, REG_MIN_PEAK)
from ..motion.capture_handoff import HandoffPynqTransport
from ..motion.lifecycle import GatedCaptureTransport
from .stream_profile import decode_slp2_fast
from .payload_digest import payload_sha256
from .stream_features import (decode_slf1, FeaturePacket, FeatureConfiguration,
                              FEATURE_DMA_CONTRACT, SLF1_MAX_BYTES)
from .stream_compact import decode_slc1, CompactPacket, COMPACT_DMA_CONTRACT, SLC1_MAX_BYTES

META_MAGIC=0x43534d31
META_ABI=0x10000
_DIAG_FIELDS=tuple(name for name in DiagnosticsSnapshot.__dataclass_fields__ if name!='rows')
_ROW_SETTERS=tuple(getattr(DiagnosticRow,name).__set__ for name in DiagnosticRow.__dataclass_fields__)

@lru_cache(maxsize=8)
def _published_row_indices(count:int)->np.ndarray:
    indices=(DIAG_OFFSETS['row_table']//4+np.arange(count,dtype=np.intp)[:,None]*16
             +np.arange(10,dtype=np.intp)[None,:])
    # A bytes backing store cannot be made writable by a caller of the cache.
    return np.frombuffer(indices.tobytes(),dtype=np.intp).reshape(count,10)

def _diagnostic_rows(values:np.ndarray,request_id:int)->tuple[DiagnosticRow,...]:
    # Match R2's first error: it checks every identity before checking order.
    if np.any(values[:,9]!=request_id):raise ProtocolError('mixed profile identity in row table')
    if not np.array_equal(values[:,0],np.arange(len(values))):
        raise ProtocolError('row order or duplicate row mismatch')
    row_set,xc_set,s0_set,s1_set,peak_set,width_set,count_set,flags_set,q_set,valid_set,diag_set,id_set=_ROW_SETTERS
    allocate=object.__new__
    rows=[]
    for row,xc,s0,s1,peak,width,count,packed,diagnostics,profile_id in values.tolist():
        # DiagnosticRow has no constructor validation. Bound slot descriptors
        # produce the identical frozen standard record without keyword dispatch
        # or twelve repeated object.__setattr__ lookups per published row.
        item=allocate(DiagnosticRow)
        row_set(item,row);xc_set(item,xc);s0_set(item,s0);s1_set(item,s1)
        peak_set(item,peak);width_set(item,width);count_set(item,count)
        flags_set(item,(packed>>8)&0x7fff);q_set(item,packed&255)
        valid_set(item,bool(packed&(1<<23)));diag_set(item,diagnostics);id_set(item,profile_id)
        rows.append(item)
    return tuple(rows)

@contextmanager
def exclusive_capture_generation(runtime):
    """Retain the existing owner/generation barrier for finite feature batches."""
    session=runtime.session
    if not session._owner.acquire(blocking=False):
        raise RuntimeError('another task already owns the capture generation')
    begun=False
    try:
        if session._closed or session._poisoned:
            raise RuntimeError('capture session is closed or poisoned')
        if session._gate.active:raise RuntimeError('capture generation already active')
        session._gate.begin();begun=True
        yield
    finally:
        try:
            if begun and not session._gate.revoke(timeout_s=session.config.cleanup_timeout_s):
                session._poisoned=True
                raise RuntimeError('capture generation did not quiesce; retain hardware memory')
        finally:session._owner.release()

def read_row_diagnostics(mmio: Any, *, width: int, height: int,
                         include_rows: bool = True) -> DiagnosticsSnapshot:
    def rd(offset):return int(mmio.read(offset))&0xffffffff
    before={name:rd(DIAG_OFFSETS[name]) for name in _DIAG_FIELDS}
    if (before['width'],before['height'])!=(width,height):raise ProtocolError('diagnostic geometry mismatch')
    if before['row_count']>height:raise ProtocolError('diagnostic row count overflow')
    if before['error']:raise ProtocolError('diagnostic capture error')
    count=before['row_count'] if include_rows and before['capabilities']&RUNTIME_CAPABILITY_BITS['row_diagnostics'] else 0
    rows=()
    if count:
        # Indexed uint32 reads avoid the six reserved/SLVERR words per row.
        indices=_published_row_indices(count)
        array=getattr(mmio,'array',None)
        if array is None:
            values=np.asarray([[rd(int(index)*4) for index in row] for row in indices],np.uint32)
        else:values=np.asarray(array[indices],dtype=np.uint32).copy()
        rows=_diagnostic_rows(values,before['request_id'])
    after=tuple(rd(DIAG_OFFSETS[k]) for k in ('epoch','request_id','row_count','ready','error'))
    expected=tuple(before[k] for k in ('epoch','request_id','row_count','ready','error'))
    if after!=expected:raise ProtocolError('diagnostic generation changed while reading')
    before['ready']=bool(before['ready']&1)
    return DiagnosticsSnapshot(**before,rows=rows)

def read_motion_metadata(mmio:Any, *, request_id:int,capture_epoch:int,required:bool=True,
                         capability_confirmed:bool=False)->dict|None:
    """Read only after capability admission; old bit has no CSM1 metadata."""
    if capability_confirmed is not True:
        if required:raise CapabilityError('manifest admission is required before CSM1 MMIO access')
        return None
    rd=lambda offset:int(mmio.read(offset))&0xffffffff
    magic=rd(0x100)
    if magic!=META_MAGIC:
        if required:raise CapabilityError('CSM1 captured-frame motion metadata is unavailable')
        return None
    if rd(0x104)!=META_ABI or rd(0x108)&15!=15:raise CapabilityError('CSM1 ABI/capabilities mismatch')
    before=(rd(0x10c),rd(0x110),rd(0x114),rd(0x12c))
    if before[0]&7!=3:raise ProtocolError('CSM1 frame metadata is not complete/idle')
    if before[1:3]!=(request_id,capture_epoch):raise ProtocolError('CSM1 capture identity mismatch')
    motor_id,motor_epoch,emitted=rd(0x118),rd(0x11c),rd(0x120)
    low,high=rd(0x124),rd(0x128);clock=rd(0x130)
    after=(rd(0x10c),rd(0x110),rd(0x114),rd(0x12c))
    if after!=before:raise ProtocolError('CSM1 generation changed while reading')
    if clock!=100000000:raise ProtocolError('CSM1 clock mismatch')
    return dict(request_id=request_id,capture_epoch=capture_epoch,motor_accepted_id=motor_id,
                motor_epoch=motor_epoch,emitted_command_count=emitted,fv_ticks=low|(high<<32),
                admitted_event_count=before[3],clock_hz=clock,
                angle_source='command_pulses_not_encoder',exposure_timing_validated=False)


def read_compact_diagnostic_extension(mmio, *, height, request_id, capture_epoch, capability_confirmed=False):
    """Only admitted RCQ1 callers may read published slot words 10..12."""
    if capability_confirmed is not True:
        raise CapabilityError('RCQ1 admission required before extended diagnostic access')
    def identity():
        return tuple(int(mmio.read(DIAG_OFFSETS[k])) for k in ('request_id','epoch','row_count','ready','error'))
    before = identity()
    # Status bit0=ready, bit1=pclk_valid, bit2=busy (RTL register 0x08).
    # Completed frames can legitimately report 1 or 3. Retain the complete
    # status in the stability snapshot; reject busy and reserved bits.
    status = before[3]
    if (before[:3] != (request_id,capture_epoch,height) or before[4] != 0
            or status & ~7 or not status & 1 or status & 4):
        raise ProtocolError('compact QA diagnostic identity/state mismatch')
    base = DIAG_OFFSETS['row_table']//4
    indices = base+np.arange(height,dtype=np.intp)[:,None]*16+np.arange(10,13,dtype=np.intp)[None,:]
    array = getattr(mmio,'array',None)
    values = (np.asarray(array[indices],dtype=np.uint32).copy() if array is not None else
              np.asarray([[int(mmio.read(int(i)*4)) for i in row] for row in indices],np.uint32))
    if identity() != before:
        raise ProtocolError('compact QA diagnostic generation changed while reading')
    return values

@dataclass(frozen=True)
class _FeatureDMAAdmission:
    artifact_binding: tuple[str, str]
    manifest_sha256: str


class FeaturePynqTransport(HandoffPynqTransport):
    """Negotiate SLF1 while retaining the original DMA lifecycle and defaults."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._feature_artifact_binding = tuple(hashlib.sha256(path.read_bytes()).hexdigest()
                                               for path in (self.bit_path, self.hwh_path))

    def admit_row_feature_dma(self, manifest_path, *, workers_joined: bool = True, enable: bool = True):
        # No new MMIO is touched for a manifest-declared legacy release.
        record = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        declaration = record.get('row_feature_dma')
        if declaration is None or declaration == {'enabled': False}:
            return False
        if (declaration != FEATURE_DMA_CONTRACT or not isinstance(declaration, dict)
                or any(type(declaration[k]) is not type(v) for k, v in FEATURE_DMA_CONTRACT.items())):
            raise CapabilityError('row feature DMA manifest contract mismatch')
        verify_overlay_artifacts(self.bit_path, self.hwh_path, manifest_path)
        binding = tuple(hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in (self.bit_path, self.hwh_path))
        if binding != getattr(self, '_feature_artifact_binding', None):
            raise CapabilityError('row feature DMA artifacts changed after transport admission')
        if _parameter_int(parse_hwh_parameters(self.hwh_path), '.FEATURE_DMA_ENABLE') != 1:
            raise CapabilityError('row feature DMA HWH parameter mismatch')
        if self.profile_dma is None:
            raise CapabilityError('row feature DMA requires the existing Profile S2MM')
        if workers_joined is not True or type(enable) is not bool:
            raise ProtocolError('row feature mode requires joined capture workers')
        self._handoff_state()
        if (int(self._diag_mmio.read(0x134)) != 0x52464431
                or int(self._diag_mmio.read(0x138)) != 0x10000):
            raise CapabilityError('RFD1 capability/ABI mismatch')
        self._feature_dma_admitted = True
        self._feature_dma_receipt = _FeatureDMAAdmission(binding, hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest())
        self.set_row_feature_mode(enable, workers_joined=workers_joined)
        return True

    def admit_compact_policy_dma(self, manifest_path, *, workers_joined=True):
        record = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        declaration = record.get('compact_policy_dma')
        if declaration is None or declaration == {'enabled':False}:
            return False
        if (declaration != COMPACT_DMA_CONTRACT or not isinstance(declaration,dict)
                or any(type(declaration[k]) is not type(v) for k,v in COMPACT_DMA_CONTRACT.items())):
            raise CapabilityError('compact policy DMA manifest contract mismatch')
        verify_overlay_artifacts(self.bit_path,self.hwh_path,manifest_path)
        binding = tuple(hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (self.bit_path,self.hwh_path))
        if binding != self._feature_artifact_binding:
            raise CapabilityError('compact policy DMA artifacts changed after transport admission')
        parameters = parse_hwh_parameters(self.hwh_path)
        if (_parameter_int(parameters,'.FEATURE_DMA_ENABLE') != 1
                or _parameter_int(parameters,'.COMPACT_POLICY_ENABLE') != 1 or self.profile_dma is None):
            raise CapabilityError('compact policy DMA HWH/DMA mismatch')
        if workers_joined is not True:
            raise ProtocolError('compact policy mode requires joined workers')
        self._handoff_state()
        if int(self._diag_mmio.read(0x140)) != 0x52435131 or int(self._diag_mmio.read(0x144)) != 0x10000:
            raise CapabilityError('RCQ1 capability/ABI mismatch')
        self._compact_dma_receipt = _FeatureDMAAdmission(binding,hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest())
        self.set_feature_mode(2,workers_joined=True)
        return True

    @property
    def compact_policy_dma_enabled(self):
        receipt = getattr(self,'_compact_dma_receipt',None)
        return (isinstance(receipt,_FeatureDMAAdmission) and receipt.artifact_binding == self._feature_artifact_binding
                and getattr(self,'_feature_dma_mode',0) == 2)

    def set_feature_mode(self, mode, *, workers_joined):
        if type(mode) is not int or mode not in (0,1,2) or workers_joined is not True:
            raise ProtocolError('feature mode requires supported integer mode and joined workers')
        if mode == 2 and not isinstance(getattr(self,'_compact_dma_receipt',None),_FeatureDMAAdmission):
            raise CapabilityError('compact policy DMA is not admitted')
        if mode == 1 and not getattr(self,'_feature_dma_admitted',False):
            raise CapabilityError('row feature DMA is not admitted')
        if mode == 0 and not (getattr(self,'_feature_dma_admitted',False) or getattr(self,'_compact_dma_receipt',None)):
            raise CapabilityError('feature mode is not admitted')
        self._handoff_state()
        # A write may commit even if the following read raises or is stale.
        # Keep the obligation before touching hardware; enabled is not evidence
        # that a failed admission left mode zero.
        self._feature_mode_restore_required = True
        self._diag_mmio.write(0x13c,mode)
        if int(self._diag_mmio.read(0x13c)) != mode:
            raise ProtocolError('feature mode write was rejected')
        self._feature_dma_mode = mode
        self._feature_mode_restore_required = mode != 0

    @property
    def feature_mode_restore_required(self):
        return getattr(self,'_feature_mode_restore_required',False)

    @property
    def row_feature_dma_enabled(self):
        receipt = getattr(self, '_feature_dma_receipt', None)
        return (isinstance(receipt, _FeatureDMAAdmission)
                and receipt.artifact_binding == getattr(self, '_feature_artifact_binding', None)
                and getattr(self, '_feature_dma_admitted', False) and getattr(self, '_feature_dma_mode', False) == 1)

    def set_row_feature_mode(self, enabled: bool, *, workers_joined: bool):
        if type(enabled) is not bool or workers_joined is not True:
            raise ProtocolError('row feature mode requires a bool mode and joined workers')
        if not getattr(self, '_feature_dma_admitted', False):
            raise CapabilityError('row feature DMA is not admitted')
        self.set_feature_mode(int(enabled),workers_joined=workers_joined)

    def arm_profile(self, size):
        return super().arm_profile(SLC1_MAX_BYTES if self.compact_policy_dma_enabled else SLF1_MAX_BYTES if self.row_feature_dma_enabled else size)

    def _profile_receive_limit(self):
        return SLC1_MAX_BYTES if self.compact_policy_dma_enabled else SLF1_MAX_BYTES if self.row_feature_dma_enabled else super()._profile_receive_limit()

    def start_capture(self, *, raw, profile):
        if self.row_feature_dma_enabled or self.compact_policy_dma_enabled:
            rd = self.mmio.read
            start, end = int(rd(REG_ROI_START)), int(rd(REG_ROI_END))
            self._feature_configuration = FeatureConfiguration(self.frame_width, self.frame_height,
                int(rd(REG_THRESHOLD)), (start & 0xffff, start >> 16, end & 0xffff, end >> 16),
                int(rd(REG_MIN_WIDTH)), int(rd(REG_MAX_WIDTH)), int(rd(REG_MIN_SUM)), int(rd(REG_MIN_PEAK)), 255)
        return super().start_capture(raw=raw, profile=profile)

    def _diagnostics_for_payload(self, profile, diagnostics):
        if self.compact_policy_dma_enabled and profile is not None:
            started = perf_counter()
            packet = decode_slc1(profile,frame_width=self.frame_width,frame_height=self.frame_height,
                request_id=self._accepted_request_id,capture_epoch=self._accepted_epoch)
            if (packet.configuration != self._feature_configuration or packet.request_id != diagnostics.request_id
                    or packet.epoch != diagnostics.epoch or int(packet.header[9]) != diagnostics.error
                    or diagnostics.row_count != self.frame_height):
                raise ProtocolError('SLC1 accepted configuration/diagnostic header mismatch')
            self._received_compact_packet = packet
            self.total_feature_decode_s = getattr(self,'total_feature_decode_s',0.)+perf_counter()-started
            return replace(diagnostics,rows=())
        if not self.row_feature_dma_enabled or profile is None:
            return super()._diagnostics_for_payload(profile, diagnostics)
        started = perf_counter()
        packet = decode_slf1(profile, frame_width=self.frame_width, frame_height=self.frame_height,
                             request_id=self._accepted_request_id, capture_epoch=self._accepted_epoch)
        if packet.configuration != self._feature_configuration:
            raise ProtocolError('SLF1 configuration differs from accepted hardware snapshot')
        if (packet.request_id != diagnostics.request_id or packet.epoch != diagnostics.epoch
                or len(packet.rows) != diagnostics.row_count or packet.profile.error_flags != diagnostics.error):
            raise ProtocolError('SLF1 and diagnostic header mismatch')
        self._received_feature_packet = packet
        self.total_feature_decode_s = getattr(self, 'total_feature_decode_s', 0.0) + perf_counter() - started
        return replace(diagnostics, rows=packet.rows)

    def claim_feature_packet(self, payload, *, request_id, capture_epoch):
        """Consume evidence validated inside receive's DMA recovery boundary."""
        packet = getattr(self, '_received_feature_packet', None)
        self._received_feature_packet = None
        if (not self.row_feature_dma_enabled or not isinstance(packet, FeaturePacket)
                or packet.payload != payload or packet.request_id != request_id or packet.epoch != capture_epoch):
            raise CapabilityError('SLF1 needs admitted transport and validated same-capture receive evidence')
        return packet

    def claim_compact_packet(self, payload, *, request_id, capture_epoch):
        packet = getattr(self,'_received_compact_packet',None)
        self._received_compact_packet = None
        if (not self.compact_policy_dma_enabled or not isinstance(packet,CompactPacket)
                or packet.payload != payload or packet.request_id != request_id or packet.epoch != capture_epoch):
            raise CapabilityError('SLC1 requires admitted same-capture receive evidence')
        return packet

    def read_diagnostics(self):
        started=perf_counter()
        result=read_row_diagnostics(self._diag_mmio,width=self.frame_width,height=self.frame_height,
                                    include_rows=not (getattr(self,'row_feature_dma_enabled',False)
                                                      or getattr(self,'compact_policy_dma_enabled',False)))
        elapsed=perf_counter()-started
        if result.rows:
            self.total_row_table_full_reads = getattr(self, 'total_row_table_full_reads', 0) + 1
        self.last_read_diagnostics_s=elapsed
        self.total_read_diagnostics_s=getattr(self,'total_read_diagnostics_s',0.0)+elapsed
        self.mmio.epoch=result.epoch&0xffff;self.mmio._request_id=result.request_id
        return result

@dataclass(frozen=True)
class FeatureCapture:
    request_id:int
    epoch:int
    raw:bytes
    raw_sha256:str
    profile:Any
    diagnostics:DiagnosticsSnapshot
    motion_metadata:dict|None
    reference_comparison_performed:bool=False
    profile_payload:bytes=b''
    timing_s:dict[str,float]|None=None
    feature_packet:FeaturePacket|None=None
    row_table_full_reads:int|None=None
    compact_packet:CompactPacket|None=None
    # Optional R22 separate multi-candidate DMA payload. Empty on legacy modes.
    # Transport may populate this only after artifact/capability admission.
    pmc2_payload:bytes=b''

def capture_features(controller:Any, *, timeout_s:float=2.0,require_motion_metadata:bool=True,
                     timing_s:dict[str,float]|None=None)->FeatureCapture:
    """One finite same-frame pair, without per-frame software centroid recompute."""
    started=perf_counter() if timing_s is not None else 0.0
    diagnostic_before=getattr(controller.transport,'total_read_diagnostics_s',0.0) if timing_s is not None else 0.0
    feature_decode_before=getattr(controller.transport,'total_feature_decode_s',0.0) if timing_s is not None else 0.0
    row_reads_before = getattr(controller.transport, 'total_row_table_full_reads', 0)
    handle=controller._arm_and_start(raw=True,profile=True)
    if timing_s is not None:
        now=perf_counter();timing_s['arm_start_s']=now-started;started=now
    transfer=controller._wait_for_transfer(timeout_s)
    if timing_s is not None:
        now=perf_counter();timing_s['wait_transfer_inclusive_s']=now-started;started=now
        timing_s['diagnostic_read_nested_s']=getattr(controller.transport,'total_read_diagnostics_s',0.0)-diagnostic_before
        timing_s['feature_packet_decode_nested_s']=getattr(controller.transport,'total_feature_decode_s',0.0)-feature_decode_before
    try:
        raw=transfer.raw
        if raw is None or len(raw)!=controller.frame_width*controller.frame_height or transfer.raw_length!=len(raw):
            raise ProtocolError('feature capture raw length mismatch')
        payload=transfer.profile
        compact_dma = getattr(controller.transport,'compact_policy_dma_enabled',False) is True
        feature_dma = getattr(controller.transport, 'row_feature_dma_enabled', False) is True
        pmc2_dma = getattr(controller.transport,'pmc2_dma_enabled',False) is True
        bound_transport = (controller.transport._transport if isinstance(controller.transport, GatedCaptureTransport)
                           else controller.transport)
        if (feature_dma or compact_dma or pmc2_dma) and not isinstance(bound_transport, FeaturePynqTransport):
            raise CapabilityError('SLF1 requires an artifact-bound FeaturePynqTransport admission')
        if pmc2_dma:
            from candidate.pmc2_transport_r22 import Pmc2PynqTransport,PMC2_MAX_BYTES
            if not isinstance(bound_transport,Pmc2PynqTransport):
                raise CapabilityError('PMC2 requires its artifact-bound transport')
        limit = PMC2_MAX_BYTES if pmc2_dma else SLC1_MAX_BYTES if compact_dma else SLF1_MAX_BYTES if feature_dma else min(MAX_PROFILE_BYTES,MAX_SLP2_PACKET_BYTES)
        if payload is None or transfer.profile_length!=len(payload) or not 0<len(payload)<=limit:
            raise ProtocolError('feature capture Profile length mismatch')
        packet = controller.transport.claim_feature_packet(payload, request_id=handle.request_id,
                                                           capture_epoch=handle.epoch) if feature_dma else None
        compact = controller.transport.claim_compact_packet(payload,request_id=handle.request_id,capture_epoch=handle.epoch) if compact_dma else None
        pmc2 = controller.transport.claim_pmc2_packet(payload,request_id=handle.request_id,capture_epoch=handle.epoch) if pmc2_dma else None
        profile = None if compact is not None or pmc2 is not None else packet.profile if packet is not None else decode_slp2_fast(payload,frame_width=controller.frame_width,frame_height=controller.frame_height)
        if timing_s is not None:
            now=perf_counter();timing_s['profile_decode_s']=now-started;started=now
        if compact is None and pmc2 is None:
            if profile.format_version!=2 or profile.profile_id!=handle.request_id:raise ProtocolError('feature capture Profile identity mismatch')
            if profile.error_flags:raise ProtocolError('feature capture Profile reports a PL error')
        elif compact is not None and compact.header[9]:raise ProtocolError('SLC1 reports a PL error')
        diag=transfer.diagnostics or controller.transport.read_diagnostics()
        if packet is not None:
            if diag.rows and diag.rows != packet.rows:
                raise ProtocolError('SLF1 and published diagnostic rows differ')
            diag = replace(diag, rows=packet.rows)
        if timing_s is not None:
            now=perf_counter();timing_s['diagnostic_read_s']=now-started;started=now
        if diag.request_id!=handle.request_id or diag.epoch!=handle.epoch:raise ProtocolError('feature capture diagnostic identity mismatch')
        if (compact is None and pmc2 is None and len(diag.rows)!=controller.frame_height) or diag.row_count!=controller.frame_height:
            raise ProtocolError('feature capture lacks full row features')
        meta=read_motion_metadata(controller.transport._diag_mmio,request_id=handle.request_id,
                                  capture_epoch=handle.epoch,required=True,
                                  capability_confirmed=getattr(controller.transport,'frame_motion_metadata_supported',False)) if require_motion_metadata else None
        if pmc2 is not None:
            if meta is None:raise ProtocolError('PMC2 requires captured CSM1 motion metadata')
            meta.update(feature_config_revision=pmc2.header['cfg_version'],
                        exposure_revision=pmc2.header['exposure_version'],
                        stripe_config_fingerprint=bound_transport._pmc2_capture_fingerprint)
        if timing_s is not None:
            now=perf_counter();timing_s['motion_metadata_s']=now-started;started=now
        digest=payload_sha256(raw)
        if timing_s is not None:timing_s['raw_sha256_s']=perf_counter()-started
        return FeatureCapture(handle.request_id,handle.epoch,raw,digest,profile,diag,meta,
                              profile_payload=bytes(payload),timing_s=timing_s,feature_packet=packet,compact_packet=compact,
                              row_table_full_reads=getattr(controller.transport, 'total_row_table_full_reads', 0)-row_reads_before
                              if isinstance(bound_transport, FeaturePynqTransport) else None,
                              pmc2_payload=bytes(payload) if pmc2 is not None else b'')
    except Exception as exc:
        controller._recover_after_processing_error('continuous_feature_processing_error',exc,handle)
        raise
    finally:controller._finish_inflight()
