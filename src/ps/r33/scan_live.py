"""Finite R42 step/settle scan. No hardware is opened without run-bound authority.

MotorController.move_pulses is blocking: it runs on one owner thread. The guard
has a separate, bounded heartbeat thread; the camera/display main loop never
waits for a move and can request STOP at every frame.
"""
import json
import math
from pathlib import Path
import threading
import time

import numpy as np

# 100 MHz motor clock: 5 RPM (267 pulses/s at 3200 pulses/rev), 100 ms enable/direction setup.
# The turntable angle factor (steps per turn) was measured under exactly these settings.
STEP_PERIOD = 375_000
SETUP_CYCLES = 10_000_000

def validate_authority(manifest, authority, *, run_id, machine_id, boot_id):
    declaration = manifest.get('motor_controller') or {}
    if declaration.get('present') is not True or declaration.get('base_address') != 0x43c40000:
        raise ValueError('manifest does not declare the exact motor_controller identity')
    motor = manifest.get('motor_scan') or {}
    if (motor.get('present') is not True or motor.get('pulses_per_revolution') != 3200
            or motor.get('guard_base') != 0x43c50000 or motor.get('motor_base') != 0x43c40000):
        raise ValueError('manifest does not declare exact R42 motor/guard contract')
    expected = dict(machine_id=machine_id, boot_id=boot_id, run_id=run_id)
    if any(authority.get(key) != value for key, value in expected.items()):
        raise ValueError('motor authority is not bound to this machine, boot and run')
    if (authority.get('allow_motor') is not True or authority.get('motor_12v_connected') is not True
            or not str(authority.get('user_statement', '')).strip()):
        raise ValueError('explicit current-run motor/12V user declaration is required')
    return expected


class AsyncMotor:
    def __init__(self, motor, guard):
        self.motor, self.guard = motor, guard
        self.thread = None
        self.result = None
        self.error = None
        self.guard_halt = threading.Event()
        self.request_id = 0
        self.guard_thread = None
        self.closed = False

    def begin(self, pulses):
        if self.closed or (self.thread is not None and self.thread.is_alive()):
            raise RuntimeError('motion owner is unavailable')
        m, g = self.motor.validate_identity(), self.guard.validate_identity()
        if (m['busy'] or m['fault'] or m['error'] or not m['enable_n']
                or g['active'] or g['fault'] or g['error'] or g['step_pin'] or not g['enable_pin']):
            raise RuntimeError('motion preflight requires idle, fault-free, disabled motor and guard')
        self.request_id = max(int(m['accepted_id']), int(g['accepted_id']), self.request_id) + 1
        if self.request_id > 0xffffffff:
            raise RuntimeError('motor request identity exhausted; no wrap/retry')
        self.result, self.error = None, None
        self.guard_halt.clear()
        self.guard.arm(request_id=self.request_id, lease_cycles=100_000_000, timeout_ms=300_000)

        def heartbeat():
            try:
                while not self.guard_halt.wait(.02):
                    self.guard.refresh()
            except BaseException as error:
                self.error = error
                self.motor.stop()

        def move():
            try:
                self.result = self.motor.move_pulses(count=pulses, period_cycles=STEP_PERIOD,
                    high_cycles=STEP_PERIOD // 2, dir_setup_cycles=SETUP_CYCLES, enable_setup_cycles=SETUP_CYCLES,
                    lease_cycles=100_000_000, timeout_cycles=1_800_000_000, direction=0,
                    request_id=self.request_id, timeout_s=18.)
            except BaseException as error:
                self.error = error
            finally:
                self.guard_halt.set()
                if self.guard_thread is not None:
                    self.guard_thread.join(timeout=.5)
                try:
                    self.guard.revoke()
                except BaseException as error:
                    self.error = self.error or error
        self.guard_thread = threading.Thread(target=heartbeat, name='R42_guard', daemon=True)
        self.thread = threading.Thread(target=move, name='R42_motor', daemon=True)
        self.guard_thread.start()
        self.thread.start()

    def poll(self):
        if self.error is not None:
            raise RuntimeError('motion failed: %s' % self.error)
        if self.thread is None or self.thread.is_alive():
            return None
        if self.result is None or self.result.get('error', 0) or self.result.get('cancelled', False):
            raise RuntimeError('motor completed without a successful finite result')
        return self.result

    def stop(self):
        errors = []
        for owner in (self.guard, self.motor):
            try:
                owner.stop()
            except BaseException as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError('; '.join(errors))

    def close(self):
        errors = []
        if self.closed:
            return
        try:
            self.stop()
        except BaseException as error:
            errors.append(str(error))
        self.guard_halt.set()
        for thread in (self.thread, self.guard_thread):
            if thread is not None:
                thread.join(timeout=2.)
                if thread.is_alive():
                    errors.append(thread.name + ' did not stop within 2 seconds')
        for name, owner in (('motor', self.motor), ('guard', self.guard)):
            try:
                result = owner.close()
                status = result.get('status') or {}
                disabled = (not status.get('busy', True) and not status.get('step', True)
                            and status.get('enable_n') is True) if name == 'motor' else (
                            not status.get('active', True) and not status.get('step_pin', True)
                            and status.get('enable_pin') is True)
                if result.get('closed') is not True or not disabled:
                    errors.append(name + ' close did not confirm disabled output')
            except BaseException as error:
                errors.append(str(error))
        self.closed = True
        if errors:
            raise RuntimeError('; '.join(errors))


