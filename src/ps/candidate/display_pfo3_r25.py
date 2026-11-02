"""Bounded display-only PL observations; full source persistence is separate.

No Raw reductions, extraction, branch linking or new depth are performed here.
A source2 packet alone is protocol syntax, not evidence of hardware execution.

R26 presentation changes are display-only:

* static legend/background bands are rasterised once into the reused template;
* per-frame long evidence lines use ``LINE_8`` (optional ``LINE_AA`` through
  ``display_text_aa_dynamic``) so a 350-point PL frame cannot spend tens of
  milliseconds in Hershey anti-aliasing;
* short/repeated lines are served from a bounded exact-pixel line cache;
* marker sampling and immutable PL decisions are unchanged.
"""
from dataclasses import dataclass
import time
import numpy as np
from .pfo3_r25 import STATUS_NAMES
from .display_fast_r26 import TextLineCache
from virtual_scan.display_renderer_r19d import ReusedCanvasRenderer

MAX_ANNOTATIONS=2400
MEASUREMENT_BGR=(80,235,90)
PREVIEW_BGR=(235,205,40)
REJECTED_BGR=(70,70,245)

@dataclass(frozen=True)
class DeferredPLAnnotations:
    """Immutable source retained only by bounded display slots.

    Materialize sampled markers only for frames actually rendered; skipped
    display updates do not spend ARM time converting thousands of points.
    """
    frame:object
    actual_PL_execution:bool
    geometry_evidence_type:str='SIMULATION_ONLY'
    max_points:int=MAX_ANNOTATIONS
    def __post_init__(self):
        from .pfo3_r25 import FinalFrame
        if (type(self.frame) is not FinalFrame or self.frame.records.flags.writeable or self.frame.rows.flags.writeable
                or type(self.actual_PL_execution) is not bool or type(self.max_points) is not int
                or not 1<=self.max_points<=MAX_ANNOTATIONS):raise ValueError('bounded immutable final-frame display source required')
        if self.actual_PL_execution and self.frame.header['source'] not in (1,2):raise ValueError('fixture cannot claim actual PL')
        if type(self.geometry_evidence_type) is not str or not self.geometry_evidence_type.isascii() or len(self.geometry_evidence_type)>96:
            raise ValueError('bounded display evidence label')
    @property
    def frame_id(self):return self.frame.header['frame_id']
    @property
    def point_count(self):return min(self.max_points,len(self.frame.records))
    def materialize(self):
        return annotations_from_frame(self.frame,actual_pl_execution=self.actual_PL_execution,
            geometry_evidence_type=self.geometry_evidence_type,max_points=self.max_points)
    def materialize_compact(self):
        # Private renderer representation: exactly the same sample, without
        # constructing and validating thousands of Python scalar tuples.
        return annotations_from_frame(self.frame,actual_pl_execution=self.actual_PL_execution,
            geometry_evidence_type=self.geometry_evidence_type,max_points=self.max_points,
            _compact=True)


@dataclass(frozen=True)
class PLDisplayAnnotations:
    frame_id:int
    backend:str
    geometry_evidence_type:str
    actual_PL_execution:bool
    measurement:tuple
    preview:tuple
    rejected:tuple
    diagnostic_count:int
    qualified_count:int
    measurement_count:int
    preview_count:int
    measurement_usable:bool
    preview_usable:bool
    measurement_status:str
    preview_status:str
    quality_flags:int
    processing_cycles:int

    @property
    def point_count(self):return len(self.measurement)+len(self.preview)+len(self.rejected)

    def __post_init__(self):
        if self.point_count>MAX_ANNOTATIONS:raise ValueError('at most2400 display annotations')
        for group in (self.measurement,self.preview,self.rejected):
            if type(group) is not tuple:raise TypeError('immutable annotation tuples required')
            for point in group:
                if (type(point) is not tuple or len(point)!=3 or any(type(v) is not int for v in point)
                        or not 0<=point[0]<752*4096 or not 0<=point[1]<480 or point[2] not in (0,1)):
                    raise ValueError('native Q12,row,start-marker annotation required')
        if any(point[2] for group in (self.measurement,self.preview) for point in group):
            raise ValueError('accepted observations require computed PL centers')
        if type(self.actual_PL_execution) is not bool:raise TypeError('explicit hardware evidence flag required')
        for text in (self.backend,self.geometry_evidence_type,self.measurement_status,self.preview_status):
            if type(text) is not str or not text.isascii() or len(text)>96:raise ValueError('bounded ASCII annotation label required')
        for value in (self.frame_id,self.diagnostic_count,self.qualified_count,self.measurement_count,
                      self.preview_count,self.quality_flags,self.processing_cycles):
            if type(value) is not int or value<0:raise ValueError('nonnegative display metadata required')
        if type(self.measurement_usable) is not bool or type(self.preview_usable) is not bool:
            raise TypeError('explicit PL frame-use bits required')


