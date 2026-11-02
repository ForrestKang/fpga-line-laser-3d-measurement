"""R33 UV observation backend: actual PL, shared geometry, HDMI, full journal.

Finite saved-input engineering replay only. Never loads an overlay, captures
a camera or commands a motor. R33 archive/geometry is deliberately versioned.
"""
from pathlib import Path
import argparse,hashlib,json,os,queue,shutil,sys,threading,time,zlib
import numpy as np
RAW_BYTES=752*480
# A legal zlib block can be slightly larger than Raw. Reject unbounded index
# lengths before read; the allowance also admits legacy non-optimal encoders.
MAX_COMPRESSED_BYTES=RAW_BYTES+65536
PREP_FRAME_BYTES=RAW_BYTES+1+MAX_COMPRESSED_BYTES+65536
PREP_FRAMES=4
PREP_BYTES=PREP_FRAMES*PREP_FRAME_BYTES
DISPLAY_CLOSE_TIMEOUT_S=30.
# Viewing hold after a run; the harness exec timeout (600 s) bounds run + hold.
DISPLAY_HOLD_MAX_S=300
WORKER_JOIN_TIMEOUT_S=6.
ROOT=Path(__file__).resolve().parents[1]
for p in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',ROOT/'structured_light_system_geometry_v1/src/python'):
 sys.path.insert(0,str(p))

def save(path,value):
 with Path(path).open('x',encoding='utf8') as f:
  json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())

def display_cleanup_confirmed(error,error_type):
 # A historical startup/display error may survive a fully confirmed close.
 # Its run still fails; only this exact owned close error can release hardware.
 if type(error) is not error_type:return False
 receipt=getattr(error,'receipt',None)
 return (isinstance(receipt,dict)
  and all(receipt.get(key) is True for key in ('cleanup_confirmed','owner_closed','worker_joined'))
  and type(receipt.get('worker_exitcode')) is int and receipt['worker_exitcode']==0
  and receipt.get('retained_lock_fd') is False)

def submit_owned_result(writer,result,stop,refresh,timeout_s=30.):
 """Retry atomic admission without surrendering ownership or hiding failures.

 A full queue is pressure, not a failed frame. Display has already received
 this immutable result; commits and cancellation remain serviceable while the
 writer drains. No acceptance or persistence is reported before its real ACK.
 """
 if not np.isfinite(timeout_s) or timeout_s<0:raise ValueError('finite nonnegative submit timeout required')
 deadline=time.monotonic()+timeout_s;retries=0
 while True:
  refresh()
  if stop.is_set():
   # Producers publish their causal error before setting stop. Give refresh
   # one final opportunity to surface it instead of masking it as cancellation.
   refresh()
   raise RuntimeError('writer admission cancelled; frame remains owned')
  if retries and time.monotonic()>=deadline:
   raise TimeoutError('writer admission timeout; frame remains owned')
  seq=writer.try_submit(result)
  if seq is not None:return seq,retries
  remaining=deadline-time.monotonic()
  if remaining<=0:raise TimeoutError('writer admission timeout; frame remains owned')
  retries+=1;stop.wait(min(.01,remaining))

def windows(events,start,end):
 # Non-overlapping complete five-second activity windows; also report all data.
 # NumPy bounds yield np.bool_ comparisons and sum becomes np.int32 on ARM.
 # Keep report scalars native and count each half-open window once.
 start=float(start);end=float(end);events=tuple(float(t) for t in events)
 result=[];index=0
 while True:
  s=start+index*5
  if s+5>end:break
  count=sum(1 for t in events if s<=t<s+5)
  result.append(dict(start_s=s-start,end_s=s+5-start,count=count,hz=count/5))
  index+=1
 return result

def capacity_budgets(limit):
 # Even a one-frame run must reserve one worst legal frame before DMA. The
 # reservation shrinks to its immutable actual charge after geometry/seal.
 if type(limit) is not int or not 1<=limit<=240:raise ValueError('bounded 1..240 replay required')
 perframe=360960+384+180480*68+480*68+7680*68+129*68+11616256+2*7680*24+131072
 # Two worst-case reservations are needed to overlap DMA and source sealing.
 # The same conservative run quota still bounds all durable plus pending data.
 pending=64*1024**2;quota=max(perframe*min(limit,2),limit*4*1024**2)
 return perframe,quota,pending,quota+int(pending*1.25)+2**31

