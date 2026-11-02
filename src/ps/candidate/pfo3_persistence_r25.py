"""Cross-check preserved PL stream, final archive, Raw and geometry observations.

This verifies identity and byte provenance. It never processes image pixels.
"""
import hashlib
import struct
import numpy as np
from .pfo3_r25 import decode,identity,PFO3Error,HEADER_FIELDS,COMPLETE,RAW_REQUIRED,UNSUPPORTED,OVERFLOW
from .pfo3_stream_r25 import assemble


def _arr_eq(a,b):
    if a is b:
        return True
    if type(a) is np.ndarray and type(b) is np.ndarray and a.dtype==b.dtype:
        return a.shape==b.shape and bool((a==b).all())
    return np.array_equal(a,b)


def _compare_observations(profile,observed):
    for name,expected in (('rows',observed.rows),('center_q12',observed.center_q12),
                          ('stripe_accepted',observed.accepted),('branch_ids',observed.branch_ids)):
        if not _arr_eq(profile[name],expected):raise PFO3Error('geometry input differs from source observations: '+name)
    obs_q=profile['observation_quality']
    for key,expected in observed.quality.items():
        if not _arr_eq(obs_q.get(key),expected):raise PFO3Error('geometry observation quality differs from source: '+key)
    from .storage_native15 import expand_observation_reasons
    reasons=expand_observation_reasons(profile)
    if reasons is not observed.reasons and tuple(reasons)!=tuple(observed.reasons):
        raise PFO3Error('geometry observation reasons differ from source')


def _validate_cpu_sources(record,profile):
    from .v3_exception_observations_r25 import decode_cpu_reference,CPUReferenceObservationsR25
    meta=profile['frame_metadata'];npz=record.get('source_cpu_v3_npz');manifest=record.get('source_cpu_v3_manifest')
    if not npz or not manifest or not record.get('source_pfo3_payload') or not record.get('source_pfo3_primary'):
        raise PFO3Error('CPU geometry requires failed PL pair and complete CPU NPZ/manifest')
    reference,info=decode_cpu_reference(npz,manifest);provenance=info['cpu_provenance'];expected=provenance['identity']
    mock=provenance['CPU_result_source']=='MOCK_ONLY_ARM_V3'
    if meta['processing_backend']!=('MOCK_ONLY_PS_V3_EXPLICIT_FALLBACK' if mock else 'PS_V3_EXPLICIT_FALLBACK'):
        raise PFO3Error('CPU evidence domain mislabeled')
    if (meta.get('cpu_v3_npz_sha256')!=hashlib.sha256(npz).hexdigest()
            or meta.get('cpu_v3_manifest_sha256')!=hashlib.sha256(manifest).hexdigest()
            or meta.get('cpu_v3_takeover')!=info or meta.get('PL_success') is not False
            or provenance['raw_sha256']!=record['raw_sha256'] or meta.get('pfo3_input_raw_sha256')!=record['raw_sha256']
            or expected['frame_id']!=record['request_id'] or expected['epoch']!=record['capture_epoch']):
        raise PFO3Error('CPU source/geometry/Raw identity mismatch')
    failed=decode(record['source_pfo3_payload'],expected_identity=expected,expected_fingerprint=reference.config_fingerprint)
    restored,_=assemble(record['source_pfo3_primary'],expected_identity=expected,expected_fingerprint=reference.config_fingerprint)
    flags=failed.header['flags']
    if (not failed.raw_required or restored.payload!=failed.payload or failed.header['error_code']
            or flags&~(COMPLETE|RAW_REQUIRED|UNSUPPORTED|OVERFLOW) or not flags&(UNSUPPORTED|OVERFLOW)
            or provenance['failed_flags']!=flags or provenance['failed_error_code']!=0
            or failed.header['diagnostic_count']>8192 or provenance.get('original_capture_fault') is not False
            or meta.get('failed_pfo3_stream_sha256')!=hashlib.sha256(failed.payload).hexdigest()):
        raise PFO3Error('CPU backend requires legal exact failed PL sources')
    raw_description=provenance['files']['Raw.gray8.bin']
    if raw_description['bytes']!=752*480 or raw_description['sha256']!=record['raw_sha256']:
        raise PFO3Error('CPU Raw archive provenance differs from stored Raw')
    for name,key in (('PL_final.pfo3','source_pfo3_payload'),('PL_primary.bin','source_pfo3_primary')):
        source=record[key];description=provenance['files'][name]
        if len(source)!=description['bytes'] or hashlib.sha256(source).hexdigest()!=description['sha256']:
            raise PFO3Error('CPU failed source hash mismatch')
    mode=meta['pfo3_mode'];adapter=CPUReferenceObservationsR25(reference);usable=adapter.frame_usable(mode)
    if meta.get('image_frame_usable') is not usable:raise PFO3Error('CPU image frame-use differs from source')
    if not usable and (np.any(profile['valid_points']) or any(profile.get(key) is True for key in
            ('valid','fused','profile_valid','simulation_geometry_valid'))):raise PFO3Error('rejected CPU frame cannot supply geometry')
    if mode=='preview' and (meta.get('pfo3_dimensions_allowed') is not False or any(profile.get(key) is True for key in
            ('valid','fused','profile_valid'))):raise PFO3Error('CPU preview cannot enter measurement')
    _compare_observations(profile,adapter.profile(mode))


