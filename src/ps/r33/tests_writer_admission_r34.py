"""Owned-result retries preserve errors and cancellation; no hardware."""
import threading
import unittest
from unittest.mock import Mock, patch
from .replay import submit_owned_result

class WriterAdmissionTests(unittest.TestCase):
 def test_pressure_retries_identical_result(self):
  writer=Mock();writer.try_submit.side_effect=[None,None,7]
  result=object();refresh=Mock()
  self.assertEqual(submit_owned_result(writer,result,threading.Event(),refresh,.2),(7,2))
  self.assertEqual(refresh.call_count,3)
  self.assertTrue(all(c.args==(result,) for c in writer.try_submit.call_args_list))
 def test_error_is_not_capacity(self):
  writer=Mock();writer.try_submit.side_effect=OSError('disk error')
  with self.assertRaisesRegex(OSError,'disk error'):
   submit_owned_result(writer,object(),threading.Event(),lambda:None,.1)
  self.assertEqual(writer.try_submit.call_count,1)
 def test_timeout_retains_no_ack(self):
  writer=Mock();writer.try_submit.return_value=None
  with self.assertRaisesRegex(TimeoutError,'remains owned'):
   submit_owned_result(writer,object(),threading.Event(),lambda:None,0)
 def test_cancel_does_not_submit(self):
  writer=Mock();stop=threading.Event();stop.set()
  with self.assertRaisesRegex(RuntimeError,'remains owned'):
   submit_owned_result(writer,object(),stop,lambda:None,.1)
  writer.try_submit.assert_not_called()
 def test_unbounded_timeout_rejected(self):
  for value in (float('nan'),float('inf'),-1):
   with self.assertRaises(ValueError):submit_owned_result(Mock(),object(),threading.Event(),lambda:None,value)
 def test_capacity_after_deadline_does_not_accept(self):
  writer=Mock();writer.try_submit.side_effect=[None,7]
  with patch('r33.replay.time.monotonic',side_effect=[0.,.001,.031]):
   with self.assertRaises(TimeoutError):
    submit_owned_result(writer,object(),threading.Event(),lambda:None,.020)
  self.assertEqual(writer.try_submit.call_count,1)
 def test_causal_failure_on_stop_is_not_masked(self):
  writer=Mock();stop=threading.Event();calls=[]
  def refresh():
   calls.append(1)
   if len(calls)==1:stop.set()
   else:raise ValueError('source SHA mismatch')
  with self.assertRaisesRegex(ValueError,'source SHA mismatch'):
   submit_owned_result(writer,object(),stop,refresh,.1)
  writer.try_submit.assert_not_called()

if __name__=='__main__':unittest.main()
