"""Offline Mock/Replay command line entry point for P2 host validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ..frame_io import GrayFrame, load_gray8_png
from .controller import P2Controller
from .session import SessionRecorder
from .transport import MockTransport, ReplayTransport


def _synthetic(width: int, height: int) -> GrayFrame:
    pixels = bytearray(width * height)
    for y in range(height):
        center = width // 2 + (y % 3) - 1
        for x in range(max(0, center - 1), min(width, center + 2)):
            pixels[y * width + x] = (70, 130, 90)[x - center + 1]
    return GrayFrame.from_pixels(width, height, pixels)


def _capture(args: argparse.Namespace) -> int:
    frame = load_gray8_png(args.frame) if args.frame else _synthetic(args.width, args.height)
    recorder = SessionRecorder(args.output, metadata={"mode": "mock", "frame_width": frame.width, "frame_height": frame.height}) if args.output else None
    controller = P2Controller(MockTransport(frame), recorder=recorder, default_timeout_s=args.timeout)
    try:
        controller.set_threshold(args.threshold)
        controller.configure_roi(0, 0, frame.width, frame.height)
        result = controller.capture_pair(timeout_s=args.timeout)
        summary = {
            "mode": "mock", "request_id": result.request_id, "epoch": result.epoch,
            "frame_width": frame.width, "frame_height": frame.height,
            "raw_length": len(result.raw_bytes), "profile_length": len(result.profile_payload),
            "valid_count": result.profile.valid_count,
            "same_frame_passed": result.comparison.passed if result.comparison else None,
            "mismatch_count": result.comparison.mismatch_count if result.comparison else None,
            "angle_valid": result.angle_valid, "motion_enabled": result.motion_enabled,
            "session_id": result.session_id,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["same_frame_passed"] else 2
    finally:
        controller.close()


def _replay(args: argparse.Namespace) -> int:
    transport = ReplayTransport(args.session)
    controller = P2Controller(transport, default_timeout_s=args.timeout)
    try:
        result = controller.capture_pair(timeout_s=args.timeout)
        print(json.dumps({
            "mode": "replay", "request_id": result.request_id, "raw_length": len(result.raw_bytes),
            "profile_length": len(result.profile_payload),
            "same_frame_passed": result.comparison.passed if result.comparison else None,
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.comparison and result.comparison.passed else 2
    finally:
        controller.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="structured-light-p2", description="Offline P2 Mock/Replay acquisition")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="capture one same-frame pair through MockTransport")
    capture.add_argument("--frame", type=Path)
    capture.add_argument("--width", type=int, default=640)
    capture.add_argument("--height", type=int, default=480)
    capture.add_argument("--threshold", type=int, default=30)
    capture.add_argument("--timeout", type=float, default=2.0)
    capture.add_argument("--output", type=Path)
    capture.set_defaults(func=_capture)
    replay = sub.add_parser("replay", help="replay one SessionRecorder directory")
    replay.add_argument("session", type=Path)
    replay.add_argument("--timeout", type=float, default=2.0)
    replay.set_defaults(func=_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"p2 error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