def annotation_snapshot(value):
    from .display_compact_r14 import CompactPLAnnotations
    if type(value) in (DeferredPLAnnotations, CompactPLAnnotations):return value
    if type(value) is not PLDisplayAnnotations:raise TypeError('PLDisplayAnnotations snapshot required')
    # Construction validates immutable nested tuples of builtin scalars. Sharing
    # this frozen value does not share mutable buffers, and avoids validating
    # every point again in submit and render.
    return value


def _sample_groups(records,center_valid,masks,quotas,*,compact=False):
    """Vectorised conversion of the already-selected native annotation rows.

    The quota/order algorithm is the historical round-robin one; only the
    per-point Python field lookup was replaced with bounded fancy indexing.
    """
    groups=[]
    for chosen,quota in zip(masks,quotas):
        if quota==0:
            groups.append(np.empty((0,3),dtype=np.int64) if compact else ())
            continue
        if quota!=len(chosen):
            chosen=chosen[np.arange(quota)*(len(chosen)-1)//max(1,quota-1)]
        cx=np.where(center_valid[chosen],records['center_q12'][chosen].astype(np.int64),
                    records['start'][chosen].astype(np.int64)*4096)
        rows=records['row'][chosen].astype(np.int64)
        starts=(~center_valid[chosen]).astype(np.int64)
        packed=np.column_stack((cx,rows,starts))
        if compact:
            # The compact snapshot validates in bulk and takes bytes ownership.
            groups.append(packed)
        else:
            groups.append(tuple(map(tuple,packed.tolist())))
    return groups


def annotations_from_frame(frame,*,actual_pl_execution=False,geometry_evidence_type='SIMULATION_ONLY',max_points=MAX_ANNOTATIONS,_compact=False):
    """Copy final PL fields into an immutable display-only <=2400point snapshot.

    Colors use PL candidate decisions and corresponding PL frame-use gates.
    Rejected records without a computed center mark their run start, not a
    fabricated center or XYZ. Sampling never changes the frame or saved data.
    """
    if type(max_points) is not int or not 1<=max_points<=MAX_ANNOTATIONS:raise ValueError('annotation limit must be1..2400')
    h=frame.header;a=frame.records
    if (h['width'],h['height'])!=(752,480):raise ValueError('native752x480 PL frame required')
    if actual_pl_execution and h['source'] not in (1,2):raise ValueError('fixture cannot claim actual PL execution')
    if type(actual_pl_execution) is not bool:raise TypeError('explicit actual execution boolean required')
    measure=frame.frame_usable('measurement');preview=frame.frame_usable('preview')
    decisions=a['decisions']>>24
    center_valid=(decisions&1)!=0
    good_measure=((decisions&2)!=0)&measure
    good_preview=((decisions&4)!=0)&preview&~good_measure
    indices=[np.flatnonzero(mask) for mask in (good_measure,good_preview,~(good_measure|good_preview))]
    # Split a bounded point budget between classes; reassign unused quotas so
    # sparse accepted/rejected classes stay visible without exceeding the cap.
    sizes=[len(ids) for ids in indices];quotas=[0,0,0];remaining=min(max_points,sum(sizes))
    # At most three classes: batch complete round-robin rounds, then the final
    # partial round. Exactly the same quotas/order as the previous point loop.
    while remaining:
        active=[i for i in range(3) if quotas[i]<sizes[i]]
        rounds=min(remaining//len(active),min(sizes[i]-quotas[i] for i in active))
        if rounds:
            for i in active:quotas[i]+=rounds
            remaining-=rounds*len(active)
        else:
            for i in active[:remaining]:quotas[i]+=1
            remaining=0
    groups=_sample_groups(a,center_valid,indices,quotas,compact=_compact)
    backend=('ACTUAL_PL_CAMERA_DDR' if h['source']==1 else 'ACTUAL_PL_SAVED_RAW_DDR') if actual_pl_execution else (
        'MOCK_ONLY_PFO3_FIXTURE' if h['source']==3 else 'PL_PACKET_SYNTAX_UNCONFIRMED')
    # Keep the public tuple snapshot and its validation unchanged. Only the
    # trusted Deferred source uses the private packed renderer representation.
    result=PLDisplayAnnotations(int(h['frame_id']),backend,geometry_evidence_type,actual_pl_execution,
        *(((),(),()) if _compact else groups),int(h['diagnostic_count']),int(h['qualified_count']),int(h['measurement_count']),int(h['preview_count']),
        measure,preview,STATUS_NAMES[h['measure_status']],STATUS_NAMES[h['preview_status']],
        int(h['frame_decisions']>>8),int(h['processing_cycles_lo'])+(int(h['processing_cycles_hi'])<<32))
    if _compact:
        from .display_compact_r14 import CompactPLAnnotations
        return CompactPLAnnotations(result,tuple(groups))
    return result


class PFO3RendererR25(ReusedCanvasRenderer):
    """1280x720 BGR canvas with full raw panel, real XYZ panel and PL marks.

    Static PL legend bands are baked once; every frame restores only the bands
    it will overwrite, then paints dynamic text and markers.  The marker loop
    for small sets is kept verbatim so the existing full-canvas marker review
    can substitute exactly one loop and still get pixel equality.
    """
    def __init__(self,config):
        super().__init__(point_raster_manifest=config.get('raster_manifest'),
            projection_raster_manifest=config.get('projection_raster_manifest'))
        self.target=config.get('target','R25 PL FRONTEND')
        self._background=(28,17,7)
        self._annotation_labels=(
            ('MEASUREMENT',(24,650),MEASUREMENT_BGR,.43),
            ('PREVIEW ONLY',(190,650),PREVIEW_BGR,.43),
            ('REJECTED',(370,650),REJECTED_BGR,.43),
            ('x = run start when PL center was not calculated; no depth assigned.',(510,650),(230,230,230),.41),
            ('Full Raw + primary PL stream + PFO3 + XYZ persistence is independent of display sampling.',(24,704),(230,230,230),.43),
        )
        self._text_cache=TextLineCache(max_entries=config.get('display_text_cache_entries',24))
        self._dynamic_line_type=config.get('display_text_aa_dynamic',False)
        self._build_static_template()
        # Bands this renderer overwrites.  Restoring from ``static_source``
        # before drawing makes shrinking strings and per-frame counters
        # ghost-free without a full-canvas memcpy.
        self._static_regions=((slice(0,24),slice(None)),
                              (slice(24,83),slice(240,None)),
                              (slice(96,121),slice(24,None)),
                              (slice(620,720),slice(16,1265)))
        self.static_source=self.template
        self.draw_core_dynamic_text=False
        self.canvas=self.template.copy()
        self._annotation_materialize_ms=0.
        self._text_ms=0.
        self._markers_ms=0.
        self._marker_backend='NONE'
        # One bounded copy preserves the last image including PL marks when
        # render_and_show temporarily borrows an HDMI CMA canvas.
        self._status_canvas=None

    def _draw_pipeline_status(self,canvas,label):
        import cv2
        cv2.rectangle(canvas,(0,689),(1279,719),self._background,-1)
        self._draw_line(canvas,str(label)[:210],(12,709),(100,235,240),scale=.43,cache=False)

    def render_status_only(self,label):
        """Update only status: no camera conversion, projection or markers."""
        if self._status_canvas is None:return None
        self._draw_pipeline_status(self._status_canvas,label)
        return self._status_canvas

    def _build_static_template(self):
        """Bake the PL background/text strips into the reused template once."""
        import cv2
        canvas=self.template
        background=self._background
        cv2.rectangle(canvas,(0,0),(1279,23),background,-1)
        cv2.rectangle(canvas,(240,24),(1279,82),background,-1)
        cv2.rectangle(canvas,(24,96),(780,121),background,-1)
        cv2.rectangle(canvas,(16,620),(1264,719),background,-1)
        for value,position,color,scale in self._annotation_labels:
            cv2.putText(canvas,value,position,cv2.FONT_HERSHEY_SIMPLEX,scale,color,1,cv2.LINE_AA)
        canvas.setflags(write=False)

    def _draw_line(self,canvas,value,position,color=(230,230,230),scale=.43,*,cache=False):
        import cv2
        return self._text_cache.draw(canvas,value,position,font=cv2.FONT_HERSHEY_SIMPLEX,
            scale=scale,color=color,thickness=1,
            line_type=(cv2.LINE_AA if cache or self._dynamic_line_type else cv2.LINE_8),
            cache=cache)

    def render(self,gray,points,*,status,cloud_revision=None,annotations=None):
        import cv2
        from structured_light.scan3d.dashboard import CAMERA_IMAGE
        started=time.perf_counter()
        canvas=super().render(gray,points,status=status,cloud_revision=cloud_revision)
        text_started=time.perf_counter()
        # Short/repeated identity lines are cached exactly; per-frame counters
        # use a non-AA line so long evidence strings do not dominate ARM CPU.
        self._draw_line(canvas,'R25 / '+self.target,(12,17),scale=.48,cache=True)
        # This core line lives outside the PL raw-label fill and was visible in
        # the pre-R26 canvas; keep the same fact while restoring its band.
        self._draw_line(canvas,f'VISIBLE {len(points):,} / FULL OBSERVED {self.full_cloud_points:,}',
                        (810,113),(170,153,132),scale=.43,cache=False)
        if annotations is None:
            # The baked PL legend is not part of the no-observation canvas;
            # clear its band before drawing the two explanatory lines.
            cv2.rectangle(canvas,(16,636),(1264,716),self._background,-1)
            self._draw_line(canvas,'NO PL OBSERVATION SNAPSHOT / INPUT AND XYZ VIEW ONLY',
                            (250,49),(80,180,255),cache=True)
            self._draw_line(canvas,'No PL execution or measurement claim is established by this canvas.',
                            (24,650),cache=True)
            annotation_points=0
            markers_ms=0.;marker_backend='NONE'
            self._annotation_materialize_ms=0.
        else:
            from .display_compact_r14 import CompactPLAnnotations
            snapshot=(annotations if type(annotations) is CompactPLAnnotations
                      else annotation_snapshot(annotations))
            if type(snapshot) is DeferredPLAnnotations:
                materialize_started=time.perf_counter()
                snapshot=snapshot.materialize_compact()
                self._annotation_materialize_ms=(time.perf_counter()-materialize_started)*1000.
            else:
                self._annotation_materialize_ms=0.
            if snapshot.frame_id!=status.frame_number:raise ValueError('display frame/annotation identity mismatch')
            self._draw_line(canvas,snapshot.backend+' / GEOMETRY '+snapshot.geometry_evidence_type
                            +' / PHYSICAL DIMENSIONS UNVERIFIED',(250,49),scale=.43,cache=True)
            self._draw_line(canvas,f'M {snapshot.measurement_status} use={int(snapshot.measurement_usable)}'
                            f' n={snapshot.measurement_count}   P {snapshot.preview_status}'
                            f' use={int(snapshot.preview_usable)} n={snapshot.preview_count}'
                            f'   FLAGS 0x{snapshot.quality_flags:02x}   CYCLES {snapshot.processing_cycles}',
                            (250,73),scale=.43,cache=False)
            marker_started=time.perf_counter()
            from .display_compact_r14 import CompactPLAnnotations,draw_compact_markers
            if type(snapshot) is CompactPLAnnotations:
                marker_backend=draw_compact_markers(canvas,snapshot)
            elif snapshot.point_count>=312:
                from .display_markers_r25 import draw_pl_markers
                draw_pl_markers(canvas,snapshot)
                marker_backend='NUMPY_EXACT_LINE8_BATCH'
            else:
                cx,cy,_,_=CAMERA_IMAGE
                for group,color in ((snapshot.measurement,MEASUREMENT_BGR),(snapshot.preview,PREVIEW_BGR),(snapshot.rejected,REJECTED_BGR)):
                    for xq,y,start in group:
                        x=min(751,xq//4096)
                        if start:cv2.drawMarker(canvas,(cx+x,cy+y),color,cv2.MARKER_TILTED_CROSS,4,1,cv2.LINE_8)
                        else:cv2.circle(canvas,(cx+x,cy+y),1,color,-1,cv2.LINE_8)
                marker_backend='OPENCV_LINE8_SMALL_SET'
            markers_ms=(time.perf_counter()-marker_started)*1000.
            self._draw_line(canvas,f'RAW 752x480 NATIVE / PL MARKS {snapshot.point_count}/{snapshot.diagnostic_count}'
                            ' (DISPLAY SAMPLE)',(24,113),scale=.42,cache=False)
            self._draw_line(canvas,f'FRAME {snapshot.frame_id} / DIAGNOSTICS {snapshot.diagnostic_count}'
                            f' / LOCAL QUALIFIED {snapshot.qualified_count} / VISIBLE XYZ {len(points)}'
                            f' / CLOUD {self.full_cloud_points}',(24,678),scale=.43,cache=False)
            annotation_points=snapshot.point_count
        text_total_ms=(time.perf_counter()-text_started)*1000.
        self._text_ms=max(0.,text_total_ms-markers_ms-self._annotation_materialize_ms)
        self._markers_ms=markers_ms
        self._marker_backend=marker_backend
        self.timings.update(renderer='R25_PL_OBSERVATIONS_NATIVE_RAW_XYZ',
            annotation_points=annotation_points,
            annotation_materialize_ms=self._annotation_materialize_ms,
            text_ms=self._text_ms,markers_ms=self._markers_ms,
            markers_backend=self._marker_backend,
            text_cache=self._text_cache.summary(),
            text_dynamic_line_type=('LINE_AA' if self._dynamic_line_type else 'LINE_8'),
            render_with_annotations_ms=(time.perf_counter()-started)*1000.)
        if status.source and ' | computed ' in status.source:
            self._draw_pipeline_status(canvas,status.source)
            if self._status_canvas is None:self._status_canvas=canvas.copy()
            else:np.copyto(self._status_canvas,canvas)
        return canvas


def renderer_factory(config):return PFO3RendererR25(config)
