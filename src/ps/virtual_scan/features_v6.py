"""Experimental V6 observed-plane pair reference contract, without V4 mutation.

A spacing references two physical faces equally. Raw point-count weighting is
sampling-density dependent and is not its geometric midpoint. All original
supports and numerical measurements remain present; this module adds an
explicit independently checkable geometric reference only.
"""
from dataclasses import dataclass
import hashlib
import numpy as np
from virtual_scan.features_v4 import (FeaturePolicyV5,
                                     discover_features_with_audit as _extract)


@dataclass(frozen=True)
class FeaturePolicyV6(FeaturePolicyV5):
    pass


def _evidence(plane, points):
    ids=np.asarray(plane['support']['point_indices'],dtype=np.int64)
    if not len(ids) or ids.min()<0 or ids.max()>=len(points):
        raise ValueError('invalid plane reference support')
    centroid=points[ids].mean(axis=0)
    normal=np.asarray(plane['direction'],float)
    norm=np.linalg.norm(normal)
    if norm<=0 or not np.isfinite(norm):
        raise ValueError('invalid plane reference normal')
    return dict(region_id=plane['region_id'],support_points=len(ids),
                support_sha256=hashlib.sha256(np.asarray(ids,dtype='<i8').tobytes()).hexdigest(),
                centroid_mm=centroid.tolist(),normal=(normal/norm).tolist())


def reference_from_planes(first,second,points):
    a,b=_evidence(first,points),_evidence(second,points)
    na,nb=np.asarray(a['normal']),np.asarray(b['normal'])
    if na@nb<.995:
        raise ValueError('reference faces are not parallel with consistent direction')
    normal=na+nb;normal/=np.linalg.norm(normal)
    ca,cb=np.asarray(a['centroid_mm']),np.asarray(b['centroid_mm'])
    midpoint=(ca+cb)/2.
    # Each measured physical face receives half weight, independent of sampling.
    return dict(schema='R26_PAIR_REFERENCE_V6',method='equal_weight_observed_face_centroids',
                faces=[a,b],weights=[.5,.5],position_mm=midpoint.tolist(),
                direction=normal.tolist(),physical_accuracy_verified=False)


def _prism_height_evidence(feature, peers, points, policy):
    """Distributed central face interiors, with sampling-aware transverse gates.

    Eight distinct transverse positions over four patches provide geometric
    support even when a patch contains two, rather than three, sample columns.
    Every patch must also span height; missing faces/rails remain unavailable.
    """
    width=next((f for f in peers if f['type']=='prism_width' and f['status']=='available'),None)
    depth=next((f for f in peers if f['type']=='prism_depth' and f['status']=='available'),None)
    if width is None or depth is None:return None
    center=np.asarray(feature['center_xy_mm'],float)
    sizes=np.array([width['value_mm'],depth['value_mm']])
    basis=np.column_stack([np.asarray(width['direction'])[:2],np.asarray(depth['direction'])[:2]])
    if not np.allclose(basis.T@basis,np.eye(2),atol=1e-6):return None
    lo,hi=feature['observed_height_range_mm']
    candidate=np.flatnonzero((points[:,2]>=lo-policy.section_height_mm)&(points[:,2]<=hi+policy.section_height_mm))
    local=(points[candidate,:2]-center)@basis
    contained=np.all(abs(local)<=sizes/2+policy.residual_mm,axis=1)
    ranges=[];evidence=[]
    for axis in (0,1):
        transverse=1-axis
        for sign in (-1.,1.):
            mask=contained&(abs(local[:,transverse])<=.4*sizes[transverse])&(abs(local[:,axis]-sign*sizes[axis]/2)<=policy.residual_mm)
            ids=candidate[mask];coordinate=np.round(local[mask,transverse],7)
            if len(np.unique(coordinate))<8:return None
            bins=np.linspace(-.4*sizes[transverse],.4*sizes[transverse],5)
            patches=[]
            for lower,upper in zip(bins,bins[1:]):
                selected=(coordinate>=lower)&(coordinate<=upper)
                values=np.unique(coordinate[selected]);z=points[ids[selected],2]
                if len(values)<2 or np.ptp(values)<.25*(upper-lower) or len(np.unique(z))<3:return None
                patches.append([float(z.min()),float(z.max())])
            interval=[max(r[0] for r in patches),min(r[1] for r in patches)]
            if interval[1]<=interval[0]:return None
            ranges.append(interval)
            evidence.append(dict(axis=axis,side=sign,transverse_columns=len(np.unique(coordinate)),
                                 patch_ranges_mm=patches,range_mm=interval))
    interval=[max(r[0] for r in ranges),min(r[1] for r in ranges)]
    if interval[1]-interval[0]<policy.minimum_segment_height_mm:return None
    if interval[0]-lo>2*policy.section_height_mm or hi-interval[1]>2*policy.section_height_mm:return None
    return dict(height_mm=interval[1]-interval[0],range_mm=interval,faces=evidence,
                method='V6_four_independent_face_interiors_eight_transverse_columns',
                physical_accuracy_verified=False)


