"""Build + differential QA only. No hardware access; output outside runtime.

ARM example: python -B r29_arm_build_observation_modes.py --runtime
 /home/xilinx/ARM_SOFTWARE16/runtime --out /home/xilinx/work/r29_observation_modes
Then root copies the resulting .so and manifest together into candidate/native.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

import numpy as np


def reference(records, q):
    a = records[(records['decisions'] & 255) == 0]
    if len(a) != q: raise ValueError('reference qualified count mismatch')
    integers = np.vstack([a['row'].astype(np.int64), a['center_q12'].astype(np.int64),
        a['end'].astype(np.int64)-a['start'].astype(np.int64)+1,
        (a['aux']>>24).astype(np.int64), a['start'].astype(np.int64), a['end'].astype(np.int64),
        a['candidate_id'].astype(np.int64), (a['branches']&65535).astype(np.int64),
        (a['branches']>>16).astype(np.int64)])
    integers[7:][integers[7:] == 65535] = -1
    floating = np.vstack([a['contrast_q12'].astype(np.float64)/4096., a['edge_drop_q12'].astype(np.float64)/4096.])
    modes = np.vstack([((a['decisions']>>25)&1).astype(np.uint8),
        ((a['decisions']>>26)&1).astype(np.uint8), ((a['decisions']>>8)&255).astype(np.uint8),
        ((a['decisions']>>16)&255).astype(np.uint8)])
    counts = np.vstack([np.bincount(((records['decisions']>>shift)&255).astype(np.intp), minlength=8)
                        for shift in (8, 16)]).astype(np.uint64)
    accepted = np.asarray([modes[0].sum(), modes[1].sum()], np.uint32)
    return integers, floating, modes, counts, accepted


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--cc', default='gcc')
    args = p.parse_args(argv)
    runtime, out = args.runtime.resolve(), args.out.resolve()
    if out == runtime or runtime in out.parents:
        raise ValueError('build outputs must be outside the release runtime')
    out.mkdir(parents=True, exist_ok=True)
    source = runtime / 'candidate/native/observation_modes_r29.c'
    library = out / ('observation_modes_r29.dll' if os.name == 'nt' else 'libobservation_modes_r29.so')
    if library.is_symlink(): raise ValueError('refuse symlink build output')
    command = [args.cc, '-O3', '-ffp-contract=off', '-std=c99', '-Wall', '-Wextra', '-Werror', '-shared']
    if os.name != 'nt': command.append('-fPIC')
    command += [str(source), '-o', str(library)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    lib = ctypes.CDLL(str(library))
    lib.observation_modes_r29_abi.restype = ctypes.c_uint32
    if lib.observation_modes_r29_abi() != 0x290001: raise ValueError('build ABI mismatch')
    call = lib.observation_modes_r29
    call.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t] + [ctypes.c_void_p]*5
    call.restype = ctypes.c_int
    # Import only the wire schema; do not load processors, board drivers or native manifests.
    sys.path.insert(0, str(runtime))
    from candidate.pfo3_r25 import RECORD_DTYPE
    cases = []
    for n, q in ((0,0), (1,0), (1,1), (17,7), (480,0), (480,480),
                 (7680,7680), (8192,7680), (180480,17)):
        rng = np.random.default_rng(290001+n+q)
        a = rng.integers(0, 2**32, size=(n,16), dtype=np.uint32).view(RECORD_DTYPE).reshape(-1)
        local = rng.integers(1,256,n,dtype=np.uint32); local[:q] = 0
        rng.shuffle(local)
        a['decisions'] = local | (rng.integers(0,8,n,dtype=np.uint32)<<8) | (rng.integers(0,8,n,dtype=np.uint32)<<16) | (rng.integers(0,8,n,dtype=np.uint32)<<24)
        if n: a['branches'][::3] = 0xffffffff
        expected = reference(a, q)
        actual = (np.empty((9,q),np.int64), np.empty((2,q),np.float64), np.empty((4,q),np.uint8),
                  np.empty((2,8),np.uint64), np.empty(2,np.uint32))
        code = call(a.ctypes.data, n, q, *(v.ctypes.data for v in actual))
        if code or any(x.dtype != y.dtype or x.shape != y.shape or x.tobytes() != y.tobytes()
                       for x,y in zip(expected,actual)):
            raise ValueError('native differential QA failed: '+str((n,q,code)))
        cases.append(dict(diagnostic=n, qualified=q, all_five_slabs_byte_equal=True))
    invalid_codes = []
    for n, q in ((180481,0), (0,1), (0,7681)):
        code = call(None,n,q,None,None,None,actual[3].ctypes.data,actual[4].ctypes.data)
        if not code: raise ValueError('invalid capacity unexpectedly accepted')
        invalid_codes.append(code)
    doc = dict(schema='R29_OBSERVATION_MODES_V1', abi=0x290001,
        runtime=[platform.system().lower(),platform.machine().lower(),sys.byteorder,
                 platform.python_version(),np.__version__],
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), library=library.name,
        library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(), command=command,
        differential_qa_passed=True, cases=cases, invalid_capacity_codes=invalid_codes,
        scope='FULL_DIAGNOSTIC_WIRE_CONVERSION_ONLY; NO IMAGE ALGORITHM OR ADMISSION')
    manifest = out / 'observation_modes_r29_manifest.json'
    if manifest.is_symlink(): raise ValueError('refuse symlink manifest output')
    manifest.write_text(json.dumps(doc,indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(manifest=str(manifest), **doc), indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
