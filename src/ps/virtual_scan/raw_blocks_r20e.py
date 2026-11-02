"""Bounded lossless replay input; no future features, geometry or result cache."""
from pathlib import Path
import zlib
try:
 import resource
except ImportError:
 resource=None

RAW_BYTES=752*480

def thread_fault_counters():
 if resource is None or not hasattr(resource,'RUSAGE_THREAD'):return None
 try:r=resource.getrusage(resource.RUSAGE_THREAD)
 except (OSError,ValueError):return None
 return dict(major=r.ru_majflt,minor=r.ru_minflt,voluntary_switch=r.ru_nvcsw,involuntary_switch=r.ru_nivcsw)

class LosslessRawBlocks:
 """Preload finite Raw as compressed native frames; restore each requested byte.

 Only aligned full native-frame reads are allowed. Preload/compression remains
 outside the frame-loop measurement just like the original RAM preload; each
 frame's decompression and complete hash admission remain inside the loop.
 Both uncompressed source size and stored memory have the explicit same cap.
 """
 def __init__(self,path,*,max_bytes=128*1024*1024):
  if type(max_bytes) is not int or max_bytes<RAW_BYTES:raise ValueError('positive bounded native-frame memory budget required')
  self.original_bytes=Path(path).stat().st_size
  if not self.original_bytes or self.original_bytes%RAW_BYTES or self.original_bytes>max_bytes:raise ValueError('bounded complete native Raw frames required')
  self.blocks=[];self.position=0;self.stored_bytes=0;self.closed=False
  with Path(path).open('rb') as stream:
   for _ in range(self.original_bytes//RAW_BYTES):
    raw=stream.read(RAW_BYTES)
    if len(raw)!=RAW_BYTES:raise ValueError('Raw source changed/truncated during preload')
    block=zlib.compress(raw,level=1);self.stored_bytes+=len(block)
    if self.stored_bytes>max_bytes:raise ValueError('compressed input exceeds finite RAM budget')
    self.blocks.append(block)
   if stream.read(1):raise ValueError('Raw source grew during preload')
 def seek(self,position):
  if self.closed:raise ValueError('Raw replay input is closed')
  if type(position) is not int or position<0 or position>self.original_bytes or position%RAW_BYTES:raise ValueError('aligned native Raw offset required')
  self.position=position
 def read(self,size):
  if self.closed:raise ValueError('Raw replay input is closed')
  if type(size) is not int or size!=RAW_BYTES:raise ValueError('one complete native Raw frame per read required')
  if self.position==self.original_bytes:return b''
  decoder=zlib.decompressobj();raw=decoder.decompress(self.blocks[self.position//RAW_BYTES],RAW_BYTES+1)
  if len(raw)!=RAW_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError('invalid compressed native Raw input')
  self.position+=RAW_BYTES
  return raw
 def __enter__(self):return self
 def __exit__(self,*args):self.blocks.clear();self.closed=True
