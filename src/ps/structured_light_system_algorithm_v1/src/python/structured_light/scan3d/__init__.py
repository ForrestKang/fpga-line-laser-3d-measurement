"""Stop-and-go turntable scan: stripe selection, XYZ sections, point cloud and HDMI canvas."""

from .cloud import PointCloud
from .controller import DeferredScanDisplay, ScanController, ScanError, ScanPlan, closure_check
from .pipeline import (
    DEFAULT_SELECTION, DEFAULT_STRIPE_CONFIG, FrameSection, ScanCalibration, ScanPipelineError,
    process_frame,
)
from .render import build_canvas
from .surface import SurfaceMesh, draw_surface_panel, surface_mesh
from .turntable import (
    ASSUMED, CALIBRATED, TurntableError, TurntableModel, assumed_turntable, board_pose,
    calibrate_turntable,
)

__all__ = [
    "ASSUMED", "CALIBRATED", "DEFAULT_SELECTION", "DEFAULT_STRIPE_CONFIG", "DeferredScanDisplay",
    "FrameSection", "PointCloud", "ScanCalibration", "ScanController", "ScanError", "ScanPipelineError",
    "ScanPlan", "SurfaceMesh", "TurntableError", "TurntableModel", "assumed_turntable", "board_pose", "build_canvas",
    "calibrate_turntable", "closure_check", "draw_surface_panel", "process_frame", "surface_mesh",
]
