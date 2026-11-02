"""Installed as /boot/boot.py: load the final firmware, then start the demo service.

Checks the pinned manifest, bit and HWH hashes before loading. On any failure it loads
the PYNQ base overlay once and starts nothing. The demo runs as a transient systemd unit
(scan3d-demo), so it outlives bootpy.service. The motor is enabled only through a standing
BTN0 permission pinned in the config.
"""
import hashlib
import json
import subprocess
from pathlib import Path

CONFIG_PATH = Path('/boot/scan3d_boot_config.json')
UNIT = 'scan3d-demo'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def checked_image(config):
    """Validate the pinned manifest and both artifacts; return the bit path."""
    if config.get('schema') != 'SCAN3D_BOOT_CONFIG_V1' or config.get('mode') not in ('bit_only', 'auto_demo'):
        raise ValueError('unsupported boot configuration')
    deploy = Path(config['deploy']).resolve(strict=True)
    manifest_path = deploy / 'manifest.json'
    if manifest_path.is_symlink() or digest(manifest_path) != config['manifest_sha256']:
        raise ValueError('manifest hash mismatch')
    manifest = json.loads(manifest_path.read_text(encoding='utf8'))
    for block in ('buttons', 'section_measure'):
        if (manifest.get(block) or {}).get('present') is not True:
            raise ValueError(block + ' capability absent')
    if not (manifest.get('pl_cloud_view') or {}).get('register_window'):      # declared as live.py reads it
        raise ValueError('pl_cloud_view capability absent')
    resolved = {}
    for kind in ('bit', 'hwh'):
        info = manifest['files'][kind]
        rel = Path(info['path'])
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('artifact escapes deployment')
        artifact = deploy / rel
        if artifact.is_symlink() or artifact.resolve(strict=True).parent != deploy:
            raise ValueError('artifact remapped')
        if artifact.stat().st_size != info['size'] or digest(artifact) != info['sha256']:
            raise ValueError('artifact hash/size mismatch: ' + kind)
        resolved[kind] = artifact
    if resolved['bit'].with_suffix('.hwh') != resolved['hwh']:
        raise ValueError('bit and HWH must have matching stems')
    return resolved['bit']


def demo_command(config):
    """systemd-run command for the demo service, after checking the pinned script."""
    demo = config['demo']
    script = Path(demo['script'])
    if script.is_symlink() or digest(script) != demo['script_sha256']:
        raise ValueError('demo script hash mismatch')
    return ['systemd-run', '--unit=' + UNIT, '--collect',
            '-p', 'KillMode=mixed', '-p', 'KillSignal=SIGTERM', '-p', 'TimeoutStopSec=90',
            '--setenv=XILINX_XRT=/usr',
            demo['python'], '-B', str(script),
            '--release', demo['release'], '--manifest', str(Path(config['deploy']) / 'manifest.json'),
            '--data', demo['data']] + motor_args(demo) + (['--live-stream'] if demo.get('live_stream') else [])


def motor_args(demo):
    """Standing BTN0 permission, pinned by hash; absent means the motor stays off."""
    standing = demo.get('motor_standing')
    if not standing:
        return []
    if Path(standing['path']).is_symlink() or digest(standing['path']) != standing['sha256']:
        raise ValueError('motor permission hash mismatch')
    return ['--motor-standing', standing['path']]


def boot(config, overlay_factory, base_factory, run, emit):
    """Load once; start the demo only after a verified load. Base overlay on failure."""
    try:
        bit = checked_image(config)
        overlay_factory(str(bit))
        emit({'phase': 'loaded', 'bit': str(bit)})
    except Exception as error:
        emit({'phase': 'load_failed', 'error': type(error).__name__ + ': ' + str(error)})
        try:
            base_factory('base.bit')
            emit({'phase': 'base_fallback_loaded'})
        except Exception as fallback_error:
            emit({'phase': 'base_fallback_failed', 'error': str(fallback_error)})
        return False
    if config['mode'] == 'auto_demo':
        try:
            run(demo_command(config), check=True, timeout=30)
            emit({'phase': 'demo_started', 'unit': UNIT})
        except Exception as error:
            emit({'phase': 'demo_start_failed', 'error': type(error).__name__ + ': ' + str(error)})
    return True


def main():
    import syslog

    from pynq import Overlay
    from pynq.overlays.base import BaseOverlay

    def emit(record):
        syslog.syslog(syslog.LOG_INFO, 'SCAN3D_BOOT ' + json.dumps(record, sort_keys=True))
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf8'))
    except Exception as error:
        emit({'phase': 'config_failed', 'error': str(error)})
        BaseOverlay('base.bit')
        return
    boot(config, Overlay, BaseOverlay, subprocess.run, emit)


if __name__ == '__main__':
    main()
