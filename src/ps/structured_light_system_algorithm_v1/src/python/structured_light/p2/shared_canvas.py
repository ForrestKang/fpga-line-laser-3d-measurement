"""Opt-in, stop-and-wait ordinary-RAM canvas transport; never DMA memory."""
from __future__ import annotations

import mmap
import math
import os
import struct
import time
import weakref

import numpy as np

MAGIC = b'SLCNV001'
ABI = 1
WIDTH, HEIGHT, CHANNELS = 1280, 720, 3
CANVAS_SIZE = WIDTH*HEIGHT*CHANNELS
HEADER = struct.Struct('<8sIIIIQ32x')
MAPPED_SIZE = HEADER.size+CANVAS_SIZE
MAX_SEQUENCE = (1 << 63)-1


def descriptor():
    return dict(abi=ABI, canvas_size=CANVAS_SIZE, mapped_size=MAPPED_SIZE,
                width=WIDTH, height=HEIGHT, channels=CHANNELS)


def check_descriptor(value):
    if not isinstance(value, dict):
        raise ValueError('shared canvas descriptor required')
    for key, expected in descriptor().items():
        if type(value.get(key)) is not int or value[key] != expected:
            raise ValueError('invalid shared canvas '+key)


def _check_seals(fd):
    import fcntl
    required = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
    if fcntl.fcntl(fd, fcntl.F_GET_SEALS) & required != required:
        raise ValueError('shared canvas size seals required')


def _check_fd(fd):
    if type(fd) is not int or fd < 0:
        raise ValueError('shared canvas descriptor must be a valid integer')
    if os.fstat(fd).st_size != MAPPED_SIZE:
        raise ValueError('invalid shared canvas mapped size')
    _check_seals(fd)


def _create_memfd():
    if os.name != 'posix' or not hasattr(os, 'memfd_create'):
        raise RuntimeError('shared canvas requires Linux memfd_create; binary transport remains the default')
    import fcntl
    fd = os.memfd_create('sl_hdmi_canvas', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.ftruncate(fd, MAPPED_SIZE)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK)
        return fd
    except BaseException:
        os.close(fd)
        raise


class _MappedCanvas:
    def __init__(self, fd, access):
        self.fd, self.mapping = fd, None
        try:
            _check_fd(fd)
            self.mapping = mmap.mmap(fd, MAPPED_SIZE, access=access)
        except BaseException:
            os.close(fd)
            self.fd = None
            raise

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _header(self, sequence):
        _check_fd(self.fd)
        expected = (MAGIC, WIDTH, HEIGHT, CHANNELS, CANVAS_SIZE, sequence)
        if HEADER.unpack_from(self.mapping) != expected:
            raise ValueError('shared canvas header/sequence mismatch')