def validate_sources(record,profile):
    meta=profile.get('frame_metadata',{})
    payload=record.get('source_pfo3_payload')
    primary=record.get('source_pfo3_primary')
    backend=meta.get('processing_backend')
    cpu=record.get('source_cpu_v3_npz') is not None or record.get('source_cpu_v3_manifest') is not None
    if backend in ('PS_V3_EXPLICIT_FALLBACK','MOCK_ONLY_PS_V3_EXPLICIT_FALLBACK'):
        _validate_cpu_sources(record,profile);return
    if cpu:raise PFO3Error('CPU sources require an explicit CPU backend')
    if payload is None and primary is None:
        if backend=='PL_FINAL_V3':raise PFO3Error('PL geometry requires both primary and final source packets')
        return
    if not payload or not primary or backend!='PL_FINAL_V3':
        raise PFO3Error('incomplete or mislabeled actual PL source records')
    raw_sha=record['raw_sha256']
    if len(payload)<256:raise PFO3Error('truncated final archive')
    completion=record.get('_pfo3_completion')
    if completion is not None:
        from .pfo3_processor_r25 import PLFrameAdmission
        if type(completion) is not PLFrameAdmission:raise PFO3Error('invalid completion capability')
        frame=completion.persistence_frame(record)
        if meta.get('pfo3_bit_sha256')!=completion.bit_sha256 or meta.get('PL_success') is not True:
            raise PFO3Error('geometry PL bit/success differs from completed hardware source')
        payload_digest=completion.stream_sha256
    else:
        header=dict(zip(HEADER_FIELDS,struct.unpack_from('<64I',payload)))
        payload_digest=hashlib.sha256(payload).hexdigest()
        frame=decode(payload,expected_identity=identity(header))
    if (meta.get('pfo3_stream_sha256')!=payload_digest or meta.get('pfo3_input_raw_sha256')!=raw_sha):
        raise PFO3Error('stored PL source differs from geometry input provenance')
    # Older immutable stage02 records predate this explicit field. They remain
    # readable as historical/unknown source statistics, never PL-only counters.
    if 'PL_success' in meta and meta['PL_success'] is not True:
        raise PFO3Error('PL backend cannot contain an explicit unsuccessful PL result')
    if frame.header['frame_id']!=record['request_id'] or frame.header['epoch']!=record['capture_epoch']:
        raise PFO3Error('PL archive/Raw frame mismatch')
    if completion is None:
        fingerprint=''.join(int(frame.header['fingerprint'+str(i)]).to_bytes(4,'little').hex() for i in range(8))
        restored,_=assemble(primary,expected_identity=identity(frame.header),expected_fingerprint=fingerprint)
        if restored.payload!=payload:raise PFO3Error('final archive differs from preserved hardware output')
    mode=meta['pfo3_mode'];usable=frame.frame_usable(mode)
    if meta.get('pfo3_frame_usable') is not usable:
        raise PFO3Error('geometry frame-use metadata differs from PL')
    if not usable and (np.any(profile['valid_points']) or any(profile.get(key) is True for key in
            ('valid','fused','profile_valid','simulation_geometry_valid'))):
        raise PFO3Error('frame rejected by PL cannot contribute valid geometry')
    if mode=='preview' and (meta.get('pfo3_dimensions_allowed') is not False or
            any(profile.get(key) is True for key in ('valid','fused','profile_valid'))):
        raise PFO3Error('preview results cannot enter measurement')
    # Only writer-private compact reason IDs enter this path. Public disk
    # readers reconstruct tuples and continue through the independent Python
    # conversion/comparison below; no native receipt is stored on disk.
    from .storage_native15 import compare_observations
    if ('_storage_reason_table15' not in profile
            or not compare_observations(profile,frame,mode)):
        observed=frame.profile(mode,qualified_only=True)
        _compare_observations(profile,observed)
