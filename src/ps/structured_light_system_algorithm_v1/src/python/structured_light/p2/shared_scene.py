"""Bounded stop-and-wait explicitly admitted scenes in sealed ordinary RAM, not CMA."""
from __future__ import annotations
import hashlib
import json
import mmap
import os
import struct
import time

import numpy as np

ABI=1
MAGIC=b'SLSCN001'
RAW_SIZE=752*480
MAX_POINTS=250_000
METADATA_SIZE=8192
HEADER=struct.Struct('<8sIIIIQQI32s52x')
RESERVED_ZERO=bytes(52)
RAW_OFFSET=HEADER.size+METADATA_SIZE
POINTS_OFFSET=RAW_OFFSET+RAW_SIZE
MAPPED_SIZE=POINTS_OFFSET+MAX_POINTS*3*8
MAX_SEQUENCE=(1<<63)-1
LIVE_STATIC_TITLE='LIVE STATIC XYZ / HISTORICAL CALIBRATION / NO METROLOGY'


def check_sources(sources):
    if (not isinstance(sources,(tuple,list)) or not sources
            or any(not isinstance(source,str) or source not in ('replay','live_static') for source in sources)
            or len(set(sources))!=len(sources)):
        raise ValueError('explicit replay/live_static supported sources required')
    return tuple(sources)


def descriptor():
    return dict(abi=ABI,raw_size=RAW_SIZE,max_points=MAX_POINTS,
        metadata_size=METADATA_SIZE,mapped_size=MAPPED_SIZE)


def _reserved_zero(mapping):
    return mapping[HEADER.size-52:HEADER.size]==RESERVED_ZERO


def xyz_sha256(points):
    # Empty multidimensional memoryviews cannot be cast on every Python build.
    return hashlib.sha256(memoryview(points).cast('B') if len(points) else b'').hexdigest()


def check_descriptor(value):
    if not isinstance(value,dict):raise ValueError('shared scene descriptor required')
    for key,expected in descriptor().items():
        if type(value.get(key)) is not int or value[key]!=expected:
            raise ValueError('shared scene descriptor mismatch: '+key)


def _check_fd(fd):
    import fcntl
    if type(fd) is not int or fd<0 or os.fstat(fd).st_size!=MAPPED_SIZE:
        raise ValueError('invalid shared scene mapped size/descriptor')
    required=fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK
    if fcntl.fcntl(fd,fcntl.F_GET_SEALS)&required!=required:
        raise ValueError('shared scene size seals required')


