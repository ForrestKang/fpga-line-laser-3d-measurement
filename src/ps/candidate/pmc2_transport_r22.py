"""Explicit opt-in PMC2 transport, preserving existing DMA recovery/ownership.

Import/construction does not load a bitstream. Admission requires matching
bit/HWH/manifest, capability readback, idle camera and joined workers.
"""
from dataclasses import replace
from pathlib import Path
import hashlib
import json

from structured_light.scan3d.stream_capture import (FeaturePynqTransport,
    _FeatureDMAAdmission,read_row_diagnostics,read_motion_metadata)
from structured_light.p2.pynq_transport import verify_overlay_artifacts,parse_hwh_parameters,_parameter_int
from structured_light.p2.contract import (REG_ROI_START,REG_ROI_END,REG_THRESHOLD,
    REG_MIN_WIDTH,REG_MAX_WIDTH,REG_MIN_SUM,REG_MIN_PEAK)
from structured_light.p2.errors import CapabilityError,ProtocolError
from reference_candidate.multi_stripe import MultiPolicy
from candidate.pl_features_r22 import (MAGIC,VERSION,HEADER_BYTES,RECORD_BYTES,
    MAX_CANDIDATES,decode_packet)

PMC2_MAX_BYTES=HEADER_BYTES+MAX_CANDIDATES*RECORD_BYTES
PMC2_DMA_CONTRACT=dict(enabled=True,format='PMC2',abi=VERSION,mode_value=6,
    mode_offset=0x13c,capability_offset=0x7c,capability_magic=MAGIC,
    capability_region='pipeline_control',configuration_region='pipeline_control',
    mode_region='camera_diagnostics',
    max_packet_bytes=PMC2_MAX_BYTES,max_candidates_per_row=8,max_candidates=3840)


def _version(value):
    if type(value) is not int or not 0<=value<=0xffffffff:
        raise ProtocolError('PMC2 snapshot version must be uint32')
    return value


