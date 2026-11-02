"""Opt-in display-only batch raster for the existing validated annotations.

Equivalent to circle(radius=1, thickness=-1, LINE_8) and
drawMarker(MARKER_TILTED_CROSS, markerSize=4, thickness=1, LINE_8).
The renderer selects this path only for sufficiently large marker sets.
Annotation construction, PL decisions,
sampling, provenance and legality checks stay in display_pfo3_r25.
"""
import numpy as np

from .display_pfo3_r25 import (PLDisplayAnnotations,MEASUREMENT_BGR,PREVIEW_BGR,
                               REJECTED_BGR,annotation_snapshot)
from structured_light.scan3d.dashboard import CAMERA_IMAGE,CANVAS_H,CANVAS_W


# Integer LINE_8 footprints. Cross endpoint offset is markerSize // 2.
_CIRCLE=np.array(((0,-1),(-1,0),(0,0),(1,0),(0,1)),dtype=np.int32)
_CROSS=np.array(((-2,-2),(-1,-1),(0,0),(1,1),(2,2),
                 (-2,2),(-1,1),(1,-1),(2,-2)),dtype=np.int32)
_CIRCLE.setflags(write=False);_CROSS.setflags(write=False)


def draw_pl_markers(canvas,snapshot):
    """Paint existing markers in green→cyan→red order, returning same canvas.

    Edges are clipped to the canvas, as OpenCV does, not to CAMERA_IMAGE: a
    run-start cross on the image boundary intentionally extends into its border.
    Input is the existing validated, frozen annotation value. Deferred values
    must be materialized by the renderer through its existing entry point.
    """
    if (not isinstance(canvas,np.ndarray) or canvas.dtype!=np.uint8
            or canvas.shape!=(CANVAS_H,CANVAS_W,3) or not canvas.flags.c_contiguous
            or not canvas.flags.writeable):
        raise ValueError('writable contiguous1280x720 BGR uint8 canvas required')
    snapshot=annotation_snapshot(snapshot)
    if type(snapshot) is not PLDisplayAnnotations:
        raise TypeError('materialized PLDisplayAnnotations required')
    cx,cy,_,_=CAMERA_IMAGE
    for group,color in ((snapshot.measurement,MEASUREMENT_BGR),
                        (snapshot.preview,PREVIEW_BGR),(snapshot.rejected,REJECTED_BGR)):
        if not group:continue
        points=np.asarray(group,dtype=np.int32)
        centers=np.column_stack((np.minimum(points[:,0]//4096,751)+cx,points[:,1]+cy))
        for start,offsets in ((0,_CIRCLE),(1,_CROSS)):
            selected=centers[points[:,2]==start]
            if not len(selected):continue
            pixels=selected[:,None,:]+offsets[None,:,:]
            xs=pixels[:,:,0].ravel();ys=pixels[:,:,1].ravel()
            inside=(xs>=0)&(xs<CANVAS_W)&(ys>=0)&(ys<CANVAS_H)
            canvas[ys[inside],xs[inside]]=color
    return canvas
