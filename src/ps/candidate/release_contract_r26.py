"""Release preflight: fail before device access, record actual native identities."""
from pathlib import Path
import hashlib
import os

PROFILE = 'R26_SOFTWARE16_V1'
def resolved_config(args):
    profile = os.environ.get('SCAN3D_RELEASE_PROFILE', PROFILE)
    if profile not in (PROFILE, 'R29_FULL_CHAIN_V1', 'R30_UNIFIED_PIPELINE_V1'):
        raise ValueError('unknown release profile')
    geometry = os.environ.get('SCAN3D_R29_GEOMETRY', 'baseline')
    if geometry not in ('baseline', 'shared'):
        raise ValueError('unknown R29 geometry backend')
    tx_cache = os.environ.get('SCAN3D_R29_TX_CACHE', 'baseline')
    if tx_cache not in ('baseline', 'cacheable'):
        raise ValueError('unknown R29 input cache policy')
    observation_modes=os.environ.get('SCAN3D_R29_OBSERVATIONS','baseline')
    if observation_modes not in ('baseline','fused'):
        raise ValueError('unknown R29 observation mode converter')
    return dict(profile=profile, runtime_options=dict(r29_geometry=geometry,
                r30_pipeline=os.environ.get('SCAN3D_R30_PIPELINE','disabled'),
                r29_tx_cache=tx_cache,
                r29_observations=observation_modes,
                observation_backend=os.environ.get('SCAN3D_R28_OBSERVATIONS', 'native')),
                **{k: str(v) if isinstance(v, Path) else v
                   for k,v in vars(args).items()})

def validate_release(validator_manifest, *, diagnostic=False):
    from candidate import geometry_fast_r21 as geometry
    from candidate import pfo3_assemble_native_r26 as assembler
    result = dict(profile=PROFILE, diagnostic=diagnostic, libraries={})
    if os.environ.get('SCAN3D_R29_OBSERVATIONS','baseline')=='fused':
        from candidate import observation_modes_r29 as observations
        if observations.identity() is None:
            raise RuntimeError('R29 fused observation converter unavailable')
        result['libraries']['observation_modes_r29']=dict(loaded=True,identity=observations.identity())
    for name, available, path in (
        ('geometry', geometry.native_available(), geometry.native_library_path()),
        ('assembler', assembler.available(), assembler.library_path())):
        p=Path(path)
        result['libraries'][name]=dict(loaded=bool(available), path=str(p),
            sha256=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
        if not available and not diagnostic:
            raise RuntimeError('release native '+name+' unavailable; build libraries or explicitly select --diagnostic-python')
    from candidate import storage_native15 as storage
    storage_loaded = storage.available()
    result['libraries']['storage'] = dict(loaded=storage_loaded, manifest=str(storage.MANIFEST),
        manifest_sha256=hashlib.sha256(storage.MANIFEST.read_bytes()).hexdigest() if storage.MANIFEST.is_file() else None,
        identity=storage.identity() if storage_loaded else None)
    if not storage_loaded and not diagnostic:
        raise RuntimeError('release native storage unavailable; build storage15 helper or explicitly select --diagnostic-python')
    from candidate.display_marker_native15c import NativeMarkers15C, MANIFEST
    marker = NativeMarkers15C(MANIFEST) if MANIFEST.is_file() else None
    result['libraries']['display_marker'] = dict(loaded=marker is not None,
        manifest=str(MANIFEST),
        manifest_sha256=hashlib.sha256(MANIFEST.read_bytes()).hexdigest() if MANIFEST.is_file() else None,
        identity=marker.identity if marker is not None else None)
    if marker is None and not diagnostic:
        raise RuntimeError('release native display marker unavailable; build and validate marker15c')
    if validator_manifest is None:
        if not diagnostic: raise RuntimeError('release native validator manifest required')
        result['validator']='STRICT_PYTHON_DIAGNOSTIC'
    else:
        from candidate.pfo3_decode_native_r25 import NativeBodyValidatorR25
        NativeBodyValidatorR25.from_manifest(validator_manifest)
        p=Path(validator_manifest)
        result['validator']=dict(manifest=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest())
    return result
