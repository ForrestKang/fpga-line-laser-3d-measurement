"""SLC2/RPG1 additive integer ridge policy; SLC1 remains a distinct ABI.

RPG1 transmits three local ridge facts only. V2 association, segment gates,
geometry and physical optical validity are not established by these bits.
"""
from dataclasses import dataclass, replace
from functools import cached_property, lru_cache
import hashlib,json
import struct
from pathlib import Path
from time import perf_counter
import numpy as np

from ..p2.errors import ProtocolError,CapabilityError
from .stream_compact import CompactPacket,decode_slc1,SLC1_MAGIC,SLC1_ABI,PSF1,NLQ1,SLC1_MAX_BYTES
from .stream_features import FeatureConfiguration

SLC2_MAGIC=0x534c4332
RPG1=0x52504731
RIDGE_DMA_CONTRACT=dict(enabled=True,format='SLC2',abi=0x10000,
    capability_magic=RPG1,capability_offset=0x148,abi_offset=0x14c,
    mode_offset=0x13c,mode_value=3,prefilter_policy=RPG1,
    flank_inner=4,flank_outer=11,min_contrast_dn=20,min_edge_drop_dn=8,
    center_policy='original_q12')

@dataclass(frozen=True)
class RidgePacket(CompactPacket):
    @cached_property
    def columns(self):
        # Payload is immutable bytes. Reuse its derived read-only vectors
        # across policy and quality stages without decoding the bitfields again.
        return tuple(np.frombuffer(value.tobytes(),dtype=value.dtype).reshape(value.shape)
                     for value in super().columns)

    @cached_property
    def ridge_flags(self):
        flags=(self.vectors[:,2]>>27)&7
        return np.frombuffer(flags.tobytes(),dtype=flags.dtype)

def decode_slc2(payload,*,frame_width,frame_height,request_id=None,capture_epoch=None):
    if not isinstance(payload,(bytes,bytearray,memoryview)):
        raise ProtocolError('SLC2 payload must be bytes-like')
    data=bytes(payload)
    # Capture admission and processing independently request the same wire
    # decode. Reuse only a fully validated immutable byte-keyed result. Two
    # entries bound retained payload storage; changed bytes/identity/geometry
    # cannot hit this cache. Unusual argument types keep the original checks.
    arguments=(frame_width,frame_height,request_id,capture_epoch)
    if (type(frame_width) is int and type(frame_height) is int
            and all(value is None or type(value) is int for value in arguments[2:])):
        return _decode_slc2_cached(data,*arguments)
    return _decode_slc2_wire(data,*arguments)


@lru_cache(maxsize=2,typed=True)
def _decode_slc2_cached(data,frame_width,frame_height,request_id,capture_epoch):
    return _decode_slc2_wire(data,frame_width,frame_height,request_id,capture_epoch)


def _decode_slc2_wire_reference(data,frame_width,frame_height,request_id,capture_epoch):
    """Original SLC2-to-SLC1 admission, retained as a differential oracle."""
    if len(data)<140 or len(data)>SLC1_MAX_BYTES or len(data)%4:
        raise ProtocolError('SLC2 length mismatch')
    words=np.frombuffer(data,'<u4')
    if words[0]!=SLC2_MAGIC or words[30]!=RPG1:
        raise ProtocolError('SLC2 magic/policy mismatch')
    local=words.copy();local[0]=SLC1_MAGIC;local[30]=PSF1
    if (len(local)-32)%3:
        raise ProtocolError('SLC2 row length mismatch')
    rows=local[32:].reshape(-1,3)
    if np.any(rows[:,2]>>30):raise ProtocolError('SLC2 reserved row bits')
    flags=(rows[:,2]>>27)&7
    count=(rows[:,1]>>20)&1023
    fault=(rows[:,0]&(1<<29))!=0
    applicable=(count==1)&~fault
    if np.any((~applicable)&(flags!=0)) or np.any(((flags&1)==0)&(flags!=0)):
        raise ProtocolError('SLC2 ridge flags without complete single support')
    rows[:,2]&=np.uint32(0x07ffffff)
    original=decode_slc1(local.tobytes(),frame_width=frame_width,frame_height=frame_height,
        request_id=request_id,capture_epoch=capture_epoch)
    return RidgePacket(data,original.request_id,original.epoch,original.configuration)


