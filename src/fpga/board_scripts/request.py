"""Execute a saved, reviewable action through the boot-bound pinned SSH transport."""
import hashlib
import json
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
request = Path(sys.argv[1]).resolve()
request.relative_to(ROOT)
data = json.loads(request.read_text(encoding='utf-8'))
script = ROOT / 'board/board_action.py'
sys.argv = [str(script), data['action'], '--receipt', str(ROOT / data['receipt']),
            '--expected-boot', data['boot_id']]
for key in ('local', 'remote', 'command', 'timeout'):
    if key in data:
        sys.argv.extend(['--' + key, str(data[key])])
if data.get('sudo'):
    sys.argv.append('--sudo')
print('REQUEST_SHA256', hashlib.sha256(request.read_bytes()).hexdigest(), flush=True)
runpy.run_path(str(script), run_name='__main__')
