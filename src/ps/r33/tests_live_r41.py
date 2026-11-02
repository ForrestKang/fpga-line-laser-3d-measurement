"""R41 live helpers: camera manifest, measured live calibration and the live replay scope."""
import json
import tempfile
import unittest
from pathlib import Path

from . import live
from .geometry import load_calibration
from .scope import PFO3LiveScopeR41, PFO3ReplayScopeR31

ROOT = Path(__file__).resolve().parents[1]
CAL = ROOT / 'calibration_r41'


class LiveTests(unittest.TestCase):
    def test_camera_manifest_is_raw_only_with_absolute_files(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / 'manifest.json').write_text(json.dumps(dict(
                device='xc7z020clg400-1', regmap_version=65536, frame_width=752, frame_height=480,
                overlay_mode='structured_light_profile_hdmi', contract_file='p2_board.json', contract_sha256='a' * 64,
                addresses=dict(pipeline_control=1, camera_diagnostics=2, raw_dma=3, hdmi_vdma=4),
                files=dict(bit=dict(path='r35.bit', sha256='b' * 64), hwh=dict(path='r35.hwh', sha256='c' * 64)))))
            rec = json.loads(Path(live.camera_manifest(d / 'manifest.json', d / 'cam.json')).read_text())
            self.assertEqual(rec['overlay_mode'], 'camera_raw')
            self.assertEqual(set(rec['addresses']), {'pipeline_control', 'camera_diagnostics', 'raw_dma'})
            self.assertTrue(Path(rec['files']['bit']['path']).is_absolute())
            self.assertEqual(rec['files']['hwh']['sha256'], 'c' * 64)

    def test_live_calibration_admitted_only_when_asked(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'c.json'
            rec = live.live_calibration(CAL / 'camera_intrinsics_r41.json', CAL / 'laser_plane_r41.json', path)
            self.assertEqual(rec['evidence_type'], 'MEASURED_CANDIDATE')
            self.assertEqual(rec['axis_status'], 'PROVISIONAL_NOT_CALIBRATED')
            self.assertGreater(rec['axis_point_camera_mm'][2], 0)
            load_calibration(path, admit_measured=True)
            with self.assertRaises(ValueError):
                load_calibration(path)

    def test_live_scope_only_relaxes_camera_state(self):
        self.assertFalse(PFO3ReplayScopeR31.live_camera)
        self.assertTrue(PFO3LiveScopeR41.live_camera)
        self.assertTrue(issubclass(PFO3LiveScopeR41, PFO3ReplayScopeR31))


if __name__ == '__main__':
    unittest.main()
