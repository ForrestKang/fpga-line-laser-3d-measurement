"""Motion-process side of the live scan display.

``LiveScanFeed`` is installed as ``MotionSession.display`` during each batch.
``show_capture`` only queues the frame and returns at once, so the guarded
motion task never waits for rendering. One sender thread owns the display
owner connection: it forwards queued captures first and, when idle, asks for a
redraw at a slowly advancing view angle. Display errors are recorded and stop
the display only; they never alter motion, capture or saved evidence.
"""

from __future__ import annotations

from collections import deque
import threading
import time
from typing import Any, Callable

FRAME_W, FRAME_H = 752, 480
_DUMMY = b"\x00"


class LiveScanFeed:
    def __init__(self, output: Any, *, pulses_per_position: int, view_period_s: float = 1.0,
                 yaw_step_deg: float = 6.0, views_enabled: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if pulses_per_position <= 0:
            raise ValueError("pulses_per_position must be positive")
        self.output = output
        self.pulses = int(pulses_per_position)
        self.view_period_s = float(view_period_s)
        self.yaw_step_deg = float(yaw_step_deg)
        self.views_enabled = bool(views_enabled)
        self.clock = clock
        self._queue: deque[dict[str, Any]] = deque()
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._yaw = 0.0
        self._stage = "READY"
        self._progress = ""
        self._batch_base = 0
        self._batch_first_index = 0
        self.batch_calls: list[Any] = []
        self.sent: list[dict[str, Any]] = []
        self.receipts: deque[dict[str, Any]] = deque(maxlen=32)
        self.error: str | None = None
        self.views_sent = 0

    # -- setup / lifecycle -------------------------------------------------------------------------
    def setup(self, *, camera: str, plane: str, turntable: dict[str, Any], expected: dict[str, str],
              title: str = "SCAN3D LIVE") -> dict[str, Any]:
        """Synchronous, before any motion: the owner must load the same calibration files."""

        receipt = self.output.show_preview(_DUMMY, 1, 1, kind="setup", camera=str(camera), plane=str(plane),
                                           turntable=turntable, title=title)
        for key, value in expected.items():
            if receipt.get(key) != value:
                raise RuntimeError(f"display owner {key} {receipt.get(key)!r} differs from {value!r}")
        return dict(receipt)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("feed already started")
        self._thread = threading.Thread(target=self._run, name="scan3d-display-feed", daemon=True)
        self._thread.start()

    def stop(self, *, drain: bool = True, timeout_s: float = 30.0) -> dict[str, Any]:
        deadline = self.clock() + timeout_s
        if drain:
            with self._cv:
                while self._queue and self.error is None and self.clock() < deadline:
                    self._cv.wait(timeout=0.05)
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, deadline - self.clock()))
        return self.summary()

    def summary(self) -> dict[str, Any]:
        return {"captures_sent": len(self.sent), "views_sent": self.views_sent, "queued": len(self._queue),
                "error": self.error, "thread_alive": bool(self._thread and self._thread.is_alive()),
                "last_receipts": list(self.receipts)[-4:]}

    # -- producer API (motion process threads) ---------------------------------------------------
    def set_stage(self, stage: str, progress: str | None = None) -> None:
        with self._cv:
            self._stage = stage
            if progress is not None:
                self._progress = progress
            self._cv.notify_all()

    def submit_capture(self, raw: bytes, *, steps: int, position_index: int, request_id: int | None) -> None:
        if len(raw) != FRAME_W * FRAME_H:
            raise ValueError("capture must be a complete 752x480 gray frame")
        with self._cv:
            self._queue.append({"raw": bytes(raw), "steps": int(steps), "position_index": int(position_index),
                                "request_id": request_id})
            self._cv.notify_all()

    def begin_batch(self, *, base_steps: int, first_position_index: int) -> None:
        self._batch_base = int(base_steps)
        self._batch_first_index = int(first_position_index)
        self.batch_calls = []

    def show_capture(self, result: Any) -> dict[str, Any]:
        """MotionSession display hook: queue only, never render or block."""

        k = len(self.batch_calls)
        self.batch_calls.append(result)
        steps = self._batch_base + (k + 1) * self.pulses
        index = self._batch_first_index + k
        request_id = getattr(result, "request_id", None)
        self.submit_capture(bytes(result.raw_bytes), steps=steps, position_index=index, request_id=request_id)
        return {"display_deferred": False, "display_queued": True, "rendered": False, "shown": False,
                "position_index": index, "nominal_steps": steps, "request_id": request_id,
                "camera_epoch": getattr(result, "epoch", None)}

    # -- sender thread ---------------------------------------------------------------------------------
    def _run(self) -> None:
        next_view = self.clock() + self.view_period_s
        while True:
            with self._cv:
                while not self._queue and not self._stopping:
                    if self.views_enabled and self.sent and self.clock() >= next_view:
                        break
                    self._cv.wait(timeout=max(0.01, min(0.25, next_view - self.clock())))
                if self._stopping and not self._queue:
                    return
                item = self._queue.popleft() if self._queue else None
                stage, progress = self._stage, self._progress
            try:
                self._yaw = (self._yaw + self.yaw_step_deg) % 360.0
                if item is not None:
                    receipt = self.output.show_preview(item["raw"], FRAME_W, FRAME_H, kind="capture",
                                                       steps=item["steps"], position_index=item["position_index"],
                                                       request_id=item["request_id"], stage=stage,
                                                       progress=progress, yaw_deg=self._yaw)
                    self.sent.append({k: item[k] for k in ("steps", "position_index", "request_id")})
                else:
                    receipt = self.output.show_preview(_DUMMY, 1, 1, kind="view", stage=stage,
                                                       progress=progress, yaw_deg=self._yaw)
                    self.views_sent += 1
                self.receipts.append(dict(receipt) if isinstance(receipt, dict) else {"receipt": receipt})
            except BaseException as exc:  # display only; never propagate into motion
                with self._cv:
                    self.error = f"{type(exc).__name__}: {exc}"
                    self._queue.clear()
                    self._cv.notify_all()
                return
            next_view = self.clock() + self.view_period_s
            with self._cv:
                self._cv.notify_all()


__all__ = ["LiveScanFeed"]
