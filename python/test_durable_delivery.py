"""Offline durable notification crash and recovery tests; never use a live transport."""
import contextlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lobster_quant_agent'))
import cli as stock

class SimulatedCrash(BaseException):
    pass

class DurableDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for key in ('MONITOR_STATE_PATH', 'MONITOR_STATE_LOCK_PATH', 'WATCHLIST_PATH'):
            self.stack.enter_context(mock.patch.object(stock, key, str(Path(self.tmp.name) / key)))
        self.stack.enter_context(mock.patch.object(stock, '_notification_channel_scope', side_effect=lambda channels=None: {'ok': True, 'notify_channels': channels or ['telegram']}, create=True))
        self.transport = self.stack.enter_context(mock.patch.object(stock, '_send_monitor_notification_channel', return_value={'ok': True, 'delivery_status': 'delivered'}))
        self.stack.enter_context(mock.patch.object(stock.requests, 'get', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(mock.patch.object(stock.requests, 'post', side_effect=AssertionError('network forbidden'), create=True))
        self.alerts = [{'_cooldown_key': 'synthetic:price', '_strategy_active_key': 'synthetic-rule', 'message': 'must not persist message'}]
        self.secretish_message = 'SYNTHETIC PRIVATE MESSAGE must not appear in journal'
        self.assertTrue(stock.save_monitor_state(stock.load_monitor_state()))

    def send(self, channels=None):
        try:
            revision = stock.load_monitor_state()['revision']
        except Exception:
            revision = 0
        return stock.send_monitor_notification(self.secretish_message, channels=channels or ['telegram'], alerts=self.alerts, expected_state_revision=revision)

    def journal(self):
        return json.loads(Path(stock._monitor_delivery_path()).read_text())

    def active(self):
        return next(e for e in self.journal()['transactions'] if e['state'] == 'active')

    def test_success_is_durable_and_strips_message(self):
        result = self.send()
        self.assertTrue(result['ok'])
        self.assertTrue(result['cooldown_committed'])
        self.assertEqual(self.journal()['transactions'][0]['state'], 'complete')
        self.assertIn('synthetic:price', stock.load_monitor_state()['last_alerts'])
        self.assertTrue(stock.load_monitor_state()['strategy_active']['synthetic-rule'])
        journal_text = Path(stock._monitor_delivery_path()).read_text()
        self.assertNotIn(self.secretish_message, journal_text)
        self.assertNotIn('must not persist message', journal_text)
        self.assertEqual(os.stat(stock._monitor_delivery_path()).st_mode & 0o777, 0o600)

    def test_missing_positive_confirmation_holds(self):
        self.transport.return_value = {'ok': True}
        result = self.send()
        self.assertTrue(result['blocked'])
        self.assertEqual(self.active()['channels'][0]['status'], 'unknown')
        self.assertFalse(self.send()['ok'])
        self.assertEqual(self.transport.call_count, 1)

    def test_timeout_blocks_remaining_channels_and_restart(self):
        self.transport.side_effect = TimeoutError('synthetic secret error')
        result = self.send(['telegram', 'weixin'])
        self.assertTrue(result['blocked'])
        self.assertEqual([c['status'] for c in self.active()['channels']], ['unknown', 'skipped'])
        self.assertFalse(stock.monitor_delivery_recover()['ok'])
        self.assertTrue(self.send()['blocked'])
        self.assertEqual(self.transport.call_count, 1)
        self.assertNotIn('synthetic secret error', Path(stock._monitor_delivery_path()).read_text())

    def test_legacy_false_is_not_safe_retry(self):
        self.transport.return_value = {'ok': False, 'error': 'nonzero exit'}
        self.assertTrue(self.send()['blocked'])
        self.send()
        self.assertEqual(self.transport.call_count, 1)

    def test_definite_not_sent_is_retryable_without_cooldown(self):
        self.transport.return_value = {'ok': False, 'delivery_status': 'not_sent'}
        first = self.send()
        self.assertFalse(first['ok'])
        self.assertFalse(first['blocked'])
        self.assertEqual(stock.load_monitor_state()['last_alerts'], {})
        self.send()
        self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(len(self.journal()['transactions']), 2)

    def test_partial_success_known_failure_commits_without_repeat(self):
        self.transport.side_effect = [{'ok': True, 'delivery_status': 'delivered'}, {'ok': False, 'delivery_status': 'not_sent'}]
        result = self.send(['telegram', 'weixin'])
        self.assertTrue(result['ok'])
        self.assertTrue(result['partial'])
        self.assertTrue(stock.monitor_delivery_recover()['ok'])
        self.assertEqual(self.transport.call_count, 2)
        self.assertIn('synthetic:price', stock.load_monitor_state()['last_alerts'])

    def test_partial_success_unknown_holds_then_only_commits(self):
        self.transport.side_effect = [{'ok': True, 'delivery_status': 'delivered'}, {'ok': False, 'delivery_status': 'unknown'}]
        result = self.send(['telegram', 'weixin'])
        self.assertTrue(result['blocked'])
        self.assertEqual(stock.load_monitor_state()['last_alerts'], {})
        resolved = stock.monitor_delivery_resolve(result['delivery_id'], 'weixin', 'not-delivered')
        self.assertTrue(resolved['ok'])
        self.assertFalse(resolved['message_sent'])
        self.assertTrue(resolved['cooldown_committed'])
        self.assertEqual(self.transport.call_count, 2)

    def test_crash_after_intent_before_network_is_unknown(self):
        self.transport.side_effect = SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.send()
        self.assertEqual(self.active()['channels'][0]['status'], 'inflight')
        self.assertTrue(stock.monitor_delivery_recover()['blocked'])
        self.assertEqual(self.active()['channels'][0]['status'], 'unknown')
        self.assertEqual(self.transport.call_count, 1)

    def test_crash_before_inflight_persistence_never_sends(self):
        writer = stock._write_monitor_delivery_unlocked
        calls = []
        def fail_second(data):
            calls.append(None)
            if len(calls) == 2:
                raise SimulatedCrash()
            writer(data)
        with mock.patch.object(stock, '_write_monitor_delivery_unlocked', side_effect=fail_second):
            with self.assertRaises(SimulatedCrash):
                self.send()
        self.transport.assert_not_called()
        self.assertTrue(stock.monitor_delivery_recover()['ok'])
        self.assertEqual(self.journal()['transactions'][0]['channels'][0]['status'], 'skipped')

    def test_sent_before_result_fsync_requires_confirmation(self):
        writer = stock._write_monitor_delivery_unlocked
        calls = []
        def fail_third(data):
            calls.append(None)
            if len(calls) == 3:
                raise SimulatedCrash()
            writer(data)
        with mock.patch.object(stock, '_write_monitor_delivery_unlocked', side_effect=fail_third):
            with self.assertRaises(SimulatedCrash):
                self.send()
        self.assertEqual(self.transport.call_count, 1)
        self.assertTrue(stock.monitor_delivery_recover()['blocked'])
        self.send()
        self.assertEqual(self.transport.call_count, 1)

    def test_known_ack_recovers_only_cooldown(self):
        with mock.patch.object(stock, '_commit_alert_cooldowns', return_value=False):
            result = self.send()
        self.assertTrue(result['blocked'])
        self.assertEqual(self.active()['channels'][0]['status'], 'delivered')
        recovered = stock.monitor_delivery_recover()
        self.assertTrue(recovered['ok'])
        self.assertTrue(recovered['cooldown_committed'])
        self.assertEqual(self.transport.call_count, 1)

    def test_cooldown_saved_before_complete_is_idempotent(self):
        writer = stock._write_monitor_delivery_unlocked
        def fail_complete(data):
            if data['transactions'][-1]['state'] == 'complete':
                raise OSError('simulated fsync failure')
            writer(data)
        with mock.patch.object(stock, '_write_monitor_delivery_unlocked', side_effect=fail_complete):
            self.assertTrue(self.send()['blocked'])
        stamp = stock.load_monitor_state()['last_alerts']['synthetic:price']
        self.assertTrue(stock.monitor_delivery_recover()['ok'])
        self.assertEqual(stock.load_monitor_state()['last_alerts']['synthetic:price'], stamp)
        self.assertEqual(self.transport.call_count, 1)

    def test_recovery_inside_send_requires_fresh_scan(self):
        with mock.patch.object(stock, '_commit_alert_cooldowns', return_value=False):
            self.send()
        result = self.send()
        self.assertEqual(result['error'], 'delivery_recovered_rescan_required')
        self.assertEqual(self.transport.call_count, 1)

    def test_abandon_consumes_candidate_without_message(self):
        self.transport.return_value = {'ok': False, 'delivery_status': 'unknown'}
        result = self.send()
        resolution = stock.monitor_delivery_resolve(result['delivery_id'], 'telegram', 'abandon')
        self.assertTrue(resolution['ok'])
        self.assertTrue(resolution['cooldown_committed'])
        self.assertFalse(resolution['message_sent'])
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(self.journal()['transactions'][0]['channels'][0]['resolution'], 'abandon')

    def test_confirm_delivered_consumes_candidate_without_message(self):
        self.transport.return_value = {'ok': False, 'delivery_status': 'unknown'}
        result = self.send()
        resolution = stock.monitor_delivery_resolve(result['delivery_id'], 'telegram', 'delivered')
        self.assertTrue(resolution['ok'])
        self.assertFalse(resolution['message_sent'])
        self.assertEqual(self.transport.call_count, 1)

    def test_confirm_not_delivered_only_all_failed_allows_fresh_evaluation(self):
        self.transport.return_value = {'ok': False, 'delivery_status': 'unknown'}
        result = self.send()
        resolution = stock.monitor_delivery_resolve(result['delivery_id'], 'telegram', 'not-delivered')
        self.assertTrue(resolution['ok'])
        self.assertFalse(resolution['cooldown_committed'])
        self.assertEqual(stock.load_monitor_state()['last_alerts'], {})
        self.assertEqual(self.transport.call_count, 1)
        self.send()
        self.assertEqual(self.transport.call_count, 2)

    def test_wrong_resolution_id_leaves_inflight_history_unchanged(self):
        self.transport.side_effect = SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.send(['telegram', 'weixin'])
        path = Path(stock._monitor_delivery_path())
        before = path.read_bytes()
        result = stock.monitor_delivery_resolve('f' * 32, 'telegram', 'delivered')
        self.assertEqual(result['error'], 'delivery_resolution_not_pending')
        self.assertEqual(path.read_bytes(), before)
        result = stock.monitor_delivery_resolve(self.active()['id'], 'weixin', 'delivered')
        self.assertEqual(result['error'], 'delivery_resolution_not_pending')
        self.assertEqual(path.read_bytes(), before)

    def test_wrong_resolution_id_does_not_commit_ready_ack(self):
        with mock.patch.object(stock, '_commit_alert_cooldowns', return_value=False):
            self.send()
        path = Path(stock._monitor_delivery_path())
        state_path = Path(stock.MONITOR_STATE_PATH)
        before, state_before = path.read_bytes(), state_path.read_bytes()
        result = stock.monitor_delivery_resolve('f' * 32, 'telegram', 'delivered')
        self.assertEqual(result['error'], 'delivery_resolution_not_pending')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(state_path.read_bytes(), state_before)

    def test_resolution_on_unattempted_transaction_is_noop(self):
        writer = stock._write_monitor_delivery_unlocked
        count = []
        def fail_second(data):
            count.append(None)
            if len(count) == 2:
                raise SimulatedCrash()
            writer(data)
        with mock.patch.object(stock, '_write_monitor_delivery_unlocked', side_effect=fail_second):
            with self.assertRaises(SimulatedCrash):
                self.send()
        path = Path(stock._monitor_delivery_path())
        before = path.read_bytes()
        for delivery_id in ('f' * 32, self.active()['id']):
            result = stock.monitor_delivery_resolve(delivery_id, 'telegram', 'delivered')
            self.assertEqual(result['error'], 'delivery_resolution_not_pending')
            self.assertEqual(path.read_bytes(), before)
        self.transport.assert_not_called()

    def test_valid_resolution_can_confirm_crashed_inflight(self):
        self.transport.side_effect = SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.send()
        result = stock.monitor_delivery_resolve(self.active()['id'], 'telegram', 'delivered')
        self.assertTrue(result['ok'])
        self.assertFalse(result['message_sent'])
        self.assertEqual(self.transport.call_count, 1)

    def test_unknown_resolution_is_rejected(self):
        self.assertFalse(stock.monitor_delivery_resolve('f' * 32, 'telegram', 'delivered')['ok'])
        self.assertFalse(stock.monitor_delivery_resolve('bad', 'telegram', 'delivered')['ok'])
        self.transport.assert_not_called()

    def test_corrupt_history_is_preserved_and_blocks(self):
        self.send()
        path = Path(stock._monitor_delivery_path())
        path.write_text('{broken')
        self.assertTrue(self.send()['blocked'])
        self.assertEqual(path.read_text(), '{broken')
        self.assertEqual(self.transport.call_count, 1)

    def test_missing_history_marker_blocks(self):
        self.send()
        Path(stock._monitor_delivery_path()).unlink()
        self.assertEqual(self.send()['error'], 'delivery_history_missing')
        self.assertEqual(self.transport.call_count, 1)

    def test_symlink_history_blocks(self):
        target = Path(self.tmp.name) / 'target'
        target.write_text('{}')
        Path(stock._monitor_delivery_path()).symlink_to(target)
        self.assertTrue(self.send()['blocked'])
        self.transport.assert_not_called()
        self.assertEqual(target.read_text(), '{}')

    def test_fifo_history_blocks_without_hanging(self):
        os.mkfifo(stock._monitor_delivery_path(), 0o600)
        self.assertTrue(self.send()['blocked'])
        self.transport.assert_not_called()

    def test_busy_lock_prevents_parallel_send_and_resolution(self):
        with stock._monitor_delivery_lock():
            self.assertEqual(self.send()['error'], 'delivery_busy')
            self.assertEqual(stock.monitor_delivery_resolve('f' * 32, 'telegram', 'delivered')['error'], 'delivery_busy')
        self.transport.assert_not_called()

    def test_other_process_cannot_send_while_channel_inflight(self):
        module_dir = str(Path(stock.__file__).resolve().parent)
        child = """
import sys
sys.path.insert(0, sys.argv[1])
import cli as stock
stock.MONITOR_STATE_PATH = sys.argv[2]
stock.MONITOR_STATE_LOCK_PATH = sys.argv[3]
stock._notification_channel_scope = lambda channels: {'ok': True, 'notify_channels': ['telegram']}
stock._send_monitor_notification_channel = lambda *args: (_ for _ in ()).throw(AssertionError('transport forbidden'))
result = stock.send_monitor_notification('synthetic concurrency test', channels=['telegram'])
assert result.get('error') == 'delivery_busy', result
"""
        stock.save_monitor_state(stock.load_monitor_state())
        with stock._monitor_delivery_lock():
            process = subprocess.run([sys.executable, '-c', child, module_dir, stock.MONITOR_STATE_PATH, stock.MONITOR_STATE_LOCK_PATH], capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.transport.assert_not_called()

    def test_first_durable_write_failure_prevents_send(self):
        with mock.patch.object(stock, '_delivery_sync_dir', side_effect=OSError('simulated disk failure')):
            self.assertTrue(self.send()['blocked'])
        self.transport.assert_not_called()
        self.assertTrue(stock.monitor_delivery_status()['blocked'])

    def test_state_preflight_failure_prevents_send_and_journal(self):
        with mock.patch.object(stock, 'load_monitor_state', side_effect=ValueError('synthetic invalid state')):
            self.assertTrue(self.send()['blocked'])
        self.transport.assert_not_called()
        self.assertFalse(Path(stock._monitor_delivery_path()).exists())

    def test_history_retains_complete_and_failed_transactions(self):
        self.send()
        self.transport.return_value = {'ok': False, 'delivery_status': 'not_sent'}
        self.send()
        self.assertEqual(len(self.journal()['transactions']), 2)
        self.assertTrue(all(e['state'] == 'complete' for e in self.journal()['transactions']))

    def assert_invalid_history_blocks(self, transform):
        self.send()
        path = Path(stock._monitor_delivery_path())
        damaged = transform(path.read_text())
        path.write_text(damaged)
        self.assertTrue(self.send()['blocked'])
        self.assertEqual(path.read_text(), damaged)
        self.assertEqual(self.transport.call_count, 1)

    def test_invalid_marker_contents_preserved_and_block(self):
        self.send()
        marker = Path(stock._monitor_delivery_path() + '.initialized')
        marker.write_bytes(b'0\n')
        self.assertEqual(self.send()['error'], 'delivery_marker_invalid')
        self.assertEqual(marker.read_bytes(), b'0\n')
        self.assertEqual(self.transport.call_count, 1)

    def test_missing_marker_with_existing_history_blocks(self):
        self.send()
        marker = Path(stock._monitor_delivery_path() + '.initialized')
        marker.unlink()
        self.assertEqual(self.send()['error'], 'delivery_marker_missing')
        self.assertFalse(marker.exists())
        self.assertEqual(self.transport.call_count, 1)

    def test_duplicate_root_json_key_is_rejected(self):
        self.assert_invalid_history_blocks(lambda raw: raw.replace('"version":1', '"version":1,"version":1', 1))

    def test_duplicate_nested_json_key_is_rejected(self):
        self.assert_invalid_history_blocks(lambda raw: raw.replace('"status":"delivered"', '"status":"unknown","status":"delivered"', 1))

    def test_nonfinite_json_constant_is_rejected(self):
        def corrupt(raw):
            data = json.loads(raw)
            data['transactions'][0]['created_at'] = float('nan')
            return json.dumps(data)
        self.assert_invalid_history_blocks(corrupt)

    def test_boolean_version_is_rejected(self):
        self.assert_invalid_history_blocks(lambda raw: raw.replace('"version":1', '"version":true', 1))

    def test_boolean_timestamp_is_rejected(self):
        def corrupt(raw):
            data = json.loads(raw)
            data['transactions'][0]['channels'][0]['confirmed_at'] = True
            return json.dumps(data)
        self.assert_invalid_history_blocks(corrupt)

    def test_unexpected_schema_field_is_rejected(self):
        def corrupt(raw):
            data = json.loads(raw)
            data['transactions'][0]['channels'][0]['raw_response'] = 'synthetic diagnostic'
            return json.dumps(data)
        self.assert_invalid_history_blocks(corrupt)

    def test_resolution_must_match_channel_status(self):
        def corrupt(raw):
            data = json.loads(raw)
            channel = data['transactions'][0]['channels'][0]
            channel.update(resolution='not-delivered', resolved_at=1)
            return json.dumps(data)
        self.assert_invalid_history_blocks(corrupt)

    def test_invalid_outgoing_alert_keys_prevent_any_send(self):
        self.alerts[0]['_cooldown_key'] = 'x' * 513
        self.assertTrue(self.send()['blocked'])
        self.transport.assert_not_called()
        self.assertFalse(Path(stock._monitor_delivery_path()).exists())

    def test_serialized_stale_candidates_send_only_once(self):
        revision = stock.load_monitor_state()['revision']
        first = stock.send_monitor_notification('first fresh signal', channels=['telegram'], alerts=self.alerts, expected_state_revision=revision)
        self.assertTrue(first['ok'])
        second = stock.send_monitor_notification('stale signal', channels=['telegram'], alerts=self.alerts, expected_state_revision=revision)
        self.assertEqual(second['error'], 'delivery_candidate_stale')
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(len(self.journal()['transactions']), 1)

    def test_alert_candidate_requires_explicit_revision(self):
        result = stock.send_monitor_notification('signal', channels=['telegram'], alerts=self.alerts)
        self.assertEqual(result['error'], 'delivery_candidate_stale')
        self.transport.assert_not_called()

    def test_boolean_candidate_revision_is_rejected(self):
        result = stock.send_monitor_notification('signal', channels=['telegram'], alerts=self.alerts, expected_state_revision=True)
        self.assertEqual(result['error'], 'delivery_candidate_stale')
        self.transport.assert_not_called()

    def test_malformed_alert_items_are_not_silently_dropped(self):
        for alerts in ([{}], [{'_strategy_active_key': 'synthetic'}], [{'unexpected': 'synthetic'}], {'_cooldown_key': 'synthetic'}, [{'_cooldown_key': ''}], [{'_cooldown_key': 'synthetic', '_strategy_active_key': None}]):
            with self.subTest(alerts=alerts):
                result = stock.send_monitor_notification('signal', channels=['telegram'], alerts=alerts, expected_state_revision=stock.load_monitor_state()['revision'])
                self.assertEqual(result['error'], 'delivery_alerts_invalid')
        self.transport.assert_not_called()
        self.assertFalse(Path(stock._monitor_delivery_path()).exists())

    def test_notify_test_without_alerts_still_journaled(self):
        result = stock.send_monitor_notification('synthetic test', channels=['telegram'])
        self.assertTrue(result['ok'])
        self.assertEqual(self.journal()['transactions'][0]['alerts'], [])
        self.assertEqual(stock.load_monitor_state()['last_alerts'], {})

if __name__ == '__main__':
    unittest.main()
