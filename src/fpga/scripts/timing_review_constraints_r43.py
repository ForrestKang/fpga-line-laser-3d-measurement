"""Check constraints exported from the actual baseline and optimized DCPs."""
import argparse
from collections import Counter
import hashlib
import json
import re
from pathlib import Path

def sha(p):
    with p.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def commands(p):
    return [s.strip() for s in p.read_text().splitlines() if s.strip() and not s.lstrip().startswith('#')]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('directory', type=Path)
    ap.add_argument('output', type=Path)
    a = ap.parse_args()
    if a.output.exists():
        raise SystemExit('Receipt exists')
    d = a.directory
    old = commands(d / 'baseline_constraints.xdc')
    new = commands(d / 'candidate_constraints.xdc')
    removed = list((Counter(old) - Counter(new)).elements())
    added = list((Counter(new) - Counter(old)).elements())
    uncertainty = re.compile(r'^set_clock_uncertainty -setup ([0-9.]+) \[get_clocks clk_fpga_1\]$')
    allowed = all(uncertainty.fullmatch(s) for s in added)
    old_user = [float(m[1]) for s in old if (m := uncertainty.fullmatch(s))]
    new_user = [float(m[1]) for s in new if (m := uncertainty.fullmatch(s))]
    old_effective = old_user[-1] if old_user else 0.0
    new_effective = new_user[-1] if new_user else 0.0
    stripped = [s for s in new if not uncertainty.fullmatch(s)]
    old_stripped = [s for s in old if not uncertainty.fullmatch(s)]
    props_equal = (d / 'baseline_clock_properties.txt').read_bytes() == (d / 'candidate_clock_properties.txt').read_bytes()
    def timing_uncertainty(name):
        s = (d / (name + '_clk1_timing.rpt')).read_text()
        return float(re.search(r'Clock Uncertainty:\s+([0-9.]+)ns', s)[1])
    original_regs = set((d / 'baseline_registers.txt').read_text().splitlines())
    candidate_regs = set((d / 'candidate_registers.txt').read_text().splitlines())
    actual_uncertainty = [timing_uncertainty(s) for s in ('baseline', 'candidate')]
    passed = not removed and allowed and old_effective == new_effective and stripped == old_stripped and props_equal and actual_uncertainty[0] == actual_uncertainty[1]
    receipt = {
        'kind': 'ACTUAL_DCP_REOPEN_CONSTRAINT_COMPARISON', 'passed': passed,
        'baseline_command_count': len(old), 'candidate_command_count': len(new),
        'removed_commands': removed, 'added_commands': added,
        'all_original_commands_identical_in_order': stripped == old_stripped,
        'all_clock_period_waveform_source_jitter_properties_identical': props_equal,
        'baseline_final_user_setup_uncertainty_ns': old_effective,
        'candidate_final_user_setup_uncertainty_ns': new_effective,
        'baseline_actual_clk1_path_uncertainty_ns': actual_uncertainty[0],
        'candidate_actual_clk1_path_uncertainty_ns': actual_uncertainty[1],
        'baseline_registers': len(original_regs), 'candidate_registers': len(candidate_regs),
        'added_registers': sorted(candidate_regs - original_regs),
        'removed_registers': sorted(original_regs - candidate_regs),
        'files': [{'file': p.name, 'size': p.stat().st_size, 'sha256': sha(p)} for p in sorted(d.iterdir()) if p.is_file()],
    }
    a.output.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt, indent=2))
    if not passed:
        raise SystemExit(2)

if __name__ == '__main__':
    main()