class AdmissionLedger:
 """Reserve worst-case ownership before DMA; shrink only after immutable seal.

 Run bytes count durable frames too. Pending bytes cover producer, current,
 queued and writer-owned frames together, rather than four independent pools.
 Frame slots are reserved before DMA too, matching the configured writer frame
 bound. A small actual payload must not bypass that independent count limit.
 """
 def __init__(self,quota_bytes,pending_bytes=64*1024**2,max_inflight_frames=32,preparation_frames=PREP_FRAMES):
  if type(max_inflight_frames) is not int or not 1<=max_inflight_frames<=128:
   raise ValueError('inflight frame capacity must be 1..128')
  if type(preparation_frames) is not int or not 1<=preparation_frames<=PREP_FRAMES:
   raise ValueError('preparation frame capacity must be 1..4')
  self.preparation_limit=preparation_frames;self.preparation_capacity=preparation_frames*PREP_FRAME_BYTES
  self.quota=int(quota_bytes);self.capacity=int(pending_bytes)
  self.max_inflight_frames=max_inflight_frames;self.peak_inflight_frames=0
  self.entries={};self.prepared={};self.charged=0;self.pending=0;self.peak=0
  self.prep_pending=0;self.prep_peak=0;self.total_peak=0
  self.lock=threading.RLock();self.changed=threading.Condition(self.lock)
 def prepare(self,frame_id,stop,timeout_s=30.):
  """Charge compressed transient AND bounded output before allocating either.

  Prepared inputs are not accepted measurements. Their capacity shares the
  same 64MiB ledger with DMA reservations/current/queued/writer-owned frames.
  """
  deadline=time.monotonic()+timeout_s
  with self.changed:
   while True:
    if stop.is_set():raise RuntimeError('input preparation cancelled before DMA')
    if frame_id in self.prepared or frame_id in self.entries:raise ValueError('duplicate preparation identity')
    if (len(self.prepared)<self.preparation_limit and self.prep_pending+PREP_FRAME_BYTES<=self.preparation_capacity
        and self.pending+self.prep_pending+PREP_FRAME_BYTES<=self.capacity):
     self.prepared[frame_id]={'charge':PREP_FRAME_BYTES,'state':'preparing','frame_id':frame_id}
     self.prep_pending+=PREP_FRAME_BYTES;self.prep_peak=max(self.prep_peak,self.prep_pending)
     self.total_peak=max(self.total_peak,self.pending+self.prep_pending);return
    remaining=deadline-time.monotonic()
    if remaining<=0:raise TimeoutError('input preparation ownership timeout before DMA')
    self.changed.wait(min(.05,remaining))
 def prepared_ready(self,frame_id,raw):
  if type(raw) is not bytes or len(raw)!=RAW_BYTES:raise ValueError('immutable prepared Raw required')
  with self.changed:
   item=self.prepared[frame_id]
   charge=len(raw)+1024 # bounded packet/identity metadata, apart from Raw
   self.prep_pending-=item['charge']-charge
   item.update(charge=charge,state='prepared_not_received')
   self.changed.notify_all()
 def release_prepared(self,frame_id):
  with self.changed:
   item=self.prepared.pop(frame_id,None)
   if item is not None:self.prep_pending-=item['charge']
   self.changed.notify_all()
 def refresh(self,commits):
  with self.lock:
   for commit in commits:
    item=self.entries.pop(int(commit['frame_id']),None)
    if item is not None:self.pending-=item['charge']
   self.changed.notify_all()
 def reserve(self,frame_id,worst,stop,commits=lambda:(),timeout_s=30.,transfer_prepared=False):
  worst=int(worst);deadline=time.monotonic()+timeout_s
  if worst>self.capacity:raise BufferError('worst frame exceeds pending ownership capacity')
  while True:
   self.refresh(commits())
   with self.lock:
    if stop.is_set():raise RuntimeError('admission cancelled before DMA')
    if frame_id in self.entries:raise ValueError('duplicate admission identity')
    preparation=self.prepared.get(frame_id) if transfer_prepared else None
    if transfer_prepared and (preparation is None or preparation['state']!='prepared_not_received'):
     raise ValueError('unprepared DMA admission identity')
    prep_release=preparation['charge'] if preparation is not None else 0
    quota_ok=self.charged+worst<=self.quota
    # Reservations for an earlier in-flight frame may shrink when sealed.
    # Wait for that, but never execute DMA against speculative capacity.
    if not quota_ok and not any('result' not in x for x in self.entries.values()):
     raise BufferError('run quota cannot reserve worst frame before DMA')
    if (quota_ok and len(self.entries)<self.max_inflight_frames
        and self.pending+self.prep_pending+worst-prep_release<=self.capacity):
     # Atomic ownership hand-off: no interval has an uncharged Raw object.
     if preparation is not None:
      del self.prepared[frame_id];self.prep_pending-=prep_release
     self.entries[frame_id]={'charge':worst,'state':'reserved','frame_id':frame_id}
     self.charged+=worst;self.pending+=worst;self.peak=max(self.peak,self.pending)
     self.peak_inflight_frames=max(self.peak_inflight_frames,len(self.entries))
     self.total_peak=max(self.total_peak,self.pending+self.prep_pending)
     return
    if stop.is_set():raise RuntimeError('admission cancelled before DMA')
    remaining=deadline-time.monotonic()
    if remaining<=0:raise TimeoutError('pending ownership reservation timeout before DMA')
    self.changed.wait(min(.05,remaining))
 def update(self,frame_id,**values):
  with self.lock:self.entries[frame_id].update(values)
 def accepted(self,frame_id,sequence):
  with self.lock:
   # The writer may have fsynced before submit() returns to this thread.
   if frame_id in self.entries:
    self.entries[frame_id].update(state='writer_accepted',sequence=sequence)
 def seal(self,frame_id,result):
  with self.lock:
   entry=self.entries[frame_id];actual=int(result.charge_bytes)
   if actual>entry['charge']:
    raise BufferError('sealed frame exceeds its pre-DMA reservation; evidence retained')
   released=entry['charge']-actual
   self.charged-=released;self.pending-=released
   entry.update(charge=actual,result=result,state='sealed')
   entry.pop('geometry_result',None)
   self.changed.notify_all()
 def snapshot(self):
  with self.lock:return {i:dict(v) for i,v in self.entries.items()}
 def prepared_ids(self):
  with self.lock:return sorted(self.prepared)
 def status(self):
  with self.lock:return dict(run_charged_bytes=self.charged,pending_bytes=self.pending,
   peak_pending_bytes=self.peak,pending_frames=len(self.entries),quota_bytes=self.quota,
   max_inflight_frames=self.max_inflight_frames,peak_inflight_frames=self.peak_inflight_frames,
   pending_capacity_bytes=self.capacity,prepared_bytes=self.prep_pending,
   prepared_frames=len(self.prepared),peak_prepared_bytes=self.prep_peak,
   peak_total_owned_bytes=self.total_peak,total_owned_bytes=self.pending+self.prep_pending,
   preparation_capacity_bytes=self.preparation_capacity,preparation_frame_limit=self.preparation_limit)


def read_prepared_input(stream,block,metadata,frame_id,stop):
 """Bounded strict zlib decode. Return immutable bytes; never touch CMA/DMA.

 A complete single zlib stream with exactly one Raw frame is required. This
 rejects truncated reads, trailing streams and decompression bombs before DMA.
 """
 if stop.is_set():raise RuntimeError('input preparation cancelled before DMA')
 offset=block.get('offset');length=block.get('bytes')
 if type(offset) is not int or offset<0 or type(length) is not int or not 1<=length<=MAX_COMPRESSED_BYTES:
  raise ValueError('frozen compressed block bounds invalid')
 source_id=metadata.get('frame_id',frame_id)
 if type(source_id) is not int or source_id!=frame_id:raise ValueError('frozen frame identity/order mismatch')
 begin=time.perf_counter();stream.seek(offset);compressed=stream.read(length)
 if len(compressed)!=length:raise ValueError('frozen compressed block truncated')
 if stop.is_set():raise RuntimeError('input preparation cancelled before DMA')
 decoder=zlib.decompressobj();raw=decoder.decompress(compressed,RAW_BYTES+1)
 if len(raw)!=RAW_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
  raise ValueError('frozen compressed stream/Raw length mismatch')
 if hashlib.sha256(raw).hexdigest()!=metadata['raw_sha256']:raise ValueError('frozen Raw SHA/length mismatch')
 if stop.is_set():raise RuntimeError('input preparation cancelled before DMA')
 return dict(frame_id=frame_id,raw=raw,raw_zlib_stream=bytes(compressed),input_begin=begin,
  source_ready_ns=time.perf_counter_ns(),source_monotonic_ns=time.monotonic_ns())


