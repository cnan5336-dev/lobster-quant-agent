import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lobster_quant_agent'))
#!/usr/bin/env python3
"""Public synthetic requests; stateful IO is isolated and all network blocked."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cli as stock


class StrategySemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in {
            "WATCHLIST_PATH": str(Path(self.tmp.name) / "watchlist.json"),
            "monitor_pid_status": mock.Mock(return_value={"running": False, "identity_verified": False}),
            "load_monitor_state": mock.Mock(return_value={}),
        }.items():
            patch = mock.patch.object(stock, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("build_strategy_snapshot", "_monitor_quote", "monitor_start", "monitor_notify_test"):
            if hasattr(stock, name):
                patch = mock.patch.object(stock, name, side_effect=AssertionError("Unexpected external IO"))
                patch.start()
                self.addCleanup(patch.stop)
        patch = mock.patch("socket.socket", side_effect=AssertionError("Unexpected network"))
        patch.start()
        self.addCleanup(patch.stop)

    def parse(self, text):
        parsed = stock.parse_strategy_text(text)
        self.assertTrue(parsed["ok"], parsed)
        return parsed

    def reject(self, text):
        result = stock.strategy_set(text)
        self.assertFalse(result["ok"], result)
        self.assertFalse(Path(stock.WATCHLIST_PATH).exists())
        self.assertTrue(result["parse_result"]["needs_clarification"])
        return result

    def test_repeated_field_preserves_each_condition(self):
        parsed = self.parse("510050价格高于3元且价格低于4元时提醒我")
        self.assertEqual([(x["operator"], x["value"]) for x in parsed["rules"][0]["conditions"]], [(">", 3), ("<", 4)])
        for price, expected in ((2, False), (3.5, True), (4, False)):
            self.assertEqual(stock.evaluate_strategy_rule(parsed["rules"][0], {"price": price})[0], expected)

    def test_cross_direction_is_local_to_each_indicator(self):
        rule = self.parse("510050一分钟MACD金叉并且五分钟KDJ死叉时提醒卖出")["rules"][0]
        self.assertEqual([(c["direction"], c["timeframe_minutes"]) for c in rule["conditions"]], [("golden_cross", 1), ("death_cross", 5)])
        self.assertEqual(rule["side"], "sell")

    def test_same_indicator_with_different_periods_is_not_dropped(self):
        rule = self.parse("5100501分钟RSI高于60并且5分钟RSI低于80时提醒我")["rules"][0]
        self.assertEqual(len(rule["conditions"]), 2)
        self.assertEqual([c["timeframe_minutes"] for c in rule["conditions"]], [1, 5])

    def test_multiple_symbols_get_independent_rules_with_common_conditions(self):
        parsed = self.parse("510050、510300价格高于3元时提醒我")
        self.assertEqual([r["code"] for r in parsed["rules"]], ["510050", "510300"])
        self.assertEqual(len({r["id"] for r in parsed["rules"]}), 2)
        self.assertTrue(all(r["scope"] == "explicit_symbols" for r in parsed["rules"]))
        self.assertEqual(parsed["targets"], [])

    def test_different_symbol_conditions_are_never_combined(self):
        self.reject("510050价格高于3元并且510300价格低于4元时提醒我")

    def test_pool_scope_is_preserved(self):
        parsed = self.parse("持仓池和观察池换手率超过3%时提醒我")
        self.assertEqual(parsed["targets"], ["holding_pool", "watch_pool"])
        self.assertIsNone(parsed["rules"][0]["code"])
        self.assertEqual(parsed["rules"][0]["scope"], "pools")
        self.reject("持仓池510050价格高于3元时提醒我")

    def test_or_is_preserved(self):
        rule = self.parse("510050价格高于3元或者换手率超过3%时提醒我")["rules"][0]
        self.assertEqual(rule["logic"], "any")
        self.assertTrue(stock.evaluate_strategy_rule(rule, {"price": 2, "turnover_rate_percent": 4})[0])

    def test_mixed_logic_and_parentheses_require_clarification(self):
        for text in ("510050价格高于3元且RSI高于60或者换手率高于3%时提醒我",
                     "510050价格高于3元且（RSI高于60或者换手率高于3%）时提醒我"):
            with self.subTest(text=text):
                self.reject(text)

    def test_inclusive_and_strict_operators_and_units(self):
        for words, op, equal in (("高于", ">", False), ("超过", ">", False), ("不低于", ">=", True),
                                 ("大于或等于", ">=", True), ("不超过", "<=", True), ("低于", "<", False), ("等于", "==", True)):
            with self.subTest(words=words):
                rule = self.parse(f"510050价格{words}3元时提醒我")["rules"][0]
                self.assertEqual(rule["conditions"][0]["operator"], op)
                self.assertEqual(stock.evaluate_strategy_rule(rule, {"price": 3})[0], equal)
        self.reject("510050价格高于3%时提醒我")
        self.reject("510050换手率高于3元时提醒我")

    def test_price_and_percent_are_not_confused(self):
        price = self.parse("510050价格涨到3元时提醒我")["rules"][0]["conditions"][0]
        rise = self.parse("510050涨幅超过3%时提醒我")["rules"][0]["conditions"][0]
        fall = self.parse("510050跌幅超过3%时提醒我")["rules"][0]["conditions"][0]
        self.assertEqual((price["field"], price["operator"], price["value"]), ("price", ">=", 3))
        self.assertEqual((rise["field"], rise["operator"], rise["value"]), ("change_percent", ">", 3))
        self.assertEqual((fall["field"], fall["operator"], fall["value"]), ("change_percent", "<", -3))

    def test_unknown_conditions_never_disappear_even_without_connector(self):
        for text in ("510050价格高于3元出现神秘形态时提醒我", "510050价格高于3元并且RSI变强时提醒我",
                     "510050价格高于3元但不要卖出", "510050价格高于3元且不出现MACD金叉时提醒我",
                     "510050价格高于3元持续5分钟时提醒我", "510050价格高于3元就自动下单"):
            with self.subTest(text=text):
                self.reject(text)

    def test_qualitative_defaults_and_crossing_are_not_invented(self):
        for text in ("510050放量时提醒我", "510050买盘占优时提醒我", "510050价格突破3元时提醒我",
                     "510050价格站上5日均线时提醒我"):
            with self.subTest(text=text):
                self.reject(text)

    def test_daily_ma_uses_daily_bars_and_no_accidental_price_rule(self):
        rule = self.parse("510050价格高于20日均线时提醒我")["rules"][0]
        self.assertEqual(rule["conditions"], [{"type": "price_vs_ma", "timeframe_minutes": 1440, "period": 20, "operator": ">"}])

    def test_timeframes_are_explicit_and_validated(self):
        for text, minutes in (("5100505分钟MACD金叉时提醒我", 5), ("510050MACD3分钟金叉时提醒我", 3),
                              ("510050日线RSI高于60时提醒我", 1440)):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)["rules"][0]["conditions"][0]["timeframe_minutes"], minutes)
        for text in ("51005015分钟MACD金叉时提醒我", "510050日线价格高于3元时提醒我",
                     "5100505分钟价格高于20日均线时提醒我", "5100505分钟MACD金叉且RSI高于60时提醒我"):
            with self.subTest(text=text):
                self.reject(text)

    def test_range_validation(self):
        for text in ("510050RSI高于101时提醒我", "510050价格高于0元时提醒我", "510050量比高于-1时提醒我",
                     "510050成交量超过过去0天均量的2倍时提醒我", "510050价格高于MA7时提醒我",
                     "510050成交量超过过去5天均量的-2倍时提醒我", "510050价格高于" + "9" * 400 + "元时提醒我"):
            with self.subTest(text=text):
                self.reject(text)

    def test_volume_operator_is_not_changed_to_inclusive(self):
        rule = self.parse("持仓池成交量超过过去5天均量的2倍时提醒买入")["rules"][0]
        self.assertEqual(rule["conditions"][0]["operator"], ">")
        self.assertFalse(stock.evaluate_strategy_rule(rule, {"bars": {1440: [{"volume": 10}] * 5 + [{"volume": 20}]}})[0])

    def test_neutral_alert_and_readable_confirmation(self):
        result = stock.strategy_dry_run("510050价格高于3元时提醒我")
        self.assertEqual(result["draft"]["rules"][0]["side"], "alert")
        self.assertIn("实时价格>3元", result["confirmation"][0])
        self.assertIn("条件提醒", result["confirmation"][0])
        self.assertNotIn("买入提醒", result["confirmation"][0])
        self.assertFalse(Path(stock.WATCHLIST_PATH).exists())

    def test_default_timeframe_and_pool_are_disclosed(self):
        parsed = self.parse("RSI高于60时提醒我")
        self.assertIn("未指定标的或股票池，按持仓池执行", parsed["warnings"])
        self.assertIn("未指定指标周期，按1分钟计算", parsed["warnings"])

    def test_existing_saved_dsl_evaluates_and_explains(self):
        cfg = stock._default_watchlist_config()
        legacy = {"id": "old", "code": "510050", "side": "buy", "logic": "all",
                  "conditions": [{"type": "quote_threshold", "field": "price", "operator": ">", "value": 3}], "raw_text": "旧策略"}
        cfg["strategy_monitor"]["rules"] = [legacy]
        stock.save_watchlist_config(cfg)
        self.assertTrue(stock.evaluate_strategy_rule(stock.strategy_list()["rules"][0], {"price": 4})[0])
        self.assertIn("买入提醒", stock.strategy_explain("old")["explanation"])

    def test_failed_replacement_preserves_existing_config_byte_for_byte(self):
        created = stock.strategy_set("510050价格高于3元时提醒我")
        self.assertTrue(created["ok"])
        before = Path(stock.WATCHLIST_PATH).read_bytes()
        rejected = stock.strategy_set("510050价格高于3元且RSI神秘形态时提醒我")
        self.assertFalse(rejected["ok"])
        self.assertEqual(Path(stock.WATCHLIST_PATH).read_bytes(), before)
        self.assertIn("实时价格>3元", stock.strategy_explain(created["strategy"]["rules"][0]["id"])["explanation"])

    def test_nl_dry_run_is_routed_without_runtime_or_model(self):
        result = stock.handle_natural_language_command("试解析策略：510050价格高于3元时提醒我")
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(Path(stock.WATCHLIST_PATH).exists())


if __name__ == "__main__":
    unittest.main()
