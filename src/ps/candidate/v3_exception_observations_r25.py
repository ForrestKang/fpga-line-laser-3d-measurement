"""Bounded CPU V3 archive decoding and observations; never a hardware packet."""
from collections import Counter
from dataclasses import fields
import hashlib
import io
import json
import zipfile
import numpy as np
from reference_candidate.multi_stripe import MultiProfile
from structured_light.scan3d.continuous_quality import FrameQuality
from .pfo3_r25 import REASON_NAMES,STATUS_NAMES,PFO3Error
from .v3_reference_r25 import V3Candidates,V3Config,V3Frame,V3Result

CPU_SOURCE_KEYS=('source_cpu_v3_npz','source_cpu_v3_manifest')
MAX_CPU_SOURCE_BYTES=4*1024*1024
MAX_CPU_MANIFEST_BYTES=256*1024
CPU_STATES=('CPU_V3_READY','CPU_REJECTED_K16_OVERFLOW','CPU_REJECTED_IMAGE_QUALITY')
BOOL_FIELDS={'center_valid','preview_usable','measurement_usable'}


def _json(data):
    if type(data) is not bytes or len(data)>MAX_CPU_MANIFEST_BYTES:raise PFO3Error('bounded immutable CPU manifest required')
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise PFO3Error('duplicate CPU manifest key')
            result[key]=value
        return result
    return json.loads(data,object_pairs_hook=pairs,parse_constant=lambda value:(_ for _ in ()).throw(PFO3Error('nonfinite CPU manifest')))


