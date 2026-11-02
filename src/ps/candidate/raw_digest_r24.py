"""Explicit RHI1 admission and coherent shadow verification; never skips CPU SHA.

The observer covers the PL Raw stream, not the CPU's received DMA bytes. Their
digests must be independently compared before this evidence is useful. The
existing capture path and payload_digest cache are deliberately unchanged.
"""
from dataclasses import dataclass
import hashlib
import time

import numpy as np

from structured_light.p2.errors import CapabilityError, ProtocolError
from structured_light.p2.pynq_transport import (
    verify_overlay_artifacts, parse_hwh_parameters, _parameter_int, sha256_file,
)

MAGIC = 0x52484931
ABI = 0x10000
READY_ENABLED = 0x22
CONTRACT = dict(enabled=True, format='RHI1', abi=ABI, capability_magic=MAGIC,
    capability_bits=7, region='camera_diagnostics', base_offset=0x200,
    control_offset=0x20c, digest_offset=0x240, snapshot_sequence_offset=0x234,
    fifo_words=4096, clock_hz=100000000, verification='cpu_shadow')


class InvalidSnapshot(ProtocolError):
    """No coherent, complete result is available; the CPU path stays authoritative."""


@dataclass(frozen=True)
class RawDigestSnapshot:
    sequence: int
    request_id: int
    epoch: int
    byte_count: int
    minimum: int
    maximum: int
    total: int
    saturated_count: int
    cycles: int
    sha256: str

    def compare_cpu(self, raw):
        if not isinstance(raw, bytes) or len(raw) != self.byte_count:
            raise InvalidSnapshot('RHI1 CPU Raw type/length mismatch')
        # Hash the actual bytes, never a caller-provided digest or manifest.
        actual = hashlib.sha256(raw).hexdigest()
        image = np.frombuffer(raw, dtype=np.uint8)
        statistics = (int(image.min()), int(image.max()),
                      int(image.sum(dtype=np.uint64)), int(np.count_nonzero(image == 255)))
        if actual != self.sha256 or statistics != (
                self.minimum, self.maximum, self.total, self.saturated_count):
            raise InvalidSnapshot('RHI1 disagrees with independently received CPU Raw')
        return dict(status='MATCH', backend='RHI1_CPU_SHADOW', sequence=self.sequence,
                    request_id=self.request_id, epoch=self.epoch, raw_sha256=actual,
                    minimum=self.minimum, maximum=self.maximum, total=self.total,
                    saturated_count=self.saturated_count, cycles=self.cycles,
                    cpu_sha_preserved=True, throughput_acceleration_claimed=False)


class RawDigestObserver:
    def __init__(self, transport):
        self.transport = transport
        self._admitted = False
        self.control_restore_required = False

    def admit(self, manifest_path, *, workers_joined):
        # Re-admission always revokes the previous software permission first.
        # A stale successful receipt must not survive changed artifacts/modes.
        self._admitted = False
        t = self.transport
        record = verify_overlay_artifacts(t.bit_path, t.hwh_path, manifest_path)
        declaration = record.get('raw_integrity')
        if declaration is None or declaration == {'enabled': False}:
            return False
        if (declaration != CONTRACT or not isinstance(declaration, dict) or
                any(type(declaration[k]) is not type(v) for k, v in CONTRACT.items())):
            raise CapabilityError('RHI1 manifest declaration mismatch')
        if workers_joined is not True:
            raise ProtocolError('RHI1 admission requires joined capture workers')
        binding = tuple(sha256_file(p) for p in (t.bit_path, t.hwh_path))
        if binding != getattr(t, '_feature_artifact_binding', None):
            raise CapabilityError('RHI1 artifacts differ from admitted capture transport')
        parameters = parse_hwh_parameters(t.hwh_path)
        if (_parameter_int(parameters, '.FRAME_WIDTH') != t.frame_width or
                _parameter_int(parameters, '.FRAME_HEIGHT') != t.frame_height):
            raise CapabilityError('RHI1 HWH frame geometry mismatch')
        # The wrapper has no new HWH parameter. This is a new register bank in
        # the already admitted diagnostic address range, bound to actual bit/HWH
        # bytes above and independently checked by hardware capability readback.
        t._handoff_state()
        mmio = t._diag_mmio
        expected = {0x200: MAGIC, 0x204: ABI, 0x208: 7,
                    0x238: t.frame_width*t.frame_height, 0x23c: 4096, 0x260: 100000000}
        if any(int(mmio.read(offset)) != value for offset, value in expected.items()):
            raise CapabilityError('RHI1 actual hardware capability mismatch')
        # Record the obligation before a transaction/readback can fail.
        self.control_restore_required = True
        mmio.write(0x20c, 3)
        if int(mmio.read(0x210)) != 0x20 or int(mmio.read(0x20c)) != 1:
            raise ProtocolError('RHI1 enable/clear readback rejected')
        self._admitted = True
        self._binding = binding
        return True

    def disable(self, *, workers_joined):
        if workers_joined is not True:
            raise ProtocolError('RHI1 disable requires joined capture workers')
        if not self.control_restore_required:
            raise CapabilityError('RHI1 has no admitted control transaction to restore')
        self._admitted = False
        self.transport._handoff_state()
        self.transport._diag_mmio.write(0x20c, 0)
        if (int(self.transport._diag_mmio.read(0x20c)) != 0 or
                int(self.transport._diag_mmio.read(0x210)) != 0):
            raise ProtocolError('RHI1 disable readback rejected')
        self.control_restore_required = False

    def read(self, *, request_id, epoch, timeout_s=0.25):
        if not self._admitted:
            raise CapabilityError('RHI1 is not artifact/capability admitted')
        if (type(request_id) is not int or not 0 <= request_id <= 0xffffffff or
                type(epoch) is not int or not 0 <= epoch <= 0xffff or
                not 0 < timeout_s <= 2):
            raise ValueError('invalid RHI1 identity or bounded timeout')
        rd = self.transport._diag_mmio.read
        deadline = time.monotonic()+timeout_s
        while True:
            sequence = int(rd(0x234))
            status = int(rd(0x210))
            if status & ~0x3f or status & 0x1c or not status & 0x20:
                raise InvalidSnapshot('RHI1 fault, disabled or reserved status')
            if status == READY_ENABLED:
                break
            if time.monotonic() >= deadline:
                raise InvalidSnapshot('RHI1 bounded ready timeout')
            time.sleep(min(0.0005, max(0, deadline-time.monotonic())))
        before_identity = (int(rd(0x214)), int(rd(0x218)))
        values = [int(rd(offset)) for offset in range(0x21c, 0x234, 4)]
        digest = ''.join(f'{int(rd(offset)):08x}' for offset in range(0x240, 0x260, 4))
        after_identity = (int(rd(0x214)), int(rd(0x218)))
        after_status = int(rd(0x210))
        after_sequence = int(rd(0x234))
        if (sequence != after_sequence or status != after_status or
                before_identity != after_identity or before_identity != (request_id, epoch)):
            raise InvalidSnapshot('RHI1 torn or wrong-frame snapshot')
        count, minimum, maximum, total, saturated, cycles = values
        expected = self.transport.frame_width*self.transport.frame_height
        if (count != expected or not 0 <= minimum <= maximum <= 255 or
                not minimum*count <= total <= maximum*count or
                not 0 <= saturated <= count or saturated*255 > total or
                (maximum < 255 and saturated != 0)):
            raise InvalidSnapshot('RHI1 invalid byte count or statistics')
        return RawDigestSnapshot(sequence, request_id, epoch, count, minimum,
                                 maximum, total, saturated, cycles, digest)
