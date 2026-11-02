"""CPU-only cold canvas deadline tests. No HDMI or board proof."""
import importlib
import json
import multiprocessing as mp
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

from virtual_scan.journal_owner_r20g import DeadlineRemoteHDMIOutput, DeadlineJournalHDMIOwner
from structured_light.p2.hdmi_owner import RemoteHDMIOutput
from tools.board_replay_r22 import ReplayOwner


def display_module():
    dashboard=types.ModuleType('structured_light.scan3d.dashboard')
    dashboard.DashboardStatus=lambda **kwargs:kwargs
    with patch.dict('sys.modules',{'structured_light.scan3d.dashboard':dashboard}):
        return importlib.import_module('candidate.display_process_r25')


class Socket:
    def __init__(self):self.timeout=5.;self.budgets=[]
    def gettimeout(self):return self.timeout
    def settimeout(self,value):self.timeout=value;self.budgets.append(value)


class StartupCanvasTests(unittest.TestCase):
    def remote(self):
        owner=DeadlineRemoteHDMIOutput.__new__(DeadlineRemoteHDMIOutput)
        owner.connection=Socket();owner.timeout_s=5.
        return owner

    def test_cold_budget_once_then_steady_five_seconds(self):
        owner=self.remote();observed=[];canvas=object()
        def present(instance,pixels):
            self.assertIs(pixels,canvas)
            observed.append((instance.timeout_s,instance.connection.gettimeout()))
            return {'hdmi_acknowledged':True}
        with patch.object(RemoteHDMIOutput,'show_canvas',present):
            owner.show_canvas_startup(canvas)
            owner.show_canvas(canvas)
            with self.assertRaisesRegex(RuntimeError,'only once'):owner.show_canvas_startup(canvas)
        self.assertEqual(observed,[(20.,20.),(5.,5.)])
        self.assertEqual(owner.connection.budgets,[20.,5.])

    def test_failure_restores_steady_timeout_without_retry(self):
        owner=self.remote();calls=[]
        def present(instance,pixels):
            calls.append(instance.timeout_s)
            raise TimeoutError('cold missing ACK')
        with patch.object(RemoteHDMIOutput,'show_canvas',present):
            with self.assertRaisesRegex(TimeoutError,'missing ACK'):owner.show_canvas_startup(None,10.)
            with self.assertRaisesRegex(RuntimeError,'only once'):owner.show_canvas_startup(None)
        self.assertEqual(calls,[10.]);self.assertEqual(owner.timeout_s,5.)
        self.assertEqual(owner.connection.gettimeout(),5.)
        self.assertFalse(getattr(owner,'_close_acknowledged',False))

    def test_failed_rpc_closed_socket_is_not_reopened(self):
        owner=self.remote();connection=owner.connection
        def present(instance,pixels):instance.connection=None;raise TimeoutError('closed')
        with patch.object(RemoteHDMIOutput,'show_canvas',present):
            with self.assertRaises(TimeoutError):owner.show_canvas_startup(None)
        self.assertIsNone(owner.connection);self.assertEqual(owner.timeout_s,5.)
        self.assertEqual(connection.budgets,[20.])

    def test_effective_remaining_budget_can_be_below_five(self):
        owner=self.remote()
        with patch.object(RemoteHDMIOutput,'show_canvas',return_value={'ok':True}):
            owner.show_canvas_startup(None,.25)
        self.assertEqual(owner.connection.budgets,[.25,5.])

    def test_invalid_remote_budget_does_not_consume_attempt(self):
        for budget in (True,False,0.,-1.,31.,float('nan'),float('inf'),'20'):
            with self.subTest(budget=budget):
                owner=self.remote()
                with self.assertRaises((ValueError,TypeError)):owner.show_canvas_startup(None,budget)
                self.assertFalse(getattr(owner,'_startup_canvas_attempted',False))

    def test_parent_deadline_caps_cold_budget(self):
        module=display_module()
        with patch.object(module.time,'monotonic',return_value=100.):
            self.assertEqual(module.startup_canvas_budget({}),20.)
            self.assertEqual(module.startup_canvas_budget({'display_startup_canvas_timeout_s':30.,
                'display_startup_deadline_monotonic':107.}),7.)
            with self.assertRaises(TimeoutError):module.startup_canvas_budget({'display_startup_deadline_monotonic':99.})
        for budget in (True,4.9,30.1,float('nan'),float('inf'),'20'):
            with self.subTest(budget=budget),self.assertRaises(ValueError):
                module.startup_canvas_budget({'display_startup_canvas_timeout_s':budget})

    def test_journal_proxy_retains_original_failure_recording(self):
        owner=DeadlineJournalHDMIOwner.__new__(DeadlineJournalHDMIOwner)
        owner._close_receipt=None
        class Remote:
            def show_canvas_startup(self,canvas,budget):raise TimeoutError('startup timeout')
        owner.owner=Remote();saved=[]
        owner._persist=lambda phase,final:saved.append((phase,final)) or {'logs_persisted':True}
        owner._record_original_error=lambda receipt,error:saved.append(type(error).__name__)
        owner._failure_message=lambda phase,receipt:'recorded '+phase
        with self.assertRaisesRegex(RuntimeError,'recorded show_canvas_startup'):
            owner.show_canvas_startup(None,20.)
        self.assertEqual(saved,[('show_canvas_startup_error',False),'TimeoutError'])

    def test_replay_owner_still_requires_real_vdma_ack(self):
        owner=ReplayOwner.__new__(ReplayOwner);budgets=[]
        owner.owner=types.SimpleNamespace(show_canvas_startup=lambda canvas,timeout_s:
            budgets.append(timeout_s) or {'hdmi_acknowledged':True})
        with self.assertRaisesRegex(RuntimeError,'VDMA confirmation'):owner.show_canvas_startup(None)
        receipt={'hdmi_acknowledged':True,'acknowledgement_basis':
            'VDMA frame event and active park store; not optical proof','active_store':1,'update_count':1}
        owner.owner.show_canvas_startup=lambda canvas,timeout_s:receipt
        self.assertIs(owner.show_canvas_startup(None),receipt);self.assertEqual(budgets,[20.])

    def test_worker_uses_startup_api_before_ready_and_excludes_warmup(self):
        module=display_module();ctx=mp.get_context('spawn');state=module._State(ctx)
        state['closing']=1;condition=threading.Condition();calls=[];events=[]
        class Owner:
            def __init__(self,config):pass
            def configure(self):calls.append('configure')
            def show_canvas(self,canvas):raise AssertionError('ordinary canvas used for startup')
            def show_canvas_startup(self,canvas,timeout_s):
                calls.append(('startup',timeout_s));return {'hdmi_acknowledged':True}
            def close(self,timeout_s):calls.append('close');return {'owner_closed':True}
        class Renderer:
            def __init__(self,config):pass
            def render(self,*args,**kwargs):return np.zeros((720,1280,3),np.uint8)
        cv2=types.ModuleType('cv2');cv2.setNumThreads=lambda n:None;cv2.getNumThreads=lambda:1
        dashboard=types.ModuleType('structured_light.scan3d.dashboard');dashboard.DashboardStatus=lambda **kw:kw
        connection=types.SimpleNamespace(send_bytes=lambda data:events.append(json.loads(data)),close=lambda:None)
        buffers=([bytearray(480*752) for _ in range(2)],[bytearray(256) for _ in range(2)],
            bytearray(24),bytearray(129),[bytearray(97) for _ in range(2)])
        config=dict(visible_points=1,warmup=True,display_startup_deadline_monotonic=time.monotonic()+3.)
        with patch.dict('sys.modules',{'cv2':cv2,'structured_light.scan3d.dashboard':dashboard}):
            module._worker(buffers,state,condition,connection,config,None,None,Owner,Renderer)
        self.assertEqual([event['kind'] for event in events],['warmup','ready','closed'])
        self.assertTrue(0<calls[1][1]<=3.);self.assertEqual(calls[0],'configure')
        self.assertTrue(events[0]['counts_excluded']);self.assertIsNone(events[0]['frame_id'])
        self.assertTrue(events[-1]['owner_closed'])


if __name__=='__main__':unittest.main()
