"""SCCB exposure transactions at an explicitly drained, camera-idle boundary.

Effect models are supplied, sensor-specific validation evidence. A configured
observation window is never evidence that the sensor applied an exposure. The
caller owns epoch admission and the truth of the three boundary confirmations.
This module issues no capture, laser, reset, or motor commands.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Callable

from ..p2.camera_exposure import manual_plan
from .continuous_exposure import ExposureController, ExposureOperation
from .continuous_timing import _uint


@dataclass(frozen=True)
class ExposureEffectModel:
    """Validated number of admitted captures to discard after the SCCB batch.

    The evidence must establish this boundary for the actual context-A linear
    sensor configuration, including the complete manual-plan write sequence.
    A hash/reference records provenance; the caller must verify its contents.
    """
    lines: int
    discard_admitted_frames: int
    validated: bool
    evidence_sha256: str | None = None
    evidence_reference: str | None = None

    def __post_init__(self) -> None:
        if type(self.lines) is not int or not 1 <= self.lines <= 32765:
            raise ValueError('exposure lines must be 1..32765')
        _uint('discard_admitted_frames', self.discard_admitted_frames)
        if type(self.validated) is not bool:
            raise ValueError('validated must be bool')
        if self.validated or self.evidence_sha256 is not None:
            if not isinstance(self.evidence_sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', self.evidence_sha256):
                raise ValueError('validated effect evidence requires SHA-256')
        if self.validated or self.evidence_reference is not None:
            if not isinstance(self.evidence_reference, str) or not self.evidence_reference.strip():
                raise ValueError('validated effect evidence reference is required')


class ExposureCoordinator:
    """Single monotonic request-id epoch; failure requires a new coordinator.

    Call ``capture_metadata`` exactly once per admitted capture, in request
    order, then ``observe`` at most once for that capture's processing result.
    Metadata and observation have separate monotonic watermarks so processing
    may lag capture. A pending proposal stops valid measurement until applied.
    Device failures return a rejected transaction and latch the controller;
    invalid boundaries/evidence raise before device access and retain pending.
    """
    laser_controlled = False

    def __init__(self, controller: ExposureController, *,
                 effect_models: dict[int, ExposureEffectModel],
                 allow_unvalidated_observation: bool = False,
                 observation_discard_frames: int = 3,
                 trace_sink: Callable[[dict], None] | None = None) -> None:
        if not isinstance(controller, ExposureController):
            raise ValueError('ExposureController required')
        if controller.pending is not None or controller.failure is not None or controller.history:
            raise ValueError('a fresh controller without pending operations, failures or acknowledgements is required')
        if type(allow_unvalidated_observation) is not bool:
            raise ValueError('allow_unvalidated_observation must be bool')
        _uint('observation_discard_frames', observation_discard_frames)
        if not isinstance(effect_models, dict):
            raise ValueError('effect_models must be a dictionary keyed by lines')
        for lines, model in effect_models.items():
            if type(lines) is not int or not isinstance(model, ExposureEffectModel) or model.lines != lines:
                raise ValueError('effect model key must match its line count')
            model.__post_init__()
        if trace_sink is not None and not callable(trace_sink):
            raise ValueError('trace_sink must be callable')
        self.controller = controller
        self.effect_models = dict(effect_models)
        self.allow_unvalidated_observation = allow_unvalidated_observation
        self.observation_discard_frames = observation_discard_frames
        self.trace_sink = trace_sink
        self.history: list[dict] = []
        self.trace: list[dict] = []
        self._last_observed: int | None = None
        self._last_capture: int | None = None
        self._last_boundary: int | None = None
        self._last_transition_capture: int | None = None
        self._remaining = 0

    @property
    def pending(self) -> ExposureOperation | None:
        return self.controller.pending

    @property
    def has_pending(self) -> bool:
        return self.pending is not None

    def _emit(self, event: dict) -> None:
        self.trace.append(deepcopy(event))
        if self.trace_sink is not None:
            self.trace_sink(deepcopy(event))

    def observe(self, quality: str, request_id: int) -> ExposureOperation | None:
        _uint('request_id', request_id)
        if self._last_observed is not None and request_id <= self._last_observed:
            raise ValueError('observation request_id must advance')
        if quality not in {'good', 'weak', 'saturated', 'incomplete', 'ambiguous', 'unvalidated'}:
            raise ValueError('unknown frame quality')
        self._last_observed = request_id
        if (self._remaining or
                (self._last_boundary is not None and request_id <= self._last_boundary) or
                (self._last_transition_capture is not None and request_id <= self._last_transition_capture)):
            return None
        return self.controller.propose(quality)

    def apply_at_boundary(self, device, last_request_id: int, *, capture_drained: bool,
                          processing_drained: bool, camera_idle: bool) -> dict:
        for name, value in (('capture_drained', capture_drained),
                            ('processing_drained', processing_drained), ('camera_idle', camera_idle)):
            if type(value) is not bool or not value:
                raise ValueError(f'{name} must explicitly be True')
        _uint('last_request_id', last_request_id)
        if self.controller.failure is not None:
            raise RuntimeError('exposure transaction previously failed; new epoch required')
        op = self.pending
        if op is None:
            raise ValueError('no pending exposure operation')
        if (any(v is not None and last_request_id < v for v in (self._last_observed, self._last_capture)) or
                (self._last_boundary is not None and last_request_id <= self._last_boundary)):
            raise ValueError('boundary request_id must cover all captures/observations and advance')
        model = self.effect_models.get(op.lines)
        if model is not None:
            model.__post_init__()
        if not self.allow_unvalidated_observation and (model is None or not model.validated):
            raise ValueError('a matching validated exposure effect model is required before SCCB writes')
        discard = (self.observation_discard_frames if self.allow_unvalidated_observation
                   else model.discard_admitted_frames)
        if last_request_id + discard + 1 > 0xFFFFFFFF:
            raise ValueError('exposure effect boundary overflow; new request epoch required')
        result = dict(revision=op.revision, lines=op.lines, gain=op.gain,
                      last_request_id=last_request_id, discard_admitted_frames=discard,
                      accepted=False, snapshot={}, writes=[], error=None,
                      laser_changed=False, reset_performed=False, motor_changed=False,
                      observation_only=self.allow_unvalidated_observation,
                      evidence_sha256=None if model is None else model.evidence_sha256,
                      evidence_reference=None if model is None else model.evidence_reference)
        failure = None
        try:
            # Read all guard registers before issuing the first write.
            for reg in (0x07, 0x0F, 0xAF, 0x35, 0x0B, 0xD5):
                result['snapshot'][reg] = device.read(reg)
            plan = manual_plan(result['snapshot'], coarse_rows=op.lines, gain_code=16)
            for reg, value in plan:
                item = dict(register=reg, value=value, attempted=False, verified=False)
                result['writes'].append(item)
                self._emit(dict(event='write_intent', revision=op.revision, **item))
                item['attempted'] = True
                device.write_verified(reg, value)
                item['verified'] = True
                self._emit(dict(event='write_verified', revision=op.revision, **item))
            result['accepted'] = True
            self._emit(dict(event='exposure_transaction', **result))
        except BaseException as exc:
            failure = exc
            result['accepted'] = False
            result['error'] = f'{type(exc).__name__}: {exc}'
            # _emit preserves local evidence even if persistence itself fails.
            try:
                self._emit(dict(event='exposure_transaction_failed', **result))
            except BaseException as trace_exc:
                result['trace_error'] = f'{type(trace_exc).__name__}: {trace_exc}'
        if result['accepted']:
            self.controller.acknowledge(op.revision, accepted=True,
                                        effective_after_request_id=last_request_id + discard)
            self._remaining = discard
            self._last_boundary = last_request_id
        else:
            self.controller.acknowledge(op.revision, accepted=False,
                                        effective_after_request_id=None, error=result['error'])
        self.history.append(deepcopy(result))
        if failure is not None and not isinstance(failure, Exception):
            raise failure
        return result

    def capture_metadata(self, request_id: int) -> dict:
        _uint('request_id', request_id)
        if self._last_capture is not None and request_id <= self._last_capture:
            raise ValueError('capture request_id must advance')
        if self._last_boundary is not None and request_id <= self._last_boundary:
            raise ValueError('capture request_id must follow the exposure boundary')
        transition = self.has_pending or self._remaining > 0
        if self._remaining:
            self._remaining -= 1
            self._last_transition_capture = request_id
        model = self.effect_models.get(self.controller.lines)
        known = (not self.allow_unvalidated_observation and not transition
                 and self.controller.failure is None and model is not None and model.validated)
        eligible = known and self.controller.frame_eligible(
            request_id=request_id, exposure_revision=self.controller.revision)
        self._last_capture = request_id
        return dict(exposure_lines=self.controller.lines, exposure_revision=self.controller.revision,
                    effect_known=bool(known), measurement_eligible=bool(eligible), transition=transition,
                    observation_only=self.allow_unvalidated_observation,
                    exposure_failure=self.controller.failure,
                    effect_evidence_sha256=model.evidence_sha256 if known else None,
                    effect_evidence_reference=model.evidence_reference if known else None)
