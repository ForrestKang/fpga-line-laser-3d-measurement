"""Prevalidated immutable geometry constants; unchanged OpenCV rays and gates.

The original vector/scalar sections remain the oracle near numerical gates.
This does not change calibration, measurement range or optical admission.
"""
from dataclasses import dataclass
import numpy as np

from .geometry import pixel_rays, _unit_rows
from .section_vector_r14 import reconstruct_section_vector, intersect_plane_vector


def _immutable(values):
    data=np.ascontiguousarray(values,dtype=np.float64)
    return np.frombuffer(data.tobytes(),dtype=np.float64).reshape(data.shape)


@dataclass(frozen=True,init=False,eq=False)
class SectionContext:
    def __init__(self,K,distortion,plane,range_limits_mm,training_bounds_mm):
        # Use public original validators once, then own immutable copies.
        pixel_rays(np.empty((0,2)),K,distortion)
        intersect_plane_vector(np.empty((0,3)),plane,depth_limits_mm=range_limits_mm)
        bounds=np.asarray(training_bounds_mm,dtype=float)
        if bounds.shape!=(2,3) or not np.isfinite(bounds).all() or np.any(bounds[0]>bounds[1]):
            raise ValueError('invalid training bounds')
        if range_limits_mm is None:raise ValueError('explicit range limits required')
        object.__setattr__(self,'K',_immutable(K))
        object.__setattr__(self,'distortion',None if distortion is None else _immutable(np.asarray(distortion).reshape(-1)))
        object.__setattr__(self,'plane',_immutable(plane))
        object.__setattr__(self,'range',_immutable(range_limits_mm))
        object.__setattr__(self,'bounds',_immutable(bounds))
        scale=float(np.max(np.abs(self.plane[:3])))
        normal=self.plane[:3]/scale
        length=float(np.linalg.norm(normal))
        object.__setattr__(self,'normal',_immutable(normal/length))
        object.__setattr__(self,'offset',float((self.plane[3]/scale)/length))

    def reference(self,rows,centers,accepted):
        return reconstruct_section_vector(rows,centers,accepted,self.K,self.distortion,
                                          self.plane,self.range,self.bounds)

    def __call__(self,rows,xc_fixed,stripe_valid):
        rows=np.asarray(rows)
        centers=np.asarray(xc_fixed)
        accepted=np.asarray(stripe_valid,bool)
        if rows.ndim!=1 or centers.shape!=rows.shape or accepted.shape!=rows.shape:
            raise ValueError('row arrays must have identical one-dimensional shape')
        if not np.isfinite(rows).all() or not np.isfinite(centers).all():
            raise ValueError('nonfinite pixel coordinates')
        points=np.full((len(rows),3),np.nan)
        valid=np.zeros(len(rows),bool)
        outside=np.zeros(len(rows),bool)
        reasons=np.full(len(rows),'invalid_stripe',dtype=object)
        ids=np.flatnonzero(accepted)
        if not len(ids):
            return dict(points_mm=points,valid=valid,reasons=reasons,outside_training_bounds=outside)
        try:import cv2
        except ImportError as exc:raise RuntimeError('pixel_rays requires OpenCV') from exc
        pixels=np.column_stack((centers[ids]/4096.,rows[ids]))
        xy=cv2.undistortPoints(pixels.reshape(-1,1,2),self.K,self.distortion).reshape(-1,2)
        if not np.isfinite(xy).all():raise ValueError('OpenCV returned non-finite undistorted coordinates')
        directions=_unit_rows(np.column_stack((xy,np.ones(len(ids)))),name='ray')
        n=self.normal
        cosines=(n[0]*directions[:,0]+n[1]*directions[:,1])+n[2]*directions[:,2]
        # Preserve the original scalar decision path near parallel/range/zero
        # and finite numeric extremes; no extrapolation or relaxed boundaries.
        if np.any(np.abs(cosines)<1e-3):return self.reference(rows,centers,accepted)
        with np.errstate(over='ignore',under='ignore',invalid='ignore',divide='ignore'):
            distances=-self.offset/cosines
        limits=self.range
        if (not np.isfinite(distances).all() or np.any(np.abs(distances)<1e-8)
                or np.any(np.abs(distances-limits[0])<1e-8)
                or np.any(np.abs(distances-limits[1])<1e-8)
                or (limits[1]>1000. and np.any(np.abs(distances)>1000.))):
            return self.reference(rows,centers,accepted)
        ahead=distances>0.
        reasons[ids[~ahead]]='behind'
        in_range=ahead&(distances>=limits[0])&(distances<=limits[1])
        reasons[ids[ahead&~in_range]]='out_of_range'
        good_ids=ids[in_range]
        with np.errstate(over='ignore',under='ignore',invalid='ignore'):
            hits=distances[in_range,None]*directions[in_range]
        if not np.isfinite(hits).all():return self.reference(rows,centers,accepted)
        points[good_ids]=hits
        valid[good_ids]=True
        reasons[good_ids]=None
        if len(hits) and np.any(np.abs(hits[:,None,:]-self.bounds[None,:,:])<1e-8):
            return self.reference(rows,centers,accepted)
        outside[good_ids]=np.any((hits<self.bounds[0])|(hits>self.bounds[1]),axis=1)
        return dict(points_mm=points,valid=valid,reasons=reasons,outside_training_bounds=outside)
