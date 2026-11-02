"""Optional lazy spawn CPU stage. No runtime, DMA, MMIO or ownership crosses IPC.

The owner admits SLC3 once. IPC restores that admitted immutable CPU record,
not a second wire admission. One serial request computes selection and optional
cached-context geometry. All exceptions/timeouts are terminal, with Raw kept.
"""
from dataclasses import asdict, fields, is_dataclass, replace
import math
import multiprocessing
import os
from pathlib import Path
import struct
import sys
import threading
import time
from types import SimpleNamespace

_SPAWN_ENV_LOCK = threading.Lock()
_THREAD_ENV = ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS',
               'NUMEXPR_NUM_THREADS','BLIS_NUM_THREADS','VECLIB_MAXIMUM_THREADS')


class CPUStageError(RuntimeError):
    """Terminal pure-CPU stage failure; caller retains its capture evidence."""


def _plain(value):
    if type(value) in (str,int,bool,float,bytes,type(None)):
        return value
    if type(value) in (tuple,list):
        return type(value)(_plain(item) for item in value)
    if type(value) is dict and all(type(key) is str for key in value):
        return {key:_plain(item) for key,item in value.items()}
    raise TypeError('CPU IPC accepts plain immutable data and frozen configuration fields only')


def _configs(config, selection):
    from structured_light.stripe_v1.types import StripeConfig
    from structured_light.stripe_v1.selection_v2 import SelectionConfigV2
    from structured_light.stripe_v1.selection_v3 import SelectionConfigV3
    from structured_light.stripe_v1.selection_v4 import SelectionConfigV4
    if (type(config) is not StripeConfig or type(selection) is not SelectionConfigV4
        or type(selection.legacy) is not SelectionConfigV3
        or type(selection.legacy.selection) is not SelectionConfigV2):
        raise TypeError('CPU stage requires exact frozen StripeConfig and V4/V3/V2 policy')
    return _plain(asdict(config)), _plain(asdict(selection))


def _restore_configs(config, selection):
    from structured_light.stripe_v1.types import StripeConfig
    from structured_light.stripe_v1.selection_v2 import SelectionConfigV2
    from structured_light.stripe_v1.selection_v3 import SelectionConfigV3
    from structured_light.stripe_v1.selection_v4 import SelectionConfigV4
    selection=dict(selection);legacy=dict(selection.pop('legacy'))
    legacy['selection']=SelectionConfigV2(**legacy['selection'])
    return StripeConfig(**config), SelectionConfigV4(legacy=SelectionConfigV3(**legacy),**selection)


def _packet_data(packet):
    from structured_light.scan3d.stream_segments_r15 import SegmentPacket
    from structured_light.scan3d.stream_ridge import RidgePacket
    from structured_light.scan3d.stream_features import FeatureConfiguration
    if (type(packet) is not SegmentPacket or type(packet.ridge_packet) is not RidgePacket
        or type(packet.configuration) is not FeatureConfiguration
        or type(packet.payload) is not bytes or type(packet.ridge_packet.payload) is not bytes):
        raise TypeError('CPU stage requires the owner-admitted immutable SegmentPacket')
    identity=(packet.epoch,packet.request_id)
    if any(type(value) is not int for value in identity):
        raise ValueError('packet identity must be integers')
    # Bind the exact admitted headers/configuration, without redoing row/global
    # protocol validation or trusting serialized cached NumPy properties.
    for payload in (packet.payload,packet.ridge_packet.payload):
        if len(payload)<128 or struct.unpack_from('<II',payload,12)!=(identity[1],identity[0]):
            raise ValueError('admitted packet/header identity mismatch')
    values=tuple(_plain(getattr(packet.configuration,f.name))
                 for f in fields(packet.configuration))
    return (packet.payload,packet.ridge_packet.payload,identity,values)


def _restore_packet(data):
    from structured_light.scan3d.stream_segments_r15 import SegmentPacket
    from structured_light.scan3d.stream_ridge import RidgePacket
    from structured_light.scan3d.stream_features import FeatureConfiguration
    wire,ridge_wire,(epoch,request_id),values=data
    return SegmentPacket(wire,RidgePacket(ridge_wire,request_id,epoch,FeatureConfiguration(*values)))


