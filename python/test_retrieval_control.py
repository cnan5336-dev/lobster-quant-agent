"""Synthetic adapters only. No credentials, sockets or model subprocesses."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lobster_quant_agent'))
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from retrieval_control import Policy, RetrievalGate, RetrievalBlocked


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.gate = RetrievalGate(self.temp.name, clock=lambda: self.now)
        self.adapter = Mock(return_value=(200, {'results': [{'url': 'https://example.com/a', 'content': 'Synthetic evidence'}]}))
        # An accidental networking regression fails immediately.
        self.socket = patch('socket.socket', side_effect=AssertionError('network forbidden')).start()
        self.addCleanup(patch.stopall)

    def call(self, gate=None, task='task-1', query='synthetic', **kw):
        return (gate or self.gate).execute('search', {'query': query}, task_id=task,
                provider='fixture.v1', reason='citation', transport=kw.pop('transport', self.adapter), **kw)

    def test_disabled_and_reason_gate(self):
        with self.assertRaisesRegex(RetrievalBlocked, 'transport_disabled'):
            self.call(transport=None)
        with self.assertRaisesRegex(RetrievalBlocked, 'reason_required'):
            self.gate.execute('search', {'query': 'x'}, task_id='a', provider='p', reason='routine', transport=self.adapter)
        self.adapter.assert_not_called()

    def test_cache_cross_task_and_restart_preserves_evidence(self):
        first = self.call()
        first['evidence']['results'].clear()
        second = self.call(RetrievalGate(self.temp.name, clock=lambda: self.now), task='task-2')
        self.assertTrue(second['cache_hit'])
        self.assertEqual(first['call_id'], second['call_id'])
        self.assertEqual(first['evidence_sha256'], second['evidence_sha256'])
        self.assertEqual(len(second['evidence']['results']), 1)
        self.assertEqual(second['estimated_cost_microyuan'], 0)
        self.assertEqual(self.gate.status(task_id='task-2')['cache_hits'], 1)
        self.adapter.assert_called_once()

    def test_ttl_and_zero_ttl_and_shorter_policy(self):
        self.call()
        self.now += timedelta(seconds=901)
        self.assertFalse(self.call()['cache_hit'])
        strict = RetrievalGate(self.temp.name, policy=Policy(search_ttl=0), clock=lambda: self.now)
        self.assertFalse(self.call(strict)['cache_hit'])
        self.assertEqual(self.adapter.call_count, 3)

    def test_query_options_and_provider_do_not_collide(self):
        self.call()
        for provider, args in [('fixture.v2', {'query':'synthetic'}), ('fixture.v1', {'query':'synthetic','count':5}),
                               ('fixture.v1', {'query':'synthetic','need_content':True})]:
            r=self.gate.execute('search',args,task_id='task-1',provider=provider,reason='citation',transport=self.adapter)
            self.assertFalse(r['cache_hit'])
        self.assertEqual(self.adapter.call_count, 4)

    def test_fetch_fragment_dedup_query_preservation(self):
        adapter = Mock(return_value=(200, {'content':'# Synthetic', 'title':'fixture'}))
        def fetch(url):
            return self.gate.execute('fetch',{'url':url},task_id='a',provider='p',reason='citation',transport=adapter)
        first=fetch('https://example.com/doc?version=1#part')
        self.assertTrue(fetch('https://example.com/doc?version=1#other')['cache_hit'])
        self.assertFalse(fetch('https://example.com/doc?version=2')['cache_hit'])
        self.assertEqual(first['estimated_cost_microyuan'],5000)

    def test_failures_refund_units_but_attempts_are_bounded(self):
        gate=RetrievalGate(self.temp.name,policy=Policy(task_attempts=2),clock=lambda:self.now)
        self.adapter.return_value=(200,{'results':[]})
        self.assertEqual(self.call(gate)['estimated_cost_microyuan'],0)
        self.adapter.return_value=(429,{'error':'synthetic'})
        self.call(gate)
        with self.assertRaisesRegex(RetrievalBlocked,'task_limit'):self.call(gate)
        self.assertEqual(gate.status(task_id='task-1')['task']['failed'],2)
        self.assertEqual(self.adapter.call_count,2)

    def test_task_limit_and_month_shared_across_tasks(self):
        gate=RetrievalGate(self.temp.name,policy=Policy(task_calls=1,month_calls=2),clock=lambda:self.now)
        self.call(gate)
        with self.assertRaisesRegex(RetrievalBlocked,'task_limit'):self.call(gate,query='next')
        self.call(gate,task='other',query='next')
        with self.assertRaisesRegex(RetrievalBlocked,'month_limit'):self.call(gate,task='third',query='third')
        self.assertTrue(self.call(gate,task='third')['cache_hit'])
        self.assertEqual(self.adapter.call_count,2)

    def test_timeout_retains_reservation_blocks_duplicate_after_restart(self):
        self.adapter.side_effect=TimeoutError('must never appear in diagnostic')
        with self.assertRaisesRegex(RetrievalBlocked,'^retrieval_outcome_unknown$'):self.call()
        gate=RetrievalGate(self.temp.name,clock=lambda:self.now)
        with self.assertRaisesRegex(RetrievalBlocked,'pending_or_unknown'):self.call(gate,task='other')
        self.assertEqual(gate.status(task_id='task-1')['month']['reserved_microyuan'],5000)
        self.adapter.assert_called_once()

    def test_malformed_success_is_unknown(self):
        self.adapter.return_value=(200,{'wrong':'schema'})
        with self.assertRaisesRegex(RetrievalBlocked,'outcome_unknown'):self.call()
        self.assertEqual(self.gate.status(task_id='task-1')['task']['reserved_unknown'],1)

    def test_bjt_month_boundary_and_completion_month(self):
        self.now=datetime(2026,10,31,15,59,59,tzinfo=timezone.utc)
        gate=RetrievalGate(self.temp.name,policy=Policy(month_calls=1),clock=lambda:self.now)
        def adapter(*_):
            self.now+=timedelta(seconds=2)
            return 200,{'results':[{'url':'https://example.com/fixture'}]}
        self.call(gate,transport=adapter)
        self.assertEqual(gate.status(task_id='task-1')['month']['successful'],0)
        self.call(gate,task='new-month',query='new')
        self.assertEqual(gate.status(task_id='new-month')['month']['successful'],1)

    def test_concurrent_duplicate_and_global_reservation(self):
        entered=threading.Event(); release=threading.Event()
        def slow(*_):
            entered.set(); release.wait(5)
            return 200, {'results':[{'url':'https://example.com/a'}]}
        with ThreadPoolExecutor(2) as pool:
            future=pool.submit(self.call,transport=slow)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(RetrievalBlocked,'pending_or_unknown'):self.call(task='other')
                gate=RetrievalGate(self.temp.name,policy=Policy(month_calls=1),clock=lambda:self.now)
                with self.assertRaisesRegex(RetrievalBlocked,'month_limit'):self.call(gate,task='other',query='other')
            finally:release.set()
            self.assertFalse(future.result()['cache_hit'])
        self.adapter.assert_not_called()

    def test_invalid_input_never_calls_adapter(self):
        for kind,args in [('search',{'query':'x','ignored':1}),('search',{'query':'x','count':True}),
                          ('fetch',{'url':'https://user:password' + '@' + 'example.com/a'}),('fetch',{'url':'file:///fixture'})]:
            with self.assertRaises(ValueError):
                self.gate.execute(kind,args,task_id='a',provider='p',reason='citation',transport=self.adapter)
        self.adapter.assert_not_called()

    def test_missing_corrupt_and_unsafe_state_fail_closed(self):
        self.call()
        self.gate.path.unlink()
        with self.assertRaisesRegex(RetrievalBlocked,'store_incomplete'):RetrievalGate(self.temp.name)
        self.gate.path.write_bytes(b'corrupt')
        self.gate.path.chmod(0o600)
        with self.assertRaisesRegex(RetrievalBlocked,'store_unavailable'):RetrievalGate(self.temp.name)
        self.gate.path.chmod(0o644)
        with self.assertRaisesRegex(RetrievalBlocked,'unsafe'):RetrievalGate(self.temp.name)

    def test_tampered_cache_detected_and_status_no_adapter(self):
        first = self.call()
        with sqlite3.connect(self.gate.path) as db:db.execute("UPDATE calls SET evidence='{}'")
        with self.assertRaisesRegex(RetrievalBlocked,'integrity'):self.call()
        with self.assertRaisesRegex(RetrievalBlocked,'integrity'):self.gate.read_evidence(first['call_id'])
        self.gate.status(task_id='task-1')
        self.adapter.assert_called_once()

    def test_process_crash_reservation_survives(self):
        import subprocess
        code = """
