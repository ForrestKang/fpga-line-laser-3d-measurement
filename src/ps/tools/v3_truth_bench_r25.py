"""Frozen image-only synthetic evaluation. Truth is passed only to the scorer.

This is not an optical precision test, PL speed test, or CAD reconstruction.
"""
from pathlib import Path
import argparse,hashlib,json,sys
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',ROOT/'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0,str(p))
import numpy as np
from candidate.v3_reference_r25 import V3Config,extract_v3
from candidate.multibranch_fast import extract_profiles
from structured_light.stripe_v1.types import StripeConfig


def images(seed):
    rng=np.random.default_rng(seed);x=np.arange(752)[None,:];y=np.arange(480)[:,None]
    for category in ('clean','gradient','weak','noise','saturated','bright_edge','occlusion'):
        for variant in range(4):
            center=(180+variant*118+rng.uniform(-18,18) +
                    (1+variant*.3)*np.sin(y/rng.uniform(75,110)+rng.uniform(-.5,.5))).ravel()
            sigma=.85+.18*variant+rng.uniform(-.08,.08)
            amplitude=(110 if category not in ('weak','saturated') else (34 if category=='weak' else 310))+rng.uniform(-2,2)
            background=np.full((480,752),15.,float)
            if category=='gradient':background[:]=60+70*x/751+15*y/479
            elif category=='bright_edge':background[:]=15+100*(x>375)
            signal=amplitude*np.exp(-.5*((x-center[:,None])/sigma)**2)
            visible=np.ones(480,bool)
            if category in ('noise','bright_edge'):
                signal[:]=0;visible[:]=False
            if category=='noise':
                background+=rng.normal(0,2,background.shape)
                for row in range(0,480,9):background[row,rng.integers(20,732,25)]+=65
            if category=='occlusion':signal[160:230]=0;visible[160:230]=False
            if category=='saturated':visible[:]=False
            raw=np.clip(np.rint(background+signal),0,255).astype(np.uint8)
            yield category,variant,raw,center,visible


def score(rows,centers,accepted,truth,visible):
    rows=np.asarray(rows)[accepted];centers=np.asarray(centers)[accepted]/4096
    matched=set();false=[];errors=[]
    for row,center in zip(rows,centers):
        row=int(row);error=abs(float(center)-float(truth[row]))
        if visible[row] and error<=.5 and row not in matched:matched.add(row);errors.append(error)
        else:false.append([row,float(center),error])
    return dict(truth_rows=int(visible.sum()),true_positive=len(matched),false_positive=len(false),
        false_points=false,center_error_px_max=max(errors,default=None),
        missed_rows=[int(r) for r in np.flatnonzero(visible) if r not in matched])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    config=V3Config();old=StripeConfig(fixed_threshold=60,min_width=1,max_width=32,min_sum=30,min_peak=15)
    frozen=dict(config_fingerprint=config.fingerprint,old_config_fingerprint=old.fingerprint,
        config_frozen_before_test=True,threshold_adjustments_after_test=0,match_tolerance_px=.5,
        policy='measurement',negative_categories=['noise','saturated','bright_edge'],
        generator_revision=2,source='synthetic image generator with held-out seed and varied stripe widths/backgrounds')
    (args.output/'FROZEN.json').write_text(json.dumps(frozen,indent=2)+'\n')
    rows=[]
    for split,seed in (('development',61021),('holdout',61022017)):
        for category,variant,raw,truth,visible in images(seed):
            before=extract_profiles(raw,old);after=extract_v3(raw,config);c=after.candidates
            rows.append(dict(split=split,seed=seed,category=category,variant=variant,
                raw_sha256=hashlib.sha256(raw.tobytes()).hexdigest(),
                old=score(before.rows,before.center_q12,before.accepted,truth,visible),
                V3=score(c.row,c.center_q12,c.measurement_usable,truth,visible),
                preview=score(c.row,c.center_q12,c.preview_usable,truth,visible)))
    summary={}
    for split in ('development','holdout'):
        summary[split]={}
        for category in sorted({r['category'] for r in rows}):
            group=[r for r in rows if r['split']==split and r['category']==category];counts={}
            for backend in ('old','V3','preview'):
                total=sum(r[backend]['truth_rows'] for r in group);tp=sum(r[backend]['true_positive'] for r in group)
                counts[backend]=dict(truth_rows=total,true_positive=tp,
                    false_positive=sum(r[backend]['false_positive'] for r in group),recall=None if not total else tp/total)
            summary[split][category]=counts
    result=dict(frozen=frozen,categories=summary,frames=rows,FPGA_execution=False,
        physical_accuracy_verified=False,parameters_changed=False)
    (args.output/'RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(summary,indent=2))

if __name__=='__main__':main()