def _readonly(value):
    import numpy as np
    if isinstance(value,np.ndarray):
        if value.dtype.hasobject:
            raise CPUStageError('object arrays cannot cross CPU result boundary')
        return np.frombuffer(value.tobytes(),dtype=value.dtype).reshape(value.shape)
    if is_dataclass(value):
        return replace(value,**{f.name:_readonly(getattr(value,f.name)) for f in fields(value) if f.init})
    if isinstance(value,dict):return {key:_readonly(item) for key,item in value.items()}
    if isinstance(value,tuple):return tuple(_readonly(item) for item in value)
    if isinstance(value,list):return [_readonly(item) for item in value]
    return value


def _stripe_data(stripe):
    from structured_light.stripe_v1.stripe_fast import StripeRows
    if type(stripe) is not StripeRows:
        raise ValueError('geometry cache requires exact selected StripeRows')
    return tuple((value.dtype.str,value.shape,value.tobytes()) for value in
        (stripe.center_q12,stripe.start,stripe.end,stripe.accepted,stripe.retained))+(tuple(stripe.reasons),)


def _memory():
    """Actual current/peak child RSS, or explicitly unavailable."""
    try:
        if sys.platform=='win32':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[
                    (key,ctypes.c_size_t) for key in ('PeakWorkingSetSize','WorkingSetSize',
                    'QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
            counters=Counters();counters.cb=ctypes.sizeof(counters)
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.GetCurrentProcess.restype=wintypes.HANDLE
            psapi=ctypes.WinDLL('psapi',use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
                raise OSError('GetProcessMemoryInfo failed')
            return dict(rss_bytes=int(counters.WorkingSetSize),peak_rss_bytes=int(counters.PeakWorkingSetSize))
        data={line.split(':')[0]:line.split(':')[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
        return dict(rss_bytes=int(data['VmRSS'].split()[0])*1024,
                    peak_rss_bytes=int(data['VmHWM'].split()[0])*1024,
                    os_threads=int(data['Threads']))
    except (OSError,KeyError,ValueError):
        return dict(rss_bytes=None,peak_rss_bytes=None)


class _RejectPynq:
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='pynq' or fullname.startswith('pynq.'):
            raise ImportError('pynq is forbidden in the pure CPU child')


def _forbidden_fd_target(target):
    target=target.removesuffix(' (deleted)')
    return (target in ('/dev/mem','/dev/xlnk','/dev/gpiomem','/tmp/sl_camera_preview.lock')
        or target.startswith(('/dev/uio','/dev/dri/','/dev/i2c-','/dev/spidev',
                              '/dev/udmabuf','/dev/dma_heap/'))
        or Path(target).name in ('scan3d_hardware.lock','hardware_owner.lock','scan3d_hw.lock'))


def _inspect_fds():
    if not sys.platform.startswith('linux'):
        return dict(performed=False,reason='Linux/proc/self/fd unavailable on this host',targets=[])
    targets=[]
    for path in Path('/proc/self/fd').iterdir():
        try:target=os.readlink(path)
        except FileNotFoundError:continue  # transient directory enumeration fd
        targets.append(dict(fd=int(path.name),target=target))
    forbidden=[row for row in targets if _forbidden_fd_target(row['target'])]
    if forbidden:raise CPUStageError('CPU child inherited forbidden hardware/owner fd: '+str(forbidden))
    return dict(performed=True,targets=sorted(targets,key=lambda item:item['fd']),
                forbidden_targets=[],scope='Read-only own fd symlinks; no target opened')


def _available_memory():
    if not sys.platform.startswith('linux'):return None
    memory={line.split(':')[0]:line.split(':')[1].strip() for line in
            Path('/proc/meminfo').read_text().splitlines() if ':' in line}
    return int(memory['MemAvailable'].split()[0])*1024


def _cpu_worker(connection,camera_path,plane_path):
    sys.meta_path.insert(0,_RejectPynq())
    try:
        if any(name=='pynq' or name.startswith('pynq.') for name in sys.modules):
            raise RuntimeError('CPU spawn main imported pynq before worker startup')
        import cv2
        import numpy as np
        cv2.setNumThreads(1)
        from structured_light.scan3d.local_segments_r16 import select_local_segment_candidates
        from structured_light.scan3d.selected_geometry_r14 import SelectedGeometryBenchmark
        geometry=(SelectedGeometryBenchmark(camera_path,plane_path,cached_context=True)
                  if camera_path is not None else None)
        connection.send(dict(kind='ready',pid=os.getpid(),cv2_threads=cv2.getNumThreads(),
            python_threads=threading.active_count(),
            thread_environment={key:os.environ.get(key) for key in _THREAD_ENV},
            cpu_s=time.process_time(),pynq_imported=False,fd_inspection=_inspect_fds(),
            calibration_identity=(dict(camera_sha256=geometry.calibration.camera_sha256,
                plane_sha256=geometry.calibration.plane_sha256) if geometry else None),**_memory()))
        last=None;sequence=0
        while True:
            request=connection.recv()
            if request is None:break
            started=time.perf_counter();cpu=time.process_time()
            seq,data,raw,config_data,selection_data=request
            packet=_restore_packet(data);identity=(packet.epoch,packet.request_id)
            if seq!=sequence+1 or (last is not None and (identity[0]!=last[0] or identity[1]<=last[1])):
                raise ValueError('CPU child requires ordered same-epoch requests')
            config,selection=_restore_configs(config_data,selection_data)
            gray=np.frombuffer(raw,np.uint8).reshape(packet.configuration.height,packet.configuration.width)
            local_stats={};selected=time.perf_counter()
            stripe,primary=select_local_segment_candidates(packet,gray,config,selection,stats=local_stats)
            selection_s=time.perf_counter()-selected
            geom_started=time.perf_counter()
            extra=(geometry(SimpleNamespace(epoch=packet.epoch,request_id=packet.request_id,
                    compact_packet=packet),stripe,config,selection,False) if geometry is not None else None)
            metrics=dict(selection_s=selection_s,geometry_s=time.perf_counter()-geom_started,
                child_service_s=time.perf_counter()-started,child_cpu_s=time.process_time()-cpu,
                local_selection=local_stats,**_memory())
            connection.send(dict(kind='result',sequence=seq,identity=identity,
                                 stripe=stripe,primary=primary,extra=extra,metrics=metrics))
            last=identity;sequence=seq
    except (EOFError,BrokenPipeError):
        pass
    except BaseException as error:
        try:connection.send(dict(kind='error',error_type=type(error).__name__,error=str(error)))
        except (EOFError,BrokenPipeError,OSError):pass
    finally:
        connection.close()


class CPUStageProcess:
    """Lazy one-child/one-pipe computation, with a one-frame identity-bound cache.

    ``stats`` reports actual child CPU/RSS and parent roundtrip overhead. Startup
    has a separate capped deadline. No retry/fallback is implicit. ``failed_input``
    retains immutable Raw/packet/config on terminal failure, including after close.
    """
    def __init__(self,camera_path=None,plane_path=None,timeout_s=5,*,startup_timeout_s=30):
        if (camera_path is None)!=(plane_path is None):
            raise ValueError('camera and plane must both be supplied or both absent')
        for value,label in ((timeout_s,'timeout_s'),(startup_timeout_s,'startup_timeout_s')):
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
                raise ValueError(label+' must be finite and positive')
        if startup_timeout_s>30:raise ValueError('startup_timeout_s must be<=30')
        self.camera_path=str(Path(camera_path).resolve()) if camera_path is not None else None
        self.plane_path=str(Path(plane_path).resolve()) if plane_path is not None else None
        self.timeout_s=float(timeout_s);self.startup_timeout_s=float(startup_timeout_s)
        self._process=None;self._connection=None;self._thread=None
        self._call_lock=threading.Lock();self._closed=False;self._failure=None
        self._last=None;self._cache=None;self._sequence=0;self.failed_input=None
        self.stats=dict(start_method='spawn',started=False,closed=False,requests=0,
                        completed=0,geometry_cache_hits=0,request_timings=[])

    def _exchange(self,message,timeout,*,receive_only=False):
        box=[];complete=threading.Event();connection=self._connection
        def exchange():
            try:
                if not receive_only:connection.send(message)
                box.append((True,connection.recv()))
            except BaseException as error:box.append((False,error))
            finally:complete.set()
        worker=threading.Thread(target=exchange,name='CPUStageIPC',daemon=True)
        self._thread=worker;worker.start()
        if not complete.wait(timeout):
            raise TimeoutError('CPU '+('startup' if receive_only else 'request')+' deadline expired')
        worker.join()
        if not box[0][0]:raise CPUStageError('CPU worker IPC closed: '+str(box[0][1]))
        return box[0][1]

    def _start(self):
        if self._process is not None:
            if not self._process.is_alive():raise CPUStageError('CPU worker died')
            return
        available=_available_memory()
        if available is not None:
            self.stats['MemAvailable_before_start_bytes']=available
            if available<128*1024*1024:
                raise CPUStageError('MemAvailable<128MiB; CPU child not started')
        started=time.perf_counter();ctx=multiprocessing.get_context('spawn')
        parent,child=ctx.Pipe(duplex=True)
        self._connection=parent
        self._process=ctx.Process(target=_cpu_worker,args=(child,self.camera_path,self.plane_path),
                                  name='scan3d-pure-CPU',daemon=False)
        try:
            with _SPAWN_ENV_LOCK:
                previous={key:os.environ.get(key) for key in _THREAD_ENV}
                try:
                    os.environ.update({key:'1' for key in _THREAD_ENV})
                    self._process.start()
                finally:
                    for key,value in previous.items():
                        if value is None:os.environ.pop(key,None)
                        else:os.environ[key]=value
            child.close()
            ready=self._exchange(None,self.startup_timeout_s,receive_only=True)
            if ready.get('kind')!='ready':raise CPUStageError('CPU startup failed: '+str(ready))
            self.stats.update(started=True,startup_s=time.perf_counter()-started,child=ready)
        except BaseException:
            child.close()
            raise

    def start(self):
        """Explicit prepare hook; lazy construction itself never starts a child."""
        if not self._call_lock.acquire(blocking=False):raise CPUStageError('CPU stage already has one request')
        try:
            self._check();self._start()
            return dict(self.stats['child'],startup_s=self.stats['startup_s'],
                start_method='spawn',camera_path=self.camera_path,plane_path=self.plane_path,
                requests=self.stats['requests'])
        except BaseException as error:
            self._fail(error)
            raise CPUStageError(str(error)) from error
        finally:self._call_lock.release()

    def _check(self):
        if self._closed:raise CPUStageError('CPU stage is closed')
        if self._failure is not None:raise CPUStageError('CPU stage failed closed: '+self._failure)

    def _fail(self,error,request=None):
        self._failure=str(error);self.stats['failure']=self._failure
        if request is not None:self.failed_input=request
        self._cache=None
        self._stop(graceful=False)

    def select(self,packet,gray,config,selection):
        import numpy as np
        if not self._call_lock.acquire(blocking=False):raise CPUStageError('CPU stage already has one request')
        request=None
        try:
            self._check()
            if self.camera_path is not None and self._cache is not None:
                raise ValueError('previous geometry result has not been consumed')
            data=_packet_data(packet);config_data,selection_data=_configs(config,selection)
            if (type(gray) is not np.ndarray or gray.dtype!=np.uint8
                or gray.shape!=(packet.configuration.height,packet.configuration.width)):
                raise ValueError('CPU stage Raw must match admitted uint8 geometry')
            if not packet.configuration.matches(config):raise ValueError('packet/configuration mismatch')
            if self.camera_path is not None and gray.shape!=(480,752):
                raise ValueError('cached geometry requires native752x480')
            identity=data[2]
            if self._last is not None and (identity[0]!=self._last[0] or identity[1]<=self._last[1]):
                raise ValueError('CPU stage requires ordered same-epoch captures')
            raw=gray.tobytes()
            request=dict(packet=data,raw=raw,config=config_data,selection=selection_data,
                         request_id=identity[1],epoch=identity[0])
            self._start();self._sequence+=1;self.stats['requests']+=1
            started=time.perf_counter()
            reply=self._exchange((self._sequence,data,raw,config_data,selection_data),self.timeout_s)
            elapsed=time.perf_counter()-started
            if (reply.get('kind')!='result' or reply.get('sequence')!=self._sequence
                or reply.get('identity')!=identity):
                raise CPUStageError('CPU request failed or identity mismatch: '+
                    str({key:reply.get(key) for key in ('kind','sequence','identity','error_type','error')}))
            stripe=_readonly(reply['stripe']);primary=_readonly(reply['primary'])
            extra=_readonly(reply['extra'])
            self._cache=dict(request=request,stripe_data=_stripe_data(stripe),extra=extra)
            self._last=identity;self.stats['completed']+=1
            metrics=reply['metrics'];metrics.update(roundtrip_s=elapsed,
                IPC_and_parent_roundtrip_overhead_s=max(0.,elapsed-metrics['child_service_s']))
            self.stats['request_timings'].append(metrics)
            if len(self.stats['request_timings'])>512:self.stats['request_timings'].pop(0)
            return stripe,primary
        except BaseException as error:
            self._fail(error,request or (self._cache['request'] if self._cache else None))
            raise CPUStageError(str(error)) from error
        finally:self._call_lock.release()

    def geometry(self,frame,stripe,config,selection,transition):
        if not self._call_lock.acquire(blocking=False):raise CPUStageError('CPU stage already has one request')
        try:
            self._check();cache=self._cache
            if cache is None:raise ValueError('geometry cache is empty or already consumed')
            request=cache['request'];configs=_configs(config,selection)
            if (type(transition) is not bool or (frame.epoch,frame.request_id)!=(request['epoch'],request['request_id'])
                or _packet_data(frame.compact_packet)!=request['packet']
                or configs!=(request['config'],request['selection'])
                or _stripe_data(stripe)!=cache['stripe_data']
                or type(frame.raw) is not bytes or frame.raw!=request['raw']):
                raise ValueError('geometry cache frame/packet/Raw/stripe/config/policy binding mismatch')
            extra=cache['extra']
            if extra is not None:
                if (extra['display_result'].request_id!=frame.request_id
                    or extra['display_result'].capture_epoch!=frame.epoch):
                    raise ValueError('cached geometry output identity mismatch')
                extra=dict(extra,summary=dict(extra['summary'],transition=transition))
            self._cache=None;self.stats['geometry_cache_hits']+=1
            return extra
        except BaseException as error:
            self._fail(error,self._cache['request'] if self._cache else None)
            raise CPUStageError(str(error)) from error
        finally:self._call_lock.release()

    def _stop(self,*,graceful):
        process=self._process
        if process is not None and process.pid is not None:
            if graceful and process.is_alive():
                # Tiny close command; bounded just like a data exchange.
                try:self._connection.send(None)
                except (EOFError,BrokenPipeError,OSError):pass
                process.join(min(self.timeout_s,1.))
            if process.is_alive():
                process.terminate();process.join(1.)
            if process.is_alive():
                self.stats['worker_unconfirmed']=True
                raise CPUStageError('CPU child termination unconfirmed')
            self.stats['child_exitcode']=process.exitcode
        if self._connection is not None:self._connection.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(1.)
            if self._thread.is_alive():
                self.stats['IPC_thread_unconfirmed']=True
                raise CPUStageError('CPU IPC thread close unconfirmed')

    def close(self):
        if self._closed and self.stats['closed']:return dict(self.stats)
        self._closed=True
        acquired=self._call_lock.acquire(blocking=False)
        try:
            self._stop(graceful=acquired)
            self._cache=None;self.stats['closed']=True
        except CPUStageError as error:
            self.stats.update(closed=False,close_error=str(error))
        finally:
            if acquired:self._call_lock.release()
        return dict(self.stats)

    def __enter__(self):return self
    def __exit__(self,*args):self.close()


__all__=['CPUStageProcess','CPUStageError']
