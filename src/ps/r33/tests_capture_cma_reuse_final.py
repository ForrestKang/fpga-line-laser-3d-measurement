"""CMA ownership regressions independent of Linux/PYNQ allocation."""
import unittest
import numpy as np
from structured_light.p2.pynq_transport import BoundedS2MM, DmaTransfer, PynqTransport
from structured_light.p2.errors import DMAError

class Buffer(np.ndarray):
    def __new__(cls, size):
        obj = np.zeros(size, np.uint8).view(cls)
        obj.frees = obj.invalidates = 0
        return obj
    def invalidate(self): self.invalidates += 1
    def freebuffer(self): self.frees += 1

class Registers:
    def read(self, offset): return 1
    def write(self, offset, value): pass

class ReuseTests(unittest.TestCase):
    def channel(self): return BoundedS2MM(Registers(), name='raw')
    def done(self, dma, buf):
        dma.transfer_state = DmaTransfer(buf, buf.nbytes, 0, completed=True)
        dma._safe_to_release = True
    def test_cannot_retain_before_completion_or_reset(self):
        dma=self.channel(); b=Buffer(8)
        dma.transfer_state=DmaTransfer(b,8,0,armed=True)
        with self.assertRaises(DMAError): dma.cleanup_buffer(retain=True)
        self.assertEqual(b.frees,0)
        self.assertIs(dma.transfer_state.buffer,b)
    def test_retained_copy_does_not_alias_next_frame(self):
        dma=self.channel();b=Buffer(8);b[:]=3
        self.done(dma,b);saved=bytes(memoryview(b))
        dma.cleanup_buffer(retain=True);b[:]=7
        self.assertEqual(saved,bytes([3])*8)
        self.assertEqual(b.frees,0)
        dma.release_idle_buffer();self.assertEqual(b.frees,1)
        dma.release_idle_buffer();self.assertEqual(b.frees,1)
    def test_hundred_frames_allocate_once(self):
        cam=PynqTransport.__new__(PynqTransport);cam.raw_dma=self.channel()
        cam.profile_dma=None;cam._raw_buffer=cam._profile_buffer=None
        allocations=[]
        def allocate(size):
            b=Buffer(size);allocations.append(b);return b
        cam._allocate=allocate;cam.enable_raw_buffer_reuse()
        cam.raw_dma.arm=lambda b,n: self.done(cam.raw_dma,b)
        for _ in range(100):
            cam.arm_raw(32)
            cam.raw_dma.cleanup_buffer(retain=True);cam._raw_buffer=None
        self.assertEqual(len(allocations),1)
        cam.close();self.assertEqual(allocations[0].frees,1)
    def test_changed_capacity_rejected_and_retained(self):
        cam=PynqTransport.__new__(PynqTransport);cam.raw_dma=self.channel()
        b=Buffer(8);cam.raw_dma.idle_buffer=b
        with self.assertRaises(DMAError):cam.arm_raw(9)
        self.assertIs(cam.raw_dma.idle_buffer,b);self.assertEqual(b.frees,0)
    def test_legacy_cleanup_still_frees(self):
        dma=self.channel();b=Buffer(8);self.done(dma,b)
        dma.cleanup_buffer();self.assertEqual(b.frees,1)
        self.assertIsNone(dma.transfer_state)
    def test_active_transfer_blocks_idle_retirement(self):
        dma=self.channel();b=Buffer(8);dma.idle_buffer=b
        dma.transfer_state=DmaTransfer(Buffer(8),8,0)
        with self.assertRaises(DMAError):dma.release_idle_buffer()
        self.assertEqual(b.frees,0)

if __name__=='__main__':unittest.main()