def _create_memfd():
    import fcntl
    if os.name!='posix' or not hasattr(os,'memfd_create'):
        raise RuntimeError('shared scene requires Linux memfd_create')
    fd=os.memfd_create('sl_hdmi_scene',os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
    try:
        os.ftruncate(fd,MAPPED_SIZE)
        fcntl.fcntl(fd,fcntl.F_ADD_SEALS,fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK)
        return fd
    except BaseException:
        os.close(fd);raise


def check_metadata(value,supported_sources=('replay',)):
    supported_sources=check_sources(supported_sources)
    if not isinstance(value,dict) or value.get('source') not in supported_sources:
        raise ValueError('scene source not explicitly admitted')
    live_static=value['source']=='live_static'
    for key,maximum in (('request_id',(1<<32)-1),('capture_epoch',65535),
                        ('submission_sequence',MAX_SEQUENCE)):
        if type(value.get(key)) is not int or not 0<=value[key]<=maximum:
            raise ValueError('invalid scene identity '+key)
    if value['submission_sequence']==0:raise ValueError('positive scene submission sequence required')
    if not isinstance(value.get('raw_sha256'),str) or len(value['raw_sha256'])!=64:
        raise ValueError('scene raw SHA required')
    try:bytes.fromhex(value['raw_sha256'])
    except ValueError as exc:raise ValueError('invalid scene raw SHA') from exc
    for key in ('cloud_revision','preview_revision','preview_generation','status'):
        if key not in value:raise ValueError('missing scene '+key)
    if live_static:
        if (not isinstance(value['cloud_revision'],(tuple,list))
                or len(value['cloud_revision'])!=4
                or any(type(item) is not int for item in value['cloud_revision'][1:])):
            raise ValueError('live static scene revision required')
        if (list(value['cloud_revision'])!=['live_static',value['capture_epoch'],value['request_id'],value['submission_sequence']]
                or value['preview_revision'] is not None or value['preview_generation'] is not None):
            raise ValueError('live static scene revision mismatch')
        expected=dict(title=LIVE_STATIC_TITLE,raw_shape=[480,752],canvas_shape=[720,1280,3],
            source_bounds=[0,0,752,480],destination_bounds=[24,128,752,480],
            point_source='live_static_camera_section',accumulated=False,fused=False,
            axis_calibrated=False,timing_calibrated=False,volume_calibrated=False,
            physical_accuracy_verified=False,current_optical_validation_passed=False,
            historical_calibration=True,preview_axis_fingerprint=None,angle_source='unregistered')
        for key,wanted in expected.items():
            actual=value.get(key)
            if (actual!=wanted or (isinstance(wanted,bool) and actual is not wanted)
                    or (isinstance(wanted,list) and (not isinstance(actual,list)
                        or any(type(item) is not int for item in actual)))):
                raise ValueError('live static scene declaration/bounds mismatch: '+key)
        if (not isinstance(value.get('rejection_counts'),dict)
                or any(not isinstance(key,str) or type(count) is not int or count<0
                       for key,count in value['rejection_counts'].items())):
            raise ValueError('invalid live static rejection counts')
    elif (any(type(value[key]) is not int or not 0<=value[key]<=MAX_SEQUENCE
            for key in ('preview_revision','preview_generation'))
            or not isinstance(value['cloud_revision'],(tuple,list))
            or len(value['cloud_revision'])!=3
            or any(type(item) is not int for item in value['cloud_revision'][1:])
            or list(value['cloud_revision'])!=['replay',value['preview_generation'],value['preview_revision']]):
        raise ValueError('scene cloud revision/generation mismatch')
    status=value['status']
    if (not isinstance(status,dict) or status.get('source')!=('live_static' if live_static else 'replay')
            or status.get('mode')!=('STATIC' if live_static else 'REPLAY') or status.get('frame_number')!=value['request_id']
            or any(status.get(key) is not False for key in
                ('axis_calibrated','timing_calibrated','volume_calibrated'))
            or (live_static and (status.get('angle_deg') is not None
                or type(status.get('frame_number')) is not int))):
        raise ValueError('scene status/measurement gate mismatch')



class _MappedScene:
    def __init__(self,fd,access):
        self.fd=fd;self.mapping=None
        try:
            _check_fd(fd);self.mapping=mmap.mmap(fd,MAPPED_SIZE,access=access)
        except BaseException:
            os.close(fd);self.fd=None;raise

    def close(self):
        if self.mapping is not None:self.mapping.close();self.mapping=None
        if self.fd is not None:os.close(self.fd);self.fd=None


class SharedSceneWriter(_MappedScene):
    def __init__(self,fd,supported_sources=('replay',)):
        self.supported_sources=check_sources(supported_sources)
        super().__init__(fd,mmap.ACCESS_WRITE)
        self.sequence=0;self.pending=None;self._pending_refs=None
        self.mapping[:HEADER.size]=bytes(HEADER.size)

    @classmethod
    def create(cls,supported_sources=('replay',)):return cls(_create_memfd(),supported_sources)

    def prepare(self,raw,points,metadata):
        if self.pending is not None:raise RuntimeError('scene ACK required before reuse')
        check_metadata(metadata,self.supported_sources)
        if type(raw) is not bytes or len(raw)!=RAW_SIZE:
            raise ValueError('immutable complete native raw bytes required')
        if (not isinstance(points,np.ndarray) or points.ndim!=2 or points.shape[1]!=3
                or points.dtype!=np.dtype('<f8') or not points.flags.c_contiguous
                or not points.flags.aligned or points.flags.writeable
                or len(points)>MAX_POINTS or not np.isfinite(points).all()):
            raise ValueError('scene requires bounded immutable contiguous finite float64 XYZ')
        points_digest=xyz_sha256(points)
        if 'xyz_sha256' in metadata and metadata['xyz_sha256']!=points_digest:
            raise ValueError('conflicting scene XYZ SHA')
        metadata=dict(metadata,xyz_sha256=points_digest)
        encoded=json.dumps(metadata,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if not 0<len(encoded)<=METADATA_SIZE:raise ValueError('scene metadata exceeds bound')
        if self.sequence>=MAX_SEQUENCE:raise RuntimeError('scene sequence exhausted')
        _check_fd(self.fd)
        sequence=self.sequence+1;digest=hashlib.sha256(encoded).digest()
        identity=(MAGIC,ABI,len(points),len(encoded),RAW_SIZE,sequence,
                  metadata['request_id'],metadata['capture_epoch'],digest)
        # Control RPC follows publication; an unpublished mapping is never read.
        self.mapping[:HEADER.size]=bytes(HEADER.size)
        self.mapping[HEADER.size:HEADER.size+len(encoded)]=encoded
        self.mapping[RAW_OFFSET:RAW_OFFSET+RAW_SIZE]=raw
        target=np.ndarray(points.shape,np.dtype('<f8'),buffer=self.mapping,offset=POINTS_OFFSET)
        try:np.copyto(target,points,casting='no')
        finally:del target
        HEADER.pack_into(self.mapping,0,*identity)
        self.pending=(identity,metadata['submission_sequence'])
        self._pending_refs=(raw,points,encoded)
        return dict(descriptor(),sequence=sequence,points=len(points),metadata_bytes=len(encoded),
            request_id=metadata['request_id'],capture_epoch=metadata['capture_epoch'],
            metadata_sha256=digest.hex(),published_monotonic_s=time.monotonic())

    def acknowledge(self,result):
        if self.pending is None:raise ValueError('no pending scene')
        identity,submission=self.pending
        if (not isinstance(result,dict) or result.get('scene_transport')!='shared_scene_v1'
                or result.get('scene_sequence')!=identity[5]
                or type(result.get('scene_sequence')) is not int
                or result.get('scene_metadata_sha256')!=identity[8].hex()
                or result.get('request_id')!=identity[6] or result.get('capture_epoch')!=identity[7]
                or result.get('submission_sequence')!=submission
                or any(type(result.get(key)) is not int for key in
                    ('request_id','capture_epoch','submission_sequence'))
                or result.get('rendered') is not True or result.get('hdmi_acknowledged') is not True
                or HEADER.unpack_from(self.mapping)!=identity or not _reserved_zero(self.mapping)):
            raise ValueError('scene acknowledgement identity/sequence mismatch')
        metadata=json.loads(self._pending_refs[2])
        if metadata['source']=='live_static' and any(result.get(key)!=metadata[key] for key in
                ('source','raw_sha256','xyz_sha256','cloud_revision')):
            raise ValueError('live static acknowledgement content identity mismatch')
        for key in ('render_s','show_s','scene_owner_cpu_s','scene_owner_service_s'):
            if type(result.get(key)) not in (int,float) or not np.isfinite(result[key]) or result[key]<0:
                raise ValueError('invalid owner scene timing '+key)
        self.sequence=identity[5];self.pending=None;self._pending_refs=None


class SharedSceneReader(_MappedScene):
    def __init__(self,fd,supported_sources=('replay',)):
        self.supported_sources=check_sources(supported_sources)
        super().__init__(fd,mmap.ACCESS_READ)
        self.sequence=0
        self.last_result=None

    def present(self,command,renderer):
        started=time.monotonic();cpu=time.process_time()
        check_descriptor(command);_check_fd(self.fd)
        sequence=command.get('sequence');count=command.get('points');length=command.get('metadata_bytes')
        if (type(sequence) is not int or sequence!=self.sequence+1 or sequence>MAX_SEQUENCE
                or type(count) is not int or not 0<=count<=MAX_POINTS
                or type(length) is not int or not 0<length<=METADATA_SIZE):
            raise ValueError('invalid scene sequence/count/metadata size')
        if any(type(command.get(key)) is not int for key in ('request_id','capture_epoch')):
            raise ValueError('invalid scene command identity')
        published=command.get('published_monotonic_s')
        if type(published) not in (int,float) or not np.isfinite(published) or not 0<=published<=started:
            raise ValueError('invalid scene publication time')
        identity=HEADER.unpack_from(self.mapping)
        if not _reserved_zero(self.mapping):raise ValueError('scene reserved header bytes must be zero')
        encoded=self.mapping[HEADER.size:HEADER.size+length]
        digest=hashlib.sha256(encoded).digest()
        expected=(MAGIC,ABI,count,length,RAW_SIZE,sequence,
            command.get('request_id'),command.get('capture_epoch'),digest)
        if identity!=expected or command.get('metadata_sha256')!=digest.hex():
            raise ValueError('scene header/identity/revision mismatch')
        metadata=json.loads(encoded);check_metadata(metadata,self.supported_sources)
        if metadata['request_id']!=identity[6] or metadata['capture_epoch']!=identity[7]:
            raise ValueError('scene metadata identity mismatch')
        raw=memoryview(self.mapping)[RAW_OFFSET:RAW_OFFSET+RAW_SIZE]
        points=np.ndarray((count,3),np.dtype('<f8'),buffer=self.mapping,offset=POINTS_OFFSET)
        try:
            if hashlib.sha256(raw).hexdigest()!=metadata['raw_sha256']:
                raise ValueError('scene Raw SHA mismatch')
            if xyz_sha256(points)!=metadata.get('xyz_sha256'):
                raise ValueError('scene XYZ SHA mismatch')
            if not np.isfinite(points).all():raise ValueError('nonfinite scene XYZ')
            validated=time.monotonic()
            try:result=renderer.present(raw,points,metadata)
            except BaseException as exc:
                partial=getattr(exc,'receipt',None)
                if isinstance(partial,dict):
                    partial.update(scene_sequence=sequence,scene_metadata_sha256=digest.hex(),
                        request_id=metadata['request_id'],capture_epoch=metadata['capture_epoch'],
                        submission_sequence=metadata['submission_sequence'])
                raise
        finally:
            del points;raw.release()
        if HEADER.unpack_from(self.mapping)!=identity or not _reserved_zero(self.mapping):
            raise ValueError('scene modified before ACK')
        if result.get('hdmi_acknowledged') is not True or result.get('rendered') is not True:
            raise RuntimeError('scene render/present ACK missing')
        self.sequence=sequence
        result=dict(result,scene_transport='shared_scene_v1',scene_sequence=sequence,
            scene_metadata_sha256=digest.hex(),request_id=metadata['request_id'],
            xyz_sha256=metadata['xyz_sha256'],raw_sha256=metadata['raw_sha256'],
            source=metadata['source'],cloud_revision=metadata['cloud_revision'],
            capture_epoch=metadata['capture_epoch'],submission_sequence=metadata['submission_sequence'],
            scene_receive_validate_s=validated-started,scene_owner_service_s=time.monotonic()-started,
            scene_owner_cpu_s=time.process_time()-cpu,
            owner_cpu_scope='process_time all owner threads; shared-scene validation/render/present, excludes control JSON/reply/startup/close')
        self.last_result=result
        return result
