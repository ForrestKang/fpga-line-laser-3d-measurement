"""Bounded SHA-256 reuse for the same strongly retained immutable bytes.

Every miss hashes the actual complete bytes. The cache retains at most eight
camera-sized payloads (8 * 752 * 480 bytes, about 2.75 MiB). An entry retains
the exact bytes object, preventing ID reuse while resident; a hit additionally
requires object identity. Capture IDs and caller-provided digests are unused.
"""
from collections import OrderedDict
from hashlib import sha256
from threading import Lock


_MAX_CACHED_PAYLOAD_BYTES = 752 * 480
_MAX_CACHED_PAYLOADS = 8
_cache = OrderedDict()
_cache_lock = Lock()


def payload_sha256(data: bytes) -> str:
    """Return real SHA-256; mutable buffers and bytes subclasses are rejected."""
    if type(data) is not bytes:
        raise TypeError('payload_sha256 requires immutable bytes')
    if len(data) > _MAX_CACHED_PAYLOAD_BYTES:
        return sha256(data).hexdigest()
    key = id(data)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry[0] is data:
            _cache.move_to_end(key)
            return entry[1]
    # SHA may release the GIL. Hash outside the LRU lock, then recheck in case
    # another caller computed this same object while our hash was in flight.
    digest = sha256(data).hexdigest()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and entry[0] is data:
            _cache.move_to_end(key)
            return entry[1]
        _cache[key] = (data, digest)
        _cache.move_to_end(key)
        if len(_cache) > _MAX_CACHED_PAYLOADS:
            _cache.popitem(last=False)
    return digest
