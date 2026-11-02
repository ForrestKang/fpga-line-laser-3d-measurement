"""R20B complete-group sync boundary, without changing persistence ordering."""
import time
from .stream_wait_r20 import CapacityWaitWriter

class TimedCapacityWriter(CapacityWaitWriter):
    def _persist_group(self,group):
        stats=super()._persist_group(group)
        # All payload/event/index fsync calls have succeeded. This is the
        # durable data boundary, before the parent updates/announces written.
        stats['sync_done_perf_s']=time.perf_counter()
        return stats
