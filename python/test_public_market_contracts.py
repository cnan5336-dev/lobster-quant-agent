import importlib.util
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PACKAGE_DIR = Path(__file__).resolve().parent / "lobster_quant_agent"
if PACKAGE_DIR.is_dir():
    sys.path.insert(0, str(PACKAGE_DIR))
    import cli as market
else:
    spec = importlib.util.spec_from_file_location("public_market_patch", Path(__file__).with_name("public_market_patch.py"))
    market = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(market)
    market.requests = mock.Mock()
    market.HEADERS = {"User-Agent": "synthetic-test"}
    market.re = re


class PublicUSQuoteTests(unittest.TestCase):
    def quote(self, meta, **extras):
        response = mock.Mock()
        response.json.return_value = {"chart": {"result": [{"meta": meta, **extras}]}}
        with mock.patch.object(market.requests, "get", return_value=response):
            return market.quote_us("AAPL")

    def test_previous_close_not_five_day_range_reference(self):
        row = self.quote({"regularMarketPrice": 110, "previousClose": 100, "chartPreviousClose": 80})
        self.assertEqual(row["涨跌幅%"], 10)
        self.assertEqual(row["昨收来源"], "meta.previousClose")

    def test_missing_previous_close_does_not_invent_daily_change(self):
        row = self.quote({"regularMarketPrice": 110, "chartPreviousClose": 80})
        self.assertIsNone(row["涨跌幅%"])

    def test_previous_session_close_can_be_derived(self):
        stamps = [datetime(2026, 9, d, 14, tzinfo=timezone.utc).timestamp() for d in (1, 2, 3)]
        row = self.quote({"regularMarketPrice": 110, "regularMarketTime": stamps[-1], "chartPreviousClose": 80}, timestamp=stamps, indicators={"quote": [{"close": [80, 100, 110]}]})
        self.assertEqual(row["涨跌幅%"], 10)
        self.assertIn("2026-09-03", row["数据时间"])

    def test_nan_and_malformed_price_not_propagated(self):
        self.assertIsNone(self.quote({"regularMarketPrice": float("nan"), "previousClose": 100})["最新价"])
        self.assertIsNone(self.quote({"regularMarketPrice": "bad", "previousClose": 100})["涨跌幅%"])

    def test_invalid_symbol_rejected_before_network(self):
        with mock.patch.object(market.requests, "get") as fetch:
            with self.assertRaises(ValueError):
                market.quote_us("../../bad")
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
