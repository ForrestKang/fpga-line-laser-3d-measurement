"""Explicit CPU-only build and differential QA; never touches FPGA or devices.

The library and manifest are exclusive outputs in the deployed candidate.
Run this before passing its explicit manifest to actual PL transport.
"""
from pathlib import Path
import hashlib,io,json,os,sys,unittest
ROOT=Path(__file__).resolve().parents[1]
for path in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',
             ROOT/'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0,str(path))
from candidate.pfo3_decode_native_r25 import ABI,build_candidate
from candidate import pfo3_r25 as wire
from structured_light.scan3d.point_raster import runtime_identity

TESTS=('test_full_nonempty_exact_arrays_and_immutable_views',
    'test_every_body_error_class_preserves_python_priority',
    'test_empty_and_maximum180480_candidates_no_truncation',
    'test_uint64_products_and_floor_Q12_extreme_are_safe',
    'test_strict_prefix_crc_identity_and_lengths_remain_python_equivalent',
    'test_deterministic_single_and_multiple_corruptions_match_error_classes',
    'test_explicit_library_hash_precondition_and_build_never_overwrite')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build',action='store_true');args=parser.parse_args()
    output=ROOT/'candidate/native'/('pfo3_validate_r25.dll' if os.name=='nt' else 'libpfo3_validate_r25.so')
    manifest=output.with_name('pfo3_validate_manifest_r25.json')
    qa_path=output.with_name('pfo3_validate_qa_r25.json')
    tests_dir=ROOT.parent/'tests/r25'
    if not args.build:
        print(json.dumps(dict(output=str(output),manifest=str(manifest),tests=TESTS,hardware_access=False)));return
    if any(path.exists() for path in (output,manifest,qa_path)):raise FileExistsError('preserve native build and validation outputs')
    for name in ('test_pfo3_native_r25.py','test_pfo3_regressions_r25.py'):
        if not (tests_dir/name).is_file():raise ValueError('explicit deployed QA source missing: '+name)
    before={name:sha(tests_dir/name) for name in ('test_pfo3_native_r25.py','test_pfo3_regressions_r25.py')}
    build=build_candidate(output)
    runtime=runtime_identity()
    sys.path.insert(0,str(tests_dir))
    import test_pfo3_native_r25 as tests
    stream=io.StringIO();prior=os.environ.get('R25_NATIVE_TEST_LIBRARY')
    os.environ['R25_NATIVE_TEST_LIBRARY']=str(output)
    try:result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.TestSuite(tests.NativeBodyTests(name) for name in TESTS))
    finally:
        if prior is None:os.environ.pop('R25_NATIVE_TEST_LIBRARY',None)
        else:os.environ['R25_NATIVE_TEST_LIBRARY']=prior
    if any(sha(tests_dir/name)!=digest for name,digest in before.items()):raise ValueError('QA source changed during execution')
    passed=result.wasSuccessful() and result.testsRun==len(TESTS) and not result.skipped
    source=ROOT/'candidate/native/pfo3_validate_r25.c'
    binding=dict(runtime=runtime,library_sha256=sha(output),source_sha256=sha(source),
                 python_decoder_sha256=sha(wire.__file__))
    validation=dict(binding,passed=passed,full_fields_equal=passed,body_error_classes_equal=passed,
        max_candidates_equal=passed,uint64_boundary_equal=passed,test_count=result.testsRun,
        tests=TESTS,test_sources_sha256=before,unittest_log=stream.getvalue(),hardware_access=False)
    with qa_path.open('x',encoding='utf8') as f:json.dump(dict(build=build,validation=validation),f,indent=2)
    if not passed:raise RuntimeError('native validator QA failed; preserve library and QA, no manifest published')
    record=dict(schema='R25_PFO3_BODY_VALIDATOR_LIBRARY_V1',abi=ABI,library=output.name,**binding,validation=validation)
    with manifest.open('x',encoding='utf8') as f:json.dump(record,f,indent=2)
    print(json.dumps(dict(manifest=str(manifest),manifest_sha256=sha(manifest),tests=result.testsRun,passed=True,
                         runtime=runtime,hardware_access=False)))

if __name__=='__main__':main()
