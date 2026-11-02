"""Explicit CPU-only build/pixel QA for the optional R14 display projection.

No HDMI, FPGA, camera, motor, network, or persistent measurement is opened.
The manifest is published only after full panel equality and ownership checks.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
for path in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',
             ROOT/'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0,str(path))

import numpy as np
from candidate.display_projection_r14 import (_ProjectionKernel,SOURCE,RASTER_SOURCE,
    MANIFEST_NAME,file_sha256,runtime_identity)
from virtual_scan.display_renderer_r19d import ReusedCanvasRenderer
from structured_light.scan3d.point_raster import _RasterKernel


def validate(library):
    kernel=_ProjectionKernel(library)
    reference=ReusedCanvasRenderer()
    # A prior QA manifest may already enable automatic discovery. The expected
    # panel must always exercise the frozen NumPy projection, never this kernel.
    reference._projected_raster=None
    # This library includes the unchanged historical raster ABI. Use it for
    # the reference too: compare NumPy projection + native raster with fusion.
    reference.base_renderer.point_raster=_RasterKernel(library)
    candidate=ReusedCanvasRenderer()
    candidate._projected_raster=kernel
    rng=np.random.default_rng(14023)
    cases=[]
    for count in (1,2,3,64,350,2400,16000):
        for yaw in (-32.,0.,90.,133.):
            points=np.ascontiguousarray(rng.normal(size=(count,3))*[30.,20.,10.])
            # Duplicate screen locations exercise last-writer and dy-pass order.
            if count>3:points[1:4]=points[0]
            for obj in (reference,candidate):obj._model_yaw_deg=yaw
            saved=points.copy()
            expected,_=reference._model_tile_fast(points,None)
            actual,_=candidate._model_tile_fast(points,None)
            if not np.array_equal(expected,actual):
                raise AssertionError('projection pixel mismatch count=%d yaw=%s pixels=%d'%(
                    count,yaw,np.count_nonzero(np.any(expected!=actual,axis=2))))
            if not np.array_equal(points,saved):raise AssertionError('input ownership changed')
            cases.append(dict(count=count,yaw=yaw,backend=candidate._model_receipt['projection_backend'],
                fallback=candidate._model_receipt['projection_fallback'],pixels_equal=True))
    # Failure must never partially paint the panel. Shape/layout, range, close,
    # and capacity checks all preserve its pre-call bytes.
    panel=np.full((480,448,3),17,dtype=np.uint8)
    right=np.array([1.,0.,0.]);up=np.array([0.,1.,0.])
    for points,reason in ((np.array([[np.nan,0.,0.]]),'nonfinite'),
                          (np.ones((2,3),np.float32),'layout')):
        before=panel.copy()
        result=kernel.try_project_raster(points,right,up,panel)
        if result['used'] or not np.array_equal(before,panel):
            raise AssertionError('failure painted panel: '+reason)
    # Half pixel guard uses the established reference rather than an alternate
    # tie-break rule. Basis producing x=232.5 exactly forces the guard.
    points=np.array([[-0.46,0.,0.],[0.46,0.,0.]],dtype=np.float64)
    # Select a basis scale that yields a deliberately half-integral screen x.
    guard_right=np.array([(232.5-448*.52)/(0.46*336.),0.,0.])
    before=panel.copy()
    result=kernel.try_project_raster(points,guard_right,up,panel)
    if result['used'] or result['reason']!='projection_rounding_boundary' or not np.array_equal(before,panel):
        raise AssertionError('rounding guard did not preserve old path')
    points=np.ascontiguousarray(rng.normal(size=(16000,3))*[30.,20.,10.])
    reference._model_yaw_deg=candidate._model_yaw_deg=-32.
    # Warm and alternate order; microbenchmark is a renderer comparison only.
    for obj in (reference,candidate):obj._model_tile_fast(points,None)
    timing={name:[] for name in ('reference','fused')}
    for iteration in range(16):
        pairs=(('reference',reference),('fused',candidate))
        if iteration%2:pairs=pairs[::-1]
        for name,obj in pairs:
            start=time.perf_counter();obj._model_tile_fast(points,None)
            timing[name].append((time.perf_counter()-start)*1000.)
    runtime=runtime_identity();digest=file_sha256(library)
    return dict(passed=True,full_panel_pixels_equal=True,overlap_order_equal=True,
        input_ownership_preserved=True,failure_panel_unchanged=True,
        rounding_guard_reference_fallback=True,runtime=runtime,library_sha256=digest,
        cases=cases,kernel=kernel.summary(),
        renderer_median_ms={name:float(np.median(values)) for name,values in timing.items()},
        reference_backend='NUMPY_PROJECTION_PLUS_UNCHANGED_NATIVE_RASTER',
        evidence_type='CPU_RENDERER_MICROBENCHMARK',hardware_access=False,
        hardware_projection=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    default=SOURCE.with_name('point_project_r14.dll' if os.name=='nt' else 'libpoint_project_r14.so')
    parser.add_argument('--library',type=Path,default=default)
    parser.add_argument('--manifest',type=Path,default=SOURCE.with_name(MANIFEST_NAME))
    parser.add_argument('--build',action='store_true')
    args=parser.parse_args()
    if args.manifest.exists():raise FileExistsError('preserve existing projection manifest')
    if args.library.parent.resolve()!=args.manifest.parent.resolve():
        raise ValueError('manifest must be beside library')
    if args.build:
        if args.library.exists():raise FileExistsError('preserve existing projection library')
        command=['gcc','-O3','-std=c99','-fno-fast-math','-ffp-contract=off',
            '-fexcess-precision=standard','-frounding-math','-Wall','-Wextra','-Werror','-shared']
        if os.name!='nt':command.append('-fPIC')
        command.extend([str(SOURCE),'-o',str(args.library),'-lm'])
        subprocess.run(command,check=True)
    qa=validate(args.library)
    manifest=dict(schema='DISPLAY_PROJECTION_R14_V1',runtime=runtime_identity(),
        library=args.library.name,library_sha256=file_sha256(args.library),
        source_sha256=file_sha256(SOURCE),raster_source_sha256=file_sha256(RASTER_SOURCE),
        validation=qa)
    with args.manifest.open('x',encoding='utf-8') as stream:
        json.dump(manifest,stream,indent=2);stream.write('\n')
    print(json.dumps(dict(manifest=str(args.manifest),qa=qa),indent=2))


if __name__=='__main__':main()
