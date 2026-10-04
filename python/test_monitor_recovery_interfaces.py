"""Offline interface checks for blocked monitor state and manual recovery."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import cli as stock


class MonitorRecoveryInterfaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("MONITOR_STATE_PATH", "MONITOR_STATE_LOCK_PATH", "WATCHLIST_PATH",
                     "MONITOR_PID_PATH", "MONITOR_LOCK_PATH", "MONITOR_LOG_PATH"):
            self.stack.enter_context(mock.patch.object(stock, name, str(self.folder / name)))
        self.cfg = stock._default_watchlist_config()
        self.cfg["monitor"].update(enabled=True, mode="normal", notify_channels=["telegram"])
        self.stack.enter_context(mock.patch.object(stock, "load_watchlist_config", side_effect=lambda: self.cfg))
        self.stack.enter_context(mock.patch.object(stock, "trading_time_status", return_value={"is_trading_time": True}))
        self.stack.enter_context(mock.patch.object(stock, "monitor_pid_status", return_value={"running": False}))
        self.fetch = self.stack.enter_context(mock.patch.object(stock, "_fetch_strategy_inputs", side_effect=AssertionError("quote fetch forbidden")))
        self.send = self.stack.enter_context(mock.patch.object(stock, "_send_monitor_notification_channel", side_effect=AssertionError("message forbidden")))
        self.process = self.stack.enter_context(mock.patch.object(stock.subprocess, "Popen", side_effect=AssertionError("daemon forbidden")))
        self.stack.enter_context(mock.patch.object(stock.subprocess, "run", side_effect=AssertionError("subprocess forbidden")))
        self.stack.enter_context(mock.patch("socket.socket", side_effect=AssertionError("network forbidden")))

    def corrupt(self):
        data = b'{"last_alerts": {"private-synthetic":'
        Path(stock.MONITOR_STATE_PATH).write_bytes(data)
        return data

    def test_status_and_text_explain_corruption_without_contents(self):
        data = self.corrupt()
        status = stock.monitor_status()
        self.assertFalse(status["ok"])
        self.assertTrue(status["blocked"])
        self.assertEqual(status["state_health"]["code"], "state_json_invalid")
        text = stock.format_monitor_status_summary(status)
        self.assertIn("盯盘已暂停", text)
        self.assertIn("state_json_invalid", text)
        self.assertNotIn("private-synthetic", text)
        self.assertEqual(Path(stock.MONITOR_STATE_PATH).read_bytes(), data)

    def test_corrupt_state_stops_diagnose_before_fetch_or_send(self):
        self.corrupt()
        result = stock.monitor_diagnose(raw_json=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertIsNone(result["scan_result"])
        self.assertIn("暂停", result["conclusion"])
        self.fetch.assert_not_called()
        self.send.assert_not_called()

    def test_corrupt_state_stops_each_open_session_scanner(self):
        self.corrupt()
        self.cfg["strategy_monitor"]["rules"] = [{"id": "synthetic-rule"}]
        for mode in ("normal", "strategy"):
            with self.subTest(mode=mode):
                self.cfg["monitor"]["mode"] = mode
                result = stock.build_monitor_alerts_once()
                self.assertFalse(result["ok"])
                self.assertEqual(result["alerts_count"], 0)
                self.assertEqual(result["checked_count"], 0)
        self.fetch.assert_not_called()

    def test_start_does_not_spawn_with_corrupt_state(self):
        self.corrupt()
        result = stock.monitor_start()
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.process.assert_not_called()

    def test_strategy_check_returns_failure_before_quote_probe(self):
        self.corrupt()
        with mock.patch.object(stock, "_monitor_quote", side_effect=AssertionError("probe forbidden")) as quote:
            result = stock.strategy_check()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["probe_executed"])
        quote.assert_not_called()

    def uncertain_delivery(self):
        with mock.patch.object(stock, "_send_monitor_notification_channel", return_value={"ok": False, "delivery_status": "unknown"}):
            result = stock.send_monitor_notification("synthetic", channels=["telegram"])
        self.assertTrue(result["blocked"])
        return result["delivery_id"]

    def test_unknown_delivery_appears_in_status_and_prevents_diagnose(self):
        delivery_id = self.uncertain_delivery()
        status = stock.monitor_status()
        self.assertTrue(status["blocked"])
        self.assertEqual(status["delivery"]["active"]["id"], delivery_id)
        text = stock.format_monitor_status_summary(status)
        self.assertIn(delivery_id, text)
        self.assertIn("unknown", text)
        self.assertIn("monitor delivery status", text)
        self.assertTrue(stock.monitor_diagnose(raw_json=True)["blocked"])
        self.fetch.assert_not_called()

    def test_unknown_delivery_prevents_daemon_start(self):
        self.uncertain_delivery()
        self.assertTrue(stock.monitor_start()["blocked"])
        self.process.assert_not_called()

    def test_recover_and_resolve_commands_never_send(self):
        delivery_id = self.uncertain_delivery()
        self.assertTrue(stock.monitor_delivery_command(["recover"])["blocked"])
        result = stock.monitor_delivery_command(["resolve", delivery_id, "telegram", "delivered"])
        self.assertTrue(result["ok"])
        self.assertFalse(result["message_sent"])
        self.assertFalse(stock.monitor_delivery_command(["status"])["blocked"])
        self.send.assert_not_called()

    def test_cli_dispatches_delivery_status(self):
        self.uncertain_delivery()
        with mock.patch.object(sys, "argv", ["cli.py", "monitor", "delivery", "status"]), contextlib.redirect_stdout(io.StringIO()) as output:
            stock.main()
        self.assertTrue(json.loads(output.getvalue())["blocked"])
        self.send.assert_not_called()

    def test_invalid_recovery_command_never_dispatches(self):
        with mock.patch.object(stock, "monitor_delivery_resolve", side_effect=AssertionError("invalid resolve dispatched")):
            for args in (["resolve"], ["status", "extra"], ["resolve", "id", "telegram", "delivered", "extra"]):
                self.assertFalse(stock.monitor_delivery_command(args)["ok"])
        self.send.assert_not_called()

    def test_broken_configuration_status_is_readable(self):
        with mock.patch.object(stock, "load_watchlist_config", side_effect=ValueError("private raw bytes must not escape")):
            status = stock.monitor_status()
            text = stock.format_monitor_status_summary(status)
        self.assertFalse(status["ok"])
        self.assertIn("notification_config_unavailable", text)
        self.assertNotIn("private raw", text)

    def simulate(self, send_test):
        with mock.patch.object(stock, "_strategy_selfcheck_state", return_value={}), \
                mock.patch.object(stock, "_save_strategy_selfcheck_state"):
            return stock.strategy_simulate("510050", send_test=send_test)

    def test_strategy_simulation_default_never_sends(self):
        self.assertIsNone(self.simulate(False)["send_result"])
        self.send.assert_not_called()
        self.assertFalse(Path(stock._monitor_delivery_path()).exists())

    def test_explicit_strategy_test_respects_pending_delivery(self):
        self.uncertain_delivery()
        result = self.simulate(True)
        self.assertTrue(result["send_result"]["blocked"])
        self.send.assert_not_called()

    def test_explicit_strategy_test_preserves_single_weixin_scope(self):
        self.cfg["monitor"]["notify_channels"] = ["weixin"]
        with mock.patch.object(stock, "_send_monitor_notification_channel", return_value={"ok": True, "delivery_status": "delivered"}) as transport:
            result = self.simulate(True)
        self.assertTrue(result["send_result"]["ok"])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0], "weixin")


if __name__ == "__main__":
    unittest.main()
