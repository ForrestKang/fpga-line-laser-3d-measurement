"""Direct frozen R42 row equivalence; isolated RTL simulation, no board I/O."""
from pathlib import Path
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = Path('D:/Workspace/CodexWork/scan3d_r42_buttons_boot_20261029_01')
IVER = Path('D:/Tools/structured_light_iverilog/bin')
SOURCE_RELS = ('rtl/r41_window_filter.sv', 'baseline_rtl/r33_direction.sv',
               'rtl/r35_direction.sv', 'rtl/r35_row.sv', 'rtl/r35_frontend.sv')
RAMS = ('pfo3_tdp_ram.sv', 'pfo3_banked_ram.sv')
CASES = ('T05', 'T06', 'cluster16', 'noise')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute(command, log, cwd, env):
    with log.open('w', encoding='utf-8') as stream:
        subprocess.run([str(x) for x in command], cwd=cwd, env=env,
                       stdout=stream, stderr=subprocess.STDOUT,
                       timeout=1800, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('output', type=Path)
    ap.add_argument('--jobs', type=int, choices=(1, 2), default=2)
    args = ap.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    temp = Path('D:/Workspace/CodexTemp/r43_frozen_compare')
    temp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp))
    started = time.time()
    sources = {}
    for variant, source_root in (('frozen_r42', BASE), ('current_r43', ROOT)):
        dest = out/'sources'/variant
        dest.mkdir(parents=True)
        sources[variant] = []
        for rel in SOURCE_RELS:
            src = source_root/rel
            target = dest/src.name
            shutil.copyfile(src, target)
            sources[variant].append(dict(path=str(src), snapshot=str(target), sha256=digest(target), bytes=target.stat().st_size))
        for name in RAMS:
            src = (BASE/'vendor/r31_baseline_copy/source_tree/pl/r25_next/rtl'/name
                   if variant == 'frozen_r42' else ROOT/'vendor/lane_sweep/rtl'/name)
            target = dest/name
            shutil.copyfile(src, target)
            sources[variant].append(dict(path=str(src), snapshot=str(target), sha256=digest(target), bytes=target.stat().st_size))
    tb_original = ROOT/'tests/lane_sweep_tb.sv'
    tb = tb_original.read_text()
    candidate_tb = out/'sources/current_r43/lane_sweep_tb.sv'
    candidate_tb.write_text(tb)
    baseline_tb = out/'sources/frozen_r42/lane_sweep_tb.sv'
    old = 'pfo3_frontend_r25 #(.FRAME_WIDTH(W),.FRAME_HEIGHT(H),.ROW_LANES(LANES),.ROW_DISPATCH(DISPATCH),.ROW_BANKED_REQUESTS(BANKED_REQUESTS),.ROW_BANKED_RESULTS(BANKED_RESULTS),.ROW_BALANCED_DISPATCH(BALANCED_DISPATCH)) dut(.*);'
    assert tb.count(old) == 1, 'Unexpected TB instantiation; refuse blind derivation'
    baseline_tb.write_text(tb.replace(old, 'pfo3_frontend_r25 #(.FRAME_WIDTH(W),.FRAME_HEIGHT(H)) dut(.*);'))
    sources['tb'] = dict(original=str(tb_original), original_sha256=digest(tb_original),
                         candidate_sha256=digest(candidate_tb), frozen_derived_sha256=digest(baseline_tb),
                         change='Only remove R43-only frontend parameters for frozen original frontend; stimulus and output checks unchanged.')
    inputs = out/'inputs'
    inputs.mkdir()
    sim = ROOT/'vendor/lane_sweep/sim'
    input_paths = {}
    for case in CASES:
        src = (ROOT/'evidence/LANE_SWEEP_R43_03_EXTENDED/cluster16.hex' if case == 'cluster16'
               else sim/({'T05':'T05_000.hex', 'T06':'T06_000.hex', 'noise':'noise.hex'}[case]))
        target = inputs/(case+'.hex')
        shutil.copyfile(src, target)
        input_paths[case] = target
    freeze = json.loads((ROOT/'evidence/BASELINE_FREEZE_R43.json').read_text(encoding='utf-8-sig'))
    expected = {e['path']:e['sha256'] for e in freeze['baseline_files']}
    for rel in SOURCE_RELS:
        assert digest(out/'sources/frozen_r42'/Path(rel).name) == expected[rel], 'Frozen source mismatch: '+rel
    # Audit actual earlier sources, rather than trusting a file named reference.
    earlier = ROOT/'evidence/LANE_SWEEP_R43_01'
    old_result = json.loads((earlier/'RESULT.json').read_text())
    assert old_result['passed'] and old_result['lanes'][0] == 2
    for name in ('r41_window_filter.sv','r33_direction.sv','r35_direction.sv','r35_row.sv',*RAMS):
        assert digest(earlier/'sources'/name) == digest(out/'sources/frozen_r42'/name), 'Earlier baseline source differs: '+name
    old_front = (earlier/'sources/r35_frontend.sv').read_text()
    old_front = old_front.replace('// R43 production builds may select direction lanes without changing AXI/BD ports.\n`ifndef R43_ROW_LANES\n`define R43_ROW_LANES 2\n`endif\n', '')
    old_front = old_front.replace(',ROW_LANES=`R43_ROW_LANES', '').replace(', .LANES(ROW_LANES)', '')
    old_front = old_front.replace(', .LANES(ROW_LANES)', '').replace(',.LANES(ROW_LANES)', '')
    assert old_front == (out/'sources/frozen_r42/r35_frontend.sv').read_text(), 'Earlier frontend differs beyond 2-lane parameter plumbing'
    rows = []
    reused = []
    for case in ('T05','T06','noise'):
        metric = next(x for x in old_result['results'] if x['case']==case and x['lanes']==2)
        assert digest(input_paths[case]) == metric['input_sha256']
        for suffix,key in (('records','record_sha256'),('geometry','geometry_sha256')):
            src=earlier/f'{case}_l2.{suffix}'
            assert digest(src)==metric[key]
            target=out/f'{case}_frozen_r42.{suffix}'
            shutil.copyfile(src,target)
        reused.append(dict(case=case,result=str(earlier/'RESULT.json'),result_sha256=digest(earlier/'RESULT.json'),actual_compiled_row_sha256=digest(earlier/'sources/r35_row.sv'),frontend_adapter_proven_default_two_lane=True))
        rows.append(dict(variant='frozen_r42',case=case,reused=True,**{k:metric[k] for k in ('cycles','processing_cycles','records','geometry','record_sha256','geometry_sha256')}))
    (out/'SOURCE_AUDIT.json').write_text(json.dumps(dict(sources=sources,reused=reused,input_sha256={k:digest(v) for k,v in input_paths.items()}),indent=2))
    configs=[('frozen_r42',2,0,0,0),('r43_2static',2,0,1,1),('r43_2dynamic',2,1,1,1),('r43_8static',8,0,1,1)]
    def run_config(config):
        tag,lanes,dispatch,requests,results=config
        group='frozen_r42' if tag=='frozen_r42' else 'current_r43'
        src=out/'sources'/group
        # The completed 8-static matrix may be reused only when every compiled
        # source (including TB), configuration, input and output hash agrees.
        # Merely finding a file called reference is never sufficient.
        reuse_dir=ROOT/'evidence/LANE_SWEEP_R43_08_STATIC_BANKED'
        if tag=='r43_8static' and (reuse_dir/'RESULT.json').is_file():
            previous=json.loads((reuse_dir/'RESULT.json').read_text())
            expected_config=(previous.get('lanes'),previous.get('dispatch',0),
                             previous.get('banked_requests',0),previous.get('banked_results',0),
                             previous.get('balanced_dispatch',0))
            same_sources=all((reuse_dir/'sources'/p.name).is_file() and
                             digest(p)==digest(reuse_dir/'sources'/p.name)
                             for p in src.glob('*.sv'))
            if previous['passed'] and expected_config==([8],0,1,1,0) and same_sources:
                reused_rows=[]
                for case in CASES:
                    metric=next(x for x in previous['results'] if x['case']==case and x['lanes']==8)
                    assert metric['input_sha256']==digest(input_paths[case])
                    for suffix,key in (('records','record_sha256'),('geometry','geometry_sha256')):
                        old_path=reuse_dir/f'{case}_l8.{suffix}'
                        assert digest(old_path)==metric[key]
                        shutil.copyfile(old_path,out/f'{case}_{tag}.{suffix}')
                    reused_rows.append(dict(metric,variant=tag,reused=True,
                                            reused_result=str(reuse_dir/'RESULT.json'),
                                            reused_result_sha256=digest(reuse_dir/'RESULT.json')))
                print('REUSED_8STATIC_EXACT_COMPILED_SOURCES',flush=True)
                return reused_rows
        vvp=out/(tag+'.vvp')
        cmd=[IVER/'iverilog.exe','-g2012','-s','lane_sweep_tb',f'-Plane_sweep_tb.LANES={lanes}',f'-Plane_sweep_tb.DISPATCH={dispatch}',f'-Plane_sweep_tb.BANKED_REQUESTS={requests}',f'-Plane_sweep_tb.BANKED_RESULTS={results}','-Plane_sweep_tb.BALANCED_DISPATCH=0','-o',vvp]
        cmd += [src/Path(rel).name for rel in SOURCE_RELS]+[src/n for n in RAMS]+[src/'lane_sweep_tb.sv']
        execute(cmd,out/(tag+'_compile.log'),out,env)
        generated=[]
        for case in (('cluster16',) if tag=='frozen_r42' else CASES):
            record=out/f'{case}_{tag}.records';geo=out/f'{case}_{tag}.geometry';log=out/f'{case}_{tag}.log'
            before=time.time()
            execute([IVER/'vvp.exe',vvp,f'+INPUT={input_paths[case]}',f'+OUTPUT={record}',f'+GEOMETRY={geo}'],log,out,env)
            txt=log.read_text();assert 'TEST_PASS' in txt,txt
            match=re.search(r'RESULT (.+)',txt);assert match,txt
            metric={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',match.group(1))}
            row=dict(variant=tag,case=case,reused=False,elapsed_s=time.time()-before,**metric,
                     record_sha256=digest(record),geometry_sha256=digest(geo))
            generated.append(row)
            print(json.dumps(row),flush=True)
        return generated
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for generated in pool.map(run_config,configs):rows.extend(generated)
    comparisons=[]
    for tag,lanes,dispatch,requests,results in configs[1:]:
        for case in CASES:
            entry=dict(variant=tag,case=case)
            for suffix in ('records','geometry'):
                a=(out/f'{case}_frozen_r42.{suffix}').read_bytes();b=(out/f'{case}_{tag}.{suffix}').read_bytes()
                entry[suffix+'_byte_equal']=a==b
                entry[suffix+'_baseline_sha256']=hashlib.sha256(a).hexdigest()
                entry[suffix+'_candidate_sha256']=hashlib.sha256(b).hexdigest()
            comparisons.append(entry)
    passed=all(x['records_byte_equal'] and x['geometry_byte_equal'] for x in comparisons)
    result=dict(passed=passed,evidence_kind='FULL_SIZE_RTL_SIMULATION_FROZEN_R42_COMPARISON_NOT_BOARD',resolution=[752,480],baseline_row_sha256=digest(out/'sources/frozen_r42/r35_row.sv'),sources=sources,reused_baseline_evidence=reused,configs=configs,cases=CASES,all_noncycle_record_fields_compared=True,geometry_uv_order_modes_compared=True,processing_cycle_difference_allowed=True,results=rows,comparisons=comparisons,elapsed_s=time.time()-started)
    (out/'RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
    print('ALL_PASS' if passed else 'MISMATCH',flush=True)
    raise SystemExit(0 if passed else 2)


if __name__=='__main__':main()
