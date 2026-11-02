"""PS-only image observations for multiple visible stripes on one sensor row.

This is a new variable-length observation contract, not a deployed PL packet.
All accepted centers are the original threshold-run Q12 centroids. No CAD,
shape labels, geometry, nominal dimensions or surface interpolation are used.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import numpy as np
from structured_light.stripe_v1.candidates import extract_candidates
from structured_light.stripe_v1.types import StripeConfig


@dataclass(frozen=True)
class MultiPolicy:
    min_contrast_dn: int = 20
    min_edge_drop_dn: int = 8
    split_peak_drop_dn: int = 8
    max_branch_step_px: float = 6.
    max_direction_residual_px: float = 1.5
    minimum_branch_rows: int = 3

    def __post_init__(self):
        for name in ('min_contrast_dn','min_edge_drop_dn','split_peak_drop_dn'):
            if type(getattr(self,name)) is not int or not 1 <= getattr(self,name) <=255:
                raise ValueError(name+' must be an integer in 1..255')
        if not np.isfinite(self.max_branch_step_px) or self.max_branch_step_px<=0:
            raise ValueError('max_branch_step_px must be positive finite')
        if not np.isfinite(self.max_direction_residual_px) or self.max_direction_residual_px<=0:
            raise ValueError('max_direction_residual_px must be positive finite')
        if type(self.minimum_branch_rows) is not int or self.minimum_branch_rows<3:
            raise ValueError('minimum_branch_rows must be at least three')


@dataclass(frozen=True)
class MultiProfile:
    rows: np.ndarray
    center_q12: np.ndarray
    accepted: np.ndarray
    reasons: tuple[str | None,...]
    branch_ids: np.ndarray
    quality: dict[str,np.ndarray]
    stats: dict
    source: str = 'PS_IMAGE_MULTIRUN_V1_NOT_DEPLOYED_PL'


def _peaks(values):
    """One index per strict local maximum/flat maximum, including no endpoints."""
    # Quantized Gaussian tops can be two identical pixels; group plateaus.
    values=np.asarray(values,int)
    edges=np.r_[0,np.flatnonzero(np.diff(values)!=0)+1,len(values)]
    result=[]
    for a,b in zip(edges[:-1],edges[1:]):
        if a>0 and b<len(values) and values[a]>values[a-1] and values[b-1]>values[b]:
            result.append((int(a),int(b-1)))
    return result


def extract_profiles(gray: np.ndarray, stripe_config: StripeConfig,
                     policy: MultiPolicy | None = None) -> MultiProfile:
    """Return every candidate plus explicit rejection, with repeated sensor rows.

    A branch must have at least three consecutive image rows. Three-row links
    must have a unique consistent direction; crossings/forks are not bridged.
    Saturation, truncated/broad/flat runs and resolved multi-peak runs are rejected.
    A single unresolved merged peak cannot be disambiguated from gray8 alone and
    remains a documented optical limitation, never inferred from a shape model.
    """
    p=policy or MultiPolicy()
    if not isinstance(p,MultiPolicy): raise ValueError('policy must be MultiPolicy')
    frame=extract_candidates(gray,stripe_config)
    image=np.asarray(gray)
    x0,_,x1,_=stripe_config.roi or (0,0,frame.width,frame.height)
    all_candidates=[]; reasons=[]; contrasts=[]; drops=[]; peak_counts=[]
    row_ids=[]
    for row in frame.rows:
        indices=[]
        for c in row.candidates:
            idx=len(all_candidates);indices.append(idx);all_candidates.append(c)
            reason=c.reasons[0] if c.reasons else None
            if row.overflow: reason='candidate_overflow'
            values=image[row.row,c.start:c.end+1].astype(int)
            peaks=_peaks(values)
            contrast=0;edge_drop=0
            if c.start<=x0 or c.end>=x1-1:
                reason=reason or 'background_window_truncated'
            else:
                # Use actual immediate flank pixels, never a fitted surface.
                # A neighbouring bright run can raise this conservative local
                # background estimate; no pixels are synthesized or replaced.
                left=image[row.row,max(x0,c.start-3):c.start]
                right=image[row.row,c.end+1:min(x1,c.end+4)]
                background=max(float(np.median(left)),float(np.median(right)))
                contrast=float(values.max())-background
                edge_drop=int(min(values.max()-values[0],values.max()-values[-1]))
                if contrast<p.min_contrast_dn: reason=reason or 'ridge_contrast'
                if len(values)<3 or edge_drop<p.min_edge_drop_dn or not peaks:
                    reason=reason or 'ridge_shape'
            # Never use the centroid between two optically resolved ridges.
            for peak_a,peak_b in zip(peaks[:-1],peaks[1:]):
                saddle=int(values[peak_a[1]:peak_b[0]+1].min())
                smaller_peak=min(int(values[peak_a[0]]),int(values[peak_b[0]]))
                if smaller_peak-saddle>=p.split_peak_drop_dn:
                    reason='merged_peak_ambiguous'
                    break
            reasons.append(reason);contrasts.append(contrast);drops.append(edge_drop);peak_counts.append(len(peaks))
        row_ids.append(indices)
    n=len(all_candidates)
    parent=np.arange(n)
    def find(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=int(parent[i])
        return i
    # A wider step alone would swap nearby parallel sloping branches. Require
    # a unique constant-direction three-row hypothesis before making any link.
    # The bounded second difference is in original Q12 image coordinates.
    triples=[];ambiguous=set()
    step=p.max_branch_step_px*4096
    residual=p.max_direction_residual_px*4096
    for before,middle,after in zip(row_ids[:-2],row_ids[1:-1],row_ids[2:]):
        for j in middle:
            if reasons[j] is not None:continue
            c=all_candidates[j].center_q12
            left=[i for i in before if reasons[i] is None and abs(all_candidates[i].center_q12-c)<=step]
            right=[k for k in after if reasons[k] is None and abs(all_candidates[k].center_q12-c)<=step]
            options=[(i,j,k) for i in left for k in right
                     if abs(all_candidates[k].center_q12-2*c+all_candidates[i].center_q12)<=residual]
            if len(options)>1:ambiguous.add(j)
            elif len(options)==1:triples.append(options[0])
    triples=[t for t in triples if not any(i in ambiguous for i in t)]
    forward={};backward={}
    for a,b,c in triples:
        for i,j in ((a,b),(b,c)):
            forward.setdefault(i,set()).add(j);backward.setdefault(j,set()).add(i)
    ambiguous.update(i for i,v in forward.items() if len(v)>1)
    ambiguous.update(i for i,v in backward.items() if len(v)>1)
    for i in ambiguous:reasons[i]='ambiguous_branch_direction'
    for a,b,c in triples:
        if any(i in ambiguous for i in (a,b,c)):continue
        parent[find(b)]=find(a);parent[find(c)]=find(a)
    branches=np.array([find(i) if reasons[i] is None else -1 for i in range(n)],int)
    counts=Counter(branches[branches>=0].tolist())
    for i in range(n):
        if reasons[i] is None and counts[int(branches[i])]<p.minimum_branch_rows:
            reasons[i]='insufficient_branch_support'
    accepted=np.array([reason is None for reason in reasons],bool)
    def ro(x,dtype=None):
        a=np.asarray(x,dtype=dtype);a.setflags(write=False);return a
    return MultiProfile(ro([c.row for c in all_candidates],int),
        ro([c.center_q12 for c in all_candidates],np.int64),ro(accepted),tuple(reasons),ro(branches),
        {'width_px':ro([c.width for c in all_candidates],int),'contrast_dn':ro(contrasts,float),
         'edge_drop_dn':ro(drops,int),'peak_count':ro(peak_counts,int),
         'start':ro([c.start for c in all_candidates],int),'end':ro([c.end for c in all_candidates],int)},
        {'candidate_count':n,'accepted':int(accepted.sum()),'rejection_counts':dict(Counter(r for r in reasons if r)),
         'multiple_accepted_rows':sum(np.count_nonzero(accepted[ids])>1 for ids in row_ids),
         'center_policy':'original_threshold_run_Q12','CAD_truth_reads':0,
         'branch_policy':'unique_three_row_direction_v2','branch_policy_parameters':asdict(p)})


def supplement_mask(profile: MultiProfile, primary_centers: np.ndarray,
                    primary_accepted: np.ndarray, tolerance_q12: int = 1) -> np.ndarray:
    """Avoid counting the same original run twice when preserving primary input."""
    if type(tolerance_q12) is not int or tolerance_q12<0:raise ValueError('nonnegative integer tolerance required')
    centers=np.asarray(primary_centers);accepted=np.asarray(primary_accepted)
    if centers.ndim!=1 or accepted.shape!=centers.shape:raise ValueError('primary arrays must be one-dimensional and aligned')
    if len(profile.rows) and profile.rows.max()>=len(centers):raise ValueError('primary rows incomplete')
    duplicate=accepted[profile.rows] & (np.abs(centers[profile.rows]-profile.center_q12)<=tolerance_q12)
    return profile.accepted & ~duplicate
