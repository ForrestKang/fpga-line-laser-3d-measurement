"""Bounded foreground control with a separate owner of the display allocation.

On an unconfirmed VDMA stop, the owner remains alive and retains its CMA
buffer until a later PL reset/stop actually makes the master idle. A host
timeout or foreground process exit never terminates this ownership process.
"""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import traceback
import uuid

MAX_MESSAGE = 5_000_000


def _journal_json(record):
    """Keep every diagnostic field using the compact C JSON encoder.

    This document is rewritten at every trace stage on verified tmpfs. Using
    indentation selects Python's recursive encoder for the growing history;
    whitespace has no meaning to journal readers. Keep the newline and the
    existing JSON defaults (including escaping and nonfinite handling).
    """
    return json.dumps(record, separators=(',', ':'))+'\n'


def _tmpfs_journal(path):
    """Extra per-stage diagnostics must never introduce an SD hot path."""
    try:
        resolved = Path(path).resolve()
        mounts = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields = line.split()
            separator = fields.index('-')
            mount = fields[4]
            for encoded, plain in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
                mount = mount.replace(encoded, plain)
            mount = Path(mount)
            if resolved == mount or mount in resolved.parents:
                mounts.append((len(mount.parts), fields[separator+1]))
        return bool(mounts and max(mounts)[1] == 'tmpfs')
    except (OSError, ValueError, IndexError):
        return False


