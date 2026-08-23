#!/usr/bin/env python3
"""Generate the deterministic, privacy-safe README demo from synthetic JSON."""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "demo" / "synthetic-market-data.json"
OUTPUT = ROOT / "docs" / "demo" / "synthetic-session.svg"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def equity_path(values: list[float]) -> str:
    width, height = 346.0, 116.0
    low, high = min(values), max(values)
    span = max(high - low, 1.0)
    points = []
    for index, value in enumerate(values):
        x = 746 + (index / max(len(values) - 1, 1)) * width
        y = 417 - ((value - low) / span) * height
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def render(data: dict[str, object]) -> str:
    cn = data["a_share"]
    us = data["us_market"]
    alert = data["alert"]
    backtest = data["backtest"]
    path = equity_path(backtest["equity_curve"])
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="675" viewBox="0 0 1200 675" role="img" aria-labelledby="title desc">
  <title id="title">Lobster Quant Agent synthetic OpenClaw session</title>
  <desc id="desc">A fully synthetic market research, fail-closed alert, and backtest preview. It contains no real account, holdings, or notification target.</desc>
  <metadata>Generated from docs/demo/synthetic-market-data.json by scripts/generate_synthetic_demo.py.</metadata>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#08111f"/><stop offset="1" stop-color="#12233b"/></linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="10" stdDeviation="12" flood-opacity=".22"/></filter>
  </defs>
  <rect width="1200" height="675" rx="28" fill="url(#bg)"/>
  <circle cx="1080" cy="90" r="165" fill="#ff5a5f" opacity=".08"/>
  <circle cx="105" cy="620" r="210" fill="#48cae4" opacity=".06"/>
  <g font-family="Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif">
    <text x="60" y="72" fill="#ff767c" font-size="22" font-weight="700">🦞 LOBSTER QUANT AGENT</text>
    <text x="60" y="112" fill="#f8fafc" font-size="30" font-weight="700">OpenClaw 市场研究会话 · Synthetic Preview</text>
    <rect x="60" y="137" width="690" height="36" rx="18" fill="#17304a"/>
    <text x="78" y="161" fill="#9ddff0" font-size="15">100% 合成数据 · 无真实账户、持仓、聊天或通知目标</text>

    <g filter="url(#shadow)">
      <rect x="60" y="205" width="520" height="178" rx="18" fill="#101f32" stroke="#29435e"/>
      <text x="84" y="239" fill="#94a3b8" font-size="14">MARKET RESEARCH / 市场研究</text>
      <text x="84" y="278" fill="#f8fafc" font-size="24" font-weight="700">{esc(cn['symbol'])}</text>
      <text x="245" y="278" fill="#f8fafc" font-size="24" font-weight="700">¥{esc(cn['price'])}</text>
      <text x="370" y="278" fill="#58d68d" font-size="22" font-weight="700">{esc(cn['move'])}</text>
      <text x="84" y="308" fill="#a9b8ca" font-size="15">A 股合成样例 · {esc(cn['note'])}</text>
      <line x1="84" y1="329" x2="556" y2="329" stroke="#29435e"/>
      <text x="84" y="360" fill="#f8fafc" font-size="20" font-weight="700">{esc(us['symbol'])}</text>
      <text x="245" y="360" fill="#f8fafc" font-size="20" font-weight="700">${esc(us['price'])}</text>
      <text x="370" y="360" fill="#f6ad55" font-size="19" font-weight="700">{esc(us['move'])}</text>
      <text x="462" y="359" fill="#94a3b8" font-size="13">DELAYED</text>

      <rect x="610" y="205" width="530" height="178" rx="18" fill="#101f32" stroke="#29435e"/>
      <text x="634" y="239" fill="#94a3b8" font-size="14">ALERT SAFETY / 提醒安全</text>
      <circle cx="651" cy="278" r="9" fill="#f6ad55"/>
      <text x="674" y="285" fill="#f8fafc" font-size="21" font-weight="700">{esc(alert['signal'])}</text>
      <text x="634" y="324" fill="#ffcf7a" font-size="17">DELIVERY SKIPPED · 未发送</text>
      <text x="634" y="354" fill="#a9b8ca" font-size="15">{esc(alert['reason'])}</text>

      <rect x="60" y="413" width="1080" height="202" rx="18" fill="#101f32" stroke="#29435e"/>
      <text x="84" y="450" fill="#94a3b8" font-size="14">BACKTEST / 简化历史模拟</text>
      <text x="84" y="492" fill="#f8fafc" font-size="22" font-weight="700">{esc(backtest['strategy'])}</text>
      <text x="84" y="531" fill="#a9b8ca" font-size="15">样本</text><text x="150" y="531" fill="#f8fafc" font-size="18" font-weight="700">{esc(backtest['sessions'])} sessions</text>
      <text x="84" y="566" fill="#a9b8ca" font-size="15">交易</text><text x="150" y="566" fill="#f8fafc" font-size="18" font-weight="700">{esc(backtest['trades'])}</text>
      <text x="315" y="531" fill="#a9b8ca" font-size="15">合成收益</text><text x="412" y="531" fill="#58d68d" font-size="18" font-weight="700">{esc(backtest['return'])}</text>
      <text x="315" y="566" fill="#a9b8ca" font-size="15">最大回撤</text><text x="412" y="566" fill="#ff9b9f" font-size="18" font-weight="700">{esc(backtest['drawdown'])}</text>
      <line x1="746" y1="417" x2="1092" y2="417" stroke="#29435e" stroke-dasharray="5 7"/>
      <line x1="746" y1="359" x2="1092" y2="359" stroke="#29435e" stroke-dasharray="5 7"/>
      <polyline points="{path}" fill="none" stroke="#48cae4" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      <text x="746" y="572" fill="#94a3b8" font-size="13">Illustrative curve · not investment performance</text>
    </g>
    <text x="60" y="649" fill="#7890a8" font-size="13">OpenClaw-only · research &amp; alerts · no broker connection · no order execution</text>
  </g>
</svg>
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Fail if the committed SVG is stale.")
    args = parser.parse_args()
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    rendered = render(data)
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print("Synthetic demo is stale; run npm run demo:generate.", file=sys.stderr)
            return 1
        print("Synthetic demo is reproducible and up to date.")
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"Generated {OUTPUT.relative_to(ROOT)} from {SOURCE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
