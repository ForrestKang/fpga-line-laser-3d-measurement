"""Opt-in PYNQ RX cache maintenance candidate. No device access at import.
Default is EXACT legacy full invalidation. Partial mode requires a completed,
CPU-read-only DMA RX allocation and must be separately admitted on the board.
Host mock tests do not establish cache coherence on Cortex-A9.
"""
import numpy as np

def invalidate_completed_rx(buffer, used_bytes, *, dma_completed=False,
                            allow_partial=False, line_bytes=64):
    if type(used_bytes) is not int or not 0<used_bytes<=int(buffer.nbytes):
        raise ValueError('completed RX length outside allocated capacity')
    if dma_completed is not True:raise ValueError('DMA completion must be confirmed first')
    if not allow_partial:
        buffer.invalidate()
        return {'mode':'legacy_full','requested_bytes':int(buffer.nbytes)}
    if type(line_bytes) is not int or line_bytes<32 or line_bytes>256 or line_bytes&(line_bytes-1):
        raise ValueError('power-of-two cache alignment required')
    if (getattr(buffer,'ndim',None)!=1 or getattr(buffer,'dtype',None)!=np.dtype('uint8')
        or not buffer.flags.c_contiguous):raise ValueError('contiguous uint8 RX allocation required')
    for name in ['offset','virtual_address','device_address','bo','device','coherent']:
        if not hasattr(buffer,name):raise ValueError('PYNQ buffer attribute missing: '+name)
    if int(buffer.virtual_address)%line_bytes or int(buffer.device_address)%line_bytes or int(buffer.offset)%line_bytes:
        raise ValueError('unaligned RX base; do not invalidate neighboring memory')
    if int(buffer.nbytes)%line_bytes:raise ValueError('unaligned RX capacity')
    rounded=(used_bytes+line_bytes-1)//line_bytes*line_bytes
    if rounded>int(buffer.nbytes):raise ValueError('rounded RX cache range exceeds allocation')
    if buffer.coherent is False:
        # Match PynqBuffer.invalidate's device call, but retain the original
        # allocation owner (do NOT construct a view with a freeing destructor).
        buffer.device.invalidate(buffer.bo,buffer.offset,buffer.virtual_address,rounded)
    elif buffer.coherent is not True:raise ValueError('unknown coherence policy')
    return {'mode':'experimental_partial','requested_bytes':rounded,'valid_payload_bytes':used_bytes}
