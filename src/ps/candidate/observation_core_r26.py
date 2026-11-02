"""Shared geometry dispatch for replay and formally admitted camera captures.

No acquisition, device control, writer or physical-validity flags are created.
The original processors own timing, exposure, admission, policy and cloud state.
"""
from dataclasses import dataclass
import threading
import time

from .pfo3_geometry_pair_r25 import PreparedGeometryPairR25
from .pfo3_r25 import PFO3Error


@dataclass(frozen=True)
class ObservationPairResult:
    measurement: object
    preview: object
    timings: dict


class ObservationProcessingCore:
    """Serial dispatch with one shared camera mapping and independent mode state.

    If either processor fails, this core cannot process another frame: a first
    mode may already have advanced its state. No mixed result is published.
    Construct a new explicitly admitted session rather than retrying implicitly.
    """
    def __init__(self, measurement_processor, preview_processor=None):
        if measurement_processor is preview_processor:
            raise PFO3Error('measurement and preview require distinct processors')
        if preview_processor is not None:
            if bool(measurement_processor.replay) != bool(preview_processor.replay):
                raise PFO3Error('camera and replay geometry domains cannot be mixed')
            if not all(callable(getattr(p, 'process_prepared_final', None))
                       for p in (measurement_processor, preview_processor)):
                raise PFO3Error('shared geometry hook missing')
        self.measurement_processor = measurement_processor
        self.preview_processor = preview_processor
        self._lock = threading.Lock()
        self._failed = False

    @staticmethod
    def _call(callback, *args, **kwargs):
        wall, cpu = time.perf_counter(), time.thread_time()
        value = callback(*args, **kwargs)
        return value, dict(wall_s=time.perf_counter()-wall,
                           thread_cpu_s=time.thread_time()-cpu)

    def process(self, capture, metadata=None, *, frame, admission, pair=None):
        with self._lock:
            if self._failed:
                raise PFO3Error('shared observation core failed; explicit new session required')
            try:
                if frame.raw_required:
                    raise PFO3Error('whole-frame exceptional handling required before shared geometry')
                measurement = self.measurement_processor
                preview = self.preview_processor
                timings = {}
                if preview is None:
                    if pair is not None:
                        raise PFO3Error('single mode cannot consume a shared pair')
                    args = (capture,) if metadata is None else (capture, metadata)
                    result, timings['measurement'] = self._call(
                        measurement.process_final, *args,
                        payload=frame.payload, admission=admission)
                    return ObservationPairResult(result, None, timings)
                # Enforce original retained-frame provenance before deriving a
                # cache. Each original processor validates admission again.
                admission.validate()
                if admission.fixture_only or admission._validated_frame is not frame:
                    raise PFO3Error('shared geometry requires exact actual retained PL frame')
                if pair is None:
                    pair, timings['paired_camera_geometry'] = self._call(
                        PreparedGeometryPairR25, frame, measurement.calibration)
                pair.assert_binding(frame, measurement.calibration)
                pair.assert_binding(frame, preview.calibration)
                measured, timings['measurement'] = self._call(
                    measurement.process_prepared_final, capture, metadata, pair=pair,
                    payload=frame.payload, admission=admission, mode='measurement')
                previewed, timings['preview'] = self._call(
                    preview.process_prepared_final, capture, metadata, pair=pair,
                    payload=frame.payload, admission=admission, mode='preview')
                return ObservationPairResult(measured, previewed, timings)
            except BaseException:
                self._failed = True
                raise
