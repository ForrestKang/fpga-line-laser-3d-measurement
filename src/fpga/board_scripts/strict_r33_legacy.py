"""Validate an R33 journal using its actual preserved R33 reader, not R35."""
import hashlib
import json
import os
from pathlib import Path
import sys

LEGACY = Path('D:/Workspace/CodexWork/scan3d_r33_full_pixel_20261025_01/runtime')
sys.path[:0] = [str(LEGACY), str(LEGACY/'structured_light_system_algorithm_v1/src/python'),
               str(LEGACY/'structured_light_system_geometry_v1/src/python')]
_DLL = os.add_dll_directory('D:/Anaconda3/Library/bin')
os.environ['R33_NATIVE_ASSEMBLER'] = 'D:/Workspace/CodexWork/scan3d_r43_timing_board_20261029_02/board/host_native/assemble_r33.dll'
import numpy as np  # noqa: E402
from r33.storage import readback  # noqa: E402

root = Path(sys.argv[1])
result = readback(root/'storage', strict_protocol=True)
run = json.loads((root/'RESULT.json').read_text(encoding='utf8'))
if not result['passed'] or result['frames'] != run['frames'] or result['incomplete_tail']:
    raise ValueError('R33 complete strict frame count mismatch')
for mode, filename in [('measurement', 'cloud.npy'), ('preview', 'preview.npy')]:
    xyz = np.load(root/filename, allow_pickle=False)
    if xyz.ndim != 2 or xyz.shape[1] != 3 or not np.isfinite(xyz).all():
        raise ValueError('R33 exported cloud malformed')
    if len(xyz) != result[mode+'_points'] or len(xyz) != run[mode+'_points']:
        raise ValueError('R33 point count mismatch')
    if hashlib.sha256(xyz.tobytes()).hexdigest() != result[mode+'_xyz_sha256']:
        raise ValueError('R33 exported cloud mismatch against full journal')
result['frozen_reader_path'] = str(LEGACY/'r33/storage.py')
result['frozen_reader_sha256'] = hashlib.sha256((LEGACY/'r33/storage.py').read_bytes()).hexdigest()
with Path(sys.argv[2]).open('x', encoding='utf8') as out:
    json.dump(result, out, indent=2)
print(json.dumps(dict(passed=True, frames=result['frames'], legacy_reader=str(LEGACY))))