class _OperationTrace:
    """Bounded ordinary-RAM diagnostics; observers cannot change RPC results."""
    def __init__(self, emit=None):
        self.emit = emit
        self.operation = None
        self.sequence = None
        self.stages = []
        self.failure = None
        self.started = time.monotonic()
        self.cpu_started = time.thread_time()
        try:
            import resource
            self.resource = resource
        except ImportError:
            self.resource = None
        self.usage_started = self._usage()

    def _usage(self):
        if self.resource is None:
            return {}
        usage = self.resource.getrusage(self.resource.RUSAGE_SELF)
        return {name: getattr(usage, name) for name in
                ('ru_minflt', 'ru_majflt', 'ru_nvcsw', 'ru_nivcsw', 'ru_utime', 'ru_stime')}

    def begin(self, operation, sequence=None):
        self.operation, self.sequence = operation, sequence
        self.stages, self.failure = [], None
        self.started = time.monotonic()
        self.cpu_started = time.thread_time()
        self.usage_started = self._usage()
        self.mark('command_received')

    def mark(self, stage, **values):
        try:
            now = time.monotonic()
            usage = self._usage()
            self.stages.append(dict(stage=stage, monotonic_s=now,
                elapsed_s=now-self.started, thread_cpu_s=time.thread_time()-self.cpu_started,
                usage_delta={name: value-self.usage_started.get(name, value)
                             for name, value in usage.items()}, **values))
            del self.stages[:-24]
            if self.emit is not None:
                self.emit()
        except Exception:
            # Diagnostics must not replace a display/transport error or skip
            # independent halt-before-free ownership handling.
            pass

    def failed(self, exc):
        self.failure = dict(type=type(exc).__name__, message=str(exc),
            stage=self.stages[-1]['stage'] if self.stages else None,
            elapsed_s=time.monotonic()-self.started,
            traceback=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    def snapshot(self):
        return dict(operation=self.operation, canvas_sequence=self.sequence,
                    stages=list(self.stages), failure=self.failure)


def receive_exact(sock, n):
    data = bytearray()
    while len(data) < n:
        block = sock.recv(n-len(data))
        if not block:
            raise EOFError('HDMI owner connection closed')
        data.extend(block)
    return bytes(data)


def receive(sock):
    length = struct.unpack('!I', receive_exact(sock,4))[0]
    if not 0 < length <= MAX_MESSAGE:
        raise ValueError('invalid HDMI IPC length')
    return json.loads(receive_exact(sock,length))


def send(sock, value):
    raw = json.dumps(value, separators=(',', ':')).encode()
    if len(raw) > MAX_MESSAGE:
        raise ValueError('HDMI IPC message exceeds bound')
    sock.sendall(struct.pack('!I', len(raw))+raw)


def artifact_paths(manifest_path, record):
    """The compatibility mode and the physical artifact basename differ.

    The motion combined design uses the structured-light display mode but
    retains structured_light_motion_profile_hdmi.bit/hwh on disk.
    """
    mode=record.get('overlay_mode',record.get('overlay'))
    if mode not in {'camera_raw_hdmi','structured_light_profile_hdmi'} or not record.get('preboard_passed'):
        raise RuntimeError('signed combined Overlay required by display owner')
    # The compatibility mode describes the hardware interface, not the name
    # of a rebuilt bitstream. Follow the manifest's explicitly hashed files;
    # deriving an old basename silently breaks admitted R28 attachments.
    files=record.get('files',{})
    if files:
        paths={}
        for kind in ('bit','hwh'):
            entry=files.get(kind,{})
            if not isinstance(entry,dict):
                raise RuntimeError('incomplete HDMI artifact identity')
            value=entry.get('path')
            if not isinstance(value,str) or not value or not entry.get('sha256'):
                raise RuntimeError('incomplete HDMI artifact identity')
            path=Path(value)
            paths[kind]=path if path.is_absolute() else Path(manifest_path).parent/path
        return paths
    stem=record.get('overlay',mode)
    if stem not in {'camera_raw_hdmi','structured_light_profile_hdmi','structured_light_motion_profile_hdmi'}:
        raise RuntimeError('unsupported HDMI artifact basename')
    return {kind:Path(manifest_path).parent/(stem+'.'+kind) for kind in ('bit','hwh')}


def owner_environment(*, preview_only=False):
    """Pass bundled algorithm and geometry imports to the independent owner."""
    environment = dict(os.environ)
    if preview_only:environment['SL_HDMI_PREVIEW_FAST']='1'
    algorithm_python = Path(__file__).resolve().parents[2]
    geometry_python = (algorithm_python.parents[2]/'structured_light_system_geometry_v1'
                       /'src'/'python')
    paths = [str(algorithm_python)]
    if geometry_python.is_dir():
        paths.append(str(geometry_python))
    existing = environment.get('PYTHONPATH', '')
    if existing:
        paths.append(existing)
    environment['PYTHONPATH'] = os.pathsep.join(paths)
    return environment


class RemoteHDMIOutput:
    def __init__(self, manifest_path, *, owner_log=None, timeout_s=3., startup_timeout_s=30.,preview_only=False,
                 hardware_lock_fd=None, shared_canvas=False, shared_scene=False, scene_raster_manifest=None, scene_sources=('replay',)):
        if os.name != 'posix' or not hasattr(socket, 'AF_UNIX'):
            raise RuntimeError('real HDMI owner requires Linux PYNQ')
        self.timeout_s = timeout_s
        if preview_only:startup_timeout_s=60.
        if not 1 <= startup_timeout_s <= 60:
            raise ValueError('HDMI owner startup timeout must be 1..60 seconds')
        if type(shared_canvas) is not bool:
            raise ValueError('shared_canvas must be an explicit boolean')
        if (type(shared_scene) is not bool or (shared_scene and (shared_canvas or scene_raster_manifest is None))
                or (not shared_scene and scene_raster_manifest is not None)):
            raise ValueError('shared_scene requires exclusive scene transport and explicit native manifest')
        from structured_light.p2.shared_scene import check_sources
        scene_sources=check_sources(scene_sources)
        if not shared_scene and scene_sources!=('replay',):
            raise ValueError('scene sources require explicit shared scene transport')
        self.shared_canvas = None
        self.shared_scene = None
        self._scene_unconfirmed_leases=[]
        self.socket_path = '/tmp/sl_hdmi_'+uuid.uuid4().hex+'.sock'
        self.owner_log = Path(owner_log or Path(manifest_path).parent/'hdmi_owner.log')
        self.owner_log.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX)
        # Importing PYNQ and parsing a complete HWH on the ARM board can take
        # more than eight seconds. Startup has its own finite budget; ordinary
        # hardware RPC deadlines remain short and unchanged.
        listener.settimeout(startup_timeout_s)
        listener.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        listener.listen(1)
        environment = owner_environment(preview_only=preview_only)
        inherited_fds=()
        if hardware_lock_fd is not None:
            if type(hardware_lock_fd) is not int or hardware_lock_fd < 0:
                raise ValueError('valid acquired hardware lock descriptor required')
            os.fstat(hardware_lock_fd)
            inherited_fds=(hardware_lock_fd,)
        command = [sys.executable, str(Path(__file__).resolve()), '--serve', self.socket_path,
                   str(Path(manifest_path).resolve()), str(self.owner_log)+'.json']
        try:
            if shared_canvas:
                from structured_light.p2.shared_canvas import SharedCanvasWriter
                self.shared_canvas = SharedCanvasWriter.create()
                inherited_fds += (self.shared_canvas.fd,)
                command += ['--shared-canvas-fd', str(self.shared_canvas.fd)]
            if shared_scene:
                from structured_light.p2.shared_scene import SharedSceneWriter
                self.shared_scene=(SharedSceneWriter.create() if scene_sources==('replay',)
                    else SharedSceneWriter.create(supported_sources=scene_sources))
                inherited_fds+=(self.shared_scene.fd,)
                command+=['--shared-scene-fd',str(self.shared_scene.fd),
                    '--scene-raster-manifest',str(Path(scene_raster_manifest).resolve())]
                if scene_sources!=('replay',):command+=['--scene-sources',','.join(scene_sources)]
            with self.owner_log.open('ab') as log:
                self.process = subprocess.Popen(command,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    env=environment, start_new_session=True, pass_fds=inherited_fds)
        except BaseException:
            self._drop_shared_canvas()
            self._drop_shared_scene()
            listener.close()
            Path(self.socket_path).unlink(missing_ok=True)
            raise
        self.owner_pid = self.process.pid
        self._close_acknowledged = False
        self.preview_pending = False
        self.connection = None
        try:
            self.connection, _ = listener.accept()
            self.connection.settimeout(startup_timeout_s if self.shared_scene is not None else self.timeout_s)
            ready = receive(self.connection)
            if not ready.get('ready'):
                raise RuntimeError('HDMI owner initialization failed: '+str(ready))
            if self.shared_canvas is not None:
                from structured_light.p2.shared_canvas import check_descriptor
                check_descriptor(ready.get('shared_canvas'))
            if self.shared_scene is not None:
                from structured_light.p2.shared_scene import check_descriptor
                from structured_light.scan3d.scene_renderer import check_scene_capability
                check_descriptor(ready.get('shared_scene'))
                check_scene_capability(ready.get('scene_renderer'),scene_raster_manifest,supported_sources=scene_sources)
                self.scene_capability=ready['scene_renderer']
                self.connection.settimeout(self.timeout_s)
        except Exception:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            self._drop_shared_canvas()
            self._drop_shared_scene()
            raise
        finally:
            listener.close()
            Path(self.socket_path).unlink(missing_ok=True)
        self.dma = self

    def _drop_shared_canvas(self):
        shared = getattr(self, 'shared_canvas', None)
        self.shared_canvas = None
        if shared is not None:
            shared.close()

    def _drop_shared_scene(self):
        shared=getattr(self,'shared_scene',None)
        self.shared_scene=None
        if shared is not None:shared.close()

    def _rpc(self, operation, *, binary=None, **values):
        if self.connection is None:
            raise RuntimeError('HDMI owner connection unavailable')
        if self.preview_pending:
            raise RuntimeError('finish the queued preview before another HDMI command')
        scene=getattr(self,'shared_scene',None)
        if scene is not None and scene.pending is not None and operation!='render_scene':
            raise RuntimeError('finish pending scene before another HDMI command')
        started = time.monotonic()
        rpc_phase = 'send_command'
        try:
            send(self.connection, dict(operation=operation, **values))
            if binary is not None:
                rpc_phase = 'send_binary'
                self.connection.sendall(binary)
            rpc_phase = 'receive_response'
            result = receive(self.connection)
        except Exception as exc:
            # Preserve the actual exception object/cause. The journal wrapper
            # can persist this context on failure without per-frame SD writes.
            try:
                exc.hdmi_rpc = dict(operation=operation, phase=rpc_phase,
                    canvas_sequence=values.get('sequence'), timeout_s=self.timeout_s,
                    elapsed_s=time.monotonic()-started)
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception as close_error:
                try:exc.hdmi_rpc['connection_close_error'] = repr(close_error)
                except Exception:pass
            finally:
                self.connection = None
            raise
        if not result.get('ok'):
            error=RuntimeError('HDMI owner error: '+str(result))
            if isinstance(result.get('scene_receipt'),dict):error.scene_receipt=result['scene_receipt']
            if isinstance(result.get('owner_diagnostics'),dict):error.hdmi_owner_diagnostics=result['owner_diagnostics']
            raise error
        return result.get('result')

    def configure(self): return self._rpc('configure')
    def show(self, raw_bgr):
        return self._rpc('show', bgr=base64.b64encode(raw_bgr).decode('ascii'))
    def show_canvas(self, canvas):
        """Submit a fixed native BGR canvas through bounded binary IPC."""
        if getattr(self,'shared_scene',None) is not None:
            raise RuntimeError('scene owner does not accept foreground canvases')
        import numpy as np
        if (not isinstance(canvas,np.ndarray) or canvas.shape!=(720,1280,3)
            or canvas.dtype!=np.uint8 or not canvas.flags.c_contiguous):
            raise ValueError('canvas must be contiguous 1280x720 BGR uint8')
        shared = getattr(self, 'shared_canvas', None)
        if shared is not None:
            if self.connection is None or self.preview_pending:
                raise RuntimeError('HDMI shared canvas owner unavailable or preview pending')
            try:
                started = time.monotonic()
                command = shared.prepare(canvas)
                prepared = time.monotonic()
                result = self._rpc('shared_canvas', **command)
                shared.acknowledge(result)
                completed = time.monotonic()
                return dict(result, canvas_prepare_s=prepared-started,
                    canvas_rpc_s=completed-prepared, canvas_submit_s=completed-started)
            except BaseException:
                if self.connection is not None:
                    self.connection.close()
                    self.connection = None
                self._drop_shared_canvas()
                raise
        return self._rpc('canvas',binary=memoryview(canvas).cast('B'),canvas_size=1280*720*3)

    def render_and_show(self, renderer, gray, points, options):
        """Reuse the shared transport's canvas without a per-frame BGR copy.

        Rendering is synchronous and ends before publication. The normal owner
        still fills/flushes its independent CMA store and confirms the same ACK.
        """
        shared=getattr(self,'shared_canvas',None)
        if shared is None:
            started=time.perf_counter();cpu=time.thread_time()
            canvas=renderer.render(gray,points,**options)
            render_s=time.perf_counter()-started;render_cpu=time.thread_time()-cpu
            result=self.show_canvas(canvas)
            return dict(result,render_direct=False,render_s=render_s,render_thread_cpu_s=render_cpu)
        if self.connection is None or self.preview_pending:
            raise RuntimeError('HDMI shared canvas owner unavailable or preview pending')
        original=renderer.canvas
        timings={}
        def render_into(pixels):
            rendered=None
            renderer.canvas=pixels
            started=time.perf_counter();cpu=time.thread_time()
            try:
                rendered=renderer.render(gray,points,**options)
                if rendered is not pixels:
                    raise ValueError('direct renderer must return its borrowed canvas')
                timings.update(render_s=time.perf_counter()-started,
                               render_thread_cpu_s=time.thread_time()-cpu)
            finally:
                renderer.canvas=original
                # Exceptions retain traceback frames. Clear both borrowed
                # references before mapping cleanup, including render failure.
                rendered=None
                del pixels
        try:
            started=time.monotonic()
            initial_copy=not getattr(shared,'_render_initialized',False)
            command=shared.prepare_render(render_into,original)
            prepared=time.monotonic()
            result=self._rpc('shared_canvas',**command)
            shared.acknowledge(result)
            completed=time.monotonic()
            return dict(result,render_direct=True,canvas_initial_copy=initial_copy,**timings,
                canvas_prepare_s=max(0.,prepared-started-timings['render_s']),
                canvas_rpc_s=completed-prepared,canvas_submit_s=completed-started)
        except BaseException as exc:
            cleanup_errors=[]
            if self.connection is not None:
                try:self.connection.close()
                except Exception as cleanup:cleanup_errors.append(repr(cleanup))
                finally:self.connection=None
            try:self._drop_shared_canvas()
            except Exception as cleanup:cleanup_errors.append(repr(cleanup))
            if cleanup_errors:
                try:exc.hdmi_direct_cleanup_errors=cleanup_errors
                except Exception:pass
            raise

    def show_scene(self,raw,points,metadata):
        shared=getattr(self,'shared_scene',None)
        if shared is None:raise RuntimeError('explicit shared scene owner required')
        started=time.monotonic();command=shared.prepare(raw,points,metadata)
        prepared=time.monotonic()
        result=None
        try:
            result=self._rpc('render_scene',**command)
            shared.acknowledge(result)
            return dict(result,scene_prepare_s=prepared-started,scene_rpc_s=time.monotonic()-prepared)
        except BaseException as exc:
            if (isinstance(result,dict) and result.get('rendered') is True
                    and result.get('scene_sequence')==command['sequence']
                    and result.get('scene_metadata_sha256')==command['metadata_sha256']):
                exc.scene_receipt=result
            if shared.pending is not None:self._scene_unconfirmed_leases.append(shared)
            if self.connection is not None:self.connection.close();self.connection=None
            self._drop_shared_scene()
            raise

    def fetch_scene_canvas(self,last_receipt):
        import numpy as np
        try:
            result=self._rpc('scene_snapshot')
            if (result.get('canvas_size')!=1280*720*3
                    or result.get('scene_sequence')!=last_receipt.get('scene_sequence')
                    or result.get('scene_metadata_sha256')!=last_receipt.get('scene_metadata_sha256')):
                raise ValueError('final scene snapshot identity mismatch')
            pixels=receive_exact(self.connection,result['canvas_size'])
            self.scene_final_snapshot_metadata=result
            return np.frombuffer(pixels,np.uint8).reshape(720,1280,3)
        except BaseException:
            if self.connection is not None:self.connection.close();self.connection=None
            raise
    def show_preview(self, gray, width, height, **metadata):
        self.submit_preview(gray,width,height,**metadata)
        return self.finish_preview()
    def submit_preview(self, gray, width, height, **metadata):
        if getattr(self,'shared_scene',None) is not None:
            raise RuntimeError('scene owner does not accept preview frames')
        if self.connection is None or self.preview_pending:
            raise RuntimeError('HDMI preview owner unavailable or already has a queued frame')
        try:
            send(self.connection,dict(operation='preview',gray_size=len(gray),
                                     width=width,height=height,metadata=metadata))
            self.connection.sendall(gray)
            self.preview_pending = True
        except Exception:
            self.connection.close();self.connection=None
            raise
    def finish_preview(self):
        if self.connection is None or not self.preview_pending:
            raise RuntimeError('no queued preview to acknowledge')
        try:
            result=receive(self.connection)
            self.preview_pending=False
        except Exception:
            self.connection.close();self.connection=None
            raise
        if not result.get('ok'):
            raise RuntimeError('HDMI preview owner error: '+str(result))
        return result.get('result')
    def read(self, offset):
        if offset != 4:
            raise ValueError('only VDMA status is exposed through display IPC')
        return self._rpc('status')
    def close(self):
        error = None
        try:
            if self.connection is not None:
                if self.preview_pending:self.finish_preview()
                self._rpc('close')
                self._close_acknowledged = True
        except BaseException as exc:
            error = exc
        finally:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            # Ordinary mapped RAM can be released independently. The child
            # retains its own fd/mapping and all DMA/CMA/lock ownership.
            self._drop_shared_canvas()
            self._drop_shared_scene()
        # Never terminate an owner that may still hold DMA memory or the
        # inherited hardware lock. A timeout is an unconfirmed close, even
        # when the IPC close command was acknowledged just before it.
        owner_pid = getattr(self, 'owner_pid', getattr(self.process, 'pid', None))
        try:
            # PYNQ interpreter shutdown can outlast the confirmed DMA stop;
            # keep the finite owner-exit budget separate from a frame RPC.
            returncode = self.process.wait(timeout=5.)
        except subprocess.TimeoutExpired as exc:
            detail = '' if error is None else '; close RPC failed: '+str(error)
            raise RuntimeError(f'HDMI owner {owner_pid} still running; close unconfirmed; '
                               'retain hardware lock and allocations'+detail) from exc
        if error is not None:
            raise RuntimeError(f'HDMI owner {owner_pid} close failed: {error}') from error
        if returncode != 0 or not getattr(self, '_close_acknowledged', False):
            raise RuntimeError(f'HDMI owner {owner_pid} exited with code {returncode} '
                               'without confirmed successful display close')
        getattr(self,'_scene_unconfirmed_leases',[]).clear()
        return dict(closed_confirmed=True, owner_exited=True, owner_pid=owner_pid,
                    returncode=returncode)


