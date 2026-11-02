"""Accumulated multi-position point cloud with per-point provenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .pipeline import FrameSection


class PointCloud:
    """Append-only cloud. Each point keeps position, steps, source row and Q12 center."""

    def __init__(self) -> None:
        self._xyz: list[np.ndarray] = []
        self._camera: list[np.ndarray] = []
        self._position: list[np.ndarray] = []
        self._steps: list[np.ndarray] = []
        self._row: list[np.ndarray] = []
        self._center: list[np.ndarray] = []
        self._outside: list[np.ndarray] = []
        self.sections: list[dict[str, Any]] = []
        # Per-section (steps, source rows, object-frame XYZ) of valid points, for meshing.
        self.frames: list[tuple[int, np.ndarray, np.ndarray]] = []

    def add(self, section: FrameSection) -> int:
        valid = section.valid
        count = int(valid.sum())
        self._xyz.append(section.points_object_mm[valid])
        self._camera.append(section.points_camera_mm[valid])
        self._position.append(np.full(count, section.position_index, np.int32))
        self._steps.append(np.full(count, section.steps, np.int64))
        self._row.append(section.rows[valid].astype(np.int32))
        self._center.append(section.center_q12[valid].astype(np.int64))
        self._outside.append(section.outside_training[valid])
        self.sections.append(section.summary())
        self.frames.append((section.steps, section.rows[valid].copy(), section.points_object_mm[valid].copy()))
        return count

    def _cat(self, items: list[np.ndarray], shape: tuple[int, ...], dtype: Any) -> np.ndarray:
        return np.concatenate(items) if items else np.zeros(shape, dtype)

    @property
    def xyz(self) -> np.ndarray:
        return self._cat(self._xyz, (0, 3), float)

    @property
    def position(self) -> np.ndarray:
        return self._cat(self._position, (0,), np.int32)

    @property
    def outside_training(self) -> np.ndarray:
        return self._cat(self._outside, (0,), bool)

    def __len__(self) -> int:
        return int(sum(len(item) for item in self._xyz))

    def summary(self) -> dict[str, Any]:
        xyz = self.xyz
        outside = self.outside_training
        result: dict[str, Any] = {
            "positions": len(self.sections), "points": len(xyz),
            "outside_training_bounds": int(outside.sum()),
        }
        if len(xyz):
            result["bounds_mm"] = {"minimum": xyz.min(axis=0).tolist(), "maximum": xyz.max(axis=0).tolist()}
        return result

    def write_ply(self, path: str | Path, *, comments: list[str]) -> Path:
        """ASCII PLY with provenance properties; never overwrites."""

        xyz = self.xyz
        camera = self._cat(self._camera, (0, 3), float)
        position = self.position
        steps = self._cat(self._steps, (0,), np.int64)
        rows = self._cat(self._row, (0,), np.int32)
        centers = self._cat(self._center, (0,), np.int64)
        outside = self.outside_training
        output = Path(path)
        with output.open("x", encoding="ascii", newline="\n") as stream:
            stream.write("ply\nformat ascii 1.0\n")
            for comment in comments:
                stream.write("comment " + " ".join(str(comment).split()) + "\n")
            stream.write(f"element vertex {len(xyz)}\n")
            for name in ("x", "y", "z", "cam_x", "cam_y", "cam_z"):
                stream.write(f"property float {name}\n")
            for name in ("position", "steps", "src_row", "center_q12", "outside_training"):
                stream.write(f"property int {name}\n")
            stream.write("end_header\n")
            for i in range(len(xyz)):
                x, y, z = xyz[i]
                cx, cy, cz = camera[i]
                stream.write(f"{x:.6f} {y:.6f} {z:.6f} {cx:.6f} {cy:.6f} {cz:.6f} "
                             f"{position[i]} {steps[i]} {rows[i]} {centers[i]} {int(outside[i])}\n")
        return output


__all__ = ["PointCloud"]
