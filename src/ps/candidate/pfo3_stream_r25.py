"""Archive tagged FPGA results. Joins explicit hardware IDs, never links images.

Retain primary_stream bytes alongside assembled payload. Assembly is not a second
optical algorithm: it applies PL final decision patches and copies PL summaries.
"""
import hashlib,struct,zlib
import numpy as np
from .pfo3_r25 import *

HARDWARE_HEADER_MAGIC=0x33544850
HARDWARE_FOOTER_MAGIC=0x33544650
TAG_BASE=0x33520000

ASSEMBLE_STAGES={}
_PROFILING=__import__('os').environ.get('SCAN3D_PROFILE_DIR')


def _mark_value(name,value):
    if not _PROFILING:return
    row=ASSEMBLE_STAGES.setdefault(name,[0.0,0])
    row[0]+=float(value);row[1]+=1


def _mark(name,t0):
    if not _PROFILING:return
    row=ASSEMBLE_STAGES.setdefault(name,[0.0,0])
    row[0]+=__import__('time').perf_counter()-t0;row[1]+=1


def _native_payload(primary_stream):
    """Native validation+reassembly when its library is present; None otherwise.

    The returned payload still goes through the Python strict decoder below, so
    no framing/identity/fingerprint/CRC check is dropped by this fast path.
    """
    try:
        from .pfo3_assemble_native_r26 import assemble_native, available
    except Exception:
        return None
    if not available():
        return None
    return assemble_native(primary_stream)


