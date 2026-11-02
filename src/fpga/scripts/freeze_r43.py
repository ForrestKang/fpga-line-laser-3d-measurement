"""Record immutable input provenance in the new candidate only; no board I/O."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = Path('D:/Workspace/CodexWork/scan3d_r42_buttons_boot_20261029_01')
UI = Path('D:/Workspace/CodexWork/scan3d_r42_ui13_20261029_01')


def record(path, root):
    return dict(path=path.relative_to(root).as_posix(), bytes=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def main():
    out = ROOT / 'evidence/BASELINE_FREEZE_R43.json'
    if out.exists():
        raise SystemExit('Freeze already exists; never overwrite')
    baseline = []
    for folder in ('rtl', 'baseline_rtl', 'constraints', 'bd', 'scripts'):
        baseline.extend(record(path, BASE) for path in sorted((BASE/folder).rglob('*')) if path.is_file())
    for name in ('r40.bit',):
        baseline.append(record(BASE/'build/b15a/artifacts'/name, BASE))
    baseline.append(record(BASE/'build/b15a/sl_profile_hdmi.gen/sources_1/bd/sl_profile_hdmi/hw_handoff/sl_profile_hdmi.hwh', BASE))
    baseline.append(record(BASE/'evidence/BUILD_B15A_FINAL_REVIEW_PL.json', BASE))
    ui = [record(path, UI) for path in sorted((UI/'runtime13').rglob('*')) if path.is_file() and '__pycache__' not in path.parts]
    vendor = [record(path, ROOT) for path in sorted((ROOT/'vendor/r31_baseline_copy').rglob('*')) if path.is_file()]
    result = dict(created_utc=datetime.now(timezone.utc).isoformat(), baseline_root=str(BASE), ui_root=str(UI),
                  baseline_files=baseline, ui_files=ui, private_build_dependencies=vendor,
                  candidate_root=str(ROOT), board_operations=0,
                  preserve='Baseline reports, RTL, runtime, bit, boot installation and UI defaults remain unchanged.')
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(baseline_files=len(baseline), ui_files=len(ui), vendor_files=len(vendor), output=str(out))))


if __name__ == '__main__':
    main()
