"""Replay ownership/metrics tests, with fake hardware; no board operations."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import zlib
import numpy as np
from . import replay

TEMP = Path('D:/Workspace/CodexTemp/r33_replay_tests')
TEMP.mkdir(parents=True, exist_ok=True)


class ReplayTests(unittest.TestCase):
 def test_small_run_quota_still_reserves_one_worst_frame(self):
  for limit in range(1,7):
   with self.subTest(limit=limit):
    worst,quota,pending,disk=replay.capacity_budgets(limit)
    self.assertGreaterEqual(quota,worst)
    self.assertEqual(disk,quota+int(pending*1.25)+2**31)
    ledger=replay.AdmissionLedger(quota,pending)
    ledger.reserve(0,worst,threading.Event(),timeout_s=.01)
    self.assertEqual(ledger.status()['pending_bytes'],worst)
  self.assertEqual(replay.capacity_budgets(240)[1],240*4*1024**2)

 def test_quota_reservation_precedes_dma_and_shrinks(self):
  x = replay.AdmissionLedger(100,80); stop=threading.Event()
  x.reserve(0,60,stop)
  with self.assertRaisesRegex(TimeoutError,'before DMA'): x.reserve(1,60,stop,timeout_s=.001)
  result=types.SimpleNamespace(charge_bytes=10)
  x.seal(0,result);x.reserve(1,60,stop)
  self.assertEqual(x.status()['run_charged_bytes'],70)
  x.refresh([dict(frame_id=0)])
  self.assertEqual(x.status()['pending_bytes'],60)
  self.assertEqual(x.status()['run_charged_bytes'],70)
  # A writer ACK racing with submit return is legal.
  x.refresh([dict(frame_id=1)]);x.accepted(1,1)
  self.assertFalse(x.snapshot())
  with self.assertRaisesRegex(BufferError,'before DMA'):x.reserve(2,60,stop)

 def test_pending_capacity_cancels_without_discard(self):
  x=replay.AdmissionLedger(1000,70);stop=threading.Event()
  x.reserve(0,60,stop);stop.set()
  with self.assertRaisesRegex(RuntimeError,'before DMA'):x.reserve(1,60,stop)
  q=queue.Queue(1);q.put('occupied')
  self.assertFalse(replay.put_owned(q,{'frame_id':0},stop))
  self.assertIn(0,x.snapshot())

 def test_preserves_current_queued_and_inflight_inputs(self):
  entries={0:dict(state='PL_in_flight',raw=b'raw0',PL_started=True),
           1:dict(state='PL_complete',raw=b'raw1',primary=b'primary',observations_r33=b'payload',PL_returned=True),
           2:dict(state='writer_accepted',result=types.SimpleNamespace(components=(('raw',b'raw2'),('measurement_xyz',b'xyz'))))}
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   saved=replay.preserve_pending(folder,entries)
   self.assertEqual(len(saved),3)
   for i,e in enumerate(saved):
    for name,desc in e['components'].items():
     content=(Path(folder)/'failure_evidence'/('frame_%04d'%i)/(name+'.bin')).read_bytes()
     self.assertEqual(hashlib.sha256(content).hexdigest(),desc['sha256'])
   self.assertEqual((Path(folder)/'failure_evidence/frame_0002/measurement_xyz.bin').read_bytes(),b'xyz')

 def test_readback_all_components_not_just_point_count(self):
  expected={0:{'raw':'abc','measurement_xyz':'def'}}
  r=dict(passed=True,frames=1,frame_summaries=[dict(frame_id=0,component_sha256=expected[0])],
         measurement_xyz_sha256='m',preview_xyz_sha256='p')
  self.assertTrue(replay.verify_readback(r,expected,'m','p'))
  for key in ('measurement_xyz_sha256','preview_xyz_sha256'):
   bad=dict(r);bad[key]='tampered'
   with self.assertRaises(ValueError):replay.verify_readback(bad,expected,'m','p')
  r['frame_summaries'][0]['component_sha256']={'raw':'changed','measurement_xyz':'def'}
  with self.assertRaisesRegex(ValueError,'component SHA'):replay.verify_readback(r,expected,'m','p')

 def test_windows_and_real_new_model_updates(self):
  self.assertEqual([x['hz'] for x in replay.windows([0,4.9,5,9.9,10],0,10)],[.4,.4])
  records=[dict(frame_id=0,cloud_points=2,receipt=True,ack_perf_s=1.),
           dict(frame_id=0,cloud_points=2,receipt=True,ack_perf_s=1.1),
           dict(frame_id=1,cloud_points=2,receipt=True,ack_perf_s=1.2),
           dict(frame_id=2,cloud_points=3,receipt=False,ack_perf_s=1.3),
           dict(frame_id=2,cloud_points=3,receipt=True,ack_perf_s=1.4)]
  self.assertEqual(replay.new_model_acks(records,bool),[1.,1.4])

 def test_fixed_windows_numpy32_inputs_are_native_json_scalars(self):
  # Mirrors the failing HDMI-through-close field: processing is shorter than
  # five seconds, but display drain yields one complete reporting window.
  start=np.float64(12786.125);end=np.float64(12791.875)
  events=np.asarray([12786.5,12787.,12790.,12791.125],dtype=np.float64)
  report=replay.windows(events,start,end)
  self.assertEqual(report,[dict(start_s=0.,end_s=5.,count=3,hz=.6)])
  self.assertEqual(json.loads(json.dumps(report,allow_nan=False)),report)
  self.assertIs(type(report[0]['count']),int)
  self.assertIs(type(report[0]['hz']),float)
  ints=replay.windows(np.array([0,4,5,9,10],dtype=np.int32),np.int32(0),np.int32(10))
  self.assertEqual([r['count'] for r in json.loads(json.dumps(ints))],[2,2])
  self.assertEqual(replay.windows([],0,4.9999999995),[])

 def fake_run(self, folder, fail_geometry=False,fail_writer=False,limit=8,fail_display_close=False,
              fail_transport_index=None,corrupt_input_index=None,dma_hook=None,prefetch_depth=None,
              fail_display_startup=False,startup_cleanup_unconfirmed=False,startup_cleanup_error=None):
  source=Path(folder)/'inputs/T05';source.mkdir(parents=True)
  raw=bytes([33])*360960;compressed=zlib.compress(raw)
  (source/'raw.gray8.zblocks').write_bytes(compressed*8)
  (source/'frames.jsonl').write_text('\n'.join(json.dumps(dict(frame_id=i,
   raw_sha256=('0'*64 if i==corrupt_input_index else hashlib.sha256(raw).hexdigest()),angle_rad=i*.1)) for i in range(8)))
  (source/'RAW_BLOCK_INDEX.json').write_text(json.dumps([dict(offset=i*len(compressed),bytes=len(compressed)) for i in range(8)]))
  manifest=Path(folder)/'manifest.json';manifest.write_text('{}')
  args=types.SimpleNamespace(limit=limit,output=Path(folder)/'out',manifest=manifest,input_root=Path(folder)/'inputs',target='T05',epoch=33,raster_manifest=None)
  if prefetch_depth is not None:args.input_prefetch_depth=prefetch_depth
  calls=[];writer_instances=[];writer_settings=[]
  class Lock:
   def __init__(self,**kw):self._handle=types.SimpleNamespace(fileno=lambda:1)
   def acquire(self):return self
   def release(self):calls.append('release')
  class Guard:
   def __enter__(self):return self
   def __exit__(self,*a):pass
   def report(self):return {'ARM_pixel_extraction_calls':0}
  class Transport:
   failed_primary=None
   def __init__(self,*a,**kw):pass
   def configure_geometry(self,*a,**kw):pass
   def run(self,gray,identity):
    i=identity['frame_id'];calls.append(('DMA',i))
    if dma_hook:dma_hook(i)
    if i==fail_transport_index:
     self.failed_primary=b'partial-primary';raise RuntimeError('injected transport failure')
    frame=types.SimpleNamespace(payload=('payload%d'%i).encode(),header={'processing_cycles_lo':42,'processing_cycles_hi':0})
    return frame,('primary%d'%i).encode(),{},None
   def close(self):calls.append('transport_close')
  CloseError=type('ProcessDisplayCloseError',(RuntimeError,),{})
  class Display:
   def __init__(self,*a,**kw):
    self.records=[];self.closed_calls=0
    if fail_display_startup:
     error=type('SessionDisplayStartupError',(RuntimeError,),{})('injected startup failure')
     error.display=self;raise error
   def begin_target(self,*a):pass
   def close(self,**kw):
    self.closed_calls+=1
    calls.append('display_close')
    if startup_cleanup_error is not None:
     error=CloseError('historical startup failure after confirmed close')
     error.receipt=dict(startup_cleanup_error,close_timeout_s=kw.get('timeout_s'),records=list(self.records))
     raise error
    if startup_cleanup_unconfirmed or (fail_display_close and self.closed_calls==1):
     error=CloseError('injected close unconfirmed')
     error.receipt=dict(cleanup_confirmed=False,owner_closed=True,worker_joined=False,
      close_phase='awaiting_spawn_exit',close_timeout_s=kw.get('timeout_s'),records=list(self.records))
     raise error
    return dict(cleanup_confirmed=True,close_timeout_s=kw.get('timeout_s'))
  class Publisher:
   def __init__(self,display):self.display=display;self.points=0
   def submit(self,gray,m,i,angle,**kw):
    self.points+=len(m);self.display.records.append(dict(frame_id=i,cloud_points=self.points,receipt=True,ack_perf_s=replay.time.perf_counter()))
   def drain(self,*a):pass
   def close(self,*a):pass
  class Source:
   @staticmethod
   def create(**kw):return kw
  class Result:
   @staticmethod
   def from_source(source,**kw):return Result.create(**source,**kw)
   @staticmethod
   def create(**kw):
    components=tuple((n,b) for n,b in [('raw',kw['raw']),('primary',kw['primary']),('observations_r33',kw['frame'].payload),
                     ('measurement_xyz',kw['measurement_xyz'].astype('<f8').tobytes()),('preview_xyz',kw['preview_xyz'].astype('<f8').tobytes())])
    return types.SimpleNamespace(frame_id=kw['metadata']['frame_id'],charge_bytes=sum(len(b) for n,b in components)+1000,
      components=components,descriptors=tuple((n,len(b),hashlib.sha256(b).hexdigest(),0) for n,b in components))
  class Writer:
   def __init__(self,*a,**kw):self.items=[];self.commits=[];writer_instances.append(self);writer_settings.append(dict(kw))
   def submit(self,result,**kw):
    if fail_writer:raise OSError('injected writer failure')
    self.items.append(result);self.commits.append(dict(frame_id=result.frame_id,persisted_ns=replay.time.perf_counter_ns()))
    return len(self.items)-1
   def try_submit(self,result):return self.submit(result)
   def status(self):return {'persisted':len(self.items)}
   def counters(self):return self.status()
   def commits_since(self,cursor=0):return {'cursor':len(self.commits),'commits':tuple(self.commits[cursor:])}
   def close(self,**kw):pass
  def readback(*a,**kw):
   w=writer_instances[0];m=hashlib.sha256();p=hashlib.sha256();rows=[]
   for item in w.items:
    comp=dict(item.components);m.update(comp['measurement_xyz']);p.update(comp['preview_xyz'])
    rows.append(dict(frame_id=item.frame_id,component_sha256={n:sha for n,size,sha,crc in item.descriptors}))
   return dict(passed=True,frames=len(rows),frame_summaries=rows,measurement_xyz_sha256=m.hexdigest(),preview_xyz_sha256=p.hexdigest())
  def geometry(frame,cal,**kw):
   if fail_geometry:raise RuntimeError('injected geometry failure')
   return types.SimpleNamespace(measurement_axis_mm=np.array([[1.,2.,3.]]),preview_axis_mm=np.array([[4.,5.,6.]]),rejection_counts={})
  def module(name,**values):
   m=types.ModuleType(name);m.__dict__.update(values);return m
  modules={
   'pynq':module('pynq',Overlay=lambda *a,**kw:object()),
   'structured_light.motion.lifecycle':module('lifecycle',HardwareProcessLock=Lock),
   'tools.pl_replay_r25':module('pl_replay',NormalImageGuard=Guard,replay_identity=lambda i,e:dict(frame_id=i)),
   'r33.transport':module('transport',ActualPLTransportR25=Transport,resolve_manifest=lambda p:dict(bit_absolute='unused',files={},pfo3={'parameter_fingerprint':'fake'}),TRANSPORT_STAGES={}),
   'r33.geometry':module('geometry',load_calibration=lambda p:types.SimpleNamespace(calibration_id='fake',evidence_type='SIMULATION_ONLY'),reconstruct_pair=geometry),
   'r33.storage':module('storage',FrameResult=Result,SourceSnapshot=Source,AsyncWriter=Writer,readback=readback,SOURCE_CHECKSUM_CHUNK_BYTES=1234),
   'candidate.display_process_r25':module('display',ProcessSessionDisplayR25=Display,ProcessDisplayCloseError=CloseError,MIN_ARCHIVE_SLOT_BYTES=1,display_timing_summary=lambda r:{}),
   'r33.display':module('r33.display',renderer_factory=None,deferred_annotations_from_frame=lambda *a,**kw:None),
   'candidate.display_publisher_r30':module('publisher',DisplayPublisherR30=Publisher),
   'tools.board_replay_r22':module('board',ReplayOwner=None,valid_vdma_ack=bool,quarantine_owner=lambda p:calls.append('quarantine')),
   'candidate.runtime_monitor_r22':module('monitor',linux_sample=lambda:dict(MemAvailable_bytes=256*1024**2,cpu_total_ticks=100,cpu_idle_ticks=50)),
   'cv2':module('cv2',setNumThreads=lambda x:None)}
  original_read=Path.read_text
  def read_text(path,*a,**kw):
   if str(path).replace('\\','/')=='/proc/sys/kernel/random/boot_id':return 'fake-boot'
   return original_read(path,*a,**kw)
  with patch.dict(sys.modules,modules),patch.object(Path,'read_text',read_text),patch.object(replay.shutil,'disk_usage',return_value=types.SimpleNamespace(free=100*1024**3)),contextlib.redirect_stdout(io.StringIO()):
   if fail_display_startup:
    with self.assertRaisesRegex(RuntimeError,'injected startup failure'):replay.run(args)
   elif fail_display_close:
    with self.assertRaisesRegex(RuntimeError,'injected close unconfirmed'):replay.run(args)
   elif fail_geometry:
    with self.assertRaisesRegex(RuntimeError,'injected geometry'):replay.run(args)
   elif fail_writer:
    with self.assertRaisesRegex(OSError,'injected writer'):replay.run(args)
   elif fail_transport_index is not None:
    with self.assertRaisesRegex(RuntimeError,'injected transport'):replay.run(args)
   elif corrupt_input_index is not None:
    with self.assertRaisesRegex(RuntimeError,'frozen Raw SHA'):replay.run(args)
   else:replay.run(args)
  if writer_settings:
   configuration=json.loads((args.output/'CONFIG.json').read_text())
   recorded=dict(configuration['writer_configuration']);self.assertEqual(recorded.pop('compression'),'raw_zlib_passthrough')
   self.assertEqual(recorded.pop('raw_encoding'),'zlib');recorded.pop('journal_format')
   self.assertEqual(recorded,writer_settings[0])
   self.assertEqual(configuration['source_sealing']['source_checksum_chunk_bytes'],1234)
   display_settings=configuration['display_configuration']
   self.assertNotIn('owner_log',display_settings['owner_config'])
   self.assertNotIn('hardware_lock_fd',display_settings['owner_config'])
   self.assertEqual(display_settings['owner_config']['display_startup_canvas_timeout_s'],20.)
  return args.output,calls

 def test_startup_failure_retains_partial_instance_for_bounded_cleanup(self):
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,fail_display_startup=True)
   self.assertEqual(calls.count('display_close'),1)
   self.assertIn('transport_close',calls);self.assertIn('release',calls)
   self.assertNotIn('quarantine',calls)
   error=json.loads((output/'ERROR.json').read_text())
   self.assertEqual(error['accepted_by_writer_frames'],0)
   self.assertFalse((output/'RESULT.json').exists())

 def test_startup_unconfirmed_cleanup_persists_receipt_and_retains_lock(self):
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,fail_display_startup=True,startup_cleanup_unconfirmed=True)
   self.assertEqual(calls.count('display_close'),1)
   self.assertNotIn('release',calls);self.assertIn('quarantine',calls)
   receipt=json.loads((output/'DISPLAY_CLEANUP_FAILED.json').read_text())
   self.assertFalse(receipt['cleanup_confirmed']);self.assertEqual(receipt['close_timeout_s'],30.)
   self.assertNotIn('records',receipt)

 def test_startup_error_with_confirmed_close_releases_lock_but_run_fails(self):
  receipt=dict(cleanup_confirmed=True,owner_closed=True,worker_joined=True,
               worker_exitcode=0,retained_lock_fd=False,error='historical startup failure')
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,fail_display_startup=True,startup_cleanup_error=receipt)
   self.assertIn('release',calls);self.assertNotIn('quarantine',calls)
   self.assertIn('transport_close',calls)
   self.assertEqual(calls.count('display_close'),1)
   error=json.loads((output/'ERROR.json').read_text())
   self.assertEqual(error['error'],'SessionDisplayStartupError: injected startup failure')
   self.assertEqual(error['accepted_by_writer_frames'],0)
   self.assertFalse((output/'RESULT.json').exists())
   persisted=json.loads((output/'DISPLAY_CLEANUP_CONFIRMED.json').read_text())
   self.assertEqual(persisted['error'],'historical startup failure')
   self.assertNotIn('records',persisted)

 def test_display_cleanup_requires_exact_error_and_strict_complete_receipt(self):
  error_type=type('ProcessDisplayCloseError',(RuntimeError,),{})
  good=dict(cleanup_confirmed=True,owner_closed=True,worker_joined=True,
            worker_exitcode=0,retained_lock_fd=False)
  error=error_type('historical failure');error.receipt=good
  self.assertTrue(replay.display_cleanup_confirmed(error,error_type))
  forged=type('ProcessDisplayCloseError',(RuntimeError,),{})('same name is not same type')
  forged.receipt=good
  self.assertFalse(replay.display_cleanup_confirmed(forged,error_type))
  subclass=type('DerivedCloseError',(error_type,),{})('derived');subclass.receipt=good
  self.assertFalse(replay.display_cleanup_confirmed(subclass,error_type))
  invalid=[]
  for key in good:
   missing=dict(good);missing.pop(key);invalid.append(missing)
  for key in ('cleanup_confirmed','owner_closed','worker_joined'):
   for value in (False,1,'true',None):invalid.append(dict(good,**{key:value}))
  for value in (True,None,1,-1,0.,'0'):invalid.append(dict(good,worker_exitcode=value))
  for value in (True,0,None,'false'):invalid.append(dict(good,retained_lock_fd=value))
  invalid.extend((None,[],True))
  for receipt in invalid:
   with self.subTest(receipt=receipt):
    error.receipt=receipt
    self.assertFalse(replay.display_cleanup_confirmed(error,error_type))

 def test_incomplete_startup_close_receipt_keeps_quarantine(self):
  receipt=dict(cleanup_confirmed=True,owner_closed=True,worker_joined=True,
               worker_exitcode=0) # retained lock state is unknown, not false.
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,fail_display_startup=True,startup_cleanup_error=receipt)
   self.assertNotIn('release',calls);self.assertIn('quarantine',calls)
   self.assertTrue((output/'DISPLAY_CLEANUP_FAILED.json').exists())
   self.assertFalse((output/'RESULT.json').exists())

 def test_close_failure_preserves_diagnostic_receipt_and_ack_records(self):
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,fail_display_close=True)
   error=json.loads((output/'ERROR.json').read_text())
   receipt=error['display_close_receipt']
   self.assertFalse(receipt['cleanup_confirmed']);self.assertFalse(receipt['worker_joined'])
   self.assertTrue(receipt['owner_closed']);self.assertEqual(receipt['close_timeout_s'],30.)
   self.assertNotIn('records',receipt)
   self.assertEqual(len(json.loads((output/'DISPLAY_FAILED.json').read_text())),8)
   self.assertEqual(len(error['persisted_frame_ids']),8)
   self.assertFalse((output/'RESULT.json').exists())

 def test_full_fake_chain_has_readback_wall_and_persisted_metrics(self):
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder)
   result=json.loads((output/'RESULT.json').read_text())
   self.assertEqual(result['frames'],8)
   self.assertEqual(result['display_close']['close_timeout_s'],30.)
   self.assertEqual(json.loads((output/'CONFIG.json').read_text())['display_close_timeout_s'],30.)
   self.assertEqual(result['new_model_ack_count'],8)
   self.assertEqual(result['ownership']['pending_frames'],0)
   self.assertTrue(result['readback_content_matches_live'])
   self.assertGreater(result['wall_clock_s'],result['activity_s'])
   self.assertIn('persisted_windows',result)
   self.assertEqual(len([x for x in calls if isinstance(x,tuple)]),8)
   trace=json.loads((output/'TRACE.json').read_text())
   for row in trace:
    self.assertLessEqual(row['input_begin'],row['source_ready_ns']/1e9)
    self.assertLessEqual(row['source_ready_ns']/1e9,row['transport_begin'])
    self.assertGreaterEqual(row['prepared_wait_s'],0)
    self.assertLessEqual(row['ownership']['total_owned_bytes'],64*1024**2)
   self.assertEqual(result['ownership']['prepared_frames'],0)

 def test_default_serial_prepares_in_unique_dma_thread_without_prefetch(self):
  events=[];reader=replay.read_prepared_input
  def read(stream,block,metadata,frame_id,stop):
   events.append(('read',frame_id,threading.current_thread().name))
   return reader(stream,block,metadata,frame_id,stop)
  def dma(i):events.append(('dma',i,threading.current_thread().name))
  with tempfile.TemporaryDirectory(dir=TEMP) as folder,patch.object(replay,'read_prepared_input',side_effect=read):
   output,calls=self.fake_run(folder,dma_hook=dma)
   self.assertEqual([(kind,i) for kind,i,name in events],[(kind,i) for i in range(8) for kind in ('read','dma')])
   self.assertEqual({name for kind,i,name in events},{'R33_unique_DMA_owner'})
   config=json.loads((output/'CONFIG.json').read_text())
   self.assertEqual(config['input_queue_frames'],0)
   prep=config['input_preparation']
   self.assertEqual(prep['backend'],'sole_DMA_producer_serial')
   self.assertEqual(prep['prefetch_depth'],0);self.assertEqual(prep['maximum_frames'],1)
   self.assertEqual(prep['maximum_bytes'],replay.PREP_FRAME_BYTES)
   result=json.loads((output/'RESULT.json').read_text())
   self.assertEqual(result['ownership']['preparation_frame_limit'],1)
   self.assertEqual(result['ownership']['max_inflight_frames'],128)
   self.assertEqual(result['ownership']['pending_capacity_bytes'],64*1024**2)

 def test_serial_bad_sha_preserves_executed_inputs_and_never_dma_bad_frame(self):
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,corrupt_input_index=3)
   error=json.loads((output/'ERROR.json').read_text())
   self.assertEqual([v[1] for v in calls if isinstance(v,tuple)],list(range(3)))
   self.assertEqual(error['preparation_errors'][0]['frame_id'],3)
   self.assertFalse(error['preparation_errors'][0]['PL_started'])
   self.assertTrue(error['producer_stopped']);self.assertTrue(error['preparation_stopped'])
   kept={r['frame_id'] for r in json.loads((output/'failure_evidence/MANIFEST.json').read_text())['frames']}
   self.assertTrue(set(range(3))<=kept|set(error['persisted_frame_ids']))
   self.assertNotIn(3,kept);self.assertEqual(error['prepared_not_received_frames'],[])

 def test_serial_cancel_before_next_dma_keeps_previous_evidence(self):
  reader=replay.read_prepared_input
  def read(stream,block,metadata,frame_id,stop):
   if frame_id==1:stop.set()
   return reader(stream,block,metadata,frame_id,stop)
  with tempfile.TemporaryDirectory(dir=TEMP) as folder,patch.object(replay,'read_prepared_input',side_effect=read):
   # fake_run normally expects success; cancellation is an expected bounded error.
   with self.assertRaisesRegex(RuntimeError,'cancelled before DMA'):self.fake_run(folder)
   output=Path(folder)/'out';error=json.loads((output/'ERROR.json').read_text())
   self.assertEqual(error['preparation_errors'][0]['frame_id'],1)
   self.assertTrue(error['producer_stopped']);self.assertTrue(error['preparation_stopped'])
   kept={r['frame_id'] for r in json.loads((output/'failure_evidence/MANIFEST.json').read_text())['frames']}
   self.assertIn(0,kept|set(error['persisted_frame_ids']))
   self.assertNotIn(1,kept);self.assertEqual(error['prepared_not_received_frames'],[])

 def test_prefetch_depth_range_and_actual_config(self):
  for depth in (1,2,3,4):
   with self.subTest(depth=depth),tempfile.TemporaryDirectory(dir=TEMP) as folder:
    output,calls=self.fake_run(folder,limit=8,prefetch_depth=depth)
    config=json.loads((output/'CONFIG.json').read_text())
    self.assertEqual(config['input_queue_frames'],depth)
    self.assertEqual(config['input_preparation']['prefetch_depth'],depth)
    self.assertEqual(config['input_preparation']['maximum_bytes'],depth*replay.PREP_FRAME_BYTES)
    self.assertEqual(config['input_preparation']['backend'],'bounded_independent_thread')
  for depth in (-1,5,True,1.5,'4'):
   with self.subTest(depth=depth),self.assertRaisesRegex(ValueError,'0..4'):
    replay.run(types.SimpleNamespace(input_prefetch_depth=depth))
  for depth in (0,5,True):
   with self.assertRaisesRegex(ValueError,'preparation frame capacity'):
    replay.AdmissionLedger(1000,preparation_frames=depth)

 def test_cli_defaults_to_serial_and_accepts_explicit_four(self):
  common=['replay','--manifest','m','--input-root','i','--output','o','--target','T05']
  for extra,expected in (([],0),(['--input-prefetch-depth','4'],4)):
   with patch.object(sys,'argv',common+extra),patch.object(replay,'run',return_value='sentinel') as run:
    self.assertEqual(replay.main(),'sentinel')
    self.assertEqual(run.call_args.args[0].input_prefetch_depth,expected)

 def test_single_frame_fake_chain_includes_metrics_in_wall_clock(self):
  from .metrics import sliding_windows
  elapsed=[]
  def delayed_metrics(*args,**kwargs):
   begin=replay.time.perf_counter()
   replay.time.sleep(.01)
   value=sliding_windows(*args,**kwargs)
   elapsed.append(replay.time.perf_counter()-begin)
   return value
  with tempfile.TemporaryDirectory(dir=TEMP) as folder,patch('r33.metrics.sliding_windows',side_effect=delayed_metrics):
   output,calls=self.fake_run(folder,limit=1)
   result=json.loads((output/'RESULT.json').read_text())
   self.assertEqual(result['frames'],1)
   self.assertEqual(len(elapsed),3)
   self.assertGreaterEqual(result['timing']['metrics_s'],sum(elapsed))
   self.assertGreaterEqual(result['timing']['export_s'],result['timing']['metrics_s'])
   self.assertGreaterEqual(result['wall_clock_s'],result['activity_s']+result['timing']['metrics_s'])
   self.assertEqual(len([x for x in calls if isinstance(x,tuple)]),1)
   self.assertFalse(result['XYZ_sliding_windows']['evaluated'])

 def test_geometry_failure_preserves_every_executed_fake_dma_frame(self):
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,fail_geometry=True)
   manifest=json.loads((output/'failure_evidence/MANIFEST.json').read_text())
   preserved={x['frame_id']:x for x in manifest['frames']}
   for kind,i in [x for x in calls if isinstance(x,tuple)]:
    self.assertIn(i,preserved)
    self.assertIn('raw',preserved[i]['components'])
    self.assertIn('primary',preserved[i]['components'])
    self.assertIn('observations_r33',preserved[i]['components'])
   self.assertTrue(json.loads((output/'ERROR.json').read_text())['producer_stopped'])

 def test_writer_failure_preserves_unaccepted_geometry_result(self):
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,fail_writer=True)
   manifest=json.loads((output/'failure_evidence/MANIFEST.json').read_text())
   current=next(x for x in manifest['frames'] if x['frame_id']==0)
   self.assertEqual(current['state'],'sealed')
   self.assertIn('measurement_xyz',current['components'])
   self.assertIn('preview_xyz',current['components'])
   error=json.loads((output/'ERROR.json').read_text())
   self.assertEqual(error['accepted_by_writer_frames'],0)
   self.assertTrue(error['failure_evidence_complete'])

 def test_bounded_preparation_strict_zlib_length_sha_and_identity(self):
  raw=bytes([11])*replay.RAW_BYTES;compressed=zlib.compress(raw)
  metadata=dict(frame_id=0,raw_sha256=hashlib.sha256(raw).hexdigest())
  def check(content=compressed,meta=metadata,block=None):
   return replay.read_prepared_input(io.BytesIO(content),
    block or dict(offset=0,bytes=len(content)),meta,0,threading.Event())
  packet=check();self.assertEqual(packet['raw'],raw);self.assertIs(type(packet['raw']),bytes)
  self.assertEqual(packet['raw_zlib_stream'],compressed)
  for content in (compressed[:-1],compressed+b'junk',compressed+compressed,
                  zlib.compress(raw+b'x'),zlib.compress(raw[:-1]),zlib.compress(raw*2)):
   with self.subTest(length=len(content)),self.assertRaises((ValueError,zlib.error)):check(content)
  with self.assertRaisesRegex(ValueError,'truncated'):
   check(block=dict(offset=0,bytes=len(compressed)+1))
  for block in (dict(offset=-1,bytes=1),dict(offset=0,bytes=replay.MAX_COMPRESSED_BYTES+1),
                dict(offset=True,bytes=1),dict(offset=0,bytes=0)):
   with self.assertRaisesRegex(ValueError,'bounds'):check(block=block)
  with self.assertRaisesRegex(ValueError,'SHA'):check(meta=dict(metadata,raw_sha256='0'*64))
  with self.assertRaisesRegex(ValueError,'identity'):check(meta=dict(metadata,frame_id=1))

 def test_prepare_capacity_counts_transient_and_atomic_dma_transfer(self):
  ledger=replay.AdmissionLedger(100*1024**2,64*1024**2);stop=threading.Event()
  for i in range(4):ledger.prepare(i,stop)
  self.assertEqual(ledger.status()['prepared_bytes'],replay.PREP_BYTES)
  with self.assertRaisesRegex(TimeoutError,'preparation ownership'):
   ledger.prepare(4,stop,timeout_s=.001)
  ledger.prepared_ready(0,b'\0'*replay.RAW_BYTES)
  before=ledger.status()['total_owned_bytes'];charge=ledger.prepared[0]['charge']
  ledger.reserve(0,20*1024**2,stop,transfer_prepared=True)
  status=ledger.status()
  self.assertEqual(status['total_owned_bytes'],before-charge+20*1024**2)
  self.assertEqual(status['prepared_frames'],3);self.assertEqual(status['pending_frames'],1)
  self.assertLessEqual(status['peak_total_owned_bytes'],status['pending_capacity_bytes'])
  with self.assertRaisesRegex(ValueError,'unprepared'):
   ledger.reserve(5,10,stop,transfer_prepared=True)
  stop.set()
  with self.assertRaisesRegex(RuntimeError,'cancelled'):ledger.reserve(6,10,stop)
  self.assertNotIn(6,ledger.entries)

 def test_cancel_preparation_waiter_and_read(self):
  ledger=replay.AdmissionLedger(100*1024**2);stop=threading.Event();errors=[]
  for i in range(4):ledger.prepare(i,stop)
  def waiting():
   try:ledger.prepare(4,stop)
   except RuntimeError as error:errors.append(str(error))
  worker=threading.Thread(target=waiting);worker.start();stop.set();worker.join(.5)
  self.assertFalse(worker.is_alive());self.assertEqual(len(errors),1)
  with self.assertRaisesRegex(RuntimeError,'cancelled'):
   replay.read_prepared_input(io.BytesIO(),{},dict(),0,stop)
  self.assertEqual(ledger.status()['prepared_frames'],4)

 def test_input_preparation_really_overlaps_unique_dma_and_keeps_order(self):
  dma_started=threading.Event();fifth_prepared=threading.Event();read=replay.read_prepared_input
  def preparing(stream,block,metadata,frame_id,stop):
   if frame_id==4:
    self.assertTrue(dma_started.wait(1.))
   packet=read(stream,block,metadata,frame_id,stop)
   if frame_id==4:fifth_prepared.set()
   return packet
  def dma(i):
   if i==0:
    dma_started.set();self.assertTrue(fifth_prepared.wait(1.))
  with tempfile.TemporaryDirectory(dir=TEMP) as folder,patch.object(replay,'read_prepared_input',side_effect=preparing):
   output,calls=self.fake_run(folder,dma_hook=dma,prefetch_depth=4)
   self.assertEqual([x[1] for x in calls if isinstance(x,tuple)],list(range(8)))
   self.assertEqual(json.loads((output/'RESULT.json').read_text())['frames'],8)

 def test_preparation_bad_sha_is_not_received_and_all_dma_evidence_survives(self):
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,corrupt_input_index=4,prefetch_depth=4)
   error=json.loads((output/'ERROR.json').read_text())
   self.assertTrue(error['preparation_stopped']);self.assertTrue(error['producer_stopped'])
   self.assertEqual(error['preparation_errors'][0]['frame_id'],4)
   self.assertFalse(error['preparation_errors'][0]['accepted_measurement'])
   manifest=json.loads((output/'failure_evidence/MANIFEST.json').read_text())
   preserved={x['frame_id']:x for x in manifest['frames']}
   for i in [x[1] for x in calls if isinstance(x,tuple)]:
    if i in error['persisted_frame_ids']:continue
    self.assertIn(i,preserved)
    self.assertIn('raw',preserved[i]['components'])
    self.assertIn('primary',preserved[i]['components'])
   self.assertNotIn(4,[x[1] for x in calls if isinstance(x,tuple)])

 def test_transport_failure_preserves_partial_primary_and_unreceived_prepared_ids(self):
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   output,calls=self.fake_run(folder,fail_transport_index=0,prefetch_depth=4)
   error=json.loads((output/'ERROR.json').read_text())
   self.assertTrue(error['failure_evidence_complete']);self.assertTrue(error['preparation_stopped'])
   manifest=json.loads((output/'failure_evidence/MANIFEST.json').read_text())
   current=next(x for x in manifest['frames'] if x['frame_id']==0)
   self.assertTrue(current['PL_started']);self.assertFalse(current['PL_returned'])
   self.assertEqual((output/'failure_evidence/frame_0000/primary.bin').read_bytes(),b'partial-primary')
   self.assertNotIn(0,error['prepared_not_received_frames'])

 def test_frame_slots_reserved_before_dma_and_commit_releases_third(self):
  ledger=replay.AdmissionLedger(100*1024**2,max_inflight_frames=2)
  stop=threading.Event();commit_ready=threading.Event();waiting=threading.Event()
  third_dma=threading.Event();errors=[];dma=[]
  for i in range(2):
   ledger.reserve(i,1024,stop);dma.append(i)
   # A sealed, small frame still owns a writer slot until durable ACK.
   ledger.seal(i,types.SimpleNamespace(charge_bytes=64))
  ledger.prepare(2,stop);ledger.prepared_ready(2,b'\0'*replay.RAW_BYTES)
  before=ledger.status()
  def commits():
   waiting.set()
   return (dict(frame_id=0),) if commit_ready.is_set() else ()
  def produce_third():
   try:
    ledger.reserve(2,1024,stop,commits,timeout_s=1.,transfer_prepared=True)
    dma.append(2);third_dma.set()
   except BaseException as error:errors.append(error)
  worker=threading.Thread(target=produce_third);worker.start()
  self.assertTrue(waiting.wait(.5));self.assertFalse(third_dma.wait(.05))
  self.assertEqual(dma,[0,1]);self.assertEqual(ledger.status()['pending_frames'],2)
  self.assertEqual(ledger.status()['prepared_frames'],1)
  self.assertEqual(ledger.status()['total_owned_bytes'],before['total_owned_bytes'])
  commit_ready.set();worker.join(1.)
  self.assertFalse(worker.is_alive());self.assertFalse(errors);self.assertTrue(third_dma.is_set())
  self.assertEqual(dma,[0,1,2]);self.assertEqual(set(ledger.snapshot()),{1,2})
  self.assertEqual(ledger.status()['prepared_frames'],0)
  self.assertEqual(ledger.status()['peak_inflight_frames'],2)

 def test_frame_slot_timeout_cancel_keep_all_dma_and_prepared_ownership(self):
  ledger=replay.AdmissionLedger(100*1024**2,max_inflight_frames=2);stop=threading.Event()
  for i in range(2):
   ledger.reserve(i,1024,stop)
   ledger.update(i,raw=b'raw'+bytes([i]),primary=b'primary',PL_started=True,PL_returned=True)
  ledger.prepare(2,stop);ledger.prepared_ready(2,b'\0'*replay.RAW_BYTES)
  with self.assertRaisesRegex(TimeoutError,'before DMA'):
   ledger.reserve(2,1024,stop,timeout_s=.001,transfer_prepared=True)
  self.assertEqual(set(ledger.snapshot()),{0,1});self.assertEqual(ledger.prepared_ids(),[2])
  stop.set()
  with self.assertRaisesRegex(RuntimeError,'cancelled before DMA'):
   ledger.reserve(2,1024,stop,transfer_prepared=True)
  self.assertEqual(set(ledger.snapshot()),{0,1});self.assertEqual(ledger.prepared_ids(),[2])
  with tempfile.TemporaryDirectory(dir=TEMP) as folder:
   preserved=replay.preserve_pending(folder,ledger.snapshot())
   self.assertEqual([x['frame_id'] for x in preserved],[0,1])
   for row in preserved:
    self.assertTrue(row['PL_started']);self.assertTrue(row['PL_returned'])
    self.assertIn('raw',row['components']);self.assertIn('primary',row['components'])
  self.assertEqual(ledger.status()['peak_inflight_frames'],2)

 def test_inflight_limit_matches_writer_and_rejects_invalid_limits(self):
  ledger=replay.AdmissionLedger(100*1024**2);stop=threading.Event()
  self.assertEqual(ledger.status()['max_inflight_frames'],32)
  for i in range(32):ledger.reserve(i,1,stop)
  with self.assertRaisesRegex(TimeoutError,'before DMA'):
   ledger.reserve(32,1,stop,timeout_s=.001)
  self.assertEqual(ledger.status()['pending_frames'],32)
  for limit in (0,-1,129,True,2.5):
   with self.subTest(limit=limit),self.assertRaises(ValueError):
    replay.AdmissionLedger(1000,max_inflight_frames=limit)

class R34AdmissionTests(unittest.TestCase):
 def test_128_small_frames_still_obey_shared_byte_bound(self):
  ledger=replay.AdmissionLedger(256*1024**2,max_inflight_frames=128)
  stop=threading.Event()
  for i in range(128):ledger.reserve(i,1024,stop)
  with self.assertRaisesRegex(TimeoutError,'before DMA'):ledger.reserve(128,1024,stop,timeout_s=.002)
  self.assertEqual(ledger.status()['pending_frames'],128)
  self.assertLessEqual(ledger.status()['total_owned_bytes'],64*1024**2)
  ledger.refresh([{'frame_id':i} for i in range(128)])
  self.assertEqual(ledger.status()['pending_bytes'],0)
 def test_128_slots_never_bypass_byte_limit(self):
  ledger=replay.AdmissionLedger(256*1024**2,max_inflight_frames=128)
  stop=threading.Event()
  ledger.reserve(0,60*1024**2,stop)
  with self.assertRaisesRegex(TimeoutError,'before DMA'):ledger.reserve(1,8*1024**2,stop,timeout_s=.002)
  self.assertEqual(ledger.status()['pending_frames'],1)

if __name__=='__main__':unittest.main()
