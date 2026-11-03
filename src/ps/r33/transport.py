"""Actual bounded SG DMA -> PL -> SG DMA transport. No image extractor imports.

The caller holds the existing cooperative hardware lock and power-off load scope.
No Overlay downloads, camera commands, motor writes or automatic reset retries.
"""
from pathlib import Path
import hashlib,struct,time,threading,json,os
import numpy as np
from candidate.pfo3_r25 import HEADER_FIELDS,IDENTITY_FIELDS,PFO3Error
from .protocol import assemble, FINGERPRINT, BIN_FINGERPRINT, SEED_RETRY_FINGERPRINT
from .scope import PFO3ReplayScopeR31

DEFAULT_FINGERPRINT=FINGERPRINT
RX_BYTES=13*1024*1024
RAW_BYTES=752*480
_TRANSPORT_TOKEN=object()
TRANSPORT_STAGES={}
_PROFILING=bool(__import__('os').environ.get('SCAN3D_PROFILE_DIR'))


def _mark(name, t0):
    """Opt-in segment timing; ordering, checks and data flow are unchanged."""
    if not _PROFILING:return
    row=TRANSPORT_STAGES.setdefault(name,[0.0,0])
    row[0]+=time.perf_counter()-t0;row[1]+=1


def _mark_value(name, value):
    if not _PROFILING:return
    row=TRANSPORT_STAGES.setdefault(name,[0.0,0])
    row[0]+=float(value);row[1]+=1

def _sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda:stream.read(1024**2),b''):h.update(part)
    return h.hexdigest()

def resolve_manifest(path):
    """Verify artifacts and resolve relative paths without rewriting frozen JSON."""
    from structured_light.motion.pynq_runtime import verify_stage_b_artifacts
    from candidate.pfo3_hwh_r25 import audit_hwh
    # runtime06: verify_stage_b_artifacts admits the raw-only (no profile_dma) Overlay itself.
    artifacts=verify_stage_b_artifacts(path)
    audit_hwh(artifacts['hwh'])
    result=json.loads(Path(path).read_text())
    result.update(bit_absolute=str(Path(artifacts['bit']).resolve()),
                  resolved_manifest_sha256=_sha(path))
    return result

