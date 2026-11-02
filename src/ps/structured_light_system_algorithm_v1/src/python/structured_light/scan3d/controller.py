"""Bounded stop-and-go scan: batches of MotionSession positions, display after guard revocation.

The motor permit is held only inside ``session.run_sequence``. While it is
held the HDMI adapter is replaced by a deferring proxy, exactly like the T02
after-batch policy. Reconstruction and HDMI output run only after each task
has returned with a revoked guard and a caller-supplied idle check passed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .cloud import PointCloud
from .pipeline import FrameSection


class ScanError(RuntimeError):
    """Raised when a scan batch violates its motion, capture or display contract."""


@dataclass(frozen=True)
class ScanPlan:
    total_positions: int
    pulses_per_position: int
    batch_positions: int
    estimated_position_s: float = 9.5
    guard_budget_s: float = 120.0
    guard_margin_s: float = 15.0

    def __post_init__(self) -> None:
        for name in ("total_positions", "pulses_per_position", "batch_positions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ScanError(f"{name} must be a positive integer")
        if self.total_positions % self.batch_positions:
            raise ScanError("total_positions must be a multiple of batch_positions "
                            "(the post-task idle check requires equal task sizes)")
        need = self.batch_positions * self.estimated_position_s + self.guard_margin_s
        if need > self.guard_budget_s:
            raise ScanError(f"batch of {self.batch_positions} positions needs about {need:.0f} s, "
                            f"over the {self.guard_budget_s:.0f} s guard task window")

    @property
    def batches(self) -> int:
        return self.total_positions // self.batch_positions

    @property
    def total_steps(self) -> int:
        return self.total_positions * self.pulses_per_position


class DeferredScanDisplay:
    """Stand-in HDMI adapter while the motor permit is held: records, never renders."""

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.calls: list[Any] = []

    def show_capture(self, result: Any) -> dict[str, Any]:
        if len(self.calls) >= self.expected:
            raise ScanError("deferred scan display received more captures than the batch size")
        self.calls.append(result)
        return {"display_deferred": True, "rendered": False, "shown": False,
                "position_in_batch": len(self.calls) - 1,
                "request_id": getattr(result, "request_id", None),
                "camera_epoch": getattr(result, "epoch", None)}


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def closure_check(first: FrameSection, last: FrameSection) -> dict[str, Any]:
    """Per-row object-frame gap between the first and a full-turn section."""

    both = first.valid & last.valid
    if not both.any():
        return {"rows_compared": 0}
    gap = np.linalg.norm(first.points_object_mm[both] - last.points_object_mm[both], axis=1)
    return {"rows_compared": int(both.sum()), "gap_rms_mm": float(np.sqrt(np.mean(gap ** 2))),
            "gap_median_mm": float(np.median(gap)), "gap_max_mm": float(gap.max()),
            "first_steps": first.steps, "last_steps": last.steps}


class ScanController:
    def __init__(self, session: Any, *, plan: ScanPlan, output: str | Path,
                 process: Callable[..., FrameSection],
                 render: Callable[..., bytes],
                 show: Callable[[bytes], Mapping[str, Any]] | None,
                 settle_sampler_factory: Callable[[int], Any] | None = None,
                 settle_frames: Sequence[Any] | None = None,
                 after_task_check: Callable[[int], Mapping[str, Any]] | None = None,
                 stop_event: threading.Event | None = None,
                 save_canvas_png: bool = True,
                 turntable_state: str = "unknown",
                 ply_comments: Sequence[str] = (),
                 live_feed: Any | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        config = getattr(session, "config", None)
        if getattr(config, "positions", None) != plan.batch_positions:
            raise ScanError("MotionSession positions must equal the plan batch size")
        if getattr(config, "pulses_per_position", None) != plan.pulses_per_position:
            raise ScanError("MotionSession pulses_per_position differs from the scan plan")
        self.session, self.plan = session, plan
        self.output = Path(output)
        if self.output.exists() and any(self.output.iterdir()):
            raise ScanError(f"refusing to reuse non-empty scan output {self.output}")
        self.output.mkdir(parents=True, exist_ok=True)
        self.process, self.render, self.show = process, render, show
        self.settle_sampler_factory, self.settle_frames = settle_sampler_factory, settle_frames
        self.after_task_check = after_task_check
        self.stop_event = stop_event or threading.Event()
        self.save_canvas_png = save_canvas_png
        self.turntable_state = turntable_state
        self.ply_comments = list(ply_comments)
        self.live_feed = live_feed
        (self.output / "canvases").mkdir(exist_ok=True)
        self.monotonic = monotonic
        self.cloud = PointCloud()
        self.sections: list[FrameSection] = []
        self.steps = 0
        self.state: dict[str, Any] = {"schema": "sl_scan3d_status_v1", "state": "ready",
                                      "plan": asdict(plan), "batches_completed": 0,
                                      "positions_completed": 0, "display_receipts": []}

    # -- bookkeeping -----------------------------------------------------------------
    def publish(self) -> None:
        self.state["cloud"] = self.cloud.summary()
        tmp = self.output / "status.json.tmp"
        tmp.write_text(json.dumps(self.state, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.output / "status.json")

    def _record(self, section: FrameSection, extra: Mapping[str, Any]) -> None:
        with (self.output / "positions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**section.summary(), **extra}, default=str) + "\n")

    def _display(self, raw: bytes, section: FrameSection | None, *, yaw_rad: float, lines: Sequence[str],
                 label: str) -> dict[str, Any]:
        started = self.monotonic()
        canvas = self.render(raw, section, self.cloud, yaw_rad=yaw_rad, status_lines=lines)
        receipt: dict[str, Any] = {"label": label, "canvas_sha256": hashlib.sha256(canvas).hexdigest(),
                                   "render_s": self.monotonic() - started}
        if self.save_canvas_png:
            from PIL import Image
            image = Image.fromarray(np.frombuffer(canvas, np.uint8).reshape(720, 1280, 3)[:, :, ::-1])
            image.save(self.output / "canvases" / f"{label}.png")
        if self.show is not None:
            shown = self.show(canvas)
            receipt["display"] = dict(shown) if isinstance(shown, Mapping) else {"receipt": shown}
        receipt["elapsed_s"] = self.monotonic() - started
        self.state["display_receipts"].append(receipt)
        return receipt

    def _progress(self) -> str:
        return (f"positions {self.state['positions_completed']}/{self.plan.total_positions} | "
                f"steps {self.steps}/{self.plan.total_steps}")

    def _lines(self, stage: str) -> list[str]:
        return [f"SCAN3D | {stage}",
                f"positions {self.state['positions_completed']}/{self.plan.total_positions} | steps {self.steps}"
                f"/{self.plan.total_steps} | batch {self.state['batches_completed']}/{self.plan.batches}",
                f"points {len(self.cloud)} | turntable {self.turntable_state} | v3 stripe selection",
                "no millimetre accuracy claim | missing rows are not filled"]

    def add_static(self, raw: bytes, *, request_id: int | None) -> FrameSection:
        """Process the pre-motion frame at step 0 (object-frame reference)."""

        section = self.process(raw, steps=0, position_index=0, request_id=request_id)
        _write_exclusive(self.output / "frames" / "position_0000.gray", bytes(raw))
        if self.live_feed is not None:
            self.live_feed.set_stage("STATIC REFERENCE - MOTOR DISABLED", self._progress())
            self.live_feed.submit_capture(bytes(raw), steps=0, position_index=0, request_id=request_id)
        self.cloud.add(section)
        self.sections.append(section)
        self._record(section, {"source": "static_pre_motion"})
        return section

    # -- main loop ---------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        original_display = getattr(self.session, "display", None)
        # MotionSession numbers position records from 0 in every task and never
        # overwrites them, so each batch gets its own record directory.
        original_records = getattr(self.session, "output", None)
        last_raw: bytes | None = None
        try:
            if self.sections:
                first = self.sections[0]
                raw0 = (self.output / "frames" / "position_0000.gray").read_bytes()
                last_raw = raw0
                self._display(raw0, first, yaw_rad=0.0, lines=self._lines("STATIC REFERENCE"),
                              label="batch_000")
            for batch in range(self.plan.batches):
                if self.stop_event.is_set():
                    self.state["state"] = "stopped_by_request"
                    break
                self.state["state"] = f"moving_batch_{batch + 1}"
                self.publish()
                if self.live_feed is not None:
                    self.live_feed.begin_batch(base_steps=self.steps, first_position_index=len(self.sections))
                    self.live_feed.set_stage(f"SCANNING - BATCH {batch + 1}/{self.plan.batches}", self._progress())
                    proxy: Any = self.live_feed
                else:
                    proxy = DeferredScanDisplay(self.plan.batch_positions)
                kwargs: dict[str, Any] = {"positions": self.plan.batch_positions}
                if self.settle_sampler_factory is not None:
                    kwargs["settle_sampler"] = self.settle_sampler_factory(batch)
                elif self.settle_frames is not None:
                    kwargs["settle_frames"] = self.settle_frames
                self.session.display = proxy
                if original_records is not None:
                    batch_records = Path(original_records) / f"batch_{batch:03d}"
                    batch_records.mkdir(parents=True, exist_ok=False)
                    self.session.output = batch_records
                try:
                    results = list(self.session.run_sequence(**kwargs))
                finally:
                    self.session.display = original_display
                    if original_records is not None:
                        self.session.output = original_records
                receipt = getattr(self.session, "last_task_receipt", None)
                if (len(results) != self.plan.batch_positions or not isinstance(receipt, Mapping)
                        or receipt.get("positions_completed") != self.plan.batch_positions
                        or receipt.get("guard_revoked") is not True):
                    raise ScanError("motion task did not complete the batch with a revoked guard")
                seen = proxy.batch_calls if self.live_feed is not None else proxy.calls
                if len(seen) != len(results) or any(a is not b.capture_result for a, b in zip(seen, results)):
                    raise ScanError("display hook did not see every capture of the batch in order")
                check = self.after_task_check(batch) if self.after_task_check is not None else None
                self.state.setdefault("after_task_checks", []).append(check)
                self.state["state"] = f"reconstructing_batch_{batch + 1}"
                self.publish()
                section = None
                for result in results:
                    emitted = int(result.motor_receipt.get("emitted", -1))
                    if emitted != self.plan.pulses_per_position:
                        raise ScanError("motor receipt emitted count differs from the plan")
                    self.steps += emitted
                    capture = result.capture_result
                    raw = bytes(capture.raw_bytes)
                    index = len(self.sections)
                    _write_exclusive(self.output / "frames" / f"position_{index:04d}.gray", raw)
                    section = self.process(raw, steps=self.steps, position_index=index,
                                           request_id=int(getattr(capture, "request_id", 0)))
                    self.cloud.add(section)
                    self.sections.append(section)
                    self._record(section, {"source": "motion", "batch": batch,
                                           "motor_request_id": result.motor_receipt.get("request_id"),
                                           "camera_epoch": getattr(capture, "epoch", None),
                                           "settle_elapsed_s": getattr(result, "settle_elapsed_s", None)})
                    last_raw = raw
                    self.state["positions_completed"] += 1
                self.state["batches_completed"] = batch + 1
                if self.live_feed is not None:
                    self.live_feed.set_stage(f"BATCH {batch + 1}/{self.plan.batches} VERIFIED - MOTOR DISABLED",
                                             self._progress())
                yaw = 2.0 * math.pi * (batch + 1) / max(1, self.plan.batches) * 0.25
                self._display(last_raw, section, yaw_rad=yaw, lines=self._lines("BATCH DONE - MOTOR DISABLED"),
                              label=f"batch_{batch + 1:03d}")
                self.publish()
            else:
                self.state["state"] = "complete"
            if self.sections and self.sections[0].steps == 0 and self.steps == self.plan.total_steps:
                self.state["closure_check"] = closure_check(self.sections[0], self.sections[-1])
        except BaseException as exc:
            self.state.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.session.display = original_display
            if original_records is not None:
                self.session.output = original_records
            if len(self.cloud):
                ply = self.output / "scan_cloud.ply"
                if not ply.exists():
                    self.cloud.write_ply(ply, comments=self.ply_comments)
                    self.state["ply"] = str(ply)
            self.publish()
        return self.state

    def spin(self, frames: int, *, raw: bytes | None = None) -> list[dict[str, Any]]:
        """After the scan, show the finished cloud from several viewpoints (no motion)."""

        if raw is None:
            raw = (self.output / "frames" / f"position_{len(self.sections) - 1:04d}.gray").read_bytes()
        receipts = []
        for index in range(frames):
            yaw = 2.0 * math.pi * index / max(1, frames)
            receipts.append(self._display(raw, None, yaw_rad=yaw, lines=self._lines("FINISHED - VIEW ONLY"),
                                          label=f"spin_{index:03d}"))
        self.publish()
        return receipts


__all__ = ["DeferredScanDisplay", "ScanController", "ScanError", "ScanPlan", "closure_check"]
