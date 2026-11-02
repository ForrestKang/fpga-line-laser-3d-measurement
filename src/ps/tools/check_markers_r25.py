"""CPU-only OpenCV-versus-batch full-canvas QA on this exact runtime."""
from pathlib import Path
import argparse,hashlib,json,sys,time
ROOT=Path(__file__).resolve().parents[1]
for path in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',
             ROOT/'structured_light_system_geometry_v1/src/python'):sys.path.insert(0,str(path))
import cv2,numpy as np
from candidate.display_pfo3_r25 import PLDisplayAnnotations,MEASUREMENT_BGR,PREVIEW_BGR,REJECTED_BGR
from candidate.display_markers_r25 import draw_pl_markers
from structured_light.scan3d.dashboard import CAMERA_IMAGE

def old(canvas,snapshot):
    cx,cy,_,_=CAMERA_IMAGE
    for group,color in ((snapshot.measurement,MEASUREMENT_BGR),(snapshot.preview,PREVIEW_BGR),(snapshot.rejected,REJECTED_BGR)):
        for xq,y,start in group:
            x=min(751,xq//4096)
            if start:cv2.drawMarker(canvas,(cx+x,cy+y),color,cv2.MARKER_TILTED_CROSS,4,1,cv2.LINE_8)
            else:cv2.circle(canvas,(cx+x,cy+y),1,color,-1,cv2.LINE_8)
    return canvas

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    cv2.setNumThreads(1);rng=np.random.default_rng(250722);cases=[]
    for count in (0,1,24,128,312,2400):
        points=[]
        for i in range(count):
            # Repeat boundary and same-pixel markers, then random Q12 centers.
            x,y=((0,0),(751,479),(0,479),(751,0),(375,240),(375,240))[i%6] if i<18 else (int(rng.integers(0,752)),int(rng.integers(0,480)))
            points.append((x*4096+int(rng.integers(0,4096)),y,int(i%2)))
        groups=[[],[],[]]
        for i,point in enumerate(points):groups[i%3].append((point[0],point[1],point[2] if i%3==2 else 0))
        snapshot=PLDisplayAnnotations(0,'MOCK_ONLY_MARKER_QA','NO_GEOMETRY',False,
            *(tuple(group) for group in groups),count,count,len(groups[0]),len(groups[1]),True,True,'good','good',0,0)
        before=rng.integers(0,256,(720,1280,3),np.uint8)
        expected=old(before.copy(),snapshot);actual=draw_pl_markers(before.copy(),snapshot)
        if not np.array_equal(expected,actual):raise AssertionError('full canvas marker mismatch at '+str(count))
        times={}
        for label,call in (('opencv',old),('batch',draw_pl_markers)):
            samples=[]
            for _ in range(5):
                canvas=before.copy();start=time.perf_counter();call(canvas,snapshot);samples.append(time.perf_counter()-start)
            times[label]=dict(median_ms=float(np.median(samples))*1000,maximum_ms=max(samples)*1000)
        cases.append(dict(points=count,full_canvas_pixels_equal=True,timing=times))
    result=dict(schema='R25_MARKER_RASTER_EXACT_RUNTIME_QA',passed=True,hardware_access=False,
        cv2_version=cv2.__version__,numpy_version=np.__version__,cases=cases,
        source_sha256=hashlib.sha256((ROOT/'candidate/display_markers_r25.py').read_bytes()).hexdigest(),
        scope='full1280x720 canvas pixel equivalence and marker-only CPU timing, no FPGA/HDMI/rate/metrology evidence')
    with args.output.open('x',encoding='utf8') as f:json.dump(result,f,indent=2)
    print(json.dumps(result))

if __name__=='__main__':main()
