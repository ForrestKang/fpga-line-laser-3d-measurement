"""Explicit optional C body validator; frozen Python decoder stays independent.

No automatic compiler, library search, Raw processing or image association.
decode_native is an isolated comparison candidate, not the production decoder.
"""
from pathlib import Path
from types import MappingProxyType
import ctypes
import hashlib
import json
import os
import platform
import struct
import subprocess
import zlib
import numpy as np
from . import pfo3_r25 as wire

ABI=0x00010019
SCHEMA='R25_PFO3_BODY_VALIDATOR_LIBRARY_V1'
SOURCE=Path(__file__).with_name('native')/'pfo3_validate_r25.c'
PYTHON_DECODER=Path(wire.__file__)
_MANIFEST_TOKEN=object()
ERRORS=('','invalid row records','invalid saturation counters','invalid global counters',
    'unknown quality status','invalid quality bounds','invalid per-row quality bounds',
    'invalid quality scalar','candidate identity/order','candidate geometry',
    'overlapping unordered candidates','decision flag/reason mismatch','invalid accepted center',
    'moment/weight bounds','zero accepted weight','center not matching recorded integer statistics',
    'invalid branch ID','header acceptance count mismatch','partial fallback forbidden',
    'empty candidate counts','diagnostic completeness mismatch','qualified count mismatch',
    'row acceptance count mismatch','per-row acceptance mismatch')


class NativeBodyValidatorR25:
    @classmethod
    def from_manifest(cls,path):
        """Load only explicit same-directory, source/runtime/QA-bound bytes."""
        from structured_light.scan3d.point_raster import runtime_identity
        path=Path(path).resolve()
        with path.open('rb') as stream:data=stream.read(256*1024+1)
        if len(data)>256*1024:raise wire.PFO3Error('bounded native validator manifest required')
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise wire.PFO3Error('duplicate native validator manifest key')
                result[key]=value
            return result
        record=json.loads(data,object_pairs_hook=unique,
            parse_constant=lambda value:(_ for _ in ()).throw(wire.PFO3Error('nonfinite native validator manifest')))
        if not isinstance(record,dict) or record.get('schema')!=SCHEMA or record.get('abi')!=ABI:
            raise wire.PFO3Error('native validator manifest schema/ABI mismatch')
        runtime=runtime_identity()
        if record.get('runtime')!=runtime:raise wire.PFO3Error('native validator runtime mismatch')
        name=record.get('library')
        if (type(name) is not str or not name or len(name)>255 or name in ('.','..')
                or any(char in name for char in ('/','\\',':'))):
            raise wire.PFO3Error('native validator library must be a same-directory basename')
        original=path.parent/name;library=original.resolve()
        if original.is_symlink() or library.parent!=path.parent or not library.is_file():
            raise wire.PFO3Error('native validator library must be beside manifest')
        digest=hashlib.sha256(library.read_bytes()).hexdigest()
        source_digest=hashlib.sha256(SOURCE.read_bytes()).hexdigest()
        python_digest=hashlib.sha256(PYTHON_DECODER.read_bytes()).hexdigest()
        if record.get('library_sha256')!=digest:raise wire.PFO3Error('native validator library SHA mismatch')
        if record.get('source_sha256')!=source_digest:raise wire.PFO3Error('native validator source SHA mismatch')
        if record.get('python_decoder_sha256')!=python_digest:raise wire.PFO3Error('native validator Python decoder SHA mismatch')
        qa=record.get('validation')
        if (not isinstance(qa,dict) or qa.get('passed') is not True or qa.get('runtime')!=runtime
                or qa.get('library_sha256')!=digest or qa.get('source_sha256')!=source_digest
                or qa.get('python_decoder_sha256')!=python_digest or any(qa.get(key) is not True for key in
                    ('full_fields_equal','body_error_classes_equal','max_candidates_equal','uint64_boundary_equal'))):
            raise wire.PFO3Error('native validator lacks source/runtime-bound complete differential QA')
        result=cls(library,expected_sha256=digest)
        result._manifest_identity=MappingProxyType(dict(manifest_sha256=hashlib.sha256(data).hexdigest(),library_sha256=digest,
            source_sha256=source_digest,python_decoder_sha256=python_digest,abi=ABI,
            runtime=MappingProxyType(dict(runtime))))
        result._manifest_library=result.library;result._manifest_call=result.call
        result._manifest_path=result.path;result._manifest_token=_MANIFEST_TOKEN
        return result

    def __init__(self,path,*,expected_sha256=None):
        self._manifest_identity=None;self._manifest_token=None
        self.path=Path(path).resolve();self.sha256=hashlib.sha256(self.path.read_bytes()).hexdigest()
        if expected_sha256 is not None and self.sha256!=expected_sha256:
            raise wire.PFO3Error('native validator library hash mismatch')
        self.library=ctypes.CDLL(str(self.path))
        abi=self.library.r25_pfo3_validator_abi;abi.argtypes=[];abi.restype=ctypes.c_uint32
        if abi()!=ABI:raise wire.PFO3Error('native validator ABI mismatch')
        self.call=self.library.r25_validate_pfo3_body
        self.call.argtypes=[ctypes.c_char_p,ctypes.c_size_t];self.call.restype=ctypes.c_int

    @property
    def identity(self):return self._manifest_identity

    def require_manifest_admission(self):
        """Production transport gate; the direct constructor is QA-only.

        Returns immutable core provenance, omitting per-test logs. No per-frame
        file reads, runtime hashing or compilation are performed here.
        """
        if (type(self) is not NativeBodyValidatorR25 or getattr(self,'_manifest_token',None) is not _MANIFEST_TOKEN
                or getattr(self,'_manifest_identity',None) is None):
            raise wire.PFO3Error('actual PL native body validator requires manifest admission')
        if (self.sha256!=self._manifest_identity['library_sha256'] or self.path!=self._manifest_path
                or self.library is not self._manifest_library or self.call is not self._manifest_call):
            raise wire.PFO3Error('native validator changed after manifest admission')
        return self._manifest_identity

    def validate(self,payload):
        if type(payload) is not bytes:raise TypeError('immutable PFO3 bytes required')
        code=self.call(payload,len(payload))
        if code==100:raise wire.PFO3Error('native body precondition failed')
        if not 0<=code<len(ERRORS):raise wire.PFO3Error('unknown native validator result')
        if code:raise wire.PFO3Error(ERRORS[code])


