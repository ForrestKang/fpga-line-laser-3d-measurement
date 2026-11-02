"""Explicit local build + full-domain differential QA; no board connection."""
from pathlib import Path
import argparse,ctypes,hashlib,json,os,platform,subprocess,sys,time
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from candidate.observation_columns_r28 import runtime_identity
from candidate.pfo3_r25 import RECORD_DTYPE

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build',action='store_true',required=True)
    args=parser.parse_args()
    source=ROOT/'candidate/native/observation_columns_r28.c'
    library=source.with_name('observation_columns_r28.dll' if os.name=='nt' else 'libobservation_columns_r28.so')
    manifest=source.with_name('observation_columns_r28_manifest.json')
    if library.exists() or manifest.exists():raise FileExistsError('preserve previous build')
    command=['gcc','-O3','-ffp-contract=off','-std=c99','-Wall','-Wextra','-Werror','-shared']
    if os.name!='nt':command.append('-fPIC')
    command += [str(source),'-o',str(library)]
    subprocess.run(command,check=True,capture_output=True)
    lib=ctypes.CDLL(str(library));fn=lib.observation_columns_r28
    fn.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_void_p];fn.restype=ctypes.c_int
    rng=np.random.default_rng(280024);cases=[]
    for n in (0,1,16,17,479,480,7680):
        raw=rng.integers(0,2**32,size=(n,16),dtype=np.uint32)
        if n:raw[0]=np.asarray([0,2**32-1]*8,np.uint32)
        records=raw.view(RECORD_DTYPE).reshape(-1)
        integer=np.empty((7,n),np.int64);floating=np.empty((2,n),np.float64)
        assert fn(records.ctypes.data,n,integer.ctypes.data,floating.ctypes.data)==0
        expected=np.vstack([records['row'].astype(np.int64),records['center_q12'].astype(np.int64),
            records['end'].astype(np.int64)-records['start'].astype(np.int64)+1,
            (records['aux']>>24).astype(np.int64),records['start'].astype(np.int64),
            records['end'].astype(np.int64),records['candidate_id'].astype(np.int64)])
        ef=np.vstack([records['contrast_q12'].astype(np.float64)/4096.,records['edge_drop_q12'].astype(np.float64)/4096.])
        assert integer.tobytes()==expected.tobytes() and floating.tobytes()==ef.tobytes()
        cases.append(dict(count=n,integer_bytes_equal=True,float_bytes_equal=True))
    assert fn(None,7681,None,None)!=0
    assert fn(None,1,None,None)!=0
    doc=dict(schema='R28_OBSERVATION_COLUMNS_V1',abi=0x280001,runtime=runtime_identity(),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),library=library.name,
        library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),command=command,
        differential_qa_passed=True,cases=cases,invalid_bounds_rejected=True,
        normal_PL_image_algorithm_calls=0,scope='WIRE_FIELD_CONVERSION_ONLY')
    with manifest.open('x') as stream:json.dump(doc,stream,indent=2)
    print(json.dumps(doc,indent=2))
if __name__=='__main__':main()
