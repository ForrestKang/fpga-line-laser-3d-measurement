"""Fail-closed admission from actual Vivado reports; never performs board I/O."""
import argparse
import json
import re
from pathlib import Path


def review(reports, expected_lanes=None):
    def read(name):
        return (reports / name).read_text(encoding='utf-8', errors='replace')

    def require(pattern, text, label):
        match = re.search(pattern, text, re.M)
        if not match:
            raise ValueError('Missing report evidence: ' + label)
        return match

    timing = read('timing_route.rpt')
    line = require(r'WNS\(ns\).*\n\s*-+.*\n\s*([-\d.]+)\s+([-\d.]+)\s+(\d+)\s+(\d+)\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\s+(\d+)\s+([-\d.]+)\s+([-\d.]+)\s+(\d+)\s+(\d+)', timing, 'timing summary')
    values = [float(value) for value in line.groups()]
    wns, tns, setup_fail, _, whs, ths, hold_fail, _, wpws, tpws, pw_fail, _ = values
    cdc = read('cdc.rpt')
    cdc_rows = re.findall(r'^CDC-\d+\s+(Info|Warning|Critical)\s+(\d+)\s+', cdc, re.M)
    if not cdc_rows:
        raise ValueError('CDC summary missing')
    critical = sum(int(count) for severity, count in cdc_rows if severity == 'Critical')
    checks = read('check_timing.rpt')
    unconstrained = int(require(r'checking unconstrained_internal_endpoints\s*\((\d+)\)', checks, 'unconstrained endpoints')[1])
    no_clock = int(require(r'checking no_clock\s*\((\d+)\)', checks, 'no clock')[1])
    timing_checks = {}
    for check in ('constant_clock', 'pulse_width_clock', 'multiple_clock', 'generated_clocks', 'loops', 'partial_input_delay', 'partial_output_delay', 'latch_loops'):
        counts = [int(value) for value in re.findall(r'checking ' + check + r'\s*\((\d+)\)', checks)]
        if not counts or len(set(counts)) != 1:
            raise ValueError('Missing or contradictory check_timing: ' + check)
        timing_checks[check] = counts[0]
    route_status = read('route_status.rpt')
    routable = int(require(r'# of routable nets\.+\s*:\s*(\d+)', route_status, 'routable count')[1])
    fully_routed = int(require(r'# of fully routed nets\.+\s*:\s*(\d+)', route_status, 'fully routed count')[1])
    route_errors = int(require(r'nets with routing errors\.+\s*:\s*(\d+)', read('route_status.rpt'), 'route errors')[1])
    drc = read('drc.rpt')
    require(r'Report DRC', drc, 'DRC report')
    require(r'1\. REPORT SUMMARY', drc, 'DRC summary')
    require(r'2\. REPORT DETAILS', drc, 'DRC details')
    drc_total = int(require(r'Checks found:\s*(\d+)', drc, 'DRC checks total')[1])
    drc_rows = re.findall(r'^\|\s*[^|]+\|\s*(Error|Critical Warning|Warning|Advisory|Info)\s*\|[^|]+\|\s*(\d+)\s*\|', drc, re.M)
    if drc_total and not drc_rows:
        raise ValueError('Missing DRC severity rows')
    drc_sum = sum(int(count) for _, count in drc_rows)
    drc_errors = sum(int(count) for severity, count in drc_rows if severity in ('Error', 'Critical Warning'))
    util = read('utilization_route.rpt')
    top = require(r'^\|\s*structured_light_motion_profile_hdmi_top\s*\|[^\n]+', util, 'top utilization')[0]
    columns = [item.strip() for item in top.split('|')[1:-1]]
    # Hierarchical report columns: Instance, Module, Total LUT, Logic, LUTRAM, SRL, FF, RAMB36, RAMB18, DSP.
    lut, ff, ramb36, ramb18, dsp = (int(columns[i].replace(',', '')) for i in (2, 6, 7, 8, 9))
    # A bounded seed retry wraps the same direction solver in one more level.
    lane_rows = re.findall(r'^\|\s*direction_lanes\[(\d+)\]\.(?:bounded_retry\.)?core\s*\|[^\n]+', util, re.M)
    lane_ids = sorted(int(index) for index in lane_rows)
    architecture_ok = expected_lanes is None or (lane_ids == list(range(expected_lanes)) and dsp == 36 + 14*expected_lanes)
    percentages = dict(lut=100*lut/53200, ff=100*ff/106400, bram=100*(ramb36+ramb18/2)/140, dsp=100*dsp/220)
    gates = dict(architecture=architecture_ok, setup=wns>0 and tns==0 and setup_fail==0,
                 hold=whs>0 and ths==0 and hold_fail==0,
                 pulse=wpws>=0 and tpws==0 and pw_fail==0,
                 cdc=critical==0, constrained=unconstrained==0 and no_clock==0,
                 route=route_errors==0 and routable>0 and fully_routed==routable,
                 clock_structure=all(count==0 for count in timing_checks.values()),
                 drc=drc_errors==0 and drc_sum==drc_total,
                 budget=all(value<=80 for value in percentages.values()))
    return dict(evidence_kind='FULL_POST_ROUTE_REPORT_REVIEW_NOT_BOARD', passed=all(gates.values()), gates=gates,
                wns_ns=wns, whs_ns=whs, wpws_ns=wpws, tns_ns=tns, ths_ns=ths, tpws_ns=tpws,
                cdc_critical=critical, unconstrained_internal_endpoints=unconstrained, no_clock=no_clock,
                route_errors=route_errors, routable_nets=routable, fully_routed_nets=fully_routed,
                timing_checks=timing_checks, drc_errors=drc_errors, drc_total=drc_total, drc_summary_sum=drc_sum,
                expected_lanes=expected_lanes, direction_lane_ids=lane_ids,
                resources=dict(lut=lut, ff=ff, ramb36=ramb36, ramb18=ramb18, dsp=dsp),
                percentages=percentages, target_note='70-80 percent is an exploration band per resource, not a utilization average or minimum.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-lanes', type=int, choices=(2, 4, 8))
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Output exists; do not overwrite admission evidence')
    try:
        result = review(args.reports, args.expected_lanes)
    except Exception as exc:
        result = dict(passed=False, error=type(exc).__name__ + ': ' + str(exc))
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))
    raise SystemExit(0 if result['passed'] else 2)


if __name__ == '__main__':
    main()
