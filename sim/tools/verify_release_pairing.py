"""Offline firmware/source/ABI checks. Does not load hardware or rewrite a manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def native_library_check(directory, source, output):
    directory = Path(directory)
    receipt = json.loads((directory / (output + '.json')).read_text(encoding='utf-8'))
    raw = (directory / output).read_bytes()
    elf_arm32_le = (len(raw) >= 52 and raw[:7] == b'\x7fELF\x01\x01\x01'
                    and int.from_bytes(raw[16:18], 'little') == 3
                    and int.from_bytes(raw[18:20], 'little') == 40)
    checks = dict(source=sha(directory / source) == receipt['source_sha256'],
                  output=sha(directory / output) == receipt['output_sha256'],
                  size=len(raw) == receipt['output_bytes'], output_name=receipt['output'] == output,
                  declared_architecture=receipt['machine'] == 'armv7l', elf_arm32_le=elf_arm32_le)
    return dict(passed=all(checks.values()), checks=checks, source=source, output=output)


def verify(root):
    root = Path(root).resolve()
    fw = root / 'board/firmware'
    manifest = json.loads((fw / 'manifest.json').read_text(encoding='utf-8'))
    checks = []
    def check(name, passed, **detail):
        checks.append(dict(name=name, passed=bool(passed), **detail))
    for kind in ('bit', 'hwh'):
        entry = manifest['files'][kind]
        path = fw / entry['path']
        check(kind, path.is_file() and path.stat().st_size == entry['size']
              and sha(path) == entry['sha256'])
    compiled = json.loads((fw / 'compiled_sources_sha256.json').read_text(encoding='utf-8'))
    original, generated, failed = [], [], []
    for name, expected in compiled.items():
        rel = name.replace('\\', '/').split('/scan3d_final_20261029/', 1)[-1]
        path = root / 'src/fpga' / rel.lower()
        if path.is_file():
            (original if sha(path) == expected else failed).append(rel)
        elif any(part in rel for part in ('.gen/', '.srcs/', '/ipshared/')):
            generated.append(rel)
        else:
            failed.append(rel)
    check('compiled_source_closure', not failed and len(original) == 45 and len(generated) == 117,
          original_count=len(original), generated_count=len(generated), failed=failed)
    # This file is the content-hash map, not the separate receipt metadata.
    check('compiled_content_binding', sha(fw / 'compiled_sources_sha256.json')
          == manifest['compiled_source_content_sha256'])
    ranges = [e.attrib for e in ET.parse(fw / 'final.hwh').iter() if e.tag == 'MEMRANGE']
    check('frontend_hwh_address', any(e.get('INSTANCE') == 'pfo3_frontend'
          and int(e['BASEVALUE'], 16) == manifest['pfo3']['frontend_base']
          and int(e['HIGHVALUE'], 16) - int(e['BASEVALUE'], 16) + 1 >= 4096 for e in ranges))
    rtl = (root / 'src/fpga/rtl/r35_axi.sv').read_text(encoding='utf-8')
    section = (root / 'src/fpga/rtl/section_measure.sv').read_text(encoding='utf-8')
    section_decl = manifest['section_measure']
    check('section_register_contract', section_decl['present'] and section_decl['register_window'] == [0x700, 0x7ff]
          and section_decl['magic'] == 0x5345434d and section_decl['version'] == 1
          and 'awaddr[11:8]==7' in rtl and 's_axi_araddr[11:8]==7' in rtl
          and 'section_measure section(' in rtl and bool(re.search(r"5345434[dD]", section)))
    check('two_direction_lanes', manifest['pfo3']['n_lanes'] == 2)
    for source, output in [('assemble_r33.c', 'libassemble_r33.so'), ('markers_r34.c', 'libmarkers_r34.so')]:
        native = native_library_check(root / 'src/ps/r33/native', source, output)
        check('arm_native_' + output, native['passed'], native=native)
    return dict(schema='OFFLINE_RELEASE_PAIRING_V1', passed=all(c['passed'] for c in checks),
                checks=checks, evidence_class='SOURCE_AND_ABI_NOT_BOARD',
                hardware_commands=0, limits=['Generated Vivado IP is regenerated during build.',
                'Source/ABI checks do not prove a bitstream is currently loaded or physical accuracy.',
                'Runtime guards must be checked separately after software regression.'])


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument('--output', type=Path)
    args = ap.parse_args()
    result = verify(args.root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
