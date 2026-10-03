import sys
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent / "lobster_quant_agent"
if PACKAGE_DIR.is_dir():
    sys.path.insert(0, str(PACKAGE_DIR))
from backtest import engine


class BacktestBoundaryTests(unittest.TestCase):
    def test_threshold_words_preserve_strictness(self):
        examples = [
            ("成交量超过过去5天均量的2倍", ">", "buy"),
            ("成交量高于过去5天均量的2倍", ">", "buy"),
            ("成交量达到过去5天均量的2倍", ">=", "buy"),
            ("成交量不少于过去5天均量的2倍", ">=", "buy"),
            ("成交量低于过去5天均量的2倍", "<", "buy"),
            ("单日涨幅超过3%", ">", "buy"),
            ("单日涨幅达到3%", ">=", "buy"),
            ("RSI高于60", ">", "buy"), ("RSI达到60", ">=", "buy"),
            ("价格高于10元", ">", "buy"), ("价格不低于10元", ">=", "buy"),
            ("价格跌破10元", "<", "sell"), ("价格不高于10元", "<=", "sell"),
            ("盈利超过3%", ">", "sell"), ("盈利达到3%", ">=", "sell"),
            ("亏损超过3%", "<", "sell"), ("亏损达到3%", "<=", "sell"),
            ("止损3%", "<=", "sell"),
        ]
        for text, op, side in examples:
            with self.subTest(text=text):
                parsed = engine._parse_atom(text, side)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["operator"], op)

    def test_volume_exactly_two_times_not_over_two_times(self):
        bars = [{"close":10,"volume":100} for _ in range(5)] + [{"close":10,"volume":200}]
        strict = engine._parse_atom("成交量超过过去5天均量的2倍", "buy")
        inclusive = engine._parse_atom("成交量达到过去5天均量的2倍", "buy")
        self.assertFalse(engine._evaluate(strict, bars, 5, None)[0])
        self.assertTrue(engine._evaluate(inclusive, bars, 5, None)[0])

    def test_percent_boundary_not_shifted_by_binary_float(self):
        for close, strict, inclusive in ((103,"盈利超过3%","盈利达到3%"),(97,"亏损超过3%","亏损达到3%")):
            bars = [{"close":close}]
            self.assertFalse(engine._evaluate(engine._parse_atom(strict,"sell"), bars, 0, 100)[0])
            self.assertTrue(engine._evaluate(engine._parse_atom(inclusive,"sell"), bars, 0, 100)[0])

    def test_daily_percent_threshold_boundary(self):
        bars = [{"close":100}, {"close":103}]
        self.assertFalse(engine._evaluate(engine._parse_atom("单日涨幅超过3%","buy"), bars, 1, None)[0])
        self.assertTrue(engine._evaluate(engine._parse_atom("单日涨幅达到3%","buy"), bars, 1, None)[0])


if __name__ == "__main__":
    unittest.main()