class ActualPLTransportR25:
    scope_class=PFO3ReplayScopeR31  # R41 live replaces it with PFO3LiveScopeR41
    binning=False                   # R41: 2x horizontal binning mode (needs a b14+ bit)
    seed_retry=False                # policy 3: same-run seed retry (manifest pfo3.same_run_seed_retry)
    algorithm_fingerprint=DEFAULT_FINGERPRINT
    def __init__(self,overlay,*,manifest,hardware_lock,timeout_s=2.,output_bytes=RX_BYTES,body_validator=None):
        if body_validator is not None:
            raise PFO3Error('old PFO3 validator cannot admit R33')
        if not getattr(hardware_lock,'acquired',False):raise PFO3Error('hardware lock not owned')
        if not .05<=timeout_s<=5:raise PFO3Error('bounded DMA deadline required')
        if not 1024**2<=output_bytes<=RX_BYTES:raise PFO3Error('invalid output capacity')
        from pynq import MMIO,allocate,PL
        m=manifest
        self.seed_retry=bool(m.get('pfo3',{}).get('same_run_seed_retry',False))
        self.algorithm_fingerprint=SEED_RETRY_FINGERPRINT if self.seed_retry else DEFAULT_FINGERPRINT
        if self.seed_retry and self.binning:
            raise PFO3Error('seed retry firmware mode requires fullwidth input')
        if m.get('pfo3',{}).get('parameter_fingerprint')!=self.algorithm_fingerprint:
            raise PFO3Error('unsupported physically compiled V3 parameters')
        actual=Path(PL.bitfile_name).resolve();expected=Path(m['bit_absolute']).resolve()
        if actual!=expected or _sha(actual)!=m['files']['bit']['sha256']:
            raise PFO3Error('loaded FPGA bit does not match final manifest')
        hwh=actual.with_suffix('.hwh')
        if _sha(hwh)!=m['files']['hwh']['sha256']:raise PFO3Error('HWH identity mismatch')
        from candidate.pfo3_hwh_r25 import audit_hwh
        audit_hwh(hwh)
        for name in ('pfo3_frontend','pfo3_dma'):
            if name not in overlay.ip_dict:raise PFO3Error('missing actual PFO3 hardware '+name)
        desc=overlay.ip_dict['pfo3_dma'];params={k.lower():v for k,v in desc['parameters'].items()}
        for name in ('c_include_sg','c_include_mm2s','c_include_s2mm'):
            if int(params.get(name,0))!=1:raise PFO3Error('required SG/MM2S/S2MM capability missing')
        if int(params.get('c_sg_length_width',0))!=24:raise PFO3Error('DMA requires exact24bit SG length contract')
        if int(params.get('c_sg_include_stscntrl_strm',-1))!=0:
            raise PFO3Error('unused SG application control/status streams must be disabled')
        ctrl_raw=MMIO(overlay.ip_dict['pfo3_frontend']['phys_addr'],4096)
        dma_raw=MMIO(desc['phys_addr'],4096)
        self.replay_scope=self.scope_class(overlay,hardware_lock,MMIO,
            ctrl_raw=ctrl_raw,dma_raw=dma_raw,hwh_path=hwh)
        self.ctrl=self.replay_scope.ctrl;self.dma=self.replay_scope.dma
        self.replay_scope.assert_safe('transport_init')
        self.allocate=allocate;self.timeout_s=timeout_s;self.lock=hardware_lock;self.bit_sha=m['files']['bit']['sha256']
        self.output_bytes=output_bytes;self.closed=False;self.poisoned=False;self.active=False
        self.failed_primary=None;self.failure_metadata={}
        self.body_validator=body_validator
        self._run_lock=threading.Lock()
        self.input=self.output=self.txbd=self.rxbd=None
        # The installed XRT explicitly supports cacheable BOs.  This opt-in
        # affects only CPU-produced TX bytes; every frame still flushes before
        # DMA. RX and descriptors retain the frozen allocation/coherence path.
        self.input_cache_policy=os.environ.get('SCAN3D_R29_TX_CACHE','baseline')
        if self.input_cache_policy not in ('baseline','cacheable'):
            raise PFO3Error('unsupported input cache policy')
        self._allocate_buffers()

    def _allocate_buffers(self):
        """Called only by constructor or the single run-lock owner."""
        if self.input is not None:return
        try:
            tx_options = ({'cacheable': 1}
                          if self.input_cache_policy == 'cacheable' else {})
            self.input=self.allocate(shape=(RAW_BYTES,),dtype=np.uint8,**tx_options)
            address=int(self.input.physical_address)
            if not 0<=address or address+RAW_BYTES>2**32:
                raise PFO3Error('TX input outside 32bit DMA address contract')
            if self.input_cache_policy=='cacheable' and self.input.coherent is not False:
                raise PFO3Error('cacheable TX experiment requires explicit noncoherent flush')
            self.output=self.allocate(shape=(self.output_bytes,),dtype=np.uint8)
            self.txbd=self.allocate(shape=(2,16),dtype=np.uint32)
            self.rxbd=self.allocate(shape=(2,16),dtype=np.uint32)
        except BaseException:
            for value in (self.rxbd,self.txbd,self.output,self.input):
                if value is not None:value.freebuffer()
            self.input=self.output=self.txbd=self.rxbd=None
            raise

    def release_idle_buffers(self):
        """Return CMA before final analysis; never stop or reset a live master.

        Independent register reads must confirm the prior finite transfer has
        halted. The same transport can allocate again for the next target; its
        PL sequence, identity and poisoned state are not reset by this method.
        """
        if not self._run_lock.acquire(blocking=False):
            raise PFO3Error('cannot retire buffers during a transport call')
        try:
            if self.closed or self.poisoned or self.active or not self.lock.acquired:
                raise PFO3Error('transport is not eligible for idle buffer retirement')
            self.replay_scope.assert_safe('buffer_retirement')
            released=0
            for value in (self.rxbd,self.txbd,self.output,self.input):
                if value is not None:
                    released+=value.nbytes
                    value.freebuffer()
            self.input=self.output=self.txbd=self.rxbd=None
            return dict(released_bytes=released,dma_halted=True,core_idle=True,
                        hardware_writes=0,sequence_reset=False)
        finally:self._run_lock.release()

    def _wait(self,predicate,deadline,label):
        while not predicate():
            if time.monotonic()>=deadline:raise TimeoutError(label)
            time.sleep(.0002)

    def _halt(self,offset,deadline):
        self.dma.write(offset,0)
        self._wait(lambda:bool(self.dma.read(offset+4)&1),deadline,'DMA halt not confirmed')

    def _arm(self,bd,buffer,offset,tx,deadline):
        bd[:]=0;half=(buffer.nbytes//2)//64*64
        # RX is one capacity descriptor: publishing an unused next packet BD
        # makes a finite S2MM halt wait for that packet. 13MiB fits SG24.
        spans=((0,half),(half,buffer.nbytes-half)) if tx else ((0,buffer.nbytes),)
        for i,(begin,length) in enumerate(spans):
            if length<=0 or length>0xffffff:raise PFO3Error('SG descriptor length outside24bit contract')
            bd[i,0]=int(bd.physical_address+((i+1)%len(spans))*64)&0xffffffff
            bd[i,2]=int(buffer.physical_address+begin)&0xffffffff
            bd[i,6]=length
            if tx:bd[i,6]|=(1<<27 if i==0 else 1<<26)
        bd.flush()
        self.dma.write(offset+8,int(bd.physical_address));self.dma.write(offset+12,0)
        self.dma.write(offset,1)
        self._wait(lambda:not(self.dma.read(offset+4)&1),deadline,'DMA start not confirmed')
        self.dma.write(offset+16,int(bd.physical_address+(len(spans)-1)*64));self.dma.write(offset+20,0)

    # ---- live mode: the PL processes every camera frame; run() is not used meanwhile
    def live_start(self):
        """Switch PFO3 to the camera stream; geometry must already be admitted."""
        if not getattr(self,'geometry_binding',None):raise PFO3Error('PL geometry parameters not configured')
        if self.closed or self.poisoned or self.active or not self.lock.acquired:raise PFO3Error('transport not available')
        self.replay_scope.assert_safe('live_start')
        self._allocate_buffers()
        if getattr(self,'live_raw',None) is None:self.live_raw=self.allocate(shape=(RAW_BYTES,),dtype=np.uint8)
        self.ctrl.write(0x24,1);self.live=True

    def live_stop(self,timeout_s=1.0):
        """Back to replay routing: the packet in flight finishes with the next camera frame."""
        self.ctrl.write(0x24,0);self.live=False
        deadline=time.monotonic()+timeout_s
        self._wait(lambda:not(self.ctrl.read(0x14)&1),deadline,'live packet did not finish')
        try:self._halt(0x30,deadline)
        except TimeoutError:
            if self.ctrl.read(0x24)&6:raise                 # a dump/copy is still owed to the DMA
            self._reset_rx(time.monotonic()+timeout_s)      # RX armed by an aborted grab, no packet coming

    def live_reangle(self,calibration,angle_rad,epoch,timeout_s=1.0):
        """New turntable angle for the live stream: hold the next packet, reconfigure, resume.

        Only the PL axis coordinates turn with the angle (the cloud view); camera coordinates,
        the section and the radius/height measurement do not change.
        """
        if not getattr(self,'live',False):raise PFO3Error('live mode is not running')
        if self.ctrl.read(0x24)&6:raise PFO3Error('live dump/copy still pending')
        self.ctrl.write(0x24,0)
        try:
            self._wait(lambda:not(self.ctrl.read(0x14)&1),time.monotonic()+timeout_s,'live packet did not finish')
            self.configure_geometry(calibration,angle_rad,epoch)
        finally:self.ctrl.write(0x24,1)

    def _reset_rx(self,deadline):
        """Soft-reset the PFO3 DMA when an armed RX descriptor will never get a packet."""
        self.dma.write(0x30,4)
        self._wait(lambda:not(self.dma.read(0x30)&4) and bool(self.dma.read(0x34)&1),deadline,'DMA reset not confirmed')

    def _owns(self,raw_dma):
        return getattr(raw_dma.transfer_state,'buffer',None) is self.live_raw

    def _detach(self,raw_dma):
        # live_raw belongs to this transport; the capture DMA only borrows it per grab
        if self._owns(raw_dma) and raw_dma._safe_to_release:
            raw_dma.transfer_state=None;raw_dma._reset_confirmed=raw_dma._safe_to_release=False

    def live_counters(self):
        return dict(packets=self.ctrl.read(0x28),errors=self.ctrl.read(0x2c),
                    last_dump=self.ctrl.read(0x30),last_copy=self.ctrl.read(0x34))

    def grab_live(self,raw_dma,*,identity):
        """The next live packet and the camera frame it was computed from (not decoded yet).

        The identity is written into the header words first; the dumped packet starts after
        that write, so its header carries it. Returns (pixels, primary, live sequence).
        """
        if not getattr(self,'live',False):raise PFO3Error('live mode is not running')
        if set(identity)!=set(IDENTITY_FIELDS) or identity['source'] not in (1,2):raise PFO3Error('actual source identity required')
        if any(type(v) is not int or not 0<=v<2**64 for v in identity.values()):raise PFO3Error('invalid input identity')
        if not self._run_lock.acquire(blocking=False):raise PFO3Error('same transport already has an in-flight caller')
        try:
            h={name:0 for name in HEADER_FIELDS}
            for key,value in identity.items():
                if key in ('motor_steps','FV_ticks','exposure_start','exposure_end'):
                    h[key+'_lo']=value&0xffffffff;h[key+'_hi']=value>>32
                else:
                    if value>=2**32:raise PFO3Error('identity scalar exceeds uint32')
                    h[key]=value
            h.update(width=752,height=480,roi_x1=752,roi_y1=480,roi_pixels=RAW_BYTES,
                     algorithm_revision=33,fixed_revision=1,policy_revision=3 if self.seed_retry else (2 if self.binning else 1))
            frame_fp=self.algorithm_fingerprint if self.seed_retry else (BIN_FINGERPRINT if self.binning else DEFAULT_FINGERPRINT)
            for i,value in enumerate(struct.unpack('<8I',bytes.fromhex(frame_fp))):h['fingerprint'+str(i)]=value
            for i,name in enumerate(HEADER_FIELDS):self.ctrl.write(0x100+4*i,h[name])
            deadline=time.monotonic()+self.timeout_s;self.active=True;requested=False;before=None
            try:
                self._halt(0x30,deadline)
                # copy target first: a failure here leaves no PFO3 descriptor waiting
                raw_dma.arm(self.live_raw,RAW_BYTES)
                self._arm(self.rxbd,self.output,0x30,False,deadline)
                before=self.ctrl.read(0x30)
                self.ctrl.write(0x24,1|2|4);requested=True
                size=0
                while True:
                    if time.monotonic()>=deadline:raise TimeoutError('bounded live packet timeout')
                    if self.dma.read(0x34)&0x770:raise PFO3Error('live S2MM DMA error')
                    self.rxbd.invalidate();done=False;size=0
                    for descriptor in self.rxbd:
                        status=int(descriptor[7])
                        if status&0x70000000:raise PFO3Error('SG descriptor data error')
                        if not status&0x80000000:break
                        if size==0 and not status&(1<<27):raise PFO3Error('RX packet start not confirmed')
                        size+=status&0xffffff
                        if status&(1<<26):done=True;break
                    if done:break
                    time.sleep(.0002)
                raw_dma.wait(max(.05,deadline-time.monotonic()))
                self._halt(0x30,deadline)
                if self.dma.read(0x34)&0x770:raise PFO3Error('post-halt live DMA error')
            except BaseException:
                self._abort_grab(raw_dma,before if requested else None)
                raise
            finally:
                self.active=False
                self._detach(raw_dma)
            sequence=self.ctrl.read(0x30)
            if sequence==before:raise PFO3Error('live dump sequence did not advance')
            self.output.invalidate()
            primary=bytes(memoryview(self.output)[:size])
            self.live_raw.invalidate()
            return bytes(memoryview(self.live_raw)),primary,sequence
        finally:self._run_lock.release()

    def _abort_grab(self,raw_dma,before):
        """Leave no DMA waiting: a requested dump still arrives with the next camera frame."""
        try:
            end=time.monotonic()+1.0
            if before is not None:   # the PL will send the packet and the frame; let both land
                self._wait(lambda:self.ctrl.read(0x30)!=before,end,'live dump not drained')
                try:raw_dma.wait(max(.05,end-time.monotonic()))
                except Exception:pass
            try:self._halt(0x30,end)
            except TimeoutError:self._reset_rx(time.monotonic()+.5)
        finally:
            if self._owns(raw_dma) and not raw_dma._safe_to_release:raw_dma.stop()

    def decode_live(self,primary,*,identity,sequence):
        """Decode a grabbed live packet exactly as run() decodes a replay packet."""
        frame_fp=self.algorithm_fingerprint if self.seed_retry else (BIN_FINGERPRINT if self.binning else DEFAULT_FINGERPRINT)
        frame,receipt,seal=assemble(primary,expected_identity=identity,expected_fingerprint=frame_fp,binning=self.binning,
            body_validator=getattr(self,'body_validator',None),return_seal=True)
        if frame.geometry_epoch!=self.geometry_binding['epoch'] or frame.geometry_sha256!=self.geometry_binding['sha256']:
            raise PFO3Error('PL geometry parameter provenance mismatch')
        frame.geometry_binding=dict(self.geometry_binding)
        receipt.update(geometry_backend='ACTUAL_PL_FIXED_Q30_Q16',ARM_geometry_calls=0,live=True,
                       live_sequence=sequence,point_records=len(frame.geometry_records),
                       actual_PL_execution=True,hardware_source=identity['source'])
        return frame,receipt

    def configure_geometry(self, calibration, angle_rad, frame_id):
        from geometry_reference import parameters
        p=parameters(calibration, angle_rad)
        if p[34]&8:raise PFO3Error('unsupported PL geometry calibration')
        self.geometry_binding=None
        packed=struct.pack('<36q',*p)
        digest=hashlib.sha256(packed).digest()
        if self.ctrl.read(0x14)&1:raise PFO3Error('geometry configuration while frame busy')
        for i,v in enumerate(p):
            self.ctrl.write(0x300,i);self.ctrl.write(0x304,v&0xffffffff)
            self.ctrl.write(0x308,(v>>32)&0xffffffff);self.ctrl.write(0x30c,1)
        for i,v in enumerate(struct.unpack('<8I',digest)):self.ctrl.write(0x340+i*4,v)
        self.ctrl.write(0x310,int(frame_id));self.ctrl.write(0x30c,2)
        deadline=time.monotonic()+1.0
        while True:
            status=self.ctrl.read(0x30c)
            if not status&4:
                if status&2 or not status&1 or self.ctrl.read(0x320)!=int(frame_id):
                    raise PFO3Error('geometry parameter commit rejected or epoch mismatch')
                break
            if time.monotonic()>=deadline:raise TimeoutError('geometry commit acknowledgement timeout')
            time.sleep(.0001)
        self.geometry_binding=dict(epoch=int(frame_id),sha256=digest.hex(),
            calibration_sha256=calibration.source_sha256,angle_rad=float(angle_rad),parameters=p)

    def run(self,raw,*,identity):
        if not self._run_lock.acquire(blocking=False):raise PFO3Error('same transport already has an in-flight caller')
        try:return self._run_once(raw,identity=identity)
        finally:self._run_lock.release()

    def _run_once(self,raw,*,identity):
        _t0=time.perf_counter()
        if not getattr(self,'geometry_binding',None):raise PFO3Error('PL geometry parameters not configured')
        if self.closed or self.poisoned or self.active or not self.lock.acquired:raise PFO3Error('transport not available')
        self.failed_primary=None;self.failure_metadata={}
        if set(identity)!=set(IDENTITY_FIELDS) or identity['source'] not in (1,2):raise PFO3Error('actual source identity required')
        if any(type(v) is not int or not 0<=v<2**64 for v in identity.values()):raise PFO3Error('invalid input identity')
        image=np.asarray(raw)
        if image.dtype!=np.uint8 or image.shape!=(480,752):raise PFO3Error('complete native Raw required')
        scope_start=self.replay_scope.assert_safe('frame_start')
        self._allocate_buffers()
        np.copyto(self.input,image.reshape(-1));self.input.flush()
        input_sha=hashlib.sha256(memoryview(self.input)).hexdigest()
        _mark('s1_input_copy_hash', _t0)
        _t0=time.perf_counter()
        h={name:0 for name in HEADER_FIELDS}
        for key,value in identity.items():
            if key in ('motor_steps','FV_ticks','exposure_start','exposure_end'):
                h[key+'_lo']=value&0xffffffff;h[key+'_hi']=value>>32
            else:
                if value>=2**32:raise PFO3Error('identity scalar exceeds uint32')
                h[key]=value
        h.update(width=752,height=480,roi_x1=752,roi_y1=480,roi_pixels=RAW_BYTES,
                 algorithm_revision=33,fixed_revision=1,policy_revision=3 if self.seed_retry else (2 if self.binning else 1))
        frame_fp=self.algorithm_fingerprint if self.seed_retry else (BIN_FINGERPRINT if self.binning else DEFAULT_FINGERPRINT)
        for i,value in enumerate(struct.unpack('<8I',bytes.fromhex(frame_fp))):h['fingerprint'+str(i)]=value
        if self.ctrl.read(0x14)&1:raise PFO3Error('hardware busy before new frame')
        sequence=self.ctrl.read(0x1c)
        if sequence==0xffffffff:raise PFO3Error('hardware sequence wrap requires explicit new epoch')
        for i,name in enumerate(HEADER_FIELDS):self.ctrl.write(0x100+4*i,h[name])
        _mark('s2_header_mmio', _t0)
        _t0=time.perf_counter()
        deadline=time.monotonic()+self.timeout_s;self.active=True;started=time.perf_counter()
        try:
            self._halt(0,deadline);self._halt(0x30,deadline)
            self._arm(self.rxbd,self.output,0x30,False,deadline)
            self.ctrl.write(0x10,1)
            self._arm(self.txbd,self.input,0,True,deadline)
            _mark('s3_dma_submit', _t0)
            _t0=time.perf_counter()
            size=0
            while True:
                if time.monotonic()>=deadline:raise TimeoutError('bounded PL frame timeout')
                if self.dma.read(4)&0x770 or self.dma.read(0x34)&0x770:raise PFO3Error('actual SG DMA error')
                self.rxbd.invalidate();done=False;size=0
                for descriptor in self.rxbd:
                    status=int(descriptor[7])
                    if status&0x70000000:raise PFO3Error('SG descriptor data error')
                    if not status&0x80000000:break
                    if size==0 and not status&(1<<27):raise PFO3Error('RX packet start not confirmed')
                    length=status&0xffffff
                    if length>(int(descriptor[6])&0xffffff):raise PFO3Error('invalid SG completion length')
                    size+=length
                    if status&(1<<26):done=True;break
                if done:break
                time.sleep(.0002)
            self.txbd.invalidate()
            if any(not(int(d[7])&0x80000000) or int(d[7])&0x70000000 for d in self.txbd):
                raise PFO3Error('input SG completion not confirmed')
            self._halt(0,deadline);self._halt(0x30,deadline)
            if self.dma.read(4)&0x770 or self.dma.read(0x34)&0x770:raise PFO3Error('post-halt DMA error')
            _mark('s4_dma_wait', _t0)
            _t0=time.perf_counter()
            self.output.invalidate()
            primary=bytes(memoryview(self.output)[:size])
            _mark('s5_reassemble', _t0)
            _t0=time.perf_counter()
            if self.ctrl.read(0x1c)!=((sequence+1)&0xffffffff) or self.ctrl.read(0x14)&1:
                raise PFO3Error('PFO3 hardware sequence/completion mismatch')
            frame,receipt,seal=assemble(primary,expected_identity=identity,expected_fingerprint=frame_fp,binning=self.binning,
                body_validator=getattr(self,'body_validator',None),return_seal=True)
            if frame.geometry_epoch!=self.geometry_binding['epoch'] or frame.geometry_sha256!=self.geometry_binding['sha256']:
                raise PFO3Error('PL geometry parameter provenance mismatch')
            frame.geometry_binding=dict(self.geometry_binding)
            receipt.update(geometry_backend='ACTUAL_PL_FIXED_Q30_Q16',ARM_geometry_calls=0,
                           geometry_binding=dict(self.geometry_binding),point_records=len(frame.geometry_records))
            scope_end=self.replay_scope.assert_safe('frame_complete')
            _hw=int(frame.header['processing_cycles_lo'])+(int(frame.header['processing_cycles_hi'])<<32)
            _mark_value('hw_cycles',_hw)
            _mark('s6_validate_decode', _t0)
            receipt.update(input_sha256=input_sha,bit_sha256=self.bit_sha,wall_s=time.perf_counter()-started,
                input_cache_policy=self.input_cache_policy,
                input_physical_address=int(self.input.physical_address),
                input_coherent=bool(self.input.coherent),
                actual_PL_execution=True,hardware_source=identity['source'],
                replay_scope_start=scope_start,replay_scope_complete=scope_end)
            return frame,primary,receipt,None
        except BaseException as original_error:
            self.poisoned=True
            self.failed_primary=locals().get('primary')
            self.failure_metadata=dict(original_error=type(original_error).__name__+': '+str(original_error),
                identity=dict(identity),input_sha256=input_sha,hardware_quiescent=False,automatic_retry=False)
            try:
                self.ctrl.write(0x10,2)
                recovery=time.monotonic()+.5
                self._halt(0,recovery);self._halt(0x30,recovery)
                self._wait(lambda:not(self.ctrl.read(0x14)&1),recovery,'PFO3 abort unconfirmed')
                self.active=False
                self.failure_metadata['hardware_quiescent']=True
                self.failure_metadata['replay_scope']=self.replay_scope.assert_safe('error_cleanup')
            except BaseException as cleanup_error:
                self.failure_metadata['cleanup_error']=type(cleanup_error).__name__+': '+str(cleanup_error)
                raise RuntimeError('DMA/core cleanup unconfirmed; retain all buffers and hardware lock; original='
                    +type(original_error).__name__+': '+str(original_error)+'; cleanup='
                    +type(cleanup_error).__name__+': '+str(cleanup_error)) from original_error
            raise
        finally:
            if not self.poisoned:self.active=False

    def close(self):
        if not self._run_lock.acquire(blocking=False):
            raise PFO3Error('cannot release buffers while a caller prepares or runs DMA')
        try:
            if self.active:raise PFO3Error('cannot release hardware-owned DMA buffers')
            if not self.closed:
                self.replay_scope.assert_safe('transport_close')
                for value in (self.rxbd,self.txbd,self.output,self.input):
                    if value is not None:value.freebuffer()
                self.closed=True
        finally:self._run_lock.release()
