"""R40 full-size AXI RTL simulation (Icarus), not a board run.

R40 additions over the R39 script below: the PL cloud view is compiled in with its
ingest enabled; (a) every case's primary stream must be byte-identical to the R39
integration evidence (the view only snoops), and (b) the view's ingest counters must
equal model/point_cloud_model.py applied to that case's own geometry records.

R39 docstring follows. Adapted from scan3d_r36_frontend_timing_20261025_01/variants/front02/scripts/
frontend_integration_verify.py. Differences:
  * RTL: the R39 workspace sources (row-local overflow);
  * expected R33 record kinds 0..2 are computed by reference/reference_r39.py for each
    input image instead of read from frozen .out files;
  * the legacy packet is assembled by the R39 ARM decoder (runtime/r33/protocol.py);
  * mixed cases splice overflowing rows of the synthetic slots17 image into T05_000,
    and the frozen R33 reference reports how many points R37 semantics would keep.
Geometry, moments and footer checks are unchanged.
"""
from pathlib import Path
import hashlib, importlib.util, json, os, shutil, struct, subprocess, sys, time
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=False)
VERIFY = ROOT / 'vendor/verification'
R36 = Path('D:/Workspace/CodexWork/scan3d_r36_frontend_timing_20261025_01/variants/front02')
sys.path.insert(0, str(VERIFY / 'r36_runtime' if (VERIFY / 'r36_runtime').is_dir() else R36 / 'runtime'))
import geometry_reference as geo  # noqa: E402
RUNTIME = ROOT / 'runtime12'
sys.path[:0] = [str(RUNTIME), str(RUNTIME / 'structured_light_system_geometry_v1/src/python')]
from r33.geometry import load_calibration  # noqa: E402
from r33.protocol import HEADER_FIELDS, FINGERPRINT, assemble_reference, identity  # noqa: E402
sys.path.insert(0, str(ROOT / 'reference'))
import reference_r39 as ref  # noqa: E402
import reference_r41_bin2 as ref_bin  # noqa: E402
_rt11_pkg = importlib.util.spec_from_file_location('r33_rt11', ROOT / 'runtime12' / 'r33' / '__init__.py',
                                                   submodule_search_locations=[str(ROOT / 'runtime12' / 'r33')])
_rt11_mod = importlib.util.module_from_spec(_rt11_pkg); sys.modules['r33_rt11'] = _rt11_mod
_rt11_pkg.loader.exec_module(_rt11_mod)
p11 = importlib.import_module('r33_rt11.protocol')   # runtime11 decoder: admits the binning mode
BIN_FINGERPRINT = p11.BIN_FINGERPRINT
LIVE = {'live03_240': 'r37_r41f_live03/raw_000240.gray8', 'live02_180': 'r37_r41f_live02/raw_000180.gray8'}
BOARD = VERIFY / 'board'
FROZEN_REF = VERIFY / 'r33/direction_tests/reference.py'
spec = importlib.util.spec_from_file_location('frozen_r33_reference', FROZEN_REF)
old_ref = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old_ref)

SIM = VERIFY / 'sim'
RAM = ROOT / 'vendor/r31_baseline_copy/source_tree/pl/r25_next/rtl'
IVER = Path('D:/Tools/structured_light_iverilog/bin')
sources = [ROOT / 'rtl/shared_window_filter.sv', ROOT / 'baseline_rtl/ridge_center_solver.sv',
           ROOT / 'rtl/direction_refiner.sv', ROOT / 'rtl/stripe_extractor.sv', ROOT / 'rtl/pixel_frontend.sv',
           ROOT / 'rtl/board_buttons.sv', ROOT / 'rtl/axi_packet_controller.sv', ROOT / 'rtl/geometry_engine.sv', ROOT / 'rtl/geometry_multiplier.sv',
           ROOT / 'rtl/fixed_point_divider.sv', ROOT / 'rtl/point_cloud_cache.sv',
           RAM / 'pfo3_tdp_ram.sv', RAM / 'pfo3_banked_ram.sv',
           ROOT / 'rtl/point_cloud_renderer.sv', ROOT / 'rtl/point_cloud_memory.sv', ROOT / 'rtl/point_cloud_tables.sv',
           ROOT / 'tests/xpm_stubs_sim.v', ROOT / 'tests/integration_axi_tb.sv', ROOT / 'rtl/profile_statistics.sv']
