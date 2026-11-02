"""Replay images and scan-only metadata through the production numerical components."""
from pathlib import Path
import argparse,hashlib,json,time
import numpy as np
from .estimator import VirtualScanEstimator
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def safe(value):
 if isinstance(value,np.ndarray):return value.tolist()
 if isinstance(value,np.generic):return value.item()
 raise TypeError(type(value).__name__)
def write_ply(path,points):
 with Path(path).open('x',encoding='ascii') as f:
  f.write('ply\nformat ascii 1.0\ncomment SIMULATION_ONLY reconstructed from camera images\nelement vertex %d\nproperty float x\nproperty float y\nproperty float z\nend_header\n'%len(points));np.savetxt(f,points,fmt='%.7f')
def main():
 p=argparse.ArgumentParser();p.add_argument('--dataset',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--target');a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 c=json.loads((a.dataset/'calibration.json').read_text());runs=[]
 paths=[x for x in sorted(a.dataset.iterdir()) if x.is_dir() and (x/'GENERATION.json').exists() and (not a.target or x.name==a.target)]
 for data in paths:
  generation=json.loads((data/'GENERATION.json').read_text());assert sha(data/'raw.gray8')==generation['raw_sha256'];out=a.output/data.name;out.mkdir();est=VirtualScanEstimator(c);records=[];start=time.perf_counter()
  with (data/'raw.gray8').open('rb') as raw,(out/'profiles.jsonl').open('x',encoding='utf8') as log:
   for line in (data/'frames.jsonl').read_text().splitlines():
    row=json.loads(line);raw.seek(row['raw_offset']);payload=raw.read(row['raw_bytes']);assert hashlib.sha256(payload).hexdigest()==row['raw_sha256']
    metadata={k:row[k] for k in ('frame_id','timestamp_s','angle_rad','calibration_id')}
    t=time.perf_counter();profile,cloud=est.process(np.frombuffer(payload,np.uint8).reshape(480,752),metadata);elapsed=time.perf_counter()-t
    v=dict(profile.summary(),processing_s=elapsed,raw_sha256=row['raw_sha256'],cloud_points=len(cloud),center_q12=profile.center_q12.tolist(),accepted=profile.stripe_accepted.tolist());log.write(json.dumps(v)+'\n');records.append(v)
  compute_s=time.perf_counter()-start;t=time.perf_counter();features=est.finalize_features();feature_s=time.perf_counter()-t;cloud=est.get_cloud();np.savez_compressed(out/'cloud.npz',points=cloud);write_ply(out/'cloud.ply',cloud)
  (out/'features.json').write_text(json.dumps(features,indent=2,default=safe),encoding='utf8')
  v=dict(evidence_type='SIMULATION_ONLY',physical_validation=False,target_label_for_report_only=data.name,frames=len(records),valid_profiles=sum(x['valid_point_count']>=3 for x in records),observed_cloud_points=len(cloud),accepted_stripe_rows=sum(x['stripe_rows'] for x in records),processing_s=compute_s,PC_processing_hz=len(records)/compute_s,feature_processing_s=feature_s,feature_count=len(features),calibration_sha256=sha(a.dataset/'calibration.json'),raw_sha256=generation['raw_sha256'],features_sha256=sha(out/'features.json'),cloud_sha256=sha(out/'cloud.npz'),source_sha256={str(x.relative_to(ROOT)):sha(x) for x in (ROOT/'virtual_scan').glob('*.py')})
  (out/'RECONSTRUCTION.json').write_text(json.dumps(v,indent=2),encoding='utf8');runs.append(v);print(json.dumps(v),flush=True)
 (a.output/'RECONSTRUCTION_ALL.json').write_text(json.dumps(dict(passed=all(x['observed_cloud_points']>0 for x in runs),runs=runs,physical_validation=False,PC_fps_is_not_board_fps=True),indent=2),encoding='utf8')
if __name__=='__main__':main()
