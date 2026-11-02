"""Offline section driver and inherited live/UI regression; never accesses a board."""
import argparse
import json
import sys
import time
import unittest
from pathlib import Path

root = Path(__file__).resolve().parent
for path in (root, root / 'structured_light_system_algorithm_v1/src/python',
             root / 'structured_light_system_algorithm_v1/board/scripts',
             root / 'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0, str(path))
parser = argparse.ArgumentParser()
parser.add_argument('--report', type=Path, required=True)
args = parser.parse_args()
modules = ['r33.tests_pl_section_final', 'r33.tests_buttons_r42', 'r33.tests_scan_live_r42',
           'r33.tests_live_r42', 'r33.tests_profile_r41', 'r33.tests_pl_measure_r41',
           'r33.tests_live_r41', 'r33.tests_pages']
suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
start = time.perf_counter()
result = unittest.TextTestRunner(verbosity=2).run(suite)
report = {'schema': 'FINAL_SECTION_RUNTIME_PC_V1', 'modules': modules, 'tests_run': result.testsRun,
              'failures': [(str(test), detail) for test, detail in result.failures],
              'errors': [(str(test), detail) for test, detail in result.errors],
              'skipped': [(str(test), why) for test, why in result.skipped], 'passed': result.wasSuccessful(),
              'duration_s': time.perf_counter() - start, 'evidence_class': 'PC_MOCK_NOT_BOARD', 'hardware_commands': 0}
args.report.parent.mkdir(parents=True, exist_ok=True)
with args.report.open('x', encoding='utf-8') as stream:
    json.dump(report, stream, ensure_ascii=False, indent=2)
raise SystemExit(not result.wasSuccessful())
