"""Boot loader and demo loop, offline: no FPGA, camera or systemd."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import boot_loader  # noqa: E402
import demo_service  # noqa: E402


def sha(b):
    return hashlib.sha256(b).hexdigest()


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        deploy = self.tmp / 'deploy'
        deploy.mkdir()
        bit, hwh = b'bit' * 10, b'hwh' * 7
        (deploy / 'final.bit').write_bytes(bit)
        (deploy / 'final.hwh').write_bytes(hwh)
        manifest = {'files': {'bit': {'path': 'final.bit', 'size': len(bit), 'sha256': sha(bit)},
                              'hwh': {'path': 'final.hwh', 'size': len(hwh), 'sha256': sha(hwh)}},
                    'buttons': {'present': True}, 'section_measure': {'present': True},
                    'pl_cloud_view': {'register_window': [1024, 1279]}}
        (deploy / 'manifest.json').write_text(json.dumps(manifest))
        script = self.tmp / 'demo_service.py'
        script.write_text('# demo')
        self.config = {'schema': 'SCAN3D_BOOT_CONFIG_V1', 'mode': 'auto_demo', 'deploy': str(deploy),
                       'manifest_sha256': sha((deploy / 'manifest.json').read_bytes()),
                       'demo': {'python': 'py', 'script': str(script), 'script_sha256': sha(b'# demo'),
                                'release': '/r', 'data': '/d'}}
        self.calls, self.events = [], []

    def run_boot(self, config):
        return boot_loader.boot(config, lambda b: self.calls.append(('load', b)),
                                lambda b: self.calls.append(('base', b)),
                                lambda cmd, **kw: self.calls.append(('run', cmd)), self.events.append)

    def test_verified_load_starts_demo_unit(self):
        self.assertTrue(self.run_boot(self.config))
        self.assertEqual(self.calls[0][0], 'load')
        self.assertEqual(self.calls[1][0], 'run')
        cmd = self.calls[1][1]
        self.assertEqual(cmd[:3], ['systemd-run', '--unit=scan3d-demo', '--collect'])
        self.assertNotIn('--allow-motor', cmd)

    def test_bit_only_starts_nothing(self):
        self.assertTrue(self.run_boot(dict(self.config, mode='bit_only')))
        self.assertEqual([c[0] for c in self.calls], ['load'])

    def test_tampered_bit_falls_back_to_base_without_demo(self):
        (Path(self.config['deploy']) / 'final.bit').write_bytes(b'xxx' * 10)
        self.assertFalse(self.run_boot(self.config))
        self.assertEqual(self.calls, [('base', 'base.bit')])

    def test_live_stream_demo_samples_the_pl_stream(self):
        self.assertNotIn('--live-stream', boot_loader.demo_command(self.config))
        config = dict(self.config, demo=dict(self.config['demo'], live_stream=True))
        self.assertEqual(boot_loader.demo_command(config)[-1], '--live-stream')

    def test_tampered_demo_script_is_not_started(self):
        Path(self.config['demo']['script']).write_text('# changed')
        self.assertTrue(self.run_boot(self.config))
        self.assertEqual([c[0] for c in self.calls], ['load'])
        self.assertEqual(self.events[-1]['phase'], 'demo_start_failed')


class InstallConfigTests(unittest.TestCase):
    def test_live_stream_needs_a_declaring_manifest(self):
        import install_boot
        firmware = Path(__file__).resolve().parents[1] / 'board/deploy_booking/firmware'
        script = Path(__file__).with_name('demo_service.py')
        config = install_boot.build_config('auto_demo', script, deploy=firmware, live_stream=True)
        self.assertEqual((config['deploy'], config['demo']['live_stream']), (str(firmware), True))
        old = Path(__file__).resolve().parents[1] / 'release/firmware'
        with self.assertRaises(ValueError):
            install_boot.build_config('auto_demo', script, deploy=old, live_stream=True)


class ReleaseManifestTests(unittest.TestCase):
    def test_real_release_passes_loader_checks(self):
        firmware = Path(__file__).resolve().parents[1] / 'release/firmware'
        config = {'schema': 'SCAN3D_BOOT_CONFIG_V1', 'mode': 'bit_only', 'deploy': str(firmware),
                  'manifest_sha256': sha((firmware / 'manifest.json').read_bytes())}
        self.assertEqual(boot_loader.checked_image(config).name, 'final.bit')


class MotorPermissionTests(unittest.TestCase):
    def test_demo_command_carries_pinned_standing_permission(self):
        tmp = Path(tempfile.mkdtemp())
        standing = tmp / 'motor.json'
        standing.write_text('{}')
        demo = {'motor_standing': {'path': str(standing), 'sha256': sha(b'{}')}}
        self.assertEqual(boot_loader.motor_args(demo), ['--motor-standing', str(standing)])
        standing.write_text('{"changed": 1}')
        with self.assertRaises(ValueError):
            boot_loader.motor_args(demo)
        self.assertEqual(boot_loader.motor_args({}), [])

    def test_live_command_with_authority_allows_bounded_motor(self):
        cmd = demo_service.live_command('py', Path('/r'), Path('/m.json'), Path('/d/run'), 1800, Path('/d/a.json'))
        self.assertEqual(cmd[cmd.index('--motor-authority') + 1], str(Path('/d/a.json')))
        self.assertIn('--allow-motor', cmd)
        self.assertEqual(cmd[cmd.index('--scan-deadline-s') + 1], '1200')

    def test_incomplete_standing_permission_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        standing = tmp / 'motor.json'
        standing.write_text(json.dumps({'schema': 'SCAN3D_MOTOR_STANDING_V1', 'btn0_scan': True,
                                        'motor_12v_connected': True, 'user_statement': ''}))
        with self.assertRaises(ValueError):
            demo_service.motor_authority(standing, tmp, 'run_20261030_100000')


class DemoTests(unittest.TestCase):
    def test_live_command_live_stream_flag(self):
        cmd = demo_service.live_command('py', Path('/r'), Path('/m.json'), Path('/d/run'), 1800, live_stream=True)
        self.assertEqual(cmd[-1], '--live-stream')
        self.assertNotIn('--live-stream', demo_service.live_command('py', Path('/r'), Path('/m.json'), Path('/d/run'), 1800))

    def test_full_width_drops_binning(self):
        binned = demo_service.live_command('py', Path('/r'), Path('/m.json'), Path('/d/run'), 1800)
        full = demo_service.live_command('py', Path('/r'), Path('/m.json'), Path('/d/run'), 1800, full_width=True)
        self.assertIn('--pfo3-bin2', binned)
        self.assertNotIn('--pfo3-bin2', full)

    def test_scan_start_request_only_with_motor(self):
        free = demo_service.live_command('py', Path('/r'), Path('/m.json'), Path('/d/run'), 60)
        motor = demo_service.live_command('py', Path('/r'), Path('/m.json'), Path('/d/run'), 60, Path('/d/a.json'))
        self.assertNotIn('--scan-start-request', free)
        self.assertIn('--scan-start-request', motor)

    def test_live_command_has_no_motor_and_no_raw(self):
        cmd = demo_service.live_command('py', Path('/r'), Path('/m.json'), Path('/d/run'), 1800)
        self.assertIn('--pipeline', cmd)
        self.assertEqual(cmd[cmd.index('--save-raw-every') + 1], '0')
        self.assertNotIn('--allow-motor', cmd)
        self.assertNotIn('--ui-cycle-test-s', cmd)

    def test_pause_backs_off_only_for_early_failures(self):
        self.assertEqual(demo_service.pause_after(1800, 0, 3), (3, 0))
        self.assertEqual(demo_service.pause_after(10, 1, 0), (10, 1))
        self.assertEqual(demo_service.pause_after(10, 1, 6), (300, 7))

    def test_prune_keeps_newest_and_ignores_other_folders(self):
        data = Path(tempfile.mkdtemp())
        for name in ('run_20261030_100000', 'run_20261030_110000', 'run_20261030_120000', 'keep_me'):
            (data / name).mkdir()
        self.assertEqual(demo_service.prune(data, 2), ['run_20261030_100000'])
        self.assertTrue((data / 'keep_me').is_dir())

    def test_loop_runs_cycles_and_logs(self):
        release = Path(tempfile.mkdtemp())
        pkg = release / 'runtime/r33'
        pkg.mkdir(parents=True)
        (pkg / '__init__.py').write_text('')
        (pkg / 'live.py').write_text(
            'import sys, json, pathlib\n'
            'out = pathlib.Path(sys.argv[sys.argv.index("--output") + 1]); out.mkdir()\n'
            '(out / "summary.json").write_text(json.dumps({"frames": 5, "fps": 2.0, "phase": "stopped"}))\n')
        data = release / 'demo_data'
        (release / 'm.json').write_text(json.dumps({'pfo3': {}}))
        import time
        real_sleep = time.sleep
        demo_service.time.sleep = lambda s: real_sleep(min(s, 1.1))
        try:
            rc = demo_service.main(['--release', str(release), '--manifest', str(release / 'm.json'), '--data', str(data),
                                    '--cycles', '2', '--keep', '1', '--run-s', '5'])
        finally:
            demo_service.time.sleep = real_sleep
        self.assertEqual(rc, 0)
        records = [json.loads(x) for x in (data / 'demo_service.jsonl').read_text().splitlines()]
        self.assertEqual([r['exit_code'] for r in records], [0, 0])
        self.assertEqual(records[0]['frames'], 5)
        self.assertEqual(len([p for p in data.iterdir() if p.is_dir()]), 1)


if __name__ == '__main__':
    unittest.main()
