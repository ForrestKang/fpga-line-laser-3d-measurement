"""Actual bounded SG DMA -> PL -> SG DMA transport. No image extractor imports.

The caller holds the existing cooperative hardware lock and power-off load scope.
No Overlay downloads, camera commands, motor writes or automatic reset retries.
"""
from pathlib import Path
import hashlib,struct,time,threading,json,os
import numpy as np
from .pfo3_r25 import HEADER_FIELDS,IDENTITY_FIELDS,PFO3Error
from .pfo3_stream_r25 import assemble
from .pfo3_replay_scope_r31 import PFO3ReplayScopeR31

DEFAULT_FINGERPRINT='bec4a5e36ccc36be6da6945979984ff630654b0cf6add3a094e88ee99e72c399'
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
    from .pfo3_hwh_r25 import audit_hwh
    artifacts=verify_stage_b_artifacts(path)
    audit_hwh(artifacts['hwh'])
    result=json.loads(Path(path).read_text())
    result.update(bit_absolute=str(Path(artifacts['bit']).resolve()),
                  resolved_manifest_sha256=_sha(path))
    return result

class ActualPLTransportR25:
    def __init__(self,overlay,*,manifest,hardware_lock,timeout_s=2.,output_bytes=RX_BYTES,body_validator=None):
        if body_validator is not None:
            from .pfo3_decode_native_r25 import NativeBodyValidatorR25
            if type(body_validator) is not NativeBodyValidatorR25:
                raise PFO3Error('exact manifest-admitted native body validator required')
            body_validator.require_manifest_admission()
        if not getattr(hardware_lock,'acquired',False):raise PFO3Error('hardware lock not owned')
        if not .05<=timeout_s<=5:raise PFO3Error('bounded DMA deadline required')
        if not 1024**2<=output_bytes<=RX_BYTES:raise PFO3Error('invalid output capacity')
        from pynq import MMIO,allocate,PL
        m=manifest
        if m.get('pfo3',{}).get('parameter_fingerprint')!=DEFAULT_FINGERPRINT:
            raise PFO3Error('unsupported physically compiled V3 parameters')
        actual=Path(PL.bitfile_name).resolve();expected=Path(m['bit_absolute']).resolve()
        if actual!=expected or _sha(actual)!=m['files']['bit']['sha256']:
            raise PFO3Error('loaded FPGA bit does not match final manifest')
        hwh=actual.with_suffix('.hwh')
        if _sha(hwh)!=m['files']['hwh']['sha256']:raise PFO3Error('HWH identity mismatch')
        from .pfo3_hwh_r25 import audit_hwh
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
        self.replay_scope=PFO3ReplayScopeR31(overlay,hardware_lock,MMIO,
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

    def run(self,raw,*,identity):
        if not self._run_lock.acquire(blocking=False):raise PFO3Error('same transport already has an in-flight caller')
        try:return self._run_once(raw,identity=identity)
        finally:self._run_lock.release()

    def _run_once(self,raw,*,identity):
        _t0=time.perf_counter()
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
                 algorithm_revision=25,fixed_revision=1,policy_revision=1)
        for i,value in enumerate(struct.unpack('<8I',bytes.fromhex(DEFAULT_FINGERPRINT))):h['fingerprint'+str(i)]=value
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
            frame,receipt,seal=assemble(primary,expected_identity=identity,expected_fingerprint=DEFAULT_FINGERPRINT,
                body_validator=getattr(self,'body_validator',None),return_seal=True)
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
            from .pfo3_processor_r25 import PLFrameAdmission
            admission=PLFrameAdmission(dict(identity),DEFAULT_FINGERPRINT,self.bit_sha,
                receipt['assembled_sha256'],True,input_raw_sha256=input_sha,
                _transport_capability=_TRANSPORT_TOKEN,_validated_frame=frame,
                _validated_primary=primary,_primary_sha256=receipt['primary_stream_sha256'],_pair_seal=seal)
            return frame,primary,receipt,admission
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