def assemble(primary_stream,*,expected_identity,expected_fingerprint,body_validator=None,return_seal=False):
    _t=__import__('time').perf_counter()
    _mark_value('a0_primary_bytes',len(primary_stream))
    fast=_native_payload(primary_stream)
    if fast is not None:
        _mark('a1n_native_validate',_t)
        _t=__import__('time').perf_counter()
        if body_validator is None:
            frame=decode(fast,expected_identity=expected_identity,expected_fingerprint=expected_fingerprint)
        else:
            from .pfo3_decode_native_r25 import decode_native
            frame=decode_native(fast,expected_identity=expected_identity,
                                expected_fingerprint=expected_fingerprint,validator=body_validator)
        _mark('a3_decode_native_check',_t)
        _t=__import__('time').perf_counter()
        validator_identity=None
        if body_validator is not None:
            core_identity=body_validator.require_manifest_admission()
            validator_identity={key:core_identity[key] for key in
                ('manifest_sha256','library_sha256','source_sha256','python_decoder_sha256','abi')}
        seal=None
        if return_seal:
            from .immutable_pair_seal_r25 import compute
            # the seal binds the frame arrays to the *primary stream* bytes
            primary=bytes(primary_stream)
            seal=compute(frame,primary);digests=seal.check(frame,primary)
            primary_sha=digests.primary_stream_sha256;final_sha=digests.assembled_sha256
        else:
            primary_sha=hashlib.sha256(bytes(primary_stream)).hexdigest();final_sha=hashlib.sha256(fast).hexdigest()
        receipt=dict(primary_stream_sha256=primary_sha,assembled_sha256=final_sha,
            native_sequence=int(np.frombuffer(bytes(primary_stream),dtype='<u4')[4]),
            diagnostic_count=frame.header['diagnostic_count'],
            final_patch_count=int(frame.header['qualified_count']),
            row_count=frame.header['row_count'],ARM_pixel_algorithm_calls=0,
            assembly_policy='PL_FINAL_FIELD_PATCH_BY_EXPLICIT_ID_ONLY',assembly_backend='NATIVE_C_R26',
            body_validation_backend='STRICT_PYTHON' if body_validator is None else 'STRICT_NATIVE_C_R25',
            body_validator_sha256=None if body_validator is None else body_validator.sha256,
            body_validator_identity=validator_identity)
        _mark('a5_seal',_t)
        return (frame,receipt,seal) if return_seal else (frame,receipt)
    data=bytes(primary_stream)
    if len(data)<384 or len(data)%4:raise PFO3Error('truncated hardware stream')
    words=np.frombuffer(data,dtype='<u4')
    start=words[:64];end=words[-32:]
    if (int(start[0]),int(start[1]),int(end[0]),int(end[1]))!=(HARDWARE_HEADER_MAGIC,1,HARDWARE_FOOTER_MAGIC,1):
        raise PFO3Error('not a native hardware stream')
    if start[4]!=end[23] or end[24]!=len(words):raise PFO3Error('stream sequence/length mismatch')
    if end[17]:raise PFO3Error('native hardware error '+str(int(end[17])))
    if (len(words)-96)%17:raise PFO3Error('truncated hardware record')
    tagged=words[64:-32].reshape(-1,17)
    if np.any((tagged[:,0]&0xfffffffc)!=TAG_BASE):raise PFO3Error('unknown native record tag')
    kinds=tagged[:,0]&3
    if np.any(kinds==3):raise PFO3Error('reserved native record kind')
    # One boolean evaluation reused for all three record classes instead of
    # three separate 2-D boolean masks (each of which copied rows twice).
    index0=np.flatnonzero(kinds==0);index1=np.flatnonzero(kinds==1);index2=np.flatnonzero(kinds==2)
    local=np.ascontiguousarray(tagged[index0][:,1:]);patches=tagged[index1][:,1:]
    row_words=np.ascontiguousarray(tagged[index2][:,1:])
    if len(local)!=end[2] or len(row_words)!=start[3]:raise PFO3Error('native output count mismatch')
    n=len(local)
    if not np.array_equal(local[:,0],np.arange(n)):raise PFO3Error('native diagnostic ID mismatch')
    qualified=np.flatnonzero((local[:,13]&255)==0)
    if len(qualified):
        # Validate the sixteen hardware slots per row in one metadata pass.
        # This is framing validation, never candidate extraction or linking.
        _,first,count=np.unique(local[qualified,1],return_index=True,return_counts=True)
        slots=np.arange(len(qualified))-np.repeat(first,count)
        expected_patch=qualified[slots<16].tolist()
    else:expected_patch=[]
    if len(patches):
        ids=patches[:,0].astype(np.int64)
        if np.any(ids>=n) or len(np.unique(ids))!=len(ids):raise PFO3Error('invalid/repeated hardware patch ID')
        # Sorted comparison is equivalent to the former Python set equality
        # (ids are already proven unique above) and avoids building two sets
        # of numpy scalars per frame.
        if (ids.size!=len(expected_patch)
                or not np.array_equal(np.sort(ids),np.sort(np.asarray(expected_patch,dtype=np.int64)))
                or np.any(patches[:,13]&255)):
            raise PFO3Error('missing/foreign qualified candidate patch')
        # Final patches update decisions, branch IDs and center-stability only.
        # Raw candidate statistics remain immutable in the archived diagnostic.
        local[ids,13]=(local[ids,13]&255)|(patches[:,13]&0xffffff00)
        local[ids,14]=patches[:,14]
        local[ids,15]=(local[ids,15]&0xff000000)|(patches[:,15]&0xffffff)
    elif expected_patch:raise PFO3Error('missing hardware final patches')
    h=dict(zip(HEADER_FIELDS,map(int,start)))
    width,height=map(int,start[2:4])
    if h['width']!=width or h['height']!=height:raise PFO3Error('configured/native geometry mismatch')
    h.update(magic=MAGIC,version=VERSION,header_bytes=256,record_bytes=64,row_record_bytes=64,
        diagnostic_count=n,qualified_count=int(end[3]),row_count=height,flags=int(end[11]),
        measurement_count=int(end[4]),preview_count=int(end[5]),measure_valid_rows=int(end[6]),
        preview_valid_rows=int(end[7]),ambiguous_rows=int(end[28]),
        sat250_count=int(end[9]),sat255_count=int(end[10]),measure_status=int(end[12]),
        preview_status=int(end[13]),processing_cycles_lo=int(end[14]),processing_cycles_hi=int(end[15]),
        input_bytes=int(end[16]),error_code=int(end[17]),measure_median_peak_x2=int(end[18]),
        measure_median_width_x2=int(end[19]),preview_median_peak_x2=int(end[20]),
        preview_median_width_x2=int(end[21]),raw_peak_median_x2=int(end[22]),
        frame_decisions=int(end[25])|(int(end[26])<<1)|(int(end[27])<<8))
    # Single allocation: write the body straight into the payload buffer, then
    # compute the CRC over the same bytes. Byte-identical to the previous
    # "body = a+b; payload = header+body" form, one copy less.
    _mark('a1_patch_validate',_t)
    _t=__import__('time').perf_counter()
    local_bytes=local.tobytes();row_bytes=row_words.tobytes()
    buffer=bytearray(256+len(local_bytes)+len(row_bytes))
    buffer[256:256+len(local_bytes)]=local_bytes
    buffer[256+len(local_bytes):]=row_bytes
    h.update(total_bytes=len(buffer),body_crc32=zlib.crc32(memoryview(buffer)[256:]))
    struct.pack_into('<64I',buffer,0,*(h[x] for x in HEADER_FIELDS))
    payload=bytes(buffer)
    _mark('a2_header_crc_pack',_t)
    _t=__import__('time').perf_counter()
    if body_validator is None:
        frame=decode(payload,expected_identity=expected_identity,expected_fingerprint=expected_fingerprint)
    else:
        from .pfo3_decode_native_r25 import decode_native
        frame=decode_native(payload,expected_identity=expected_identity,expected_fingerprint=expected_fingerprint,
                            validator=body_validator)
    _mark('a3_decode_native_check',_t)
    _t=__import__('time').perf_counter()
    validator_identity=None
    if body_validator is not None:
        core_identity=body_validator.require_manifest_admission()
        validator_identity={key:core_identity[key] for key in
            ('manifest_sha256','library_sha256','source_sha256','python_decoder_sha256','abi')}
    seal=None
    if return_seal:
        from .immutable_pair_seal_r25 import compute
        _mark('a4_pre_seal',_t)
        _t=__import__('time').perf_counter()
        seal=compute(frame,data);digests=seal.check(frame,data)
        _mark('a5_seal',_t)
        _t=__import__('time').perf_counter()
        primary_sha=digests.primary_stream_sha256;final_sha=digests.assembled_sha256
    else:
        primary_sha=hashlib.sha256(data).hexdigest();final_sha=hashlib.sha256(payload).hexdigest()
    receipt=dict(primary_stream_sha256=primary_sha,
        assembled_sha256=final_sha,native_sequence=int(start[4]),
        diagnostic_count=n,final_patch_count=len(patches),row_count=len(row_words),
        ARM_pixel_algorithm_calls=0,assembly_policy='PL_FINAL_FIELD_PATCH_BY_EXPLICIT_ID_ONLY',
        body_validation_backend='STRICT_PYTHON' if body_validator is None else 'STRICT_NATIVE_C_R25',
        body_validator_sha256=None if body_validator is None else body_validator.sha256,
        body_validator_identity=validator_identity)
    return (frame,receipt,seal) if return_seal else (frame,receipt)
