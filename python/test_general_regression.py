"""Deterministic regression tests; fixtures are synthetic, all state is temporary."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_DIR = Path(__file__).resolve().parent / "lobster_quant_agent"
if PACKAGE_DIR.is_dir():
    sys.path.insert(0, str(PACKAGE_DIR))
    import cli as stock
else:
    import a_stock_query as stock
from backtest import config, engine, data_provider as provider, chart


def bar(day, price=10, volume=1000, **kw):
    result = dict(time=day, open=price, close=price, high=price, low=price, volume=volume, amount=price * volume)
    result.update(kw)
    return result


def strategy(buy="价格高于9", sell="价格高于11"):
    return engine.parse_strategy(f"买入条件：{buy}；卖出条件：{sell}")


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        paths = [(stock, "WATCHLIST_PATH", "watch.json"), (config, "CONFIG_PATH", "config.json"),
                 (config, "RESULTS_DIR", "results"), (config, "CHARTS_DIR", "charts"),
                 (chart, "CHARTS_DIR", "charts"), (provider, "CACHE_DIR", "cache")]
        for module, attr, suffix in paths:
            self.stack.enter_context(mock.patch.object(module, attr, str(Path(self.temp.name) / suffix)))
        self.stack.enter_context(mock.patch("socket.socket.connect", side_effect=AssertionError("network forbidden in regression")))
        self.stack.enter_context(mock.patch.object(stock.requests, "get", side_effect=AssertionError("network forbidden in regression")))
        self.stack.enter_context(mock.patch.object(provider.requests, "get", side_effect=AssertionError("network forbidden in regression")))
        self.stack.enter_context(mock.patch.object(stock.subprocess, "run", side_effect=AssertionError("external process forbidden")))
        self.stack.enter_context(mock.patch.object(stock.subprocess, "Popen", side_effect=AssertionError("external process forbidden")))

    def run_fixture(self, bars, parsed=None, **kwargs):
        data = dict(bars=copy.deepcopy(bars), source="synthetic", volume_unit="share", price_adjustment="qfq")
        with mock.patch.object(engine, "load_bars", return_value=data):
            return engine.run_backtest("600000", days=kwargs.pop("days", 30), start="2026-09-01", end="2026-09-30", strategy_override=parsed or strategy(), **kwargs)


class BacktestGrammarTests(IsolatedTests):
    def test_boolean_and_or_preserved(self):
        parsed = strategy("价格高于9并且RSI低于70或者MACD金叉", "价格低于8或者盈利超过10%")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["buy_conditions"][0]["type"], "any")
        self.assertEqual(parsed["buy_conditions"][0]["conditions"][0]["type"], "all")
        self.assertEqual(parsed["sell_conditions"][0]["type"], "any")

    def test_unknown_suffix_and_negation_not_swallowed(self):
        for atom in ("MACD金叉但不能追高", "MACD金叉后连续确认三次", "没有MACD金叉", "KDJ死叉不成立", "MACD金叉神秘形态"):
            with self.subTest(atom=atom):
                self.assertFalse(strategy(atom)["ok"])

    def test_unknown_mixed_clause_refused(self):
        self.assertFalse(strategy("价格高于9并且神秘形态")["ok"])
        self.assertFalse(strategy("价格高于9或者")["ok"])

    def test_ambiguous_volume_and_missing_exit_require_clarification(self):
        self.assertFalse(strategy("放量")["ok"])
        self.assertFalse(engine.parse_strategy("买入条件：MACD金叉")["ok"])

    def test_invalid_window_and_threshold_refused(self):
        for atom in ("成交量超过过去0天均量的2倍", "突破过去0天最高价", "高于MA0", "RSI高于101", "价格高于1.2.3"):
            with self.subTest(atom=atom):
                self.assertFalse(strategy(atom)["ok"])

    def test_unsupported_order_book_refused(self):
        self.assertFalse(strategy("委比大于20")["ok"])

    def test_saved_unknown_condition_rejected_before_fetch(self):
        bad = strategy()
        bad["buy_conditions"] = [{"type": "future_information"}]
        with mock.patch.object(engine, "load_bars") as fetch:
            with self.assertRaisesRegex(ValueError, "不支持"):
                engine.run_backtest("600000", strategy_override=bad)
            fetch.assert_not_called()

    def test_natural_timeframe_and_multiple_symbols_never_silently_change(self):
        for text in ("回测600000最近0天", "回测600000最近5天15分钟", "回测600000和600001最近5天", "回测600000 1分钟并且5分钟"):
            self.assertFalse(stock._parse_natural_backtest_request(text)["ok"], text)
        self.assertEqual(stock._parse_natural_backtest_request("回测600000最近5天五分钟")["interval"], "5m")

    def test_run_text_failure_preserves_saved_strategy(self):
        config.set_strategy(strategy())
        original = config.load_config()["strategy"]
        with mock.patch.object(engine, "run_backtest", side_effect=RuntimeError("synthetic outage")):
            with self.assertRaises(RuntimeError):
                stock.backtest_run_text_command("回测600000最近5天。买入条件：价格高于8；卖出条件：价格低于7")
        self.assertEqual(config.load_config()["strategy"], original)


class BacktestExecutionTests(IsolatedTests):
    def test_signal_next_open_and_reasons(self):
        rows = [bar("2026-09-01", 10), bar("2026-09-02", 12), bar("2026-09-03", 13)]
        result = self.run_fixture(rows)
        trade = result["trades"][0]
        self.assertEqual(trade["buy_time"], "2026-09-02")
        self.assertEqual(trade["sell_time"], "2026-09-03")
        self.assertAlmostEqual(trade["buy_price"], 12 * 1.0002, places=4)
        self.assertIn("收盘价=10", trade["buy_reason"])
        self.assertIn("收盘价=12", trade["sell_reason"])
        self.assertFalse(trade["forced_exit"])

    def test_nonstandard_ma_period_is_computed(self):
        rows = [bar(f"2026-09-{i:02}", i + 10) for i in range(1, 12)]
        result = self.run_fixture(rows, strategy("高于MA7", "价格高于99"))
        self.assertGreater(result["metrics"]["trade_count"], 0)
        saved = config.load_last_result()
        self.assertIsNone(saved["bars"][5]["ma7"])
        self.assertAlmostEqual(saved["bars"][6]["ma7"], 14)

    def test_indicators_do_not_look_into_future(self):
        prefix = [bar(f"2026-09-{i:02}", i + 10) for i in range(1, 10)]
        a = copy.deepcopy(prefix)
        b = copy.deepcopy(prefix) + [bar("2026-09-10", 999)]
        engine._prepare_bars(a)
        engine._prepare_bars(b)
        self.assertEqual(a, b[:len(a)])

    def test_explicit_range_not_silently_truncated_by_default_days(self):
        rows = [bar(f"2026-09-{i:02}", 10) for i in range(1, 8)]
        self.assertEqual(self.run_fixture(rows, days=3)["start"], "2026-09-01")

    def test_empty_zero_volume_history_does_not_count_as_shrink(self):
        rows = [bar(f"2026-09-{i:02}", 10, volume=0) for i in range(1, 7)]
        matched, reason = engine._evaluate(dict(type="volume_vs_average", lookback=5, operator="<=", factor=.7), rows, 5, None)
        self.assertFalse(matched)
        self.assertIn("零", reason)

    def test_zero_volume_bar_cannot_execute_pending_buy(self):
        rows = [bar("2026-09-01", 10), bar("2026-09-02", 12, volume=0), bar("2026-09-03", 13), bar("2026-09-04", 14)]
        self.assertEqual(self.run_fixture(rows)["trades"][0]["buy_time"], "2026-09-03")

    def test_minute_execution_obeys_t_plus_one(self):
        rows = [bar("2026-09-01 09:31:00", 10), bar("2026-09-01 09:32:00", 12), bar("2026-09-01 09:33:00", 13), bar("2026-09-02 09:31:00", 14)]
        result = self.run_fixture(rows, interval="1m")
        self.assertEqual(result["trades"][0]["sell_time"], "2026-09-02 09:31:00")

    def test_terminal_same_day_holding_not_invented_as_closed_trade(self):
        rows = [bar("2026-09-01 09:31:00", 10), bar("2026-09-01 09:32:00", 12), bar("2026-09-01 09:33:00", 13)]
        result = self.run_fixture(rows, interval="1m")
        self.assertEqual(result["metrics"]["trade_count"], 0)
        self.assertTrue(result["open_position"]["unrealized"])
        self.assertGreater(result["final_equity"], result["cash"])
        self.assertEqual(config.load_last_result()["open_position"], result["open_position"])

    def test_nan_rejected_and_result_not_written(self):
        rows = [bar("2026-09-01"), bar("2026-09-02", float("nan")), bar("2026-09-03")]
        with self.assertRaises(RuntimeError):
            self.run_fixture(rows)
        self.assertFalse(Path(config.RESULTS_DIR).exists())

    def test_invalid_capital_rejected(self):
        cfg = config.default_config()
        cfg["defaults"]["initial_capital"] = 0
        config.save_config(cfg)
        with self.assertRaises(ValueError):
            self.run_fixture([bar(f"2026-09-0{i}") for i in range(1, 4)])

    def test_chart_is_local_and_escapes_strategy(self):
        self.run_fixture([bar(f"2026-09-0{i}", i+9) for i in range(1, 4)])
        result = config.load_last_result()
        result["strategy_text"] = '</script><script>alert("fixture")</script>'
        config.save_last_result(result)
        rendered = chart.render_last_chart()
        content = Path(rendered["chart_path"]).read_text()
        self.assertNotIn('</script><script>alert', content)
        self.assertIn('data.bars', content)


class ProviderTests(IsolatedTests):
    def test_bar_validation_rejects_nonfinite_negative_and_bad_ohlc(self):
        for change in ({"close": float("inf")}, {"volume": -1}, {"open": 0}, {"high": 9}, {"time": "nonsense"}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                provider._validate_bars([bar("2026-09-01", **change)], "synthetic")

    def test_duplicate_conflict_not_silently_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, "冲突"):
            provider._validate_bars([bar("2026-09-01"), bar("2026-09-01", 11)], "synthetic")
        self.assertEqual(len(provider._validate_bars([bar("2026-09-01")]*2, "synthetic")), 1)

    def test_amount_missing_remains_missing(self):
        rows = provider._validate_bars([bar("2026-09-01", amount=None)], "synthetic")
        self.assertIsNone(rows[0]["amount"])

    def test_volume_normalized_once_through_cache(self):
        csvpath, metapath = provider._cache_paths("600000", "1d", "2026-09-01", "2026-09-02")
        data = provider._source_result([bar("2026-09-01", volume=12)], "eastmoney", csvpath, metapath, [])
        self.assertEqual(data["bars"][0]["volume"], 1200)
        cached = provider._read_cache(csvpath, metapath)
        self.assertEqual(cached["bars"][0]["volume"], 1200)
        self.assertEqual(cached["volume_unit"], "share")
        self.assertEqual(cached["price_adjustment"], "qfq")

    def test_sina_volume_not_multiplied_and_missing_amount_cached(self):
        csvpath, metapath = provider._cache_paths("600000", "1d", "2026-09-01", "2026-09-02")
        data = provider._source_result([bar("2026-09-01", volume=1200, amount=None)], "sina_daily", csvpath, metapath, [])
        cached = provider._read_cache(csvpath, metapath)
        self.assertEqual(data["bars"][0]["volume"], cached["bars"][0]["volume"])
        self.assertEqual(cached["price_adjustment"], "none")
        self.assertIsNone(cached["bars"][0]["amount"])

    def test_legacy_unknown_unit_cache_not_trusted(self):
        csvpath, metapath = provider._cache_paths("600000", "1d", "2026-09-01", "2026-09-02")
        provider._write_cache(csvpath, metapath, [bar("2026-09-01")], "unknown")
        Path(metapath).write_text('{}')
        with self.assertRaisesRegex(RuntimeError, "单位"):
            provider._read_cache(csvpath, metapath)

    def test_corrupt_cache_does_not_block_live_fallback(self):
        csvpath, metapath = provider._cache_paths("600000", "1d", "2026-09-01", "2026-09-02")
        Path(csvpath).parent.mkdir(parents=True)
        Path(csvpath).write_text('time,open\nBAD,XX\n')
        with mock.patch.object(provider, "_fetch_eastmoney", return_value=[bar("2026-09-01")]):
            result = provider.load_bars("600000", "1d", "2026-09-01", "2026-09-02")
        self.assertFalse(result["used_cache"])
        self.assertIn("本地缓存", result["fallback_errors"][0])

    def test_source_failure_retains_stale_cache_notice(self):
        csvpath, metapath = provider._cache_paths("600000", "1d", "2026-09-01", "2026-09-02")
        provider._write_cache(csvpath, metapath, [bar("2026-09-01")], provider.SOURCE_NAMES["eastmoney"], "qfq")
        os.utime(csvpath, (0, 0))
        with mock.patch.object(provider, "_fetch_eastmoney", side_effect=RuntimeError("synthetic")), mock.patch.object(provider, "_fetch_sina_daily", side_effect=RuntimeError("synthetic")), mock.patch.object(provider, "_fetch_akshare", side_effect=RuntimeError("synthetic")):
            result = provider.load_bars("600000", "1d", "2026-09-01", "2026-09-02")
        self.assertTrue(result["cache_stale"])
        self.assertIn("接口均失败", result["cache_notice"])
        self.assertEqual(len(result["fallback_errors"]), 3)

    def test_bounded_fetch_does_not_enter_unbounded_akshare(self):
        with mock.patch.object(provider, "_fetch_eastmoney", side_effect=RuntimeError("synthetic")) as east, mock.patch.object(provider, "_fetch_sina_daily", side_effect=RuntimeError("synthetic")) as sina, mock.patch.object(provider, "_fetch_akshare") as ak:
            with self.assertRaises(RuntimeError):
                provider.load_bars("600000", "1d", "2026-09-01", "2026-09-02", timeout_seconds=3)
        east.assert_called_once_with("600000", "1d", "2026-09-01", "2026-09-02", timeout=3.0, retries=1)
        sina.assert_called_once_with("600000", "2026-09-01", "2026-09-02", timeout=3.0)
        ak.assert_not_called()

    def test_invalid_request_rejected_before_io(self):
        for args in (("../x", "1d", "2026-09-01", "2026-09-02"), ("600000", "15m", "2026-09-01", "2026-09-02"), ("600000", "1d", "2026-09-02", "2026-09-01")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                provider.load_bars(*args)


class GeneralFeatureTests(IsolatedTests):
    def quote_text(self, **changes):
        parts = ["样本", "10", "10", "11", "12", "9", "11", "11", "10000", "110000"]
        parts += ["100", "10"] * 10
        parts += ["2026-09-01", "10:00:00"]
        for key, value in changes.items():
            parts[int(key)] = value
        return 'var hq_str_sh600000="' + ','.join(parts) + '";'

    def test_quote_percent_units_and_time(self):
        result = stock.parse_sina_quote_text(self.quote_text(), "600000")
        self.assertEqual(result["涨跌幅%"], 10)
        self.assertEqual(result["成交量_手"], 100)
        self.assertEqual(result["数据时间"], "2026-09-01 10:00:00")

    def test_quote_partial_order_book_not_reported_as_complete_ratio(self):
        result = stock.parse_sina_quote_text(self.quote_text(**{"10": ""}), "600000")
        self.assertIsNone(result["买卖盘强弱比"])
        self.assertIsNone(result["盘口委比%"])

    def test_quote_nan_missing_and_malformed(self):
        result = stock.parse_sina_quote_text(self.quote_text(**{"3": "nan"}), "600000")
        self.assertIsNone(result["最新价"])
        self.assertIsNone(result["涨跌幅%"])
        self.assertIsNone(stock.parse_sina_quote_text('empty', "600000"))

    def test_quote_http_parser_wiring_and_failure(self):
        response = mock.Mock(text=self.quote_text())
        with mock.patch.object(stock.requests, "get", return_value=response):
            self.assertEqual(stock.quote_sina("600000")["最新价"], 11)
        with mock.patch.object(stock, "_quote_with_realtime_metrics", side_effect=RuntimeError("synthetic")), contextlib.redirect_stdout(io.StringIO()) as out:
            stock.quote("600000")
        self.assertIn("error", json.loads(out.getvalue()))

    def test_index_six_identified_sources(self):
        with mock.patch.object(stock, "quote_sina_by_code", side_effect=lambda code, symbol, name: {"code": code, "symbol": symbol, "name": name}) as fetch:
            rows = stock.index_sina()
        self.assertEqual(len(rows), 6)
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual(rows[0]["code"], "sh000001")

    def test_pool_add_update_remove_and_nl_route(self):
        stock.watchlist_add("watch", "600000", "样本", "old")
        stock.watchlist_add("watch", "600000", "样本", "new")
        self.assertEqual(len(stock.watchlist_list("watch")["items"]), 1)
        self.assertEqual(stock.watchlist_list("watch")["items"][0]["note"], "new")
        self.assertEqual(stock.watchlist_remove("watch", "样本")["removed"], 1)
        result = stock.handle_natural_language_command("持仓池加入 600001 样本B")
        self.assertTrue(result["ok"])
        self.assertEqual(stock.pool_list_command("all")["total_count"], 1)
        with self.assertRaises(ValueError):
            stock.pool_list_command("bad")

    def test_pool_snapshot_failure_is_explicit_and_no_monitor(self):
        stock.watchlist_add("watch", "600000", "样本")
        with mock.patch.object(stock, "_monitor_quote", return_value={"error": "synthetic unavailable"}), mock.patch.object(stock, "monitor_start") as start:
            result = stock.pool_snapshot_command("watch")
        self.assertIn("error", result["snapshots"][0])
        start.assert_not_called()

    def test_news_fallback_dedup_and_mapping(self):
        title = "半导体芯片行业新政策发布推动产业发展"
        with mock.patch.object(stock, "_fetch_json_or_text", side_effect=[None, f'<h2>{title}</h2><h2>{title}</h2>']):
            self.assertEqual(stock.news(3), [title])
        mapping = {"半导体": {"keywords": ["芯片"], "stocks": ["600000", "600000"]}}
        with mock.patch.object(stock, "_load_news_stock_map", return_value=mapping):
            result = stock._match_news_to_stocks([title, "无关联标题"])
        self.assertEqual(result[0]["关联个股/ETF"], ["600000"])
        self.assertEqual(result[1]["命中的题材"], "暂无数据")

    def test_lhb_etf_rejected_without_fetch(self):
        fake = types.SimpleNamespace(stock_lhb_detail_em=mock.Mock())
        with mock.patch.dict(sys.modules, {"akshare": fake}):
            self.assertIn("不适用", stock.lhb(symbol="510050", emit=False)["error"])
        fake.stock_lhb_detail_em.assert_not_called()

    def test_pool_rejects_invalid_code_and_normalizes_prefix(self):
        with self.assertRaises(ValueError):
            stock.watchlist_add("watch", "../bad", "fixture")
        self.assertFalse(Path(stock.WATCHLIST_PATH).exists())
        stock.watchlist_add("watch", "sh600000", "fixture")
        self.assertEqual(stock.watchlist_list("watch")["items"][0]["code"], "600000")

    def test_lhb_bare_stock_and_explicit_index_disambiguated(self):
        self.assertFalse(stock._is_index_or_etf("000001"))
        self.assertFalse(stock._is_index_or_etf("sz000001"))
        self.assertTrue(stock._is_index_or_etf("sh000001"))
        self.assertTrue(stock._is_index_or_etf("上证指数"))
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("optional pandas unavailable; LHB core rejection tests still run")
        frame = pd.DataFrame([{"代码": "000001", "名称": "synthetic-bank", "上榜日": "2026-09-01"}, {"代码": "100001", "名称": "other", "上榜日": "2026-09-01"}])
        fake = types.SimpleNamespace(stock_lhb_detail_em=mock.Mock(return_value=frame))
        with mock.patch.dict(sys.modules, {"akshare": fake}):
            result = stock.lhb(symbol="sz000001", emit=False)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["records"][0]["代码"], "000001")

    def test_lhb_empty_and_error_are_explicit(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("optional pandas unavailable; LHB core rejection tests still run")
        fake = types.SimpleNamespace(stock_lhb_detail_em=mock.Mock(return_value=pd.DataFrame()))
        with mock.patch.dict(sys.modules, {"akshare": fake}):
            self.assertIn("暂无", stock.lhb(date="20260901", emit=False)["error"])
        fake.stock_lhb_detail_em.side_effect = RuntimeError("synthetic unavailable")
        with mock.patch.dict(sys.modules, {"akshare": fake}):
            self.assertIn("接口不可用", stock.lhb(symbol="600000", emit=False)["error"])

    def test_cli_unknown_empty_and_bad_backtest_args(self):
        self.assertIn("error", stock.handle_natural_language_command(""))
        self.assertIn("error", stock.handle_natural_language_command("不支持的命令"))
        for args in (["--days", "0"], ["--interval", "15m"], ["--bogus"]):
            with self.assertRaises(ValueError):
                stock._parse_backtest_cli_args(args)
        with mock.patch.object(sys, "argv", ["stock", "unknown"]), contextlib.redirect_stdout(io.StringIO()) as out:
            stock.main()
        self.assertIn("未知命令", json.loads(out.getvalue())["error"])

    def test_natural_backtest_disabled_does_not_fetch(self):
        with mock.patch.object(engine, "load_bars") as fetch:
            result = stock.handle_natural_language_command("回测600000最近5天")
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_backtest_enable"])
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
