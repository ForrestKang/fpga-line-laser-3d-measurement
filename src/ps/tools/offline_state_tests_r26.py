"""State-machine tests for the offline flow: exposure, rescan budget, failure stop.

These prove controller behaviour, not a closed optical loop: the saved frames
never change with the simulated exposure.
"""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
CAND = RUNTIME.parent
for p in (RUNTIME, RUNTIME / 'structured_light_system_algorithm_v1/src/python',
          RUNTIME / 'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0, str(p))
sys.path.append(str(Path(r'D:/NUECSDC-FPGA/structured_light_system_geometry_v1/.venv/Lib/site-packages')))

from structured_light.scan3d.continuous_exposure import ExposureController  # noqa: E402


def test_exposure_up():
    c = ExposureController(line_values=(30, 60, 120), initial_lines=60, hysteresis_frames=3)
    assert c.propose('weak') is None and c.propose('weak') is None
    op = c.propose('weak')
    assert op is not None and op.lines == 120 and op.revision == 1, 'hysteresis then raise'
    assert c.propose('weak') is None, 'pending blocks new proposals'
    c.acknowledge(op.revision, accepted=True, effective_after_request_id=10)
    assert c.lines == 120 and c.revision == 1
    assert c.frame_eligible(request_id=10, exposure_revision=1) is False, 'boundary frame not eligible'
    assert c.frame_eligible(request_id=11, exposure_revision=1) is True
    assert c.frame_eligible(request_id=11, exposure_revision=0) is False, 'stale revision rejected'
    return dict(history=c.history)


def test_exposure_down_and_revision():
    c = ExposureController(line_values=(30, 60, 120), initial_lines=120, hysteresis_frames=2)
    c.propose('saturated'); op = c.propose('saturated')
    assert op is not None and op.lines == 60 and op.revision == 1
    c.acknowledge(op.revision, accepted=True, effective_after_request_id=5)
    assert c.revision == 1 and c.lines == 60, 'one acknowledged change advances the revision once'
    return dict(revision=c.revision, lines=c.lines)


def test_exposure_bounds_and_failure():
    c = ExposureController(line_values=(30, 60), initial_lines=60, hysteresis_frames=1)
    assert c.propose('weak') is None, 'cannot exceed the largest line count'
    c2 = ExposureController(line_values=(30, 60), initial_lines=60, hysteresis_frames=1)
    op = c2.propose('saturated')
    try:
        c2.acknowledge(op.revision + 1, accepted=True, effective_after_request_id=1)
    except ValueError:
        pass
    else:
        raise AssertionError('mismatched revision must be rejected')
    c3 = ExposureController(line_values=(30, 60), initial_lines=30, hysteresis_frames=1)
    assert c3.propose('saturated') is None, 'cannot go below the smallest line count'
    return dict(bounds='ok')


def test_failure_receipt(tmp=None):
    out = CAND / 'offline_runs' / 'failure_receipt_test'
    if out.exists():
        for p in sorted(out.rglob('*'), reverse=True):
            p.rmdir() if p.is_dir() else p.unlink()
        out.rmdir()
    r = subprocess.run([sys.executable, str(RUNTIME / 'tools' / 'offline_scan_r26.py'),
                        '--run-root', str(CAND / 'does_not_exist'), '--target', 'T05',
                        '--output', str(out), '--limit', '1'],
                       capture_output=True, text=True)
    error = out / 'ERROR.json'
    assert r.returncode != 0, 'failure must be non-zero'
    assert error.exists(), 'bounded failure receipt required'
    doc = json.loads(error.read_text())
    assert doc['automatic_retry'] is False and doc['partial_evidence_preserved'] is True
    return dict(exit=r.returncode, receipt=doc['error'][:80])


def main():
    result = dict(schema='R26_OFFLINE_STATE_TESTS_V1',
                  exposure_up=test_exposure_up(),
                  exposure_down=test_exposure_down_and_revision(),
                  exposure_bounds=test_exposure_bounds_and_failure(),
                  failure_receipt=test_failure_receipt(),
                  closed_loop_optical_verification=False,
                  note='simulated exposure never changes the frozen frames')
    (CAND / 'offline_runs' / 'STATE_TESTS.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2)[:1200])
    print('STATE_TESTS_PASS')


if __name__ == '__main__':
    main()
