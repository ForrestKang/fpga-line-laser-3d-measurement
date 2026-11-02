"""Fetch the official Z2 image without touching any disk or SD card."""
from pathlib import Path
import hashlib
import json
import time
from urllib.request import urlopen, Request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
URL = 'https://download.amd.com/opendownload/pynq/pynq_z2_v3.1.1.zip'

def main():
    folder = ROOT / 'build/toolchain/pynq_image'
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / 'pynq_z2_v3.1.1.zip'
    temporary = destination.with_suffix('.zip.partial')
    record = {'source_page': 'https://www.pynq.io/index.html', 'url': URL,
              'image_version': '3.1.1', 'board': 'pynq_z2',
              'manufacturer_checksum': None, 'download_verified': False, 'sd_written': False}
    if not destination.exists():
        start = time.monotonic()
        total = None
        for attempt in range(20):
            received = temporary.stat().st_size if temporary.exists() else 0
            headers = {'Range': f'bytes={received}-'} if received else {}
            try:
                with urlopen(Request(URL, headers=headers), timeout=120) as response:
                    if received:
                        content_range = response.headers.get('Content-Range', '')
                        if response.status != 206 or not content_range.startswith(f'bytes {received}-'):
                            raise RuntimeError('Server did not honor resume range; partial retained')
                        total = int(content_range.split('/')[-1])
                    else:
                        total = int(response.headers['Content-Length'])
                    with temporary.open('ab' if received else 'wb') as sink:
                        while block := response.read(256 * 1024):
                            if time.monotonic() - start > 7200:
                                raise RuntimeError('Download exceeded two hours; partial retained')
                            sink.write(block)
                            received += len(block)
                            if received % (100 * 1024 * 1024) < 256 * 1024:
                                print(f'downloaded {received // (1024*1024)} MiB / {total // (1024*1024)} MiB', flush=True)
                if received == total:
                    break
            except (TimeoutError, OSError) as exc:
                print(f'download interrupted; resumable attempt {attempt+1}: {type(exc).__name__}', flush=True)
        else:
            raise RuntimeError('Download retries exhausted; partial retained')
        record['content_length'] = total
        if total is None or temporary.stat().st_size != total:
            raise RuntimeError('HTTP length mismatch; partial retained')
        temporary.rename(destination)
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f'ZIP CRC failed: {bad}')
        record['members'] = [{'name': member.filename, 'size': member.file_size} for member in archive.infolist()]
    digest = hashlib.sha256()
    with destination.open('rb') as source:
        while block := source.read(2 * 1024 * 1024):
            digest.update(block)
    record.update(sha256=digest.hexdigest(), size=destination.stat().st_size, download_verified=True)
    (folder / 'image_manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))

if __name__ == '__main__':
    main()
