"""Independent readback of an offline full-flow run.

Re-opens the written records without the writer's own objects, verifies every
per-frame XYZ hash, rebuilds the cloud and compares it byte-for-byte with the
exported cloud.npy. Never imports the processor.
"""
from __future__ import annotations
import argparse, hashlib, io, json, sys
from pathlib import Path
import numpy as np

CAND = Path(__file__).resolve().parents[2]
FROZEN = Path(r'D:/Workspace/CodexWork/scan3d_pixel_frontend_20261021_25/inputs')


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def verify(run, target):
    run = Path(run)
    result = json.loads((run / 'RESULT.json').read_text())
    frames = [json.loads(l) for l in (run / 'records' / 'frames.jsonl').read_text().splitlines()]
    frame_rows = [r for r in frames if r.get('kind') == 'frame']
    commits = [r for r in frames if r.get('kind') == 'batch_commit']
    frozen = {r['frame_id']: r for r in
              (json.loads(l) for l in (FROZEN / target / 'frames.jsonl').read_text().splitlines())}
    points, problems = [], []
    with (run / 'records' / 'payloads.bin').open('rb') as fh:
        for row in frame_rows:
            entry = row['point_files']['points_axis_mm']
            fh.seek(entry['offset'])
            data = fh.read(entry['bytes'])
            if hashlib.sha256(data).hexdigest() != entry['sha256']:
                problems.append(f"frame {row['request_id']} XYZ hash")
                continue
            points.append(np.load(io.BytesIO(data), allow_pickle=False))
            if frozen[row['request_id']]['raw_sha256'] != row['raw_sha256']:
                problems.append(f"frame {row['request_id']} raw identity")
    rebuilt = np.concatenate(points) if points else np.empty((0, 3))
    cloud = np.load(run / 'cloud.npy')
    same_cloud = rebuilt.shape == cloud.shape and np.array_equal(rebuilt, cloud)
    features = json.loads((run / 'features.json').read_text())['features']
    bad_support = 0
    for feature in features:
        ids = np.asarray(feature.get('support', {}).get('point_indices', []), dtype=np.int64)
        if len(ids) and (ids.min() < 0 or ids.max() >= len(cloud)):
            bad_support += 1
    report = dict(
        schema='R26_OFFLINE_VERIFY_V1', run=str(run), target=target,
        frames_written=len(frame_rows), commits=len(commits),
        frames_reported=result['frames'], points_rebuilt=int(len(rebuilt)),
        points_reported=result['measurement']['points'],
        cloud_identical=bool(same_cloud), cloud_points=int(len(cloud)),
        features=len(features), features_with_bad_support=bad_support,
        problems=problems, passed=bool(same_cloud and not problems and bad_support == 0
                                       and len(frame_rows) == result['frames']
                                       and len(rebuilt) == result['measurement']['points']))
    (run / 'VERIFY.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--target', required=True, choices=('T05', 'T06'))
    a = p.parse_args()
    r = verify(a.run, a.target)
    print(json.dumps(r, indent=2)[:1500])
    return 0 if r['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
