"""Process-local GIL scheduling experiment around the unchanged formal entry.

No algorithm, protocol, validation or persistence policy is changed. Both the
old and requested switch interval are reported before running the normal entry.
"""
import argparse
import json
from pathlib import Path
import sys

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--switch-ms',type=float,required=True)
    args,forward=p.parse_known_args()
    if not 0.1 <= args.switch_ms <= 5.0:
        raise ValueError('bounded process-local switch interval required')
    before=sys.getswitchinterval()
    sys.setswitchinterval(args.switch_ms/1000.0)
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from r26_scan import main as scan
    print(json.dumps({'experiment':'PROCESS_LOCAL_GIL_SWITCH_INTERVAL',
                      'before_s':before,'requested_s':sys.getswitchinterval(),
                      'algorithm_changed':False,'validation_removed':False}),flush=True)
    return scan(forward)

if __name__=='__main__':raise SystemExit(main())