def hardware_motor(overlay, manifest, authority_path, run_id):
    """Called only after --allow-motor; all declarations checked before motor MMIO."""
    authority = json.loads(Path(authority_path).read_text(encoding='utf-8'))
    machine = Path('/etc/machine-id').read_text().strip()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    validate_authority(manifest, authority, run_id=run_id, machine_id=machine, boot_id=boot)
    from structured_light.motion.guard import GuardController
    from structured_light.motion.motor_control import MotorController
    from structured_light.motion.pynq_runtime import MMIO32, _overlay_mmio
    def transport(name, address):
        return MMIO32(_overlay_mmio(overlay, name, address, 0x10000), base_address=address, name=name)
    return AsyncMotor(MotorController(transport('motor_controller', 0x43c40000)),
                      GuardController(transport('bsg1_guard', 0x43c50000), carrier_mode='direct_wire', manifest=manifest))


class StepScan:
    """One bounded revolution per process; no automatic retry or implicit restart."""
    def __init__(self, backend=None, *, views=200, settle_frames=2, settle_s=.1, deadline_s=600., invalid_retries=3,
                 monotonic=time.monotonic, steps_per_turn=3200, axis_calibrated=False):
        if (views not in (200, 320) or 3200 % views or settle_frames < 2 or not math.isfinite(settle_s)
                or settle_s < 0 or not math.isfinite(deadline_s) or not 1 <= deadline_s <= 1800
                or type(invalid_retries) is not int or not 1 <= invalid_retries <= 10):
            raise ValueError('scan requires 200/320 views, >=2 settle frames and finite nonnegative settle time')
        self.backend, self.views, self.settle_frames, self.settle_s = backend, views, settle_frames, settle_s
        # pulses per view cover one measured turntable revolution (3200 when the drive is nominal)
        if not 1000 <= int(steps_per_turn) <= 8000:
            raise ValueError('steps per turntable turn outside 1000..8000')
        self.steps_per_turn = int(steps_per_turn)
        self.axis_note = 'axis calibrated' if axis_calibrated else 'axis uncalibrated'
        self.axis_status = 'CALIBRATED' if axis_calibrated else 'PROVISIONAL_NOT_CALIBRATED'
        self.step = -(-self.steps_per_turn // views)
        self.clock, self.state, self.phase = monotonic, 'IDLE', 'idle'
        self.steps, self.accepted, self.request_id, self.epoch = 0, 0, 0, 0
        self.visited, self.skipped, self.invalid_attempts = 0, 0, 0
        self.preview = 0
        self.invalid_retries, self.last_view = invalid_retries, None
        self.stable, self.ready_at, self.message = 0, 0., 'BTN0 disabled: current-run motor authority required'
        self.deadline_s, self.started_at = float(deadline_s), None
        self.closed = False

    def button(self, presses):
        if not presses:
            return
        if self.state == 'SCANNING':
            self.stop('BTN0 STOP')
        elif self.state == 'IDLE' and self.backend is not None and presses == 1:
            self.state, self.phase, self.message = 'SCANNING', 'move', self.axis_note
            self.started_at = self.clock()
            try:
                self.backend.begin(self.step)
            except BaseException:
                self.fail('scan start failed')
                raise
        elif self.backend is None:
            self.message = 'BTN0 refused: --allow-motor and current-run 12V authorization required'
        elif self.state == 'IDLE':
            self.message = 'BTN0 burst refused; press once to start a bounded scan'
        else:
            self.message = 'finite scan finished; start a new authorized run to scan again'

    def frame(self, *, valid, preview_valid=False):
        """Called before a new capture is requested, then after every captured frame.

        A frame whose exposure may overlap motion can only advance the settling
        counter. The minimum two later frames and time gate precede acceptance.
        `valid`: measurement-grade points. `preview_valid`: a preview-usable frame with
        enough preview points; it is taken only on the last retry, as a 'preview' view.
        """
        if self.state != 'SCANNING':
            return False
        try:
            if self.clock() - self.started_at >= self.deadline_s:
                raise TimeoutError('absolute scan deadline reached; incomplete revolution')
            if self.phase == 'move':
                result = self.backend.poll()
                if result is None:
                    return False
                if int(result.get('emitted', -1)) != self.step:
                    raise RuntimeError('emitted pulse count differs from finite request')
                self.steps += int(result['emitted'])
                self.request_id = int(result['accepted_id'])
                self.epoch = int(result.get('epoch', 0))
                self.phase, self.stable, self.ready_at = 'settle', 0, self.clock()
                self.invalid_attempts = 0
                return False
            if self.phase != 'settle':
                return False
            self.stable += 1
            if self.stable <= self.settle_frames or self.clock() - self.ready_at < self.settle_s:
                return False
            if not valid:
                self.invalid_attempts += 1
                if self.invalid_attempts < self.invalid_retries:
                    self.message = 'view %d: no measurement points; retry %d/%d' % (
                        self.visited + 1, self.invalid_attempts, self.invalid_retries)
                    return False
                if preview_valid:
                    self.preview += 1
                    self.visited += 1
                    self.phase = 'accepted'
                    self.last_view = dict(index=self.visited, outcome='preview',
                                          reason='preview_points_only_after_bounded_retries',
                                          invalid_attempts=self.invalid_attempts, **self.identity())
                    self.message = 'view %d: preview points only' % self.visited
                    return True
                self.skipped += 1
                self.visited += 1
                self.phase = 'skipped'
                self.last_view = dict(index=self.visited, outcome='skipped',
                                      reason='no_measurement_points_after_bounded_retries',
                                      invalid_attempts=self.invalid_attempts, **self.identity())
                self.message = 'view %d skipped: no measurement points after %d frames' % (
                    self.visited, self.invalid_attempts)
                return False
            self.accepted += 1
            self.visited += 1
            self.phase = 'accepted'
            self.last_view = dict(index=self.visited, outcome='measured', reason=None,
                                  invalid_attempts=self.invalid_attempts, **self.identity())
            self.message = 'view %d measured; %s' % (self.visited, self.axis_note)
            return True
        except BaseException:
            self.fail('motor result/settle failure')
            raise

    def advance(self):
        if self.phase not in ('accepted', 'skipped') or self.state != 'SCANNING':
            return
        if self.visited == self.views:
            self.state, self.phase = 'DONE', 'complete'
            self.message = 'revolution complete: %d measured, %d preview, %d skipped; %s' % (
                self.accepted, self.preview, self.skipped, self.axis_note)
            self.close()
        else:
            self.phase = 'move'
            try:
                self.backend.begin(self.step)
            except BaseException:
                self.fail('next move failed')
                raise

    def stop(self, reason='cancelled'):
        self.state, self.phase, self.message = 'STOPPED', 'stopped', reason
        self.close()

    def fail(self, reason):
        self.state, self.phase, self.message = 'FAILED', 'failed', reason
        self.close()

    def close(self):
        if self.backend is not None and not self.closed:
            self.closed = True
            try:
                self.backend.stop()
            finally:
                self.backend.close()

    def identity(self):
        return dict(motor_id=self.request_id, motor_epoch=self.epoch, motor_steps=self.steps)

    def status(self):
        return dict(state=self.state, phase=self.phase, views=self.accepted, target_views=self.views,
                    visited_views=self.visited, measured_views=self.accepted, preview_views=self.preview,
                    skipped_views=self.skipped,
                    coverage_complete=self.state == 'DONE' and self.visited == self.views and self.skipped == 0,
                    invalid_attempts=self.invalid_attempts, invalid_retries=self.invalid_retries,
                    last_view=self.last_view,
                    motion_complete=self.state == 'DONE' and self.steps == self.step * self.views and self.visited == self.views,
                    data_complete=self.state == 'DONE' and self.accepted == self.views and self.skipped == 0,
                    steps=self.steps, angle_deg=360. * self.steps / self.steps_per_turn,
                    deadline_s=self.deadline_s,
                    axis_status=self.axis_status, message=self.message, **self.identity())


def scan_panel(scan, point_blocks, *, max_points=16000, render_cards=True):
    """Main-thread Pillow only; caller caches at 0.33s/1s like the profile panel."""
    from PIL import Image, ImageDraw
    from r33.profile_panel import PLOT_W, PLOT_H, CARD_H
    im = Image.new('RGB', (PLOT_W, PLOT_H), '#101822')
    draw = ImageDraw.Draw(im)
    draw.text((12, 12), 'OBJECT CLOUD | AXIS UNCALIBRATED', fill='#ffd060')
    if point_blocks:
        p = np.concatenate(point_blocks)
        p = p[::max(1, int(math.ceil(len(p) / max_points))), :3]
        p = p[np.isfinite(p).all(axis=1)]
        if len(p):
            # Isometric view; output coordinates are display-only.
            uv = np.column_stack((p[:, 0] - .6 * p[:, 2], -p[:, 1] + .3 * p[:, 2]))
            lo, hi = uv.min(axis=0), uv.max(axis=0)
            scale = min((PLOT_W - 32) / max(hi[0] - lo[0], .01), (PLOT_H - 70) / max(hi[1] - lo[1], .01))
            uv = (uv - (lo + hi) / 2) * scale + [PLOT_W / 2, PLOT_H / 2 + 20]
            for x, y in np.rint(uv).astype(int):
                draw.point((int(x), int(y)), fill='#60ff90')
    if not render_cards:
        return np.asarray(im), None
    cards = Image.new('RGB', (1280, CARD_H), '#101822')
    d = ImageDraw.Draw(cards)
    status = scan.status()
    d.text((24, 12), 'SCAN %s | visited %d/%d | measured %d skipped %d | %.2f deg | %d pulses' %
           (status['state'], status['visited_views'], status['target_views'], status['measured_views'],
            status['skipped_views'], status['angle_deg'], status['steps']), fill='white')
    d.text((24, 40), status['message'], fill='#ffd060')
    d.text((24, 67), 'STEP-AND-SETTLE PREVIEW; no calibrated dimensions. BTN0 = start / stop', fill='#80c8ff')
    return np.asarray(im), np.asarray(cards)
