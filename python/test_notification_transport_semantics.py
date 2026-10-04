"""Notification transport fault injection; no configuration, subprocess or network I/O."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import cli as stock

SENTINEL = "SYNTHETIC_PROVIDER_PRIVATE_TEXT"


def cli_receipt(payload=None, **updates):
    result = {"action": "send", "channel": "telegram", "dryRun": False,
              "handledBy": "core", "payload": payload if payload is not None else {
                  "channel": "telegram", "via": "direct", "deliveryStatus": "sent",
                  "result": {"channel": "telegram", "messageId": "123"}}}
    result.update(updates)
    return result


class NotificationTransportTests(unittest.TestCase):
    def setUp(self):
        forbidden = mock.patch("socket.socket", side_effect=AssertionError("network forbidden"))
        forbidden.start()
        self.addCleanup(forbidden.stop)

    def call(self, payload=None, raw=None, error=None, returncode=0):
        proc = SimpleNamespace(returncode=returncode,
                               stdout=raw if raw is not None else json.dumps(payload),
                               stderr=SENTINEL)
        with mock.patch.object(stock, "_notification_targets", return_value={
                "telegram": {"target": "synthetic-target", "account": "synthetic-account"}}), \
                mock.patch.object(stock.subprocess, "run", return_value=proc, side_effect=error) as run:
            result = stock.send_openclaw_message("telegram", "synthetic notification")
        run.assert_called_once_with(
            ["openclaw", "message", "send", "--account", "synthetic-account", "--channel", "telegram",
             "--target", "synthetic-target", "--message", "synthetic notification", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25)
        self.assertNotIn(SENTINEL, json.dumps(result))
        self.assertNotIn("synthetic-target", json.dumps(result))
        return result

    def assert_unknown(self, result):
        self.assertFalse(result["ok"])
        self.assertEqual(result["delivery_status"], "unknown")
        self.assertTrue(result["attempted"])

    def test_installed_core_sent_shape_is_delivered(self):
        result = self.call(cli_receipt())
        self.assertTrue(result["ok"])
        self.assertEqual(result["delivery_status"], "delivered")
        self.assertTrue(result["attempted"])
        self.assertEqual(result["response"], {"ok": True})

    def test_plugin_explicit_confirmation_and_gateway_receipt_are_supported(self):
        for payload in ({"ok": True}, {"success": True}, {"messageId": "123"},
                        {"via": "gateway", "result": {"messageId": "123"}}):
            with self.subTest(payload=payload):
                self.assertTrue(self.call(cli_receipt(payload, handledBy="plugin"))["ok"])

    def test_empty_and_unrecognized_json_are_not_receipts(self):
        for payload in ({}, {"foo": 1}, {"ok": True}, cli_receipt({}),
                        cli_receipt({"messageId": ""}), cli_receipt({}, ok=True)):
            with self.subTest(payload=payload):
                self.assert_unknown(self.call(payload))

    def test_wrong_action_channel_or_handler_is_unknown(self):
        for update in ({"action": "read"}, {"channel": "weixin"}, {"handledBy": "internal-source"},
                       {"handledBy": []}, {"dryRun": 0}, {"dryRun": None}):
            with self.subTest(update=update):
                self.assert_unknown(self.call(cli_receipt(**update)))

    def test_no_json_and_nonobjects_are_unknown(self):
        for raw in ("", SENTINEL, "null", "[]", '"ok"'):
            with self.subTest(raw=raw):
                self.assert_unknown(self.call(raw=raw))

    def test_nonzero_even_with_receipt_is_unknown(self):
        self.assert_unknown(self.call(cli_receipt(), returncode=1))

    def test_timeout_and_postlaunch_errors_are_unknown(self):
        for error in (subprocess.TimeoutExpired("synthetic", 25), OSError(SENTINEL),
                      UnicodeError(SENTINEL), RuntimeError(SENTINEL)):
            with self.subTest(error=type(error).__name__):
                self.assert_unknown(self.call(error=error))

    def test_exec_failure_before_child_is_known_not_sent(self):
        for error in (FileNotFoundError(SENTINEL), PermissionError(SENTINEL)):
            result = self.call(error=error)
            self.assertFalse(result["ok"])
            self.assertEqual(result["delivery_status"], "not_sent")
            self.assertFalse(result["attempted"])

    def test_dry_run_cannot_claim_delivery(self):
        self.assert_unknown(self.call(cli_receipt(dryRun=True)))
        self.assert_unknown(self.call(cli_receipt({"ok": True, "dryRun": True})))

    def test_contradictory_errors_and_provider_failure_are_unknown(self):
        for failure in ({"ok": False}, {"success": False}, {"error": SENTINEL},
                        {"isError": True}, {"sentBeforeError": True}, {"deliveryStatus": "unknown"},
                        {"deliveryStatus": "partial_failed"}, {"status": {}}, {"status": "queued"}):
            with self.subTest(failure=failure):
                self.assert_unknown(self.call(cli_receipt({"messageId": "123", **failure})))

    def test_nested_payload_outcome_failure_overrides_last_receipt(self):
        for outcome in ({"status": "failed", "sentBeforeError": True},
                        {"status": "suppressed"}, {"status": "unknown"}, "unexpected"):
            payload = cli_receipt()["payload"]
            payload["payloadOutcomes"] = [{"status": "sent"}, outcome]
            self.assert_unknown(self.call(cli_receipt(payload)))

    def test_large_or_deep_untrusted_receipt_is_unknown(self):
        payload = {"ok": True, "results": [{}] * 257}
        self.assert_unknown(self.call(cli_receipt(payload)))
        payload = {"ok": True}
        for _ in range(10):
            payload = {"result": payload}
        self.assert_unknown(self.call(cli_receipt(payload)))

    def test_unknown_channel_does_not_read_targets_or_launch(self):
        with mock.patch.object(stock, "_notification_targets") as targets, \
                mock.patch.object(stock.subprocess, "run") as run:
            result = stock.send_openclaw_message("unknown-channel", "synthetic notification")
        targets.assert_not_called()
        run.assert_not_called()
        self.assertEqual(result["delivery_status"], "not_sent")
        self.assertFalse(result["attempted"])

    def test_missing_target_does_not_launch(self):
        with mock.patch.object(stock, "_notification_targets", return_value={}), \
                mock.patch.object(stock.subprocess, "run") as run:
            result = stock.send_openclaw_message("telegram", "synthetic notification")
        run.assert_not_called()
        self.assertEqual(result["delivery_status"], "not_sent")
        self.assertFalse(result["attempted"])


if __name__ == "__main__":
    unittest.main()
