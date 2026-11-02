"""Independent offline pixel QA, called by explicit build tool, never per frame."""
import numpy as np


def reference_raster(points, px, py, panel, scale, z_min, z_span):
    xs=np.rint(panel.shape[1]*.52+px*scale).astype(np.int32)
    ys=np.rint(panel.shape[0]*.48-py*scale).astype(np.int32)
    ratio=np.clip((points[:,2]-z_min)/z_span,0.,1.)
    colors=np.column_stack((210-110*ratio,150+75*ratio,105+115*ratio)).astype(np.uint8)
    inside=(xs>=3)&(xs<panel.shape[1]-3)&(ys>=3)&(ys<panel.shape[0]-3)
    for dy in (0,1):panel[ys[inside]+dy,xs[inside]]=colors[inside]


def validate_kernel(kernel):
    from .dashboard import _draw_point_panel
    checks=[]
    for count in (0,1,431,13260):
        for yaw in (-32.,0.,111.):
            points=np.random.default_rng(1314).normal(size=(count,3))
            expected=_draw_point_panel(points,448,480,yaw)[0]
            receipt={}
            actual=_draw_point_panel(points,448,480,yaw,raster=kernel,raster_receipt=receipt)[0]
            if not np.array_equal(actual,expected):
                raise RuntimeError('native full-panel pixel mismatch: '+str((count,yaw)))
            if count and receipt['point_raster_backend']!='native':
                raise RuntimeError('native full-panel QA unexpectedly fell back')
            checks.append(dict(kind='full_panel',points=count,yaw=yaw,pixels_equal=True))
    # Repeated pixels, adjacent-y conflicts, clipping, half-even and nextafter.
    rng=np.random.default_rng(1306)
    count=8192
    points=np.zeros((count,3),float);points[:,2]=rng.uniform(-2.,3.,count)
    sx=np.resize(np.array([3.,3.,3.,4.,4.,444.,445.,2.,-1.,10.5,11.5,
        np.nextafter(10.5,-np.inf),np.nextafter(10.5,np.inf)]),count)
    sy=np.resize(np.array([10.,11.,10.,12.,11.,476.,477.,2.,-1.]),count)
    px=np.ascontiguousarray(sx-448*.52)
    py=np.ascontiguousarray(480*.48-sy)
    expected=np.zeros((480,448,3),np.uint8);actual=expected.copy()
    reference_raster(points,px,py,expected,1.,-2.,5.)
    outcome=kernel.try_raster(points,px,py,actual,1.,-2.,5.)
    if not outcome['used'] or not np.array_equal(expected,actual):
        raise RuntimeError('native repeated-pixel/rounding-order mismatch')
    checks.append(dict(kind='repeated_pixels_half_even_boundaries',points=count,pixels_equal=True))
    return dict(passed=True,full_panel_pixels_equal=True,overlap_order_equal=True,
        cases=checks,boundary='Only this recorded NumPy binary/runtime and native library. No cross-version duplicate-index order claim; no hardware or metrology result.')
