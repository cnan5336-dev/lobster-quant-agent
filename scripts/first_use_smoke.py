#!/usr/bin/env python3
"""Run a dependency-free first-use smoke test with synthetic data only."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "python" / "lobster_quant_agent"
sys.path.insert(0, str(CORE))


def synthetic_bars(count: int = 65):
    start = date(2026, 1, 1)
    bars = []
    for index in range(count):
        close = 10.2 + index * 0.08 + (0.12 if index % 6 == 0 else 0)
        bars.append({
            "time": (start + timedelta(days=index)).isoformat(),
            "open": round(close - 0.03, 4),
            "close": round(close, 4),
            "high": round(close + 0.09, 4),
            "low": round(close - 0.11, 4),
            "volume": 100000 + index * 1000,
            "amount": round(close * (100000 + index * 1000), 2),
        })
    return bars


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lobster-first-use-") as state_dir:
        os.environ["LOBSTER_QUANT_HOME"] = state_dir
        os.environ["LOBSTER_QUANT_NOTIFY_CHANNELS"] = ""
        os.environ["LOBSTER_QUANT_NOTIFY_TARGETS"] = "{}"

        import cli
        from backtest import engine
        from report_pipeline import render_report

        demo = cli.synthetic_demo()
        assert demo["ok"] and demo["mode"] == "synthetic_offline"
        assert demo["alert"]["fail_closed"] is True

        report_data = {
            "sections": {
                "今日盘前核心结论": "合成数据仅验证固定报告模板。",
                "隔夜/盘前重要消息": [{"title": "合成市场消息", "命中的题材": ["演示题材"]}],
                "热点题材方向": ["演示题材（合成）"],
                "观察池盘前预案": [],
                "持仓池盘前预案": [],
                "实时盯盘状态": {"状态": "关闭", "刷新频率_秒": 0, "提醒渠道": "未配置"},
                "需要回避的风险": ["合成数据不可用于投资决策"],
            }
        }
        rendered = render_report(report_data, "morning_report", variant="full", channel="telegram")
        assert "核心结论" in rendered["text"]
        assert "仅供研究参考" in rendered["text"]

        parsed = engine.parse_strategy("买入条件：收盘价高于10；卖出条件：超过买入价3%")
        assert parsed["ok"] is True
        data = {
            "bars": synthetic_bars(),
            "source": "synthetic_offline",
            "cache_path": None,
            "cache_time": None,
            "cache_stale": False,
        }
        with mock.patch.object(engine, "load_bars", return_value=data):
            backtest = engine.run_backtest(
                "CN-DEMO",
                days=60,
                interval="1d",
                strategy_override=parsed,
            )
        assert backtest["data_source"] == "synthetic_offline"
        assert backtest["回测指标"]["交易次数"] >= 1

        delivery = cli.send_openclaw_message("telegram", "synthetic offline smoke")
        assert delivery["ok"] is False
        assert delivery["skipped"] is True
        assert "未配置通知目标" in delivery["error"]

        print(json.dumps({
            "ok": True,
            "checks": {
                "synthetic_demo": "passed",
                "fixed_report_render": "passed",
                "synthetic_backtest": {
                    "passed": True,
                    "trade_count": backtest["回测指标"]["交易次数"],
                },
                "missing_target_fail_closed": "passed",
            },
            "network_requests": 0,
            "messages_sent": 0,
            "broker_actions": 0,
            "state_scope": "temporary directory removed on exit",
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
