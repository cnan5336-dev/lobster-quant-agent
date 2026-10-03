import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lobster_quant_agent'))
#!/usr/bin/env python3
"""Offline monitor fault-injection tests. Never send notifications or start daemons."""
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

try:
    import cli as stock
except ModuleNotFoundError:
    import cli as stock


class MonitorReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for attr in ("WATCHLIST_PATH", "MONITOR_STATE_PATH", "MONITOR_STATE_LOCK_PATH", "MONITOR_PID_PATH", "MONITOR_LOG_PATH", "MONITOR_LOCK_PATH"):
            self.stack.enter_context(mock.patch.object(stock, attr, str(Path(self.tempdir.name) / attr)))
        self.now = datetime(2026, 6, 8, 10, 0, 0)
        self.stack.enter_context(mock.patch.object(stock, "_market_now", return_value=self.now))
        self.stack.enter_context(mock.patch.object(stock.requests, "get", side_effect=AssertionError("network forbidden")))
        self.real_send_function = stock.send_monitor_notification
        self.stack.enter_context(mock.patch.object(stock, "send_monitor_notification", side_effect=AssertionError("notification forbidden")))
        self.config = stock._default_watchlist_config()
        self.config["monitor"].update(enabled=True, mode="strategy", market_hours_only=False)
        self.config["holding_pool"] = [{"code": "510050", "name": "Synthetic ETF"}]
        self.config["strategy_monitor"].update(targets=["holding_pool"], rules=[self.rule()])
        self.stack.enter_context(mock.patch.object(stock, "load_watchlist_config", side_effect=lambda: self.config))
        self.price = 4.0
        self.quote_time = self.now.strftime("%Y-%m-%d %H:%M:%S")
        self.quote = self.stack.enter_context(mock.patch.object(stock, "_monitor_quote", side_effect=self.quote_row))
        self.supplement = self.stack.enter_context(mock.patch.object(stock, "_eastmoney_quote_supplement", return_value={"volume_ratio": 1, "turnover_rate_percent": 1, "data_time": self.quote_time}))
        self.minute = self.stack.enter_context(mock.patch.object(stock, "_eastmoney_minute_bars", return_value=[]))
        self.daily_patch = mock.patch.object(stock, "_strategy_daily_bars", return_value=[])
        self.daily = self.daily_patch.start()
        self.addCleanup(self.daily_patch.stop)
        stock._STRATEGY_DAILY_CACHE.clear()

    def rule(self, **values):
        return {"id": "synthetic-rule", "code": "510050", "side": "alert", "raw_text": "价格超过3元时提醒", "logic": "all", "conditions": [{"type": "quote_threshold", "field": "price", "operator": ">", "value": 3}], **values}

    def quote_row(self, code):
        return {"代码": code, "最新价": self.price, "涨跌幅%": 4, "振幅%": 1, "成交量": 5000, "成交额": 20000, "数据时间": self.quote_time}

    def test_price_rule_reads_only_quote(self):
        result = stock.build_strategy_alerts_once()
        self.assertEqual(result["alerts_count"], 1)
        self.quote.assert_called_once_with("510050")
        self.supplement.assert_not_called()
        self.minute.assert_not_called()
        self.daily.assert_not_called()
        self.assertEqual(set(result["checked"][0]["timings_ms"]), {"quote"})

    def test_explicit_symbol_does_not_require_pool_membership(self):
        self.config["holding_pool"] = []
        result = stock.build_strategy_alerts_once()
        self.assertEqual(result["checked_count"], 1)
        self.assertEqual(result["alerts"][0]["pool"], "指定标的")
        self.assertNotIn("买入", result["alerts"][0]["message"])

    def test_snapshot_reused_across_rules_and_pools(self):
        self.config["watch_pool"] = self.config["holding_pool"][:]
        self.config["strategy_monitor"]["targets"] = ["holding_pool", "watch_pool"]
        self.config["strategy_monitor"]["rules"].append(self.rule(id="second"))
        result = stock.build_strategy_alerts_once()
        self.assertEqual(result["checked_count"], 2)
        self.quote.assert_called_once()

    def test_failed_delivery_remains_retryable_until_commit(self):
        first = stock.build_strategy_alerts_once()
        self.assertEqual(first["alerts_count"], 1)
        self.assertEqual(stock.build_strategy_alerts_once()["alerts_count"], 1)
        self.assertTrue(stock._commit_alert_cooldowns(first["alerts"]))
        self.assertEqual(stock.build_strategy_alerts_once()["alerts_count"], 0)

    def test_unknown_data_does_not_rearm_delivered_signal(self):
        first = stock.build_strategy_alerts_once()
        stock._commit_alert_cooldowns(first["alerts"])
        self.price = None
        missing = stock.build_strategy_alerts_once()
        self.assertEqual(missing["alerts_count"], 0)
        self.assertFalse(missing["checked"][0]["available"])
        self.price = 4
        self.assertEqual(stock.build_strategy_alerts_once()["alerts_count"], 0)

    def test_false_then_true_obeys_cooldown_then_rearms(self):
        first = stock.build_strategy_alerts_once()
        stock._commit_alert_cooldowns(first["alerts"])
        self.price = 2
        self.assertEqual(stock.build_strategy_alerts_once()["alerts_count"], 0)
        self.price = 4
        self.assertEqual(stock.build_strategy_alerts_once()["alerts_count"], 0)
        stock._commit_alert_cooldowns(first["alerts"], sent_at=time.time() - 120)
        self.price = 2
        stock.build_strategy_alerts_once()
        self.price = 4
        self.assertEqual(stock.build_strategy_alerts_once()["alerts_count"], 1)

    def test_stale_and_missing_time_quotes_never_trigger(self):
        for stamp in (None, "2026-06-05 15:00:00", "2026-06-08 10:05:00", "bad"):
            with self.subTest(stamp=stamp):
                self.quote_time = stamp
                result = stock.build_strategy_alerts_once()
                self.assertEqual(result["alerts_count"], 0)
                self.assertFalse(result["checked"][0]["available"])
                self.assertTrue(result["runtime_unavailable"])

    def test_nonfinite_values_and_zero_price_are_unavailable(self):
        for value in (float("nan"), float("inf"), 0):
            with self.subTest(value=value):
                self.price = value
                result = stock.build_strategy_alerts_once()
                self.assertFalse(result["checked"][0]["available"])
                self.assertEqual(result["alerts_count"], 0)

    def test_unavailable_supplement_does_not_block_valid_or_branch(self):
        rule = self.rule(logic="any")
        rule["conditions"].append({"type": "quote_threshold", "field": "volume_ratio", "operator": ">", "value": 2})
        self.config["strategy_monitor"]["rules"] = [rule]
        self.supplement.side_effect = TimeoutError("synthetic timeout")
        result = stock.build_strategy_alerts_once()
        self.assertEqual(result["alerts_count"], 1)
        self.assertTrue(result["runtime_unavailable"])
        self.minute.assert_not_called()
        self.daily.assert_not_called()

    def test_stale_minute_and_missing_ohlc_are_unavailable(self):
        self.config["strategy_monitor"]["rules"] = [self.rule(conditions=[{"type": "indicator_threshold", "indicator": "rsi", "timeframe_minutes": 1, "operator": ">", "value": 60}])]
        for bar in ({"time": "2026-06-05 15:00:00"}, {"time": self.quote_time, "close": 3}):
            self.minute.return_value = [bar]
            result = stock.build_strategy_alerts_once()
            self.assertEqual(result["alerts_count"], 0)
            self.assertFalse(result["checked"][0]["available"])

    def test_required_sources_parallelism_is_bounded(self):
        lock = threading.Lock()
        concurrent = 0
        maximum = 0
        def delayed(code):
            nonlocal concurrent, maximum
            with lock:
                concurrent += 1
                maximum = max(maximum, concurrent)
            time.sleep(0.02)
            with lock:
                concurrent -= 1
            return self.quote_row(code)
        self.quote.side_effect = delayed
        results = stock._fetch_strategy_inputs({str(i): {"quote"} for i in range(8)})
        self.assertEqual(len(results), 8)
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 4)

    def test_daily_cache_normalizes_volume_and_refreshes_today(self):
        self.daily_patch.stop()
        provider = importlib.import_module(stock.__package__ + ".backtest.data_provider" if stock.__package__ else "backtest.data_provider")
        historical = [{"time": "2026-06-05", "open": 3, "high": 3, "low": 3, "close": 3, "volume": 100, "amount": 30000}]
        with mock.patch.object(provider, "load_bars", return_value={"source": "东方财富", "bars": historical}) as loader:
            first = stock._strategy_daily_bars("510050", {"price": 4, "volume": 20000, "amount": 80000})
            second = stock._strategy_daily_bars("510050", {"price": 5, "volume": 30000, "amount": 150000})
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(loader.call_args.kwargs["timeout_seconds"], 3)
        self.assertEqual(first[0]["volume"], 10000)
        self.assertEqual(second[-1]["volume"], 30000)
        self.assertEqual(second[-1]["close"], 5)

    def test_normalized_daily_provider_is_not_converted_twice(self):
        self.daily_patch.stop()
        provider = importlib.import_module(stock.__package__ + ".backtest.data_provider" if stock.__package__ else "backtest.data_provider")
        with mock.patch.object(provider, "load_bars", return_value={"source": "东方财富", "volume_unit": "share", "bars": [{"time": "2026-06-05", "volume": 10000}]}):
            self.assertEqual(stock._strategy_daily_bars("510050", {})[0]["volume"], 10000)

    def test_stale_daily_cache_is_rejected(self):
        self.daily_patch.stop()
        provider = importlib.import_module(stock.__package__ + ".backtest.data_provider" if stock.__package__ else "backtest.data_provider")
        with mock.patch.object(provider, "load_bars", return_value={"cache_stale": True}):
            with self.assertRaisesRegex(RuntimeError, "过期"):
                stock._strategy_daily_bars("510050", {})

    def test_normal_monitor_rejects_stale_quote(self):
        self.config["monitor"]["mode"] = "normal"
        self.quote_time = "2026-06-05 15:00:00"
        result = stock.build_monitor_alerts_once()
        self.assertEqual(result["alerts_count"], 0)
        self.assertIn("过期", result["suppressed"][0]["suppressed_reason"])

    def test_same_symbol_in_both_pools_gets_one_normal_alert(self):
        self.config["monitor"]["mode"] = "normal"
        self.config["watch_pool"] = self.config["holding_pool"][:]
        result = stock.build_monitor_alerts_once()
        self.assertEqual(result["checked_count"], 1)
        self.assertEqual(result["alerts_count"], 1)
        self.quote.assert_called_once()

    def test_flat_prices_have_neutral_rsi_and_crosses_need_warmup(self):
        bars = [{"close": 3, "open": 3, "high": 3, "low": 3} for _ in range(20)]
        indicators = stock._indicator_values(bars)
        self.assertEqual(indicators["rsi"][-1], 50)
        self.assertTrue(all(value is None for value in indicators["dif"]))
        self.assertTrue(all(value is None for value in indicators["kdj_k"][:8]))

    def test_delivery_only_commits_the_displayed_batch(self):
        alerts = [{"code": str(i), "message": "synthetic", "_cooldown_key": str(i)} for i in range(11)]
        def finish(*args):
            self.config["monitor"]["enabled"] = False
        with mock.patch.object(stock, "build_monitor_alerts_once", return_value={"alerts_count": 11, "alerts": alerts}), mock.patch.object(stock, "send_monitor_notification", return_value={"ok": True}), mock.patch.object(stock, "_commit_alert_cooldowns", return_value=True) as commit, mock.patch.object(stock, "_monitor_wait", side_effect=finish), contextlib.redirect_stdout(io.StringIO()):
            stock.monitor_loop()
        self.assertEqual(len(commit.call_args.args[0]), 10)

    def test_state_save_failure_is_exposed_in_both_modes(self):
        for mode in ("normal", "strategy"):
            self.config["monitor"]["mode"] = mode
            with mock.patch.object(stock, "save_monitor_state", return_value=False):
                self.assertFalse(stock.build_monitor_alerts_once()["ok"])

    def test_failed_scan_persistence_never_sends(self):
        def finish(*args):
            self.config["monitor"]["enabled"] = False
        with mock.patch.object(stock, "save_monitor_state", return_value=False), mock.patch.object(stock, "_monitor_wait", side_effect=finish), contextlib.redirect_stdout(io.StringIO()) as output:
            stock.monitor_loop()
        stock.send_monitor_notification.assert_not_called()
        self.assertIn("monitor_scan_not_committed", output.getvalue())

    def test_delivery_ack_failure_retries_save_without_resending(self):
        real_commit = stock._commit_alert_cooldowns
        commit_attempts = 0
        waits = 0
        def commit(alerts, sent_at=None):
            nonlocal commit_attempts
            commit_attempts += 1
            return False if commit_attempts < 3 else real_commit(alerts, sent_at)
        def wait(seconds, mode):
            nonlocal waits
            waits += 1
            if waits <= 2:
                self.assertGreaterEqual(seconds, 5)
            if waits == 3:
                self.config["monitor"]["enabled"] = False
        with mock.patch.object(stock, "_commit_alert_cooldowns", side_effect=commit), mock.patch.object(stock, "send_monitor_notification", return_value={"ok": True}) as send, mock.patch.object(stock, "_monitor_wait", side_effect=wait), contextlib.redirect_stdout(io.StringIO()) as output:
            stock.monitor_loop()
        self.assertEqual(commit_attempts, 3)
        self.assertEqual(send.call_count, 1)
        self.assertIn("monitor_delivery_uncommitted", output.getvalue())
        self.assertIn("monitor_delivery_commit_pending", output.getvalue())
        self.assertTrue(stock.load_monitor_state()["last_alerts"])

    def test_partial_channel_success_is_explicit_in_diagnose(self):
        with mock.patch.object(stock, "send_openclaw_message", side_effect=lambda channel, text: {"ok": channel == "telegram", "channel": channel}):
            delivery = self.real_send_function("synthetic", channels=["weixin", "telegram"])
        self.assertTrue(delivery["ok"])
        self.assertTrue(delivery["partial"])
        self.assertEqual(delivery["failed_channels"], ["weixin"])
        self.assertEqual(delivery["delivery_policy"], "any_channel_success")
        text = stock._format_monitor_diagnose({"last_notify_result": delivery})
        self.assertIn("部分成功", text)
        self.assertIn("不单独补发失败通道", text)

    def test_unquoted_space_path_matches_only_exact_script_and_python(self):
        script = "/tmp/synthetic project/cli.py"
        with mock.patch.object(stock, "__file__", script):
            for command in (f"python -u {script} monitor_loop", ["python", "-u", script, "monitor_loop"], f'python -u "{script}" monitor_loop'):
                with mock.patch.object(stock, "_process_command", return_value=command):
                    self.assertTrue(stock._is_monitor_process(123))
            for command in ("python -u /tmp/other project/cli.py monitor_loop", f"echo {script} monitor_loop", f"python -c {script} monitor_loop", f"python {script}.old monitor_loop", f"python {script} monitor_loop_extra"):
                with mock.patch.object(stock, "_process_command", return_value=command):
                    self.assertFalse(stock._is_monitor_process(123))

    @staticmethod
    def minute_bar(hm):
        return {"time": "2026-06-08 " + hm + ":00", "open": 3, "high": 4, "low": 2, "close": 3.5, "volume": 100, "amount": 350}

    def test_minute_aggregation_is_aligned_and_excludes_opening_point(self):
        bars = [self.minute_bar(value) for value in ("09:30", "09:31", "09:32", "09:33", "09:34", "09:35", "09:36")]
        result = stock._aggregate_minute_bars(bars, 3)
        self.assertEqual([bar["time"][-8:] for bar in result], ["09:33:00", "09:36:00"])
        self.assertEqual([bar["volume"] for bar in result], [300, 300])
        result = stock._aggregate_minute_bars(bars, 5)
        self.assertEqual([bar["time"][-8:] for bar in result], ["09:35:00"])
        self.assertEqual(result[0]["volume"], 500)

    def test_minute_aggregation_never_bridges_lunch_gaps_or_duplicates(self):
        cases = (("11:30", "13:00", "13:01"), ("09:31", "09:33", "09:34"), ("09:32", "09:33", "09:34"), ("09:31", "09:31", "09:33"), ("09:33", "09:32", "09:31"))
        for times in cases:
            with self.subTest(times=times):
                self.assertEqual(stock._aggregate_minute_bars([self.minute_bar(value) for value in times], 3), [])
        afternoon = stock._aggregate_minute_bars([self.minute_bar(value) for value in ("13:00", "13:01", "13:02", "13:03")], 3)
        self.assertEqual([bar["time"][-8:] for bar in afternoon], ["13:03:00"])

    def test_order_book_strict_greater_does_not_match_equality(self):
        condition = {"type": "order_book_strength", "direction": "buy", "min_ratio": 1.5, "operator": ">"}
        self.assertFalse(stock.evaluate_strategy_condition(condition, {"buy_sell_ratio": 1.5})[0])
        condition["operator"] = ">="
        self.assertTrue(stock.evaluate_strategy_condition(condition, {"buy_sell_ratio": 1.5})[0])

    def test_runtime_cleanup_keeps_stable_lock_inode(self):
        lock = stock._acquire_monitor_instance_lock()
        inode = os.stat(stock.MONITOR_LOCK_PATH).st_ino
        stock._cleanup_monitor_runtime(os.getpid(), lock)
        self.assertEqual(os.stat(stock.MONITOR_LOCK_PATH).st_ino, inode)
        second = stock._acquire_monitor_instance_lock()
        self.assertIsNotNone(second)
        stock._cleanup_monitor_runtime(os.getpid(), second)

    def test_process_identity_rejects_same_basename_other_checkout(self):
        basename = os.path.basename(stock.__file__)
        for command in (f"python /tmp/unrelated/{basename} monitor_loop", f"python {basename} monitor_loop", f"python {stock.__file__} not_monitor_loop"):
            with mock.patch.object(stock, "_process_command", return_value=command):
                self.assertFalse(stock._is_monitor_process(123))
        with mock.patch.object(stock, "_process_command", return_value=f"python {os.path.abspath(stock.__file__)} monitor_loop"):
            self.assertTrue(stock._is_monitor_process(123))

    def test_disabled_wait_exits_without_sleep(self):
        self.config["monitor"]["enabled"] = False
        with mock.patch.object(stock.time, "sleep") as sleep:
            stock._monitor_wait(300, "strategy")
        sleep.assert_not_called()

    def test_scan_failure_does_not_kill_monitor(self):
        def finish(*args):
            self.config["monitor"]["enabled"] = False
        with mock.patch.object(stock, "build_monitor_alerts_once", side_effect=RuntimeError("synthetic data failure")), mock.patch.object(stock, "_monitor_wait", side_effect=finish), contextlib.redirect_stdout(io.StringIO()) as output:
            stock.monitor_loop()
        self.assertIn("monitor_iteration_failed", output.getvalue())
        self.assertFalse(Path(stock.MONITOR_PID_PATH).exists())
        self.assertFalse(stock._is_monitor_lock_held())

    def test_disable_during_scan_prevents_notification(self):
        def scan():
            self.config["monitor"]["enabled"] = False
            return {"alerts_count": 1, "alerts": [{"message": "synthetic"}]}
        with mock.patch.object(stock, "build_monitor_alerts_once", side_effect=scan), contextlib.redirect_stdout(io.StringIO()):
            stock.monitor_loop()
        # Global notification sentinel would raise if called; inspect explicitly too.
        stock.send_monitor_notification.assert_not_called()


