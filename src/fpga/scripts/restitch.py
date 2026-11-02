"""Re-stitch a rotation scan with another calibration: camera points from the scan's own axis,
then scale s about the camera centre and the new axis. Writes object points (camera-frame
coordinates, same layout as xyz_object_mm.npy) for eval_t02_scan.py / compare_vase_stl.py."""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fit_joint_artifacts import camera_points


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('cloud', type=Path)
    ap.add_argument('views', type=Path)
    ap.add_argument('scan_axis', type=Path)
    ap.add_argument('fit', type=Path, help='fit JSON with scale, axis_point_camera_mm, axis_direction_camera')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    cloud, views = np.load(a.cloud), np.load(a.views)
    fit = json.loads(a.fit.read_text())
    P, th = camera_points(cloud, views, json.loads(a.scan_axis.read_text()), 3547.0)
    p, d = np.array(fit['axis_point_camera_mm']), np.array(fit['axis_direction_camera'])
    d /= np.linalg.norm(d)
    v = fit['scale'] * P - p
    ct, st = np.cos(-th)[:, None], np.sin(-th)[:, None]
    q = v * ct + np.cross(d, v) * st + np.outer(v @ d, d) * (1 - ct) + p
    np.save(a.out, np.column_stack((q, cloud[:, 3])))
    a.out.with_suffix('.axis.json').write_text(json.dumps(dict(axis_point_camera_mm=p.tolist(),
                                                                axis_direction_camera=d.tolist())))


if __name__ == '__main__':
    main()
