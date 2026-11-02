"""RAM-only status coordination; never reads files or consumes model ACKs."""
import threading

class PipelineStatusR30:
    def __init__(self,display,writer):
        self.display=display;self.writer=writer;self.computed=-1;self.phase=0
        self.error=None;self.updates=0;self._stop=threading.Event()
        self._thread=threading.Thread(target=self._run,name='r30-pipeline-status',daemon=True)
        self._thread.start()
    def note(self,index):self.computed=index
    def set_phase(self,phase):self.phase=phase
    def _run(self):
        previous=None
        try:
            while not self._stop.is_set():
                with self.writer._condition:
                    pending=self.writer._accepted-self.writer._written
                    size=self.writer._pending_bytes_locked()
                    durable=self.writer._written-1
                    phase=(5 if self.writer._worker_error else 2 if self.writer.admission_paused
                           else self.phase if self.phase else 1 if pending>8 else 0)
                state=(self.computed,durable,pending,size,phase)
                if state!=previous:
                    self.display.update_pipeline_status(computed_frame=state[0],durable_frame=state[1],
                        queued_frames=state[2],queued_bytes=state[3],phase=state[4])
                    previous=state;self.updates+=1
                self._stop.wait(.25)
        except BaseException as exc:self.error=type(exc).__name__+': '+str(exc)
    def close(self):
        self._stop.set();self._thread.join(1.)
        if self._thread.is_alive():raise RuntimeError('status coordinator did not join')
        if self.error:raise RuntimeError('status coordinator failed: '+self.error)
        return dict(updates=self.updates,joined=True,does_not_count_as_model_update=True)
