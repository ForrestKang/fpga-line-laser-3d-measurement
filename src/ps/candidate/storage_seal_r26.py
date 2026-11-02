"""R26 immutable MBR1 seal and persistence admission receipts.

This module never reads camera images, never validates geometry, never writes
storage and never issues hardware authority.  ``storage_r21.encode_profile``
assembles the one immutable MBR1 encoding and attaches a receipt that binds the
exact bytes object, its length and the SHA-256 computed while the bytes were
assembled.  ``MultiBranchChunkWriter`` performs its existing strict decode,
source cross-check and XYZ check exactly once on the submitter thread, then
attaches an admission receipt that additionally binds the raw bytes, every
source payload and every point array by object identity, length/admission
digest and array descriptor.

The writer worker reuses those proofs only for the exact strongly retained
objects.  A plain ``bytes`` payload, a bytes subclass that is not the sealed
type, or a record with no admission receipt still takes the full strict
decode/validation path.  A sealed object without a valid receipt fails closed
rather than falling back.  Disk readers in ``iter_records`` never import or
consult these receipts: they continue to re-read and re-hash each stored piece
independently.

Python has no in-process security boundary against code that deliberately uses
``object.__setattr__`` or imports module-private state.  Frozen dataclasses,
private issuer tokens, exact type checks, exact object identity and pointer
checks make ordinary tampering/forgery fail closed; they are not a claim of
cryptographic isolation from arbitrary Python code in the same interpreter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from types import MappingProxyType
import struct

import numpy as np

ADMISSION_KEY = '_storage_admission_r26'
_SEAL_ATTR = '_seal_r26'
_ISSUER = object()
_HEX = frozenset('0123456789abcdef')
_HEX_STR = '0123456789abcdef'


def _sha_text(value):
    return (type(value) is str and len(value) == 64
            and not value.strip(_HEX_STR))


class SealedMBR1(bytes):
    """Immutable, bytes-compatible MBR1 encoding with one attached receipt.

    The inherited bytes constructor is intentionally usable only as an
    intermediate step: a constructed instance has no receipt and ``open_mbr1``
    rejects it.  ``storage_r21.encode_profile`` creates the object, builds the
    read-only array views over it, validates that encoded view once and then
    attaches the receipt.  Normal attribute assignment/deletion is refused.
    """

    def __setattr__(self, name, value):
        raise AttributeError('sealed MBR1 encoding is immutable')

    def __delattr__(self, name):
        raise AttributeError('sealed MBR1 encoding is immutable')


@dataclass(frozen=True, slots=True)
class MBR1ReceiptR26:
    """Proof that one exact bytes object was assembled once by encode_profile."""
    issuer: object
    payload: object = field(repr=False, compare=False)
    length: int
    sha256: str
    header_length: int
    header_bytes: bytes = field(repr=False)
    arrays: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SourceBindingR26:
    """One source-payload digest computed from the exact immutable bytes."""
    key: str
    payload: bytes = field(repr=False, compare=False)
    length: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PointBindingR26:
    """One point-array digest computed from the exact retained array."""
    key: str
    array: object = field(repr=False, compare=False)
    dtype: str
    shape: tuple
    strides: tuple
    address: int
    nbytes: int
    header: bytes = field(repr=False)
    total_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AdmissionDigestsR26:
    """Digests reusable only for the exact objects bound by AdmissionSealR26."""
    profile_sha256: str
    raw_sha256: str
    sources: object = field(repr=False, compare=False)
    points: object = field(repr=False, compare=False)

    def source_sha256(self, key, payload):
        binding = self.sources.get(key)
        if (binding is None or type(payload) is not bytes
                or payload is not binding.payload or len(payload) != binding.length):
            raise ValueError('source payload identity changed after admission')
        return binding.sha256

    def point_file(self, key, array):
        binding = self.points.get(key)
        if binding is None or binding.array is None or array is not binding.array:
            raise ValueError('point array identity changed after admission')
        _require_point_binding(array, binding)
        return binding.header, binding.total_bytes, binding.sha256


@dataclass(frozen=True, slots=True)
class AdmissionSealR26:
    """One record's already-performed strict validation and content digests."""
    issuer: object
    request_id: int
    capture_epoch: int
    raw: bytes = field(repr=False, compare=False)
    raw_length: int
    raw_sha256: str
    profile: object = field(repr=False, compare=False)
    profile_length: int
    profile_sha256: str
    source_keys: tuple
    sources: object = field(repr=False, compare=False)
    point_keys: tuple
    points: object = field(repr=False, compare=False)
    completion: object = field(repr=False, compare=False)

    def check(self, record):
        """Verify every persisted binding without re-reading image content."""
        if type(self) is not AdmissionSealR26 or self.issuer is not _ISSUER:
            raise ValueError('unissued storage admission seal')
        if type(record) is not dict or record.get(ADMISSION_KEY) is not self:
            raise ValueError('storage admission seal belongs to another record')
        if (type(self.request_id) is not int or type(self.capture_epoch) is not int
                or type(record.get('request_id')) is not int
                or type(record.get('capture_epoch')) is not int
                or record.get('request_id') != self.request_id
                or record.get('capture_epoch') != self.capture_epoch):
            raise ValueError('storage admission frame identity changed')
        raw = record.get('raw')
        if (type(raw) is not bytes or raw is not self.raw
                or len(raw) != self.raw_length
                or record.get('raw_sha256') != self.raw_sha256
                or not _sha_text(self.raw_sha256)):
            raise ValueError('storage admission raw binding changed')
        profile = record.get('profile_payload')
        if (type(profile) is not SealedMBR1 or profile is not self.profile
                or len(profile) != self.profile_length
                or not _sha_text(self.profile_sha256)):
            raise ValueError('storage admission profile binding changed')
        receipt = open_mbr1(profile)
        if (receipt.sha256 != self.profile_sha256
                or receipt.length != self.profile_length):
            raise ValueError('sealed MBR1 digest differs from admission receipt')
        present = tuple(key for key in self.source_keys if key in record)
        if present != tuple(binding.key for binding in self.sources.values()):
            raise ValueError('storage admission source set changed')
        for binding in self.sources.values():
            payload = record.get(binding.key)
            if (type(payload) is not bytes or payload is not binding.payload
                    or len(payload) != binding.length
                    or not _sha_text(binding.sha256)):
                raise ValueError('storage admission source binding changed')
        for key in self.point_keys:
            binding = self.points[key]
            array = record.get(key)
            if binding.array is None:
                if array is not None:
                    raise ValueError('storage admission point binding changed')
                continue
            if array is not binding.array:
                raise ValueError('storage admission point binding changed')
            _require_point_binding(array, binding)
        if record.get('_pfo3_completion') is not self.completion:
            raise ValueError('storage admission completion binding changed')
        return AdmissionDigestsR26(
            self.profile_sha256, self.raw_sha256,
            MappingProxyType(dict(self.sources)),
            MappingProxyType(dict(self.points)))


