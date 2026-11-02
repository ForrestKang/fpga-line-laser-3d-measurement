"""Private display snapshot: owned immutable arrays, unchanged LINE_8 pixels.

Only sampling/painting uses this representation. No measurement, PL decision,
pixel extraction, provenance, or persistent format is changed.
"""
from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class CompactPLAnnotations:
    metadata: object
    groups: tuple

    def __post_init__(self):
        from .display_pfo3_r25 import PLDisplayAnnotations, MAX_ANNOTATIONS
        if type(self.metadata) is not PLDisplayAnnotations or len(self.groups)!=3:
            raise TypeError('validated PL annotation metadata and three groups required')
        if self.metadata.point_count or self.point_count>MAX_ANNOTATIONS:
            raise ValueError('bounded compact PL annotations required')
        for index,group in enumerate(self.groups):
            if (not isinstance(group,np.ndarray) or group.dtype!=np.int64
                    or group.ndim!=2 or group.shape[1]!=3 or not group.flags.c_contiguous):
                raise TypeError('contiguous int64 annotation coordinates required')
            if len(group) and (np.any(group[:,0]<0) or np.any(group[:,0]>=752*4096)
                    or np.any(group[:,1]<0) or np.any(group[:,1]>=480)
                    or np.any(group[:,2]<0) or np.any(group[:,2]>1)):
                raise ValueError('native Q12,row,start-marker annotation required')
            if index<2 and np.any(group[:,2]):
                raise ValueError('accepted observations require computed PL centers')
        # Even a caller-owned or empty array must not remain mutable through an
        # alias. bytes-backed views cannot be made writeable with setflags.
        object.__setattr__(self,'groups',tuple(np.frombuffer(group.tobytes(),dtype=np.int64)
            .reshape(-1,3) for group in self.groups))

    def __getattr__(self,name):
        return getattr(self.metadata,name)

    @property
    def point_count(self):
        return sum(len(group) for group in self.groups)


def draw_compact_markers(canvas,snapshot):
    from .display_marker_native15c import optional_markers
    kernel=optional_markers()
    if kernel is not None:
        from .display_pfo3_r25 import MEASUREMENT_BGR,PREVIEW_BGR,REJECTED_BGR
        from structured_light.scan3d.dashboard import CAMERA_IMAGE
        if type(snapshot) is not CompactPLAnnotations:
            raise TypeError('owned compact annotations required')
        return kernel.draw(canvas,snapshot.groups,CAMERA_IMAGE[:2],
            (MEASUREMENT_BGR,PREVIEW_BGR,REJECTED_BGR))
    return draw_compact_markers_reference(canvas,snapshot)


def draw_compact_markers_reference(canvas,snapshot):
    from .display_markers_r25 import _CIRCLE,_CROSS
    from .display_pfo3_r25 import MEASUREMENT_BGR,PREVIEW_BGR,REJECTED_BGR
    from structured_light.scan3d.dashboard import CAMERA_IMAGE,CANVAS_H,CANVAS_W
    if (type(snapshot) is not CompactPLAnnotations or not isinstance(canvas,np.ndarray)
            or canvas.dtype!=np.uint8 or canvas.shape!=(CANVAS_H,CANVAS_W,3)
            or not canvas.flags.c_contiguous or not canvas.flags.writeable):
        raise ValueError('owned compact annotations and writable contiguous BGR canvas required')
    cx,cy,_,_=CAMERA_IMAGE
    for group,color in zip(snapshot.groups,(MEASUREMENT_BGR,PREVIEW_BGR,REJECTED_BGR)):
        if not len(group):continue
        centers=np.column_stack((np.minimum(group[:,0]//4096,751)+cx,group[:,1]+cy))
        # Within each group every marker has one colour. Group order remains
        # measurement -> preview -> rejected, including all overlap pixels.
        for start,offsets in ((0,_CIRCLE),(1,_CROSS)):
            selected=centers[group[:,2]==start]
            if not len(selected):continue
            pixels=selected[:,None,:]+offsets[None,:,:]
            xs=pixels[:,:,0].ravel();ys=pixels[:,:,1].ravel()
            inside=(xs>=0)&(xs<CANVAS_W)&(ys>=0)&(ys<CANVAS_H)
            canvas[ys[inside],xs[inside]]=color
    return 'NUMPY_EXACT_LINE8_COMPACT'
