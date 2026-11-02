"""Finite losslessly encoded full native images; no feature or XYZ cache."""
from pathlib import Path
import json,hashlib,zlib,time
from structured_light.scan3d.payload_digest import payload_sha256
RAW_BYTES=752*480
class NativeImageReplay:
    def __init__(self,directory):
        r=Path(directory);self.metadata=json.loads((r/'INPUT_MANIFEST.json').read_text())
        self.frames=[json.loads(x) for x in (r/'frames.jsonl').read_text().splitlines()]
        self.index=json.loads((r/'RAW_BLOCK_INDEX.json').read_text())
        self.blocks=(r/'raw.gray8.zblocks').read_bytes()
        for file,key in [('calibration.json','calibration_sha256'),('frames.jsonl','frames_sha256')]:
            if hashlib.sha256((r/file).read_bytes()).hexdigest()!=self.metadata[key]:raise ValueError('source metadata hash mismatch: '+file)
        if hashlib.sha256(self.blocks).hexdigest()!=self.metadata['encoded_sha256']:raise ValueError('encoded raw hash mismatch')
        if len(self.frames)!=len(self.index) or len(self.frames)!=240:raise ValueError('complete 240 frames required')
        end=0
        for i,(fr,b) in enumerate(zip(self.frames,self.index)):
            if fr['frame_id']!=i or fr['raw_offset']!=i*RAW_BYTES or fr['raw_bytes']!=RAW_BYTES or b['offset']!=end:
                raise ValueError('source frame/index mismatch')
            end+=b['bytes']
        if end!=len(self.blocks):raise ValueError('source trailing/truncated bytes')
    def read_frame(self,i):
        started=time.perf_counter();cpu=time.thread_time()
        b=self.index[i];compressed=self.blocks[b['offset']:b['offset']+b['bytes']]
        d=zlib.decompressobj();raw=d.decompress(compressed,RAW_BYTES+1)
        if len(raw)!=RAW_BYTES or not d.eof or d.unused_data or d.unconsumed_tail:raise ValueError('raw decompress size/trailing data')
        decompressed=time.perf_counter();decompressed_cpu=time.thread_time()
        if payload_sha256(raw)!=self.frames[i]['raw_sha256']:raise ValueError('raw frame hash mismatch')
        self.last_stage_timings=dict(decompress=dict(wall_s=decompressed-started,thread_cpu_s=decompressed_cpu-cpu),
            hash=dict(wall_s=time.perf_counter()-decompressed,thread_cpu_s=time.thread_time()-decompressed_cpu))
        return raw
