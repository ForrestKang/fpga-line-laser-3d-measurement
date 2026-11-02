"""CPU-only bounded process-display close lifecycle tests; no HDMI proof."""
from collections import deque
import importlib
import json
import multiprocessing as mp
import os
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

TEMP=('D:/Workspace/CodexTemp/r33_display_close' if os.name=='nt' else os.path.join(tempfile.gettempdir(),'r33_display_close'))
os.makedirs(TEMP,exist_ok=True)


def process_module():
    dashboard=types.ModuleType('structured_light.scan3d.dashboard')
    dashboard.DashboardStatus=object
    with patch.dict('sys.modules',{'structured_light.scan3d.dashboard':dashboard}):
        return importlib.import_module('candidate.display_process_r25')


class DisplayCloseTests(unittest.TestCase):
    def make_display(self):
        module=process_module();ctx=mp.get_context('spawn')
        d=module.ProcessSessionDisplayR25.__new__(module.ProcessSessionDisplayR25)
        d._state=module._State(ctx);d._condition=threading.Condition()
        d.connection=module._Events(ctx);d._connection_closed=False
        d._closed_message=None;d._records=deque();d.owner_closed=False;d.error=None
        d._closing=False;d._released=False;d._retained_lock_fd=None
        d.submitted=d.acknowledged=d.dropped=d.peak_frames=d._count=0
        d.startup_warmup=d.startup_receipt=None;d.config={};d._allocation={}
        d.archive_slot_bytes=256;d._storage_released=False;d._storage_released_bytes=0
        return module,d

    def test_declared_budget_preserves_failure_during_diagnostic_persistence(self):
        module,d=self.make_display();entered=threading.Event();release=threading.Event();budgets=[]
        class Owner:
            def __init__(self,config):pass
            def configure(self):pass
            def close(self,timeout_s):
                budgets.append(timeout_s);entered.set()
                if not release.wait(2):raise RuntimeError('test gate timeout')
                return {'owner_closed':True,'logs_persisted':True,'owner_exited':True}
        class Renderer:
            def __init__(self,config):pass
        cv2=types.ModuleType('cv2');cv2.setNumThreads=lambda n:None;cv2.getNumThreads=lambda:1
        buffers=([bytearray(480*752) for _ in range(2)],[bytearray(256) for _ in range(2)],
                 bytearray(24),bytearray(129),[bytearray(97) for _ in range(2)])
        failure=[];exitcode=[None]
        def target():
            try:module._worker(buffers,d._state,d._condition,types.SimpleNamespace(send_bytes=d.connection.send_bytes,close=lambda:None),
                 dict(visible_points=1),None,None,Owner,Renderer)
            except BaseException as exc:failure.append(exc);exitcode[0]=1
            else:exitcode[0]=0
        thread=threading.Thread(target=target)
        class Process:
            def is_alive(self):return thread.is_alive()
            def join(self,timeout):thread.join(timeout)
            @property
            def exitcode(self):return exitcode[0]
        d.process=Process()
        with tempfile.TemporaryFile(dir=TEMP) as stream:
            descriptor=os.dup(stream.fileno());d._retained_lock_fd=descriptor
            dashboard=types.ModuleType('structured_light.scan3d.dashboard');dashboard.DashboardStatus=object
            with patch.dict('sys.modules',{'cv2':cv2,'structured_light.scan3d.dashboard':dashboard}):
                thread.start()
                self.assertTrue(d.connection.poll(1));d._drain();self.assertTrue(d.ready)
                try:
                    with self.assertRaises(module.ProcessDisplayCloseError) as caught:d.close(timeout_s=.02)
                    self.assertTrue(entered.is_set())
                    receipt=caught.exception.receipt
                    self.assertEqual(receipt['close_phase'],'owner_close_including_diagnostic_persistence')
                    self.assertFalse(receipt['cleanup_confirmed']);self.assertTrue(receipt['retained_lock_fd'])
                    self.assertEqual(receipt['close_timeout_s'],.02);os.fstat(descriptor)
                    json.dumps(receipt,allow_nan=False)
                finally:release.set();thread.join(2)
                receipt=d.close(timeout_s=.5)
            self.assertFalse(failure);self.assertTrue(receipt['cleanup_confirmed'])
            self.assertTrue(receipt['worker_joined']);self.assertEqual(receipt['worker_exitcode'],0)
            self.assertEqual(receipt['owner_close_message']['receipt']['logs_persisted'],True)
            self.assertGreater(receipt['close_stage_monotonic_ns']['owner_close_end_ns'],
                               receipt['close_stage_monotonic_ns']['owner_close_begin_ns'])
            self.assertTrue(0<=budgets[0]<=.020001,(budgets,receipt))
            with self.assertRaises(OSError):os.fstat(descriptor)

    def test_confirmed_owner_message_without_spawn_join_still_fails(self):
        module,d=self.make_display()
        class Process:
            exitcode=None
            def is_alive(self):return True
            def join(self,timeout):pass
        d.process=Process();d.owner_closed=True
        d._closed_message={'kind':'closed','owner_closed':True,'error':None,'receipt':{'logs_persisted':True}}
        with self.assertRaises(module.ProcessDisplayCloseError) as caught:d.close(timeout_s=0.)
        self.assertFalse(caught.exception.receipt['cleanup_confirmed'])
        self.assertFalse(caught.exception.receipt['worker_joined'])
        self.assertTrue(caught.exception.receipt['owner_closed'])
        self.assertFalse(d._released)


if __name__=='__main__':unittest.main()
