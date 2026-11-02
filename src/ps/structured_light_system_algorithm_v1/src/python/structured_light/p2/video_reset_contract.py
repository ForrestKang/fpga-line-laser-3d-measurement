"""Static HDMI reset handoff check; no imports or operations on PYNQ hardware."""
from pathlib import Path
import hashlib
import xml.etree.ElementTree as ET


def verify_video_reset_handoff(path):
    path = Path(path)
    raw = path.read_bytes()
    modules = {node.get('FULLNAME'): node for node in ET.fromstring(raw).iter('MODULE')}
    reset = modules.get('/hdmi_out/video_pixel_reset')
    constant = modules.get('/hdmi_out/video_reset_clear')
    if reset is None or constant is None:
        raise ValueError('HWH lacks exact HDMI pixel reset/constant modules')
    parameters = {p.get('NAME'): p.get('VALUE') for p in reset.iter('PARAMETER')}
    levels = {p.get('NAME'): p.get('VALUE') for p in constant.iter('PARAMETER')}
    if (parameters.get('C_EXT_RESET_HIGH') != '0' or
            parameters.get('C_AUX_RESET_HIGH') != '0' or
            parameters.get('C_EXT_RST_WIDTH') != '4' or
            parameters.get('C_AUX_RST_WIDTH') != '4' or
            int(levels.get('CONST_VAL', '-1'), 0) != 1 or
            levels.get('CONST_WIDTH') != '1'):
        raise ValueError('HWH HDMI pixel reset polarity/constant level mismatch')
    ports = {p.get('NAME'): p for p in reset.iter('PORT')}
    expected = {
        'ext_reset_in': ('hdmi_out_video_reset_clear', 'dout'),
        'dcm_locked': ('hdmi_out_axi_dynclk', 'LOCKED_O'),
        'slowest_sync_clk': ('hdmi_out_axi_dynclk', 'PXL_CLK_O'),
        'peripheral_aresetn': ('hdmi_out_vtc_out', 'resetn'),
        'peripheral_reset': ('hdmi_out_v_axi4s_vid_out_0', 'vid_io_out_reset'),
    }
    for name, connection in expected.items():
        port = ports.get(name)
        if port is None or connection not in {(c.get('INSTANCE'), c.get('PORT')) for c in port.iter('CONNECTION')}:
            raise ValueError('HWH HDMI reset connection mismatch: ' + name)
    return {'hwh_sha256': hashlib.sha256(raw).hexdigest(), 'passed': True,
            'external_reset_active_high': False, 'inactive_external_level': 1,
            'external_reset_width': 4, 'aux_reset_width': 4,
            'clock_and_lock_source': 'hdmi_out/axi_dynclk',
            'scope': 'static handoff wiring/parameter contract; not a physical display proof'}


def verify_video_reset_simulation(root):
    """Require current actual-vendor logs, not a previously passing surrogate."""
    import json
    root = Path(root)
    path = root / 'sim/results/p2/hdmi_pixel_reset/summary.json'
    record = json.loads(path.read_text(encoding='utf-8'))
    contract = record.get('contract', {})
    expected = {'fixed_ext_reset_in': 1, 'fixed_c_ext_reset_high': 0,
                'fixed_c_ext_rst_width': 4, 'fixed_c_aux_rst_width': 4,
                'legacy_negative_ext_reset_in': 0, 'legacy_negative_c_ext_reset_high': 0}
    if (not record.get('passed') or not record.get('source_unchanged') or
            any(contract.get(k) != v for k, v in expected.items()) or
            record.get('vendor_model', {}).get('library') != 'proc_sys_reset_v5_0_17'):
        raise ValueError('Actual vendor HDMI reset simulation is missing or mismatched')
    for relative, fingerprint in record['source_sha256_before'].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != fingerprint:
            raise ValueError('Stale HDMI reset simulation source: ' + relative)
    simulation = record['simulation']
    if (simulation.get('missing_markers') or simulation.get('failure_tokens') or
            len(simulation.get('observed_markers', [])) != 9 or
            set(simulation['observed_markers']) != set(simulation.get('expected_markers', [])) or
            {item['tool'] for item in simulation['commands']} != {'xvhdl', 'xvlog', 'xelab', 'xsim'}):
        raise ValueError('Incomplete HDMI reset simulation markers')
    for library_file, fingerprint in record['vendor_model']['files'].items():
        if hashlib.sha256(Path(library_file).read_bytes()).hexdigest() != fingerprint:
            raise ValueError('Vendor reset model fingerprint mismatch')
    for item in simulation['commands']:
        if item['returncode'] or item.get('timed_out') or hashlib.sha256((root / item['log']).read_bytes()).hexdigest() != item['log_sha256']:
            raise ValueError('Invalid HDMI reset tool execution or log')
    if hashlib.sha256((root / simulation['vcd']).read_bytes()).hexdigest() != simulation['vcd_sha256']:
        raise ValueError('HDMI reset VCD fingerprint mismatch')
    return {'passed': True, 'summary_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'run_id': record['run_id'], 'contract': contract,
            'scope': 'current actual vendor functional simulation; physical display remains separate'}
