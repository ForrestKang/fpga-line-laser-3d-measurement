"""Preview-only metadata reader; the measurement transport is unchanged."""
from .contract import DIAG_OFFSETS
from .errors import CapabilityError, ProtocolError, DMAError
from .pynq_transport import PynqTransport
from .types import DiagnosticsSnapshot


class PreviewPynqTransport(PynqTransport):
    def _allocate(self, size):
        # Two bounded allocations (raw/Profile), reused only after the
        # existing DMA cleanup barrier has marked the lease as available.
        if size not in (self.frame_width*self.frame_height,8192):
            raise DMAError('unexpected preview DMA buffer size')
        if not hasattr(self,'_preview_pool'):self._preview_pool={}
        lease=self._preview_pool.get(size)
        if lease is None:
            lease=_BufferLease(super()._allocate(size))
            self._preview_pool[size]=lease
        if lease.leased:
            raise DMAError('preview DMA buffer is still owned')
        lease.leased=True
        return lease

    def close(self):
        super().close()  # no freeing on an unconfirmed stop/reset
        pool=getattr(self,'_preview_pool',{})
        if any(lease.leased for lease in pool.values()):
            raise DMAError('preview allocation remains in flight')
        for lease in pool.values():lease.storage.freebuffer()
        pool.clear()

    def stream_requirements(self, *, raw, profile):
        if profile:
            raise CapabilityError('preview transport cannot publish a measured Profile')
        return super().stream_requirements(raw=raw, profile=False)

    def read_diagnostics(self):
        # _wait_published still checks both TLASTs, PL completion, errors,
        # request/epoch and all 480 rows before and after this metadata read.
        # No row BRAM values are claimed or used by this preview-only class.
        names = [field for field in DiagnosticsSnapshot.__dataclass_fields__ if field != 'rows']
        values = {name: int(self._diag_mmio.read(DIAG_OFFSETS[name])) & 0xffffffff
                  for name in names}
        values['ready'] = bool(values['ready'] & 1)
        if (values['width'], values['height']) != (self.frame_width, self.frame_height):
            raise ProtocolError('preview diagnostic geometry mismatch')
        if values['row_count'] > self.frame_height:
            raise ProtocolError('preview diagnostic row count overflow')
        snapshot = DiagnosticsSnapshot(**values)
        self.mmio.epoch, self.mmio._request_id = snapshot.epoch, snapshot.request_id
        return snapshot


class _BufferLease:
    """Return storage to a fixed pool at the existing DMA cleanup boundary."""
    def __init__(self,storage):
        self.storage,self.leased=storage,False
    def __getattr__(self,name):return getattr(self.storage,name)
    def __len__(self):return len(self.storage)
    def __getitem__(self,key):return self.storage[key]
    def invalidate(self):self.storage.invalidate()
    def freebuffer(self):
        if not self.leased:raise DMAError('duplicate preview DMA lease release')
        self.leased=False
