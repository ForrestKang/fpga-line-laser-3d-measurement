"""Feed real board frames through PageState + PageCompositor (the live-loop path) and save the pages.

Uses r37_r41f_live02 raws with the reference detector standing in for the PL frontend.
"""
import json
import sys
import types
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_ui_mockups as m  # noqa: E402
from r33.page_state import PageState  # noqa: E402
from r33.profile_live import ProfileConfig, ProfileTracker, SectionBasis  # noqa: E402
from r33.ui_pages import PageCompositor, PageRenderer  # noqa: E402

RUN = 'r37_r41f_live02'


def main():
    out = m.ROOT / 'ui_mockups'
    cal_path = m.BOARD / RUN / 'calibration_live.json'
    cal = json.loads(cal_path.read_text())
    params = m.geo.parameters(m.load_calibration(cal_path, admit_measured=True), 0.)
    basis, cfg = SectionBasis(cal['laser_plane_camera']), ProfileConfig()
    tracker = ProfileTracker(cfg)
    axis = types.SimpleNamespace(axis_point_mm=cal['axis_point_camera_mm'], axis_direction=cal['axis_direction_camera'])
    ps = PageState(run_name=RUN, binned=True, motor_allowed=False, motor_power_disconnected=True, turntable=axis)
    comp = PageCompositor(PageRenderer(), refresh_s=1e9)
    for k in range(15, 360, 15):
        raw = m.BOARD / RUN / f'raw_{k:06d}.gray8'
        if not raw.exists():
            continue
        gray = np.fromfile(raw, np.uint8).reshape(480, 752)
        f, q, recs, patches = m.frame_profile(gray, True, params, basis, cfg)
        rows = m.detect(gray, True)[2]
        c = m.centres(recs, patches)
        ps.frame(number=k, fps=12.0, capture_ms=60.0, frame_ms=70.0, cycles=448813, gray=gray, centres=c,
                 rows=rows, points=f['points'], quality=q)
        ps.profile(f, tracker.update(f, q), cfg, gray, c)
        ps.scan(dict(state='IDLE', views=0, target_views=200, angle_deg=0.0), [])
    for page in 'ABC':
        comp.base = None
        img = comp.frame(page, lambda: ps.build(page, cloud_frames=0), gray, c)
        Image.fromarray(img).save(out / f'replay_{page}.png')
    print('render ms', [round(v, 1) for v in comp.render_ms])


if __name__ == '__main__':
    main()
