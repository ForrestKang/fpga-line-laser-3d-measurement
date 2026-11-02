"""One explicit, run-bound scan request; no hardware or implicit retries."""
import json
from pathlib import Path


class ScanStartRequest:
    def __init__(self, path, *, machine_id, boot_id, run_id):
        self.path = Path(path)
        self.expected = dict(machine_id=machine_id, boot_id=boot_id, run_id=run_id)
        self.consumed = False

    def poll(self, scan, *, allow_motor):
        if self.consumed:
            return None
        try:
            text = self.path.read_text(encoding='utf8')
        except FileNotFoundError:
            return None
        self.consumed = True
        try:
            request = json.loads(text)
        except (ValueError, TypeError) as error:
            raise ValueError('scan start request is not valid JSON') from error
        if (not isinstance(request, dict) or request.get('action') != 'start'
                or any(request.get(k) != v for k, v in self.expected.items())):
            raise ValueError('scan start request must match this machine, boot and output run')
        if not allow_motor or scan.backend is None:
            raise ValueError('scan start request requires an initialized authorized motor')
        record = dict(path=str(self.path), action='start', **self.expected)
        if scan.state != 'IDLE':
            return dict(record, started=False, reason='scan is not IDLE', state=scan.state)
        scan.button(1)
        if scan.state != 'SCANNING':
            raise RuntimeError('explicit scan start did not enter SCANNING')
        return dict(record, started=True, reason='explicit run-bound request', state=scan.state)
