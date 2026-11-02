"""Opt-in nested timing of the existing transport, without changing its protocol.

All instrumented hardware calls must stay on the installing thread. Counters
are inclusive (for example arm_raw includes allocate), not additive latency.
"""
from functools import wraps
import threading
import time


def instrument_transport(transport):
    if hasattr(transport, 'r14_transport_timings'):
        raise ValueError('transport instrumentation already installed')
    owner = threading.get_ident()
    totals, counts = {}, {}
    transport.r14_transport_timings = totals
    transport.r14_transport_calls = counts
    for name in ('_allocate', 'arm_raw', 'arm_profile', 'start_capture', 'poll',
                 'receive', '_wait_published', 'read_diagnostics'):
        original = getattr(transport, name)

        def wrap(method, key):
            @wraps(method)
            def timed(*args, **kwargs):
                if threading.get_ident() != owner:
                    raise RuntimeError('hardware transport left its exclusive owner thread')
                begin = time.perf_counter()
                try:
                    return method(*args, **kwargs)
                finally:
                    totals[key] = totals.get(key, 0.) + time.perf_counter() - begin
                    counts[key] = counts.get(key, 0) + 1
            return timed
        setattr(transport, name, wrap(original, name))
    return transport


def timed_capture(capture):
    """Wrap capture_features and attach per-frame inclusive transport deltas."""
    @wraps(capture)
    def run(controller, **kwargs):
        transport = controller.transport
        # GatedCaptureTransport delegates unknown attributes to its real transport.
        counters = getattr(transport, 'r14_transport_timings', {})
        before = dict(counters)
        frame = capture(controller, **kwargs)
        timings = kwargs.get('timing_s')
        if timings is not None:
            for key, value in counters.items():
                timings['transport_nested_' + key + '_s'] = value - before.get(key, 0.)
        return frame
    return run
