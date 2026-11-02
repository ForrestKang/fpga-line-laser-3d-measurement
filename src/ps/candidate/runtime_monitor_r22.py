"""Bounded Linux resource telemetry; request scheduler stop on memory/I/O limits."""
from pathlib import Path
import json
import os
import threading
import time


def linux_sample():
    values={}
    for line in Path('/proc/meminfo').read_text().splitlines():
        parts=line.replace(':','').split()
        if len(parts)>=2 and parts[0] in ('MemAvailable','MemTotal','CmaTotal','CmaFree','SwapFree','SwapTotal'):
            values[parts[0]+'_bytes']=int(parts[1])*1024
    if 'MemAvailable_bytes' not in values:raise RuntimeError('MemAvailable unavailable')
    ticks=[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    values.update(cpu_total_ticks=sum(ticks),cpu_idle_ticks=ticks[3]+ticks[4],
                  cpu_iowait_ticks=ticks[4])
    return values


class RuntimeMonitor:
    """Streaming telemetry, not an unbounded in-memory history or hardware owner."""
    def __init__(self,path,*,storage_budget,scheduler_stop,sampler=linux_sample,interval_s=.25):
        if not 0<interval_s<=1.:raise ValueError('resource sampling interval must be (0,1]')
        self.path=Path(path);self.budget=storage_budget;self.scheduler_stop=scheduler_stop
        self.sampler=sampler;self.interval_s=interval_s
        self._stop=threading.Event();self._thread=None;self._stream=None
        self._sample_lock=threading.RLock()
        self.minimum_available=None;self.first=None;self.last=None;self.samples=0
        self.error=None;self.stop_reason=None;self.telemetry_bytes=0

    def _sample_once(self):
        with self._sample_lock:
            self._sample_once_locked()

    def _sample_once_locked(self):
        values=self.sampler();available=values['MemAvailable_bytes']
        if type(available) is not int or available<0:raise ValueError('invalid memory sample')
        self.minimum_available=available if self.minimum_available is None else min(available,self.minimum_available)
        if self.first is None:self.first=values
        self.last=values;self.samples+=1
        line=(json.dumps(dict(perf_s=time.perf_counter(),**values),sort_keys=True)+'\n').encode('ascii')
        if self.telemetry_bytes+len(line)>8*1024*1024:
            raise RuntimeError('resource telemetry byte bound reached')
        token=self.budget.reserve((len(line)*5+3)//4)
        self._stream.write(line);self.telemetry_bytes+=len(line)
        self.budget.commit(token,len(line))
        if available<self.budget.limits.min_available_memory_bytes:
            self.stop_reason='memory_below_configured_budget';self.scheduler_stop.set()

    def interval_samples(self,start,end):
        """Read completed telemetry rows in a bounded interval, without new sampling.

        Flush and read share the sample lock so an in-progress JSONL row cannot
        be mistaken for corrupt telemetry. The existing 8 MiB file bound still
        applies; only selected rows are retained in memory.
        """
        with self._sample_lock:
            if self._stream is None:raise RuntimeError('monitor not started')
            if not self._stream.closed:self._stream.flush()
            result=[]
            with self.path.open('r',encoding='ascii') as stream:
                for line in stream:
                    row=json.loads(line)
                    if start<=row['perf_s']<=end:result.append(row)
            return result

    def start(self):
        if self._stream is not None:raise RuntimeError('monitor is one-shot')
        self._stream=self.path.open('xb')
        try:self._sample_once()
        except BaseException:
            self._stream.close();raise
        if self.stop_reason:
            self._stream.flush();os.fsync(self._stream.fileno());self._stream.close()
            raise RuntimeError(self.stop_reason)
        try:
            self._thread=threading.Thread(target=self._run,name='r22-resource-monitor',daemon=True)
            self._thread.start()
        except BaseException as exc:
            self.error=type(exc).__name__+': '+str(exc);self.stop_reason='resource_monitor_startup_failure'
            self.scheduler_stop.set();self._stop.set()
            if self._thread is not None and self._thread.ident is None:self._thread=None
            if self._thread is not None:self._thread.join(1.)
            if self._thread is None or not self._thread.is_alive():
                try:self._stream.flush();os.fsync(self._stream.fileno())
                finally:self._stream.close()
            raise
        return self

    def _run(self):
        try:
            while not self._stop.wait(self.interval_s):
                self._sample_once()
                if self.stop_reason:return
        except BaseException as exc:
            self.error=type(exc).__name__+': '+str(exc)
            self.stop_reason='resource_monitor_failure';self.scheduler_stop.set()

    def close(self,timeout_s=2.):
        self._stop.set()
        if self._thread is not None:self._thread.join(timeout_s)
        joined=self._thread is None or not self._thread.is_alive()
        if joined and self._stream is not None and not self._stream.closed:
            try:
                if not self.error and not self.stop_reason:self._sample_once()
                self._stream.flush();os.fsync(self._stream.fileno())
            except BaseException as exc:self.error=self.error or type(exc).__name__+': '+str(exc)
            finally:self._stream.close()
        cpu=None;iowait=None
        if self.first is not None and self.last is not None:
            total=self.last['cpu_total_ticks']-self.first['cpu_total_ticks']
            if total>0:
                cpu=100*(1-(self.last['cpu_idle_ticks']-self.first['cpu_idle_ticks'])/total)
                iowait=100*(self.last['cpu_iowait_ticks']-self.first['cpu_iowait_ticks'])/total
        return dict(worker_joined=joined,samples=self.samples,telemetry_bytes=self.telemetry_bytes,
            mem_available_min_bytes=self.minimum_available,CPU_total_capacity_pct=cpu,
            IOwait_pct=iowait,error=self.error,stop_reason=self.stop_reason,
            memory_goal_passed=self.minimum_available is not None and self.minimum_available>=self.budget.limits.min_available_memory_bytes,
            CPU_goal_passed=cpu is not None and cpu<=80.,
            scope='OS aggregate over monitor interval; stage CPU timing recorded separately')
