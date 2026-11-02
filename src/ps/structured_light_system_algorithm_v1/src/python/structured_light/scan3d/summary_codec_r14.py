"""Lossless opt-in storage of large per-row JSON vectors as one bounded blob.

Native Raw/SLC2 remain unchanged. Encoding is explicit and old plain summaries
still decode. SHA binds the exact uncompressed JSON, including failure reasons.
"""
import base64
import hashlib
import json
import zlib

KEYS = ('center_q12', 'accepted', 'retained', 'start', 'end', 'reasons', 'primary_rows',
        'geometry_valid_rows', 'geometry_reasons')
ENCODING = 'scan3d_row_vectors_json_zlib_base64_v1'
MAX_JSON_BYTES = 256*1024


def pack_summary(summary):
    if 'row_vectors_blob' in summary:
        raise ValueError('summary already encoded')
    vectors = {key:summary[key] for key in KEYS if key in summary}
    if not vectors: return dict(summary)
    raw = json.dumps(vectors, separators=(',', ':'), allow_nan=False).encode('utf8')
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('row-vector JSON exceeds storage bound')
    packed = base64.b64encode(zlib.compress(raw, 1)).decode('ascii')
    result = {key:value for key,value in summary.items() if key not in vectors}
    result['row_vectors_blob'] = dict(encoding=ENCODING, json_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(), data=packed)
    return result


def unpack_summary(summary):
    if 'row_vectors_blob' not in summary: return dict(summary)
    item = summary['row_vectors_blob']
    if (not isinstance(item, dict) or item.get('encoding') != ENCODING
            or type(item.get('json_bytes')) is not int or not 0 < item['json_bytes'] <= MAX_JSON_BYTES
            or not isinstance(item.get('data'), str) or len(item['data']) > 2*MAX_JSON_BYTES):
        raise ValueError('unsupported or oversized row-vector encoding')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(base64.b64decode(item['data'], validate=True), MAX_JSON_BYTES+1)
    if (len(raw) != item['json_bytes'] or len(raw)>MAX_JSON_BYTES or not decoder.eof
            or decoder.unused_data or decoder.unconsumed_tail
            or hashlib.sha256(raw).hexdigest() != item.get('sha256')):
        raise ValueError('row-vector bytes/hash mismatch')
    vectors = json.loads(raw)
    if not isinstance(vectors, dict) or set(vectors)-set(KEYS):
        raise ValueError('unsupported row-vector field')
    result = {key:value for key,value in summary.items() if key != 'row_vectors_blob'}
    if set(vectors) & set(result): raise ValueError('conflicting plain and encoded vectors')
    result.update(vectors)
    return result