R39_EVIDENCE = VERIFY / 'R39_reference'
sys.path.insert(0, str(ROOT / 'model'))
import point_cloud_model as view_model  # noqa: E402
os.environ.update(TEMP='D:/Workspace/CodexTemp/r40_integration', TMP='D:/Workspace/CodexTemp/r40_integration')
Path(os.environ['TEMP']).mkdir(parents=True, exist_ok=True)
(OUT / 'sources').mkdir()
source_sha = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
for p in sources:
    shutil.copy2(p, OUT / 'sources' / p.name)


def run(args, log, timeout=3600):
    with (OUT / log).open('w') as f:
        subprocess.run(list(map(str, args)), cwd=OUT, stdout=f, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def load_hex(path):
    return np.array([int(v, 16) for v in Path(path).read_text().split()], dtype=np.uint8).reshape(480, 752)


def mixed_case(name, rows):
    """T05_000 with the given rows (and their 3x3 neighbourhood source) taken from slots17."""
    image = load_hex(SIM / 'T05_000.hex').copy()
    comb = load_hex(SIM / 'slots17.hex')
    for y in rows:
        image[y] = comb[y]
    path = OUT / (name + '.hex')
    path.write_text('\n'.join(f'{v:02x}' for v in image.ravel()) + '\n')
    return path


def r39_without_moments(w):
    """R39 primary stream with its kind 4/5 moment records removed (b08 comparison)."""
    body = [w[i:i + 17] for i in range(64, len(w) - 32, 17)]
    kept = [r for r in body if r[0] & 255 not in (4, 5)]
    footer = list(w[-32:])
    footer[24] = 64 + 17 * len(kept) + 32
    footer[30] = 0
    return list(w[:64]) + [x for r in kept for x in r] + footer


def words16(lists):
    return [list(r) for r in lists]


frozen_sources = [OUT / 'sources' / p.name for p in sources]
run([IVER / 'iverilog.exe', '-g2012', '-s', 'integration_axi_tb', '-I', RAM, '-o', OUT / 'sim.vvp', *frozen_sources], 'compile.log')


def signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def moment_reference(records):
    bins = [{'count': 0, 'sum': [0] * 3, 'products': [0] * 6, 'min': None, 'max': None} for _ in range(64)]
    for r in records:
        if not r[7] & 256 or not r[7] & 1:
            continue
        xyz = [signed(n, 32) for n in r[4:7]]
        b = (xyz[2] + 65536) >> 16
        if not 0 <= b < 64:
            continue
        d = bins[b]
        d['count'] += 1
        for j in range(3):
            d['sum'][j] += xyz[j]
        x, y, z = xyz
        for j, n in enumerate((x * x, y * y, z * z, x * y, x * z, y * z)):
            d['products'][j] += n
        d['min'] = xyz if d['min'] is None else [min(a, b) for a, b in zip(d['min'], xyz)]
        d['max'] = xyz if d['max'] is None else [max(a, b) for a, b in zip(d['max'], xyz)]
    output = []
    for d in bins:
        v = d['count']
        for j, n in enumerate(d['sum']):
            v |= (n & ((1 << 64) - 1)) << (64 + 64 * j)
        for j, n in enumerate(d['products']):
            v |= (n & ((1 << 96) - 1)) << (256 + 96 * j)
        for j in range(3):
            v |= ((d['min'][j] if d['min'] else 0) & 0xffffffff) << (832 + 64 * j)
            v |= ((d['max'][j] if d['max'] else 0) & 0xffffffff) << (864 + 64 * j)
        output.extend([v & ((1 << 512) - 1), v >> 512])
    return output, sum(d['count'] for d in bins)


cases = sys.argv[2:] or ['T05_000', 'T06_000', 'empty', 'saturated', 'noise', 'slots17',
                         'mix_ovf_mid', 'mix_ovf_edge', 'live03_240', 'live02_180',
                         'T05_000:bin', 'T06_000:bin', 'empty:bin', 'saturated:bin', 'noise:bin', 'slots17:bin',
                         'mix_ovf_mid:bin', 'live03_240:bin', 'live02_180:bin']
mixed = {'mix_ovf_mid': range(200, 204), 'mix_ovf_edge': (1, 2, 477, 478)}
results = []
for case_name in cases:
    base_case, binned = case_name.split(':')[0], case_name.endswith(':bin')
    case = base_case + ('_bin' if binned else '')
    if base_case in LIVE:
        input_hex = OUT / (base_case + '_input.hex')
        if not input_hex.exists():
            raw = np.fromfile(BOARD / LIVE[base_case], np.uint8)
            input_hex.write_text('\n'.join('%02x' % v for v in raw) + '\n')
    else:
        input_hex = mixed_case(base_case, mixed[base_case]) if base_case in mixed else SIM / (base_case + '.hex')
    image = load_hex(input_hex)
    target = base_case[:3] if base_case.startswith(('T05', 'T06')) else 'T05'
    cal = load_calibration(VERIFY / 'calibrations' / target / 'calibration.json')
    params = geo.parameters(cal, .37)
    config_sha = hashlib.sha256(b''.join((n & ((1 << 64) - 1)).to_bytes(8, 'little') for n in params)).digest()
    sha_words = list(struct.unpack('<8I', config_sha))
    cfg = OUT / (case + '_config.hex')
    cfg.write_text('\n'.join(f'{n & ((1 << 64) - 1):016x}' for n in params + sha_words) + '\n')
    header = {key: 0 for key in HEADER_FIELDS}
    header.update(source=2, frame_id=35, epoch=35, cfg_version=1, exposure_version=1,
                  motor_id=35, motor_epoch=35, motor_steps_lo=123, FV_ticks_lo=456,
                  exposure_start_lo=400, exposure_end_lo=450, width=752, height=480,
                  roi_x0=0, roi_y0=0, roi_x1=752, roi_y1=480, roi_pixels=752 * 480,
                  algorithm_revision=33, fixed_revision=1, policy_revision=2 if binned else 1)
    for j, n in enumerate(struct.unpack('<8I', bytes.fromhex(BIN_FINGERPRINT if binned else FINGERPRINT))):
        header['fingerprint' + str(j)] = n
    hdr = OUT / (case + '_header.hex')
    hdr.write_text('\n'.join(f'{header[key]:08x}' for key in HEADER_FIELDS) + '\n')
    t = time.monotonic()
    stream = OUT / (case + '_stream.hex')
    run([IVER / 'vvp.exe', OUT / 'sim.vvp', '+INPUT=' + str(input_hex), '+CONFIG=' + str(cfg),
         '+HEADER=' + str(hdr), '+OUTPUT=' + str(stream)], case + '.log')
    log = (OUT / (case + '.log')).read_text()
    assert 'TEST_PASS' in log and 'ERROR:' not in log, case
    words = [int(n, 16) for n in stream.read_text().split()]
    primary = struct.pack('<' + str(len(words)) + 'I', *words)
    (OUT / (case + '_primary.bin')).write_bytes(primary)
    r41_exact = VERIFY / 'R41_14_reference' / (case + '_primary.bin')
    if not binned and case not in LIVE:
        assert r41_exact.is_file(), f'R41_14 reference missing: {r41_exact}'
        assert primary == r41_exact.read_bytes(), f'R42 changed R41_14 primary bytes: {case}'

    assert words[:4] == [0x35544850, 35, 752, 480]
    footer = words[-32:]
    assert footer[:2] == [0x35544650, 35] and footer[24] == len(words) and footer[17] == 0, case
    groups = {k: [] for k in range(7)}
    body = words[64:-32]
    assert len(body) % 17 == 0
    for i in range(0, len(body), 17):
        tag = body[i]
        assert tag & 0xffffff00 == 0x35520000
        groups[tag & 255].append(body[i + 1:i + 17])
    # Legacy R33 packet through the R39 ARM decoder (row-local overflow rule).
    legacy = words[:64].copy()
    legacy[:2] = [0x33544850, 33]
    for i in range(0, len(body), 17):
        if body[i] & 255 < 3:
            legacy.extend([0x33520000 | (body[i] & 255)] + body[i + 1:i + 17])
    oldfooter = footer.copy()
    oldfooter[:2] = [0x33544650, 33]
    oldfooter[24] = len(legacy) + 32
    legacy.extend(oldfooter)
    if binned:
        archive, receipt = p11.assemble_reference(struct.pack('<' + str(len(legacy)) + 'I', *legacy),
                                                  expected_identity=identity(header), binning=True,
                                                  expected_fingerprint=BIN_FINGERPRINT)
    else:
        archive, receipt = assemble_reference(struct.pack('<' + str(len(legacy)) + 'I', *legacy),
                                              expected_identity=identity(header))
    assert groups[6] == [[35] + sha_words + [(-65536) & 0xffffffff, 16, 1, 2, 0, 0, 0]], case
    # Expected R33 kinds from the R39 reference.
    if binned:
        records_b, rows = ref.candidates(ref_bin.bin2(image))
        patches = ref.graph_patches(records_b, rows)
        final = ref.final_rows(ref_bin.bin2(image), records_b, rows, patches)
        records = ref_bin.to_full(records_b)
    else:
        records, rows = ref.candidates(image)
        patches = ref.graph_patches(records, rows)
        final = ref.final_rows(image, records, rows, patches)
    for k, expected in ((0, records), (1, patches), (2, final)):
        if groups[k] != words16(expected):
            bad = next((i for i, (a, b) in enumerate(zip(groups[k], expected)) if list(a) != list(b)), None)
            raise AssertionError((case, 'R39 kind mismatch', k, len(groups[k]), len(expected), bad))
    diagnostics = {r[0]: r for r in groups[0]}
    expected_geo = []
    for patch in groups[1]:
        modes = ((patch[13] >> 25) & 1) * bool(footer[25]) | (((patch[13] >> 26) & 1) * bool(footer[26]) << 1)
        if modes:
            r = diagnostics[patch[0]]
            expected_geo.append(geo.reconstruct(params, r[4], r[5], r[0], modes))
    actual_geo = [r[:8] for r in groups[3]]
    assert all(r[8:] == [0] * 8 for r in groups[3]), case
    assert actual_geo == expected_geo, (case, 'geometry mismatch', len(actual_geo), len(expected_geo))
    assert footer[29] == len(actual_geo)
    moments = []
    # b08: moment_accumulator removed; no kind 4/5 records, footer moment words zero.
    assert groups[4] == groups[5] == [] and footer[30] == 0 and footer[31] == 0, case
    count = 0
    # R40 (a): PFO3 output unchanged by the snooping view. The only words allowed to
    # differ are footer 14/15 (processing_cycles): the R40 bench spends extra cycles on
    # view setup before START, which shifts its pseudo-random output back-pressure.
    r39_file = R39_EVIDENCE / (case + '_primary.bin')
    if not binned and r39_file.exists():
        r39_bytes = r39_file.read_bytes()
        r39_words = list(struct.unpack('<%dI' % (len(r39_bytes) // 4), r39_bytes))
        r39_words = r39_without_moments(r39_words)
        assert len(r39_words) == len(words), (case, 'primary length differs from R39 without moments')
        cycle_words = {len(words) - 32 + 14, len(words) - 32 + 15}
        differing = [i for i, (x, y) in enumerate(zip(words, r39_words)) if x != y]
        assert set(differing) <= cycle_words, (case, 'primary differs from R39 beyond cycle counter', differing[:8])
        r39_cycles, compared = r39_words[-32 + 14], 'R39 minus moment records'
    else:
        r39_cycles, compared = None, ('binning reference (reference_r41_bin2)' if binned
                                      else 'R39 reference only (no R39 board-sim evidence for this input)')
    # R40 (b): view ingest counters against the model on this case's geometry records.
    ingest = view_model.Ingest(dict(view_model.DEFAULTS, ctrl=0x2))
    for r in actual_geo:
        ingest.point(r)
    ingest.end_packet(True)
    status_line = next(line for line in log.splitlines() if line.startswith('VIEW_STATUS'))
    view = {k: int(v) for k, v in (kv.split('=') for kv in status_line.split()[1:])}
    expected_view = dict(inserted=ingest.inserted, dropped=ingest.dropped, committed=ingest.frames_committed,
                         discarded=ingest.frames_discarded, occupied=ingest.grid.occupied,
                         out_of_range=ingest.out_of_range)
    assert view == expected_view, (case, 'view ingest mismatch', view, expected_view)
    # R41 measurement statistics against the independent model on this case's geometry records.
    sys.path.insert(0, str(ROOT / 'scripts'))
    from r41_measure_verify import Model as MeasureModel
    mm = MeasureModel()
    for a, d in ((0x08, 0), (0x0C, 18), (0x10, -4194304), (0x14, 17), (0x04, 3)):
        mm.write(a, d & 0xffffffff)
    for r in groups[3]:
        mm.point(r[:8])
    m_regs, m_hout, m_bands = mm.commit()
    m_line = next(l for l in log.splitlines() if l.startswith('MEASURE_R')).split()[1:]
    m_got = list(map(int, m_line))
    assert m_got[:16] == m_regs and m_got[16] == 0 and m_got[17] == m_hout, (case, 'measure regs', m_got, m_regs)
    for l in (l for l in log.splitlines() if l.startswith('MEASURE_B')):
        row = [int(x) if x.lstrip('-').isdigit() else -1 for x in l.split()[1:]]
        want = m_bands[row[0]]
        assert row[1] == want['n'] and (row[6] | row[7] << 32) == want['sm'], (case, 'measure band', row, want)
        if want['n']:
            assert (row[2] | row[3] << 32) == want['mx'] and (row[4] | row[5] << 32) == want['mn'], (case, 'band max/min')
    measure = dict(points=m_regs[1], median_bin=m_regs[6] & 255, median_valid=m_regs[6] >> 31,
                   bands_used=sum(1 for b in m_bands if b['n']))
    old_patches = old_ref.graph_patches(records_b if binned else records, rows)
    accepted = lambda ps, bit: sum((p[13] >> bit) & 1 for p in ps)
    result = dict(case=case, overflow_rows=sum(r['qualified'] > 16 for r in rows),
                  max_qualified=max(r['qualified'] for r in rows),
                  r39_measurement=accepted(patches, 25), r39_preview=accepted(patches, 26),
                  r37_semantics_measurement=accepted(old_patches, 25),
                  r37_semantics_preview=accepted(old_patches, 26),
                  frame_measurement_usable=footer[25], frame_preview_usable=footer[26],
                  geometry_records=len(actual_geo), moments_count=count,
                  R39_records_exact=True, geometry_integer_exact=True, moments_integer_exact=True,
                  arm_decoder_r39_accepts=True, binning=binned, primary_equals_r39_except_cycles=r39_cycles is not None,
                  primary_compared_to=compared,
                  processing_cycles_r40_r39=[words[-32 + 14], r39_cycles], view_ingest=view, measure=measure,
                  primary_sha256=hashlib.sha256(primary).hexdigest(), wall_s=round(time.monotonic() - t, 1))
    results.append(result)
    (OUT / 'PROGRESS.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(result), flush=True)
(OUT / 'RESULT.json').write_text(json.dumps(dict(passed=True, cases=results, source_sha256=source_sha,
    reference_sha256=hashlib.sha256((ROOT / 'reference/reference_r39.py').read_bytes()).hexdigest(),
    scope='full-size R40 RTL AXI simulation: R39 reference/decoder, primary == R39 evidence, '
          'view ingest == point_cloud_model; not a board run'), indent=2))
print('R40_INTEGRATION_PASS', flush=True)
