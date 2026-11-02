"""Complete old-R33 archive with the preserved protocol reader; read-only board."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import archive_space_readonly as archive


def legacy_readback(storage, *, strict_protocol):
    if strict_protocol is not True:
        raise ValueError('strict protocol is mandatory')
    output = archive.OUT/('STRICT_R33_'+storage.parent.name+'.json')
    if output.exists():
        raise FileExistsError(output)
    result = subprocess.run([sys.executable, '-B', str(archive.ROOT/'board/strict_r33_legacy.py'),
                             str(storage.parent), str(output)],
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return json.loads(output.read_text(encoding='utf8'))


def main():
    report = json.loads((archive.OUT/'ARCHIVE_MANIFEST.json').read_text(encoding='utf8'))
    report['original_failed_attempt_preserved'] = str(archive.OUT/'ARCHIVE_MANIFEST.json')
    report['original_failure_reason'] = report.pop('error', None)
    report['legacy_compatibility_note'] = 'R33 uses unchanged preserved R33 protocol reader; current R35 reader rejects legacy stream identity as expected'
    archive.readback = legacy_readback
    client, key = archive.connect('192.168.2.99', 'xilinx', os.environ['PYNQ_PASSWORD'], '192.168.2.100')
    client.get_transport().set_keepalive(10)
    start = time.time()
    report['finish_identity_before'] = archive.identity(client)
    try:
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(60)
            for name in ['r33_formal_v13_T05_rep1', 'r33_short_T06_v14', 'r33_short_T05_v13']:
                remote = '/home/xilinx/'+archive.R33.name+'/data/'+name
                row = archive.archive_one(client, sftp, archive.R33, name, remote,
                                          reuse_download=name == 'r33_formal_v13_T05_rep1')
                report['runs'].append(row)
                archive.save(archive.OUT/(archive.R33.name+'__'+name+'.json'), row)
                print(json.dumps(dict(run=name, eligible=row['eligible'],
                                      logical_bytes=row.get('logical_bytes'))), flush=True)
        report['identity_after'] = archive.identity(client)
        report['passed'] = True
    except Exception as error:
        report.update(passed=False, finish_error=type(error).__name__+': '+str(error))
        raise
    finally:
        client.close()
        report['finish_wall_s'] = time.time()-start
        report['reclaim_candidates'] = [r['remote'] for r in report['runs'] if r['eligible']]
        report['total_logical_bytes'] = sum(r.get('logical_bytes', 0) for r in report['runs'])
        report['total_conservative_reclaim_bytes'] = sum(r.get('conservative_reclaim_bytes', 0) for r in report['runs'])
        archive.save(archive.OUT/'ARCHIVE_MANIFEST_FINAL.json', report)
    print('MANIFEST', archive.OUT/'ARCHIVE_MANIFEST_FINAL.json')


if __name__ == '__main__':
    main()