class StartupScriptTests(unittest.TestCase):
    def test_machine_readable_status_and_no_manual_pid_launch(self):
        candidate = Path(__file__).parent / "scripts" / "start-market-monitor.sh"
        script = candidate if candidate.exists() else Path(__file__).parents[1] / "scripts" / "start-market-monitor.sh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "fake_assistant.py"
            fixture.write_text('import json,os,sys\nfrom pathlib import Path\np=Path(os.environ["MONITOR_TEST_CALLS"])\np.write_text(p.read_text()+json.dumps(sys.argv[1:])+"\\n" if p.exists() else json.dumps(sys.argv[1:])+"\\n")\nprint(os.environ["MONITOR_TEST_STATUS"] if sys.argv[1:3]==["monitor","status"] else json.dumps({"ok":True}))\n')
            calls = root / "calls"
            env = dict(os.environ, PYTHON_BIN=sys.executable, MARKET_ASSISTANT_SCRIPT=str(fixture), MONITOR_TEST_CALLS=str(calls))
            for enabled in (False, True):
                calls.unlink(missing_ok=True)
                env["MONITOR_TEST_STATUS"] = json.dumps({"monitor": {"enabled": enabled}})
                result = subprocess.run(["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                recorded = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual(recorded[0], ["monitor", "status", "原始JSON"])
                self.assertEqual(recorded[1:], [["monitor_start"]] if enabled else [])
            calls.unlink()
            env["MONITOR_TEST_STATUS"] = "中文摘要不是JSON"
            result = subprocess.run(["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(calls.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
