"""Offline-only R42 + inherited R41 regression runner (safe with python -I)."""
from pathlib import Path
import sys
import unittest
import argparse
import json
import time
root = Path(__file__).resolve().parent
for path in (root, root/'structured_light_system_algorithm_v1/src/python',
             root/'structured_light_system_algorithm_v1/board/scripts',
             root/'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0, str(path))
parser = argparse.ArgumentParser()
parser.add_argument('--report', type=Path)
args = parser.parse_args()
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'r33.tests_buttons_r42', 'r33.tests_scan_live_r42', 'r33.tests_live_r42', 'r33.tests_profile_r41',
    'r33.tests_pl_measure_r41', 'r33.tests_live_r41', 'r33.tests_pages'])
def cases(items):
    for item in items:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item.id()
names = list(cases(suite))
started = time.perf_counter()
result = unittest.TextTestRunner(verbosity=2).run(suite)
if args.report is not None:
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(dict(schema='R42_RUNTIME_PC_MOCK_V1', tests_run=result.testsRun,
                       failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                       passed=result.wasSuccessful(), duration_s=time.perf_counter()-started,
                       hardware_commands=0, network_used=False, tests=names), stream, indent=2)
raise SystemExit(not result.wasSuccessful())
