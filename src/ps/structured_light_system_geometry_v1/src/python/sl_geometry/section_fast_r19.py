"""Vectorized calibrated ray/plane intersection with reference boundary fallback.

No altered thresholds, units, image resolution or geometry admission. Static
calibration validation is cached, including the original empty-section check.
Rows near a decision boundary use the unchanged scalar reference.
"""
from functools import lru_cache
import numpy as np
import cv2
from .section import reconstruct_section
from .geometry import _unit_rows,intersect_plane

@lru_cache(maxsize=16)
def _calibration(k,d,p,limits,bounds):
    K=np.array(k,float).reshape(3,3);dist=None if d is None else np.array(d,float)
    plane=np.array(p,float);box=np.array(bounds,float).reshape(2,3)
    reconstruct_section(np.empty(0),np.empty(0),np.empty(0,bool),K,dist,plane,limits,box)
    scale=float(np.max(abs(plane[:3])));normal=plane[:3]/scale;length=float(np.linalg.norm(normal));normal/=length
    offset=float((plane[3]/scale)/length)
    return K,dist,plane,normal,offset,box

def reconstruct_section_fast(rows,xc_fixed,stripe_valid,K,distortion,plane,range_limits_mm,training_bounds_mm):
    rows=np.asarray(rows);centers=np.asarray(xc_fixed);accepted=np.asarray(stripe_valid,bool)
    if rows.ndim!=1 or centers.shape!=rows.shape or accepted.shape!=rows.shape:
        raise ValueError('row arrays must have identical one-dimensional shape')
    if not np.isfinite(rows).all() or not np.isfinite(centers).all():raise ValueError('nonfinite pixel coordinates')
    # On malformed calibration preserve reference diagnostics rather than a
    # shape/key construction error from the fast cache.
    ka=np.asarray(K,float);pa=np.asarray(plane,float);ba=np.asarray(training_bounds_mm,float)
    da=None if distortion is None else np.asarray(distortion,float).reshape(-1)
    if ka.shape!=(3,3) or pa.shape!=(4,) or ba.shape!=(2,3):
        return reconstruct_section(rows,centers,accepted,K,distortion,plane,range_limits_mm,training_bounds_mm)
    la=None if range_limits_mm is None else np.asarray(range_limits_mm,float)
    if la is not None and la.shape!=(2,):
        return reconstruct_section(rows,centers,accepted,K,distortion,plane,range_limits_mm,training_bounds_mm)
    limits=None if la is None else tuple(la)
    kval,dist,planeval,normal,offset,bounds=_calibration(tuple(ka.ravel()),None if da is None else tuple(da),tuple(pa),limits,tuple(ba.ravel()))
    points=np.full((len(rows),3),np.nan);valid=np.zeros(len(rows),bool);outside=np.zeros(len(rows),bool);reasons=np.full(len(rows),'invalid_stripe',object)
    ids=np.flatnonzero(accepted)
    if not len(ids):return dict(points_mm=points,valid=valid,reasons=reasons,outside_training_bounds=outside)
    pixels=np.column_stack((centers[ids]/4096.,rows[ids]))
    normalized=cv2.undistortPoints(pixels.reshape(-1,1,2),kval,dist).reshape(-1,2)
    if not np.isfinite(normalized).all():raise ValueError('OpenCV returned non-finite undistorted coordinates')
    rays=np.column_stack((normalized,np.ones(len(ids))));directions=_unit_rows(rays,name='ray')
    cosine=np.einsum('ij,j->i',directions,normal);floor=max(1e-6,10*np.finfo(float).eps)
    rr=np.full(len(ids),None,object);parallel=abs(cosine)<=floor
    with np.errstate(divide='ignore',invalid='ignore',over='ignore'):
        distance=-offset/cosine
    behind=(~np.isfinite(distance))|(distance<=0)|(directions[:,2]<=0)
    rr[behind]='behind';rr[parallel]='parallel';rr[directions[:,2]<=0]='behind'
    ok=~parallel&~behind
    near=(abs(abs(cosine)-floor)<=1e-12)|(np.isfinite(distance)&(abs(distance)<=1e-12))
    if limits is not None:
        lo,hi=limits;in_range=(distance>=lo)&(distance<=hi)
        rr[ok&~in_range]='out_of_range';ok&=in_range
        tolerance=1e-9*np.maximum(1.,abs(distance))
        near|=(abs(distance-lo)<=tolerance)|(abs(distance-hi)<=tolerance)
    hit=np.full((len(ids),3),np.nan);hit[ok]=distance[ok,None]*directions[ok]
    bad_z=ok&(hit[:,2]<=0);rr[bad_z]='behind';ok[bad_z]=False;hit[bad_z]=np.nan
    # Floating reduction order may differ by a few ULPs from np.dot for one
    # row. Preserve exact reference decisions near all scalar boundaries.
    ni=np.flatnonzero(near)
    if len(ni):
        reference=intersect_plane(rays[ni],planeval,depth_limits_mm=limits)
        hit[ni]=reference['points_mm'];ok[ni]=reference['valid'];rr[ni]=reference['reasons']
    points[ids]=hit;valid[ids]=ok;reasons[ids]=rr
    outside[valid]=np.any((points[valid]<bounds[0])|(points[valid]>bounds[1]),axis=1)
    # Training-volume inclusive boundaries need the same scalar point as the
    # reference too, since admission must never change due to a rounding ULP.
    vi=np.flatnonzero(ok)
    near_box=vi[np.any(np.isclose(hit[vi],bounds[0],rtol=1e-9,atol=1e-8)|np.isclose(hit[vi],bounds[1],rtol=1e-9,atol=1e-8),axis=1)]
    if len(near_box):
        ref=intersect_plane(rays[near_box],planeval,depth_limits_mm=limits)
        actual=ids[near_box];points[actual]=ref['points_mm'];valid[actual]=ref['valid'];reasons[actual]=ref['reasons']
        outside[actual]=False
        insidevalid=actual[valid[actual]]
        outside[insidevalid]=np.any((points[insidevalid]<bounds[0])|(points[insidevalid]>bounds[1]),axis=1)
    return dict(points_mm=points,valid=valid,reasons=reasons,outside_training_bounds=outside)