def put_owned(q,value,stop):
 """Cancellation never discards ownership; caller's ledger retains the value."""
 while not stop.is_set():
  try:q.put(value,timeout=.05);return True
  except queue.Full:pass
 return False


def join_workers_until(threads,timeout_s):
 """One absolute join budget, not a fresh timeout for each worker."""
 deadline=time.monotonic()+timeout_s
 for thread in threads:
  if thread is None:continue
  # start() may fail before assigning an OS thread. Do not replace that
  # original failure with Python's 'cannot join thread before it is started'.
  # SourceSealer wraps its actual Thread; a started/finished Thread retains
  # ident, so it still follows the normal absolute-deadline join path.
  worker=getattr(thread,'thread',thread)
  if worker.ident is None and not worker.is_alive():continue
  thread.join(max(0.,deadline-time.monotonic()))
 return [thread.name for thread in threads if thread is not None and thread.is_alive()]


class SourceSealer:
 """One ordered, bounded CPU source-validation stage; never accesses DMA.

 Both queues reference bytes already owned by the shared admission ledger.
 Entries keep their worst-case reservation until geometry's immutable seal.
 A queue slot is not an independent memory allowance.
 """
 def __init__(self,source_factory,ledger,stop,producer_done,output,errors):
  self.source_factory=source_factory;self.ledger=ledger;self.stop=stop
  self.producer_done=producer_done;self.output=output;self.errors=errors
  self.incoming=queue.Queue(4);self.done=threading.Event();self.failures=[]
  self.thread=threading.Thread(target=self.run,name='R34_source_snapshot_owner',daemon=True)
 @property
 def name(self):return self.thread.name
 def start(self):self.thread.start()
 def join(self,timeout=None):self.thread.join(timeout)
 def is_alive(self):return self.thread.is_alive()
 def seal_packet(self,packet,queued_at):
  i,raw,frame,primary,receipt,ready,ready_perf,input_begin,transport_end,transport_begin=packet
  begin=time.perf_counter();self.ledger.update(i,state='source_sealing')
  source=self.source_factory(raw=raw,primary=primary,frame=frame,receipt=receipt)
  end=time.perf_counter();self.ledger.update(i,state='source_ready')
  timing=dict(source_queue_wait_s=max(0.,begin-queued_at),source_ready_perf_s=end)
  return packet+(source,end-begin,timing)
 def submit(self,packet):
  self.ledger.update(packet[0],state='source_queued')
  return put_owned(self.incoming,(packet,time.perf_counter()),self.stop)
 def run(self):
  current=None
  try:
   while not self.stop.is_set():
    try:packet,queued_at=self.incoming.get(timeout=.05)
    except queue.Empty:
     if self.producer_done.is_set():break
     continue
    current=packet[0]
    sealed=self.seal_packet(packet,queued_at)
    if not put_owned(self.output,sealed,self.stop):break
    current=None;packet=sealed=None
  except BaseException as error:
   text=type(error).__name__+': '+str(error)
   self.failures.append(dict(frame_id=current,error=text,stage='source_sealing'))
   if current is not None:self.ledger.update(current,failure=text)
   self.errors.append(text);self.stop.set()
  finally:self.done.set()


