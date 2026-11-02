"""Offline replay inputs: saved frames presented through the MotionSession interface."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from .pipeline import FRAME_HEIGHT, FRAME_WIDTH


FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT


@dataclass(frozen=True)
class SavedFrame:
    raw: bytes
    steps: int
    request_id: int | None
    source: str
    angle_source: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


def _read_raw(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) != FRAME_BYTES:
        raise ValueError(f"{path} is not a 752x480 gray8 frame")
    return data


def load_tenpoint(case_dir: str | Path, *, pulses_per_position: int = 320) -> tuple[SavedFrame, list[SavedFrame]]:
    """T02 ten-point stop-and-go batch: static pre-motion frame plus ten settled positions."""

    case = Path(case_dir)
    static_path = case / "prepare_evidence/captures/frame_000/raw_gray8.bin"
    static = SavedFrame(_read_raw(static_path), 0, None, str(static_path), "motor_steps_nominal")
    frames = []
    for index in range(10):
        path = case / f"sequence_evidence/captures/frame_{index:03d}/raw_gray8.bin"
        frames.append(SavedFrame(_read_raw(path), (index + 1) * pulses_per_position, None, str(path),
                                 "motor_steps_nominal"))
    return static, frames


def load_rotating(case_dir: str | Path, *, steps_per_rev: int = 3200,
                  motor_worker_s: float = 12.137351508001302) -> list[SavedFrame]:
    """Continuous-rotation frames; the step count is estimated from capture time only."""

    case = Path(case_dir)
    records = [json.loads(line) for line in (case / "evidence_02/frames.jsonl").read_text().splitlines() if line]
    rotating = [record for record in records if str(record.get("stage", "")).startswith("ROTATING")]
    if not rotating:
        raise ValueError("no rotating-stage frames in frames.jsonl")
    start = float(rotating[0]["capture_started_s"])
    frames = []
    for record in rotating:
        path = case / "evidence_02/frames" / f"frame_{int(record['index']):04d}.gray"
        raw = _read_raw(path)
        if hashlib.sha256(raw).hexdigest() != record["raw_sha256"]:
            raise ValueError(f"{path} does not match its recorded SHA-256")
        middle = 0.5 * (float(record["capture_started_s"]) + float(record["capture_completed_s"]))
        steps = int(round(steps_per_rev * (middle - start) / motor_worker_s))
        frames.append(SavedFrame(raw, steps, int(record["request_id"]), str(path), "capture_time_estimate"))
    return frames


class SavedFrameSession:
    """Duck-typed MotionSession that returns saved frames instead of moving a motor."""

    mode = "replay_saved_frames"

    def __init__(self, frames: Sequence[SavedFrame], *, batch_positions: int, pulses_per_position: int) -> None:
        self.frames = list(frames)
        self.config = SimpleNamespace(positions=batch_positions, pulses_per_position=pulses_per_position)
        self.display: Any = None
        self.last_task_receipt: dict[str, Any] | None = None
        self._cursor = 0
        self._steps = 0
        self._motor_id = 1000

    def run_sequence(self, positions: int | None = None, **_: Any) -> tuple[Any, ...]:
        count = self.config.positions if positions is None else int(positions)
        results = []
        for _index in range(count):
            frame = self.frames[self._cursor]
            self._cursor += 1
            self._steps += self.config.pulses_per_position
            if frame.steps != self._steps:
                raise ValueError("saved frame step count does not follow the replayed pulse plan")
            self._motor_id += 1
            capture = SimpleNamespace(raw_bytes=frame.raw, request_id=frame.request_id or self._cursor,
                                      epoch=0, input_sha256=frame.sha256)
            if self.display is not None:
                self.display.show_capture(capture)
            results.append(SimpleNamespace(
                motor_receipt={"emitted": self.config.pulses_per_position, "request_id": self._motor_id,
                               "done": True},
                capture_result=capture, settle_elapsed_s=None))
        self.last_task_receipt = {"positions_requested": count, "positions_completed": count,
                                  "guard_revoked": True, "hardware_evidence": self.mode}
        return tuple(results)


__all__ = ["SavedFrame", "SavedFrameSession", "load_rotating", "load_tenpoint"]
