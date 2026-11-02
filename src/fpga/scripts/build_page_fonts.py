"""Subset the CJK and mono fonts to every character the HDMI pages can draw.

Characters come from the page sources themselves (labels, verdicts, quality hints), plus
printable ASCII. Writes the subsets and a SOURCES file next to the runtime fonts.
"""
import hashlib
import json
from pathlib import Path

from fontTools import subset

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime_scan'   # the runtime the board patch is built from
FONTS = RUNTIME / 'assets/fonts'
PAGE_SOURCES = ['ui_pages.py', 'page_state.py', 'ui_theme.py', 'profile_live.py']
CJK_SOURCE = Path('D:/Workspace/CodexWork/board_archive_20261026/scan3d_r13_ab_20261019T182540Z_c1b7c531'
                  '/source/assets/fonts/NotoSansCJKsc-Regular.otf')
MONO_SOURCE = Path('D:/NUECSDC-FPGA/structured_light_system_algorithm_v1/.venv/Lib/site-packages'
                   '/matplotlib/mpl-data/fonts/ttf/DejaVuSansMono.ttf')
OUTPUTS = {'cjk': 'NotoSansCJKsc-pages-subset.otf', 'mono': 'DejaVuSansMono-pages-subset.ttf'}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def page_chars():
    chars = {chr(c) for c in range(32, 127)}
    for name in PAGE_SOURCES:
        chars |= {c for c in (RUNTIME / 'r33' / name).read_text(encoding='utf8') if ord(c) > 127}
    return ''.join(sorted(chars))


def main():
    # keep every character an earlier subset had: other drawing code may still use it
    previous = FONTS / 'SOURCES_PAGES.json'
    kept = json.loads(previous.read_text(encoding='utf8'))['text'] if previous.is_file() else ''
    chars = ''.join(sorted(set(page_chars()) | set(kept)))
    record = dict(chars=len(chars), text=chars)
    for key, source in (('cjk', CJK_SOURCE), ('mono', MONO_SOURCE)):
        out = FONTS / OUTPUTS[key]
        opts = subset.Options()
        opts.layout_features = ['*']
        font = subset.load_font(str(source), opts)
        sub = subset.Subsetter(opts)
        sub.populate(text=chars)
        sub.subset(font)
        subset.save_font(font, str(out), opts)
        record[key] = dict(source=str(source), source_sha256=sha256(source), file=out.name, sha256=sha256(out),
                           bytes=out.stat().st_size)
    (FONTS / 'SOURCES_PAGES.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
    print(len(chars), 'chars', {k: record[k]['bytes'] for k in OUTPUTS})


if __name__ == '__main__':
    main()
