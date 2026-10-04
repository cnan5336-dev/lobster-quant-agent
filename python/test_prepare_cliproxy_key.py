"""Only synthetic keys in disposable directories; no real terminal or user-key I/O."""
import contextlib
import getpass
import importlib.util
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import warnings


MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'prepare-cliproxy-key.py'
spec = importlib.util.spec_from_file_location('prepare_cliproxy_key', MODULE)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
OLD = 'synthetic-old-key'
NEW = 'synthetic-new-key'


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class KeyEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='proxy-key-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / '.openclaw'
        self.root.mkdir(mode=0o700)
        self.folder = self.root / 'cliproxy-private'
        self.folder.mkdir(mode=0o700)
        self.target = self.folder / 'client-key'

    def existing(self, content=OLD):
        self.target.write_text(content)
        self.target.chmod(0o600)

    def run_helper(self, args=(), answers=(NEW, NEW), tty=True):
        output = FakeTTY() if tty else io.StringIO()
        errors = io.StringIO()
        input_stream = FakeTTY() if tty else io.StringIO()
        with mock.patch.object(Path, 'home', return_value=self.home), \
                mock.patch.object(helper.getpass, 'getpass', side_effect=answers) as prompt, \
                mock.patch.object(helper.sys, 'stdin', input_stream), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = helper.main(list(args))
        text = output.getvalue() + errors.getvalue()
        self.assertNotIn(OLD, text)
        self.assertNotIn(NEW, text)
        self.assertNotIn(str(self.home), text)
        return result, prompt.call_count, text

    def assert_no_temp(self):
        self.assertEqual(list(self.folder.glob('.client-key.tmp-*')), [])

    def test_first_save_creates_private_file_and_directory(self):
        self.folder.rmdir()
        result, count, _ = self.run_helper()
        self.assertEqual((result, count), (0, 2))
        self.assertEqual(self.target.read_text(), NEW)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assert_no_temp()

    def test_default_refuses_existing_before_prompt(self):
        self.existing()
        before = self.target.stat()
        result, count, _ = self.run_helper()
        self.assertEqual((result, count), (1, 0))
        self.assertEqual(self.target.read_text(), OLD)
        self.assertEqual(helper.identity(before), helper.identity(self.target.stat()))

    def test_replace_is_atomic_and_never_opens_old_key(self):
        self.existing()
        old_inode = self.target.stat().st_ino
        original_open = os.open
        writes = []

        def guarded_open(name, flags, *args, **kwargs):
            if str(name) == 'client-key' or str(name) == str(self.target):
                raise AssertionError('old key must never be opened')
            if str(name).startswith('.client-key.tmp-'):
                self.assertTrue(flags & os.O_EXCL)
                self.assertTrue(flags & os.O_NOFOLLOW)
                self.assertTrue(flags & os.O_WRONLY)
                writes.append(name)
            return original_open(name, flags, *args, **kwargs)

        with mock.patch.object(helper.os, 'open', side_effect=guarded_open), \
                mock.patch.object(Path, 'read_text', side_effect=AssertionError('no existing file reads')):
            result, count, _ = self.run_helper(('--replace',))
        self.assertEqual((result, count), (0, 2))
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.target.read_text(), NEW)
        self.assertNotEqual(self.target.stat().st_ino, old_inode)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assert_no_temp()

    def test_replace_missing_file_or_folder_refuses_without_prompt(self):
        for missing_folder in (False, True):
            with self.subTest(missing_folder=missing_folder):
                if missing_folder:
                    self.folder.rmdir()
                result, count, text = self.run_helper(('--replace',))
                self.assertEqual((result, count), (1, 0))
                self.assertIn('省略 --replace', text)
                self.assertFalse(self.target.exists())

    def test_non_tty_refused(self):
        result, count, _ = self.run_helper(('--replace',), tty=False)
        self.assertEqual((result, count), (1, 0))

    def test_mismatched_empty_unicode_control_and_long_inputs_preserve_old(self):
        self.existing()
        for pair in ((NEW, OLD), ('', ''), ('中文', '中文'), ('a\nb', 'a\nb'), ('x' * 4097,) * 2):
            with self.subTest(kind=len(pair[0])):
                result, count, _ = self.run_helper(('--replace',), answers=pair)
                self.assertEqual((result, count), (1, 2))
                self.assertEqual(self.target.read_text(), OLD)
                self.assert_no_temp()

    def test_getpass_warning_stops_hidden_input_fallback(self):
        self.existing()

        def fallback(_):
            warnings.warn('synthetic-warning-secret', getpass.GetPassWarning)
            return NEW

        result, count, text = self.run_helper(('--replace',), answers=fallback)
        self.assertEqual((result, count), (1, 1))
        self.assertNotIn('synthetic-warning-secret', text)
        self.assertEqual(self.target.read_text(), OLD)

    def test_argument_errors_do_not_echo_input(self):
        result, count, text = self.run_helper(('--api-key', 'synthetic-argument-secret'))
        self.assertEqual((result, count), (1, 0))
        self.assertNotIn('synthetic-argument-secret', text)

    def test_target_symlink_and_dangling_symlink_refused(self):
        other = self.home / 'other'
        other.write_text(OLD)
        for link in (other, self.home / 'missing'):
            with self.subTest(link=link.name):
                self.target.symlink_to(link)
                result, count, _ = self.run_helper(('--replace',))
                self.assertEqual((result, count), (1, 0))
                self.assertTrue(self.target.is_symlink())
                self.target.unlink()
        self.assertEqual(other.read_text(), OLD)

    def test_target_bad_permissions_and_hardlink_refused(self):
        self.existing()
        self.target.chmod(0o644)
        self.assertEqual(self.run_helper(('--replace',))[:2], (1, 0))
        self.target.chmod(0o600)
        os.link(self.target, self.home / 'other-link')
        self.assertEqual(self.run_helper(('--replace',))[:2], (1, 0))

    def test_private_directory_symlink_and_bad_permissions_refused(self):
        self.folder.chmod(0o755)
        self.assertEqual(self.run_helper()[:2], (1, 0))
        self.folder.rmdir()
        other = self.home / 'other-dir'
        other.mkdir(mode=0o700)
        self.folder.symlink_to(other, target_is_directory=True)
        self.assertEqual(self.run_helper()[:2], (1, 0))
        self.assertEqual(list(other.iterdir()), [])

    def test_root_symlink_and_wrong_owner_refused(self):
        with mock.patch.object(helper.os, 'getuid', return_value=os.getuid() + 1):
            self.assertEqual(self.run_helper()[:2], (1, 0))
        self.folder.rmdir()
        self.root.rmdir()
        other = self.home / 'other-root'
        other.mkdir()
        self.root.symlink_to(other, target_is_directory=True)
        self.assertEqual(self.run_helper()[:2], (1, 0))

    def test_target_changed_during_confirmation_is_not_overwritten(self):
        self.existing()
        calls = 0

        def entry(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement = self.folder / 'concurrent'
                replacement.write_text('synthetic-concurrent-key')
                replacement.chmod(0o600)
                os.replace(replacement, self.target)
            return NEW

        result, count, _ = self.run_helper(('--replace',), answers=entry)
        self.assertEqual((result, count), (1, 2))
        self.assertEqual(self.target.read_text(), 'synthetic-concurrent-key')
        self.assert_no_temp()

    def test_target_changed_after_temp_fsync_is_not_overwritten(self):
        self.existing()
        original_fsync = os.fsync
        called = False

        def change_then_sync(fd):
            nonlocal called
            original_fsync(fd)
            if not called:
                called = True
                self.target.write_text('synthetic-concurrent-inplace-key')

        with mock.patch.object(helper.os, 'fsync', side_effect=change_then_sync):
            result, count, _ = self.run_helper(('--replace',))
        self.assertEqual((result, count), (1, 2))
        self.assertEqual(self.target.read_text(), 'synthetic-concurrent-inplace-key')
        self.assert_no_temp()

    def test_new_file_race_does_not_overwrite(self):
        original_link = os.link

        def racing_link(*args, **kwargs):
            self.existing('synthetic-concurrent-key')
            return original_link(*args, **kwargs)

        with mock.patch.object(helper.os, 'link', side_effect=racing_link):
            result, count, _ = self.run_helper()
        self.assertEqual((result, count), (1, 2))
        self.assertEqual(self.target.read_text(), 'synthetic-concurrent-key')
        self.assert_no_temp()

    def test_write_failure_cleans_temp_and_redacts_error(self):
        self.existing()
        with mock.patch.object(helper.os, 'write', side_effect=OSError('synthetic-error-secret')):
            result, count, text = self.run_helper(('--replace',))
        self.assertEqual((result, count), (1, 2))
        self.assertNotIn('synthetic-error-secret', text)
        self.assertEqual(self.target.read_text(), OLD)
        self.assert_no_temp()

    def test_file_fsync_precedes_replace_and_directory_fsync_follows(self):
        self.existing()
        events = []
        original_sync, original_replace = os.fsync, os.replace

        def sync(fd):
            events.append('dir-sync' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file-sync')
            return original_sync(fd)

        def replace(*args, **kwargs):
            events.append('replace')
            return original_replace(*args, **kwargs)

        with mock.patch.object(helper.os, 'fsync', side_effect=sync), \
                mock.patch.object(helper.os, 'replace', side_effect=replace):
            self.assertEqual(self.run_helper(('--replace',))[:2], (0, 2))
        self.assertEqual(events, ['file-sync', 'replace', 'dir-sync'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
