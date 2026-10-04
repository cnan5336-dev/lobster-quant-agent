"""Offline tests for CLI entries advertised by the installed A-share skill."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_DIR = Path(__file__).resolve().parent / "lobster_quant_agent"
if PACKAGE_DIR.is_dir():
    sys.path.insert(0, str(PACKAGE_DIR))
    import cli as stock
else:
    import a_stock_query as stock
from backtest import data_provider as provider


class DocumentedCommandTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(mock.patch.object(stock.requests, "get", side_effect=AssertionError("HTTP forbidden")))
        self.stack.enter_context(mock.patch.object(provider.requests, "get", side_effect=AssertionError("HTTP forbidden")))
        self.stack.enter_context(mock.patch.object(stock.subprocess, "run", side_effect=AssertionError("process forbidden")))
        self.stack.enter_context(mock.patch.object(stock.subprocess, "Popen", side_effect=AssertionError("process forbidden")))

    def cli(self, *args):
        with mock.patch.object(sys, "argv", ["stock", *args]), contextlib.redirect_stdout(io.StringIO()) as output:
            stock.main()
        return json.loads(output.getvalue())

    def test_lhb_date_routes_as_date_once(self):
        with mock.patch.object(stock, "lhb", return_value={"date": "20260520"}) as lookup:
            result = self.cli("lhb", "20260520")
        lookup.assert_called_once_with(date="20260520", emit=False)
        self.assertEqual(result["date"], "20260520")

    def test_lhb_stock_routes_as_symbol_and_no_argument_is_overview(self):
        for code in ("000001", "600519", "sh000001", "sz000001"):
            with self.subTest(code=code), mock.patch.object(stock, "lhb", return_value={"symbol": code}) as lookup:
                self.cli("lhb", code)
                lookup.assert_called_once_with(symbol=code, emit=False)
        with mock.patch.object(stock, "lhb", return_value={"mode": "recent"}) as lookup:
            self.cli("lhb")
        lookup.assert_called_once_with(emit=False)

    def test_lhb_invalid_date_and_ambiguous_args_rejected_before_fetch(self):
        for args in (("20260230",), ("20261301",), ("2026052",), ("../x",), ("600519", "20260520")):
            with self.subTest(args=args), mock.patch.object(stock, "lhb") as lookup:
                self.assertIn("error", self.cli("lhb", *args))
                lookup.assert_not_called()

    def test_kline_wiring_retains_source_time_and_last_requested_rows(self):
        bars = [{"time": f"2026-09-0{day}", "open": 10, "close": 11, "high": 12, "low": 9, "volume": 1000, "amount": 11000} for day in range(1, 8)]
        payload = {"bars": bars, "source": "synthetic", "volume_unit": "share", "amount_unit": "CNY", "price_adjustment": "qfq", "cache_stale": False}
        with mock.patch.object(provider, "resolve_range", return_value=("2026-09-01", "2026-09-10")), mock.patch.object(provider, "load_bars", return_value=payload) as fetch:
            result = self.cli("kline", "600519", "5")
        fetch.assert_called_once_with("600519", "1d", "2026-09-01", "2026-09-10", timeout_seconds=5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["available_start"], "2026-09-03")
        self.assertEqual(result["available_end"], "2026-09-07")
        self.assertEqual(result["source"], "synthetic")
        self.assertEqual(result["volume_unit"], "share")

    def test_kline_partial_stale_and_empty_data_explicit(self):
        with mock.patch.object(provider, "resolve_range", return_value=("2026-09-01", "2026-09-10")), mock.patch.object(provider, "load_bars", return_value={"bars": [{"time": "2026-09-01"}], "source": "cache", "cache_stale": True}):
            result = self.cli("kline", "600519", "5")
        self.assertEqual(result["count"], 1)
        self.assertTrue(any("少于" in line for line in result["warnings"]))
        self.assertTrue(any("过期" in line for line in result["warnings"]))
        with mock.patch.object(provider, "load_bars", return_value={"bars": [], "source": "synthetic"}):
            result = self.cli("kline", "600519", "5")
        self.assertFalse(result["ok"])

    def test_kline_invalid_args_cannot_fetch(self):
        for args in ((), ("600519",), ("../../x", "5"), ("600519", "0"), ("600519", "-1"), ("600519", "1.5"), ("600519", "1001"), ("600519", "5", "extra")):
            with self.subTest(args=args), mock.patch.object(provider, "load_bars") as fetch:
                self.assertIn("error", self.cli("kline", *args))
                fetch.assert_not_called()

    def test_kline_outage_is_error_not_success(self):
        with mock.patch.object(provider, "load_bars", side_effect=RuntimeError("synthetic outage")):
            self.assertEqual(self.cli("kline", "600519", "5")["error"], "synthetic outage")

    def test_news_map_uses_original_text_and_returns_keywords(self):
        mapping = {"芯片": {"keywords": ["AI", "芯片"], "stocks": ["600000"]}}
        with mock.patch.object(stock, "_load_news_stock_map", return_value=mapping), mock.patch.object(stock, "_match_news_to_stocks", return_value=[{"新闻标题": "AI 新芯片"}]) as match:
            result = self.cli("news_map", "AI 新芯片")
        match.assert_called_once_with(["AI 新芯片"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["matched_keywords"], ["AI", "芯片"])
        self.assertIn("不等于买卖建议", result["note"])

    def test_news_map_missing_dictionary_or_invalid_text_does_not_invent_matches(self):
        with mock.patch.object(stock, "_load_news_stock_map", return_value={}):
            self.assertFalse(self.cli("news_map", "fixture")["ok"])
        for text in ("", "x" * 12001):
            with self.subTest(size=len(text)), mock.patch.object(stock, "_load_news_stock_map") as load:
                self.assertIn("error", self.cli("news_map", text))
                load.assert_not_called()

    def test_morning_news_limit_and_age_disclosure(self):
        with mock.patch.object(stock, "news", return_value=["合成新闻标题"]) as news, mock.patch.object(stock, "_load_news_stock_map", return_value={}), mock.patch.object(stock, "_match_news_to_stocks") as match:
            result = self.cli("morning_news", "10")
        news.assert_called_once_with(limit=10)
        match.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertFalse(result["mapping_available"])
        self.assertIsNone(result["published_at"])
        self.assertIn("+00:00", result["fetched_at"])
        self.assertTrue(any("不等于新闻发布时间" in line for line in result["warnings"]))

    def test_morning_news_missing_source_and_invalid_limit(self):
        with mock.patch.object(stock, "news", return_value=[]) as news:
            self.assertFalse(self.cli("morning_news")["ok"])
        news.assert_called_once_with(limit=10)
        for args in (("0",), ("51",), ("-1",), ("two",), ("10", "extra")):
            with self.subTest(args=args), mock.patch.object(stock, "news") as news:
                self.assertIn("error", self.cli("morning_news", *args))
                news.assert_not_called()

    def test_us_quote_alias_calls_existing_implementation(self):
        with mock.patch.object(stock, "quote_us", return_value={"代码": "NVDA", "数据源": "synthetic"}) as lookup:
            self.assertEqual(self.cli("us_quote", "NVDA")["代码"], "NVDA")
        lookup.assert_called_once_with("NVDA")

    def test_us_index_alias_calls_existing_implementation(self):
        with mock.patch.object(stock, "us_index", return_value=[{"代码": "^GSPC"}]) as lookup:
            self.assertEqual(self.cli("us_index")[0]["代码"], "^GSPC")
        lookup.assert_called_once_with()

    def test_us_alias_invalid_inputs_rejected_before_fetch(self):
        for command, args in (("us_quote", ()), ("us_quote", ("../x",)), ("us_quote", ("AAPL", "NVDA")), ("us_index", ("unexpected",))):
            with self.subTest(command=command, args=args), mock.patch.object(stock, "quote_us") as quote, mock.patch.object(stock, "us_index") as index:
                self.assertIn("error", self.cli(command, *args))
                quote.assert_not_called()
                index.assert_not_called()

    def test_other_commands_fall_through_unchanged(self):
        self.assertIsNone(stock._dispatch_auxiliary_market_command("watch", ["list"]))
        self.assertIn("未知命令", self.cli("invalid-command")["error"])


if __name__ == "__main__":
    unittest.main()