def _require_point_binding(array, binding):
    if (type(array) is not np.ndarray
            or array.dtype.str != binding.dtype
            or tuple(array.shape) != binding.shape
            or tuple(array.strides) != binding.strides
            or array.ctypes.data != binding.address
            or array.nbytes != binding.nbytes
            or array.flags.writeable or not array.flags.c_contiguous
            or not array.flags.owndata or array.base is not None):
        raise ValueError('point array descriptor/immutability changed after admission')


def _payload_base_address(payload):
    return np.frombuffer(payload, dtype=np.uint8, count=0).ctypes.data


def _new_mbr1(payload_bytes):
    """Construct an unsealed intermediate; only storage_r21's encoder uses it."""
    if not isinstance(payload_bytes, (bytes, bytearray, memoryview)):
        raise TypeError('MBR1 assembly requires a bytes-like buffer')
    return SealedMBR1(payload_bytes)


def _attach_mbr1(payload, *, sha256, header_length, header_bytes, arrays):
    """Attach one receipt after the encoder validated the exact encoded views."""
    if type(payload) is not SealedMBR1:
        raise TypeError('exact sealed MBR1 encoding required')
    if getattr(payload, _SEAL_ATTR, None) is not None:
        raise ValueError('MBR1 encoding is already sealed')
    if not _sha_text(sha256):
        raise ValueError('sealed MBR1 requires a SHA-256 digest')
    if type(header_length) is not int or header_length < 2 or header_length > 4 * 1024 * 1024:
        raise ValueError('sealed MBR1 requires a bounded header length')
    if type(header_bytes) is not bytes or len(header_bytes) != header_length:
        raise ValueError('sealed MBR1 header bytes differ from header length')
    if type(arrays) is not MappingProxyType or not arrays:
        raise ValueError('sealed MBR1 requires immutable array views')
    if payload[:4] != b'MBR1' or struct.unpack_from('<I', payload, 4)[0] != header_length:
        raise ValueError('sealed MBR1 magic/header mismatch')
    for name, array in arrays.items():
        if type(name) is not str or type(array) is not np.ndarray:
            raise ValueError('invalid sealed MBR1 array view')
    if len(payload) != 8 + header_length + sum(array.nbytes for array in arrays.values()):
        raise ValueError('sealed MBR1 length does not cover its array views')
    base = _payload_base_address(payload)
    end = base + len(payload)
    for array in arrays.values():
        if array.flags.writeable or not array.flags.c_contiguous:
            raise ValueError('sealed MBR1 array views must be read-only and contiguous')
        if array.nbytes:
            start = array.ctypes.data
            if start < base or start + array.nbytes > end:
                raise ValueError('sealed MBR1 array view lies outside the encoding')
    receipt = MBR1ReceiptR26(_ISSUER, payload, len(payload), sha256,
                             header_length, header_bytes, arrays)
    object.__setattr__(payload, _SEAL_ATTR, receipt)
    return payload


