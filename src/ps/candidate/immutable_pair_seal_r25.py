"""One immutable pair's content-digest receipt; never hardware authority.

compute(frame, primary) makes exactly two real SHA256 calls. seal.check(frame,
primary) reuses them only for the exact strongly retained objects and unchanged
frame representations. No global cache, frame-ID lookup, hardware token, PL
completion, optical validation or primary/final semantic association is issued.
Callers must retain their existing strict decode/assembly and DMA admission.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
import struct
import numpy as np

from .pfo3_r25 import FinalFrame,PFO3Error,HEADER_FIELDS,RECORD_DTYPE,ROW_DTYPE

_ISSUER=object()


@dataclass(frozen=True,slots=True)
class PairDigestsR25:
    assembled_sha256:str
    primary_stream_sha256:str


def _descriptor(array):
    return array.dtype,array.shape,array.strides,array.ctypes.data


def _header_words(header):
    if type(header) is not MappingProxyType or len(header)!=len(HEADER_FIELDS):
        raise PFO3Error('pair requires immutable complete frame header')
    try:words=tuple(header[name] for name in HEADER_FIELDS)
    except KeyError as error:raise PFO3Error('pair frame header fields changed') from error
    if any(type(word) is not int for word in words):
        raise PFO3Error('pair frame header requires builtin integer words')
    return words


def _array_bound(array,payload,descriptor):
    if (type(array) is not np.ndarray or array.base is not payload
            or array.flags.writeable or not array.flags.c_contiguous
            or _descriptor(array)!=descriptor):
        raise PFO3Error('pair array reference/layout/immutable owner changed')


@dataclass(frozen=True,slots=True)
class _BoundPair:
    issuer:object
    frame:object
    primary:bytes
    payload:bytes
    header:object
    header_words:tuple
    records:object
    record_descriptor:tuple
    rows:object
    row_descriptor:tuple
    digests:PairDigestsR25


@dataclass(frozen=True,slots=True,init=False)
class ImmutablePairSealR25:
    """Issued by compute only. A seal attests content hashing, not PL success."""
    _bound:_BoundPair

    def check(self,frame,primary):
        bound=getattr(self,'_bound',None)
        if (type(self) is not ImmutablePairSealR25 or type(bound) is not _BoundPair
                or bound.issuer is not _ISSUER):
            raise PFO3Error('unissued immutable pair seal')
        if (frame is not bound.frame or primary is not bound.primary
                or type(frame) is not FinalFrame or type(primary) is not bytes
                or frame.payload is not bound.payload or type(frame.payload) is not bytes
                or frame.header is not bound.header or frame.records is not bound.records
                or frame.rows is not bound.rows):
            raise PFO3Error('pair seal requires exact retained frame/primary/payload references')
        if _header_words(frame.header)!=bound.header_words:
            raise PFO3Error('pair frame header values changed')
        _array_bound(frame.records,bound.payload,bound.record_descriptor)
        _array_bound(frame.rows,bound.payload,bound.row_descriptor)
        return bound.digests


def compute(frame,primary):
    """Hash one strictly decoded frame's archive and immutable primary bytes.

    Structural bindings only: this does not redo strict body validation or
    establish that primary semantically assembles to frame. The assembler must
    establish those facts first. Frozen dataclasses/private tokens prevent
    ordinary accidental misuse; they are not a security boundary against code
    with arbitrary Python object.__setattr__/module-private access.
    """
    if type(frame) is not FinalFrame or type(frame.payload) is not bytes or type(primary) is not bytes:
        raise PFO3Error('pair requires exact FinalFrame and immutable bytes')
    payload=frame.payload;words=_header_words(frame.header)
    if len(payload)<256 or struct.unpack_from('<64I',payload)!=words:
        raise PFO3Error('pair header differs from retained archive bytes')
    n=frame.header['diagnostic_count'];t=frame.header['row_count']
    if len(payload)!=256+64*(n+t):raise PFO3Error('pair frame payload size/count mismatch')
    address=np.frombuffer(payload,dtype=np.uint8,count=0).ctypes.data
    descriptors=((RECORD_DTYPE,(n,),(64,),address+256),
                 (ROW_DTYPE,(t,),(64,),address+256+64*n))
    _array_bound(frame.records,payload,descriptors[0])
    _array_bound(frame.rows,payload,descriptors[1])
    digests=PairDigestsR25(sha256(payload).hexdigest(),sha256(primary).hexdigest())
    bound=_BoundPair(_ISSUER,frame,primary,payload,frame.header,words,
                     frame.records,descriptors[0],frame.rows,descriptors[1],digests)
    seal=object.__new__(ImmutablePairSealR25)
    object.__setattr__(seal,'_bound',bound)
    return seal
