"""Optional single-file v4 transactions, with one fsync before batch acknowledgement.

The journal contains both payload and authoritative indexes. It has no external
index whose flush could be mistaken for durability. Recovery reports validated
complete transactions only: file bytes cannot prove that the original caller
received an acknowledgement, or that an interrupted fsync succeeded.
"""
from __future__ import annotations

from bisect import bisect_right
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import zlib
from resource_policy import accounted_file_bytes

NAME='transactions.v4.bin'
MAGIC=b'S3J15C\0\0'
TRAILER_MAGIC=b'COM15C\0\0'
HEADER_BYTES=128
TRAILER_BYTES=64
HEADER_BASE=struct.Struct('<8sII6Q5I')
TRAILER=struct.Struct('<8sQQQ32s')
LENGTHS=struct.Struct('<QQ')
MAX_TRANSACTION_BYTES=64*1024*1024
MAX_TRANSACTIONS=32768
TRANSACTION_BUFFER_BYTES=512*1024
MAX_TRANSACTION_BUFFER_BYTES=2*1024*1024


def _write_all(stream,data):
    view=memoryview(data).cast('B')
    if stream.write(view)!=len(view):
        raise OSError('short journal write')


class _TransactionStream:
    """Coalesce small transactions without changing the on-disk contract.

    The header's final values are available only after the payload and indexes.
    A fixed-size buffer lets ordinary transactions patch that header in RAM and
    issue one sequential file write. Larger legal transactions spill to the old
    streaming/seek path. This buffer does not extend the admitted writer queue.
    It never acknowledges data: flush and the owner's real fsync still follow.
    """
    def __init__(self,stream,buffer_bytes=TRANSACTION_BUFFER_BYTES):
        if type(buffer_bytes) is not int or not 0<=buffer_bytes<=MAX_TRANSACTION_BUFFER_BYTES:
            raise ValueError('journal transaction buffer capacity')
        self.stream=stream;self.start=stream.tell();self.buffer=bytearray()
        self.buffer_bytes=buffer_bytes
        self.position=0;self.streaming=False;self.write_calls=0
        self.buffered_peak_bytes=0

    def tell(self):
        return self.stream.tell() if self.streaming else self.start+self.position

    def _physical_write(self,data):
        self.write_calls+=1
        _write_all(self.stream,data)

    def _spill(self):
        if self.streaming:return
        position=self.start+self.position
        if self.buffer:self._physical_write(self.buffer)
        self.buffer=bytearray();self.streaming=True
        if self.stream.tell()!=position:self.stream.seek(position)

    def write(self,data):
        view=memoryview(data).cast('B')
        end=self.position+len(view)
        if self.streaming:
            self._physical_write(view)
        elif end<=self.buffer_bytes:
            self.buffer[self.position:end]=view;self.position=end
            self.buffered_peak_bytes=max(self.buffered_peak_bytes,len(self.buffer))
        else:
            self._spill();self._physical_write(view)
        return len(view)

    def seek(self,offset):
        relative=offset-self.start
        if not self.streaming and 0<=relative<=len(self.buffer):
            self.position=relative;return offset
        self._spill();return self.stream.seek(offset)

    def flush(self):
        self._spill();self.stream.flush()


def _header(txn,first,last,start,payload_bytes,metadata_bytes,items,frames,events,pcrc,mcrc):
    base=HEADER_BASE.pack(MAGIC,4,HEADER_BYTES,txn,first,last,start,payload_bytes,
                          metadata_bytes,items,frames,events,pcrc,mcrc)
    return base+struct.pack('<I',zlib.crc32(base)&0xffffffff)+bytes(HEADER_BYTES-len(base)-4)


