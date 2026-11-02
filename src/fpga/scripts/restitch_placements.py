"""Re-stitch a rotation scan with a fit_t02_placements.py calibration (laser plane + axis): camera
points from the scan's own axis/plane, moved along their rays onto the new plane, turned back
about the new axis. Writes object points (camera-frame layout, as xyz_object_mm.npy) and the
axis JSON for eval_t02_scan.py / compare_vase_stl.py / compare_polar_stl.py."""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fit_t02_placements import camera_points, rotate


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('cloud', type=Path)
    ap.add_argument('views', type=Path)
    ap.add_argument('scan_axis', type=Path)
    ap.add_argument('scan_plane', type=Path)
    ap.add_argument('calibration', type=Path, help='folder with laser_plane_r41.json and turntable_axis.json')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    cloud, views = np.load(a.cloud), np.load(a.views)
    old = np.array(json.loads(a.scan_plane.read_text())['plane_coefficients'], float)
    new = np.array(json.loads((a.calibration / 'laser_plane_r41.json').read_text())['plane_coefficients'], float)
    axis = json.loads((a.calibration / 'turntable_axis.json').read_text())
    P, theta, _ = camera_points(cloud, views, json.loads(a.scan_axis.read_text()), 1.0, 1.0)
    # ray from the camera centre through P meets the new plane a.X + d = 0 at t P
    P = P * (-new[3] / (P @ new[:3]))[:, None]
    p, d = np.array(axis['axis_point_camera_mm'], float), np.array(axis['axis_direction_camera'], float)
    d /= np.linalg.norm(d)
    q = rotate(P - p, d, -theta) + p
    np.save(a.out, np.column_stack((q, cloud[:, 3])))
    a.out.with_suffix('.axis.json').write_text(json.dumps(dict(axis_point_camera_mm=p.tolist(),
                                                                axis_direction_camera=d.tolist())))


if __name__ == '__main__':
    main()
