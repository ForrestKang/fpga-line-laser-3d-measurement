"""R35 Q30 integer geometry/config encoder. No hardware access or pixel work.

This is a numerical reference and parameter packer, not a substitute for PL.
All fixed multiplications and divisions use nearest-even, signed64 checks.
"""
import math
import numpy as np

ONE = 1 << 30
WORDS = 36
REASONS = {0: '', 1: 'parallel', 2: 'behind', 3: 'out_of_range',
           4: 'outside_training_bounds', 5: 'numeric_overflow', 6: 'unconfigured',
           7: 'unsupported', 8: 'invalid_uv', 9: 'inverse_domain'}


def encode(value):
    n = round(float(value)*ONE)
    if not -(1 << 63) <= n < (1 << 63):
        raise OverflowError('Q30 coefficient outside signed64')
    return n


def round_ratio(a, b):
    if not b:
        raise ZeroDivisionError
    negative = (a < 0) != (b < 0)
    q, r = divmod(abs(a), abs(b))
    q += 2*r > abs(b) or (2*r == abs(b) and q & 1)
    return -q if negative else q


def signed64(n):
    if not -(1 << 63) <= n < (1 << 63):
        raise OverflowError
    return n


def mul(a, b):
    return signed64(round_ratio(a*b, ONE))


def div(a, b):
    return signed64(round_ratio(a << 30, b))


def parameters(calibration, angle_rad=0.):
    c = calibration
    p = [0]*WORDS
    k = c.camera_matrix
    p[:4] = list(map(encode, [k[0, 2], k[1, 2], 1/k[0, 0], 1/k[1, 1]]))
    distortion = np.asarray(c.distortion)
    # Admission is deliberately narrower than arbitrary float64 calibration.
    # Unsupported layouts/domains receive an explicit hardware reason; they
    # must not become a silently approximated measurement calibration.
    unsupported = (len(distortion) not in (4, 5) or k[0, 1] != 0 or k[1, 0] != 0
                   or min(k[0, 0], k[1, 1]) < 1
                   or max(abs(k[0, 2]), abs(k[1, 2])) > 4096
                   or np.any(abs(distortion) > 1))
    d = np.pad(distortion[:5], (0, max(0, 5-len(distortion))))
    p[4:9] = list(map(encode, d))
    norm = np.linalg.norm(c.plane[:3])
    normalized_plane = c.plane/norm
    unsupported |= abs(normalized_plane[3]) > 16384
    p[9:13] = list(map(encode, normalized_plane))
    p[13] = round(1e-12*(1 << 60))
    flags = 0
    if np.any(distortion): flags |= 4
    if c.ray_range_mm is not None:
        flags |= 1
        p[14:16] = list(map(encode, np.asarray(c.ray_range_mm)**2))
    if c.training_bounds_mm is not None:
        flags |= 2
        p[16:22] = list(map(encode, np.asarray(c.training_bounds_mm).ravel()))
    theta = -float(angle_rad)*c.rotation_angle_sign
    co, si = math.cos(theta), math.sin(theta)
    rotation = np.array([[co, si, 0], [-si, co, 0], [0, 0, 1.]])
    h = c.basis_camera @ rotation
    p[22:31] = list(map(encode, h.ravel()))
    p[31:34] = list(map(encode, -c.axis_point_mm @ h))
    unsupported |= (not np.allclose(h.T @ h, np.eye(3), rtol=0, atol=1e-6)
                    or np.max(abs(c.axis_point_mm @ h)) > 16384)
    if unsupported: flags |= 8
    p[34] = flags
    p[35] = encode(16)
    return p


def record(candidate_id, camera, axis, modes, reason=0):
    if reason:
        return [candidate_id] + [0]*6 + [(reason << 16) | modes]
    values = [round_ratio(n, 1 << 14) for n in camera+axis]
    if any(n < -(1 << 31) or n >= 1 << 31 for n in values):
        return record(candidate_id, [], [], modes, 5)
    return [candidate_id] + [n & 0xffffffff for n in values] + [256 | modes]


def reconstruct(p, u_q12, v_q12, candidate_id=0, modes=3):
    if p[34] & 8:
        return record(candidate_id, [], [], modes, 7)
    if u_q12 < 0 or v_q12 < 0 or u_q12 >= 752*4096 or v_q12 >= 480*4096:
        return record(candidate_id, [], [], modes, 8)
    try:
        xd = mul((u_q12 << 18)-p[0], p[2])
        yd = mul((v_q12 << 18)-p[1], p[3])
        x, y = xd, yd
        for i in range(6 if p[34] & 4 else 1):
            x2, y2, xy = mul(x, x), mul(y, y), mul(x, y)
            r2 = x2+y2
            if r2 < 0 or r2 > p[35]:
                return record(candidate_id, [], [], modes, 9)
            if not p[34] & 4 or i == 5: break
            r4, r6 = mul(r2, r2), mul(mul(r2, r2), r2)
            radial = ONE+mul(p[4], r2)+mul(p[5], r4)+mul(p[8], r6)
            dx = 2*mul(p[6], xy)+mul(p[7], r2+2*x2)
            dy = mul(p[6], r2+2*y2)+2*mul(p[7], xy)
            if radial <= 0:
                return record(candidate_id, [], [], modes, 9)
            x, y = div(xd-dx, radial), div(yd-dy, radial)
        q = mul(p[9], x)+mul(p[10], y)+p[11]
        l2 = mul(x, x)+mul(y, y)+ONE
        if (q*q << 30) <= p[13]*l2:
            return record(candidate_id, [], [], modes, 1)
        depth = div(-p[12], q)
        if depth <= 0:
            return record(candidate_id, [], [], modes, 2)
        camera = [mul(depth, x), mul(depth, y), depth]
        if p[34] & 1:
            radius2 = sum(mul(n, n) for n in camera)
            if radius2 < p[14] or radius2 > p[15]:
                return record(candidate_id, [], [], modes, 3)
        if p[34] & 2 and any(camera[i] < p[16+i] or camera[i] > p[19+i] for i in range(3)):
            return record(candidate_id, [], [], modes, 4)
        axis = [p[31+r]+sum(mul(camera[j], p[22+3*j+r]) for j in range(3)) for r in range(3)]
        return record(candidate_id, camera, axis, modes)
    except (OverflowError, ZeroDivisionError):
        return record(candidate_id, [], [], modes, 5)
