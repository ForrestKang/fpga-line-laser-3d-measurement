"""Copy an independently admitted local build into a new handoff directory."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from review_build_r43 import review

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--build', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--lanes', type=int, choices=(2,4,8), required=True)
    args = parser.parse_args()
    if Path(args.build).name != args.build:
        raise SystemExit('Expected a local build name, not a path')
    build = ROOT/'build'/args.build
    receipt = args.receipt.resolve()
    receipt.relative_to(ROOT)
    saved = json.loads(receipt.read_text())
    actual = review(build/'reports', args.lanes)
    if not actual['passed'] or saved != actual:
        raise SystemExit('Current full-board review does not match the saved admission')
    output = ROOT/'firmware'/args.build
    if output.exists():
        raise SystemExit('Refuse to replace a firmware handoff')
    sources = {'r43.bit': build/'artifacts/r43.bit',
               'r43.bin': build/'artifacts/r43.bin',
               'r43.hwh': build/'sl_profile_hdmi.gen/sources_1/bd/sl_profile_hdmi/hw_handoff/sl_profile_hdmi.hwh'}
    for path in sources.values():
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit('Missing/nonempty artifact requirement: '+str(path))
    output.mkdir(parents=True)
    files = []
    for name, source in sources.items():
        dest = output/name
        shutil.copy2(source,dest)
        if sha(source) != sha(dest):
            raise RuntimeError('Artifact copy verification failed')
        files.append(dict(name=name, source=str(source.relative_to(ROOT)),
                          size=dest.stat().st_size, sha256=sha(dest)))
    manifest = dict(kind='R43_OFFLINE_FIRMWARE_HANDOFF_NOT_DEPLOYED',
                    build=args.build, admission=str(receipt.relative_to(ROOT)),
                    admission_sha256=sha(receipt), review=actual, files=files,
                    note='No device load or board test performed. Existing R42 is unchanged.')
    (output/'MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
