"""R25 image-only integer oracle; no hardware access or import-time builds."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from enum import IntEnum, IntFlag
import hashlib
import json

import numpy as np

Q = 4096
INVALID_BRANCH = 65535


class CandidateReason(IntFlag):
    NO_HIGH = 1
    WIDTH = 2
    BORDER = 4
    SATURATED = 8
    MULTIPEAK = 16
    ZERO_WEIGHT = 32
    BACKGROUND_ASYMMETRY = 64
    ROW_OVERFLOW = 128
    BRANCH_AMBIGUOUS = 256
    LOW_SUPPORT = 512
    LOW_CONTRAST = 1024


class FrameFlag(IntFlag):
    NO_PREVIEW = 1
    NO_MEASUREMENT = 2
    CAPACITY = 4
    AMBIGUOUS = 8
    SATURATED = 16
    BORDER = 32
    LOW_SUPPORT = 64


class ImageStatus(IntEnum):
    GOOD = 0
    SATURATED = 1
    AMBIGUOUS = 2
    WEAK = 3
    INCOMPLETE = 4
    UNVALIDATED = 5


class PolicyReason(IntEnum):
    ACCEPTED = 0
    LOCAL_REJECT = 1
    AMBIGUOUS_BRANCH = 2
    INSUFFICIENT_SUPPORT = 3
    UNSTABLE_CENTER = 4
    WEAK_RESPONSE = 5
    FRAME_CAPACITY = 6
    INVALID_CENTER = 7


@dataclass(frozen=True)
class V3Config:
    low_dn: int = 6
    high_dn: int = 12
    measure_dn: int = 18
    min_width: int = 3
    max_width: int = 17
    max_asymmetry_dn: int = 12
    peak_drop_dn: int = 4
    branch_step_px: int = 6
    branch_width_step: int = 8
    minimum_preview_support: int = 3
    minimum_measure_support: int = 8

    def __post_init__(self):
        if any(type(v) is not int for v in asdict(self).values()):
            raise ValueError('V3 configuration requires integers')
        if not (1 <= self.low_dn <= self.high_dn <= self.measure_dn <= 255):
            raise ValueError('thresholds must satisfy 1<=low<=high<=measure<=255')
        if not (1 <= self.min_width <= self.max_width <= 31):
            raise ValueError('widths must satisfy 1<=min<=max<=31')
        if not (0 <= self.max_asymmetry_dn <= 255 and 1 <= self.peak_drop_dn <= 255):
            raise ValueError('invalid background or competing peak limits')
        if not (0 <= self.branch_step_px <= 31 and 0 <= self.branch_width_step <= 31):
            raise ValueError('invalid association limits')
        if not (3 <= self.minimum_preview_support <= self.minimum_measure_support <= 480):
            raise ValueError('invalid component support limits')

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True,
                                        separators=(',', ':')).encode()).hexdigest()


DEVELOPMENT_GRID = tuple(V3Config(low_dn=l, high_dn=h, measure_dn=m,
                                 branch_step_px=s, minimum_measure_support=n)
                         for l, h, m in ((4, 8, 12), (6, 12, 18), (8, 16, 24))
                         for s in (4, 6, 8) for n in (8, 12))


@dataclass(frozen=True)
class V3Candidates:
    candidate_id: np.ndarray
    row: np.ndarray
    start: np.ndarray
    end: np.ndarray
    width: np.ndarray
    center_q12: np.ndarray
    sum_weight_q12: np.ndarray
    sum_weight_x: np.ndarray
    detector_peak_q12: np.ndarray
    peak_raw: np.ndarray
    contrast_q12: np.ndarray
    edge_drop_q12: np.ndarray
    background_q12: np.ndarray
    asymmetry_q12: np.ndarray
    peak_count: np.ndarray
    saturated_count: np.ndarray
    local_reasons: np.ndarray
    reasons: np.ndarray
    slot: np.ndarray
    canonical_id: np.ndarray
    component_support: np.ndarray
    stability_q12: np.ndarray
    center_valid: np.ndarray
    preview_usable: np.ndarray
    measurement_usable: np.ndarray
    measurement_reason: np.ndarray
    preview_reason: np.ndarray
    measurement_branch_id: np.ndarray
    preview_branch_id: np.ndarray

    def __len__(self):
        return len(self.row)

    def as_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class V3Frame:
    width: int
    height: int
    raw_count: int
    local_count: int
    slotted_count: int
    preview_count: int
    measurement_count: int
    preview_rows: int
    measurement_rows: int
    ambiguous_count: int
    saturated_pixels: int
    saturated255_pixels: int
    overflow_rows: int
    flags: int
    measure_status: int
    preview_status: int
    measure_median_peak_x2: int
    measure_median_width_x2: int
    preview_median_peak_x2: int
    preview_median_width_x2: int
    raw_peak_median_x2: int
    ambiguous_rows: int
    preview_usable: bool
    measurement_usable: bool


@dataclass(frozen=True)
class V3Result:
    candidates: V3Candidates
    raw_count_by_row: np.ndarray
    local_count_by_row: np.ndarray
    slot_count_by_row: np.ndarray
    row_data: np.ndarray
    frame: V3Frame
    config_fingerprint: str


def _readonly(value, dtype=np.int64):
    result = np.asarray(value, dtype=dtype)
    result.setflags(write=False)
    return result


def detector_row(row):
    """Return exact Q12 background, raw residual, detector and asymmetry arrays."""
    values = np.asarray(row)
    if values.ndim != 1 or values.dtype != np.uint8 or values.size == 0:
        raise ValueError('row must be a nonempty uint8 vector')
    padded = np.pad(values.astype(np.int64), (13, 13), mode='edge')
    w = len(values)
    left = sum(padded[13-d:13-d+w] for d in range(8, 12))
    right = sum(padded[13+d:13+d+w] for d in range(8, 12))
    background = (left + right) * 512
    weight = np.maximum(0, values.astype(np.int64) * Q - background)
    padded_weight = np.pad(weight, (2, 2), mode='edge')
    detector = sum(k*padded_weight[d:d+w]
                   for d, k in enumerate((1, 4, 6, 4, 1))) // 16
    return background, weight, detector, np.abs(left-right)*512


def _peak_count(detector, high, drop):
    maxima = []
    j = 0
    while j < len(detector):
        k = j
        while k+1 < len(detector) and detector[k+1] == detector[j]:
            k += 1
        value = int(detector[j])
        if (value >= high and (j == 0 or value > detector[j-1])
                and (k == len(detector)-1 or value > detector[k+1])):
            maxima.append((j, value))
        j = k+1
    if not maxima:
        return 0
    count = 1
    previous, value = maxima[0]
    for position, next_value in maxima[1:]:
        valley = int(detector[previous:position+1].min())
        if position-previous >= 3 and valley <= min(value, next_value)-drop:
            count += 1
            previous, value = position, next_value
        elif next_value > value:
            previous, value = position, next_value
    return count


def _median_x2(values):
    values = np.sort(np.asarray(values, dtype=np.int64))
    n = len(values)
    return int(values[(n-1)//2] + values[n//2]) if n else 0


def _associate(rows, centers, widths, slots, local, overflow, config):
    """Pure bounded adjacent-row graph; expose for independent graph tests."""
    n = len(rows)
    eligible = (local == 0) & (slots >= 0) & ~overflow[rows]
    edges = []
    predecessors = np.zeros(n, np.int64)
    successors = np.zeros(n, np.int64)
    row_nodes = [np.flatnonzero(eligible & (rows == y)) for y in range(len(overflow))]
    for y in range(1, len(row_nodes)):
        for a in row_nodes[y-1]:
            for b in row_nodes[y]:
                if (abs(int(centers[a])-int(centers[b])) <= config.branch_step_px*Q
                        and abs(int(widths[a])-int(widths[b])) <= config.branch_width_step):
                    edges.append((int(a), int(b)))
                    successors[a] += 1
                    predecessors[b] += 1
    ambiguous = np.zeros(n, bool)
    for a, b in edges:
        if successors[a] != 1 or predecessors[b] != 1:
            ambiguous[a] = ambiguous[b] = True
    parent = np.arange(n)
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = int(parent[a])
        return a
    for a, b in edges:
        if not (ambiguous[a] or ambiguous[b]):
            a, b = find(a), find(b)
            if a != b:
                parent[max(a, b)] = min(a, b)
    ids = np.full(n, INVALID_BRANCH, np.int64)
    support = np.zeros(n, np.int64)
    stability = np.zeros(n, np.int64)
    components = {}
    for a in np.flatnonzero(eligible & ~ambiguous):
        components.setdefault(find(int(a)), []).append(int(a))
    for nodes in components.values():
        canonical = min(int(rows[a])*16+int(slots[a]) for a in nodes)
        for a in nodes:
            ids[a], support[a] = canonical, len(nodes)
    for a, b in edges:
        if not (ambiguous[a] or ambiguous[b]):
            delta = abs(int(centers[a])-int(centers[b]))
            stability[a] = max(stability[a], delta)
            stability[b] = max(stability[b], delta)
    return eligible, ambiguous, ids, support, stability


def extract_v3(gray, config=None):
    """Extract all raw diagnostics and PL-final image sets deterministically."""
    config = V3Config() if config is None else config
    if not isinstance(config, V3Config):
        raise ValueError('config must be V3Config')
    image = np.asarray(gray)
    if image.ndim != 2 or image.dtype != np.uint8:
        raise ValueError('image must be a 2D uint8 array')
    height, width = image.shape
    if not (1 <= width <= 752 and 1 <= height <= 480):
        raise ValueError('image dimensions must be within 752x480')
    names = [f.name for f in fields(V3Candidates)][:-4]
    records = {name: [] for name in names}
    raw_counts = np.zeros(height, np.int64)
    local_counts = np.zeros(height, np.int64)
    for y, row in enumerate(image):
        background, weights, detector, asymmetry = detector_row(row)
        mask = detector >= config.low_dn*Q
        transitions = np.diff(np.r_[False, mask, False].astype(np.int8))
        for a, stop in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)):
            a, b = int(a), int(stop)-1
            length = b-a+1
            peak_index = a+int(np.argmax(detector[a:b+1]))
            peak = int(detector[peak_index])
            total = int(weights[a:b+1].sum())
            moment = int(np.dot(np.arange(a, b+1, dtype=np.int64), weights[a:b+1]))
            sat = int(np.count_nonzero(row[a:b+1] >= 250))
            peaks = _peak_count(detector[a:b+1], config.high_dn*Q, config.peak_drop_dn*Q)
            reason = 0
            if peak < config.high_dn*Q: reason |= CandidateReason.NO_HIGH
            if not config.min_width <= length <= config.max_width: reason |= CandidateReason.WIDTH
            if a < 13 or b > width-14: reason |= CandidateReason.BORDER
            if sat: reason |= CandidateReason.SATURATED
            if peaks > 1: reason |= CandidateReason.MULTIPEAK
            if not total: reason |= CandidateReason.ZERO_WEIGHT
            if asymmetry[peak_index] > config.max_asymmetry_dn*Q:
                reason |= CandidateReason.BACKGROUND_ASYMMETRY
            slot = int(local_counts[y]) if not reason and local_counts[y] < 16 else -1
            local_counts[y] += not reason
            raw_counts[y] += 1
            # Edge drop is a residual ridge diagnostic, not another hidden gate.
            edge = peak - max(int(detector[a-1]) if a else peak,
                              int(detector[b+1]) if b+1 < width else peak)
            record = (len(records['row']), y, a, b, length, moment*Q//total if total else 0,
                      total, moment, peak, int(row[a:b+1].max()), int(weights[a:b+1].max()),
                      edge, int(background[peak_index]), int(asymmetry[peak_index]), peaks,
                      sat, int(reason), int(reason), slot, INVALID_BRANCH, 0, 0,
                      bool(total), False, False)
            for name, value in zip(names, record):
                records[name].append(value)
    arrays = {name: np.asarray(values, dtype=np.int64) for name, values in records.items()}
    overflow = local_counts > 16
    eligible, ambiguous, ids, support, stability = _associate(
        arrays['row'], arrays['center_q12'], arrays['width'], arrays['slot'],
        arrays['local_reasons'], overflow, config)
    reasons = arrays['reasons']
    reasons[(arrays['local_reasons'] == 0) & overflow[arrays['row']]] |= CandidateReason.ROW_OVERFLOW
    reasons[ambiguous] |= CandidateReason.BRANCH_AMBIGUOUS
    reasons[eligible & ~ambiguous & (support < config.minimum_preview_support)] |= CandidateReason.LOW_SUPPORT
    preview = eligible & ~ambiguous & (support >= config.minimum_preview_support)
    reasons[preview & (arrays['detector_peak_q12'] < config.measure_dn*Q)] |= CandidateReason.LOW_CONTRAST
    measure = preview & (support >= config.minimum_measure_support) & (arrays['detector_peak_q12'] >= config.measure_dn*Q)
    # Capacity failure is global: no seemingly successful partial compact sets.
    if np.any(overflow):
        preview[:] = False
        measure[:] = False
    def policy_reasons(minimum_support, measurement):
        result = np.zeros(len(reasons), np.int64)
        if measurement:
            result[arrays['detector_peak_q12'] < config.measure_dn*Q] = PolicyReason.WEAK_RESPONSE
        result[support < minimum_support] = PolicyReason.INSUFFICIENT_SUPPORT
        result[ambiguous] = PolicyReason.AMBIGUOUS_BRANCH
        result[~arrays['center_valid'].astype(bool)] = PolicyReason.INVALID_CENTER
        result[arrays['local_reasons'] != 0] = PolicyReason.LOCAL_REJECT
        if np.any(overflow):
            result[arrays['local_reasons'] == 0] = PolicyReason.FRAME_CAPACITY
        return result
    arrays.update(canonical_id=ids, component_support=support, stability_q12=stability,
                  preview_usable=preview, measurement_usable=measure,
                  measurement_reason=policy_reasons(config.minimum_measure_support, True),
                  preview_reason=policy_reasons(config.minimum_preview_support, False),
                  measurement_branch_id=np.where(measure, ids, INVALID_BRANCH),
                  preview_branch_id=np.where(preview, ids, INVALID_BRANCH))
    row_data = np.zeros((height, 16), np.int64)
    row_data[:, 0] = np.arange(height)
    row_data[:, 1] = image.max(axis=1)
    row_data[:, 2] = (image >= 250).sum(axis=1)
    row_data[:, 3] = (image == 255).sum(axis=1)
    row_data[:, 4] = raw_counts
    row_data[:, 5] = local_counts
    for y in range(height):
        for mask, offset in ((measure, 6), (preview, 7)):
            chosen = mask & (arrays['row'] == y)
            row_data[y, offset] = int(chosen.sum())
            row_data[y, offset+2] = int(arrays['width'][chosen].max()) if np.any(chosen) else 0
            row_data[y, offset+4] = int(arrays['peak_raw'][chosen].max()) if np.any(chosen) else 0
        row_data[y, 12:14] = int(np.any(ambiguous & (arrays['row'] == y)))
    sat_pixels = int((image >= 250).sum())
    slotted = int(np.count_nonzero(arrays['slot'] >= 0))
    amb_count = int(ambiguous.sum())
    sat_ok = sat_pixels*1000 <= image.size
    amb_ok = amb_count*10 <= slotted
    preview_rows = int(np.count_nonzero(row_data[:, 7]))
    measure_rows = int(np.count_nonzero(row_data[:, 6]))
    frame_preview = bool(np.any(preview))
    frame_measure = bool(measure.sum() >= 8 and measure_rows*4 >= height
                         and not np.any(overflow) and sat_ok and amb_ok)
    def status(mask, complete):
        if not sat_ok: return int(ImageStatus.SATURATED)
        if not amb_ok or np.any(overflow): return int(ImageStatus.AMBIGUOUS)
        if not np.any(mask): return int(ImageStatus.WEAK)
        return int(ImageStatus.GOOD if complete else ImageStatus.INCOMPLETE)
    flags = 0
    if not frame_preview: flags |= FrameFlag.NO_PREVIEW
    if not frame_measure: flags |= FrameFlag.NO_MEASUREMENT
    if np.any(overflow): flags |= FrameFlag.CAPACITY
    if amb_count: flags |= FrameFlag.AMBIGUOUS
    if sat_pixels: flags |= FrameFlag.SATURATED
    if np.any(reasons & CandidateReason.BORDER): flags |= FrameFlag.BORDER
    if np.any(reasons & CandidateReason.LOW_SUPPORT): flags |= FrameFlag.LOW_SUPPORT
    frame = V3Frame(width, height, len(arrays['row']), int(local_counts.sum()), slotted,
                    int(preview.sum()), int(measure.sum()), preview_rows, measure_rows,
                    amb_count, sat_pixels, int((image == 255).sum()), int(overflow.sum()),
                    int(flags), status(measure, frame_measure), status(preview, frame_preview),
                    _median_x2(arrays['peak_raw'][measure]), _median_x2(arrays['width'][measure]),
                    _median_x2(arrays['peak_raw'][preview]), _median_x2(arrays['width'][preview]),
                    _median_x2(image.max(axis=1)), int(np.count_nonzero(row_data[:, 12])),
                    frame_preview, frame_measure)
    bool_fields = {'center_valid', 'preview_usable', 'measurement_usable'}
    candidates = V3Candidates(**{name: _readonly(value, bool if name in bool_fields else np.int64)
                                 for name, value in arrays.items()})
    return V3Result(candidates, _readonly(raw_counts), _readonly(local_counts),
                    _readonly(np.minimum(local_counts, 16)), _readonly(row_data), frame,
                    config.fingerprint)


extract_profiles = extract_v3