def preserve_pending(output,entries):
 """Persist immutable snapshots of every not-yet-ACKed input/result.

 Failure of this evidence write is itself reported; it never turns a partial
 save into success. This includes PL-executed frames not submitted to writer.
 """
 folder=Path(output)/'failure_evidence';folder.mkdir(exist_ok=False)
 manifest=[]
 for frame_id,entry in sorted(entries.items()):
  sub=folder/('frame_%04d'%frame_id);sub.mkdir()
  result=entry.get('result')
  components=dict(result.components) if result is not None else {
   name:entry[name] for name in ('raw','primary','observations_r33') if name in entry}
  if result is not None and 'raw' in entry:components['raw']=entry['raw']
  if result is None and entry.get('geometry_result') is not None:
   geometry=entry['geometry_result']
   for name,array in (('measurement_xyz',geometry.measurement_axis_mm),('preview_xyz',geometry.preview_axis_mm)):
    value=np.asarray(array,dtype='<f8')
    if value.ndim==2 and value.shape[1]==3 and np.isfinite(value).all():
     components[name]=value.tobytes()
  descriptors={}
  for name,data in components.items():
   data=bytes(data)
   with (sub/(name+'.bin')).open('xb') as f:
    f.write(data);f.flush();os.fsync(f.fileno())
   descriptors[name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
  record=dict(frame_id=frame_id,state=entry['state'],components=descriptors,
   PL_started=bool(entry.get('PL_started')),PL_returned=bool(entry.get('PL_returned')),
   failure=entry.get('failure'),identity=entry.get('identity'),
   receipt=entry.get('receipt'),source_ready_ns=entry.get('source_ready_ns'))
  save(sub/'STATE.json',record);manifest.append(record)
 save(folder/'MANIFEST.json',dict(schema='R33_FAILED_OWNERSHIP_V1',frames=manifest,
  complete_for_snapshot=True,not_a_reconstructed_original_run=True))
 return manifest


def verify_readback(readback,expected,measurement_sha,preview_sha):
 if not readback.get('passed') or readback.get('frames')!=len(expected):
  raise ValueError('strict readback frame count mismatch')
 seen=set()
 for row in readback['frame_summaries']:
  i=int(row['frame_id'])
  if i in seen or i not in expected:raise ValueError('readback unexpected/duplicate identity')
  seen.add(i)
  if row.get('component_sha256')!=expected[i]:raise ValueError('readback component SHA mismatch')
 if seen!=set(expected):raise ValueError('readback missing accepted frame')
 if (readback.get('measurement_xyz_sha256')!=measurement_sha or
     readback.get('preview_xyz_sha256')!=preview_sha):
  raise ValueError('readback XYZ content differs from live geometry')
 return True


def save_array(path,array):
 with Path(path).open('xb') as f:
  np.save(f,array,allow_pickle=False);f.flush();os.fsync(f.fileno())


def new_model_acks(records,valid_ack):
 events=[];seen=set();points=0
 for row in records:
  if not valid_ack(row.get('receipt')) or not row.get('ack_perf_s'):continue
  identity=(row.get('target'),row.get('frame_id'));count=int(row.get('cloud_points',0))
  if identity not in seen and count>points:
   events.append(float(row['ack_perf_s']));points=count
  seen.add(identity)
 return events


def run(args):
 # Independent preparation is experimental: board v12 showed worse throughput
 # and source-to-display age. Default restores the sole DMA producer read path.
 prefetch_depth=getattr(args,'input_prefetch_depth',0)
 if type(prefetch_depth) is not int or not 0<=prefetch_depth<=PREP_FRAMES:
  raise ValueError('input prefetch depth must be 0..4')
 source_worker=getattr(args,'source_worker',True)
 if type(source_worker) is not bool:raise ValueError('source worker must be a boolean')
 preparation_frames=max(1,prefetch_depth)
 raw_encoding=getattr(args,'raw_encoding','zlib')
 parallel_display=getattr(args,'parallel_display_startup',True)
 overlap_close=getattr(args,'overlap_display_close',True)
 if raw_encoding not in ('zlib','identity') or type(parallel_display) is not bool or type(overlap_close) is not bool:
  raise ValueError('R38 storage/startup options')
 hold_s=getattr(args,'display_hold_s',0.)
 if type(hold_s) not in (int,float) or not 0<=hold_s<=DISPLAY_HOLD_MAX_S:
  raise ValueError('display hold must be 0..%d s'%DISPLAY_HOLD_MAX_S)
 wall_start=time.perf_counter();stages={}
 from pynq import Overlay
 from structured_light.motion.lifecycle import HardwareProcessLock
 from tools.pl_replay_r25 import NormalImageGuard,replay_identity
 from r33.transport import ActualPLTransportR25,resolve_manifest,TRANSPORT_STAGES
 from r33.geometry import load_calibration,reconstruct_pair
 from r33.metrics import sliding_windows,display_metrics
 from r33.storage import FrameResult,SourceSnapshot,AsyncWriter,readback,SOURCE_CHECKSUM_CHUNK_BYTES
 from candidate.display_process_r25 import ProcessSessionDisplayR25,ProcessDisplayCloseError,MIN_ARCHIVE_SLOT_BYTES,display_timing_summary
 from r33.display import renderer_factory,deferred_annotations_from_frame
 from candidate.display_publisher_r30 import DisplayPublisherR30
 from tools.board_replay_r22 import ReplayOwner,valid_vdma_ack,quarantine_owner
 from candidate.runtime_monitor_r22 import linux_sample
 import cv2
 cv2.setNumThreads(1)
 import r33.storage as storage_module
 storage_module.RAW_ENCODING=raw_encoding
 stages['imports_s']=time.perf_counter()-wall_start
 if not 1<=args.limit<=240:raise ValueError('bounded 1..240 replay required')
 if args.output.exists():raise FileExistsError('preserve existing output')
 perframe,quota,pending_capacity,budget=capacity_budgets(args.limit)
 if shutil.disk_usage(args.output.parent).free<budget:
  raise BufferError('R33 quota plus failure reserve and 2GiB need '+str(budget)+' bytes')
 manifest=resolve_manifest(args.manifest)
 args.output.mkdir()
 boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
 source=args.input_root/args.target
 frames=[json.loads(l) for l in (source/'frames.jsonl').read_text().splitlines()]
 blocks=json.loads((source/'RAW_BLOCK_INDEX.json').read_text())
 if len(frames)<args.limit or len(blocks)<args.limit:raise ValueError('insufficient frozen inputs')
 calibration=load_calibration(source/'calibration.json')
 lock=HardwareProcessLock(label='R33_PL_UV_REPLAY_NO_CAMERA_MOTION').acquire()
 transport=display=publisher=writer=None;producer=preparer=sealer=None;clean=True
 pl_view=None;pl_view_params=None;pl_view_summary=None;display_hold=None
 pl_view_enabled=bool(getattr(args,'pl_view',False))
 stop=threading.Event();producer_done=threading.Event();preparation_done=threading.Event()
 prepared=queue.Queue(max(1,prefetch_depth));preparation_errors=[]
 samples=[];traces=[];produced=queue.Queue(4);errors=[];cloud=[];previewcloud=[]
 monitor_stop=threading.Event();monitor=None;monitor_errors=[]
 ledger=AdmissionLedger(quota,pending_capacity,preparation_frames=preparation_frames,max_inflight_frames=128);expected={}
 zlib_streams={}
 def make_source(*,raw,primary,frame,receipt):
  # Keyed by the Raw object itself (held in the entry, so its id cannot be reused).
  entry=zlib_streams.pop(id(raw),None)
  if entry is not None and entry[0] is raw:
   return SourceSnapshot.create(raw=raw,primary=primary,frame=frame,receipt=receipt,raw_zlib_stream=entry[1])
  return SourceSnapshot.create(raw=raw,primary=primary,frame=frame,receipt=receipt)
 source_stage=SourceSealer(make_source,ledger,stop,producer_done,produced,errors)
 source_done=source_stage.done if source_worker else producer_done
 m_hash=hashlib.sha256();p_hash=hashlib.sha256()
 commit_lock=threading.Lock();commit_cursor=0
 def consume_commits():
  nonlocal commit_cursor
  with commit_lock:
   delta=writer.commits_since(commit_cursor);commit_cursor=delta['cursor']
   return delta['commits']

 def refresh_writer_admission():
  if errors:raise RuntimeError(errors[0])
  ledger.refresh(consume_commits())

 def monitor_resources():
  while not monitor_stop.is_set():
   try:samples.append(dict(perf_s=time.perf_counter(),stage='periodic_250ms',**linux_sample()))
   except Exception as error:monitor_errors.append(type(error).__name__+': '+str(error))
   monitor_stop.wait(.25)

 def prepare_inputs():
  current=None
  try:
   with (source/'raw.gray8.zblocks').open('rb') as stream:
    for i in range(args.limit):
     if stop.is_set():break
     ledger.prepare(i,stop);current=i
     packet=read_prepared_input(stream,blocks[i],frames[i],i,stop)
     ledger.prepared_ready(i,packet['raw'])
     if not put_owned(prepared,packet,stop):break
     current=None;packet=None
  except BaseException as error:
   preparation_errors.append(dict(frame_id=current,error=type(error).__name__+': '+str(error),
    accepted_measurement=False,PL_started=False))
   errors.append(preparation_errors[-1]['error']);stop.set()
  finally:
   if current is not None:ledger.release_prepared(current)
   preparation_done.set()

 def produce():
  current=None
  try:
   with (source/'raw.gray8.zblocks').open('rb') as stream:
    for i in range(args.limit):
     if not prefetch_depth:
      if stop.is_set():return
      ledger.prepare(i,stop)
      try:
       input_packet=read_prepared_input(stream,blocks[i],frames[i],i,stop)
       ledger.prepared_ready(i,input_packet['raw'])
      except BaseException as error:
       preparation_errors.append(dict(frame_id=i,error=type(error).__name__+': '+str(error),
        accepted_measurement=False,PL_started=False))
       ledger.release_prepared(i);raise
     else:
      while True:
       if stop.is_set():return
       try:input_packet=prepared.get(timeout=.05);break
       except queue.Empty:
        if preparation_done.is_set():raise RuntimeError('input preparation ended before all inputs ready')
     if input_packet['frame_id']!=i:raise ValueError('prepared frame identity/order mismatch')
     if raw_encoding=='zlib' and input_packet.get('raw_zlib_stream'):
      zlib_streams[id(input_packet['raw'])]=(input_packet['raw'],input_packet['raw_zlib_stream'])
     # Atomically subtract prepared ownership and reserve full PL/result space.
     ledger.reserve(i,perframe,stop,consume_commits,transfer_prepared=True)
     current=i;raw=input_packet['raw']
     ledger.update(i,raw=raw,state='input_checked')
     if shutil.disk_usage(args.output).free<2**31+int(pending_capacity*1.25):
      raise BufferError('2GiB reserve plus pending failure capacity required before DMA')
     sample=linux_sample()
     if sample['MemAvailable_bytes']<128*1024**2:raise MemoryError('128MiB reserve breached before DMA')
     gray=np.frombuffer(raw,np.uint8).reshape(480,752)
     ready=input_packet['source_monotonic_ns'];ready_perf=input_packet['source_ready_ns']
     identity=replay_identity(i,args.epoch);identity['cfg_version']=33
     ledger.update(i,identity=identity,source_ready_ns=ready_perf,PL_started=True,state='PL_in_flight')
     transport_begin=time.perf_counter()
     transport.configure_geometry(calibration,float(frames[i]['angle_rad']),i)
     frame,primary,receipt,_=transport.run(gray,identity=identity)
     ledger.update(i,primary=bytes(primary),observations_r33=bytes(frame.payload),receipt=receipt,
                   PL_returned=True,state='PL_complete')
     transport_end=time.perf_counter()
     # These are independently owned archival bytes, never reused DMA views.
     packet=(i,raw,frame,primary,dict(receipt),ready,ready_perf,input_packet['input_begin'],
      transport_end,transport_begin)
     if source_worker:
      if not source_stage.submit(packet):break
     else:
      packet=source_stage.seal_packet(packet,time.perf_counter())
      if not put_owned(produced,packet,stop):break
     current=None;input_packet=raw=frame=primary=receipt=packet=None
  except BaseException as e:
   error=type(e).__name__+': '+str(e);errors.append(error)
   stop.set()
   if current is not None:
    extra={'failure':error}
    failed=getattr(transport,'failed_primary',None)
    if failed is not None:extra['primary']=bytes(failed)
    ledger.update(current,**extra)
  finally:
   if not prefetch_depth:preparation_done.set()
   producer_done.set()
 try:
  config=dict(frame_total=args.limit,manifest=str(args.manifest),owner_log=str(args.output/'HDMI_OWNER.log'),
   hardware_lock_fd=lock._handle.fileno(),warmup=True,display_source='ACTUAL PL R35 XYZ / SIMULATION ONLY',
   display_min_interval_s=.1,display_sample_cap=16000,display_native_finite_validation_only=True,
   display_startup_canvas_timeout_s=20.)
  if args.raster_manifest:config['raster_manifest']=str(args.raster_manifest)
  # R40: the display child inherits this; its renderer then leaves the model window to the PL.
  if pl_view_enabled:os.environ['SCAN3D_R40_PL_VIEW']='1'
  display_box={}
  def start_display():
   started=time.perf_counter()
   try:
    display_box['display']=ProcessSessionDisplayR25(owner_factory=ReplayOwner,renderer_factory=renderer_factory,
     config=config,cloud_bytes=8*1024**2,archive_slot_bytes=MIN_ARCHIVE_SLOT_BYTES,startup_timeout_s=65.)
   except BaseException as startup_error:
    # A constructor can already own a child, shared buffers and a duplicate
    # hardware-lock FD. Retain that partial instance for bounded cleanup.
    display_box['error']=startup_error
    if type(startup_error).__name__=='SessionDisplayStartupError':
     display_box['display']=getattr(startup_error,'display',None)
   finally:
    display_box['startup_s']=time.perf_counter()-started
  def collect_display():
   # The display child shares no IP with the PFO3 DMA path and transport init
   # performs no PL/clock reset, so its startup can overlap HWH parsing.
   nonlocal display
   if display_thread is None:start_display()
   else:
    display_thread.join(70.)
    if display_thread.is_alive():raise TimeoutError('R38 display startup thread did not finish')
   display=display_box.get('display')
   if 'error' in display_box:raise display_box['error']
  display_thread=None
  if parallel_display:
   display_thread=threading.Thread(target=start_display,name='R38_display_startup',daemon=True)
   display_thread.start()
  try:
   overlay=Overlay(manifest['bit_absolute'],download=False);stages['overlay_s']=time.perf_counter()-wall_start
   transport=ActualPLTransportR25(overlay,manifest=manifest,hardware_lock=lock)
   stages['transport_s']=time.perf_counter()-wall_start
   if pl_view_enabled:
    # R40 display-only PL view: own 0x400-0x4ff MMIO window, cleared before the first packet.
    from pynq import MMIO
    from r33.pl_view import PLCloudView,default_params
    pl_view=PLCloudView(MMIO,overlay.ip_dict['pfo3_frontend']['phys_addr'])
    pl_view_params=default_params();pl_view.configure(pl_view_params);pl_view.start(auto_rotate=True)
    stages['pl_view_s']=time.perf_counter()-wall_start
  except BaseException:
   if display_thread is not None:
    display_thread.join(70.);display=display_box.get('display')
   raise
  collect_display();stages['display_ready_s']=time.perf_counter()-wall_start
  stages['display_startup_own_s']=display_box.get('startup_s')
  display.begin_target(args.target+'/'+args.output.name);publisher=DisplayPublisherR30(display)
  writer_configuration=dict(capacity_frames=128,capacity_bytes=pending_capacity,group=8,max_delay_s=.180,
   preallocate_bytes=getattr(args,'journal_preallocate_bytes',0))
  writer=AsyncWriter(args.output/'storage',**writer_configuration);stages['writer_s']=time.perf_counter()-wall_start
  save(args.output/'CONFIG.json',dict(schema='R35_REPLAY_V1',boot_id=boot,limit=args.limit,target=args.target,
   manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),geometry='SIMULATION_ONLY',
   firmware=manifest['files'],capacity_budget_bytes=budget,run_quota_bytes=quota,
   pending_ownership_bytes=pending_capacity,max_inflight_frames=ledger.max_inflight_frames,input_queue_frames=prefetch_depth,
   input_preparation=dict(backend=('bounded_independent_thread' if prefetch_depth else 'sole_DMA_producer_serial'),
    prefetch_depth=prefetch_depth,maximum_frames=preparation_frames,
    maximum_bytes=preparation_frames*PREP_FRAME_BYTES,compressed_block_limit=MAX_COMPRESSED_BYTES,
    ownership='shared64MiB_atomic_transfer_before_DMA',
    source_ready='after Raw length/SHA; includes prepared queue and DMA admission wait in age'),
   source_sealing=dict(backend=('bounded_independent_thread' if source_worker else 'sole_DMA_producer_serial'),
    source_worker=source_worker,input_queue_frames=4 if source_worker else 0,output_queue_frames=4,
    source_checksum_chunk_bytes=SOURCE_CHECKSUM_CHUNK_BYTES,
    ownership='shared64MiB_preDMA_reservation_until_geometry_seal',
    worker_join_total_timeout_s=WORKER_JOIN_TIMEOUT_S),
   writer_configuration=dict(writer_configuration,compression=('raw_zlib_passthrough' if raw_encoding=='zlib' else 'none'),
    journal_format=getattr(storage_module,'FORMAT','unavailable'),raw_encoding=raw_encoding),
   r38_options=dict(parallel_display_startup=parallel_display,overlap_display_close=overlap_close),
   display_configuration=dict(owner_config={k:v for k,v in config.items() if k not in ('owner_log','hardware_lock_fd')},
    cloud_bytes=8*1024**2,archive_slot_bytes=MIN_ARCHIVE_SLOT_BYTES,startup_timeout_s=65.,
    omitted_config_keys=['owner_log','hardware_lock_fd']),
   annotation_encoding='compact binary CRC; same sampled centers and classifications',
   annotation_backend=__import__('r33.markers_native_r34',fromlist=['backend_status']).backend_status(),
   writer_capacity_frames=128,writer_capacity_bytes=64*1024**2,
   minimum_available_memory_bytes=128*1024**2,display_close_timeout_s=DISPLAY_CLOSE_TIMEOUT_S,
   FPGA_loads=0,camera_commands=0,motor_commands=0,protocol='R35_PL_FIXED_XYZ_Q16',
   algorithm_fingerprint=manifest['pfo3']['parameter_fingerprint']))
  before=linux_sample();activity_start=time.perf_counter()
  samples.append(dict(perf_s=activity_start,stage='activity_start',**before))
  monitor=threading.Thread(target=monitor_resources,name='R33_resource_samples',daemon=True);monitor.start()
  with NormalImageGuard() as guard:
   if source_worker:sealer=source_stage;sealer.start()
   if prefetch_depth:
    preparer=threading.Thread(target=prepare_inputs,name='R33_bounded_input_preparation',daemon=True);preparer.start()
   producer=threading.Thread(target=produce,name='R33_unique_DMA_owner',daemon=True);producer.start()
   last_progress=time.monotonic()
   while True:
    ledger.refresh(consume_commits())
    try:item=produced.get(timeout=.1)
    except queue.Empty:
     if errors:raise RuntimeError(errors[0])
     if source_done.is_set():break
     if time.monotonic()-last_progress>35.:raise TimeoutError('producer progress timeout')
     continue
    last_progress=time.monotonic()
    i,raw,frame,primary,receipt,ready,ready_perf,input_begin,transport_end,transport_begin,source_snapshot,source_seal_s,source_timing=item
    ledger.update(i,state='geometry_current')
    sample=linux_sample();samples.append(dict(perf_s=time.perf_counter(),**sample))
    if sample['MemAvailable_bytes']<128*1024**2:raise MemoryError('128MiB reserve breached')
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=boot:raise RuntimeError('boot identity changed')
    begin=time.perf_counter();geo=reconstruct_pair(frame,calibration,angle_rad=float(frames[i]['angle_rad']))
    xyz_ready=time.perf_counter_ns();xyz_end=time.perf_counter()
    m=geo.measurement_axis_mm;p=geo.preview_axis_mm
    ledger.update(i,geometry_result=geo)
    metadata=dict(frame_id=i,target=args.target,angle_rad=float(frames[i]['angle_rad']),source_ready_ns=ready_perf,
     xyz_ready_ns=xyz_ready,calibration_id=calibration.calibration_id,geometry_evidence_type='SIMULATION_ONLY',
     geometry_rejection_counts=geo.rejection_counts)
    result=FrameResult.from_source(source_snapshot,measurement_xyz=m,preview_xyz=p,metadata=metadata)
    ledger.seal(i,result)
    sealed_perf=time.perf_counter()
    annotations=deferred_annotations_from_frame(frame.base,actual_pl_execution=True,
     geometry_evidence_type=calibration.evidence_type,max_points=384)
    annotated_perf=time.perf_counter()
    # No file write precedes this submit; its immutable frame is already owned.
    publisher.submit(raw,m,i,float(frames[i]['angle_rad']),
     annotations=annotations,
     source_capture_monotonic_ns=ready)
    published_perf=time.perf_counter()
    sequence,submit_retries=submit_owned_result(writer,result,stop,
     refresh_writer_admission,timeout_s=30.)
    submitted_perf=time.perf_counter()
    ledger.accepted(i,sequence)
    expected[i]={name:sha for name,size,sha,crc in result.descriptors}
    m_hash.update(np.asarray(m,dtype='<f8').tobytes());p_hash.update(np.asarray(p,dtype='<f8').tobytes())
    cloud.append(m);previewcloud.append(p)
    traces.append(dict(frame_id=i,source_ready_ns=ready_perf,xyz_perf_s=xyz_end,transport_end=transport_end,
     input_begin=input_begin,input_preparation_s=ready_perf/1e9-input_begin,
     prepared_wait_s=transport_begin-ready_perf/1e9,transport_begin=transport_begin,
     transport_s=transport_end-transport_begin,
     geometry_s=xyz_end-begin,points=len(m),preview_points=len(p),
     source_seal_s=source_seal_s,result_seal_s=sealed_perf-xyz_end,annotations_s=annotated_perf-sealed_perf,
     source_queue_wait_s=source_timing['source_queue_wait_s'],
     source_output_wait_s=max(0.,begin-source_timing['source_ready_perf_s']),
     display_publish_s=published_perf-annotated_perf,writer_submit_s=submitted_perf-published_perf,
     writer_submit_capacity_retries=submit_retries,
     processing_cycles=int(frame.header['processing_cycles_lo'])+(int(frame.header['processing_cycles_hi'])<<32),
     writer_status={k:v for k,v in writer.counters().items() if isinstance(v,(int,float,str,bool,type(None)))},
     ownership=ledger.status()))
   if errors:raise RuntimeError(errors[0])
   alive_workers=join_workers_until((producer,sealer,preparer),WORKER_JOIN_TIMEOUT_S)
   if alive_workers:raise RuntimeError('worker termination unconfirmed: '+', '.join(alive_workers))
   if ledger.status()['prepared_frames']:raise RuntimeError('prepared ownership remains after bounded input')
   if len(traces)!=args.limit:raise RuntimeError('producer ended before all bounded inputs accepted')
   activity_end=time.perf_counter();after=linux_sample()
   writer.close(timeout_s=30.);ledger.refresh(consume_commits())
   if ledger.snapshot():raise RuntimeError('pending ownership remains after writer completion')
   durable_end=time.perf_counter();publisher.drain(20.);publisher.close(20.)
   time.sleep(.25)
   if pl_view is not None:
    # While HDMI still streams: settle two renders after the last commit, freeze,
    # read the grid and the displayed panel back, resume rotation.
    pv_state,pv_voxels,pv_panel,pv_readback_s=pl_view.snapshot()
    save_array(args.output/'PL_VIEW_VOXELS.npy',pv_voxels);save_array(args.output/'PL_VIEW_PANEL.npy',pv_panel)
    pl_view_summary=dict(schema='R40_PL_VIEW_V1',params=pl_view_params,status=pv_state,readback_s=pv_readback_s,
     panel_nonzero=int(np.count_nonzero(pv_panel)),
     source='PL geometry measurement records of packets that ended without error')
    save(args.output/'PL_VIEW.json',pl_view_summary)
   if hold_s>0:
    # Viewing only, after every result is durable: HDMI keeps scanning the last
    # store and the PL view keeps rotating. Counters prove the video kept flowing.
    hold_begin=time.perf_counter();pv0=pl_view.status() if pl_view is not None else None
    time.sleep(hold_s)
    pv1=pl_view.status() if pl_view is not None else None
    display_hold=dict(requested_s=float(hold_s),actual_s=time.perf_counter()-hold_begin,
     pl_view_renders=None if pv0 is None else (pv1['render_count']-pv0['render_count'])&0xffffffff,
     pl_view_swaps=None if pv0 is None else (pv1['swap_count']-pv0['swap_count'])&0xffffffff,
     note='display hold for viewing; wall clock and close timing include it')
    save(args.output/'DISPLAY_HOLD.json',display_hold)
   close_box={}
   def close_display():
    try:close_box['receipt']=display.close(timeout_s=DISPLAY_CLOSE_TIMEOUT_S)
    except BaseException as close_error:close_box['error']=close_error
    finally:close_box['end']=time.perf_counter()
   if overlap_close:
    # Readback touches only the journal; the HDMI owner shutdown is a separate
    # process. Join before any result is reported or the lock is released.
    closer=threading.Thread(target=close_display,name='R38_display_close',daemon=True);closer.start()
   else:
    closer=None;close_display()
    if 'error' in close_box:raise close_box['error']
   transport.close();transport=None
   read_start=time.perf_counter();disk_result=readback(args.output/'storage',strict_protocol=True)
   verify_readback(disk_result,expected,m_hash.hexdigest(),p_hash.hexdigest())
   read_end=time.perf_counter()
   if closer is not None:
    closer.join(DISPLAY_CLOSE_TIMEOUT_S+5.)
    if closer.is_alive():raise RuntimeError('R38 display close thread unconfirmed')
    if 'error' in close_box:raise close_box['error']
   display_close=close_box['receipt'];display_end=close_box['end']
   c=np.concatenate(cloud) if cloud else np.empty((0,3));pc=np.concatenate(previewcloud) if previewcloud else np.empty((0,3))
   save_array(args.output/'cloud.npy',c);save_array(args.output/'preview.npy',pc)
   final_sample=linux_sample();samples.append(dict(perf_s=time.perf_counter(),stage='readback_export',**final_sample))
   if final_sample['MemAvailable_bytes']<128*1024**2:raise MemoryError('128MiB reserve breached during readback/export')
   monitor_stop.set();monitor.join(2.)
   if monitor.is_alive() or monitor_errors:raise RuntimeError('resource sampling incomplete: '+str(monitor_errors))
   if min(x['MemAvailable_bytes'] for x in samples)<128*1024**2:raise MemoryError('sampled 128MiB reserve breached during run')
   save(args.output/'TRACE.json',traces);save(args.output/'DISPLAY.json',list(display.records))
   save(args.output/'RESOURCES.json',samples);save(args.output/'COMMITS.json',list(writer.commits))
   ticks=after['cpu_total_ticks']-before['cpu_total_ticks']
   xyz=[x['xyz_perf_s'] for x in traces if x['points']>0]
   strict_display=display_metrics(display.records,valid_vdma_ack)
   ack=strict_display['ack_events_perf_s']
   persisted=[x['persisted_ns']/1e9 for x in writer.commits]
   metrics_start=time.perf_counter()
   window_metrics=dict(
    XYZ_windows=windows(xyz,activity_start,activity_end),
    persisted_windows=windows(persisted,activity_start,activity_end),
    persisted_through_drain_windows=windows(persisted,activity_start,durable_end),
    HDMI_windows=windows(ack,activity_start,activity_end),
    HDMI_through_close_windows=windows(ack,activity_start,display_end),
    XYZ_sliding_windows=sliding_windows(xyz,activity_start,activity_end),
    persisted_sliding_windows=sliding_windows(persisted,activity_start,activity_end),
    HDMI_sliding_windows=sliding_windows(ack,activity_start,activity_end))
   legacy_display_timing=display_timing_summary(display.records)
   writer_status=writer.status();ownership_status=ledger.status();guard_status=guard.report()
   wall_end=time.perf_counter();duration=activity_end-activity_start
   result=dict(schema='R35_BOARD_RESULT_V1',geometry_backend='ACTUAL_PL_FIXED_Q30_Q16',ARM_geometry_calls=0,N=2,actual_PL_execution=True,geometry_evidence_type='SIMULATION_ONLY',
    frames=len(traces),measurement_points=len(c),preview_points=len(pc),data_correct=True,
    readback_content_matches_live=True,activity_s=duration,activity_hz=len(traces)/duration,
    nonempty_XYZ_hz=len(xyz)/duration,wall_clock_s=wall_end-wall_start,
    wall_clock_hz=len(traces)/(wall_end-wall_start),
    timing=dict(cold_start_s=activity_start-wall_start,processing_s=duration,
     persistence_drain_s=durable_end-activity_end,display_drain_close_s=display_end-durable_end,
     readback_s=read_end-read_start,export_s=wall_end-read_end,
     cold_start_stages_s=dict(stages),overlapped_display_close=overlap_close,
     scan_complete_s=max(display_end,read_end)-wall_start,
     metrics_s=wall_end-metrics_start,
     result_json_sync_included=False),
    CPU_total_capacity_pct=(100*(1-(after['cpu_idle_ticks']-before['cpu_idle_ticks'])/ticks) if ticks else None),
    memory_min_bytes=min(x['MemAvailable_bytes'] for x in samples),
    memory_evidence='sampled every250ms plus frame boundaries; not instantaneous hardware peak',
    CPU_evidence='activity interval whole-system two-core /proc/stat capacity',
    window_semantics='legacy fixed bins plus exact all complete half-open sliding windows',
    **window_metrics,
    new_model_ack_count=len(ack),display_timing=strict_display,
    display_close={key:value for key,value in display_close.items() if key!='records'},
    legacy_display_timing_all_ack_population=legacy_display_timing,
    writer=writer_status,ownership=ownership_status,readback=disk_result,
    annotation_backend=__import__('r33.markers_native_r34',fromlist=['backend_status']).backend_status(),
    stage_profile=TRANSPORT_STAGES,pl_view=pl_view_summary,display_hold=display_hold,**guard_status)
   save(args.output/'RESULT.json',result);print(json.dumps(result));return result
 except BaseException as exc:
  stop.set()
  alive_workers=join_workers_until((producer,sealer,preparer),WORKER_JOIN_TIMEOUT_S)
  # Never close DMA or release its lock while its sole producer is in flight.
  if alive_workers:clean=False
  preservation_error=None;preserved=[]
  try:
   if writer:ledger.refresh(consume_commits())
   preserved=preserve_pending(args.output,ledger.snapshot())
  except BaseException as error:
   preservation_error=type(error).__name__+': '+str(error);clean=False
  display_failure_receipt=None
  if type(exc).__name__=='ProcessDisplayCloseError' and isinstance(getattr(exc,'receipt',None),dict):
   display_failure_receipt={key:value for key,value in exc.receipt.items() if key!='records'}
   try:save(args.output/'DISPLAY_FAILED.json',list(getattr(display,'records',())))
   except BaseException:clean=False
  try:save(args.output/'ERROR.json',dict(display_close_receipt=display_failure_receipt,error=type(exc).__name__+': '+str(exc),automatic_retry=False,
   accepted_by_writer_frames=len(expected),producer_errors=errors,producer_stopped=not(producer and producer.is_alive()),
   source_errors=source_stage.failures,source_stopped=not(sealer and sealer.is_alive()),
   preparation_errors=preparation_errors,preparation_stopped=not(preparer and preparer.is_alive()),
   prepared_not_received_frames=ledger.prepared_ids(),
   persisted_frame_ids=([int(x['frame_id']) for x in writer.commits] if writer else []),
   ownership=ledger.status(),failure_evidence_frames=len(preserved),failure_evidence_error=preservation_error,
   failure_evidence_complete=preservation_error is None and not alive_workers,
   failed_primary_present=bool(transport is not None and getattr(transport,'failed_primary',None))))
  except BaseException:clean=False
  raise
 finally:
  stop.set()
  monitor_stop.set()
  if monitor:join_workers_until((monitor,),2.)
  alive=bool(producer and producer.is_alive())
  for obj in (publisher,writer,display,transport):
   if obj is None or (alive and obj is transport):continue
   try:
    if obj is display:
     try:
      close_receipt=obj.close(timeout_s=DISPLAY_CLOSE_TIMEOUT_S)
     except BaseException as close_error:
      close_receipt=getattr(close_error,'receipt',None)
      if isinstance(close_receipt,dict):
       confirmed=display_cleanup_confirmed(close_error,ProcessDisplayCloseError)
       save(args.output/('DISPLAY_CLEANUP_CONFIRMED.json' if confirmed else 'DISPLAY_CLEANUP_FAILED.json'),
        {key:value for key,value in close_receipt.items() if key!='records'})
       if confirmed:continue
      raise
    else:obj.close()
   except BaseException:clean=False
  if alive:clean=False
  if preparer and preparer.is_alive():clean=False
  if sealer and sealer.is_alive():clean=False
  if clean:lock.release()
  else:quarantine_owner(args.output)

