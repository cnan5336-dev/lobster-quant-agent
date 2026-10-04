#!/usr/bin/env python3
"""Public synthetic strategy clauses; parsing/evaluation only, no external IO."""
import unittest
from unittest import mock

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import cli as stock


class StrategyThresholdEllipsisTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "cli.load_watchlist_config", "cli.build_strategy_snapshot"):
            patch = mock.patch(target, side_effect=AssertionError("Unexpected IO during semantic parsing"))
            patch.start()
            self.addCleanup(patch.stop)

    def rule(self, text):
        parsed = stock.parse_strategy_text(text)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["rules"][0]["raw_text"], text)
        return parsed["rules"][0]

    def test_price_range_inherits_only_omitted_second_subject(self):
        text = "510050价格高于3元且低于4元时提醒"
        rule = self.rule(text)
        self.assertEqual(rule["conditions"], [
            {"type": "quote_threshold", "field": "price", "operator": ">", "value": 3},
            {"type": "quote_threshold", "field": "price", "operator": "<", "value": 4},
        ])
        for price, matched in ((2.9, False), (3, False), (3.5, True), (4, False), (4.1, False)):
            with self.subTest(price=price):
                self.assertEqual(stock.evaluate_strategy_rule(rule, {"price": price})[0], matched)
        confirmation = stock.parse_strategy_text(text)["confirmation"][0]
        self.assertIn("实时价格>3元；实时价格<4元", confirmation)

    def test_inclusive_thresholds_and_ascii_operators(self):
        for text in (
            "510050价格不低于3元且不超过4元时提醒",
            "510050价格>=3元AND<=4元时提醒",
            "510050现价≥3元且≤4元时提醒",
        ):
            with self.subTest(text=text):
                rule = self.rule(text)
                self.assertEqual([c["operator"] for c in rule["conditions"]], [">=", "<="])
                for price in (3, 4):
                    self.assertTrue(stock.evaluate_strategy_rule(rule, {"price": price})[0])

    def test_or_inheritance_preserves_boolean_logic(self):
        for connector in ("或", "或者", "OR"):
            with self.subTest(connector=connector):
                rule = self.rule(f"510050股价低于3元{connector}高于4元时提醒")
                self.assertEqual(rule["logic"], "any")
                for price, matched in ((2, True), (3.5, False), (5, True)):
                    self.assertEqual(stock.evaluate_strategy_rule(rule, {"price": price})[0], matched)

    def test_explicit_subject_switch_prevents_price_inheritance(self):
        rule = self.rule("510050价格高于3元且RSI高于60且低于80时提醒")
        self.assertEqual([c.get("field") or c.get("indicator") for c in rule["conditions"]], ["price", "rsi", "rsi"])
        self.assertEqual(rule["conditions"][2]["value"], 80)
        turnover = self.rule("510050价格高于3元且换手率高于2%且低于5%时提醒")
        self.assertEqual([c.get("field") for c in turnover["conditions"]], ["price", "turnover_rate_percent", "turnover_rate_percent"])

    def test_indicator_timeframe_is_retained(self):
        rule = self.rule("5100505分钟RSI高于60且低于80时提醒")
        self.assertEqual([c["timeframe_minutes"] for c in rule["conditions"]], [5, 5])
        self.assertEqual([c["operator"] for c in rule["conditions"]], [">", "<"])

    def test_decline_subject_retains_signed_percent_meaning(self):
        rule = self.rule("510050跌幅高于3%且低于5%时提醒")
        self.assertEqual([(c["field"], c["operator"], c["value"]) for c in rule["conditions"]],
                         [("change_percent", "<", -3), ("change_percent", ">", -5)])
        self.assertTrue(stock.evaluate_strategy_rule(rule, {"change_percent": -4})[0])
        self.assertFalse(stock.evaluate_strategy_rule(rule, {"change_percent": 4})[0])

    def test_ellipsis_does_not_bypass_units_ranges_or_unknown_conditions(self):
        cases = (
            "510050低于4元时提醒",  # No previous explicit subject.
            "510050价格高于3元且低于4%时提醒",
            "510050价格高于3元且RSI高于60且低于4元时提醒",
            "510050价格高于3元且MACD金叉且低于4元时提醒",
            "510050成交量超过过去5天均量的2倍且低于4元时提醒",
            "510050RSI高于60且低于101时提醒",
            "510050价格高于3元且低于4元出现神秘形态时提醒",
            "510050价格高于3元且神秘值低于4元时提醒",
            "510050价格高于3元且5分钟低于4元时提醒",
            "510050价格高于3元且低于4元或RSI高于60时提醒",
        )
        for text in cases:
            with self.subTest(text=text):
                parsed = stock.parse_strategy_text(text)
                self.assertFalse(parsed["ok"], parsed)
                self.assertEqual(parsed["rules"], [])


if __name__ == "__main__":
    unittest.main()