class SharedCanvasWriter(_MappedCanvas):
    def __init__(self, fd):
        super().__init__(fd, mmap.ACCESS_WRITE)
        self.sequence, self.pending = 0, None
        HEADER.pack_into(self.mapping, 0, MAGIC, WIDTH, HEIGHT, CHANNELS, CANVAS_SIZE, 0)

    @classmethod
    def create(cls):
        return cls(_create_memfd())

    def close(self):
        # A failed renderer's original traceback may retain a local canvas or
        # ROI. ndarray(buffer=mmap) does not prevent mmap.close(), so closing
        # immediately would leave that diagnostic reference dangling. Release
        # the producer fd now, but retain ordinary RAM until the final canvas
        # reference disappears. This never owns or releases a DMA/CMA store.
        reference=getattr(self,'_render_view',None)
        pixels=None if reference is None else reference()
        if self.mapping is not None and pixels is not None:
            mapping=self.mapping
            self.mapping=None
            weakref.finalize(pixels,mapping.close)
        super().close()

    def prepare(self, canvas):
        if (not isinstance(canvas, np.ndarray) or canvas.shape != (HEIGHT, WIDTH, CHANNELS)
                or canvas.dtype != np.uint8 or not canvas.flags.c_contiguous):
            raise ValueError('canvas must be contiguous 1280x720 BGR uint8')
        if self.pending is not None:
            raise RuntimeError('shared canvas requires acknowledgement before reuse')
        _check_fd(self.fd)
        if self.sequence >= MAX_SEQUENCE:
            raise RuntimeError('shared canvas sequence exhausted')
        sequence = self.sequence+1
        self.pending = sequence
        # Sequence zero denotes an unpublished frame. The control socket is
        # sent only after the copy and final header publication complete.
        HEADER.pack_into(self.mapping, 0, MAGIC, WIDTH, HEIGHT, CHANNELS, CANVAS_SIZE, 0)
        pixels = np.ndarray((HEIGHT, WIDTH, CHANNELS), np.uint8,
                            buffer=self.mapping, offset=HEADER.size)
        try:
            np.copyto(pixels, canvas, casting='no')
        finally:
            del pixels
        HEADER.pack_into(self.mapping, 0, MAGIC, WIDTH, HEIGHT, CHANNELS, CANVAS_SIZE, sequence)
        return dict(descriptor(), sequence=sequence, published_monotonic_s=time.monotonic())

    def prepare_render(self, render, initial_canvas):
        """Render directly into unpublished ordinary RAM, then publish as usual.

        The synchronous callback releases its views before successful return.
        A failure traceback may retain a view; close defers ordinary-RAM unmap
        until that view disappears, without changing the original exception.
        The immutable dashboard regions are initialized exactly once; subsequent
        renders reuse the same pixels just as the renderer's own canvas does.
        """
        if (not isinstance(initial_canvas,np.ndarray)
                or initial_canvas.shape!=(HEIGHT,WIDTH,CHANNELS)
                or initial_canvas.dtype!=np.uint8 or not initial_canvas.flags.c_contiguous):
            raise ValueError('canvas must be contiguous 1280x720 BGR uint8')
        if self.pending is not None:
            raise RuntimeError('shared canvas requires acknowledgement before reuse')
        _check_fd(self.fd)
        if self.sequence>=MAX_SEQUENCE:
            raise RuntimeError('shared canvas sequence exhausted')
        sequence=self.sequence+1
        self.pending=sequence
        HEADER.pack_into(self.mapping,0,MAGIC,WIDTH,HEIGHT,CHANNELS,CANVAS_SIZE,0)
        pixels=np.ndarray((HEIGHT,WIDTH,CHANNELS),np.uint8,buffer=self.mapping,offset=HEADER.size)
        self._render_view=weakref.ref(pixels)
        try:
            if not getattr(self,'_render_initialized',False):
                np.copyto(pixels,initial_canvas,casting='no')
            render(pixels)
            self._render_initialized=True
        finally:
            del pixels
        HEADER.pack_into(self.mapping,0,MAGIC,WIDTH,HEIGHT,CHANNELS,CANVAS_SIZE,sequence)
        return dict(descriptor(),sequence=sequence,published_monotonic_s=time.monotonic())

    def acknowledge(self, result):
        if (not isinstance(result, dict) or self.pending is None
                or type(result.get('canvas_sequence')) is not int
                or result['canvas_sequence'] != self.pending
                or result.get('canvas_transport') != 'shared_canvas_v1'
                or result.get('hdmi_acknowledged') is not True):
            raise ValueError('shared canvas acknowledgement mismatch')
        self._header(self.pending)
        self.sequence, self.pending = self.pending, None


class SharedCanvasReader(_MappedCanvas):
    def __init__(self, fd):
        super().__init__(fd, mmap.ACCESS_READ)
        self.sequence = 0
        try:
            self._header(0)
        except BaseException:
            self.close()
            raise

    def present(self, command, output):
        received = time.monotonic()
        check_descriptor(command)
        sequence = command.get('sequence')
        if (type(sequence) is not int or not 1 <= sequence <= MAX_SEQUENCE
                or sequence != self.sequence+1):
            raise ValueError('shared canvas sequence must advance exactly once')
        published = command.get('published_monotonic_s')
        if (type(published) not in (int,float) or not math.isfinite(published)
                or not 0 <= published <= received):
            raise ValueError('invalid shared canvas publication timestamp')
        self._header(sequence)
        pixels = np.ndarray((HEIGHT, WIDTH, CHANNELS), np.uint8,
                            buffer=self.mapping, offset=HEADER.size)
        try:
            # show_canvas retains the existing CMA fill/flush/VDMA switch and
            # frame-event ACK. The mmap view is not retained by that method.
            result = output.show_canvas(pixels)
        finally:
            del pixels
        self._header(sequence)
        if not isinstance(result, dict) or result.get('hdmi_acknowledged') is not True:
            raise RuntimeError('display did not acknowledge shared canvas')
        self.sequence = sequence
        return dict(result, canvas_transport='shared_canvas_v1', canvas_sequence=sequence,
                    canvas_transfer_s=received-published,
                    canvas_owner_present_s=time.monotonic()-received)
