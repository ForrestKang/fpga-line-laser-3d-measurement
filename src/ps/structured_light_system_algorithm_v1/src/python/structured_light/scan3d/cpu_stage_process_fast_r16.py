"""Profile-directed parent result freezing for the frozen R16 spawn CPU stage.

Only select's recursive result freezer differs from the bound base source.
Spawn, worker computation, IPC, identity/cache checks and cleanup are inherited.
ARM throughput must be measured separately; this module makes no speed claim.
"""
from dataclasses import fields, is_dataclass, replace
from hashlib import sha256
from pathlib import Path
import time

import numpy as np

from . import cpu_stage_process_r16 as _base

BASE_SOURCE_SHA256 = '4b34599e2b69e52a6a3eda31f5df714a442bbb7d2f243a064014b5a671673bc0'
if sha256(Path(_base.__file__).read_bytes()).hexdigest() != BASE_SOURCE_SHA256:
    raise RuntimeError('fast CPU stage requires the frozen R16 base source')

CPUStageError = _base.CPUStageError
_PRIMITIVE_TYPES = (str, int, bool, float, bytes, type(None))


def _fast_readonly(value):
    # Reasons and summary scalars dominate recursion. Exact types cannot hide
    # dataclass/container behavior in a subclass, which must retain base rules.
    if type(value) in _PRIMITIVE_TYPES:
        return value
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            raise CPUStageError('object arrays cannot cross CPU result boundary')
        return np.frombuffer(value.tobytes(), dtype=value.dtype).reshape(value.shape)
    if is_dataclass(value):
        return replace(value, **{f.name: _fast_readonly(getattr(value, f.name))
                                 for f in fields(value) if f.init})
    if isinstance(value, dict):
        return {key: _fast_readonly(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_fast_readonly(item) for item in value)
    if isinstance(value, list):
        return [_fast_readonly(item) for item in value]
    return value


class CPUStageProcess(_base.CPUStageProcess):
    """Same bounded CPU child lifecycle; faster parent scalar dispatch only."""
    def select(self,packet,gray,config,selection):
        if not self._call_lock.acquire(blocking=False):raise CPUStageError('CPU stage already has one request')
        request=None
        try:
            self._check()
            if self.camera_path is not None and self._cache is not None:
                raise ValueError('previous geometry result has not been consumed')
            data=_base._packet_data(packet);config_data,selection_data=_base._configs(config,selection)
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
            stripe=_fast_readonly(reply['stripe']);primary=_fast_readonly(reply['primary'])
            extra=_fast_readonly(reply['extra'])
            self._cache=dict(request=request,stripe_data=_base._stripe_data(stripe),extra=extra)
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

