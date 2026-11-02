"""Optional single-file journal: strict v3 records, one actual fsync per batch.

Legacy writers/readers are untouched. Commit bytes establish structural closure,
not proof of a successful fsync. A trusted live written watermark is required
for a durable readback claim. Recovery reads only; it never repairs originals.
No pickle or executable/deserializable Python objects are stored.
"""
from __future__ import annotations
from contextlib import ExitStack
import hashlib
import io
import os
from pathlib import Path
import queue
import struct
import threading
import time
import zlib

import numpy as np
from . import storage_r21 as legacy
from structured_light.scan3d.stream_writer import WriteItem, _validate_json_value

MAGIC=b'R26JNL2\n'
FILE_HEADER=struct.Struct('<8sI32s')
HEAD=struct.Struct('<8sQQQ32s')
FOOT=struct.Struct('<8sQQ32s')
DIRECTORY=struct.Struct('<6Q')
TX_MAGIC=b'R26TXN2\0'
COMMIT_MAGIC=b'R26CMT2\0'
MAX_BODY=8*1024**2
PREVIEW='_journal_preview_r26'


class _MemoryEncoder(legacy.MultiBranchChunkWriter):
    """Existing strict validation/encoding, staging only, no disk or callbacks."""
    def _persistence_phase(self,*args):pass
    @staticmethod
    def _sync(stream):return 0.


class _StagingBudget:
    def __init__(self,limit):self.limit=limit;self.used=0


class _BoundedMemoryStream(io.BytesIO):
    def __init__(self,budget):super().__init__();self.budget=budget
    def write(self,data):
        if self.budget.used+len(data)>self.budget.limit:
            raise BufferError('journal staging buffer capacity exceeded')
        size=super().write(data);self.budget.used+=size;return size


def _encode_memory(items, codec, validation_phase, budget):
    encoder=_MemoryEncoder.__new__(_MemoryEncoder)
    encoder._condition=threading.RLock();encoder._storage_actual={}
    encoder.raw_codec=codec;encoder.validation_phase=validation_phase
    encoder.performance_trace=None
    encoder._streams=tuple(_BoundedMemoryStream(budget) for _ in range(3))
    stats=encoder._persist_group(items)
    return tuple(stream.getvalue() for stream in encoder._streams),stats,encoder._storage_actual


def _paired_sources(measurement,preview):
    for key in ('request_id','capture_epoch','raw_sha256'):
        if measurement[key]!=preview[key]:raise ValueError('preview identity/Raw mismatch')
    for key in legacy._SOURCE_PAYLOADS:
        if measurement.get(key)!=preview.get(key):raise ValueError('preview source differs from measurement')


def _record_role(record):
    packet=record['profile_payload']
    if not packet.startswith(b'MBR1'):return None
    header,_=legacy._parse_header(packet);metadata=header['metadata']
    return metadata.get('frame_metadata',{}).get('pfo3_mode',metadata.get('mode'))