def decode_native(payload,*,expected_identity,validator,expected_fingerprint=None,allow_fixture=False):
    """Comparison-only complete decoder, preserving every strict prefix check.

    Root may integrate validator.validate(data) after the existing strict prefix;
    until then this independent entrypoint supports differential verification.
    """
    if not isinstance(validator,NativeBodyValidatorR25):raise TypeError('explicit native validator required')
    data=bytes(payload)
    if len(data)<256:raise wire.PFO3Error('truncated header')
    h=dict(zip(wire.HEADER_FIELDS,struct.unpack_from('<64I',data)))
    if (h['magic'],h['version'],h['header_bytes'],h['record_bytes'],h['row_record_bytes'])!=(wire.MAGIC,wire.VERSION,256,64,64):
        raise wire.PFO3Error('unknown ABI')
    if (h['algorithm_revision'],h['fixed_revision'],h['policy_revision'])!=(25,1,1):
        raise wire.PFO3Error('unknown algorithm/fixed/policy revision')
    if h['frame_decisions']&~0x7f03 or h['flags']&~63 or not h['flags']&wire.COMPLETE:
        raise wire.PFO3Error('incomplete/reserved flags')
    if h['source'] not in (1,2,3) or (h['source']==3 and not allow_fixture):raise wire.PFO3Error('unadmitted source')
    if set(expected_identity)!=set(wire.IDENTITY_FIELDS) or wire.identity(h)!=expected_identity:
        raise wire.PFO3Error('frame/config/exposure/motion identity mismatch')
    for name,val in expected_identity.items():
        if type(val) is not int or val<0:raise wire.PFO3Error('invalid expected '+name)
    if expected_fingerprint is not None:
        fp=struct.pack('<8I',*(h['fingerprint'+str(i)] for i in range(8))).hex()
        if fp!=expected_fingerprint:raise wire.PFO3Error('algorithm parameter fingerprint mismatch')
    w,t=h['width'],h['height'];n=h['diagnostic_count']
    if not (1<=w<=752 and 1<=t<=480 and h['row_count']==t and
            0<=h['roi_x0']<h['roi_x1']<=w and 0<=h['roi_y0']<h['roi_y1']<=t):
        raise wire.PFO3Error('invalid image dimensions')
    if n>((w+1)//2)*t or h['qualified_count']>n or (h['qualified_count']>16*t and not h['flags']&wire.OVERFLOW):
        raise wire.PFO3Error('candidate capacity')
    size=256+64*(n+t)
    if len(data)!=size or h['total_bytes']!=size:raise wire.PFO3Error('length/count mismatch')
    if h['input_bytes']!=w*t or h['roi_pixels']!=(h['roi_x1']-h['roi_x0'])*(h['roi_y1']-h['roi_y0']):
        raise wire.PFO3Error('pixel count mismatch')
    if zlib.crc32(data[256:])!=h['body_crc32']:raise wire.PFO3Error('body checksum mismatch')
    if bool(h['flags']&wire.RAW_REQUIRED)!=bool(h['flags']&(wire.UNSUPPORTED|wire.OVERFLOW|wire.PROTOCOL|wire.DIAG_OVERFLOW)):
        raise wire.PFO3Error('fallback cause inconsistent')
    validator.validate(data)
    a=np.frombuffer(data,wire.RECORD_DTYPE,n,256);r=np.frombuffer(data,wire.ROW_DTYPE,t,256+64*n)
    a.setflags(write=False);r.setflags(write=False)
    return wire.FinalFrame(MappingProxyType(h),a,r,data)


def build_candidate(output,*,compiler='gcc'):
    """Explicit local build only, refusing every pre-existing output artifact."""
    output=Path(output).resolve()
    if output.exists():raise FileExistsError('preserve existing library: '+str(output))
    output.parent.mkdir(parents=True,exist_ok=True)
    source=SOURCE
    command=[compiler,'-O3','-std=c99','-Wall','-Wextra','-Werror','-shared']
    if os.name!='nt':command.append('-fPIC')
    command.extend([str(source),'-o',str(output)])
    temporary=output.parent/'build_temp';temporary.mkdir(exist_ok=True)
    result=subprocess.run(command,capture_output=True,text=True,check=True,
        env=dict(os.environ,TEMP=str(temporary),TMP=str(temporary),TMPDIR=str(temporary)))
    return dict(command=command,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        output=str(output),output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        stderr=result.stderr,machine=platform.machine(),board_access=False)
