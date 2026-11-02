"""Finite native static R15 chain, optional historical-geometry workload."""
from pathlib import Path
import argparse
import json
import os
import sys

import static_segments_bench_r15 as bench
from structured_light.scan3d.transport_timing_r14 import instrument_transport, timed_capture


def make_dependencies(args):
    deps = bench.dependencies(args.source)
    original = deps.runtime_factory
    def runtime_factory(*a, **kw):
        runtime = original(*a, **kw)
        if args.dma_pool:
            from structured_light.scan3d.dma_pool_r14 import install_runtime_dma_pool
            install_runtime_dma_pool(runtime)
        instrument_transport(runtime.transport)
        return runtime
    deps.runtime_factory = runtime_factory
    deps.capture = timed_capture(deps.capture)
    writer_factory = deps.writer_factory
    def grouped_writer(*a, **kwargs):
        kwargs['max_delay_s'] = args.writer_delay
        return writer_factory(*a, **kwargs)
    deps.writer_factory = grouped_writer
    if args.packed_metadata:
        from structured_light.scan3d.summary_codec_r14 import pack_summary
        deps.pack_summary = pack_summary
    if args.primary_screen:
        raise ValueError('R15 uses admitted PL RSG1 decisions; legacy primary-screen is unsupported')
    if args.camera is not None:
        from structured_light.scan3d.selected_geometry_r14 import SelectedGeometryBenchmark
        from structured_light.scan3d.processed_display import ProcessedObserver
        from structured_light.scan3d.dashboard import DashboardRenderer
        geometry = SelectedGeometryBenchmark(args.camera, args.plane, vector_geometry=args.vector_geometry)
        deps.process_selected = geometry
        deps.geometry_enabled = True
        deps.geometry_declaration = dict(source='historical_frozen_calibration_computational_workload',
            current_optical_validation_passed=False, axis_validated=False,
            camera_sha256=geometry.calibration.camera_sha256, plane_sha256=geometry.calibration.plane_sha256,
            original_range_and_training_bounds_enforced=True, physical_accuracy_verified=False)
        def observer_factory(remote, roi):
            if args.owner_scene:
                from structured_light.scan3d.live_static_scene_r14 import (
                    OwnerLiveStaticSceneObserver, LiveStaticSceneSink)
                return OwnerLiveStaticSceneObserver(source='live_static', sink=LiveStaticSceneSink(remote),
                                                     asynchronous=True, capacity=1)
            observer = ProcessedObserver(source='live_static', sink=bench.ConfirmedSink(remote),
                renderer=DashboardRenderer(), asynchronous=True, capacity=1)
            observer._title = 'LIVE STATIC XYZ / HISTORICAL CALIBRATION / NO METROLOGY'
            return observer
        deps.observer_factory = observer_factory
    if args.owner_scene:
        def scene_display_factory(runtime, manifest, output, **unused):
            from structured_light.p2.hdmi_owner import RemoteHDMIOutput
            previous = os.environ.get('SL_HDMI_PROCESSED_CANVAS')
            try:
                os.environ['SL_HDMI_PROCESSED_CANVAS'] = '1'
                return RemoteHDMIOutput(manifest,owner_log=output/'HDMI_OWNER.log',timeout_s=5.,
                    startup_timeout_s=60.,preview_only=True,hardware_lock_fd=runtime.hardware_lock._handle.fileno(),
                    shared_scene=True,scene_raster_manifest=args.scene_raster_manifest,
                    scene_sources=('replay','live_static'))
            finally:
                if previous is None: os.environ.pop('SL_HDMI_PROCESSED_CANVAS',None)
                else: os.environ['SL_HDMI_PROCESSED_CANVAS'] = previous
        deps.display_factory = scene_display_factory
    return deps


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source','manifest','output','power-off-proof'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--operator-state', type=Path)
    parser.add_argument('--camera', type=Path)
    parser.add_argument('--plane', type=Path)
    parser.add_argument('--frames', type=int, default=120)
    parser.add_argument('--exposure-lines', type=int, default=80)
    parser.add_argument('--roi', type=int, nargs=4, default=(220,225,420,450))
    parser.add_argument('--load', action='store_true', help='Explicit new reviewed FPGA load; requires 12V-OFF artifact-bound proof')
    parser.add_argument('--display', action='store_true')
    parser.add_argument('--shared-canvas', action='store_true')
    parser.add_argument('--primary-screen', action='store_true')
    parser.add_argument('--packed-metadata', action='store_true')
    parser.add_argument('--vector-geometry', action='store_true')
    parser.add_argument('--dma-pool', action='store_true')
    parser.add_argument('--owner-scene', action='store_true')
    parser.add_argument('--scene-raster-manifest',type=Path)
    parser.add_argument('--concurrent', action='store_true')
    parser.add_argument('--processing-capacity', type=int, default=3)
    parser.add_argument('--processing-timeout', type=float, default=10.)
    parser.add_argument('--capacity', type=int, default=16)
    parser.add_argument('--writer-delay', type=float, default=.05)
    parser.add_argument('--capture-timeout', type=float, default=2.)
    parser.add_argument('--writer-timeout', type=float, default=10.)
    parser.add_argument('--display-timeout', type=float, default=10.)
    args = parser.parse_args(argv)
    if not args.load and args.receipt is None:
        parser.error('attach requires an explicit latest clean receipt')
    if args.load and args.receipt is not None:
        parser.error('choose explicit load or attach receipt, not both')
    if (args.camera is None) != (args.plane is None):
        parser.error('camera and plane must be supplied together')
    if not 0 < args.writer_delay <= .5:
        parser.error('writer-delay must be in (0,.5] seconds')
    if args.vector_geometry and args.camera is None:
        parser.error('vector-geometry requires explicit camera and plane')
    if args.owner_scene and (not args.display or args.camera is None or args.scene_raster_manifest is None):
        parser.error('owner-scene requires display, explicit geometry and native raster manifest')
    # Explicit --load is admitted by the production static runtime and power-OFF proof; no motion path.
    import cv2
    cv2.setNumThreads(1)
    try:
        result = bench.run_bench(args, deps=make_dependencies(args))
    except Exception as exc:
        print(json.dumps(dict(passed=False, entry_error=type(exc).__name__+': '+str(exc))), flush=True)
        if bench._QUARANTINED: bench.wait_quarantined()
        return 2
    print(json.dumps({key:result.get(key) for key in ('passed','failure','capture_count',
        'full_chain_fps','actual_hdmi_acknowledged_fps','xyz_computed','processing_pipeline','quarantined')}), flush=True)
    if result.get('quarantined'): bench.wait_quarantined()
    return 0 if result['passed'] else 1


if __name__ == '__main__': raise SystemExit(main())
