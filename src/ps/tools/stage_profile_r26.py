"""Hierarchical, exclusive-time profiler for the PL-final geometry path.

Fixes the earlier measurement error: the replay admission now carries the
already validated frame, exactly like the DMA transport, so process_final does
not re-hash/re-decode. Times are nested and exclusive time is own wall minus the
directly measured children, so nothing is counted twice.
"""
from __future__ import annotations
import time

# child -> parent (exclusive accounting)
TREE = {
    'process_final': None,
    'entry_validate_decode': 'process_final',
    'super_process': 'process_final',
    'return_packaging': 'process_final',
    'admit': 'super_process',
    'observations': 'super_process',
    'section': 'super_process',
    'geom_mask': 'super_process',
    'quality': 'super_process',
    'quality_gate': 'super_process',
    'axis_unrotate': 'super_process',
    'cloud_append': 'super_process',
    # Paired-geometry path: these replace observations/section/quality inside
    # the same super_process, so they are siblings, never double-counted with
    # the separate-path wrappers above (a given run uses one path).
    'pair_profile': 'super_process',
    'pair_quality': 'super_process',
    'pair_section': 'super_process',
}


class Recorder:
    def __init__(self):
        self.data = {name: [] for name in TREE}
        self.enabled = True

    def _wrap(self, name, call):
        def wrapped(*args, **kwargs):
            if not self.enabled:
                return call(*args, **kwargs)
            wall = time.perf_counter(); cpu = time.thread_time()
            try:
                return call(*args, **kwargs)
            finally:
                self.data[name].append((time.perf_counter() - wall, time.thread_time() - cpu))
        return wrapped

    def report(self):
        import numpy as np
        rows = {}
        for name, vals in self.data.items():
            if not vals:
                continue
            w = np.asarray([v[0] for v in vals], float) * 1000
            c = np.asarray([v[1] for v in vals], float) * 1000
            rows[name] = dict(n=len(vals), wall_sum=float(w.sum()), cpu_sum=float(c.sum()))
        for name, row in rows.items():
            children = [k for k, v in TREE.items() if v == name and k in rows]
            child_wall = sum(rows[k]['wall_sum'] for k in children)
            row['children'] = children
            row['child_wall_sum'] = child_wall
            row['exclusive_wall_sum'] = row['wall_sum'] - child_wall
        for name, row in rows.items():
            parent = TREE.get(name)
            row['pct_of_parent'] = (round(100 * row['wall_sum'] / rows[parent]['wall_sum'], 1)
                                    if parent and parent in rows and rows[parent]['wall_sum'] else None)
        return rows


def install(recorder=None):
    rec = recorder or Recorder()
    from structured_light.scan3d import multibranch_processor_r21 as mb
    import candidate.pfo3_processor_r25 as p25
    saved = []

    def swap(obj, name, wrapper):
        saved.append((obj, name, obj.__dict__.get(name, None)))
        setattr(obj, name, wrapper)

    cls = p25.PLFinalProcessorR25
    original_pf = cls.process_final

    def process_final(self, capture, metadata=None, *, payload, admission, mode='measurement', **kw):
        if not rec.enabled:
            return original_pf(self, capture, metadata, payload=payload, admission=admission, mode=mode, **kw)
        wall = time.perf_counter(); cpu = time.thread_time()
        try:
            return original_pf(self, capture, metadata, payload=payload, admission=admission, mode=mode, **kw)
        finally:
            rec.data['process_final'].append((time.perf_counter() - wall, time.thread_time() - cpu))
    swap(cls, 'process_final', process_final)

    for obj, name, key in ((mb.MultiBranchProcessor, 'process', 'super_process'),
                           (p25, 'decode', 'entry_validate_decode')):
        original = getattr(obj, name)
        swap(obj, name, rec._wrap(key, original))

    for target in (mb.MultiBranchProcessor, cls):
        for name, key in (('_admit_replay', 'admit'), ('_observations', 'observations'),
                          ('_quality', 'quality'), ('_geometry_quality_gate', 'quality_gate'),
                          ('_reconstruct_final_section', 'section')):
            if name not in target.__dict__ and target is cls:
                continue
            original = target.__dict__.get(name) or getattr(target, name)
            swap(target, name, rec._wrap(key, original))
    for name, key in (('strict_geometry_mask', 'geom_mask'), ('unrotate_points', 'axis_unrotate')):
        if hasattr(mb, name):
            swap(mb, name, rec._wrap(key, getattr(mb, name)))
    try:
        from candidate.pfo3_geometry_pair_r25 import PreparedGeometryPairR25
        for name, key in (('profile', 'pair_profile'), ('quality', 'pair_quality'),
                          ('section', 'pair_section')):
            swap(PreparedGeometryPairR25, name, rec._wrap(key, getattr(PreparedGeometryPairR25, name)))
    except Exception:
        pass
    try:
        from candidate.ps_pipeline_r22 import CloudSpool
        swap(CloudSpool, 'append', rec._wrap('cloud_append', CloudSpool.append))
    except Exception:
        pass

    def restore():
        for obj, name, previous in reversed(saved):
            if previous is None:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, previous)
    return rec, restore


def install_if_requested():
    """Enabled only when SCAN3D_PROFILE_DIR is set; used by the board replay."""
    import os
    directory = os.environ.get('SCAN3D_PROFILE_DIR')
    if not directory:
        return None
    rec, _restore = install()
    return rec, directory