def discover_features_with_audit(points_axis_mm, **kwargs):
    points=np.asarray(points_axis_mm,dtype=float)
    kwargs.setdefault('policy',FeaturePolicyV6())
    extracted,audit=_extract(points,**kwargs)
    planes={f['region_id']:f for f in extracted if f['type']=='plane_offset'}
    output=[];count=0
    height_updates=0
    unsupported_pairs=[]
    feature_policy=kwargs.get('policy') or FeaturePolicyV6()
    for feature in extracted:
        updated=dict(feature)
        refs=feature.get('reference_regions')
        if feature['type']=='parallel_plane_spacing' and refs and len(refs)==2:
            if refs[0] in planes and refs[1] in planes:
                try:
                    updated['measurement_reference']=reference_from_planes(planes[refs[0]],planes[refs[1]],points)
                    count+=1
                except ValueError as error:
                    updated['measurement_reference_status']='unavailable_parent_geometry'
                    unsupported_pairs.append(dict(region_id=feature['region_id'],reason=str(error)))
        if feature['type']=='prism_height' and feature['status']=='unavailable':
            peers=[f for f in extracted if f['region_id']==feature['region_id']]
            height=_prism_height_evidence(feature,peers,points,feature_policy)
            if height is not None:
                updated.update(value_mm=height['height_mm'],status='available',reason=None,
                               endpoint_method=height['method'],endpoint_support_v6=height)
                height_updates+=1
        output.append(updated)
    audit=dict(audit)
    audit['reference_contract']=dict(version='V6',updated_pairs=count,
                                    raw_union_support_preserved=True,distributed_height_updates=height_updates,unsupported_pairs=unsupported_pairs)
    return output,audit


def validated_reference_anchor(feature,features,points):
    reference=feature.get('measurement_reference')
    if reference is None:
        ids=feature.get('support',{}).get('point_indices',[])
        return points[ids].mean(axis=0) if len(ids) else None
    if feature.get('type')!='parallel_plane_spacing':
        raise ValueError('pair reference on unsupported feature type')
    refs=feature.get('reference_regions')
    planes={f['region_id']:f for f in features if f['type']=='plane_offset'}
    if not refs or len(refs)!=2 or any(r not in planes for r in refs):
        raise ValueError('reference parent faces missing')
    # Independent verifier: recompute each face from its observed support, not
    # from the extractor's stored centroids or reference-construction helper.
    if (reference.get('schema')!='R26_PAIR_REFERENCE_V6'
            or reference.get('method')!='equal_weight_observed_face_centroids'
            or reference.get('weights')!=[.5,.5]
            or len(reference.get('faces',[]))!=2):
        raise ValueError('invalid pair reference contract')
    centers=[];normals=[]
    for region,stored in zip(refs,reference['faces']):
        face=planes[region]
        ids=np.asarray(face['support']['point_indices'],dtype=np.int64)
        if not len(ids) or ids.min()<0 or ids.max()>=len(points):
            raise ValueError('invalid reference support indices')
        digest=hashlib.sha256(np.asarray(ids,dtype='<i8').tobytes()).hexdigest()
        if (stored.get('region_id')!=region or stored.get('support_points')!=len(ids)
                or stored.get('support_sha256')!=digest):
            raise ValueError('reference source support mismatch')
        center=np.add.reduce(points[ids],axis=0)/len(ids)
        normal=np.asarray(face['direction'],float)
        normal=normal/np.linalg.norm(normal)
        if (not np.array_equal(center,np.asarray(stored.get('centroid_mm')))
                or not np.array_equal(normal,np.asarray(stored.get('normal')))):
            raise ValueError('reference geometry mismatch')
        centers.append(center);normals.append(normal)
    if normals[0]@normals[1]<.995:
        raise ValueError('reference faces are not consistently parallel')
    expected_position=.5*centers[0]+.5*centers[1]
    if not np.array_equal(expected_position,np.asarray(reference.get('position_mm'))):
        raise ValueError('reference position mismatch')
    return expected_position