class MultiBranchJournalWriterV2(legacy.MultiBranchChunkWriter):
    """Writer-compatible optional backend; preview_record is independently sealed.

    Every frame requires `preview_record` by default. Explicit diagnostic use may
    set require_preview=False. Preview and measurement keep independent MBR/XYZ
    records; neither becomes measurement data. Scratch-heavy groups fail closed
    before disk writes rather than silently exceeding the32MiB workspace.
    """
    def __init__(self,output_dir,*,require_preview=True,fault_inject=None,**kwargs):
        if type(require_preview) is not bool:raise ValueError('require_preview must be bool')
        if kwargs.get('sink') is not None:raise ValueError('journal owns its transaction sink')
        if fault_inject is not None and not callable(fault_inject):raise ValueError('fault injector must be callable')
        self.require_preview=require_preview;self._fault_inject=fault_inject
        self._journal_stream=None
        super().__init__(output_dir,**kwargs)

    def _snapshot_record(self,record):
        core=dict(record);preview=core.pop('preview_record',None)
        if preview is None and self.require_preview:raise ValueError('independent preview_record required')
        snapshot=super()._snapshot_record(core)
        if _record_role(snapshot)=='preview':raise ValueError('preview cannot be journal measurement')
        if preview is not None:
            paired=super()._snapshot_record(preview)
            _paired_sources(snapshot,paired)
            if _record_role(paired)!='preview':raise ValueError('preview_record must be explicitly preview-only')
            snapshot[PREVIEW]=paired
        return snapshot

    def _fault(self,phase):
        if self._fault_inject is not None:self._fault_inject(phase,self._journal_stream)

    def _run(self):
        group=[]
        try:
            with (self.output_dir/'journal.r26j2').open('xb') as stream:
                self._journal_stream=stream
                flags=int(self.require_preview)
                header_hash=hashlib.sha256(MAGIC+struct.pack('<I',flags)).digest()
                stream.write(FILE_HEADER.pack(MAGIC,flags,header_hash));stream.flush();os.fsync(stream.fileno())
                # Persist the newly created file name on platforms supporting
                # directory fsync; no sidecar participates in transaction commit.
                if os.name=='posix':
                    for directory in (self.output_dir,self.output_dir.parent):
                        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
                        try:os.fsync(fd)
                        finally:os.close(fd)
                while True:
                    try:first=self._queue.get(timeout=.01)
                    except queue.Empty:
                        with self._condition:
                            if self._closing.is_set() and self._queue.empty():return
                        continue
                    self._persistence_phase([first],'collect')
                    group=self._collect(first);self._persistence_phase(group,'worker')
                    wall,cpu=time.perf_counter(),time.thread_time()
                    try:
                        stats=self._persist_group(group)
                        stats.update(first_sequence=group[0].sequence,last_sequence=group[-1].sequence,
                            item_count=len(group),frame_count=sum(item.kind=='frame' for item in group),
                            event_count=sum(item.kind=='event' for item in group),
                            io_s=time.perf_counter()-wall,batch_s=time.perf_counter()-wall,
                            thread_cpu_s=time.thread_time()-cpu)
                        with self._condition:
                            self._persistence_complete(group);self._written+=len(group)
                            for item in group:self._pending_ids.pop(item.sequence,None)
                            self._batch_count+=1;self._batch_stats.append(stats)
                            self._condition.notify_all()
                    except BaseException as exc:
                        self._mark_failed(group[0],exc);return
                    finally:
                        for item in group:self._queue.task_done()
        except BaseException as exc:
            self._mark_failed(group[0] if group else WriteItem(0,'event',{}),exc)
        finally:
            self._journal_stream=None
            with self._condition:
                self._worker_stopped=True;self._condition.notify_all()

    def _persist_group(self,group):
        # Queued snapshots remain within the inherited64MiB bound. Reserve a
        # conservative3x envelope for the transient encoded sections/body/views.
        resident=sum(self._resident_bytes(item.payload) for item in group)
        if resident*3>self.scratch_bytes:
            raise BufferError('journal staging exceeds bounded scratch; reduce group size')
        self._persistence_phase(group,'validate')
        measured=[];preview=[];preview_parent=[]
        for local,item in enumerate(group,1):
            payload=dict(item.payload);paired=payload.pop(PREVIEW,None)
            measured.append(WriteItem(local,item.kind,payload))
            if paired is not None:
                _paired_sources(payload,paired)
                preview.append(WriteItem(len(preview)+1,'frame',paired))
                preview_parent.append(item.sequence)
        # Both sides complete all legacy strict validation before first diskbyte.
        budget=_StagingBudget(min(MAX_BODY-DIRECTORY.size,self.scratch_bytes//3))
        mparts,mstats,mactual=_encode_memory(measured,self.raw_codec,self.validation_phase,budget)
        if preview:
            pparts,pstats,pactual=_encode_memory(preview,self.raw_codec,self.validation_phase,budget)
        else:pparts=(b'',b'',b'');pstats={};pactual={}
        lengths=tuple(len(part) for part in mparts+pparts)
        total=DIRECTORY.size+sum(lengths)
        if total>MAX_BODY or total*3>self.scratch_bytes:
            raise BufferError('journal encoded transaction exceeds bounded scratch')
        body=DIRECTORY.pack(*lengths)+b''.join(mparts+pparts)
        first,last=group[0].sequence,group[-1].sequence
        head=HEAD.pack(TX_MAGIC,first,last,len(body),hashlib.sha256(body).digest())
        digest=hashlib.sha256(head);digest.update(body)
        foot=FOOT.pack(COMMIT_MAGIC,first,last,digest.digest())
        self._persistence_phase(group,'build_write')
        stream=self._journal_stream;stream.write(head);stream.write(body)
        self._fault('before_commit')
        stream.write(foot)
        self._persistence_phase(group,'journal_sync')
        started=time.perf_counter();stream.flush();self._fault('during_sync');os.fsync(stream.fileno())
        sync_s=time.perf_counter()-started
        self._fault('after_commit')
        actual={item.sequence:mactual[local] for local,item in enumerate(group,1)}
        for local,parent in enumerate(preview_parent,1):actual[parent]+=pactual[local]
        actual[first]+=HEAD.size+FOOT.size+DIRECTORY.size+(FILE_HEADER.size if first==1 else 0)
        with self._condition:self._storage_actual.update(actual)
        trace=self.performance_trace
        if trace is not None:trace.durable_group(legacy._trace_compatible_group(group),time.perf_counter())
        return dict(payload_fsync_s=sync_s,index_fsync_s=0.,journal_fsync_s=sync_s,
            payload_bytes=HEAD.size+len(body)+FOOT.size,
            validation_s=mstats['validation_s']+pstats.get('validation_s',0.),
            raw_compress_s=mstats['raw_compress_s']+pstats.get('raw_compress_s',0.),
            raw_input_bytes=mstats['raw_input_bytes']+pstats.get('raw_input_bytes',0),
            raw_stored_bytes=mstats['raw_stored_bytes']+pstats.get('raw_stored_bytes',0),
            storage_format='R26_JOURNAL_V2',sync_done_perf_s=time.perf_counter())


class _ReaderBudget:
    """Conservative shared resident budget, including JSON before decoding.

    Encoded transaction bytes are separately bounded by MAX_BODY. JSON token
    charges cover containers, dict entries, keys and values; strings reserve
    four bytes per encoded byte plus object overhead. This intentionally errs
    on the side of rejecting pathological metadata before json.loads allocates.
    """
    def __init__(self,limit):
        if type(limit) is not int or limit<0:raise ValueError('invalid journal reader budget')
        self.limit=limit;self.used=0

    def ensure(self,size):
        if size<0 or self.used+size>self.limit:
            raise ValueError('journal reader resident scratch exceeded')

    def charge(self,size):
        self.ensure(size);self.used+=size


def _json_preflight(line,budget):
    # Bound JSON object amplification before allocating Python containers.
    i=0;depth=0;length=len(line)
    while i<length:
        value=line[i]
        if value in b' \t\r\n,:':i+=1;continue
        if value in (123,91):
            depth+=1
            if depth>64:raise ValueError('journal JSON nesting capacity exceeded')
            budget.charge(256);i+=1;continue
        if value in (125,93):
            depth-=1
            if depth<0:raise ValueError('journal JSON nesting mismatch')
            i+=1;continue
        if value==34:
            start=i;i+=1
            while i<length:
                if line[i]==92:i+=2;continue
                if line[i]==34:break
                i+=1
            if i>=length:raise ValueError('journal JSON unterminated string')
            budget.charge(128+4*(i-start+1));i+=1;continue
        start=i
        while i<length and line[i] not in b' \t\r\n,:{}[]':i+=1
        if i==start:raise ValueError('journal JSON invalid token')
        budget.charge(128)
    if depth:raise ValueError('journal JSON nesting mismatch')


def _json_lines(data,budget):
    result=[];view=memoryview(data).cast('B');start=0
    # No splitlines/full-index copy: reject an overlong line before copying it.
    for end,value in enumerate(view):
        if end-start>=1024**2:raise ValueError('journal JSONL length/truncation')
        if value!=10:continue
        line=bytes(view[start:end+1]);start=end+1
        _json_preflight(line,budget)
        value=legacy._json(line)
        if type(value) is not dict:raise ValueError('journal index must contain objects')
        _validate_json_value(value,path='journal index');result.append(value)
        if len(result)>33:raise ValueError('journal JSONL record capacity exceeded')
    if start!=len(view):raise ValueError('journal JSONL length/truncation')
    return result


def _decode_memory(parts, resident_limit=16*1024**2, *, budget=None):
    if budget is None:budget=_ReaderBudget(resident_limit)
    payload,index,event_data=parts
    rows=_json_lines(index,budget);events=_json_lines(event_data,budget)
    if not rows or rows[-1].get('kind')!='batch_commit':raise ValueError('journal stage missing commit')
    commit=rows.pop()
    if any(row.get('kind')!='frame' or 'event' in row for row in rows):
        raise ValueError('journal frame index kind mismatch')
    if any(set(row)!=set(('sequence','event','batch_first_sequence','batch_last_sequence')) for row in events):
        raise ValueError('journal event index shape mismatch')
    items=sorted(rows+events,key=lambda row:row.get('sequence',0))
    count=len(items)
    if any(type(commit.get(key)) is not int for key in
           ('first_sequence','last_sequence','item_count','frame_count','event_count','payload_end')):
        raise ValueError('journal commit counts must be integers')
    if any(type(row.get('sequence')) is not int for row in items):raise ValueError('journal sequence must be integer')
    if (count>32 or commit.get('first_sequence')!=1 or commit.get('last_sequence')!=count
        or commit.get('item_count')!=count or commit.get('frame_count')!=len(rows)
        or commit.get('event_count')!=len(events) or [row.get('sequence') for row in items]!=list(range(1,count+1))):
        raise ValueError('journal item continuity/count mismatch')
    offset=0;restored=[];frame_charge=0
    def piece(at,size,digest):
        nonlocal offset,frame_charge
        if type(at) is not int or type(size) is not int or at!=offset or not 0<=size<=MAX_BODY or at+size>len(payload):
            raise ValueError('journal payload offset/length mismatch')
        budget.ensure(frame_charge+size+128)
        frame_charge+=size+128
        data=bytes(payload[at:at+size]);offset+=size
        if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('journal piece hash mismatch')
        return data
    for row in items:
        if row.get('batch_first_sequence')!=1 or row.get('batch_last_sequence')!=count:
            raise ValueError('journal item batch mismatch')
        if 'event' in row:
            restored.append(dict(kind='event',event=row['event'],metadata=row));continue
        if (row.get('kind')!='frame' or row.get('storage_format')!=legacy.SCHEMA
            or row.get('raw_bytes')!=legacy.RAW_BYTES or row.get('raw_file')!='payloads.bin'
            or row.get('profile_file')!='payloads.bin'):
            raise ValueError('journal unsupported frame schema')
        frame_charge=4096
        compressed=piece(row['raw_offset'],row['raw_stored_bytes'],row['raw_stored_sha256'])
        if row['raw_codec']=='none':raw=compressed
        elif row['raw_codec'] in ('zlib','zlib1'):
            budget.ensure(frame_charge+legacy.RAW_BYTES+128)
            frame_charge+=legacy.RAW_BYTES+128
            decoder=zlib.decompressobj();raw=decoder.decompress(compressed,legacy.RAW_BYTES+1)
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError('journal compressed Raw boundary mismatch')
        else:raise ValueError('journal unknown Raw codec')
        if len(raw)!=legacy.RAW_BYTES or hashlib.sha256(raw).hexdigest()!=row['raw_sha256']:
            raise ValueError('journal Raw length/hash mismatch')
        packet=piece(row['profile_offset'],row['profile_bytes'],row['profile_sha256'])
        sources={}
        desc=row.get('source_payloads',{})
        if set(desc)-set(legacy._SOURCE_PAYLOADS):raise ValueError('unknown journal source')
        for key in legacy._SOURCE_PAYLOADS:
            if key not in desc:continue
            item=desc[key]
            if item['file']!='payloads.bin':raise ValueError('journal source file mismatch')
            sources[key]=piece(item['offset'],item['bytes'],item['sha256'])
        points={}
        if set(row['point_files'])!=set(legacy._POINTS):raise ValueError('journal point set mismatch')
        for key in legacy._POINTS:
            item=row['point_files'][key];points[key]=None
            if item is None:continue
            if item['file']!='payloads.bin' or item['format']!='npy':raise ValueError('journal XYZ descriptor mismatch')
            data=piece(item['offset'],item['bytes'],item['sha256']);stream=io.BytesIO(data)
            if np.lib.format.read_magic(stream)!=(1,0):raise ValueError('journal NPY version mismatch')
            shape,fortran,dtype=np.lib.format.read_array_header_1_0(stream)
            if (len(shape)!=2 or any(type(v) is not int or v<0 for v in shape)
                or shape[1]!=3 or shape[0]>legacy.MAX_POINTS or fortran
                or dtype.hasobject or dtype.kind not in 'iuf' or dtype.itemsize not in (1,2,4,8)
                or stream.tell()+shape[0]*3*dtype.itemsize!=len(data)):
                raise ValueError('journal XYZ header/length invalid')
            value=np.frombuffer(data,dtype=dtype,offset=stream.tell()).reshape(shape)
            if not np.isfinite(value).all():raise ValueError('journal XYZ nonfinite')
            value.setflags(write=False);points[key]=value
        record=dict(request_id=row['request_id'],capture_epoch=row['capture_epoch'],raw=raw,
            raw_sha256=row['raw_sha256'],profile_payload=packet,summary=row['summary'],
            motion=row['motion'],exposure=row['exposure'],**points,**sources)
        legacy._validate_record(record)
        budget.charge(legacy.MultiBranchChunkWriter._resident_bytes(record))
        restored.append(dict(kind='frame',record=record,metadata=row))
    if offset!=len(payload) or commit.get('payload_end')!=offset:raise ValueError('journal payload trailing/watermark mismatch')
    return restored


class JournalReaderV2:
    """Streaming strict audit; recovery permits truncated tails, never bad checksums.

    Without trusted written_watermark, even intact commits are STRUCTURAL_ONLY.
    A failed writer sidecar rejects ordinary success reads. recovery=True may
    inspect structurally complete prefix but never promotes it to durable.
    """
    def __init__(self,directory,*,written_watermark=None,recovery=False):
        if written_watermark is not None and (type(written_watermark) is not int or written_watermark<0):
            raise ValueError('nonnegative confirmed watermark required')
        self.directory=Path(directory);self.watermark=written_watermark;self.recovery=recovery
        self.report=None

    def __iter__(self):
        failure=self.directory/'failures.jsonl'
        if failure.exists() and failure.stat().st_size and not self.recovery:
            raise ValueError('journal writer failure receipt; durable success forbidden')
        expected=1;last_request=-1;epoch=None;committed_end=FILE_HEADER.size;incomplete=False
        with (self.directory/'journal.r26j2').open('rb') as stream:
            file_header=stream.read(FILE_HEADER.size)
            if len(file_header)!=FILE_HEADER.size:raise ValueError('journal magic/version mismatch')
            magic,preview_required,header_hash=FILE_HEADER.unpack(file_header)
            if (magic!=MAGIC or preview_required not in (0,1)
                or header_hash!=hashlib.sha256(magic+struct.pack('<I',preview_required)).digest()):
                raise ValueError('journal magic/version/flags checksum mismatch')
            while True:
                head=stream.read(HEAD.size)
                if not head:break
                if len(head)!=HEAD.size:
                    if not self.recovery:raise ValueError('incomplete journal transaction header')
                    incomplete=True;break
                magic,first,last,length,body_hash=HEAD.unpack(head)
                if magic!=TX_MAGIC or first!=expected or last<first or last-first>=32 or not DIRECTORY.size<=length<=MAX_BODY:
                    raise ValueError('journal transaction header/count/capacity mismatch')
                body=stream.read(length);foot=stream.read(FOOT.size)
                if len(body)!=length or len(foot)!=FOOT.size:
                    if not self.recovery:raise ValueError('incomplete journal transaction body/commit')
                    incomplete=True;break
                marker,f_first,f_last,digest=FOOT.unpack(foot)
                hashed=hashlib.sha256(head);hashed.update(body)
                if (marker!=COMMIT_MAGIC or f_first!=first or f_last!=last or digest!=hashed.digest()
                    or hashlib.sha256(body).digest()!=body_hash):raise ValueError('journal transaction checksum/commit mismatch')
                if self.watermark is not None and last>self.watermark:raise ValueError('journal commit exceeds confirmed written watermark')
                lengths=DIRECTORY.unpack_from(body)
                if DIRECTORY.size+sum(lengths)!=length:raise ValueError('journal section length mismatch')
                view=memoryview(body);parts=[];at=DIRECTORY.size
                for size in lengths:parts.append(view[at:at+size]);at+=size
                budget=_ReaderBudget(16*1024**2)
                measured=_decode_memory(parts[:3],budget=budget)
                preview=_decode_memory(parts[3:],budget=budget) if any(lengths[3:]) else []
                if len(measured)!=last-first+1:raise ValueError('journal outer/inner sequence mismatch')
                preview_by_id={}
                for row in preview:
                    if row['kind']!='frame':raise ValueError('journal preview cannot contain events')
                    record=row['record'];identity=(record['capture_epoch'],record['request_id'])
                    if identity in preview_by_id:raise ValueError('journal repeated preview identity')
                    preview_by_id[identity]=record
                for row in measured:
                    row['sequence']=first+row['metadata']['sequence']-1
                    row['durability']='CONFIRMED' if self.watermark is not None and not self.recovery else 'STRUCTURAL_ONLY'
                    if row['kind']=='frame':
                        record=row['record'];request=record['request_id'];current=record['capture_epoch']
                        if _record_role(record)=='preview':raise ValueError('journal preview in measurement channel')
                        if (type(request) is not int or type(current) is not int or not 0<=request<2**32 or not 0<=current<2**16
                            or request<=last_request or epoch not in (None,current)):
                            raise ValueError('journal measurement identity regression')
                        last_request=request;epoch=current
                        pair=preview_by_id.pop((current,request),None)
                        if pair is None and preview_required:raise ValueError('journal required preview missing')
                        if pair is not None:
                            _paired_sources(record,pair)
                            if _record_role(pair)!='preview':
                                raise ValueError('journal preview role mismatch')
                        row['preview_record']=pair
                if preview_by_id:raise ValueError('journal orphan preview identity')
                committed_end=stream.tell();expected=last+1
                yield from measured
        if self.watermark is not None and self.watermark!=expected-1:raise ValueError('journal missing confirmed commit')
        self.report=dict(storage_format='R26_JOURNAL_V2',written=expected-1,incomplete_tail=incomplete,
            complete=not incomplete,last_structural_commit_offset=committed_end,
            durable_confirmed=self.watermark is not None and not self.recovery and not incomplete,
            recovery_read_only=self.recovery,preview_required=bool(preview_required))