class Pmc2PynqTransport(FeaturePynqTransport):
    @property
    def pmc2_dma_enabled(self):
        receipt=getattr(self,'_pmc2_receipt',None)
        return (isinstance(receipt,_FeatureDMAAdmission) and
                receipt.artifact_binding==getattr(self,'_feature_artifact_binding',None)
                and getattr(self,'_feature_dma_mode',0)==6)

    def admit_pmc2_dma(self,manifest_path,*,stripe_config,cfg_version,exposure_version,workers_joined):
        record=json.loads(Path(manifest_path).read_text(encoding='utf8'))
        declaration=record.get('multi_candidate_dma')
        if declaration is None or declaration=={'enabled':False}:return False
        if (declaration!=PMC2_DMA_CONTRACT or not isinstance(declaration,dict)
                or any(type(declaration[k]) is not type(v) for k,v in PMC2_DMA_CONTRACT.items())):
            raise CapabilityError('PMC2 manifest declaration mismatch')
        if workers_joined is not True:raise ProtocolError('PMC2 admission requires joined workers')
        verify_overlay_artifacts(self.bit_path,self.hwh_path,manifest_path)
        binding=tuple(hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (self.bit_path,self.hwh_path))
        if binding!=self._feature_artifact_binding:
            raise CapabilityError('PMC2 artifacts changed after transport creation')
        parameters=parse_hwh_parameters(self.hwh_path)
        if (_parameter_int(parameters,'.MULTI_CANDIDATE_ENABLE')!=1
                or _parameter_int(parameters,'.FEATURE_DMA_ENABLE')!=1 or self.profile_dma is None):
            raise CapabilityError('PMC2 HWH parameter / Profile DMA mismatch')
        self._handoff_state()
        if int(self._control_raw_mmio.read(0x7c))!=MAGIC:
            raise CapabilityError('PMC2 capability readback mismatch')
        self._pmc2_receipt=_FeatureDMAAdmission(binding,hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest())
        self.configure_pmc2(stripe_config,cfg_version=cfg_version,exposure_version=exposure_version,workers_joined=True)
        self.set_feature_mode(6,workers_joined=True)
        return True

    def configure_pmc2(self,stripe_config,*,cfg_version,exposure_version,workers_joined):
        if not isinstance(getattr(self,'_pmc2_receipt',None),_FeatureDMAAdmission):
            raise CapabilityError('PMC2 configuration is not artifact-admitted')
        if workers_joined is not True:raise ProtocolError('PMC2 configuration requires drained capture workers')
        self._handoff_state();_version(cfg_version);_version(exposure_version)
        previous=getattr(self,'_pmc2_versions',None)
        if previous is not None and (cfg_version<previous[0] or exposure_version<previous[1]
                or (stripe_config.fingerprint!=self._pmc2_fingerprint and cfg_version<=previous[0])):
            raise ProtocolError('PMC2 immutable configuration/exposure versions must not regress or alias')
        p=MultiPolicy();rd=self.mmio.read
        start,end=int(rd(REG_ROI_START)),int(rd(REG_ROI_END))
        roi=(start&65535,start>>16,end&65535,end>>16)
        expected_roi=stripe_config.roi or (0,0,self.frame_width,self.frame_height)
        if (roi!=expected_roi or any(int(rd(reg))!=value for reg,value in (
                (REG_THRESHOLD,stripe_config.fixed_threshold),(REG_MIN_WIDTH,stripe_config.min_width),
                (REG_MAX_WIDTH,stripe_config.max_width),(REG_MIN_SUM,stripe_config.min_sum),
                (REG_MIN_PEAK,stripe_config.min_peak)))):
            raise CapabilityError('PMC2 configuration differs from actual pixel-processing registers')
        unsupported=int(stripe_config.threshold_mode!='fixed' or stripe_config.max_candidates_per_row!=8)
        settings={0x80:cfg_version,0x84:exposure_version,
                  0x88:p.min_contrast_dn|p.min_edge_drop_dn<<8|p.split_peak_drop_dn<<16,
                  0x8c:int(p.max_branch_step_px*4096),0x90:int(p.max_direction_residual_px*4096),
                  0x94:stripe_config.saturation_level|unsupported<<8}
        # Before the first write preserve a restore obligation, even if the
        # following readback fails after a successful hardware transaction.
        self._feature_mode_restore_required=True
        for reg,value in settings.items():
            self._control_raw_mmio.write(reg,value)
            if int(self._control_raw_mmio.read(reg))!=value:raise ProtocolError('PMC2 snapshot register write rejected')
        x0,y0,x1,y1=expected_roi
        self._pmc2_config=dict(width=self.frame_width,height=self.frame_height,
            roi_x0=x0,roi_y0=y0,roi_x1=x1,roi_y1=y1,threshold=stripe_config.fixed_threshold,
            min_width=stripe_config.min_width,max_width=stripe_config.max_width,
            min_sum=stripe_config.min_sum,min_peak=stripe_config.min_peak,
            saturation=stripe_config.saturation_level,quality_packed=settings[0x88],
            step_q12=settings[0x8c],residual_q12=settings[0x90])
        self._pmc2_versions=(cfg_version,exposure_version)
        self._pmc2_fingerprint=stripe_config.fingerprint
        self._pmc2_registers=settings
        self._pmc2_pixel_registers={REG_ROI_START:start,REG_ROI_END:end,
            REG_THRESHOLD:stripe_config.fixed_threshold,REG_MIN_WIDTH:stripe_config.min_width,
            REG_MAX_WIDTH:stripe_config.max_width,REG_MIN_SUM:stripe_config.min_sum,REG_MIN_PEAK:stripe_config.min_peak}

    def set_feature_mode(self,mode,*,workers_joined):
        if mode not in (0,6) or not getattr(self,'_pmc2_receipt',None):
            return super().set_feature_mode(mode,workers_joined=workers_joined)
        if type(mode) is not int or workers_joined is not True:
            raise ProtocolError('PMC2 mode requires integer and joined workers')
        self._handoff_state();self._feature_mode_restore_required=True
        self._diag_mmio.write(0x13c,mode)
        if int(self._diag_mmio.read(0x13c))!=mode:raise ProtocolError('PMC2 mode write rejected')
        self._feature_dma_mode=mode;self._feature_mode_restore_required=mode!=0

    def arm_profile(self,size):
        return super().arm_profile(PMC2_MAX_BYTES if self.pmc2_dma_enabled else size)

    def _profile_receive_limit(self):
        return PMC2_MAX_BYTES if self.pmc2_dma_enabled else super()._profile_receive_limit()

    def start_capture(self,*,raw,profile):
        if self.pmc2_dma_enabled:
            if raw is not True or profile is not True:raise ProtocolError('PMC2 requires same-START Raw and features')
            if int(self._diag_mmio.read(0x13c))!=6:
                raise ProtocolError('PMC2 feature mode changed before START')
            if any(int(self.mmio.read(k))!=v for k,v in self._pmc2_pixel_registers.items()):
                raise ProtocolError('PMC2 pixel-processing configuration changed before START')
            if any(int(self._control_raw_mmio.read(k))!=v for k,v in self._pmc2_registers.items()):
                raise ProtocolError('PMC2 configuration changed before START')
            self._pmc2_capture_config=dict(self._pmc2_config)
            self._pmc2_capture_versions=self._pmc2_versions
            self._pmc2_capture_fingerprint=self._pmc2_fingerprint
            self._received_pmc2=None
        return super().start_capture(raw=raw,profile=profile)

    def read_diagnostics(self):
        if not self.pmc2_dma_enabled:return super().read_diagnostics()
        result=read_row_diagnostics(self._diag_mmio,width=self.frame_width,height=self.frame_height,include_rows=False)
        self.mmio.epoch=result.epoch&0xffff;self.mmio._request_id=result.request_id
        return result

    def _diagnostics_for_payload(self,profile,diagnostics):
        if not self.pmc2_dma_enabled:return super()._diagnostics_for_payload(profile,diagnostics)
        if (profile is None or diagnostics.error or diagnostics.row_count!=self.frame_height
                or diagnostics.request_id!=self._accepted_request_id or diagnostics.epoch!=self._accepted_epoch):
            raise ProtocolError('PMC2 completion/diagnostic identity mismatch')
        meta=read_motion_metadata(self._diag_mmio,request_id=self._accepted_request_id,
            capture_epoch=self._accepted_epoch,required=True,
            capability_confirmed=getattr(self,'frame_motion_metadata_supported',False))
        cfg,exp=self._pmc2_capture_versions
        identity=dict(frame_id=self._accepted_request_id,epoch=self._accepted_epoch,cfg_version=cfg,
            exposure_version=exp,motor_id=meta['motor_accepted_id'],motor_epoch=meta['motor_epoch'],
            motor_steps=meta['emitted_command_count'],timestamp=meta['fv_ticks'])
        try:packet=decode_packet(profile,expected_identity=identity,expected_config=self._pmc2_capture_config)
        except ValueError as exc:raise ProtocolError('PMC2 DMA packet validation failed: '+str(exc)) from exc
        self._received_pmc2=(bytes(profile),packet)
        return replace(diagnostics,rows=())

    def claim_pmc2_packet(self,payload,*,request_id,capture_epoch):
        received=getattr(self,'_received_pmc2',None);self._received_pmc2=None
        if (not self.pmc2_dma_enabled or received is None or received[0]!=payload
                or received[1].header['frame_id']!=request_id or received[1].header['epoch']!=capture_epoch):
            raise CapabilityError('PMC2 requires admitted same-capture DMA receive evidence')
        return received[1]
