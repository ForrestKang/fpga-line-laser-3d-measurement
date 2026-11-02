"""Generate immutable simulation images/truth; estimator never imported here."""
from pathlib import Path
import argparse,hashlib,json,math,time,sys
import cv2,numpy as np
from .renderer import default_config,load_target,render_frame
from .mesh_helpers import fixture_transform
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--frames',type=int,default=240);p.add_argument('--scale',type=float,default=1.);p.add_argument('--laser-offset-mm',type=float,default=0.);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 c=default_config();c['laser_plane_world'][3]-=a.laser_offset_mm;c['laser_plane_camera'][3]-=a.laser_offset_mm;c['laser_origin_world_mm'][0]+=a.laser_offset_mm;c['calibration_id']+='_'+str(a.laser_offset_mm)+'mm';(a.output/'calibration.json').write_text(json.dumps(c,indent=2),encoding='utf8')
 manifests=[]
 for item in json.loads((ROOT/'fixtures/printed_targets_v1/manifest.json').read_text())['targets']:
  name=item['id'];out=a.output/name;out.mkdir();source=ROOT/'fixtures/printed_targets_v1'/(name+'.stl');mesh=load_target(source,transform=fixture_transform(name),scale=a.scale)
  meta=[];truth={};start=time.perf_counter();raw=out/'raw.gray8'
  with raw.open('xb') as f:
   for i in range(a.frames):
    angle=math.tau*i/a.frames;gray,t=render_frame(mesh,angle,c,seed=i);data=gray.tobytes();f.write(data)
    meta.append(dict(frame_id=i,timestamp_s=i/10.,angle_rad=angle,calibration_id=c['calibration_id'],raw_offset=i*752*480,raw_bytes=len(data),raw_sha256=hashlib.sha256(data).hexdigest()))
    truth[str(i)]=dict(visible_points_world_mm=t['visible_points_world_mm'].tolist(),visible_pixels_uv=t['visible_pixels_uv'].tolist(),dual_visible_fraction=t['dual_visible_fraction'],occluded_sample_count=t['occluded_sample_count'])
    if i in (0,a.frames//4,a.frames//2,3*a.frames//4,a.frames-1):cv2.imwrite(str(out/f'frame_{i:04d}.png'),gray)
   f.flush()
  (out/'frames.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in meta),encoding='utf8')
  (out/'truth_EVALUATOR_ONLY.json').write_text(json.dumps(dict(schema='simulation_forward_truth.v1',evidence='SIMULATION_ONLY',target=name,mesh_sha256=sha(source),scale=a.scale,mesh_world_bounds_mm=mesh.bounds.tolist(),frames=truth)),encoding='utf8')
  v=dict(target=name,frames=a.frames,raw_sha256=sha(raw),frame_metadata_sha256=sha(out/'frames.jsonl'),truth_sha256=sha(out/'truth_EVALUATOR_ONLY.json'),generation_s=time.perf_counter()-start,native_resolution=[752,480],evidence='SIMULATION_ONLY')
  (out/'GENERATION.json').write_text(json.dumps(v,indent=2),encoding='utf8');manifests.append(v);print(json.dumps(v),flush=True)
 (a.output/'GENERATION_ALL.json').write_text(json.dumps(dict(passed=True,targets=manifests,renderer_sha256=sha(ROOT/'virtual_scan/renderer.py'),mesh_helpers_sha256=sha(ROOT/'virtual_scan/mesh_helpers.py'),calibration_sha256=sha(a.output/'calibration.json'),physical_validation=False),indent=2),encoding='utf8')
if __name__=='__main__':main()