def is_sealed(payload):
    """Only the exact sealed type is a seal; every other bytes value is plain."""
    return type(payload) is SealedMBR1


def open_mbr1(payload):
    """Return the exact receipt or reject a missing/forged/tampered envelope."""
    if type(payload) is not SealedMBR1:
        raise ValueError('not a sealed MBR1 encoding')
    receipt = getattr(payload, _SEAL_ATTR, None)
    if type(receipt) is not MBR1ReceiptR26 or receipt.issuer is not _ISSUER:
        raise ValueError('unissued sealed MBR1 encoding')
    if receipt.payload is not payload:
        raise ValueError('sealed MBR1 receipt belongs to another payload')
    if type(receipt.length) is not int or receipt.length != len(payload):
        raise ValueError('sealed MBR1 length changed')
    if not _sha_text(receipt.sha256):
        raise ValueError('sealed MBR1 digest malformed')
    if (type(receipt.header_length) is not int
            or not 2 <= receipt.header_length <= 4 * 1024 * 1024
            or type(receipt.header_bytes) is not bytes
            or len(receipt.header_bytes) != receipt.header_length):
        raise ValueError('sealed MBR1 header receipt malformed')
    if (len(payload) < 8 + receipt.header_length
            or payload[:4] != b'MBR1'
            or struct.unpack_from('<I', payload, 4)[0] != receipt.header_length
            or bytes(payload[8:8 + receipt.header_length]) != receipt.header_bytes):
        raise ValueError('sealed MBR1 header bytes changed')
    if type(receipt.arrays) is not MappingProxyType:
        raise ValueError('sealed MBR1 array receipt malformed')
    return receipt


def verify_mbr1_digest(payload):
    """Recompute the one content digest at admission, then reuse it at write."""
    receipt = open_mbr1(payload)
    if hashlib.sha256(payload).hexdigest() != receipt.sha256:
        raise ValueError('sealed MBR1 digest mismatch')
    return receipt


def _issue_admission(*, request_id, capture_epoch, raw, raw_sha256, profile,
                     profile_sha256, source_keys, sources, point_keys, points,
                     completion):
    """Create one admission receipt; storage_r21 vouches for prior validation."""
    if type(profile) is not SealedMBR1:
        raise TypeError('admission requires the exact sealed MBR1 encoding')
    receipt = open_mbr1(profile)
    if receipt.sha256 != profile_sha256:
        raise ValueError('admission profile digest differs from sealed encoding')
    if type(raw) is not bytes or not _sha_text(raw_sha256):
        raise ValueError('admission requires immutable raw bytes and digest')
    if (type(request_id) is not int or type(capture_epoch) is not int
            or type(source_keys) is not tuple or type(point_keys) is not tuple):
        raise ValueError('invalid admission identity/key binding')
    sources = tuple(sources)
    for binding in sources:
        if (type(binding) is not SourceBindingR26
                or type(binding.payload) is not bytes
                or len(binding.payload) != binding.length
                or not _sha_text(binding.sha256)
                or binding.key not in source_keys):
            raise ValueError('invalid admission source binding')
    if len({binding.key for binding in sources}) != len(sources):
        raise ValueError('duplicate admission source binding')
    points = tuple(points)
    for binding in points:
        if (type(binding) is not PointBindingR26
                or binding.key not in point_keys
                or (binding.array is not None
                    and (type(binding.array) is not np.ndarray
                         or binding.array.nbytes != binding.nbytes
                         or not _sha_text(binding.sha256)
                         or len(binding.header) + binding.nbytes != binding.total_bytes))):
            raise ValueError('invalid admission point binding')
        if binding.array is not None:
            _require_point_binding(binding.array, binding)
    if len({binding.key for binding in points}) != len(points):
        raise ValueError('duplicate admission point binding')
    return AdmissionSealR26(
        _ISSUER, request_id, capture_epoch, raw, len(raw), raw_sha256,
        profile, len(profile), profile_sha256, source_keys,
        MappingProxyType({binding.key: binding for binding in sources}),
        point_keys,
        MappingProxyType({binding.key: binding for binding in points}),
        completion)
