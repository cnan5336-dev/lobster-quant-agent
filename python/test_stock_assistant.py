#!/usr/bin/env python3
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

PACKAGE_DIR = Path(__file__).resolve().parent / "lobster_quant_agent"
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_DIR))

import cli as stock
import http_client
from report_pipeline import (
    SCHEMA_VERSION,
    build_after_close_analysis,
    build_morning_analysis,
    parse_analysis_candidates,
    render_report,
)


class ReleaseMetadataTests(unittest.TestCase):
    def test_package_manifest_lock_and_python_versions_match(self):
        package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
        manifest = json.loads(
            (REPO_ROOT / "openclaw.plugin.json").read_text(encoding="utf-8")
        )
        package_lock = json.loads(
            (REPO_ROOT / "package-lock.json").read_text(encoding="utf-8")
        )
        python_init = (PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']$', python_init, re.M)

        self.assertIsNotNone(match)
        expected = package["version"]
        self.assertEqual(expected, "0.2.0-rc.2")
        self.assertEqual(manifest["version"], expected)
        self.assertEqual(package_lock["version"], expected)
        self.assertEqual(package_lock["packages"][""]["version"], expected)
        self.assertEqual(match.group(1), expected)


class StrategyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = stock.WATCHLIST_PATH
        self.original_pid_status = stock.monitor_pid_status
        stock.WATCHLIST_PATH = str(Path(self.tempdir.name) / "market_watchlist.json")
        stock.monitor_pid_status = lambda: {
            "running": False,
            "identity_verified": False,
        }

    def tearDown(self):
        stock.WATCHLIST_PATH = self.original_path
        stock.monitor_pid_status = self.original_pid_status
        self.tempdir.cleanup()

    def test_five_natural_language_strategies_produce_dsl(self):
        cases = [
            "如果510050一分钟MACD金叉，就提醒我买入信号",
            "持仓池成交量超过过去5天均量的2倍时提醒买入",
            "观察池买盘强于卖盘1.5倍时提醒买入",
            "如果510050一分钟RSI高于60就提醒买入",
            "持仓池换手率超过3%时提醒买入",
        ]
        for text in cases:
            with self.subTest(text=text):
                result = stock.strategy_dry_run(text)
                self.assertTrue(result["ok"])
                self.assertEqual(result["draft"]["schema_version"], "strategy-dsl/v1")
                self.assertFalse(result["write_result"]["ok"])

    def test_dry_run_does_not_create_config(self):
        result = stock.strategy_dry_run("510050价格高于3元时提醒买入")
        self.assertTrue(result["ok"])
        self.assertFalse(Path(stock.WATCHLIST_PATH).exists())

    def test_set_reload_and_verify(self):
        result = stock.strategy_set("510050价格高于3元时提醒买入")
        self.assertTrue(result["ok"])
        self.assertTrue(result["parse_result"]["ok"])
        self.assertTrue(result["write_result"]["ok"])
        self.assertTrue(result["reload_result"]["ok"])
        self.assertTrue(result["effective_status"]["config_effective"])
        self.assertTrue(result["verification_result"]["ok"])

    def test_list_explain_and_offline_test(self):
        created = stock.strategy_set("510050价格高于3元时提醒买入")
        rule_id = created["strategy"]["rules"][0]["id"]
        self.assertEqual(stock.strategy_list()["count"], 1)
        self.assertTrue(stock.strategy_explain(rule_id)["ok"])
        tested = stock.strategy_test(rule_id, live_probe=False)
        self.assertTrue(tested["ok"])
        self.assertFalse(tested["verification_result"]["real_signal_emitted"])

    def test_unrecognized_condition_never_writes(self):
        result = stock.strategy_set("510050出现神秘形态时自动提醒")
        self.assertFalse(result["ok"])
        self.assertFalse(Path(stock.WATCHLIST_PATH).exists())

    def test_mixed_unknown_clause_is_rejected(self):
        result = stock.strategy_dry_run(
            "510050价格高于3元并且出现神秘形态时提醒买入"
        )
        self.assertFalse(result["ok"])
        self.assertIn("无法识别条件片段", " ".join(result["parse_result"]["unavailable_conditions"]))

    def test_or_logic_is_preserved_and_evaluated(self):
        parsed = stock.parse_strategy_text(
            "510050价格高于3元或者换手率超过3%时提醒买入"
        )
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["rules"][0]["logic"], "any")
        snapshot = stock._strategy_mock_snapshot(
            "510050", mock_text="price=2.5,turnover_rate_percent=4"
        )
        matched, _ = stock.evaluate_strategy_rule(parsed["rules"][0], snapshot)
        self.assertTrue(matched)


