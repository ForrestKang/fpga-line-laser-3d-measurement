"""Unified actual PL replay; never commands camera, laser or motor."""
import os,sys
from pathlib import Path
RUNTIME=Path(__file__).resolve().parents[1]
if str(RUNTIME) not in sys.path:sys.path.insert(0,str(RUNTIME))
def main(argv=None):
    os.environ['SCAN3D_R30_PIPELINE']='unified'
    os.environ['SCAN3D_RELEASE_PROFILE']='R30_UNIFIED_PIPELINE_V1'
    os.environ.setdefault('SCAN3D_R29_GEOMETRY','shared')
    os.environ.setdefault('SCAN3D_R29_OBSERVATIONS','fused')
    os.environ.setdefault('SCAN3D_R29_TX_CACHE','baseline')
    args=list(sys.argv[1:] if argv is None else argv)
    if not any(x.split('=',1)[0]=='--display-min-interval-ms' for x in args):args.extend(('--display-min-interval-ms','0'))
    from tools.r29_scan import release_arguments
    from tools.r28_scan import main as run
    return run(release_arguments(args))
if __name__=='__main__':raise SystemExit(main())