def _parse_header(blob):
    if len(blob)!=HEADER_BYTES:raise ValueError('truncated journal header')
    base=blob[:HEADER_BASE.size]
    fields=HEADER_BASE.unpack(base)
    if (fields[:3]!=(MAGIC,4,HEADER_BYTES)
            or struct.unpack_from('<I',blob,len(base))[0]!=(zlib.crc32(base)&0xffffffff)
            or any(blob[len(base)+4:])):
        raise ValueError('journal header identity/checksum/reserved mismatch')
    (_,_,_,txn,first,last,start,payload,meta,items,frames,events,pcrc,mcrc)=fields
    if (not 1<=txn<=MAX_TRANSACTIONS or not 1<=items<=32 or first<1
            or last-first+1!=items or frames+events!=items
            or payload>MAX_TRANSACTION_BYTES or not 16<=meta<=MAX_TRANSACTION_BYTES):
        raise ValueError('journal header bounds/count mismatch')
    return dict(txn=txn,first=first,last=last,start=start,payload=payload,meta=meta,
                items=items,frames=frames,events=events,pcrc=pcrc,mcrc=mcrc)


class _Payload:
    def __init__(self,journal):self.journal=journal
    def tell(self):return self.journal.logical_end
    def write(self,data):return self.journal.write_payload(data)


class _Index:
    def __init__(self,journal,kind):self.journal=journal;self.kind=kind
    def write(self,data):
        data=bytes(data)
        if sum(map(len,self.journal.indexes[self.kind]))+len(data)>MAX_TRANSACTION_BYTES:
            raise ValueError('journal metadata capacity exceeded')
        self.journal.indexes[self.kind].append(data)
        return len(data)


class JournalSink15C:
    def __init__(self,stream,*,buffer_bytes=TRANSACTION_BUFFER_BYTES):
        if type(buffer_bytes) is not int or not 0<=buffer_bytes<=MAX_TRANSACTION_BUFFER_BYTES:
            raise ValueError('journal transaction buffer capacity')
        self.buffer_bytes=buffer_bytes
        self.stream=stream;self.logical_end=0;self.txn=0;self.current=None
        self.payload=_Payload(self);self.frames=_Index(self,'frames');self.events=_Index(self,'events')
        self.metrics={};self.budget_accounted_bytes=0

    @property
    def streams(self):return self.payload,self.frames,self.events

    def begin(self,group):
        if self.current is not None:raise RuntimeError('unfinished journal transaction')
        if not 1<=len(group)<=32:raise ValueError('journal group capacity')
        self.txn+=1
        if self.txn>MAX_TRANSACTIONS:raise ValueError('journal transaction bound reached')
        self.current=dict(physical=self.stream.tell(),start=self.logical_end,
            first=group[0].sequence,last=group[-1].sequence,items=len(group),
            frames=sum(item.kind=='frame' for item in group),
            events=sum(item.kind=='event' for item in group))
        self.indexes={'frames':[],'events':[]};self.payload_crc=0;self.digest=hashlib.sha256()
        self.transaction_stream=_TransactionStream(self.stream,self.buffer_bytes)
        self.metrics=dict(fsync_calls=0,journal_fsync_s=0.,journal_finalize_s=0.)
        _write_all(self.transaction_stream,bytes(HEADER_BYTES))

    def write_payload(self,data):
        if self.current is None:raise RuntimeError('journal batch not active')
        view=memoryview(data).cast('B')
        if self.logical_end-self.current['start']+len(view)>MAX_TRANSACTION_BYTES:
            raise ValueError('journal payload capacity exceeded')
        _write_all(self.transaction_stream,view)
        self.payload_crc=zlib.crc32(view,self.payload_crc)&0xffffffff
        self.digest.update(view);self.logical_end+=len(view)
        return len(view)

    def sync(self,stream):
        if stream is self.payload or stream is self.events:
            # The batch is not acknowledged here. Its payload and indexes are
            # synchronized together by the final frames/commit operation.
            return 0.
        if stream is not self.frames:raise ValueError('foreign journal stream')
        return self.finish()

    def observed_file_bytes(self):
        return accounted_file_bytes(os.fstat(self.stream.fileno()))

    def finish(self):
        started=time.perf_counter();c=self.current
        if c is None:raise RuntimeError('journal transaction already finished')
        frame_bytes=sum(map(len,self.indexes['frames']))
        event_bytes=sum(map(len,self.indexes['events']))
        prefix=LENGTHS.pack(event_bytes,frame_bytes)
        meta_bytes=16+event_bytes+frame_bytes
        if meta_bytes>MAX_TRANSACTION_BYTES:raise ValueError('journal metadata capacity exceeded')
        metadata_crc=0
        for piece in [prefix,*self.indexes['events'],*self.indexes['frames']]:
            _write_all(self.transaction_stream,piece)
            metadata_crc=zlib.crc32(piece,metadata_crc)&0xffffffff;self.digest.update(piece)
        payload_bytes=self.logical_end-c['start']
        header=_header(self.txn,c['first'],c['last'],c['start'],payload_bytes,meta_bytes,
            c['items'],c['frames'],c['events'],self.payload_crc,metadata_crc)
        total=HEADER_BYTES+payload_bytes+meta_bytes+TRAILER_BYTES
        trailer=TRAILER.pack(TRAILER_MAGIC,self.txn,c['physical'],total,self.digest.digest())
        stream=self.transaction_stream
        _write_all(stream,trailer)
        end=stream.tell()
        stream.seek(c['physical']);_write_all(stream,header);stream.seek(end)
        stream.flush()
        before=time.perf_counter();self.metrics['fsync_calls']+=1
        # An exception leaves current non-None, no durable acknowledgement and
        # all original bytes available for failure diagnosis.
        os.fsync(self.stream.fileno())
        elapsed=time.perf_counter()-before
        self.metrics.update(journal_fsync_s=elapsed,journal_finalize_s=time.perf_counter()-started,
            transaction_bytes=total,payload_bytes=payload_bytes,
            framing_and_metadata_bytes=total-payload_bytes,
            journal_write_calls=stream.write_calls,
            journal_buffer_peak_bytes=stream.buffered_peak_bytes,
            journal_sequential_buffered=total<=self.buffer_bytes)
        self.current=None;self.indexes={'frames':[],'events':[]}
        return elapsed


