"""Reproducible R25 software-only evidence; never reads calibration/CAD/truth."""
from collections import Counter
from dataclasses import asdict, fields
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zlib

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'runtime'),str(ROOT/'runtime/structured_light_system_algorithm_v1/src/python')]
from candidate.v3_reference_r25 import DEVELOPMENT_GRID, V3Config, extract_v3
from reference_candidate.multi_stripe import MultiPolicy, extract_profiles as old_extract
from structured_light.stripe_v1.types import StripeConfig


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint_result(result):
    digest=hashlib.sha256()
    for key,value in result.candidates.as_dict().items():
        digest.update(key.encode()+b'\0')
        digest.update(np.asarray(value,dtype='<i8').tobytes())
    digest.update(np.asarray(result.row_data,dtype='<i8').tobytes())
    digest.update(json.dumps(asdict(result.frame),sort_keys=True,separators=(',',':')).encode())
    return digest.hexdigest()


def saved_frames(directory):
    manifest=json.loads((directory/'INPUT_MANIFEST.json').read_text())
    encoded=(directory/'raw.gray8.zblocks').read_bytes()
    frame_bytes=(directory/'frames.jsonl').read_bytes()
    assert sha(encoded)==manifest['encoded_sha256']
    assert sha(frame_bytes)==manifest['frames_sha256']
    index=json.loads((directory/'RAW_BLOCK_INDEX.json').read_text())
    metadata=[json.loads(line) for line in frame_bytes.splitlines()]
    assert len(index)==len(metadata)==manifest['frames']==240
    whole=hashlib.sha256()
    for i,(block,frame) in enumerate(zip(index,metadata)):
        payload=encoded[block['offset']:block['offset']+block['bytes']]
        dec=zlib.decompressobj()
        raw=dec.decompress(payload,752*480+1)
        assert len(raw)==752*480 and dec.eof and not dec.unused_data and not dec.unconsumed_tail
        assert sha(raw)==frame['raw_sha256'] and frame['frame_id']==i
        whole.update(raw)
        yield i,raw,manifest
    assert whole.hexdigest()==manifest['raw_sha256']


