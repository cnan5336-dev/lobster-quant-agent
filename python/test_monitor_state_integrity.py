"""Synthetic state-only tests; no configuration, credential, network or send I/O."""
import contextlib
import builtins
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

import requests


BASE = Path(__file__).resolve().parent


def load_candidate():
    candidate = BASE / 'candidate.py'
    if not candidate.exists():
        from lobster_quant_agent import cli
        return cli
    spec = importlib.util.spec_from_file_location('state_candidate', candidate)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(BASE))
    real_expand = os.path.expanduser
    try:
        with mock.patch.object(os.path, 'expanduser', side_effect=lambda path: str(BASE / 'unused-import-path' / str(path)[2:]) if str(path).startswith('~/') else real_expand(path)):
            spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(BASE))
    return module


class MonitorStateIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guards = contextlib.ExitStack()
        cls.addClassCleanup(cls.guards.close)
        private_root = str(Path.home() / '.openclaw')
        def guarded(original):
            def call(path, *args, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)):
                    value = os.fsdecode(path)
                    if value == private_root or value.startswith(private_root + '/') or value.startswith('~/.openclaw'):
                        raise AssertionError('production private file I/O forbidden')
                return original(path, *args, **kwargs)
            return call
        for owner, name in ((builtins, 'open'), (io, 'open'), (os, 'open')):
            cls.guards.enter_context(mock.patch.object(owner, name, side_effect=guarded(getattr(owner, name))))
        for owner, name in ((socket.socket, 'connect'), (socket, 'create_connection'),
                            (socket, 'getaddrinfo'), (requests.sessions.Session, 'request'),
                            (urllib.request, 'urlopen'), (subprocess, 'run'), (subprocess, 'Popen')):
            cls.guards.enter_context(mock.patch.object(owner, name, side_effect=AssertionError('real I/O forbidden')))
        cls.stock = load_candidate()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='monitor-state-synthetic-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / 'monitor-state.json'
        self.marker = Path(str(self.path) + '.initialized')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(self.stock, 'MONITOR_STATE_PATH', str(self.path)))
        self.stack.enter_context(mock.patch.object(self.stock, 'MONITOR_STATE_LOCK_PATH', str(self.folder / 'state.lock')))
        self.stock._MONITOR_STATE_OBSERVED_PATHS.clear()
        self.stock._MONITOR_STATE_SYNCED_MARKERS.clear()

    def original(self, **updates):
        state = self.stock._default_monitor_state()
        state.update(updates)
        return state

    def put(self, state=None, raw=None):
        self.path.write_bytes(raw if raw is not None else json.dumps(state or self.original()).encode())
        self.path.chmod(0o600)

    def assert_preserved_failure(self, raw):
        self.put(raw=raw)
        before = self.path.stat()
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state()
        self.assertFalse(self.stock.save_monitor_state(self.original()))
        self.assertFalse(self.stock._commit_alert_cooldowns([{'_cooldown_key': 'synthetic'}], 123))
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.path.stat().st_ino, before.st_ino)
        self.assertFalse(self.marker.exists())

    def test_fresh_missing_initializes_private_files(self):
        first = self.stock.load_monitor_state()
        self.assertEqual(first['revision'], 0)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.stock.save_monitor_state(first))
        self.assertEqual(self.stock.load_monitor_state()['revision'], 1)
        self.assertEqual(first['revision'], 0, 'caller snapshot must not be changed')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)

    def test_valid_legacy_read_migrates_marker_without_state_rewrite(self):
        raw = b'{"last_alerts":{"synthetic":12},"last_quotes":{},"strategy_active":{}}'
        self.put(raw=raw)
        before = self.path.stat()
        loaded = self.stock.load_monitor_state()
        self.assertEqual(loaded['revision'], 0)
        self.assertEqual(loaded['last_alerts']['synthetic'], 12)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.path.stat().st_ino, before.st_ino)
        self.assertTrue(self.marker.exists())
        with mock.patch.object(self.stock, '_monitor_state_fsync_directory', side_effect=AssertionError('unexpected migration write')):
            self.assertEqual(self.stock.load_monitor_state(), loaded)

    def test_empty_file_preserved(self):
        self.assert_preserved_failure(b'')

    def test_invalid_json_preserved_and_no_contents_in_error(self):
        self.assert_preserved_failure(b'{"synthetic-secret-canary":')
        with self.assertRaises(self.stock.MonitorStateBlocked) as error:
            self.stock.load_monitor_state()
        output = json.dumps(error.exception.as_status(), ensure_ascii=False)
        self.assertNotIn('synthetic-secret-canary', output)
        self.assertNotIn(str(self.path), output)
        self.assertIn('停止扫描及发送', output)

    def test_non_dict_json_preserved(self):
        for raw in (b'[]', b'null', b'42', b'"text"'):
            with self.subTest(raw_type=type(json.loads(raw)).__name__):
                self.assert_preserved_failure(raw)

    def test_missing_core_fields_not_silently_defaulted(self):
        for payload in ({}, {'last_alerts': {}}, {'last_quotes': {}},
                        {'last_alerts': {}, 'last_quotes': {}},
                        {'last_alerts': {}, 'last_quotes': {}, 'revision': 1}):
            with self.subTest(keys=sorted(payload)):
                self.assert_preserved_failure(json.dumps(payload).encode())

    def test_invalid_field_schemas_preserved(self):
        mutations = [
            {'last_alerts': []}, {'last_alerts': {'x': -1}}, {'last_alerts': {'x': True}},
            {'last_alerts': {'x': 'not-a-number'}}, {'strategy_active': []},
            {'strategy_active': {'x': 'false'}}, {'last_quotes': {'x': []}},
            {'last_quotes': {'x': {'ts': -1}}}, {'last_checked_symbols': ['x']},
            {'last_quotes': {'x': {'price': 'invalid'}}}, {'last_quotes': {'x': {'pct': False}}},
            {'last_quotes': {'x': {'time': {}}}},
            {'last_alert_candidates': {}}, {'last_suppressed': [None]},
            {'strategy_last_unavailable': [{}]}, {'last_scan_at': []},
            {'last_notify_result': {'results': [None]}}, {'last_notify_result': {'ok': 'yes'}},
            {'last_notify_result': {'results': [{'ok': 'yes'}]}},
            {'last_notify_result': {'failed_channels': [None]}},
            {'revision': -1}, {'revision': True}, {'revision': '1'},
        ]
        for mutation in mutations:
            with self.subTest(field=next(iter(mutation))):
                self.assert_preserved_failure(json.dumps(self.original(**mutation)).encode())

    def test_duplicate_keys_and_nonfinite_json_blocked(self):
        for raw in (b'{"last_alerts":{},"last_alerts":{},"last_quotes":{}}',
                    b'{"last_alerts":{"x":NaN},"last_quotes":{}}',
                    b'{"last_alerts":{},"last_quotes":{},"extra":Infinity}'):
            self.assert_preserved_failure(raw)

    def test_oversized_file_blocked(self):
        self.put(raw=b' ' * 50)
        with mock.patch.object(self.stock, '_MONITOR_STATE_MAX_BYTES', 20):
            with self.assertRaises(self.stock.MonitorStateBlocked) as error:
                self.stock.load_monitor_state()
        self.assertEqual(error.exception.code, 'state_too_large')
        self.assertEqual(self.path.read_bytes(), b' ' * 50)

    def test_unreadable_state_never_overwritten(self):
        self.put()
        raw = self.path.read_bytes()
        real_open = os.open
        def blocked(path, *args, **kwargs):
            if os.fspath(path) == str(self.path):
                raise PermissionError('synthetic private error')
            return real_open(path, *args, **kwargs)
        with mock.patch.object(self.stock.os, 'open', side_effect=blocked):
            with self.assertRaises(self.stock.MonitorStateBlocked):
                self.stock.load_monitor_state()
            self.assertFalse(self.stock.save_monitor_state(self.original()))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_state_symlink_rejected_without_touching_target(self):
        target = self.folder / 'target.json'
        target.write_text(json.dumps(self.original()))
        self.path.symlink_to(target)
        raw = target.read_bytes()
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state()
        self.assertFalse(self.stock.save_monitor_state(self.original()))
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), raw)

    def test_hardlinked_state_rejected(self):
        self.put()
        os.link(self.path, self.folder / 'other-link')
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state()
        self.assertFalse(self.stock.save_monitor_state(self.original()))

    def test_nonregular_state_rejected(self):
        self.path.mkdir()
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state()
        self.assertTrue(self.path.is_dir())

    def test_unsafe_permissions_rejected_without_chmod(self):
        self.put()
        self.path.chmod(0o666)
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o666)

    def test_missing_state_after_marker_and_process_restart_blocks(self):
        self.assertTrue(self.stock.save_monitor_state(self.stock.load_monitor_state()))
        self.path.unlink()
        self.stock._MONITOR_STATE_OBSERVED_PATHS.clear()
        with self.assertRaises(self.stock.MonitorStateBlocked) as error:
            self.stock.load_monitor_state()
        self.assertEqual(error.exception.code, 'state_missing_after_initialization')
        self.assertFalse(self.stock.save_monitor_state(self.original()))
        self.assertFalse(self.path.exists())

    def test_seen_corrupt_state_deleted_in_same_process_stays_blocked(self):
        self.put(raw=b'bad')
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state()
        self.path.unlink()
        with self.assertRaises(self.stock.MonitorStateBlocked) as error:
            self.stock.load_monitor_state()
        self.assertEqual(error.exception.code, 'state_missing_after_initialization')

    def test_explicit_delivery_history_blocks_first_missing_state(self):
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state(history_expected=True)
        self.assertFalse(self.path.exists())

    def test_journal_or_journal_marker_anchor_blocks_missing_state(self):
        for suffix in ('.delivery.json', '.delivery.json.initialized'):
            anchor = Path(str(self.path) + suffix)
            anchor.write_bytes(b'synthetic opaque journal not parsed')
            with self.assertRaises(self.stock.MonitorStateBlocked):
                self.stock.load_monitor_state()
            anchor.unlink()

    def test_corrupt_or_empty_marker_blocks_with_and_without_state(self):
        self.marker.write_bytes(b'')
        for has_state in (False, True):
            if has_state:
                self.put()
            with self.assertRaises(self.stock.MonitorStateBlocked) as error:
                self.stock.load_monitor_state()
            self.assertEqual(error.exception.code, 'state_marker_invalid')
            self.assertEqual(self.marker.read_bytes(), b'')

    def test_marker_symlink_rejected(self):
        target = self.folder / 'marker-target'
        target.write_bytes(self.stock._MONITOR_STATE_MARKER)
        self.marker.symlink_to(target)
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state()
        self.assertTrue(self.marker.is_symlink())

    def test_lock_symlink_rejected_without_referent_writes(self):
        target = self.folder / 'lock-target'
        target.write_bytes(b'keep')
        Path(self.stock.MONITOR_STATE_LOCK_PATH).symlink_to(target)
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state()
        self.assertEqual(target.read_bytes(), b'keep')

    def test_valid_original_can_be_restored_without_automatic_reset(self):
        state = self.stock.load_monitor_state()
        state['last_alerts']['synthetic'] = 100
        self.assertTrue(self.stock.save_monitor_state(state))
        valid = self.path.read_bytes()
        self.path.write_bytes(b'bad')
        self.assertFalse(self.stock.save_monitor_state(self.original()))
        self.path.write_bytes(valid)
        loaded = self.stock.load_monitor_state()
        self.assertEqual(loaded['last_alerts']['synthetic'], 100)
        self.assertEqual(loaded['revision'], 1)

    def test_two_loaded_snapshots_cannot_overwrite_each_other(self):
        self.assertTrue(self.stock.save_monitor_state(self.stock.load_monitor_state()))
        first = self.stock.load_monitor_state()
        stale = self.stock.load_monitor_state()
        first['last_scan_at'] = 'synthetic-first'
        stale['last_scan_at'] = 'synthetic-stale'
        self.assertTrue(self.stock.save_monitor_state(first))
        committed = self.path.read_bytes()
        self.assertFalse(self.stock.save_monitor_state(stale))
        self.assertEqual(self.path.read_bytes(), committed)
        self.assertEqual(first['revision'], 1)
        self.assertEqual(stale['revision'], 1)
        self.assertEqual(self.stock.load_monitor_state()['revision'], 2)

    def test_stale_scan_cannot_erase_delivered_activation(self):
        self.put(self.original(strategy_active={'synthetic-active': False}))
        stale = self.stock.load_monitor_state()
        alerts = [{'_cooldown_key': 'synthetic', '_strategy_active_key': 'synthetic-active'}]
        self.assertTrue(self.stock._commit_alert_cooldowns(alerts, 200))
        committed = self.path.read_bytes()
        self.assertFalse(self.stock.save_monitor_state(stale))
        self.assertEqual(self.path.read_bytes(), committed)
        self.assertTrue(self.stock.load_monitor_state()['strategy_active']['synthetic-active'])

    def test_commit_timestamp_never_regresses_on_old_replay(self):
        alerts = [{'_cooldown_key': 'synthetic', '_strategy_active_key': 'synthetic-active'}]
        self.assertTrue(self.stock._commit_alert_cooldowns(alerts, 200))
        self.assertTrue(self.stock._commit_alert_cooldowns(alerts, 100))
        state = self.stock.load_monitor_state()
        self.assertEqual(state['last_alerts']['synthetic'], 200)
        self.assertEqual(state['revision'], 2)

    def test_empty_alert_commit_still_checks_corrupt_state(self):
        self.put(raw=b'corrupt')
        self.assertFalse(self.stock._commit_alert_cooldowns([], 100))
        self.assertEqual(self.path.read_bytes(), b'corrupt')

    def test_revisionless_manual_snapshot_rejected_after_migration_write(self):
        self.put()
        self.assertTrue(self.stock.save_monitor_state(self.stock.load_monitor_state()))
        self.assertFalse(self.stock.save_monitor_state(self.original()))
        self.assertEqual(self.stock.load_monitor_state()['revision'], 1)

    def test_marker_directory_fsync_failure_leaves_initialization_blocked(self):
        initial = self.stock.load_monitor_state()
        with mock.patch.object(self.stock, '_monitor_state_fsync_directory', side_effect=self.stock.MonitorStateBlocked('state_directory_sync_failed')):
            self.assertFalse(self.stock.save_monitor_state(initial))
        self.assertTrue(self.marker.exists())
        self.assertFalse(self.path.exists())
        with self.assertRaises(self.stock.MonitorStateBlocked):
            self.stock.load_monitor_state()

    def test_legacy_marker_file_fsync_failure_never_becomes_accepted(self):
        self.put()
        raw = self.path.read_bytes()
        with mock.patch.object(self.stock.os, 'fsync', side_effect=OSError('synthetic fsync failure')) as fsync:
            for _ in range(3):
                with self.assertRaises(self.stock.MonitorStateBlocked):
                    self.stock.load_monitor_state()
            self.assertEqual(fsync.call_count, 3)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.stock.load_monitor_state()['revision'], 0)

    def test_legacy_marker_directory_fsync_failure_never_becomes_accepted(self):
        self.put()
        raw = self.path.read_bytes()
        with mock.patch.object(self.stock, '_monitor_state_fsync_directory', side_effect=self.stock.MonitorStateBlocked('state_directory_sync_failed')) as sync:
            for _ in range(3):
                with self.assertRaises(self.stock.MonitorStateBlocked):
                    self.stock.load_monitor_state()
            self.assertEqual(sync.call_count, 3)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.stock.load_monitor_state()['revision'], 0)

    def test_existing_marker_sync_rechecked_after_process_restart(self):
        self.assertTrue(self.stock.save_monitor_state(self.stock.load_monitor_state()))
        self.stock._MONITOR_STATE_SYNCED_MARKERS.clear()
        with mock.patch.object(self.stock.os, 'fsync', side_effect=OSError('synthetic fsync failure')):
            with self.assertRaises(self.stock.MonitorStateBlocked):
                self.stock.load_monitor_state()
        self.assertEqual(self.stock.load_monitor_state()['revision'], 1)

    def test_file_fsync_failure_preserves_old_state_and_cleans_temp(self):
        self.assertTrue(self.stock.save_monitor_state(self.stock.load_monitor_state()))
        snapshot = self.stock.load_monitor_state()
        raw = self.path.read_bytes()
        with mock.patch.object(self.stock.os, 'fsync', side_effect=OSError('synthetic fsync failure')):
            self.assertFalse(self.stock.save_monitor_state(snapshot))
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse(list(self.folder.glob('.monitor-state-*.tmp')))

    def test_replace_failure_preserves_old_state_and_cleans_temp(self):
        self.assertTrue(self.stock.save_monitor_state(self.stock.load_monitor_state()))
        snapshot = self.stock.load_monitor_state()
        raw = self.path.read_bytes()
        with mock.patch.object(self.stock.os, 'replace', side_effect=PermissionError('synthetic denied')):
            self.assertFalse(self.stock.save_monitor_state(snapshot))
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse(list(self.folder.glob('.monitor-state-*.tmp')))

    def test_post_replace_directory_fsync_reports_failure_without_phantom_snapshot_update(self):
        self.assertTrue(self.stock.save_monitor_state(self.stock.load_monitor_state()))
        snapshot = self.stock.load_monitor_state()
        with mock.patch.object(self.stock, '_monitor_state_fsync_directory', side_effect=self.stock.MonitorStateBlocked('state_directory_sync_failed')):
            self.assertFalse(self.stock.save_monitor_state(snapshot))
        self.assertEqual(snapshot['revision'], 1)
        self.assertEqual(self.stock.load_monitor_state()['revision'], 2)
        self.assertFalse(list(self.folder.glob('.monitor-state-*.tmp')))

    def test_replacement_during_atomic_write_is_not_overwritten(self):
        self.assertTrue(self.stock.save_monitor_state(self.stock.load_monitor_state()))
        snapshot = self.stock.load_monitor_state()
        replacement = self.folder / 'injected-replacement'
        replacement.write_bytes(b'preserve this synthetic corruption')
        real_fsync = self.stock.os.fsync
        replaced = False
        def replace_during_flush(descriptor):
            nonlocal replaced
            real_fsync(descriptor)
            if not replaced:
                os.replace(replacement, self.path)
                replaced = True
        with mock.patch.object(self.stock.os, 'fsync', side_effect=replace_during_flush):
            self.assertFalse(self.stock.save_monitor_state(snapshot))
        self.assertEqual(self.path.read_bytes(), b'preserve this synthetic corruption')


if __name__ == '__main__':
    unittest.main(verbosity=2)