def _median2_uint(values):
    # All decoded fields are uint32 <=1023. The two central integers sum to
    # exactly the former np.median(values)*2, without a float reduction.
    n=len(values)
    if not n:return 0
    lower,upper=(n-1)//2,n//2
    middle=np.partition(values,(lower,upper))
    return int(middle[lower])+int(middle[upper])


def _decode_slc2_wire(data,frame_width,frame_height,request_id,capture_epoch):
    # Preserve unusual scalar comparison/type behavior in the original path.
    # Ordinary cold admission never delegates or constructs a rewritten SLC1.
    if (type(frame_width) is not int or type(frame_height) is not int
            or any(value is not None and type(value) is not int
                   for value in (request_id,capture_epoch))):
        return _decode_slc2_wire_reference(data,frame_width,frame_height,request_id,capture_epoch)
    if len(data)<140 or len(data)>SLC1_MAX_BYTES or len(data)%4:
        raise ProtocolError('SLC2 length mismatch')
    h=struct.unpack_from('<32I',data)
    if h[0]!=SLC2_MAGIC or h[30]!=RPG1:
        raise ProtocolError('SLC2 magic/policy mismatch')
    if (len(data)//4-32)%3:
        raise ProtocolError('SLC2 row length mismatch')
    w=np.frombuffer(data,'<u4',offset=128).reshape(-1,3)
    if np.any(w[:,2]>>30):raise ProtocolError('SLC2 reserved row bits')
    ridge_flags=(w[:,2]>>27)&7
    count=(w[:,1]>>20)&1023
    fault=(w[:,0]&(1<<29))!=0
    applicable=(count==1)&~fault
    if np.any(((~applicable)|((ridge_flags&1)==0))&(ridge_flags!=0)):
        raise ProtocolError('SLC2 ridge flags without complete single support')

    # Same ordered SLC1 admission stages and messages; only magic/policy and
    # the three explicitly admitted ridge bits differ on the SLC2 wire.
    if (h[1]!=SLC1_ABI or h[2]*4!=len(data)
            or not 1<=h[10]<=480 or h[2]!=32+3*h[10]):
        raise ProtocolError('SLC1 magic/ABI/word count mismatch')
    if (not 1<=frame_width<=752 or not 1<=frame_height<=480
            or h[11:13]!=(frame_width,frame_height)):
        raise ProtocolError('SLC1 frame geometry mismatch')
    if ((request_id is not None and h[3]!=request_id)
            or (capture_epoch is not None and h[4]!=capture_epoch)):
        raise ProtocolError('SLC1 capture identity/epoch mismatch')
    if (h[4]>>16 or h[9]>>16 or h[13]>>8 or h[14]>>20 or h[15]>>20
            or h[16]>>20 or h[17]>>18 or h[18]!=((255<<8)|(h[18]&255))
            or h[19]!=NLQ1 or h[31]):
        raise ProtocolError('SLC1 reserved bits/policy mismatch')
    x0,x1=h[14]&1023,h[14]>>10
    y0,y1=h[15]&1023,h[15]>>10
    lo,hi=h[16]&1023,h[16]>>10
    if (not 0<=x0<x1<=frame_width or not 0<=y0<y1<=frame_height
            or h[10]!=y1-y0 or h[20]!=h[10] or not 1<=lo<=hi<=1023):
        raise ProtocolError('SLC1 configuration bounds mismatch')
    cfg=FeatureConfiguration(frame_width,frame_height,h[13],(x0,y0,x1,y1),
                             lo,hi,h[17],h[18]&255,255)
    packet=RidgePacket(data,h[3],h[4],cfg)
    if np.any((w[:,0]>>30)|(w[:,1]>>30)):
        raise ProtocolError('SLC1 reserved row bits')
    center,flags,valid,fallback,fault,first,last,count,width,rawpeak,peak,sat=packet.columns
    single=(count==1)&~fault
    positive=count>0
    invalid=(fallback!=((count>1)|fault))|((~single)&((center!=0)|(flags!=0)))
    invalid|=~positive&((first!=0)|(last!=0)|(width!=0)|(peak!=0)|sat|valid|(rawpeak>h[13]))
    invalid|=positive&((first<x0)|(last>=x1)|(first>last)
        |(width!=last-first+1)|(count>(width+1)//2)|(peak==0)
        |(rawpeak<=h[13])|(peak>255-h[13])|(peak!=rawpeak-h[13]))
    invalid|=positive&(sat!=(rawpeak==255))
    invalid|=single&((center<(first.astype(np.int64)<<12))
        |(center>(last.astype(np.int64)<<12)))
    if np.any(invalid):raise ProtocolError('SLC1 support/fallback/center mismatch')
    expected=(((width<lo)|(width>hi)).astype(np.uint32)
        |(sat.astype(np.uint32)<<2)
        |(((first==x0)|(last==x1-1)).astype(np.uint32)<<3))
    if np.any((single&((flags&13)!=expected))
            |(single&(peak<max(1,h[18]&255))&((flags&2)==0))
            |(valid!=(single&((flags&19)==0)))):
        raise ProtocolError('SLC1 preclassification mismatch')
    n=y1-y0
    valid_width=width[valid]
    if (h[8]!=np.count_nonzero(valid) or h[23]!=np.count_nonzero(count>1)
            or h[22]!=np.count_nonzero(rawpeak>=250)
            or not h[22]<=h[21]<=h[22]*(x1-x0)
            or h[24]!=_median2_uint(rawpeak) or h[25]!=_median2_uint(peak[valid])
            or h[26]!=_median2_uint(valid_width)
            or h[27]!=(int(valid_width.max()) if len(valid_width) else 0)
            or h[29]!=np.count_nonzero(single&~fallback&(flags==0))):
        raise ProtocolError('SLC1 quality summary/vector mismatch')
    status=(1 if h[21]*100>n*(x1-x0) or h[22]*20>n else
            2 if h[23]*20>n or h[27]>32 else
            3 if h[8]==0 and h[24]<120 else
            4 if h[8]*5<n*4 else 3 if h[25]<120 else 0)
    if h[28]!=status:raise ProtocolError('SLC1 NLQ1 status mismatch')
    return packet

def _expected_flags(packet,gray):
    from ..stripe_v1.selection_v4 import ridge_integer_values
    if gray.dtype!=np.uint8 or gray.shape!=(packet.configuration.height,packet.configuration.width):
        raise ProtocolError('RPG1 raw geometry mismatch')
    x0,y0,x1,y1=packet.configuration.roi
    _,_,_,_,fault,first,last,count,*_=packet.columns
    result=np.zeros(y1-y0,np.uint32)
    for i in np.flatnonzero((count==1)&~fault):
        s,e=int(first[i]),int(last[i]);row=gray[y0+int(i)]
        values=ridge_integer_values(row,s,e,x0,x1)
        if values.window_complete:
            peak=int(row[s:e+1].max())
            contrast=values.contrast8>=160
            shape=(e-s+1>=3 and peak-int(row[s])>=8 and peak-int(row[e])>=8
                   and int(row[s+1:e].max(initial=0))==peak)
            result[i]=1|(int(contrast)<<1)|(int(shape)<<2)
    return result

def model_slc2(raw,config,*,request_id,capture_epoch,width=752,height=480):
    """Offline oracle, never a captured-FPGA provenance claim."""
    from .processed_replay import model_slc1
    payload=model_slc1(raw,config,request_id=request_id,capture_epoch=capture_epoch,width=width,height=height)
    packet=decode_slc1(payload,frame_width=width,frame_height=height)
    flags=_expected_flags(packet,np.frombuffer(raw,np.uint8).reshape(height,width))
    words=np.frombuffer(payload,'<u4').copy();words[0]=SLC2_MAGIC;words[30]=RPG1
    words[32:].reshape(-1,3)[:,2]|=flags<<27
    result=words.tobytes()
    decode_slc2(result,frame_width=width,frame_height=height,request_id=request_id,capture_epoch=capture_epoch)
    return result

def compare_ridge_raw(packet,gray):
    expected=_expected_flags(packet,gray)
    mismatch=np.flatnonzero(expected!=packet.ridge_flags)
    return dict(passed=not len(mismatch),checked_rows=len(expected),
        mismatching_source_rows=(mismatch+packet.configuration.roi[1]).tolist(),
        expected_flags=expected.tolist(),observed_flags=packet.ridge_flags.tolist())

from .stream_capture import FeaturePynqTransport,_FeatureDMAAdmission

class RidgePynqTransport(FeaturePynqTransport):
    """Explicit manifest/artifact/capability-bound mode3, legacy modes retained."""
    def admit_ridge_dma(self,manifest_path,*,workers_joined=True):
        declaration=json.loads(Path(manifest_path).read_text(encoding='utf8')).get('ridge_policy_dma')
        if (declaration!=RIDGE_DMA_CONTRACT or not isinstance(declaration,dict)
                or any(type(declaration[k]) is not type(v) for k,v in RIDGE_DMA_CONTRACT.items())):
            raise CapabilityError('RPG1 manifest declaration mismatch')
        if workers_joined is not True:raise ProtocolError('RPG1 requires joined workers')
        if not self.admit_compact_policy_dma(manifest_path,workers_joined=True):
            raise CapabilityError('RPG1 requires admitted compact DMA')
        if int(self._diag_mmio.read(0x148))!=RPG1 or int(self._diag_mmio.read(0x14c))!=0x10000:
            raise CapabilityError('RPG1 capability/ABI mismatch')
        self._ridge_dma_receipt=_FeatureDMAAdmission(self._feature_artifact_binding,
            hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest())
        self.set_feature_mode(3,workers_joined=True)
        return True

    @property
    def ridge_policy_dma_enabled(self):
        proof=getattr(self,'_ridge_dma_receipt',None)
        return (isinstance(proof,_FeatureDMAAdmission) and proof.artifact_binding==self._feature_artifact_binding
                and getattr(self,'_feature_dma_mode',0)==3)

    @property
    def compact_policy_dma_enabled(self):
        return self.ridge_policy_dma_enabled or super().compact_policy_dma_enabled

    def set_feature_mode(self,mode,*,workers_joined):
        if mode!=3:return super().set_feature_mode(mode,workers_joined=workers_joined)
        if type(mode) is not int or workers_joined is not True:
            raise ProtocolError('RPG1 mode requires joined workers')
        proof=getattr(self,'_ridge_dma_receipt',None)
        if not isinstance(proof,_FeatureDMAAdmission) or proof.artifact_binding!=self._feature_artifact_binding:
            raise CapabilityError('RPG1 mode is not artifact admitted')
        self._handoff_state();self._feature_mode_restore_required=True
        self._diag_mmio.write(0x13c,3)
        if int(self._diag_mmio.read(0x13c))!=3:raise ProtocolError('RPG1 mode write rejected')
        self._feature_dma_mode=3

    def _diagnostics_for_payload(self,profile,diagnostics):
        if not self.ridge_policy_dma_enabled:return super()._diagnostics_for_payload(profile,diagnostics)
        if profile is None:return super()._diagnostics_for_payload(profile,diagnostics)
        started=perf_counter()
        packet=decode_slc2(profile,frame_width=self.frame_width,frame_height=self.frame_height,
            request_id=self._accepted_request_id,capture_epoch=self._accepted_epoch)
        if (packet.configuration!=self._feature_configuration or packet.request_id!=diagnostics.request_id
                or packet.epoch!=diagnostics.epoch or int(packet.header[9])!=diagnostics.error
                or diagnostics.row_count!=self.frame_height):
            raise ProtocolError('SLC2 accepted configuration/diagnostic mismatch')
        self._received_compact_packet=packet
        self.total_feature_decode_s=getattr(self,'total_feature_decode_s',0.)+perf_counter()-started
        return replace(diagnostics,rows=())
