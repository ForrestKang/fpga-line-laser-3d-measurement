"""Bounded contiguous payload writes; transaction durability stays in the writer."""


class PayloadBlocks15:
    """Copy small immutable pieces into one reusable, fixed-size output block.

    Logical offsets advance as pieces are staged, exactly as with direct writes.
    ``finish`` must complete before the existing payload fsync. A short/failed
    write raises and leaves the whole batch uncommitted; no receipt is issued.
    No array or producer buffer is retained beyond the synchronous copy.
    """
    def __init__(self, stream, buffer):
        if not isinstance(buffer, bytearray) or not buffer:
            raise ValueError('nonempty fixed payload buffer required')
        self.stream=stream;self.buffer=buffer;self.view=memoryview(buffer)
        self.position=stream.tell();self.used=0;self.write_calls=0

    def tell(self):return self.position

    def _flush(self):
        if not self.used:return
        count=self.stream.write(self.view[:self.used]);self.write_calls+=1
        if count!=self.used:raise OSError('short payload block write')
        self.used=0

    def write(self, data):
        view=memoryview(data).cast('B')
        at=0
        while at<len(view):
            count=min(len(view)-at,len(self.buffer)-self.used)
            self.view[self.used:self.used+count]=view[at:at+count]
            at+=count;self.used+=count;self.position+=count
            if self.used==len(self.buffer):self._flush()
        return len(view)

    def finish(self):
        self._flush()
        if self.stream.tell()!=self.position:raise OSError('payload block offset mismatch')
