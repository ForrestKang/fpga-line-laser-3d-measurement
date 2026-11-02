"""Read archived live evidence only; never accesses hardware or changes source files.

Example: python scripts/summarize_static_final.py --run archive/run --monitor
archive/run_monitor --output evidence/static_summary_new.json
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

RAW_BYTES = 752 * 480
RGB_BYTES = 1280 * 720 * 3


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def distribution(values):
    if not values:
        return {'count': 0, 'p50': None, 'p95': None, 'maximum': None}
    ordered = sorted(values)
    def percentile(q):
        index = (len(ordered) - 1) * q
        lo = int(index)
        return ordered[lo] + (ordered[min(lo + 1, len(ordered) - 1)] - ordered[lo]) * (index - lo)
    return {'count': len(values), 'p50': percentile(.5), 'p95': percentile(.95), 'maximum': ordered[-1]}


def windows(start, end, processed, displayed):
    """Complete non-overlapping windows, left inclusive and right exclusive."""
    if start is None or end is None or end <= start:
        return []
    result = []
    for i in range(int((end - start) // 5)):
        a, b = start + i * 5, start + (i + 1) * 5
        n, d = sum(a <= t < b for t in processed), sum(a <= t < b for t in displayed)
        result.append({'start_monotonic': a, 'end_monotonic': b,
                       'new_processed_frames': n, 'new_displayed_frames': d,
                       'processed_hz': n / 5, 'displayed_hz': d / 5})
    return result


def resources(samples, start, end, issues):
    result = {'cpu_definition': 'whole-board aggregate, both cores together 100%; busy excludes idle and iowait',
              'overlap_method': 'sample CPU interval fractions weighted by overlap duration; not per-process CPU',
              'busy_percent': None, 'iowait_percent': None, 'covered_s': 0., 'coverage_fraction': None,
              'sampled_min_available_mib': None, 'sampled_min_cma_mib': None,
              'memory_samples_in_activity': 0, 'maximum_sample_gap_s': None}
    if start is None or end is None or end <= start:
        return result
    valid = []
    for sample in samples:
        ticks = sample.get('cpu_ticks')
        if not (number(sample.get('monotonic_s')) and isinstance(ticks, list) and len(ticks) == 8
                and all(number(v) and v >= 0 for v in ticks)):
            issues.append('invalid resource sample')
            continue
        valid.append(sample)
    busy, io, covered, gaps = 0., 0., 0., []
    for a, b in zip(valid, valid[1:]):
        left, right = a['monotonic_s'], b['monotonic_s']
        if right <= left:
            issues.append('resource sample time not increasing')
            continue
        overlap = max(0., min(right, end) - max(left, start))
        if not overlap:
            continue
        delta = [y - x for x, y in zip(a['cpu_ticks'], b['cpu_ticks'])]
        total = sum(delta)
        if min(delta) < 0 or total <= 0:
            issues.append('resource CPU counters reset or did not advance')
            continue
        busy += overlap * 100 * (total - delta[3] - delta[4]) / total
        io += overlap * 100 * delta[4] / total
        covered += overlap
        gaps.append(right - left)
    if covered:
        result.update(busy_percent=busy / covered, iowait_percent=io / covered,
                      covered_s=covered, coverage_fraction=covered / (end - start),
                      maximum_sample_gap_s=max(gaps))
    inside = [s for s in valid if start <= s['monotonic_s'] <= end]
    result['memory_samples_in_activity'] = len(inside)
    for key, target in (('mem_available_kib', 'sampled_min_available_mib'), ('cma_free_kib', 'sampled_min_cma_mib')):
        values = [s[key] for s in inside if number(s.get(key)) and s[key] >= 0]
        if values:
            result[target] = min(values) / 1024
    return result


def summarize(run, monitor):
    run, monitor = Path(run), Path(monitor)
    issues, sources = [], []
    def load(path, expected):
        if not path.is_file() or path.is_symlink():
            issues.append('missing or non-regular source: ' + str(path))
            return expected()
        sources.append({'path': str(path.resolve()), 'bytes': path.stat().st_size, 'sha256': sha(path)})
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(value, expected):
                raise ValueError('wrong top-level JSON type')
            return value
        except (ValueError, UnicodeError) as error:
            issues.append(str(path) + ': ' + str(error))
            return expected()
    state = load(run / 'summary.json', dict)
    stats = load(run / 'frames.json', list)
    events = load(run / 'ui_display_events.json', list)
    resource = load(monitor / 'RESOURCE_MONITOR.json', dict)
    if state and state.get('phase') != 'stopped':
        issues.append('live did not finish in stopped state')
    if resource and resource.get('returncode') != 0:
        issues.append('camera wrapper returncode not zero')
    if resource and (not resource.get('boot_id') or resource.get('boot_id') != resource.get('boot_after')):
        issues.append('boot identity absent or changed')
    frames, valid_frames, evidence = {}, [], []
    for frame in stats:
        if not isinstance(frame, dict) or not positive_id(frame.get('frame')) or frame['frame'] in frames:
            issues.append('invalid or repeated processed frame ID')
            continue
        fid = frame['frame']
        frames[fid] = frame
        times = [frame.get(k) for k in ('capture_started_monotonic', 'capture_ready_monotonic', 'processed_monotonic')]
        if all(number(v) for v in times) and times == sorted(times):
            valid_frames.append(frame)
        else:
            issues.append('missing or inconsistent monotonic times for frame ' + str(fid))
        check = {'frame': fid}
        for kind, name in (('raw', f'raw_{fid:06d}.gray8'), ('primary', f'primary_{fid:06d}.bin')):
            path = run / name
            item = {'name': name, 'present': path.is_file() and not path.is_symlink(), 'verified': False}
            if item['present']:
                item.update(bytes=path.stat().st_size, sha256=sha(path))
                if kind == 'raw':
                    item['verified'] = item['bytes'] == RAW_BYTES and item['sha256'] == frame.get('raw_sha256')
                else:
                    expected_size = frame.get('primary_bytes')
                    item['verified'] = positive_id(expected_size) and item['bytes'] == expected_size
            if not item['verified']:
                issues.append(kind + ' saved evidence mismatch for frame ' + str(fid))
            check[kind] = item
        evidence.append(check)
    if state.get('frames') != len(frames):
        issues.append('summary frame count differs from unique processed frames')
    if not frames:
        issues.append('no processed frame records available')
    start = min((f['capture_started_monotonic'] for f in valid_frames), default=None)
    processing_end = max((f['processed_monotonic'] for f in valid_frames), default=None)
    shown, acks, display_times, input_ages, ready_ages, submit_times = set(), set(), [], [], [], []
    snapshots, valid_events, duplicates = [], [], 0
    for event in events:
        if not isinstance(event, dict):
            issues.append('non-object display event')
            continue
        fid, ack, t = event.get('frame'), event.get('ack'), event.get('ack_monotonic')
        frame = frames.get(fid) if positive_id(fid) else None
        if frame is None or not positive_id(ack) or not number(t):
            issues.append('display event lacks known frame, positive VDMA ACK or monotonic timestamp')
            continue
        capture, ready, processed = (frame.get(k) for k in
                                    ('capture_started_monotonic', 'capture_ready_monotonic', 'processed_monotonic'))
        if not all(number(v) for v in (capture, ready, processed)) or t < processed:
            issues.append('display confirmation precedes processing or lacks frame times')
            continue
        if ack in acks:
            issues.append('repeated VDMA acknowledgement ID')
            continue
        acks.add(ack)
        if fid in shown:
            duplicates += 1
        else:
            shown.add(fid)
            display_times.append(t)
            input_ages.append((t - capture) * 1000)
            ready_ages.append((t - ready) * 1000)
            valid_events.append(event)
        for key, actual in (('input_to_ack_ms', (t - capture) * 1000), ('ready_to_ack_ms', (t - ready) * 1000)):
            if not number(event.get(key)) or abs(event[key] - actual) > .01:
                issues.append('logged display age mismatch: frame ' + str(fid) + ' ' + key)
        if number(event.get('display_ms')) and event['display_ms'] >= 0:
            submit_times.append(event['display_ms'])
        if 'snapshot_sha256' in event or 'framebuffer_equal' in event:
            page = event.get('page')
            snap = {'page': page, 'frame': fid, 'framebuffer_equal_logged': event.get('framebuffer_equal'),
                    'scope': 'ARM RGB framebuffer before final PL HDMI overlay', 'verified': False}
            if page in ('A', 'B', 'C'):
                path = run / ('ui_page_' + page + '.rgb')
                if path.is_file() and not path.is_symlink():
                    snap.update(bytes=path.stat().st_size, sha256=sha(path))
                    snap['verified'] = (snap['bytes'] == RGB_BYTES and snap['sha256'] == event.get('snapshot_sha256')
                                        and event.get('framebuffer_equal') is True)
            if not snap['verified']:
                issues.append('ARM framebuffer snapshot mismatch: ' + str(page))
            snapshots.append(snap)
    end = max([processing_end] + display_times) if processing_end is not None else None
    buckets = windows(start, end, [f['processed_monotonic'] for f in valid_frames], display_times)
    changes = state.get('page_changes', [])
    page_changes = {'software_cycle': [], 'physical_button': [], 'unknown': []}
    for change in changes if isinstance(changes, list) else []:
        if not isinstance(change, dict):
            issues.append('non-object page change')
            continue
        kind = change.get('source')
        dest = 'software_cycle' if kind == 'explicit_cycle_test' else 'physical_button' if kind == 'physical_button' else 'unknown'
        page_changes[dest].append(change)
        frame = frames.get(change.get('frame'))
        if (frame is None or change.get('page') not in ('A', 'B', 'C')
                or change.get('before') not in ('A', 'B', 'C')
                or change['page'] == change['before'] or frame.get('page') != change['page']):
            issues.append('page change does not agree with processed-frame page')
        if dest == 'physical_button':
            delta = change.get('button_delta')
            if not isinstance(delta, list) or len(delta) < 4 or not any(number(v) and v > 0 for v in delta[1:4]):
                issues.append('physical page change lacks logged BTN1-3 counter increment')
    resource_summary = resources(resource.get('samples', []), start, end, issues)
    return {'schema': 'FINAL_STATIC_ARCHIVE_SUMMARY_V1', 'evidence_class': 'ARCHIVED_BOARD_FILES_OFFLINE_ANALYSIS',
            'sources': sources, 'issues': issues, 'source_checks_ok': not issues,
            'run_state': {'phase': state.get('phase'), 'returncode': resource.get('returncode'),
                          'boot_id': resource.get('boot_id'), 'boot_after': resource.get('boot_after'),
                          'error': state.get('error'), 'stop_reason': state.get('stop_reason')},
            'shutdown': {key: state.get(key) for key in (
                'threads_alive_after_join', 'thread_errors', 'cleanup_errors',
                'owner_close_results', 'camera_stop_evidence', 'capture_quiescent',
                'hardware_quiescent', 'automatic_retry', 'hardware_lock_limit')},
            'activity': {'start_monotonic': start, 'processing_end_monotonic': processing_end,
                         'end_monotonic': end, 'duration_s': end - start if end is not None else None,
                         'definition': 'first formal capture start through last formal PFO3 return or new-frame display ACK; excludes warmup and subsequent cleanup',
                         'running_start_monotonic_including_warmup': state.get('running_start_monotonic')},
            'frames': {'processed_unique': len(frames), 'timed_processed_unique': len(valid_frames),
                       'displayed_unique_confirmed': len(shown), 'repeated_display_of_same_frame': duplicates,
                       'camera_latest_queue_discarded': (state.get('dropped') or {}).get('capture'),
                       'display_latest_queue_discarded': (state.get('dropped') or {}).get('display'),
                       'processed_raw_primary_all_verified': bool(evidence) and all(e['raw']['verified'] and e['primary']['verified'] for e in evidence),
                       'saved_files': evidence},
            'five_second_windows': buckets,
            'minimum_complete_window_hz': {'processed': min((w['processed_hz'] for w in buckets), default=None),
                                           'new_display': min((w['displayed_hz'] for w in buckets), default=None)},
            'rates_definition': 'non-overlapping complete 5s windows, first formal capture anchored, [start,end); processing is PFO3 return before ARM section/UI/save; distinct frame IDs only',
            'display_age_ms': {'capture_start_to_vdma_ack': distribution(input_ages),
                               'camera_ready_to_vdma_ack': distribution(ready_ages),
                               'percentile_method': 'linear interpolation over distinct confirmed frame IDs'},
            'display_submit_wait_ms': distribution(submit_times),
            'ui_redraw_ms_reported': (state.get('ui') or {}).get('render_ms'),
            'ui_redraw_ms_by_confirmed_page': {
                page: distribution([e['render_ms'] for e in events
                    if isinstance(e, dict) and e.get('page') == page
                    and number(e.get('render_ms')) and e['render_ms'] >= 0])
                for page in ('A', 'B', 'C')},
            'ui_redraw_limit': 'per-page samples cover redraws carried by displayed events; replaced snapshots and cached reuse do not contribute a redraw sample',
            'resources': resource_summary,
            'pages': {'changes': page_changes, 'confirmed_display_pages': sorted({e.get('page') for e in valid_events if e.get('page') in ('A', 'B', 'C')}),
                      'snapshots': snapshots, 'physical_button_test_declared': (state.get('ui') or {}).get('physical_button_test'),
                      'monitor_page_source': resource.get('page_switch_source')},
            'limits': ['No formal durability journal exists in this live entry; file readback is not fsync or crash-recovery proof.',
                       'Raw/primary verification covers processed frames; camera latest-only discards are separate and not saved-frame loss.',
                       'ARM framebuffer readback does not capture final PL overlay or prove physical monitor appearance.',
                       'Software cycle is not physical button evidence; ACK totals alone are not new-model Hz.',
                       'CPU is sampled whole-board load; sampled memory minimum is not a continuous lower-bound guarantee.',
                       'No persistence-window Hz or physical dimension accuracy is inferred.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--monitor', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(args.output)
    for source in (args.run, args.monitor):
        if args.output.resolve().is_relative_to(source.resolve()):
            raise ValueError('summary must be outside both archived source directories')
    result = summarize(args.run, args.monitor)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return 0 if result['source_checks_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