def serve(socket_path, manifest_path, journal, shared_canvas_fd=None,
          shared_scene_fd=None,scene_raster_manifest=None,scene_sources=('replay',)):
    # MMIO bus aborts terminate the ARM process before Python can catch them.
    # Preserve their actual stack in the existing owner stderr log.
    import faulthandler
    faulthandler.enable()
    shared_canvas = None
    shared_scene = scene_renderer = None
    if shared_canvas_fd is not None:
        from structured_light.p2.shared_canvas import SharedCanvasReader
        shared_canvas = SharedCanvasReader(shared_canvas_fd)
    if shared_scene_fd is not None:
        from structured_light.p2.shared_scene import SharedSceneReader
        shared_scene=SharedSceneReader(shared_scene_fd,supported_sources=scene_sources)
    from pynq import Overlay, PL
    from structured_light.p2.hdmi_output import create_output
    manifest_path = Path(manifest_path)
    record = json.loads(manifest_path.read_text())
    artifacts=artifact_paths(manifest_path,record)
    import hashlib
    for kind in ('bit','hwh'):
        artifact = artifacts[kind]
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != record['files'][kind]['sha256']:
            raise RuntimeError('HDMI owner artifact readback mismatch')
    active = Path(PL.bitfile_name) if PL.bitfile_name else None
    if active is None or not active.is_file() or hashlib.sha256(active.read_bytes()).hexdigest() != record['files']['bit']['sha256']:
        raise RuntimeError('HDMI owner refused: active PL Overlay does not match the signed combined bitstream')
    # Parse metadata without reprogramming the already running camera Overlay.
    overlay = Overlay(str(artifacts['bit']), download=False)
    if os.environ.get('SL_HDMI_PROCESSED_CANVAS') == '1':
        from structured_light.scan3d.canvas_output import create_canvas_output
        output = create_canvas_output(overlay, manifest_path)
    else:
        output = create_output(overlay, manifest_path)
    if os.environ.get('SL_HDMI_PREVIEW_FAST')=='1':
        # Import the optional native blitter during the separate, bounded
        # startup budget, before the first ordinary frame RPC begins.
        from structured_light.p2.preview_output import prepare_preview_backend
        prepare_preview_backend()
    channel = socket.socket(socket.AF_UNIX)
    channel.settimeout(8.)
    channel.connect(socket_path)
    channel.settimeout(None)
    trace = None
    trace_to_ram = _tmpfs_journal(journal)
    current_phase = ['starting']
    def status(phase, **extra):
        # A journal I/O failure must never unwind ownership of live CMA memory.
        try:
            current_phase[0] = phase
            record = dict(pid=os.getpid(), phase=phase, **extra)
            if trace_to_ram and trace is not None:
                record['operation_diagnostics'] = trace.snapshot()
            Path(journal).write_text(_journal_json(record))
        except OSError as exc:
            print('HDMI owner journal failure: '+str(exc), flush=True)
    if trace_to_ram:
        trace = _OperationTrace()
        trace.emit = lambda: status(current_phase[0], operation=trace.operation)
        # The optional observer only writes the already verified tmpfs journal.
        # It does not add MMIO reads, alter frame stores or change deadlines.
        output._diagnostic_stage = trace.mark
    error = None
    try:
        if shared_scene is not None:
            from structured_light.scan3d.scene_renderer import SceneRenderer
            from structured_light.scan3d.canvas_output import PersistentCanvasOutput
            if not isinstance(output,PersistentCanvasOutput):
                raise RuntimeError('scene requires processed canvas owner factory')
            scene_renderer=SceneRenderer(output,scene_raster_manifest,supported_sources=scene_sources)
        ready = {'ready': True}
        if shared_canvas is not None:
            from structured_light.p2.shared_canvas import descriptor
            ready['shared_canvas'] = descriptor()
        if shared_scene is not None:
            from structured_light.p2.shared_scene import descriptor
            ready['shared_scene']=descriptor();ready['scene_renderer']=scene_renderer.capability()
        send(channel, ready)
        status('ready')
        while True:
            command = receive(channel)
            operation = command['operation']
            if trace is not None:
                trace.begin(operation, command.get('sequence'))
            status('executing', operation=operation)
            if shared_scene is not None and operation in ('show','canvas','shared_canvas','preview'):
                raise ValueError('scene owner refuses mixed presentation modes')
            if operation == 'configure': result = output.configure()
            elif operation == 'show': result = output.show(base64.b64decode(command['bgr'],validate=True))
            elif operation == 'canvas':
                import numpy as np
                from structured_light.scan3d.canvas_output import PersistentCanvasOutput
                if (type(command.get('canvas_size')) is not int
                    or command['canvas_size'] != 1280*720*3):
                    raise ValueError('invalid bounded BGR canvas IPC payload')
                if not isinstance(output,PersistentCanvasOutput):
                    raise RuntimeError('canvas RPC requires the processed canvas owner factory')
                pixels = receive_exact(channel,command['canvas_size'])
                result = output.show_canvas(np.frombuffer(pixels,np.uint8).reshape(720,1280,3))
            elif operation == 'shared_canvas':
                from structured_light.scan3d.canvas_output import PersistentCanvasOutput
                if shared_canvas is None or not isinstance(output,PersistentCanvasOutput):
                    raise RuntimeError('shared canvas requires its explicit processed owner transport')
                if trace is not None:trace.mark('shared_canvas_present_begin')
                result = shared_canvas.present(command, output)
                if trace is not None:trace.mark('shared_canvas_present_complete')
            elif operation == 'render_scene':
                from structured_light.scan3d.canvas_output import PersistentCanvasOutput
                if shared_scene is None or not isinstance(output,PersistentCanvasOutput):
                    raise RuntimeError('render_scene requires its explicit processed owner transport')
                result=shared_scene.present(command,scene_renderer)
            elif operation == 'scene_snapshot':
                if shared_scene is None or shared_scene.last_result is None:
                    raise RuntimeError('no acknowledged scene to snapshot')
                canvas,metadata=scene_renderer.snapshot()
                result=dict(canvas_size=1280*720*3,scene_sequence=shared_scene.sequence,
                    scene_metadata_sha256=shared_scene.last_result['scene_metadata_sha256'],
                    native=scene_renderer.renderer.point_raster.summary())
                send(channel,{'ok':True,'result':result})
                channel.sendall(memoryview(canvas).cast('B'))
                continue
            elif operation == 'preview':
                from structured_light.p2.preview_output import PreviewHDMIOutput
                size = command['gray_size']
                if type(size) is not int or not 0 < size <= 752*480 or size != command['width']*command['height']:
                    raise ValueError('invalid bounded gray IPC payload')
                pixels = receive_exact(channel,size)
                if not isinstance(output,PreviewHDMIOutput):
                    output.close()
                    output = PreviewHDMIOutput(output.clock,output.vtc,output.dma,
                                               output.allocator,timeout_s=output.timeout_s)
                result = output.show_preview(pixels,command['width'],command['height'],**command['metadata'])
            elif operation == 'status': result = int(output.dma.read(4))
            elif operation == 'close':
                output.close()
                status('closed_confirmed', buffer_released=True)
                send(channel, {'ok':True,'result':None})
                return
            else: raise ValueError('unknown HDMI owner operation')
            status('active' if output.buffer is not None else 'ready')
            if trace is not None:trace.mark('ack_send_begin')
            send(channel, {'ok':True,'result':result})
            if trace is not None:trace.mark('ack_send_complete')
    except Exception as exc:
        error = type(exc).__name__+': '+str(exc)
        if trace is not None:
            trace.failed(exc)
            status('operation_error', error=error)
        partial=getattr(exc,'receipt',None)
        try: send(channel, {'ok':False,'error':error,'owner_pid':os.getpid(),
            **({'owner_diagnostics':trace.snapshot()} if trace is not None else {}),
            **({'scene_receipt':partial} if isinstance(partial,dict) else {})})
        except Exception: pass
    finally:
        channel.close()
        if shared_canvas is not None:
            try:
                shared_canvas.close()
            except Exception as exc:
                # An ordinary-RAM cleanup failure must not skip the existing
                # independent DMA stop/quarantine procedure below.
                print('HDMI shared canvas cleanup failure: '+str(exc),flush=True)
        if shared_scene is not None:
            try:shared_scene.close()
            except Exception as exc:print('HDMI shared scene cleanup failure: '+str(exc),flush=True)
        if scene_renderer is not None:
            try:scene_renderer.close()
            except Exception as exc:print('HDMI scene renderer cleanup failure: '+str(exc),flush=True)
        try:
            output.close()
            if error is not None:
                status('released_after_error',error=error,buffer_released=True,
                       stop_confirmed=True)
        except Exception as exc:
            if output.buffer is None:
                status('stop_failed_without_allocation',error=error,stop_error=str(exc))
                return
            # The process, not only a module-level reference in the foreground,
            # retains the allocation. PynqBuffer has a freeing __del__.
            status('quarantined', error=error, stop_error=str(exc),
                   buffer_released=False, physical_address=int(output.buffer.physical_address))
            while True:
                time.sleep(.5)
                try:
                    if int(output.dma.read(4)) & 1 and not int(output.dma.read(0)) & 1:
                        output.close()
                        status('released_after_confirmed_idle',buffer_released=True)
                        break
                except Exception: pass


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('socket_path');parser.add_argument('manifest_path');parser.add_argument('journal')
    parser.add_argument('--shared-canvas-fd',type=int)
    parser.add_argument('--shared-scene-fd',type=int)
    parser.add_argument('--scene-raster-manifest')
    parser.add_argument('--scene-sources',default='replay')
    args=parser.parse_args()
    if not args.serve:
        raise SystemExit('Internal HDMI allocation owner; use the board acceptance entry')
    serve(args.socket_path,args.manifest_path,args.journal,
        shared_canvas_fd=args.shared_canvas_fd,shared_scene_fd=args.shared_scene_fd,
        scene_raster_manifest=args.scene_raster_manifest,scene_sources=tuple(args.scene_sources.split(',')))
