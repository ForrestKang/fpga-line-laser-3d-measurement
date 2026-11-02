"""Exact slice scheduling + circle-scoring helpers for observed-feature discovery.

This module owns three orthogonal, behaviour-preserving accelerations of the
frozen end-analysis used by :mod:`virtual_scan.features_v4`:

1. ``run_slice_jobs`` evaluates the 275 independent Z-slice primitive fits on a
   bounded ``fork`` process pool.  The parent inherits the observed cloud and the
   already-grouped level indices into every worker copy-on-write, so a task
   payload is only a ``(start, stop)`` pair. Results are re-assembled in original
   level order. Operational pool failures abort without restarting serially.
2. ``fit_circle_diameter_batched`` scores the same 128 deterministic RANSAC
   hypotheses as ``continuous_measurement.fit_circle_diameter`` with one
   ``(hypotheses, points)`` residual matrix.  The draw sequence, triplet-circle
   solver, inlier thresholds, tie-break order and final least-squares/coverage
   gates are unchanged; only the per-hypothesis medians that can never win the
   tie-break are skipped.
3. ``circle_triples`` memoises the immutable ``default_rng(0)`` triplet draws for
   a given point count.  ``fit_circle_diameter`` re-creates that generator on
   every call, so the 128 index rows are a pure function of ``len(points)``.

The module never changes a policy value, seed, gate or output order.  It does not
read CAD/truth data.  ``last_execution()`` exposes scheduling diagnostics for
tests/benchmarks *outside* the feature records, so ``features.json`` and the
association audit keep their exact schema.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import sys
from typing import Any, Callable, Sequence

import numpy as np

# Keep the import contract of ``features_v4`` when this helper is imported first
# by a test or a benchmark.  The two source trees are the frozen synthetic
# geometry packages shipped with this workspace.
_RUNTIME = Path(__file__).resolve().parents[1]
for _source in ("structured_light_system_algorithm_v1", "structured_light_system_geometry_v1"):
    _path = str(_RUNTIME / _source / "src" / "python")
    if _path not in sys.path:
        sys.path.insert(0, _path)
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))

from candidate.feature_fast import connected_cells  # noqa: E402
from structured_light.scan3d import continuous_measurement as _measurement  # noqa: E402

TRIPLE_HYPOTHESES = 128
# A slice is thinned to <=700 points by ``features_v4._primitive`` before this
# helper sees it.  The cap bounds the residual matrix if a future caller uses the
# wrapper directly; above it the frozen original implementation is used instead.
_MAX_BATCH_ELEMENTS = 2_000_000
_MAX_WORKERS = 2
_TRIPLE_CACHE_LIMIT = 1024
_TRIPLE_CACHE: dict[int, np.ndarray] = {}

_WORKER_STATE: dict[str, Any] | None = None
_LAST_EXECUTION: dict[str, Any] = {"mode": "not_run"}


def last_execution() -> dict[str, Any]:
    """Diagnostics of the most recent slice run (never part of extraction output)."""
    return dict(_LAST_EXECUTION)


def _note_execution(**fields: Any) -> None:
    _LAST_EXECUTION.clear()
    _LAST_EXECUTION.update(fields)


def mark_legacy(points: int) -> None:
    """Record that the verbatim frozen reference path was requested."""
    _note_execution(mode="legacy_reference", effective_workers=1,
                    reason="legacy_reference", points=int(points))


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _fork_available() -> bool:
    if os.name != "posix":
        return False
    try:
        import multiprocessing as multiprocessing

        return "fork" in multiprocessing.get_all_start_methods()
    except Exception:
        return False


def resolve_workers(requested: int | None, points: int, levels: int) -> tuple[int, str]:
    """Return ``(workers, reason)`` without touching the extraction result.

    The default is deliberately serial: existing verification/scoring callers
    must keep the frozen result path and process behaviour.  Callers that want
    the bounded pool pass ``workers=2`` or set ``R26_FEATURE_WORKERS=2``.  An
    explicit request is honoured even when ``fork`` is unavailable so the caller
    can observe the exact serial fallback.

    ``points``/``levels`` are accepted for forward-compatible diagnostics; they
    do not enable an implicit pool (cloud-size heuristics made existing tests
    fork depending on input size).
    """
    if requested is not None:
        if type(requested) is not int or requested < 0:
            raise ValueError("workers must be a non-negative integer or None")
        if requested <= 1:
            return 1, "requested_serial"
        return min(requested, _MAX_WORKERS), "requested"
    disabled = os.environ.get("R26_FEATURE_PARALLEL", "").strip().lower()
    if disabled in {"0", "false", "no", "off"}:
        return 1, "env_disabled"
    env_workers = _env_int("R26_FEATURE_WORKERS")
    if env_workers is not None:
        if env_workers <= 1:
            return 1, "env_serial"
        return min(env_workers, _MAX_WORKERS), "env"
    return 1, "default_serial"


def circle_triples(point_count: int) -> np.ndarray:
    """The exact 128 ``default_rng(0)`` triplet rows for ``point_count`` points.

    ``fit_circle_diameter`` constructs the generator afresh on each call, so this
    is a pure memoisation of immutable random state.  The returned array is
    read-only; callers only index it.
    """
    cached = _TRIPLE_CACHE.get(point_count)
    if cached is not None:
        return cached
    rng = np.random.default_rng(0)
    triples = np.empty((TRIPLE_HYPOTHESES, 3), np.int64)
    for index in range(TRIPLE_HYPOTHESES):
        triples[index] = rng.choice(point_count, 3, replace=False)
    triples.setflags(write=False)
    if len(_TRIPLE_CACHE) >= _TRIPLE_CACHE_LIMIT:
        _TRIPLE_CACHE.clear()
    _TRIPLE_CACHE[point_count] = triples
    return triples


def _circle_hypotheses_rank_filtered(points, triples):
    """Elide conservatively rank-deficient ambiguous triplets, preserving fits.

    The frozen helper computes one SVD/rank test in Python per ambiguous
    triplet. Batch those tiny SVDs, but only reject with a 16x margin below
    its rank tolerance. All borderline/nonfinite rows and every remaining
    fit go through the unchanged helper. No new circle solver is introduced.
    """
    subset = points[triples]
    origin = subset.mean(axis=1)
    shifted = subset - origin[:, None, :]
    matrix = np.concatenate((2 * shifted, np.ones((len(subset), 3, 1))), axis=2)
    rhs = np.sum(shifted ** 2, axis=2)
    determinant = np.linalg.det(matrix)
    band = 1e-9 * np.maximum(np.abs(matrix).max(axis=(1, 2)), 1.0) ** 3
    unambiguous = np.abs(determinant) > band
    out = [None] * len(triples)
    # Verbatim arithmetic/order from the frozen helper; no alternative solver.
    if unambiguous.any():
        solution = np.linalg.solve(matrix[unambiguous], rhs[unambiguous][..., None])[..., 0]
        radius2 = solution[:, 2] + np.sum(solution[:, :2] ** 2, axis=1)
        ok = np.isfinite(radius2) & (radius2 > 0)
        centers = origin[unambiguous] + solution[:, :2]
        radii = np.sqrt(np.where(ok, radius2, 1.0))
        for spot, center, radius, good in zip(np.flatnonzero(unambiguous), centers, radii, ok):
            out[int(spot)] = (center, float(radius)) if good else None
    ambiguous = np.flatnonzero(~unambiguous)
    if len(ambiguous) and np.isfinite(matrix[ambiguous]).all():
        try:
            singular = np.linalg.svd(matrix[ambiguous], compute_uv=False)
            tolerance = singular[:, 0] * 3 * np.finfo(matrix.dtype).eps
            ambiguous = ambiguous[~(singular[:, -1] < tolerance / 16.)]
        except np.linalg.LinAlgError:
            pass  # Frozen scalar fallback remains authoritative on failure.
    for spot in ambiguous:
        out[int(spot)] = _measurement._circle(points[triples[spot]])
    return out


def fit_circle_diameter_batched(points_xy_mm: object, *, validity,
                                min_points: int = 12,
                                max_residual_mm: float = 0.25,
                                min_angular_coverage_rad: float = 1.5 * math.pi):
    """Drop-in exact variant of ``continuous_measurement.fit_circle_diameter``.

    The only work removed is the ``np.median`` evaluation of hypotheses whose
    inlier count is below the current maximum: those can never replace the best
    mask.  Among the hypotheses tied at the maximum count the original loop keeps
    the first strictly-lowest median error, which is reproduced below by
    iterating the tied candidates in draw order.
    """
    validity.__post_init__()
    _measurement._finite("max_residual_mm", max_residual_mm)
    _measurement._finite("min_angular_coverage_rad", min_angular_coverage_rad)
    if (type(min_points) is not int or min_points < 3 or max_residual_mm <= 0
            or not 0 < min_angular_coverage_rad <= math.tau):
        raise ValueError("invalid circle fit limits")
    if not validity.valid:
        return _measurement._unavailable("axis_timing_or_volume_unvalidated", validity)
    points = _measurement._points(points_xy_mm, 2)
    if points is None:
        return _measurement._unavailable("invalid_or_nonfinite_points", validity)
    if len(points) < min_points:
        return _measurement._unavailable("insufficient_points", validity, len(points))
    if len(points) * TRIPLE_HYPOTHESES > _MAX_BATCH_ELEMENTS:
        # Bounded-memory escape hatch: bit-identical to the frozen implementation.
        return _measurement.fit_circle_diameter(
            points, validity=validity, min_points=min_points,
            max_residual_mm=max_residual_mm,
            min_angular_coverage_rad=min_angular_coverage_rad)

    triples = circle_triples(len(points))
    hypothesis_helper = (_circle_hypotheses_rank_filtered
        if os.environ.get('R28_FEATURE_BATCH_RANK','0') == '1'
        else _measurement._circle_hypotheses)
    hypotheses = hypothesis_helper(points, triples)
    centers = np.empty((len(hypotheses), 2), dtype=float)
    radii = np.empty(len(hypotheses), dtype=float)
    usable = np.zeros(len(hypotheses), dtype=bool)
    for index, hypothesis in enumerate(hypotheses):
        if hypothesis is None:
            continue
        centers[index] = hypothesis[0]
        radii[index] = hypothesis[1]
        usable[index] = True
    if not usable.any():
        return _measurement._unavailable("insufficient_circle_consensus", validity, 0)

    centers = centers[usable]
    radii = radii[usable]
    # Same float expression per point as ``np.linalg.norm(points - center, axis=1)``;
    # the broadcast only removes the Python hypothesis loop.
    differences = points[None, :, :] - centers[:, None, :]
    residual = np.abs(np.linalg.norm(differences, axis=2) - radii[:, None])
    inliers = residual <= max_residual_mm
    counts = inliers.sum(axis=1)
    peak = int(counts.max())
    if peak < min_points:
        return _measurement._unavailable("insufficient_circle_consensus", validity, peak)

    candidates = np.flatnonzero(counts == peak)
    best_index = int(candidates[0])
    if len(candidates) > 1 and peak < len(points):
        cand_inliers = inliers[candidates]
        if not (cand_inliers == cand_inliers[0]).all():
            best_error = math.inf
            for index in candidates:
                error = float(np.median(residual[index][inliers[index]]))
                if error < best_error:
                    best_error, best_index = error, int(index)
    best = inliers[best_index].copy()

    fit = _measurement._circle(points[best])
    if fit is None:
        return _measurement._unavailable("degenerate_circle", validity, int(best.sum()))
    center, radius = fit
    residual = np.abs(np.linalg.norm(points - center, axis=1) - radius)
    best &= residual <= max_residual_mm
    if best.sum() < min_points:
        return _measurement._unavailable("insufficient_circle_consensus", validity, int(best.sum()))
    angles = np.sort(np.arctan2(points[best, 1] - center[1], points[best, 0] - center[0]))
    largest_gap = np.max(np.diff(np.r_[angles, angles[0] + math.tau]))
    if math.tau - largest_gap < min_angular_coverage_rad:
        return _measurement._unavailable("occluded_angular_support", validity, int(best.sum()))
    rms = float(np.sqrt(np.mean(residual[best] ** 2)))
    return _measurement.MeasurementResult(
        2 * radius, "available", None, int(best.sum()), validity.reference_basis,
        residual_mm=rms)


def slice_models(points: np.ndarray, ids: np.ndarray, validity, policy,
                 primitive: Callable[..., dict[str, Any] | None]):
    """Reproduce one level of ``features_v4._solid_features`` before track matching.

    The sequential track/partial-track state is deliberately *not* touched here:
    every fit, component scan and reuse decision depends only on this Z level.
    Returning the exact ``(support, model, partial_center)`` triples lets the
    caller replay the frozen level order without re-fitting anything.
    """
    subset = points[ids, :2]
    whole = primitive(subset, validity, policy)
    count = len(ids)
    whole_indices = np.arange(count)
    if whole is not None and whole["shape"] == "cylinder":
        radial = np.abs(np.linalg.norm(subset - whole["center"], axis=1)
                        - whole["sizes"][0] / 2)
        owned = np.flatnonzero(radial <= policy.residual_mm)
        remainder = np.flatnonzero(radial > policy.residual_mm)
        clusters = [owned]
        clusters.extend(remainder[component] for component in connected_cells(
            points[ids[remainder], :2], policy.component_cell_mm))
    else:
        clusters = connected_cells(subset, policy.component_cell_mm)

    results = []
    for cluster in clusters:
        support = ids[cluster]
        if len(cluster) == count and np.array_equal(cluster, whole_indices):
            # Same already-fitted slice-level model as the historical code; this
            # avoids a duplicate 128-hypothesis fit without changing its inputs.
            model = whole
        else:
            model = primitive(points[support, :2], validity, policy)
        observed_center = None
        if model is None and len(support) >= policy.minimum_points:
            observed_center = np.median(points[support, :2], axis=0)
        results.append((support, model, observed_center))
    return results


def _run_slice_chunk(bounds: tuple[int, int]):
    """Worker entry point; state is inherited copy-on-write under ``fork``."""
    state = _WORKER_STATE
    if state is None:
        raise RuntimeError("slice worker state missing")
    output = []
    for index in range(bounds[0], bounds[1]):
        ids = state["levels"][index][1]
        output.append(slice_models(state["points"], ids, state["validity"],
                                   state["policy"], state["primitive"]))
    return output


def _chunk_bounds(total: int, tasks: int) -> list[tuple[int, int]]:
    tasks = max(1, min(total, tasks))
    step = int(math.ceil(total / tasks))
    return [(start, min(start + step, total)) for start in range(0, total, step)]


def run_slice_jobs(points: np.ndarray, levels: Sequence[tuple[int, np.ndarray]],
                   validity, policy, primitive: Callable[..., dict[str, Any] | None],
                   workers: int, reason: str):
    """Return one ``slice_models`` result per level, in the original level order.

    Serial is the correctness baseline.  A ``fork`` pool is used only for
    ``workers >= 2`` on POSIX; the input cloud and level index arrays live in the
    parent and are inherited, while the parent replays results strictly by level
    order. Operational pool errors discard partial results and fail; only an
    unavailable fork backend is selected as serial before work starts.
    """
    total = len(levels)
    base = {"requested_workers": int(workers), "reason": reason,
            "levels": total, "points": int(len(points))}
    if workers <= 1 or total <= 1:
        results = [slice_models(points, ids, validity, policy, primitive)
                   for _level, ids in levels]
        _note_execution(mode="serial", effective_workers=1, **base)
        return results
    if not _fork_available():
        results = [slice_models(points, ids, validity, policy, primitive)
                   for _level, ids in levels]
        _note_execution(mode="serial_fallback", effective_workers=1,
                        fallback_reason="fork_unavailable", **base)
        return results

    import multiprocessing as multiprocessing

    bounds = _chunk_bounds(total, workers * 8)
    global _WORKER_STATE
    _WORKER_STATE = {"points": points, "levels": levels, "validity": validity,
                     "policy": policy, "primitive": primitive}
    try:
        timeout = float(os.environ.get("R26_FEATURE_POOL_TIMEOUT_S", "300") or 300)
    except ValueError:
        timeout = 300.0
    timeout = max(1.0, timeout)
    try:
        context = multiprocessing.get_context("fork")
        with context.Pool(processes=workers) as pool:
            chunks = pool.map_async(_run_slice_chunk, bounds, chunksize=1).get(
                timeout=timeout)
        results = [item for chunk in chunks for item in chunk]
        if len(results) != total:
            raise RuntimeError("slice worker returned %d/%d results" % (len(results), total))
        _note_execution(mode="process_pool", effective_workers=workers,
                        tasks=len(bounds), timeout_s=timeout, **base)
        return results
    except multiprocessing.TimeoutError as error:
        _note_execution(mode="failed", effective_workers=workers,
                        fallback_reason="pool_timeout_no_retry", **base)
        raise FeatureOperationError("feature_pool_timeout") from error
    except Exception as error:  # no full serial retry after operational failure
        _note_execution(mode="failed", effective_workers=workers,
                        fallback_reason=type(error).__name__, **base)
        raise FeatureOperationError('feature_pool_failed: %s' % type(error).__name__) from error
    finally:
        _WORKER_STATE = None


class ZIndex:
    """Ascending point indices inside a closed Z interval, in original index order.

    ``np.flatnonzero((z >= lo) & (z <= hi))`` scans the complete cloud for every
    track/side pair in the frozen extractor.  One stable sort plus two
    ``searchsorted`` calls return exactly the same index set, then ``np.sort``
    restores the ascending index order the historical boolean scan produced.
    """

    __slots__ = ("z_sorted", "order")

    def __init__(self, points: np.ndarray):
        z = np.asarray(points)[:, 2]
        self.order = np.argsort(z)
        self.z_sorted = z[self.order]

    def window(self, lower: float, upper: float) -> np.ndarray:
        left = int(np.searchsorted(self.z_sorted, lower, side="left"))
        right = int(np.searchsorted(self.z_sorted, upper, side="right"))
        return np.sort(self.order[left:right])


class FeatureOperationError(RuntimeError):
    """A final analysis did not finish; no partial feature result is publishable."""

    def __init__(self, reason, elapsed_s=0.0, diagnostics=None):
        self.reason = reason
        self.elapsed_s = float(elapsed_s)
        self.diagnostics = dict(diagnostics or {})
        super().__init__(reason)


def _available_memory():
    """Linux available memory; unavailable monitoring is explicit, never zero."""
    try:
        with open('/proc/meminfo', encoding='ascii') as stream:
            for line in stream:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _bounded_child(connection, operation, args, kwargs, result_path, maximum_result_bytes):
    # The forked task inherits the acquisition/HDMI parent's Python object graph.
    # Cyclic-GC traversal would dirty its copy-on-write pages for no algorithmic
    # benefit. This bounded disposable process contains no retained cycles; the
    # OS reclaims all of it on exit. Slice children inherit the disabled GC too.
    import gc
    import pickle
    gc.disable()
    if os.name == 'posix':
        try:
            import ctypes
            ctypes.CDLL(None).malloc_trim(0)
        except (AttributeError, OSError):
            pass
    # A separate session lets the supervisor stop the slice-pool descendants too.
    if os.name == 'posix':
        os.setsid()
    try:
        result = operation(*args, **kwargs)
        # Stream the result instead of materializing pickle bytes alongside the
        # complete feature/audit object graph. Only small metadata uses the pipe.
        class BoundedOutput:
            def __init__(self, stream):
                self.stream = stream
                self.size = 0

            def write(self, data):
                if self.size + len(data) > maximum_result_bytes:
                    raise FeatureOperationError('feature_result_capacity')
                written = self.stream.write(data)
                self.size += written
                return written

        with open(result_path, 'xb') as stream:
            output = BoundedOutput(stream)
            pickle.dump(result, output, protocol=4)
        connection.send(('ok_file', output.size, last_execution()))
    except BaseException as error:
        connection.send(('error', type(error).__name__, str(error)))
    finally:
        connection.close()


def run_bounded(operation, args=(), kwargs=None, *, deadline_s=120.0,
                cancelled=None, minimum_available_bytes=128 * 1024**2,
                memory_available=None, result_directory=None,
                maximum_result_bytes=64 * 1024**2):
    """Supervise *all* feature computation, IPC and cleanup under one budget.

    The child may use its two-worker slice pool. Timeout/cancellation never
    retries serially. A bounded file spool carries the result and is consumed
    after child exit; the pipe carries only completion metadata. On POSIX the
    whole isolated process group is killed, including pool descendants.
    """
    import multiprocessing
    import queue
    import signal
    import threading
    import time
    import tempfile
    import pickle

    if not math.isfinite(deadline_s) or deadline_s <= 0:
        raise ValueError('deadline_s must be positive finite')
    if type(minimum_available_bytes) is not int or minimum_available_bytes < 0:
        raise ValueError('minimum_available_bytes must be a non-negative integer')
    if type(maximum_result_bytes) is not int or maximum_result_bytes <= 0:
        raise ValueError('maximum_result_bytes must be a positive integer')
    started = time.monotonic()
    deadline = started + deadline_s
    memory_available = memory_available or _available_memory

    def guard(stage='feature_supervisor'):
        if cancelled is not None and cancelled():
            raise FeatureOperationError('feature_cancelled', time.monotonic() - started)
        available = memory_available()
        if available is not None and available < minimum_available_bytes:
            raise FeatureOperationError('feature_low_memory', time.monotonic() - started,
                diagnostics=dict(available_at_guard_bytes=available,
                                 minimum_available_bytes=minimum_available_bytes,
                                 supervisor_pid=os.getpid(), stage=stage))
        if time.monotonic() >= deadline:
            raise FeatureOperationError('feature_deadline_exceeded', time.monotonic() - started)

    guard()
    # fork inherits large point arrays copy-on-write; Windows uses spawn.
    context = multiprocessing.get_context('fork' if os.name == 'posix' else 'spawn')
    receive, send = context.Pipe(duplex=False)
    options = dict(kwargs or {})
    available_at_launch = memory_available()
    requested_workers = options.get('workers')
    # One supervisor plus two pool workers created a new memory peak on the
    # 512MiB board. Reserve 64MiB above the *unchanged*128MiB safety floor before
    # requesting that pool. Serial execution is still deadline supervised.
    memory_selected_workers = requested_workers
    if (requested_workers is not None and requested_workers > 1
            and available_at_launch is not None
            and available_at_launch < minimum_available_bytes + 64 * 1024**2):
        options['workers'] = 1
        memory_selected_workers = 1
    spool = tempfile.TemporaryDirectory(prefix='r26_feature_', dir=result_directory)
    result_path = str(Path(spool.name) / 'result.pickle')
    process = context.Process(target=_bounded_child,
                              args=(send, operation, args, options, result_path,
                                    maximum_result_bytes))
    messages = queue.Queue(maxsize=1)

    aborted = threading.Event()

    def read_message():
        import pickle
        try:
            metadata = receive.recv()
            # Do not materialize a second full feature/audit object graph while
            # the child's original graph and slice working sets remain live.
            while process.is_alive() and not aborted.is_set():
                time.sleep(.005)
            if not aborted.is_set():
                guard()
                if metadata[0] == 'ok_file':
                    size = Path(result_path).stat().st_size
                    if size != metadata[1] or size > maximum_result_bytes:
                        raise OSError('feature result spool size mismatch')
                    guard('feature_result_deserialize_before')
                    with open(result_path, 'rb') as stream:
                        result = pickle.load(stream)
                        if stream.read(1):
                            raise OSError('feature result spool has trailing data')
                    guard('feature_result_deserialize_after')
                    metadata = ('ok', result, metadata[2])
                guard()
                messages.put(metadata)
        except FeatureOperationError as error:
            if not aborted.is_set():
                messages.put(('supervisor_error', error))
        except (EOFError, OSError, pickle.UnpicklingError, ValueError) as error:
            if not aborted.is_set():
                messages.put(('error', type(error).__name__, str(error)))

    receiver = threading.Thread(target=read_message, daemon=True)
    try:
        process.start()
        send.close()
        receiver.start()
        while True:
            guard()
            try:
                message = messages.get(timeout=min(0.025, max(0.001, deadline-time.monotonic())))
            except queue.Empty:
                continue
            guard()
            if message[0] == 'supervisor_error':
                raise message[1]
            if message[0] != 'ok':
                raise FeatureOperationError('feature_worker_failed: %s: %s' % message[1:],
                                            time.monotonic()-started)
            global _LAST_EXECUTION
            _LAST_EXECUTION = dict(message[2])
            _LAST_EXECUTION['supervisor_memory'] = dict(
                available_at_launch_bytes=available_at_launch,
                minimum_available_bytes=minimum_available_bytes,
                requested_workers=requested_workers,
                selected_workers=memory_selected_workers,
                fork_child_gc_disabled=True,
                deserialize_after_child_exit=True,
                result_transport='bounded_file',
                maximum_result_bytes=maximum_result_bytes,
                serialized_result_bytes=Path(result_path).stat().st_size)
            return message[1]
    finally:
        aborted.set()
        # SIGKILL avoids an unbounded Pool.join/serial-fallback cleanup path.
        # Do not use killpg until setsid is confirmed; never signal our group.
        if process.pid is not None:
            if os.name == 'posix':
                try:
                    if os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                    elif process.is_alive():
                        process.kill()
                except ProcessLookupError:
                    pass
            elif process.is_alive():
                process.kill()
            process.join(timeout=max(0.0, min(0.1, deadline-time.monotonic())))
        send.close()
        receive.close()
        receiver.join(timeout=.1)
        if not receiver.is_alive():
            spool.cleanup()
        else:
            # Never unlink a result while the bounded receiver still reads it.
            # Retain the path explicitly for the failed-run audit and cleanup.
            spool._finalizer.detach()
            retained = dict(result_spool_retained=result_path,
                            reason='receiver_still_active_at_deadline')
            _LAST_EXECUTION['cleanup'] = retained
            error = sys.exc_info()[1]
            if isinstance(error, FeatureOperationError):
                error.diagnostics.update(retained)
