"""Post-run metrics for actual new observations, without hardware dependencies.

Sliding intervals are half-open [s, s+width), with start <= s <= end-width.
Finite Python floats mean their exact binary rational values, not decimal
rounding or a guessed epsilon. Counts can change at event times and event-width
times. Testing those boundaries and one interior point between them is exact.
These post-run functions do not estimate missing ACKs or physical exposure age.
"""
from bisect import bisect_left
from fractions import Fraction
import math
import numbers


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(name + ' must be finite real seconds')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(name + ' must be finite real seconds')
    return result


def sliding_windows(events, start, end, width=5):
    """Summarize *all* complete sliding windows, including open-side changes.

    ``worst_window`` is a genuine witness, rather than an epsilon-shifted
    boundary. Its exact relative start fraction is included so float formatting
    cannot make a midpoint look like the adjacent event. No performance PASS is
    inferred; a run shorter than width is explicitly NOT_EVALUATED.
    """
    start = _finite(start, 'start'); end = _finite(end, 'end')
    width = _finite(width, 'width')
    if width <= 0 or end < start:
        raise ValueError('positive width and end >= start required')
    first, finish, span = map(Fraction, (start, end, width))
    times = sorted(Fraction(_finite(t, 'event')) for t in events)
    in_run = [t for t in times if first <= t < finish]
    result = dict(schema='R33_EXACT_SLIDING_WINDOWS_V1', width_s=width,
                  activity_s=end-start, event_count=len(in_run),
                  interval='[s,s+width)', epsilon_s=0,
                  arithmetic='exact rational values of supplied binary floats')
    last = finish-span
    if last < first:
        result.update(status='NOT_EVALUATED', evaluated=False,
                      reason='run shorter than complete window',
                      worst_count=None, worst_hz=None, best_count=None,
                      candidate_windows=0, worst_window=None)
        return result
    boundaries = {first, last}
    for t in in_run:
        for b in (t, t-span):
            if first <= b <= last:
                boundaries.add(b)
    boundaries = sorted(boundaries)
    # Counts are constant on every open interval between adjacent breakpoints.
    starts = boundaries + [(a+b)/2 for a, b in zip(boundaries, boundaries[1:])]
    worst = best = None; witness = None
    for s in starts:
        count = bisect_left(in_run, s+span)-bisect_left(in_run, s)
        if worst is None or count < worst:
            worst = count; witness = s
        best = count if best is None else max(best, count)
    relative = witness-first
    result.update(status='EVALUATED', evaluated=True, worst_count=worst,
                  worst_hz=worst/width, best_count=best,
                  candidate_windows=len(starts),
                  worst_window=dict(start_s=float(relative),
                                    end_s=float(relative+span),
                                    start_exact_fraction=str(relative),
                                    end_exact_fraction=str(relative+span)))
    return result


def _new_model_rows(records, valid_ack):
    if not callable(valid_ack):
        raise TypeError('actual VDMA ACK validator required')
    selected = []; seen = set(); points = 0; last_time = None
    for row in records:
        if not isinstance(row, dict) or not valid_ack(row.get('receipt')):
            continue
        timestamp = row.get('ack_perf_s')
        if timestamp is None:
            continue
        timestamp = _finite(timestamp, 'ACK time')
        if timestamp <= 0:
            continue
        identity = (row.get('target'), row.get('frame_id'))
        count = row.get('cloud_points', 0)
        if isinstance(count, bool) or not isinstance(count, numbers.Integral) or count < 0:
            raise ValueError('cloud_points must be a nonnegative integer')
        # Match replay.new_model_acks: a validated identity already seen with
        # no cloud growth cannot later become a second new-model event.
        if identity not in seen and count > points:
            if last_time is not None and timestamp < last_time:
                raise ValueError('new-model ACK clock moved backwards')
            selected.append(row); points = int(count); last_time = timestamp
        seen.add(identity)
    return selected


def new_model_acks(records, valid_ack):
    """Validated, unique, strictly growing model events for one target run."""
    return [float(row['ack_perf_s']) for row in _new_model_rows(records, valid_ack)]


def _percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = fraction*(len(values)-1)
    lower = math.floor(position); upper = math.ceil(position)
    return values[lower]+(values[upper]-values[lower])*(position-lower)


def display_metrics(records, valid_ack):
    """Age and cadence of the SAME valid new-model ACK population.

    display_age means source-ready -> actual ACK, using supplied system-wide
    monotonic integer nanoseconds. Missing age metadata does not remove the ACK
    from cadence, but makes age_population_complete false. Negative ages are
    invalid evidence and raise. This is replay latency, not camera exposure age.
    """
    rows = _new_model_rows(records, valid_ack)
    events = [float(row['ack_perf_s']) for row in rows]
    ages = []; missing = 0
    for row in rows:
        source = row.get('source_capture_monotonic_ns')
        ack = row.get('ack_monotonic_ns')
        if (type(source) is not int or type(ack) is not int or source <= 0 or ack <= 0):
            missing += 1
            continue
        if ack < source:
            raise ValueError('new-model ACK precedes source-ready clock')
        ages.append((ack-source)/1e9)
    span = events[-1]-events[0] if len(events) > 1 else None
    return dict(schema='R33_VALID_NEW_MODEL_DISPLAY_V1',
                population='valid VDMA, unique target/frame, strictly increasing cloud points',
                new_model_ack_count=len(rows), ack_events_perf_s=events,
                new_model_ack_rate_hz=(len(events)-1)/span if span else None,
                age_scope='source-ready to actual ACK; replay not physical exposure',
                age_samples=len(ages), age_missing_count=missing,
                age_population_complete=bool(rows) and missing == 0,
                age_status=('EVALUATED' if rows and missing == 0 else
                            'PARTIAL' if ages else 'NOT_EVALUATED'),
                display_age_p50_s=_percentile(ages, .5),
                display_age_p95_s=_percentile(ages, .95),
                display_age_p99_s=_percentile(ages, .99))