class ReportPipelineTests(unittest.TestCase):
    def setUp(self):
        self.morning = {
            "sections": {
                "今日盘前核心结论": "关注政策催化与开盘承接。",
                "隔夜/盘前重要消息": [
                    {"新闻标题": "产业政策更新", "命中的题材": ["算力"]},
                    {"新闻标题": "海外市场震荡", "命中的题材": []},
                ],
                "热点题材方向": ["算力", "半导体"],
                "观察池盘前预案": [{"股票": "样本A 600000", "盘前看点": "观察承接"}],
                "持仓池盘前预案": [{"股票": "样本B 000001", "盘前看点": "风险优先"}],
                "实时盯盘状态": {"状态": "关闭", "刷新频率_秒": 20, "提醒渠道": "telegram"},
                "需要回避的风险": ["高开低走"],
            }
        }
        self.after = {
            "sections": {
                "简短判断": "市场震荡，结构性机会为主。",
                "指数概览": [{"名称": "上证指数", "代码": "000001", "最新价": 3000, "涨跌幅%": 0.5}],
                "全A市场概览": {"市场宽度": {"上涨家数": 3000, "下跌家数": 2000, "涨停数量_估算": 50, "跌停数量_估算": 8}},
                "观察池重点跟踪": [{"名称": "样本A", "代码": "600000", "最新价": 10, "涨跌幅%": 1.2}],
                "持仓池重点跟踪": [{"名称": "样本B", "代码": "000001", "最新价": 12, "涨跌幅%": -0.3}],
                "龙虎榜摘要": "净买入方向偏科技。",
                "实时盯盘状态": {"状态": "关闭", "刷新频率_秒": 20, "提醒渠道": "telegram"},
            }
        }

    def test_morning_full_report(self):
        rendered = render_report(self.morning, "morning_report", "full", "weixin")
        self.assertIn("【先看结论】", rendered["text"])
        self.assertIn("【风险清单】", rendered["text"])

    def test_morning_simple_report(self):
        analysis = build_morning_analysis(self.morning, "simple")
        self.assertEqual(analysis["variant"], "simple")
        self.assertNotIn("risks", [section["id"] for section in analysis["sections"]])

    def test_after_close_full_report(self):
        rendered = render_report(self.after, "after_close_report", "full", "telegram")
        self.assertIn("# A股盘后复盘", rendered["text"])
        self.assertIn("## 龙虎榜与资金", rendered["text"])

    def test_after_close_simple_report(self):
        analysis = build_after_close_analysis(self.after, "simple")
        self.assertEqual(analysis["variant"], "simple")
        self.assertNotIn("lhb", [section["id"] for section in analysis["sections"]])

    def test_missing_data_fallback_and_invalid_model_json(self):
        analysis = build_after_close_analysis({}, "full")
        self.assertEqual(analysis["status"], "WARN")
        fallback, meta = parse_analysis_candidates(
            ["not-json", '{"sections": []}'], "after_close_report", "full"
        )
        self.assertEqual(fallback["schema_version"], SCHEMA_VERSION)
        self.assertTrue(meta["fallback_used"])

    def test_application_report_wiring_uses_fixed_text(self):
        with mock.patch.object(stock, "morning_report", return_value=self.morning):
            result = stock.report_output("morning_report", "simple", "weixin")
        self.assertTrue(result["ok"])
        self.assertEqual(result["_output_format"], "text")
        self.assertIn("【先看结论】", result["text"])

    def test_report_cli_options_preserve_channel_and_variant(self):
        options = stock._report_cli_options(
            ["--channel", "weixin", "--simple", "--json"]
        )
        self.assertEqual(options["channel"], "weixin")
        self.assertEqual(options["variant"], "simple")
        self.assertTrue(options["raw_json"])

    def test_qq_uses_compact_plain_text_renderer(self):
        rendered = render_report(self.morning, "morning_report", "simple", "qq")
        self.assertIn("【先看结论】", rendered["text"])
        self.assertIn("仅供研究参考", rendered["text"])

    def test_us_quote_normalizes_public_chart_payload(self):
        response = mock.Mock()
        response.json.return_value = {
            "chart": {
                "result": [{
                    "meta": {
                        "regularMarketPrice": 201.5,
                        "chartPreviousClose": 200,
                        "currency": "USD",
                        "exchangeName": "NMS",
                    }
                }]
            }
        }
        with mock.patch.object(stock.requests, "get", return_value=response):
            result = stock.quote_us("aapl")
        self.assertEqual(result["代码"], "AAPL")
        self.assertEqual(result["涨跌幅%"], 0.75)

    def test_synthetic_demo_is_offline_and_fail_closed(self):
        result = stock.synthetic_demo()
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "synthetic_offline")
        self.assertTrue(result["alert"]["fail_closed"])

    def test_notifications_fail_closed_without_local_target(self):
        with mock.patch.dict("os.environ", {"LOBSTER_QUANT_NOTIFY_TARGETS": "{}"}, clear=False):
            result = stock.send_openclaw_message("telegram", "synthetic test")
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])


class PackagedHttpClientTests(unittest.TestCase):
    def test_response_decodes_json_without_requests_dependency(self):
        response = http_client.Response(b'{"ok": true}', 200)
        self.assertEqual(response.json(), {"ok": True})
        response.raise_for_status()

    def test_http_error_is_raised_only_after_status_check(self):
        response = http_client.Response(b"unavailable", 503)
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            response.raise_for_status()


if __name__ == "__main__":
    unittest.main()
