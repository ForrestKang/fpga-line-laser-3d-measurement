"""Turntable 3D scan on the already loaded Stage B Overlay, plus offline replay and calibration.

Subcommands
-----------
replay               Saved frames only. No device, Overlay, camera or motor access.
calibrate-turntable  Fit the axis and angle-per-step from a finished checkerboard scan directory.
live                 Board only. Bounded stop-and-go batches through the existing MotionSession;
                     reconstruction and HDMI output run only after each batch's guard revocation.

Without a fitted turntable model every output is stamped as an assumed-axis preview.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path, PureWindowsPath
import sys
import threading
import time
from typing import Any, Callable, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
ALGORITHM_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_GEOMETRY_ROOT = ALGORITHM_ROOT.parent / "structured_light_system_geometry_v1"
STRIPE_ROI = (620, 210, 710, 430)
THRESHOLD = 60


def _paths(geometry_root: Path) -> None:
    for path in (ALGORITHM_ROOT / "src" / "python", Path(geometry_root) / "src" / "python", SCRIPT_DIR):
        value = str(Path(path).resolve())
        if value not in sys.path:
            sys.path.insert(0, value)


def _default_calibration(geometry_root: Path) -> tuple[Path, Path]:
    camera = geometry_root / "data/calibration/camera_combined_train_v2_20261017/camera_intrinsics_candidate.json"
    pointer = json.loads((geometry_root / "report/laser_plane_current.json").read_text(encoding="utf-8"))
    recorded = PureWindowsPath(pointer["candidate"])
    rel = recorded.relative_to(PureWindowsPath("D:/NUECSDC-FPGA/structured_light_system_geometry_v1"))
    return camera, geometry_root.joinpath(*rel.parts)


def _turntable(args: Any, calibration: Any, reference_raw: bytes | None) -> Any:
    from structured_light.scan3d import TurntableModel, assumed_turntable, process_frame
    from structured_light.scan3d.turntable import placeholder_axis_from_stripe

    if args.turntable is not None:
        return TurntableModel.from_json(json.loads(Path(args.turntable).read_text(encoding="utf-8")))
    if not args.allow_assumed_axis:
        raise SystemExit("no --turntable model: pass --allow-assumed-axis to render an uncalibrated preview")
    if args.assumed_axis_point is not None and args.assumed_axis_direction is not None:
        point, direction = args.assumed_axis_point, args.assumed_axis_direction
        source = "operator-declared axis"
    else:
        if reference_raw is None:
            raise SystemExit("assumed axis needs --assumed-axis-point/--assumed-axis-direction")
        probe = assumed_turntable((0, 0, 0), (0, 1, 0), source="probe")
        section = process_frame(reference_raw, steps=0, position_index=0, calibration=calibration, turntable=probe)
        point, direction = placeholder_axis_from_stripe(section.points_camera_mm[section.valid], calibration.plane,
                                                        offset_mm=args.assumed_axis_offset_mm)
        source = (f"placeholder: laser-plane axis {args.assumed_axis_offset_mm:g} mm behind the step-0 stripe; "
                  "declared, not measured")
    return assumed_turntable(point, direction, steps_per_rev=args.steps_per_rev, source=source)


def _make_process(calibration: Any, turntable: Any) -> Callable[..., Any]:
    from structured_light.scan3d import process_frame

    def process(raw: bytes, *, steps: int, position_index: int, request_id: int | None) -> Any:
        return process_frame(raw, steps=steps, position_index=position_index, calibration=calibration,
                             turntable=turntable, request_id=request_id)
    return process


def _make_render(turntable: Any) -> Callable[..., bytes]:
    from structured_light.scan3d import build_canvas

    def render(raw: bytes, section: Any, cloud: Any, *, yaw_rad: float, status_lines: list[str]) -> bytes:
        return build_canvas(raw, section, cloud, turntable, yaw_rad=yaw_rad, status_lines=status_lines)
    return render


def _ply_comments(turntable: Any, calibration: Any, mode: str) -> list[str]:
    from structured_light.scan3d import DEFAULT_SELECTION, DEFAULT_STRIPE_CONFIG

    return [f"sl_scan3d mode={mode}", f"turntable_state={turntable.state}",
            f"turntable_fingerprint={turntable.fingerprint}", f"turntable_source={turntable.source}",
            f"camera_sha256={calibration.camera_sha256}", f"plane_sha256={calibration.plane_sha256}",
            f"stripe_config={DEFAULT_STRIPE_CONFIG.fingerprint}", f"selection_v3={DEFAULT_SELECTION.fingerprint}",
            "units=mm object_frame=camera_frame_at_step_0", "physical_accuracy_verified=false"]


# -- replay ---------------------------------------------------------------------------------
def run_replay(args: Any) -> dict[str, Any]:
    _paths(args.geometry_root)
    from structured_light.scan3d import PointCloud, ScanCalibration, ScanController, ScanPlan, closure_check
    from structured_light.scan3d.replay import SavedFrameSession, load_rotating, load_tenpoint

    camera_path, plane_path = (args.camera, args.plane) if args.camera else _default_calibration(args.geometry_root)
    calibration = ScanCalibration.load(camera_path, plane_path)
    output = Path(args.output)
    if args.source == "tenpoint":
        static, frames = load_tenpoint(args.case)
        turntable = _turntable(args, calibration, static.raw)
        plan = ScanPlan(total_positions=len(frames), pulses_per_position=320, batch_positions=args.batch_positions)
        session = SavedFrameSession(frames, batch_positions=plan.batch_positions,
                                    pulses_per_position=plan.pulses_per_position)
        controller = ScanController(session, plan=plan, output=output, process=_make_process(calibration, turntable),
                                    render=_make_render(turntable), show=None, turntable_state=turntable.state,
                                    ply_comments=_ply_comments(turntable, calibration, "replay_tenpoint"))
        controller.state["angle_source"] = "motor_steps_nominal"
        controller.add_static(static.raw, request_id=None)
        state = controller.run()
        if args.spin_frames:
            controller.spin(args.spin_frames)
        state = controller.state
    else:
        frames = load_rotating(args.case, steps_per_rev=args.steps_per_rev)
        turntable = _turntable(args, calibration, frames[0].raw)
        output.mkdir(parents=True, exist_ok=False)
        (output / "canvases").mkdir()
        cloud = PointCloud()
        render = _make_render(turntable)
        process = _make_process(calibration, turntable)
        sections = []
        from PIL import Image
        import numpy as np
        for index, frame in enumerate(frames):
            section = process(frame.raw, steps=frame.steps, position_index=index, request_id=frame.request_id)
            cloud.add(section)
            sections.append(section)
            if index % args.canvas_every == 0 or index == len(frames) - 1:
                canvas = render(frame.raw, section, cloud, yaw_rad=0.5 * math.pi * index / len(frames),
                                status_lines=["SCAN3D REPLAY | continuous-rotation frames",
                                              f"frame {index + 1}/{len(frames)} | steps estimated from capture time",
                                              f"points {len(cloud)} | turntable {turntable.state}",
                                              "motion blur present; angles are time estimates"])
                Image.fromarray(np.frombuffer(canvas, np.uint8).reshape(720, 1280, 3)[:, :, ::-1]).save(
                    output / "canvases" / f"frame_{index:03d}.png")
        cloud.write_ply(output / "scan_cloud.ply", comments=_ply_comments(turntable, calibration, "replay_rotating"))
        state = {"schema": "sl_scan3d_status_v1", "state": "complete", "angle_source": "capture_time_estimate",
                 "cloud": cloud.summary(), "sections": cloud.sections,
                 "closure_check": closure_check(sections[0], sections[-1])}
        (output / "status.json").write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    state["turntable"] = turntable.to_json()
    state["calibration"] = {"camera": str(camera_path), "camera_sha256": calibration.camera_sha256,
                            "plane": str(plane_path), "plane_sha256": calibration.plane_sha256}
    state["hardware_accessed"] = False
    (output / "summary.json").write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    return state


# -- turntable calibration from a checkerboard scan --------------------------------------------
def run_calibrate(args: Any) -> dict[str, Any]:
    _paths(args.geometry_root)
    import numpy as np
    from structured_light.scan3d import ScanCalibration, board_pose, calibrate_turntable

    camera_path, plane_path = (args.camera, args.plane) if args.camera else _default_calibration(args.geometry_root)
    calibration = ScanCalibration.load(camera_path, plane_path)
    scan = Path(args.scan_dir)
    records = [json.loads(line) for line in (scan / "positions.jsonl").read_text().splitlines() if line]
    poses, used = [], []
    for record in records:
        raw = (scan / "frames" / f"position_{int(record['position_index']):04d}.gray").read_bytes()
        gray = np.frombuffer(raw, np.uint8).reshape(480, 752)
        pose = board_pose(gray, calibration.camera_matrix, calibration.distortion,
                          inner_corners=tuple(args.inner_corners), square_size_mm=args.square_mm)
        used.append({"position_index": record["position_index"], "steps": record["steps"], "found": pose["found"],
                     "reprojection_rms_px": pose.get("reprojection_rms_px")})
        if pose["found"]:
            poses.append((pose["rotation"], pose["translation_mm"], int(record["steps"])))
    if len(poses) < 3:
        raise SystemExit(f"only {len(poses)} checkerboard poses found; need at least 3")
    fit = calibrate_turntable([p[0] for p in poses], [p[1] for p in poses], [p[2] for p in poses],
                              source=f"checkerboard scan {scan.name}; square {args.square_mm} mm nominal")
    model = fit.pop("model")
    output = Path(args.output)
    with output.open("x", encoding="utf-8") as stream:
        json.dump({**model.to_json(), "fit": fit, "views": used,
                   "camera_sha256": calibration.camera_sha256}, stream, indent=2)
    return {"model": model.to_json(), **fit, "views_found": len(poses), "views_total": len(records)}


# -- live board scan ---------------------------------------------------------------------------
def _open_live_display(args: Any, output: Path) -> Any:
    """Start the separate HDMI owner process that renders the live scan (board only)."""

    import os
    from b_alignment_preview import prepare_display_artifacts
    from structured_light.motion.pynq_runtime import verify_stage_b_artifacts
    from structured_light.p2 import hdmi_owner

    alias = prepare_display_artifacts(verify_stage_b_artifacts(args.manifest), output)
    original = hdmi_owner.__file__
    saved = {key: os.environ.get(key) for key in ("SL_SCAN3D_SOURCE_ROOT", "SL_SCAN3D_GEOMETRY_ROOT")}
    try:
        # The owner subprocess runs scan3d_owner.py, which wraps the unmodified serve loop.
        hdmi_owner.__file__ = str(SCRIPT_DIR / "scan3d_owner.py")
        os.environ["SL_SCAN3D_SOURCE_ROOT"] = str(Path(args.source_root).resolve())
        os.environ["SL_SCAN3D_GEOMETRY_ROOT"] = str(Path(args.geometry_root).resolve())
        display = hdmi_owner.RemoteHDMIOutput(alias, owner_log=output / "hdmi_owner.log", timeout_s=10.0,
                                              preview_only=True)
    finally:
        hdmi_owner.__file__ = original
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    display.configure()
    return display



def latest_persisted_capture_identity(previous: Mapping[str, Any] | None,
                                      samplers: list[Any]) -> dict[str, int] | None:
    """Include successful settle raw captures when closing a failed task.

    A sampler publishes this identity only after the raw frame and its JSONL
    record are durable. It does not imply the settle decision passed. Epoch
    changes remain an error rather than a guessed cross-generation ordering.
    """
    latest: dict[str, int] | None = None
    values = [previous] + [getattr(sampler, "last_persisted_capture", None) for sampler in samplers]
    for value in values:
        if value is None:
            continue
        if not isinstance(value, Mapping):
            raise ValueError("capture identity must be a mapping")
        request, epoch = value.get("request_id"), value.get("epoch")
        if type(request) is not int or not 0 <= request <= 0xffffffff:
            raise ValueError("capture request_id must be uint32")
        if type(epoch) is not int or not 0 <= epoch <= 0xffff:
            raise ValueError("capture epoch must be uint16")
        item = {"request_id": request, "epoch": epoch}
        if latest is None:
            latest = item
        elif epoch != latest["epoch"]:
            raise ValueError("capture epoch changed during one runtime")
        elif 0 < (request - latest["request_id"]) % (1 << 32) < (1 << 31):
            latest = item
    return latest


def run_live(args: Any, *, runtime_factory: Callable[..., Any] | None = None,
             sensor_access_factory: Callable[..., Any] | None = None,
             monitor_runner: Callable[..., Any] | None = None, signal_assessor: Callable[..., Any] | None = None,
             settle_sampler_factory: Callable[..., Any] | None = None,
             capture_helper_loader: Callable[..., Any] | None = None,
             display_factory: Callable[[Any, Path], Any] | None = None,
             sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
             stop_event: threading.Event | None = None) -> dict[str, Any]:
    """Live scan. Every hardware dependency can be injected for offline tests."""

    _paths(args.geometry_root)
    import t02_motion_round as t02
    from structured_light.motion.types import MotionConfig
    from structured_light.scan3d import ScanCalibration, ScanController, ScanPlan

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    state: dict[str, Any] = {"schema": "sl_scan3d_live_v1", "state": "starting", "passed": False,
                             "motion_attempted": False, "overlay_downloads": 0, "sensor_write_count": 0}

    def publish() -> None:
        t02._atomic_json(output / "live_status.json", state)

    plan = ScanPlan(total_positions=args.positions, pulses_per_position=args.pulses_per_position,
                    batch_positions=args.batch_positions)
    state["plan"] = {"positions": plan.total_positions, "pulses_per_position": plan.pulses_per_position,
                     "batch_positions": plan.batch_positions, "total_steps": plan.total_steps}
    if args.motion_confirmation is None or args.driver_proof is None:
        raise SystemExit("live scan requires fresh --motion-confirmation and --driver-proof")
    publish()
    runtime = helper = None
    feed = live_display = None
    realtime = not args.no_display and getattr(args, "display_mode", "realtime") == "realtime"
    state["display_mode"] = "none" if args.no_display else ("realtime" if realtime else "after_batch")
    last_capture: Mapping[str, Any] | None = None
    samplers: list[Any] = []
    close_valid = False
    root = t02._source_root(args.source_root)
    runtime_output = output / "runtime"
    try:
        loader = capture_helper_loader or t02._load_capture_helper
        helper, _, helper_sha = loader(args.capture_helper, args.capture_helper_sha256)
        exposure_path, exposure_sha = helper.verify_module_digest(args.exposure_module, args.module_sha256)
        state.update(capture_helper_sha256=helper_sha, exposure_module_sha256=exposure_sha)
        if sensor_access_factory is None:
            access_class, _ = helper.load_frozen_sccb_access(exposure_path, exposure_sha)

            def sensor_access_factory(rt, trace, save):  # noqa: F811 - same contract as T02
                return access_class(rt.transport._diag_mmio, trace, save)
        if monitor_runner is None:
            monitor_runner = lambda diag: t02._monitor_runner(diag, source_root=root)  # noqa: E731
        if signal_assessor is None:
            signal_assessor = lambda snap, **kw: t02._signal_assessor(snap, source_root=root, **kw)  # noqa: E731
        factory = runtime_factory or t02._runtime_factory(root)

        calibration = ScanCalibration.load(args.camera, args.plane)
        state["state"] = "attaching"
        publish()
        runtime = factory(args.manifest, stage="sequence", attach=True, loaded_overlay_receipt=args.receipt,
                          output=runtime_output, settle_roi=tuple(args.settle_roi), stripe_roi=STRIPE_ROI,
                          threshold=THRESHOLD, installation=args.installation,
                          no_display=bool(args.no_display) or realtime,
                          motion_confirmation=args.motion_confirmation, driver_proof=args.driver_proof,
                          settle_protocol="extended_6s_v2",
                          config=MotionConfig(positions=plan.batch_positions,
                                              pulses_per_position=plan.pulses_per_position,
                                              rpm=5.0, host_poll_max_s=0.010))
        if not isinstance(getattr(runtime, "motion_confirmation", None), Mapping):
            raise RuntimeError("attached runtime did not validate a fresh motion confirmation")
        if not isinstance(getattr(runtime, "driver_proof", None), Mapping):
            raise RuntimeError("attached runtime did not validate a fresh read-only driver proof")
        state["guard_before"] = t02._guard_idle(runtime)
        state["capture_configuration_readback"] = t02._validate_capture_config(runtime, tuple(args.settle_roi))
        if getattr(runtime.session.settle_config, "protocol_id", None) != "extended_6s_v2":
            raise RuntimeError("scan requires the extended_6s_v2 settle protocol")

        trace: list[dict[str, Any]] = []
        access = sensor_access_factory(runtime, trace, lambda: t02._atomic_json(output / "sensor_trace.json", trace))
        sensor = t02._read_verified_sensor(helper, access)
        state["sensor_before"] = {f"0x{k:02x}": v for k, v in sensor.items()}
        state["monitor"] = t02._fresh_monitor(runtime, monitor_runner=monitor_runner,
                                              signal_assessor=signal_assessor, sleep=sleep)

        def sensor_unchanged(label: str) -> None:
            if t02._read_verified_sensor(helper, access) != sensor:
                raise RuntimeError(f"sensor settings drifted ({label})")

        sensor_unchanged("after monitor")
        baseline_frames, baseline_records, previous = [], [], None
        for index in range(20):
            started = monotonic()
            frame = runtime.session.run_static(raw=True)
            completed = monotonic()
            identity = t02._p2_identity(runtime.controller, root)
            if previous is not None and (identity["camera_epoch"] != previous["camera_epoch"]
                                         or identity["camera_request_id"] != previous["camera_request_id"] + 1):
                raise RuntimeError("P2 request/epoch not continuous during the pre-ARM baseline")
            previous = identity
            baseline_records.append(t02._persist_baseline_frame(
                output, index, frame, started, completed,
                {"camera_request_id": identity["camera_request_id"], "camera_epoch": identity["camera_epoch"]}))
            baseline_frames.append(frame)
        last_capture = {"request_id": previous["camera_request_id"], "epoch": previous["camera_epoch"]}
        sensor_unchanged("after baseline")
        turntable = _turntable(args, calibration, bytes(baseline_frames[0].pixels))
        state["turntable"] = turntable.to_json()

        if settle_sampler_factory is None:
            from structured_light.motion.pynq_runtime import MeasuredSettleSampler
            settle_sampler_factory = MeasuredSettleSampler
        sampler_records = [{k: item[k] for k in ("capture_started_monotonic_s", "capture_completed_monotonic_s",
                                                 "camera_request_id", "camera_epoch")} for item in baseline_records]

        def make_sampler(batch: int) -> Any:
            sampler = settle_sampler_factory(
                runtime.controller, baseline_frames=baseline_frames, baseline_capture_records=sampler_records,
                output=runtime_output / "settle_samples" / f"batch_{batch:03d}", roi=tuple(args.settle_roi),
                config=runtime.session.settle_config, config_hash=runtime.session.settle_config_hash,
                frame_period_s=runtime.frame_period_s, guard=runtime.guard,
                expected_pulses=plan.pulses_per_position,
                capture_timeout_s=runtime.session.config.capture_timeout_s,
                persistence_timeout_s=runtime.session.config.capture_phase_timeout_s,
                monotonic=monotonic, sleep=sleep)
            samplers.append(sampler)
            return sampler

        def after_task(batch: int) -> dict[str, Any]:
            guard = t02._post_disarm_guard_idle(runtime)
            motor = t02._motor_idle(runtime)
            sensor_unchanged(f"after batch {batch}")
            return {"batch": batch, "guard": guard, "motor": motor}

        show = None
        adapter = runtime.session.display
        if realtime:
            from structured_light.scan3d.feed import LiveScanFeed

            live_display = (display_factory or _open_live_display)(args, output)
            feed = LiveScanFeed(live_display, pulses_per_position=plan.pulses_per_position,
                                view_period_s=args.view_period_s)
            state["display_setup"] = feed.setup(
                camera=str(args.camera), plane=str(args.plane), turntable=turntable.to_json(),
                expected={"camera_sha256": calibration.camera_sha256, "plane_sha256": calibration.plane_sha256,
                          "turntable_fingerprint": turntable.fingerprint})
            feed.start()
        elif not args.no_display:
            hdmi = getattr(adapter, "display", None)
            if hdmi is None or getattr(adapter, "no_display", False):
                raise RuntimeError("HDMI output requested but the runtime has no attached display")

            def hdmi_show(canvas: bytes) -> Mapping[str, Any]:
                if not getattr(adapter, "configured", False):
                    hdmi.configure()
                    adapter.configured = True
                t02._post_disarm_guard_idle(runtime)
                return hdmi.show(canvas)
            show = hdmi_show

        controller = ScanController(runtime.session, plan=plan, output=output / "scan",
                                    process=_make_process(calibration, turntable), render=_make_render(turntable),
                                    show=show, settle_sampler_factory=make_sampler, after_task_check=after_task,
                                    stop_event=stop_event, save_canvas_png=not args.no_canvas_png,
                                    turntable_state=turntable.state,
                                    ply_comments=_ply_comments(turntable, calibration, "live"),
                                    live_feed=feed, monotonic=monotonic)
        controller.add_static(bytes(baseline_frames[0].pixels), request_id=baseline_records[0]["camera_request_id"])
        sensor_unchanged("before arm")
        state["guard_before_arm"] = t02._guard_idle(runtime)
        for remaining in range(5, 0, -1):
            state.update(state="pre_motion_countdown", countdown_s=remaining)
            publish()
            print(f"SCAN3D: turntable motion starts in {remaining} s", flush=True)
            sleep(1.0)
        state.update(state="scanning", motion_attempted=True)
        publish()
        scan_state = controller.run()
        if feed is not None:
            feed.set_stage("SCAN COMPLETE - MOTOR DISABLED - VIEW ONLY")
            until = monotonic() + max(0.0, float(args.hold_s))
            while monotonic() < until and not (stop_event is not None and stop_event.is_set()):
                sleep(min(0.5, max(0.0, until - monotonic())))
        elif scan_state.get("state") == "complete" and args.spin_frames:
            controller.spin(args.spin_frames)
        state["scan"] = controller.state
        last = controller.sections[-1] if controller.sections else None
        if last is not None and last.request_id:
            last_capture = {"request_id": last.request_id, "epoch": previous["camera_epoch"]}
        state["state"] = "closing" if scan_state.get("state") == "complete" else scan_state.get("state")
    except BaseException as exc:
        state.update(state="failed", passed=False, error_type=type(exc).__name__, error=str(exc))
    finally:
        if feed is not None:
            state["display"] = feed.stop(drain=True, timeout_s=20.0)
            state["display_ok"] = feed.error is None
        if live_display is not None:
            try:
                live_display.close()
                state["display_closed"] = True
            except BaseException as display_error:
                state["display_closed"] = False
                state["display_close_error"] = f"{type(display_error).__name__}: {display_error}"
        if runtime is not None:
            receipt = getattr(getattr(runtime, "session", None), "last_task_receipt", None)
            if isinstance(receipt, Mapping):
                state["last_motion_task_receipt"] = dict(receipt)
            try:
                cleanup = runtime.close()
                state["cleanup"] = cleanup
                # close() joins capture workers first. Read their last durable
                # identity afterwards, including a rejected late settle pair.
                if isinstance(cleanup, Mapping) and cleanup.get("closed") is True:
                    last_capture = latest_persisted_capture_identity(last_capture, samplers)
                    state["cleanup_capture_identity"] = last_capture
                if (isinstance(cleanup, Mapping) and cleanup.get("closed") is True and helper is not None
                        and last_capture is not None):
                    state["cleanup"] = helper._validate_cleanup(cleanup, runtime_output=runtime_output,
                                                                last_capture=last_capture)
                    close_valid = True
            except BaseException as close_error:
                state["close_error"] = f"{type(close_error).__name__}: {close_error}"
        if state.get("state") == "closing" and close_valid:
            state.update(state="complete", passed=True)
        elif state.get("state") == "closing":
            state.update(state="failed", passed=False, error="runtime cleanup was not confirmed")
        state["finished_unix_ns"] = time.time_ns()
        publish()
    return state


# -- ARM timing benchmark (no hardware) ------------------------------------------------------------
def run_bench(args: Any) -> dict[str, Any]:
    """Time LiveScene on saved frames exactly as the display owner would run it."""

    _paths(args.geometry_root)
    import numpy as np
    from structured_light.scan3d import ScanCalibration
    from structured_light.scan3d.live import LiveScene
    from structured_light.scan3d.hdmi import pack_hdmi_lanes
    from structured_light.scan3d.replay import load_rotating, load_tenpoint

    camera_path, plane_path = (args.camera, args.plane) if args.camera else _default_calibration(args.geometry_root)
    calibration = ScanCalibration.load(camera_path, plane_path)
    if args.source == "tenpoint":
        static, frames = load_tenpoint(args.case)
        frames = [static] + frames
    else:
        frames = load_rotating(args.case, steps_per_rev=args.steps_per_rev)
    turntable = _turntable(args, calibration, frames[0].raw)
    scene = LiveScene(calibration, turntable)
    store = np.zeros((720, 1280, 4), np.uint8)
    rows = []
    for index, frame in enumerate(frames):
        update = scene.add_capture(frame.raw, steps=frame.steps, position_index=index, request_id=frame.request_id)
        canvas, timing = scene.render(yaw_deg=6.0 * index)
        started = time.perf_counter()
        pack_hdmi_lanes(canvas, store)
        rows.append({"capture_update_s": update["process_s"] + update["mesh_s"], "render_s": timing["render_s"],
                     "pack_s": time.perf_counter() - started, "quads": timing["quads"]})
    view = []
    for k in range(args.views):
        canvas, timing = scene.render(yaw_deg=6.0 * k)
        view.append(timing["render_s"])

    def stats(values: list[float]) -> dict[str, float]:
        return {"median_ms": float(np.median(values) * 1e3), "max_ms": float(np.max(values) * 1e3)}

    result = {"frames": len(frames), "points": len(scene.cloud), "quads": int(rows[-1]["quads"]),
              "capture_update": stats([r["capture_update_s"] for r in rows]),
              "render_growing": stats([r["render_s"] for r in rows]),
              "render_full_scene": stats(view), "pack_into_store": stats([r["pack_s"] for r in rows]),
              "python": sys.version.split()[0], "numpy": np.__version__}
    import cv2
    result["opencv"] = cv2.__version__
    result["capture_to_screen_ms_estimate"] = (result["capture_update"]["median_ms"]
                                               + result["render_full_scene"]["median_ms"]
                                               + result["pack_into_store"]["median_ms"])
    output = Path(args.output)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    return result


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--geometry-root", type=Path, default=DEFAULT_GEOMETRY_ROOT)
        p.add_argument("--camera", type=Path)
        p.add_argument("--plane", type=Path)
        p.add_argument("--output", type=Path, required=True)

    def axis(p: argparse.ArgumentParser) -> None:
        p.add_argument("--turntable", type=Path, help="fitted turntable model JSON")
        p.add_argument("--allow-assumed-axis", action="store_true")
        p.add_argument("--assumed-axis-point", type=float, nargs=3)
        p.add_argument("--assumed-axis-direction", type=float, nargs=3)
        p.add_argument("--assumed-axis-offset-mm", type=float, default=20.0)
        p.add_argument("--steps-per-rev", type=int, default=3200)
        p.add_argument("--spin-frames", type=int, default=0)

    replay = sub.add_parser("replay")
    common(replay)
    axis(replay)
    replay.add_argument("--source", choices=("tenpoint", "rotating"), required=True)
    replay.add_argument("--case", type=Path, required=True)
    replay.add_argument("--batch-positions", type=int, default=5)
    replay.add_argument("--canvas-every", type=int, default=8)

    bench = sub.add_parser("bench", help="time the live display pipeline on saved frames (no hardware)")
    common(bench)
    axis(bench)
    bench.add_argument("--source", choices=("tenpoint", "rotating"), required=True)
    bench.add_argument("--case", type=Path, required=True)
    bench.add_argument("--views", type=int, default=20)

    calibrate = sub.add_parser("calibrate-turntable")
    common(calibrate)
    calibrate.add_argument("--scan-dir", type=Path, required=True)
    calibrate.add_argument("--inner-corners", type=int, nargs=2, default=(11, 8))
    calibrate.add_argument("--square-mm", type=float, default=6.0)

    live = sub.add_parser("live")
    common(live)
    axis(live)
    live.add_argument("--source-root", type=Path, default=ALGORITHM_ROOT)
    for name in ("manifest", "receipt", "capture-helper", "exposure-module"):
        live.add_argument("--" + name, type=Path, required=True)
    live.add_argument("--capture-helper-sha256", required=True)
    live.add_argument("--module-sha256", required=True)
    live.add_argument("--motion-confirmation", type=Path)
    live.add_argument("--driver-proof", type=Path)
    live.add_argument("--settle-roi", type=int, nargs=4, required=True)
    live.add_argument("--installation", required=True)
    live.add_argument("--positions", type=int, default=40)
    live.add_argument("--pulses-per-position", type=int, default=80)
    live.add_argument("--batch-positions", type=int, default=10)
    live.add_argument("--no-display", action="store_true")
    live.add_argument("--display-mode", choices=("realtime", "after-batch"), default="realtime",
                      help="realtime: redraw after every capture in a separate HDMI owner process")
    live.add_argument("--view-period-s", type=float, default=1.0, help="idle redraw period (view turns)")
    live.add_argument("--hold-s", type=float, default=20.0, help="keep showing the finished model")
    live.add_argument("--no-canvas-png", action="store_true")
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "replay":
        state = run_replay(args)
        print(json.dumps({"state": state.get("state"), "cloud": state.get("cloud"),
                          "closure_check": state.get("closure_check")}, indent=2))
        return 0 if state.get("state") == "complete" else 1
    if args.command == "bench":
        print(json.dumps(run_bench(args), indent=2))
        return 0
    if args.command == "calibrate-turntable":
        print(json.dumps(run_calibrate(args), indent=2, default=str))
        return 0
    if args.camera is None or args.plane is None:
        raise SystemExit("live scan requires explicit --camera and --plane calibration files")
    stop = threading.Event()
    try:
        import signal
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except (ValueError, AttributeError):
        pass
    state = run_live(args, stop_event=stop)
    return 0 if state.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