import os, sys
from retrieval_control import RetrievalGate
def crash(*args): os._exit(7)
RetrievalGate(sys.argv[1]).execute('search', {'query':'crash'}, task_id='crash-task',
    provider='fixture.v1', reason='citation', transport=crash)
"""
        result = subprocess.run([sys.executable, '-c', code, self.temp.name],
            env={'PATH': '', 'PYTHONPATH': str(Path(__file__).resolve().parent / 'lobster_quant_agent')},
            capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7)
        gate = RetrievalGate(self.temp.name)
        self.assertEqual(gate.status(task_id='crash-task')['task']['reserved_unknown'], 1)
        with self.assertRaisesRegex(RetrievalBlocked, 'pending_or_unknown'):
            self.call(gate, query='crash')
        self.adapter.assert_not_called()

    def test_month_attempt_limit_includes_failures(self):
        gate = RetrievalGate(self.temp.name, policy=Policy(month_attempts=1), clock=lambda:self.now)
        self.adapter.return_value = (404, {'error':'synthetic'})
        self.call(gate)
        with self.assertRaisesRegex(RetrievalBlocked, 'month_limit'):
            self.call(gate, task='other', query='other')
        self.adapter.assert_called_once()

    def test_empty_database_cannot_reset_history(self):
        self.call()
        self.gate.path.write_bytes(b'')
        with self.assertRaisesRegex(RetrievalBlocked, 'store_unavailable'):
            RetrievalGate(self.temp.name)

    def test_expired_evidence_remains_immutable(self):
        first = self.call()
        self.now += timedelta(hours=1)
        self.adapter.return_value = (200, {'results':[{'url':'https://example.com/b'}]})
        second = self.call()
        self.assertNotEqual(first['call_id'], second['call_id'])
        with sqlite3.connect(self.gate.path) as db:
            stored = db.execute('SELECT evidence_sha256 FROM calls WHERE id=?', (first['call_id'],)).fetchone()[0]
        self.assertEqual(stored, first['evidence_sha256'])
        self.assertEqual(self.gate.read_evidence(first['call_id'])['evidence_sha256'], stored)
        self.assertEqual(self.gate.read_evidence(first['call_id'])['evidence']['results'][0]['url'], 'https://example.com/a')


if __name__ == '__main__':unittest.main()
