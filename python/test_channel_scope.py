"""Public channel scope with a real temporary journal and mocked transport only."""
import copy
import contextlib
import subprocess
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import cli as stock
SECRET = chr(95).join(("SYNTHETIC", "CONFIG", "ERROR", "MUST", "NOT", "ESCAPE"))


class ScopeChecks:
    def setUp(self):
        self.stock = stock
        self.kind = "public"
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-channel-scope-")
        self.addCleanup(self.temp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.path = Path(self.temp.name) / "watchlist.json"
        for name, path in (("WATCHLIST_PATH", self.path),
                           ("MONITOR_STATE_PATH", Path(self.temp.name) / "monitor_state.json"),
                           ("MONITOR_STATE_LOCK_PATH", Path(self.temp.name) / "monitor_state.lock")):
            self.stack.enter_context(mock.patch.object(stock, name, str(path)))
        self.stack.enter_context(mock.patch.object(stock, "_MONITOR_STATE_OBSERVED_PATHS", set()))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "LOBSTER_QUANT_NOTIFY_CHANNELS": "weixin,telegram", "LOBSTER_QUANT_CHANNEL": "weixin"}))
        self.stack.enter_context(mock.patch("socket.socket", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess forbidden")))
        self.sent = []
        self.state_saves = []
        self.config_saves = []

        def sender(channel, text):
            self.sent.append(channel)
            return {"ok": True, "channel": channel, "delivery_status": "delivered"}

        self.stack.enter_context(mock.patch.object(stock, "_send_monitor_notification_channel", side_effect=sender))
        self.stack.enter_context(mock.patch.object(stock, "send_openclaw_message", side_effect=AssertionError("real sender forbidden")))
        real_state_save = stock.save_monitor_state

        def save_state(value):
            self.state_saves.append(copy.deepcopy(value))
            return real_state_save(value)

        self.stack.enter_context(mock.patch.object(stock, "save_monitor_state", side_effect=save_state))
        self.stack.enter_context(mock.patch.object(stock, "_dispatch_auxiliary_market_command", return_value=None))
        self.stack.enter_context(mock.patch.object(stock, "monitor_start", return_value={"ok": True}))
        self.stack.enter_context(mock.patch.object(stock, "monitor_verify", return_value={"verified": True}))
        self.stack.enter_context(mock.patch.object(stock, "strategy_get", return_value={}))
        self.stack.enter_context(mock.patch.object(stock, "trading_time_status", return_value={}))
        self.stack.enter_context(mock.patch.object(stock, "_monitor_start_message", return_value="synthetic started"))
        self.stack.enter_context(mock.patch.object(stock, "print_json"))

    def config(self, channels, **extra):
        return {"monitor": {"enabled": False, "notify_channels": channels, **extra}}

    def mock_config(self, cfg):
        self.stack.enter_context(mock.patch.object(stock, "load_watchlist_config", side_effect=lambda: copy.deepcopy(cfg)))
        self.stack.enter_context(mock.patch.object(stock, "save_watchlist_config", side_effect=lambda value: self.config_saves.append(copy.deepcopy(value))))

    def test_plural_single_channel_is_authoritative_over_legacy_env_and_source(self):
        monitor = {"notify_channels": ["telegram"], "notify_channel": "weixin", "source_channel": "weixin"}
        self.assertEqual(self.stock._normalize_notify_channels(monitor), ["telegram"])
        cfg = self.stock._normalize_monitor_config({"monitor": monitor})
        self.assertEqual(cfg["monitor"]["notify_channels"], ["telegram"])
        self.assertEqual(cfg["monitor"]["notify_channel"], "telegram")

    def test_explicit_empty_list_never_falls_back(self):
        self.assertEqual(self.stock._normalize_notify_channels({"notify_channels": [], "notify_channel": "telegram"}), [])
        self.assertEqual(self.stock._normalize_notify_channels([]), [])

    def test_only_absent_keys_allow_explicit_environment_defaults(self):
        self.assertEqual(self.stock._normalize_notify_channels({}), ["weixin", "telegram"])
        self.assertEqual(self.stock._normalize_notify_channels({}, include_fallback=False), [])
        self.assertEqual(self.stock._normalize_notify_channels({"notify_channel": "tg"}), ["telegram"])
        self.assertEqual(self.stock._normalize_notify_channels({"notify_channel": ""}), [])
        with mock.patch.dict(os.environ, {"LOBSTER_QUANT_NOTIFY_CHANNELS": ""}):
            self.assertEqual(self.stock._normalize_notify_channels({"source_channel": "weixin"}), [])

    def test_invalid_values_are_rejected_and_aliases_deduplicated(self):
        for invalid in ([""], [None], [False], ["telegram", "unknown"], None, False, {}, ""):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaisesRegex(ValueError, "notification_channel_invalid"):
                    self.stock._normalize_notify_channels({"notify_channels": invalid})
        self.assertEqual(self.stock._normalize_notify_channels({"notify_channels": ["tg", "telegram", "wechat", "微信"]}),
                         ["telegram", "weixin"])

    def test_unsupported_channel_is_validated_before_any_send(self):
        invalid = ["telegram", "unknown"]
        result = self.stock.send_monitor_notification("synthetic", channels=invalid)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "notification_channel_invalid")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state_saves, [])

    def test_explicit_empty_delivery_has_no_read_send_or_persistence(self):
        self.stack.enter_context(mock.patch.object(stock, "load_watchlist_config", side_effect=AssertionError("config must not be read")))
        result = self.stock.send_monitor_notification("synthetic", channels=[])
        self.assertEqual(result["error_code"], "no_notification_channels")
        self.stock.load_watchlist_config.assert_not_called()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state_saves, [])

    def test_none_delivery_uses_existing_scope_without_adding_destinations(self):
        self.mock_config(self.config(["telegram"], notify_channel="weixin"))
        result = self.stock.send_monitor_notification("synthetic")
        self.assertTrue(result["ok"])
        self.assertEqual(self.sent, ["telegram"])
        self.assertEqual(result["notify_channels"], ["telegram"])

    def test_single_explicit_delivery_and_aliases_send_once(self):
        self.mock_config(self.config(["weixin", "telegram"]))
        result = self.stock.send_monitor_notification("synthetic", channels=["tg", "telegram"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.sent, ["telegram"])

    def test_monitor_set_omission_preserves_scope_and_explicit_choice_replaces_it(self):
        self.mock_config(self.config(["telegram"], notify_channel="weixin"))
        self.assertTrue(self.stock.monitor_set(True, mode="normal")["ok"])
        self.assertEqual(self.config_saves[-1]["monitor"]["notify_channels"], ["telegram"])
        self.assertTrue(self.stock.monitor_set(True, source_channel="weixin", notify_channels=["weixin"])["ok"])
        self.assertEqual(self.config_saves[-1]["monitor"]["notify_channels"], ["weixin"])

    def test_monitor_set_invalid_scope_rejects_before_read_or_save(self):
        self.mock_config(self.config(["telegram"]))
        result = self.stock.monitor_set(True, notify_channels=["telegram", "unknown"])
        self.assertFalse(result["ok"])
        self.stock.load_watchlist_config.assert_not_called()
        self.assertEqual(self.config_saves, [])

    def test_empty_enabled_scope_fails_clearly_without_saving(self):
        self.mock_config(self.config(["telegram"]))
        result = self.stock.monitor_set(True, notify_channels=[])
        self.assertEqual(result["error_code"], "no_notification_channels")
        self.stock.load_watchlist_config.assert_not_called()
        self.assertEqual(self.config_saves, [])

    def test_cli_absent_channel_is_none_and_explicit_alias_is_single(self):
        self.assertEqual(self.stock._parse_monitor_cli_args(["normal"]), ("normal", None))
        self.assertEqual(self.stock._parse_monitor_cli_args(["strategy", "--channel", "tg"]), ("strategy", "telegram"))
        self.assertIsNone(self.stock._monitor_channel_arg([]))
        self.assertEqual(self.stock._monitor_channel_arg(["--channel", "wechat"]), "weixin")
        for args in (["--channel"], ["--channel", ""], ["--channel", "unknown"],
                     ["--chanel", "telegram"], ["--channel", "tg", "-c", "weixin"]):
            with self.assertRaises(ValueError):
                self.stock._parse_monitor_cli_args(args)
            with self.assertRaises(ValueError):
                self.stock._monitor_channel_arg(args)

    def test_cli_start_preserves_scope_or_selects_only_explicit_channel(self):
        for args, expected in (([], ["telegram"]), (["--channel", "weixin"], ["weixin"])):
            self.mock_config(self.config(["telegram"], notify_channel="weixin"))
            with mock.patch.object(sys, "argv", ["synthetic", "monitor", "on", *args]):
                self.stock.main()
            self.assertEqual(self.config_saves[-1]["monitor"]["notify_channels"], expected)
            self.assertEqual(self.sent, [])

    def test_invalid_or_empty_cli_start_cannot_start_monitor(self):
        for args, channels in ((["--channel", "unknown"], ["telegram"]), ([], [])):
            self.mock_config(self.config(channels))
            self.stock.monitor_start.reset_mock()
            with mock.patch.object(sys, "argv", ["synthetic", "monitor", "on", *args]):
                self.stock.main()
            self.stock.monitor_start.assert_not_called()
        self.assertEqual(self.config_saves, [])

    def test_natural_start_preserves_existing_single_scope(self):
        for command in ("开启盯盘", "开启策略盯盘", "启动后台盯盘"):
            self.mock_config(self.config(["telegram"], notify_channel="weixin"))
            result = self.stock.handle_natural_language_command(command)
            self.assertTrue(result["ok"])
            self.assertEqual(self.config_saves[-1]["monitor"]["notify_channels"], ["telegram"])
        self.assertEqual(self.sent, [])

    def test_natural_start_empty_scope_does_not_launch(self):
        self.mock_config(self.config([]))
        for command in ("开启盯盘", "开启策略盯盘", "启动后台盯盘"):
            result = self.stock.handle_natural_language_command(command)
            self.assertEqual(result["error_code"], "no_notification_channels")
        self.stock.monitor_start.assert_not_called()

    def test_generic_notify_test_and_simulation_use_existing_single_scope(self):
        self.mock_config(self.config(["telegram"]))
        for command in ("测试盯盘提醒", "模拟盯盘提醒"):
            self.sent.clear()
            result = self.stock.handle_natural_language_command(command)
            self.assertTrue(result["ok"])
            self.assertEqual(self.sent, ["telegram"])

    def test_explicit_notification_test_uses_only_that_channel(self):
        self.mock_config(self.config(["telegram"]))
        for fn in (self.stock.monitor_notify_test, self.stock.monitor_simulate_alert):
            self.sent.clear()
            self.assertTrue(fn("weixin")["ok"])
            self.assertEqual(self.sent, ["weixin"])
            self.sent.clear()
            self.assertFalse(fn("unknown")["ok"])
            self.assertEqual(self.sent, [])

    def test_generic_notify_cli_uses_current_scope_not_default_weixin(self):
        self.mock_config(self.config(["telegram"]))
        for action in ("notify-test", "simulate-alert"):
            self.sent.clear()
            with mock.patch.object(sys, "argv", ["synthetic", "monitor", action]):
                self.stock.main()
            self.assertEqual(self.sent, ["telegram"])

    def test_invalid_existing_config_is_not_rewritten_or_disclosed(self):
        for value in (b'{"bad":', b'[]', b'null', b'{"monitor":null}', b'{"monitor":[]}',
                      ('{"monitor":"' + SECRET + '"}').encode()):
            self.path.write_bytes(value)
            before = self.path.stat()
            with self.assertRaises(ValueError) as error:
                self.stock.load_watchlist_config()
            self.assertNotIn(SECRET, str(error.exception))
            self.assertEqual(self.path.read_bytes(), value)
            self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_missing_enabled_does_not_overwrite_explicit_single_scope(self):
        self.path.write_text(json.dumps({"monitor": {"notify_channels": ["telegram"], "notify_channel": "weixin"}}))
        before = self.path.read_bytes()
        cfg = self.stock.load_watchlist_config()
        self.assertFalse(cfg["monitor"]["enabled"])
        self.assertEqual(cfg["monitor"]["notify_channels"], ["telegram"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_first_missing_file_initializes_disabled_without_destinations(self):
        cfg = self.stock.load_watchlist_config()
        self.assertFalse(cfg["monitor"]["enabled"])
        self.assertEqual(cfg["monitor"]["notify_channels"], [])
        self.assertTrue(self.path.exists())

    def test_corrupt_config_blocks_scope_set_and_natural_start(self):
        self.path.write_text(SECRET)
        before = self.path.read_bytes()
        for result in (self.stock._notification_channel_scope(), self.stock.monitor_set(True),
                       self.stock.handle_natural_language_command("开启盯盘")):
            self.assertEqual(result["error_code"], "notification_config_unavailable")
            self.assertNotIn(SECRET, json.dumps(result))
        self.stock.monitor_start.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)


class PublicChannelScopeTests(ScopeChecks, unittest.TestCase):
    kind = "public"

    def test_qq_is_supported_without_adding_other_channels(self):
        self.mock_config(self.config(["telegram"]))
        result = self.stock.send_monitor_notification("synthetic", channels=["qqbot", "qq"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.sent, ["qq"])


if __name__ == "__main__":
    unittest.main()
