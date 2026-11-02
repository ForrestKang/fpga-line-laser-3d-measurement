"""Hardware-free session lifecycle and input boundaries for R26 orchestration.

This module schedules no device operation. Source labels describe routing, not
proof of hardware execution or physical metrology. Frontend protocol/provenance
validation remains mandatory and is supplied by the actual input owner.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, Iterable, Iterator


STATES = ('preflight', 'ready', 'processing', 'draining', 'features',
          'completed', 'cancelled', 'failed')
_NEXT = dict(preflight='ready', ready='processing', processing='draining',
             draining='features', features='completed')
_TERMINAL = frozenset(('completed', 'cancelled', 'failed'))
_COMPLETION = ('persistence_durable', 'workers_joined', 'features_complete',
               'readback_verified')


class SessionContractError(ValueError):
    pass


class ScanSession:
    """Thread-safe bookkeeping; no output is successful before four confirmations.

    A cancelled/failed run may still retain durable partial data. Cancellation
    itself does not claim that a worker or hardware operation has stopped.
    """
    def __init__(self, *, source_kind: str, clock: Callable[[], float] = time.perf_counter):
        if source_kind not in ('offline', 'actual_pl', 'camera'):
            raise SessionContractError('unknown source kind')
        self._lock = threading.RLock()
        self._clock = clock
        self._source_kind = source_kind
        self._state = 'preflight'
        self._completion = {key: False for key in _COMPLETION}
        self._history = [dict(state='preflight', at_perf_s=clock(), reason=None)]

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def transition(self, state: str, *, reason: str | None = None) -> None:
        with self._lock:
            if state not in STATES:
                raise SessionContractError('unknown session state')
            if self._state in _TERMINAL:
                raise SessionContractError('terminal session cannot transition')
            if state in ('failed', 'cancelled'):
                if not isinstance(reason, str) or not reason.strip():
                    raise SessionContractError('failure/cancellation requires a reason')
            elif _NEXT.get(self._state) != state:
                raise SessionContractError(f'invalid transition {self._state} -> {state}')
            if state == 'completed' and not all(self._completion.values()):
                missing = [key for key, value in self._completion.items() if not value]
                raise SessionContractError('completion unconfirmed: ' + ', '.join(missing))
            self._state = state
            self._history.append(dict(state=state, at_perf_s=self._clock(), reason=reason))

    def confirm_completion(self, *, persistence_durable: bool, workers_joined: bool,
                           features_complete: bool, readback_verified: bool) -> None:
        values = dict(persistence_durable=persistence_durable, workers_joined=workers_joined,
                      features_complete=features_complete, readback_verified=readback_verified)
        if any(type(value) is not bool for value in values.values()):
            raise SessionContractError('completion confirmations must be booleans')
        with self._lock:
            if self._state != 'features':
                raise SessionContractError('completion is confirmed after draining and features')
            self._completion = values

    def finish(self) -> None:
        self.transition('completed')

    def fail(self, reason: str) -> None:
        self.transition('failed', reason=reason)

    def cancel(self, reason: str = 'user_cancelled') -> None:
        self.transition('cancelled', reason=reason)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(schema='scan_session_r26_v1', source_kind=self._source_kind,
                        state=self._state, completed=self._state == 'completed',
                        confirmations=dict(self._completion),
                        transitions=[dict(row) for row in self._history],
                        physical_accuracy_verified=False)


@dataclass(frozen=True)
class FrameInput:
    """Immutable bytes and identity; this envelope does not itself validate PFO3."""
    source_kind: str
    frame_id: int
    capture_epoch: int
    raw: bytes
    observations: bytes
    config_version: int = 0
    exposure_version: int = 0


def validate_frame_contract(frame: FrameInput, *, source_kind: str) -> None:
    if not isinstance(frame, FrameInput) or frame.source_kind != source_kind:
        raise SessionContractError('source/frame kind mismatch')
    for key, bits in (('frame_id', 32), ('capture_epoch', 16),
                      ('config_version', 32), ('exposure_version', 32)):
        value = getattr(frame, key)
        if type(value) is not int or not 0 <= value < (1 << bits):
            raise SessionContractError(f'{key} must be uint{bits}')
    if type(frame.raw) is not bytes or len(frame.raw) != 752 * 480:
        raise SessionContractError('raw must be immutable native 752x480 bytes')
    if type(frame.observations) is not bytes or not frame.observations:
        raise SessionContractError('observations must be nonempty immutable wire bytes')


class SourceAdapter:
    """One-shot input boundary; invokes its owner's strict validator per frame.

    Camera is contract-only in this release: use validate_camera_contract for
    fixtures, not iteration. Actual camera/laser testing is a later acceptance.
    No imports of PYNQ or implicit DMA/device commands occur here.
    """
    def __init__(self, kind: str, frames: Iterable[FrameInput], *,
                 validator: Callable[[FrameInput], None]):
        if kind not in ('offline', 'actual_pl', 'camera'):
            raise SessionContractError('unknown source kind')
        if not callable(validator):
            raise SessionContractError('strict frontend validator required')
        self.kind = kind
        self._frames = frames
        self._validator = validator
        self._started = False

    def validate_camera_contract(self, frame: FrameInput) -> None:
        if self.kind != 'camera':
            raise SessionContractError('camera contract requested for another source')
        validate_frame_contract(frame, source_kind='camera')
        self._validator(frame)

    def __iter__(self) -> Iterator[FrameInput]:
        if self.kind == 'camera':
            raise SessionContractError('camera adapter contract-only; live acceptance pending')
        if self._started:
            raise SessionContractError('source adapter is one-shot')
        self._started = True
        epoch = last = None
        for frame in self._frames:
            validate_frame_contract(frame, source_kind=self.kind)
            if epoch is not None and frame.capture_epoch != epoch:
                raise SessionContractError('capture epoch changed within input session')
            if last is not None and frame.frame_id <= last:
                raise SessionContractError('frame identity repeated or regressed')
            self._validator(frame)
            epoch, last = frame.capture_epoch, frame.frame_id
            yield frame