def _json(blob):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate journal JSON key')
            result[key]=value
        return result
    return json.loads(blob,object_pairs_hook=pairs,
        parse_constant=lambda value:(_ for _ in ()).throw(ValueError('nonfinite journal JSON')))


class JournalReader15C:
    """Validate framing first, then expose old logical-payload/index semantics."""
    def __init__(self,path,*,recover_incomplete_tail=False):
        self.path=Path(path);self.batches=[];self.logical_end=0;self.valid_end=0
        self.incomplete_tail=False;self._scan(recover_incomplete_tail)

    def _scan(self,recover):
        size=self.path.stat().st_size;expected_sequence=1
        with self.path.open('rb') as stream:
            while stream.tell()<size:
                physical=stream.tell()
                blob=stream.read(HEADER_BYTES)
                if len(blob)<HEADER_BYTES or not any(blob):
                    if recover:self.incomplete_tail=True;break
                    raise ValueError('uncommitted/truncated journal header')
                h=_parse_header(blob)
                if (h['txn']!=len(self.batches)+1 or h['first']!=expected_sequence
                        or h['start']!=self.logical_end):
                    raise ValueError('journal transaction/sequence/payload continuity mismatch')
                total=HEADER_BYTES+h['payload']+h['meta']+TRAILER_BYTES
                if physical+total>size:
                    if recover:self.incomplete_tail=True;break
                    raise ValueError('truncated journal transaction')
                digest=hashlib.sha256()
                for key,expected_crc in [('payload',h['pcrc']),('meta',h['mcrc'])]:
                    left=h[key];crc=0
                    while left:
                        block=stream.read(min(left,1024*1024))
                        if not block:raise ValueError('truncated journal data')
                        crc=zlib.crc32(block,crc)&0xffffffff;digest.update(block);left-=len(block)
                    if crc!=expected_crc:raise ValueError('journal '+key+' CRC mismatch')
                trailer=stream.read(TRAILER_BYTES)
                if TRAILER.unpack(trailer)!=(TRAILER_MAGIC,h['txn'],physical,total,digest.digest()):
                    raise ValueError('journal trailer/hash mismatch')
                h['physical']=physical;h['metadata_at']=physical+HEADER_BYTES+h['payload']
                stream.seek(h['metadata_at']);event_bytes,frame_bytes=LENGTHS.unpack(stream.read(16))
                if 16+event_bytes+frame_bytes!=h['meta']:raise ValueError('journal index lengths mismatch')
                h.update(event_bytes=event_bytes,frame_bytes=frame_bytes)
                self.batches.append(h);self.logical_end+=h['payload'];self.valid_end=physical+total
                expected_sequence=h['last']+1;stream.seek(self.valid_end)

    def lines(self,kind):
        if kind not in ('frames','events'):raise ValueError('unknown journal index')
        with self.path.open('rb') as stream:
            for h in self.batches:
                start=h['metadata_at']+16+(h['event_bytes'] if kind=='frames' else 0)
                left=h['frame_bytes'] if kind=='frames' else h['event_bytes']
                stream.seek(start);count=0;commit_count=0
                while left:
                    line=stream.readline(min(left,1024*1024+1))
                    if not line or len(line)>1024*1024 or not line.endswith(b'\n'):
                        raise ValueError('oversize/truncated journal index row')
                    left-=len(line)
                    entry=_json(line)
                    if not isinstance(entry,dict):raise ValueError('journal index row must be object')
                    if kind=='frames' and entry.get('kind')=='batch_commit':
                        if (commit_count or left or entry.get('first_sequence')!=h['first']
                                or entry.get('last_sequence')!=h['last']
                                or entry.get('item_count')!=h['items']
                                or entry.get('frame_count')!=h['frames']
                                or entry.get('event_count')!=h['events']
                                or entry.get('payload_end')!=h['start']+h['payload']):
                            raise ValueError('journal header/commit mismatch')
                        commit_count+=1
                    else:count+=1
                    yield entry
                if count!=h[kind] or (kind=='frames' and commit_count!=1):
                    raise ValueError('journal index record count mismatch')

    def payload_stream(self):return JournalPayloadReader15C(self)

    def recovery_report(self):
        return dict(schema='SCAN3D_JOURNAL_V4_RECOVERY',transactions=len(self.batches),
            validated_last_sequence=(self.batches[-1]['last'] if self.batches else 0),
            validated_bytes=self.valid_end,payload_bytes=self.logical_end,
            file_bytes=self.path.stat().st_size,incomplete_tail=self.incomplete_tail,
            original_run_durable=False,original_acknowledgement_verified=False,
            semantic_payload_validation=False,
            note='Validated bytes are recovery evidence; they do not retroactively acknowledge the failed/interrupted original run.')


class JournalPayloadReader15C:
    def __init__(self,reader):
        self.reader=reader;self.stream=reader.path.open('rb');self.position=0
        self.ends=[row['start']+row['payload'] for row in reader.batches]
    def __enter__(self):return self
    def __exit__(self,*args):self.stream.close()
    def read(self,count=-1):
        available=self.reader.logical_end-self.position
        if count<0:count=available
        count=min(count,available);pieces=[]
        while count:
            at=bisect_right(self.ends,self.position)
            if at>=len(self.reader.batches):raise ValueError('journal logical offset missing')
            h=self.reader.batches[at];take=min(count,h['start']+h['payload']-self.position)
            self.stream.seek(h['physical']+HEADER_BYTES+self.position-h['start'])
            block=self.stream.read(take)
            if len(block)!=take:raise ValueError('truncated journal logical payload')
            pieces.append(block);self.position+=take;count-=take
        return b''.join(pieces)


def recovery_report(path):
    """Read only. Never truncates, rewrites, or marks the original run complete."""
    return JournalReader15C(path,recover_incomplete_tail=True).recovery_report()
