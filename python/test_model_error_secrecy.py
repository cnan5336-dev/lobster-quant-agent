"""Synthetic-only regression for error-token leakage and private diagnostics."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import cli as stock
SENTINEL = "SYNTHETIC_CREDENTIAL_MUST_NOT_ESCAPE_987654"
MODEL = "deepseek-official/deepseek-v4-flash"


class ModelErrorSecrecyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lobster-security-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "private" / "model_fallback.log"
        patch = mock.patch.object(stock, "MODEL_FALLBACK_LOG_PATH", str(self.log))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("socket.socket", side_effect=AssertionError("network forbidden"))
        patch.start()
        self.addCleanup(patch.stop)

    def event(self):
        return {"event": "primary_model_failed", "primary_model": MODEL,
                "error_type": "unknown_error", "request_id": SENTINEL,
                "prompt": SENTINEL, "raw_error": SENTINEL}

    def test_freeform_or_labelled_tokens_are_not_request_ids(self):
        for prefix in ("Authorization Bearer ", "request_id: ", "request id: ", ""):
            self.assertIsNone(stock._model_request_id(prefix + SENTINEL))

    def test_controlled_and_legacy_failures_never_return_or_log_error_credentials(self):
        for controlled in (False, True):
            with self.subTest(controlled=controlled):
                route = {"ok": True, "installed": controlled, "mode": "off", "selected_model": MODEL}
                metadata = {"error_type": None, "keys": {MODEL}, "default": MODEL}
                response = SimpleNamespace(returncode=1, stdout="", stderr="401 Authorization Bearer " + SENTINEL)
                with mock.patch.object(stock, "_model_route_snapshot", return_value=route), mock.patch.object(stock, "_configured_model_metadata", return_value=metadata), mock.patch.object(stock, "MODEL_PRIMARY", ""), mock.patch.object(stock, "MODEL_FALLBACK_CANDIDATES", []), mock.patch.object(stock, "_model_subprocess_options", return_value={}), mock.patch.object(stock.subprocess, "run", return_value=response) as run:
                    result = stock.model_call_with_fallback("synthetic prompt")
                self.assertEqual(run.call_count, 1)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["attempts"][0]["request_id"])
                self.assertNotIn(SENTINEL, json.dumps(result))
                self.assertNotIn(SENTINEL, self.log.read_text())

    def test_parse_exception_text_is_not_returned(self):
        response = SimpleNamespace(returncode=0, stdout="synthetic bad payload", stderr=SENTINEL)
        with mock.patch.object(stock.subprocess, "run", return_value=response), mock.patch.object(stock.json, "loads", side_effect=ValueError(SENTINEL)):
            result = stock._run_model_once(MODEL, "synthetic prompt")
        self.assertEqual(result["provider_summary"], "模型返回格式无效或为空")
        self.assertNotIn(SENTINEL, json.dumps(result))

    def test_new_log_and_directory_are_private_and_extra_fields_dropped(self):
        stock._write_model_fallback_log(self.event())
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.log.parent.stat().st_mode), 0o700)
        value = json.loads(self.log.read_text())
        self.assertEqual(set(value), {"event", "primary_model", "error_type", "time"})
        self.assertNotIn(SENTINEL, self.log.read_text())

    def test_existing_wide_permission_log_is_skipped_unchanged(self):
        self.log.parent.mkdir()
        self.log.write_text("synthetic previous entry\n")
        self.log.chmod(0o644)
        before = self.log.stat()
        with mock.patch.object(stock.os, "fchmod", side_effect=AssertionError("existing permissions must not change")):
            stock._write_model_fallback_log(self.event())
        after = self.log.stat()
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o644)
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_ctime_ns),
                         (after.st_ino, after.st_mtime_ns, after.st_ctime_ns))
        self.assertEqual(self.log.read_text(), "synthetic previous entry\n")

    def test_existing_private_log_appends_without_permission_changes(self):
        self.log.parent.mkdir()
        self.log.write_text("synthetic previous entry\n")
        self.log.chmod(0o600)
        with mock.patch.object(stock.os, "fchmod", side_effect=AssertionError("existing permissions must not change")):
            stock._write_model_fallback_log(self.event())
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertTrue(self.log.read_text().startswith("synthetic previous entry\n"))
        self.assertEqual(len(self.log.read_text().splitlines()), 2)
        self.assertNotIn(SENTINEL, self.log.read_text())
        self.log.chmod(0o200)
        with mock.patch.object(stock.os, "fchmod", side_effect=AssertionError("existing permissions must not change")):
            stock._write_model_fallback_log(self.event())
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o200)

    def test_new_log_is_0600_even_with_strict_umask(self):
        self.log.parent.mkdir(mode=0o700)
        old_umask = os.umask(0o777)
        try:
            stock._write_model_fallback_log(self.event())
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)


    def test_log_symlink_cannot_modify_target(self):
        self.log.parent.mkdir()
        target = self.root / "synthetic-target"
        target.write_text("unchanged")
        self.log.symlink_to(target)
        stock._write_model_fallback_log(self.event())
        self.assertEqual(target.read_text(), "unchanged")
        self.assertTrue(self.log.is_symlink())

    def test_parent_symlink_cannot_redirect_logging(self):
        target = self.root / "synthetic-target-dir"
        target.mkdir()
        self.log.parent.symlink_to(target, target_is_directory=True)
        stock._write_model_fallback_log(self.event())
        self.assertFalse((target / self.log.name).exists())

    def test_hardlinked_log_cannot_modify_target(self):
        self.log.parent.mkdir()
        target = self.root / "synthetic-target"
        target.write_text("unchanged")
        os.link(target, self.log)
        stock._write_model_fallback_log(self.event())
        self.assertEqual(target.read_text(), "unchanged")

    def test_fifo_log_is_rejected_without_blocking(self):
        self.log.parent.mkdir()
        os.mkfifo(self.log, mode=0o600)
        stock._write_model_fallback_log(self.event())
        self.assertTrue(stat.S_ISFIFO(self.log.stat().st_mode))

    def test_logging_os_error_is_silent_and_does_not_escape(self):
        output = io.StringIO()
        with mock.patch.object(stock.os, "open", side_effect=OSError(SENTINEL)), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            stock._write_model_fallback_log(self.event())
        self.assertEqual(output.getvalue(), "")

    def test_notification_summaries_discard_all_provider_credential_forms(self):
        errors = [
            "api_key=" + SENTINEL,
            json.dumps({"apiKey": SENTINEL}),
            "access_token=" + SENTINEL,
            "Authorization: Bearer " + SENTINEL,
            "request_id: " + SENTINEL,
            ValueError(SENTINEL),
            OSError(SENTINEL),
            subprocess.TimeoutExpired(SENTINEL, 25, output=SENTINEL, stderr=SENTINEL),
        ]
        for error in errors:
            with self.subTest(kind=type(error).__name__):
                self.assertNotIn(SENTINEL, stock._safe_error_summary(error))
                self.assertTrue(stock._safe_error_summary(error))

    def test_notification_known_failures_have_fixed_useful_categories(self):
        for raw, expected in (("401 " + SENTINEL, "认证"), ("429 " + SENTINEL, "限流"),
                              ("timed out " + SENTINEL, "超时"),
                              ("connection refused " + SENTINEL, "连接失败")):
            self.assertIn(expected, stock._safe_error_summary(raw))
            self.assertNotIn(SENTINEL, stock._safe_error_summary(raw))

    def test_http_error_authentication_precedes_oserror_classification(self):
        # The production Telegram sender uses urllib; HTTPError is an OSError.
        from urllib.error import HTTPError
        error = HTTPError("https://synthetic.invalid/" + SENTINEL, 401,
                          "Unauthorized " + SENTINEL, {}, None)
        self.assertIsInstance(error, OSError)
        summary = stock._safe_error_summary(error)
        self.assertIn("认证", summary)
        self.assertNotIn("启动", summary)
        self.assertNotIn(SENTINEL, summary)
        summary = stock._safe_error_summary(OSError(SENTINEL))
        self.assertIn("通道或本机", summary)
        self.assertNotIn(SENTINEL, summary)

    def notification(self, response=None, error=None):
        targets = {"telegram": {"target": "synthetic-target", "account": "synthetic-account"}}
        with mock.patch.object(stock, "_notification_targets", return_value=targets), \
                mock.patch.object(stock.subprocess, "run", return_value=response, side_effect=error) as run:
            result = stock.send_openclaw_message("telegram", "synthetic notification")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["openclaw", "message", "send", "--account", "synthetic-account",
                                               "--channel", "telegram", "--target", "synthetic-target",
                                               "--message", "synthetic notification", "--json"])
        self.assertNotIn(SENTINEL, json.dumps(result))
        return result

    def test_failed_notification_output_and_os_exception_are_not_echoed(self):
        response = SimpleNamespace(returncode=1, stdout=json.dumps({"apiKey": SENTINEL}), stderr="401 " + SENTINEL)
        self.assertFalse(self.notification(response=response)["ok"])
        self.assertFalse(self.notification(error=OSError(SENTINEL))["ok"])

    def test_exit_zero_malformed_or_nonobject_json_fails_without_raw_body(self):
        for raw in (SENTINEL, "", json.dumps([SENTINEL]), json.dumps(SENTINEL), "null"):
            result = self.notification(response=SimpleNamespace(returncode=0, stdout=raw, stderr=SENTINEL))
            self.assertFalse(result["ok"])
            self.assertNotIn("response", result)
            self.assertIn("格式无效", result["error"])

    def test_successful_json_response_exports_confirmation_only(self):
        payload = {"ok": True, "apiKey": SENTINEL, "message": SENTINEL,
                   "raw": SENTINEL, "payload": {"ok": True, "messageId": SENTINEL}}
        result = self.notification(response=SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=SENTINEL))
        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], {"ok": True})

    def test_provider_reported_failure_is_fixed_summary_even_with_exit_zero(self):
        for payload in ({"ok": False, "message": SENTINEL}, {"success": False, "raw": SENTINEL},
                        {"error": SENTINEL}, {"payload": {"error": SENTINEL}},
                        {"payload": {"ok": False, "message": SENTINEL}}):
            result = self.notification(response=SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""))
            self.assertFalse(result["ok"])
            self.assertNotIn("response", result)


if __name__ == "__main__":
    unittest.main()