def main():
    output=ROOT/'evidence/algorithm_r25'
    output.mkdir(parents=True,exist_ok=True)
    temporary=Path('D:/Workspace/CodexTemp/r25_algorithm')
    temporary.mkdir(parents=True,exist_ok=True)
    environment=dict(os.environ,TEMP=str(temporary),TMP=str(temporary))
    command=[sys.executable,'-m','unittest','discover','-s','tests/r25','-p','test_v3_reference.py','-v']
    check=subprocess.run(command,cwd=ROOT,env=environment,capture_output=True,text=True)
    (output/'unittest.log').write_text(check.stdout+check.stderr,encoding='utf-8')
    report={'evidence_type':'HOST_INTEGER_SOFTWARE_AND_SIMULATED_SAVED_IMAGES_ONLY',
            'extractor_config':asdict(V3Config()),'config_fingerprint':V3Config().fingerprint,
            'commands':[{'argv':command,'exit_code':check.returncode,'log':'unittest.log'}],
            'gates':{'software_tests':check.returncode==0,'saved_input_hashes':False,
                     'rtl_exact_match':'NOT_RUN_BY_ALGORITHM_WORKER','actual_pl_replay':'NOT_RUN',
                     'resource_timing':'NOT_RUN','physical_metrology':'NOT_RUN'},
            'caveats':['T05 and T06 manifests explicitly say SIMULATED_IMAGES_ONLY.',
                       'Old/new pixel-center deltas measure algorithm changes, not accuracy against truth.',
                       'Host timings are Python-oracle diagnostics, not ARM/PL performance.',
                       'No calibration/CAD/truth data is passed into either extractor.',
                       'NativeImageReplay import was not used: its scan3d import requires unavailable cv2.',
                       'No dependency installation, hardware access or board writes were performed.'],
            'old_config':asdict(StripeConfig(fixed_threshold=60,min_width=1,max_width=32,roi=(0,0,752,480))),
            'old_policy':asdict(MultiPolicy()),'datasets':{},'development_grid':[],
            'source_sha256':{str(path.relative_to(ROOT)):sha(path.read_bytes()) for path in (
                ROOT/'runtime/candidate/v3_reference_r25.py',ROOT/'docs/V3_INTEGER_CONTRACT.md',
                ROOT/'tests/r25/test_v3_reference.py')}}
    assert check.returncode==0, 'software test gate failed; see log'
    config=StripeConfig(fixed_threshold=60,min_width=1,max_width=32,roi=(0,0,752,480))
    for target in ('T05','T06'):
        totals=Counter(); statuses=Counter(); reasons=Counter(); frames=[]; matched_deltas=[]
        source_hash=hashlib.sha256(); output_hash=hashlib.sha256()
        for i,raw,manifest in saved_frames(ROOT/'inputs'/target):
            image=np.frombuffer(raw,np.uint8).reshape(480,752)
            started=time.perf_counter();old=old_extract(image,config);old_s=time.perf_counter()-started
            started=time.perf_counter();new=extract_v3(image);new_s=time.perf_counter()-started
            c=new.candidates
            source_hash.update(raw);output_hash.update(bytes.fromhex(fingerprint_result(new)))
            totals.update(raw=new.frame.raw_count,local=new.frame.local_count,
                          preview=new.frame.preview_count,measurement=new.frame.measurement_count,
                          old_raw=len(old.rows),old_accepted=int(old.accepted.sum()),
                          preview_frames=int(new.frame.preview_usable),measurement_frames=int(new.frame.measurement_usable),
                          overflow_rows=new.frame.overflow_rows)
            statuses[str(new.frame.measure_status)]+=1
            reasons.update(str(v) for v in c.local_reasons if v)
            # An explicit image-only comparison metric: mutual nearest within3pixels,
            # same row, only accepted old and measurement-qualified new candidates.
            paired=0;delta_sum=0
            for y in range(480):
                oi=np.flatnonzero(old.accepted & (old.rows==y))
                ni=np.flatnonzero(c.measurement_usable & (c.row==y))
                if not len(oi) or not len(ni):continue
                distance=np.abs(old.center_q12[oi,None]-c.center_q12[ni][None,:])
                left=distance.argmin(axis=1);right=distance.argmin(axis=0)
                for a,b in enumerate(left):
                    if right[b]==a and distance[a,b]<=3*4096:
                        delta=int(c.center_q12[ni[b]]-old.center_q12[oi[a]])
                        matched_deltas.append(delta);paired+=1;delta_sum+=delta
            frames.append({'frame_id':i,'raw_sha256':sha(raw),'result_sha256':fingerprint_result(new),
                           'old_raw':len(old.rows),'old_accepted':int(old.accepted.sum()),
                           'v3_frame':asdict(new.frame),'matched_3px_count':paired,
                           'matched_center_delta_q12_sum':delta_sum,
                           'old_host_wall_s':old_s,'v3_host_wall_s':new_s})
            if i%60==0:print(target,'frame',i,'old',int(old.accepted.sum()),'new',new.frame.measurement_count,flush=True)
            if i==0:
                for development in DEVELOPMENT_GRID:
                    trial=extract_v3(image,development)
                    report['development_grid'].append({'target':target,'frame_id':0,
                        'config':asdict(development),'fingerprint':development.fingerprint,
                        'frame':asdict(trial.frame)})
        delta=np.asarray(matched_deltas,np.int64)
        report['datasets'][target]={'evidence_type':manifest['evidence_type'],
            'encoded_sha256':manifest['encoded_sha256'],'raw_sha256':source_hash.hexdigest(),
            'frames_sha256':manifest['frames_sha256'],'result_stream_sha256':output_hash.hexdigest(),
            'frames':240,'totals':dict(totals),'measurement_status_counts':dict(statuses),
            'local_rejection_mask_counts':dict(reasons),'matched_centers':len(delta),
            'absolute_center_delta_q12_median':float(np.median(np.abs(delta))) if len(delta) else None,
            'absolute_center_delta_q12_p95':float(np.percentile(np.abs(delta),95)) if len(delta) else None,
            'per_frame_file':target+'_comparison.jsonl'}
        with (output/(target+'_comparison.jsonl')).open('w',encoding='utf-8') as stream:
            for frame in frames:stream.write(json.dumps(frame,sort_keys=True)+'\n')
    report['gates']['saved_input_hashes']=True
    (output/'evidence.json').write_text(json.dumps(report,indent=2,sort_keys=True),encoding='utf-8')
    print(json.dumps({'software_tests':True,'saved_input_hashes':True,'datasets':
                     {k:v['totals'] for k,v in report['datasets'].items()}},indent=2),flush=True)


if __name__=='__main__':main()