def decode_cpu_reference(npz,manifest):
    """Validate saved CPU arrays/scalars, without processing Raw pixels."""
    if type(npz) is not bytes or not 1<=len(npz)<=MAX_CPU_SOURCE_BYTES:raise PFO3Error('bounded immutable CPU NPZ required')
    info=_json(manifest);provenance=info['cpu_provenance']
    if (info.get('state') not in CPU_STATES or info.get('PL_success') is not False
            or info.get('backend')!='PS_V3_EXPLICIT_FALLBACK' or info.get('stop_required') is not False
            or provenance.get('PL_success') is not False or provenance.get('CPU_state')!=info['state']):
        raise PFO3Error('CPU source cannot claim hardware success or unsupported outcome')
    if provenance.get('CPU_result_source') not in ('ARM_V3_EXPLICIT_FALLBACK','MOCK_ONLY_ARM_V3'):
        raise PFO3Error('unknown CPU result provenance')
    config=V3Config(**provenance['config'])
    if (config.fingerprint!=provenance['config_fingerprint']
            or provenance.get('failed_parameter_fingerprint')!=config.fingerprint):raise PFO3Error('CPU config fingerprint mismatch')
    description=provenance['CPU_arrays']
    if description['bytes']!=len(npz) or description['sha256']!=hashlib.sha256(npz).hexdigest():raise PFO3Error('CPU NPZ source hash/length mismatch')
    frame_data=info['reference_frame']
    if frame_data!=provenance['CPU_frame']:raise PFO3Error('CPU frame summaries differ')
    frame=V3Frame(**frame_data)
    for field in fields(V3Frame):
        value=getattr(frame,field.name)
        if field.name in ('measurement_usable','preview_usable'):
            if type(value) is not bool:raise PFO3Error('CPU frame-use must be boolean')
        elif type(value) is not int or value<0:raise PFO3Error('invalid CPU frame scalar')
    if ((frame.width,frame.height)!=(752,480) or frame.raw_count>8192 or frame.slotted_count>7680
            or frame.measure_status>5 or frame.preview_status>5 or frame.overflow_rows>480):raise PFO3Error('CPU frame outside resource domain')
    names=[field.name for field in fields(V3Candidates)]
    shapes={**{'candidate_'+name:(frame.raw_count,) for name in names},
        'raw_count_by_row':(480,),'local_count_by_row':(480,),'slot_count_by_row':(480,),'row_data':(480,16)}
    with zipfile.ZipFile(io.BytesIO(npz)) as source_zip:
        entries=source_zip.infolist()
        if (len(entries)!=len(shapes) or len({item.filename for item in entries})!=len(entries)
                or {item.filename for item in entries}!={name+'.npy' for name in shapes}
                or any(item.compress_type!=zipfile.ZIP_STORED for item in entries)
                or sum(item.file_size for item in entries)>MAX_CPU_SOURCE_BYTES):raise PFO3Error('unsupported CPU NPZ members/compression/size')
    arrays={}
    with np.load(io.BytesIO(npz),allow_pickle=False) as archive:
        for name,shape in shapes.items():
            value=archive[name];dtype=np.dtype(bool if name.startswith('candidate_') and name[10:] in BOOL_FIELDS else np.int64)
            if value.shape!=shape or value.dtype!=dtype:raise PFO3Error('invalid CPU array schema: '+name)
            value.setflags(write=False);arrays[name]=value
    c=V3Candidates(**{name:arrays['candidate_'+name] for name in names});n=frame.raw_count
    if (not np.array_equal(c.candidate_id,np.arange(n)) or np.any(c.row<0) or np.any(c.row>=480)
            or np.any(np.diff(c.row)<0) or np.any(c.start<0) or np.any(c.end<c.start) or np.any(c.end>=752)
            or np.any(c.width!=c.end-c.start+1) or np.any(c.center_q12<0) or np.any(c.center_q12>=752*4096)):
        raise PFO3Error('CPU candidate identity/geometry mismatch')
    selected=(c.local_reasons==0)&(c.slot>=0)
    if np.any(c.slot[selected]>=16) or np.any(c.local_reasons<0) or np.any(c.local_reasons>127):
        raise PFO3Error('CPU local reason/slot domain mismatch')
    weight=c.sum_weight_q12;moment=c.sum_weight_x
    if (np.any(weight<0) or np.any(weight>c.width*(255*4096)) or np.any(moment<weight*c.start)
            or np.any(moment>weight*c.end) or np.any(c.center_valid&(weight==0))):
        raise PFO3Error('CPU integer weight/moment bounds')
    valid_center=np.flatnonzero(c.center_valid)
    if len(valid_center) and np.any((moment[valid_center]*4096)//weight[valid_center]!=c.center_q12[valid_center]):
        raise PFO3Error('CPU center differs from saved integer statistics')
    raw_counts=np.bincount(c.row,minlength=480);local_counts=np.bincount(c.row[c.local_reasons==0],minlength=480)
    slot_counts=np.bincount(c.row[selected],minlength=480)
    if (not np.array_equal(raw_counts,arrays['raw_count_by_row']) or not np.array_equal(local_counts,arrays['local_count_by_row'])
            or not np.array_equal(slot_counts,arrays['slot_count_by_row']) or np.any(slot_counts>16)
            or int(local_counts.sum())!=frame.local_count or int(slot_counts.sum())!=frame.slotted_count
            or not np.array_equal(arrays['row_data'][:,4],raw_counts) or not np.array_equal(arrays['row_data'][:,5],local_counts)):
        raise PFO3Error('CPU raw/local/slot counts differ')
    for mode,usable,reason,branch,column,count in (
            ('measurement',c.measurement_usable,c.measurement_reason,c.measurement_branch_id,6,frame.measurement_count),
            ('preview',c.preview_usable,c.preview_reason,c.preview_branch_id,7,frame.preview_count)):
        if (np.any(reason<0) or np.any(reason>=len(REASON_NAMES)) or np.any(usable!=(reason==0))
                or np.any(usable&(~c.center_valid|~selected)) or np.any(branch[usable]<0) or np.any(branch[usable]>=7680)
                or int(usable.sum())!=count or not np.array_equal(np.bincount(c.row[usable],minlength=480),arrays['row_data'][:,column])):
            raise PFO3Error('CPU policy arrays/count mismatch: '+mode)
    if frame.overflow_rows and (c.measurement_usable.any() or c.preview_usable.any() or frame.measurement_usable or frame.preview_usable):
        raise PFO3Error('CPU overflow must reject whole frame')
    if (info['measurement_allowed']!=bool(info['state']=='CPU_V3_READY' and frame.measurement_usable)
            or info['preview_allowed']!=bool(info['state']=='CPU_V3_READY' and frame.preview_usable)):
        raise PFO3Error('CPU outcome permissions differ from frame')
    return V3Result(c,arrays['raw_count_by_row'],arrays['local_count_by_row'],arrays['slot_count_by_row'],arrays['row_data'],frame,config.fingerprint),info


def takeover_sources(outcome):
    """Read bounded immutable coordinator artifacts for geometry and writer."""
    from .pfo3_exception_r25 import TakeoverOutcomeR25
    if not isinstance(outcome,TakeoverOutcomeR25) or outcome.state not in CPU_STATES or outcome.stop_required:
        raise PFO3Error('CPU takeover outcome cannot supply observations')
    def read(name,limit):
        with (outcome.archive_dir/name).open('rb') as stream:data=stream.read(limit+1)
        if len(data)>limit:raise PFO3Error('CPU takeover source too large: '+name)
        return data
    manifest=read('TAKEOVER_OUTCOME.json',MAX_CPU_MANIFEST_BYTES);info=_json(manifest)
    if info!=outcome.as_dict():raise PFO3Error('CPU outcome differs from coordinator archive')
    npz=read('CPU_V3_reference_arrays.npz',MAX_CPU_SOURCE_BYTES);result,_=decode_cpu_reference(npz,manifest)
    sources=dict(source_cpu_v3_npz=npz,source_cpu_v3_manifest=manifest,
        source_pfo3_payload=read('PL_final.pfo3',256+64*(8192+480)),
        source_pfo3_primary=read('PL_primary.bin',384+68*(8192+7680+480)))
    for name,key in (('PL_final.pfo3','source_pfo3_payload'),('PL_primary.bin','source_pfo3_primary')):
        expected=info['cpu_provenance']['files'][name]
        if expected['bytes']!=len(sources[key]) or expected['sha256']!=hashlib.sha256(sources[key]).hexdigest():
            raise PFO3Error('CPU failed source archive changed: '+name)
    return sources,result,info

class CPUReferenceObservationsR25:
    def __init__(self,result):self.result=result
    @staticmethod
    def _mode(mode):
        if mode not in ('measurement','preview'):raise PFO3Error('unknown CPU observation mode')
    def frame_usable(self,mode):
        self._mode(mode)
        return bool(getattr(self.result.frame,'measurement_usable' if mode=='measurement' else 'preview_usable'))
    def profile(self,mode,*,qualified_only=True):
        self._mode(mode);c=self.result.candidates
        selected=(c.local_reasons==0)&(c.slot>=0)
        if self.result.frame.overflow_rows:selected=np.zeros(len(c.row),bool)
        reasons=c.measurement_reason if mode=='measurement' else c.preview_reason
        usable=c.measurement_usable if mode=='measurement' else c.preview_usable
        branches=c.measurement_branch_id if mode=='measurement' else c.preview_branch_id
        branch=branches[selected].copy();branch[branch==65535]=-1
        return MultiProfile(c.row[selected],c.center_q12[selected],usable[selected],
            tuple(REASON_NAMES[int(code)] for code in reasons[selected]),branch,
            dict(width_px=c.width[selected],contrast_dn=c.contrast_q12[selected]/4096.,
                 edge_drop_dn=c.edge_drop_q12[selected]/4096.,peak_count=c.peak_count[selected],
                 start=c.start[selected],end=c.end[selected],source_candidate_id=c.candidate_id[selected]),
            dict(candidate_count=int(selected.sum()),accepted=int(usable[selected].sum()),
                 branch_policy='PS_V3_EXPLICIT_FALLBACK',ARM_image_algorithm_calls=1),
            source='PS_V3_EXPLICIT_FALLBACK')
    def quality(self,mode):
        self._mode(mode);f=self.result.frame;preview=mode=='preview'
        count=f.preview_rows if preview else f.measurement_rows
        peak=f.preview_median_peak_x2 if preview else f.measure_median_peak_x2
        width=f.preview_median_width_x2 if preview else f.measure_median_width_x2
        reason=self.result.candidates.preview_reason if preview else self.result.candidates.measurement_reason
        rejected=Counter(REASON_NAMES[int(code)] for code in reason if code)
        return FrameQuality(STATUS_NAMES[f.preview_status if preview else f.measure_status],count/f.height,
            peak/2 if count else None,width/2 if count else None,f.saturated_pixels/(f.width*f.height),
            f.ambiguous_rows/f.height,count,f.height,dict(rejected),True,
            int(np.count_nonzero(self.result.row_data[:,2])))
