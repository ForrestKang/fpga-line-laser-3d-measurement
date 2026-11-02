"""R22 resource admission and disk-backed cloud for the formal application."""
from pathlib import Path
import math
import os
import shutil
import numpy as np

from resource_policy import ResourceLimits, StorageBudget, capacity_plan, BudgetExceeded, accounted_file_bytes
from candidate.ps_pipeline_r22 import CloudSpool
from structured_light.scan3d.multibranch_processor_r21 import MultiBranchProcessor
from structured_light.scan3d.continuous_processor import CloudSnapshot


def managed_data_bytes(root):
    root=Path(root)
    if not root.exists():return 0
    if root.is_symlink() or getattr(root,'is_junction',lambda:False)():
        raise ValueError('managed data root must not be a link')
    total=0;seen=set()
    def fail(error):raise error
    for current,dirs,files in os.walk(root,followlinks=False,onerror=fail):
        for name in dirs+files:
            p=Path(current)/name
            if p.is_symlink() or getattr(p,'is_junction',lambda:False)():
                raise ValueError('managed data tree contains a link')
        for name in files:
            p=Path(current)/name;s=p.stat();key=s.st_dev,s.st_ino
            if key not in seen:total+=accounted_file_bytes(s);seen.add(key)
    return total


def admit_formal_storage(output, *, max_frames, data_root=None, limits=ResourceLimits(),frame_bound_bytes=4*1024*1024):
    output=Path(output).resolve();data_root=Path(data_root or output.parent).resolve()
    if data_root not in output.parents:
        raise ValueError('run output must be beneath managed data root')
    existing=data_root
    while not existing.exists():existing=existing.parent
    used=managed_data_bytes(data_root)
    # Conservative bound includes native Raw, every candidate/quality/XYZ,
    # both original PL packets, spool + final export, indexes and summaries.
    if type(frame_bound_bytes) is not int or frame_bound_bytes<4*1024*1024:
        raise ValueError('conservative per-frame storage bound required')
    frame_bound=frame_bound_bytes
    free=lambda:int(shutil.disk_usage(existing).free)
    plan=capacity_plan(max_frames,frame_bound,existing_data_bytes=used,
                       filesystem_free_bytes=free(),limits=limits)
    if not plan['admitted']:
        raise BudgetExceeded('formal scan storage preflight failed: '+','.join(plan['reasons']))
    plan.update(data_root=str(data_root),max_frames=max_frames,frame_bound_bytes=frame_bound)
    return StorageBudget(used,free,limits),plan


class DiskCloudProcessor(MultiBranchProcessor):
    supports_streamed_measurement=True
    def __init__(self,*,cloud_spool,**kwargs):
        # The old 250k limit protected a RAM list. Disk-backed accumulation is
        # governed by live quota reservations, not that unrelated RAM limit.
        kwargs.setdefault('max_cloud_points',cloud_spool.storage_budget.limits.data_bytes//24)
        super().__init__(**kwargs)
        self._cloud=cloud_spool

    def snapshot(self):
        with self._lock:
            return CloudSnapshot(self._revision,self._cloud.snapshot(),self.measurement_validity.reference_basis)

    def display_snapshot(self,limit=24000):
        if type(limit) is not int or not 1<=limit<=24000:
            raise ValueError('display point budget exceeded')
        with self._lock:
            count=self._cloud.count
            if not count:points=np.empty((0,3),np.float64)
            else:
                self._cloud.stream.flush()
                mm=np.memmap(self._cloud.path,dtype=np.float64,mode='r',shape=(count,3))
                try:points=mm[::max(1,math.ceil(count/limit))].copy()
                finally:mm._mmap.close()
            points.setflags(write=False)
            return CloudSnapshot(self._revision,points,self.measurement_validity.reference_basis)

    def measure_diameters(self,*,height_bins_mm,**fit_options):
        from structured_light.scan3d.continuous_measurement import (
            diameters_by_height,fit_circle_diameter,MeasurementResult)
        # Reuse frozen validation, bin convention and fitter. Only select the
        # same points from disk instead of materializing the entire cloud.
        validity=self.measurement_validity
        validation=diameters_by_height(np.empty((0,3)),height_bins_mm=height_bins_mm,
                                       validity=validity,**fit_options)
        if not validity.valid:return validation
        results=[]
        for index,(lo,hi) in enumerate(height_bins_mm):
            def mask(chunk):
                return (chunk[:,2]>=lo)&((chunk[:,2]<=hi) if index==len(height_bins_mm)-1 else chunk[:,2]<hi)
            count=sum(int(mask(chunk).sum()) for chunk in self._cloud.chunks())
            # Measured NumPy/SVD fitter scratch scales below128 bytes/point on
            # the host; reserve256 plus2MiB for chunk/masks and library workspace.
            if count*256+2*1024*1024>self._cloud.scratch_bytes:
                results.append(MeasurementResult(None,'unavailable','resource_scratch_budget',
                                                 count,validity.reference_basis))
                continue
            selected=np.empty((count,2),np.float64);offset=0
            for chunk in self._cloud.chunks():
                rows=chunk[mask(chunk),:2];selected[offset:offset+len(rows)]=rows;offset+=len(rows)
            results.append(fit_circle_diameter(selected,validity=validity,**fit_options))
        return tuple(results)


def export_cloud_ply(spool,path,source):
    """Complete measured XYZ, streamed with an accounted ASCII upper bound."""
    maximum=0.
    for chunk in spool.chunks():
        if len(chunk):maximum=max(maximum,float(np.max(np.abs(chunk))))
    digits=len(str(int(maximum))) if maximum>=1 else 1
    header=('ply\nformat ascii 1.0\ncomment source '+source+'\n'
            'comment physical_accuracy_verified false\n'
            f'element vertex {spool.count}\nproperty float x\nproperty float y\nproperty float z\nend_header\n')
    bound=len(header.encode('ascii'))+spool.count*3*(digits+12)
    token=spool.storage_budget.reserve((bound*5+3)//4)
    # On partial I/O failure leave the reservation and file for diagnosis.
    with Path(path).open('x',encoding='ascii',newline='\n') as out:
        out.write(header)
        for chunk in spool.chunks():np.savetxt(out,chunk,fmt='%.7f')
        out.flush();os.fsync(out.fileno())
    spool.storage_budget.commit(token,Path(path).stat().st_size)
