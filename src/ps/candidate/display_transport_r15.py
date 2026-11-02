"""Bounded display-only marker IPC; no PFO3 or geometry admission authority."""
from dataclasses import asdict
import json
import struct
import zlib
import numpy as np
from .display_compact_r14 import CompactPLAnnotations
from .display_pfo3_r25 import PLDisplayAnnotations, MAX_ANNOTATIONS

_HEADER=struct.Struct('<4sI3II')
_MAX_METADATA=4096
MAX_COMPACT_BYTES=_HEADER.size+_MAX_METADATA+MAX_ANNOTATIONS*12

# Native record fields, not preclassified marker groups.  Sampling and the
# expensive immutable group construction belong to the renderer consumer:
# pending display frames may be coalesced without doing that work in submit.
_NATIVE_HEADER=struct.Struct('<4sII')
_NATIVE_DTYPE=np.dtype([(key,'<u4') for key in ('row','start','center_q12','decisions')])
MAX_NATIVE_MARKER_BYTES=_NATIVE_HEADER.size+256+180480*_NATIVE_DTYPE.itemsize


def pack_native_markers(frame,index):
    """Validate the original immutable archive, then retain display fields only.

    Never trust caller-retained record/header views: a dataclass-replaced payload
    must still fail the exact full-source CRC/header checks before publication.
    The compact transport has an independent CRC over header plus all records.
    """
    from .display_process_r25 import _display_frame
    if type(frame.payload) is not bytes:raise ValueError('immutable display archive required')
    checked=_display_frame(memoryview(frame.payload),index)
    count=len(checked.records)
    packed=bytearray(_NATIVE_HEADER.size+256+count*_NATIVE_DTYPE.itemsize)
    body=memoryview(packed)[_NATIVE_HEADER.size:]
    body[:256]=frame.payload[:256]
    records=np.ndarray((count,),dtype=_NATIVE_DTYPE,buffer=packed,
        offset=_NATIVE_HEADER.size+256)
    for name in _NATIVE_DTYPE.names:records[name]=checked.records[name]
    _NATIVE_HEADER.pack_into(packed,0,b'SLN6',count,zlib.crc32(body))
    # A pending slot never aliases a producer-owned mutable workspace.
    return bytes(packed)


def unpack_native_markers(view,index,*,actual,evidence,max_points):
    """Rebuild a display-only deferred source; never grants geometry admission."""
    from types import MappingProxyType
    from .pfo3_r25 import HEADER_FIELDS,MAGIC,VERSION,FinalFrame,ROW_DTYPE
    from .display_pfo3_r25 import DeferredPLAnnotations
    if not _NATIVE_HEADER.size+256<=len(view)<=MAX_NATIVE_MARKER_BYTES:
        raise ValueError('native marker length')
    magic,count,crc=_NATIVE_HEADER.unpack_from(view)
    body=view[_NATIVE_HEADER.size:]
    if magic!=b'SLN6' or count>180480 or len(body)!=256+count*_NATIVE_DTYPE.itemsize:
        raise ValueError('native marker layout')
    if zlib.crc32(body)!=crc:raise ValueError('native marker CRC mismatch')
    h=dict(zip(HEADER_FIELDS,struct.unpack_from('<64I',body)))
    if ((h['magic'],h['version'],h['header_bytes'],h['record_bytes'],h['row_record_bytes'])!=(MAGIC,VERSION,256,64,64)
            or (h['width'],h['height'],h['row_count'])!=(752,480,480) or h['frame_id']!=index
            or h['diagnostic_count']!=count or h['qualified_count']>7680 or h['source'] not in (1,2,3)
            or h['total_bytes']!=256+64*(count+480)
            or h['measure_status']>=6 or h['preview_status']>=6):
        raise ValueError('native marker header/frame mismatch')
    # The worker owns the active shared slot until ACK. These readonly views
    # cannot escape that render call. No source Raw/quality/branch work occurs.
    records=np.frombuffer(body.toreadonly(),dtype=_NATIVE_DTYPE,count=count,offset=256)
    rows=np.frombuffer(b'',dtype=ROW_DTYPE)
    frame=FinalFrame(MappingProxyType(h),records,rows,b'')
    return DeferredPLAnnotations(frame,bool(actual),evidence,max_points)


def pack_annotations(snapshot):
    if type(snapshot) is not CompactPLAnnotations:
        raise TypeError('owned compact annotation snapshot required')
    metadata=json.dumps(asdict(snapshot.metadata),separators=(',',':'),allow_nan=False).encode('ascii')
    if len(metadata)>_MAX_METADATA:raise ValueError('bounded annotation metadata required')
    coordinates=b''.join(group.astype('<i4').tobytes() for group in snapshot.groups)
    body=metadata+coordinates
    return _HEADER.pack(b'SLM5',len(metadata),*(len(group) for group in snapshot.groups),zlib.crc32(body))+body


def unpack_annotations(view,index):
    if not _HEADER.size<=len(view)<=MAX_COMPACT_BYTES:raise ValueError('compact annotation length')
    magic,size,m,p,r,crc=_HEADER.unpack_from(view)
    if (magic!=b'SLM5' or not 1<=size<=_MAX_METADATA or m+p+r>MAX_ANNOTATIONS
            or len(view)!=_HEADER.size+size+12*(m+p+r)):
        raise ValueError('compact annotation layout')
    body=view[_HEADER.size:]
    if zlib.crc32(body)!=crc:raise ValueError('compact annotation CRC mismatch')
    data=json.loads(bytes(body[:size]).decode('ascii'))
    for key in ('measurement','preview','rejected'):
        if data.get(key)!=[]:raise ValueError('compact metadata contains marker coordinates')
        data[key]=()
    metadata=PLDisplayAnnotations(**data)
    if metadata.frame_id!=index:raise ValueError('display marker/frame mismatch')
    packed=np.frombuffer(body,dtype='<i4',offset=size).reshape(-1,3)
    groups=tuple(group.astype(np.int64) for group in np.split(packed,(m,m+p)))
    return CompactPLAnnotations(metadata,groups)