def main():
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True)
 p.add_argument('--input-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--target',choices=['T05','T06'],required=True);p.add_argument('--limit',type=int,default=8)
 p.add_argument('--epoch',type=int,default=33);p.add_argument('--raster-manifest',type=Path)
 p.add_argument('--journal-preallocate-bytes',type=int,choices=(0,8388608,16777216),default=0)
 p.add_argument('--input-prefetch-depth',type=int,choices=range(5),default=0,
  help='0: sole DMA producer prepares input (default); 1..4: experimental bounded preparation thread')
 p.add_argument('--raw-encoding',choices=('zlib','identity'),default='zlib',
  help='R38 journal V2 Raw storage: zlib (verified input stream, default) or identity')
 p.add_argument('--parallel-display-startup',action=argparse.BooleanOptionalAction,default=True)
 p.add_argument('--overlap-display-close',action=argparse.BooleanOptionalAction,default=True)
 p.add_argument('--pl-view',action=argparse.BooleanOptionalAction,default=False,
  help='R40: FPGA renders the point view into the HDMI stream; ARM skips point projection')
 p.add_argument('--display-hold-s',type=float,default=0.,
  help='keep HDMI on this many seconds after the run for viewing (0..%d)'%DISPLAY_HOLD_MAX_S)
 p.add_argument('--source-worker',action=argparse.BooleanOptionalAction,default=True,
  help='ordered source sealing worker (default); --no-source-worker gives serial A/B')
 return run(p.parse_args())

if __name__=='__main__':main()
