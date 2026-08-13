# 🦞 Lobster Quant Agent

**Turn OpenClaw into a multi-channel market-research assistant for A-shares and US-market snapshots.** Lobster Quant Agent combines quotes, reports, alerts, natural-language strategy monitoring, and backtesting behind one OpenClaw tool—without broker access or order execution.

> Research and alerts only. This project does not provide investment advice, connect to a broker, or place, modify, or cancel orders.

## What it does

- Runs inside **OpenClaw** and answers through Telegram or the OpenClaw WeChat extension; QQ is supported through the optional `qqbot` extension.
- Queries A-share quotes, major indices, K-lines, 龙虎榜 data, market breadth, and delayed US stock/index snapshots.
- Maps market news to themes and representative companies, then builds morning reports and post-market reviews.
- Maintains private local watchlist and holdings labels, with compact pool snapshots.
- Supports ordinary threshold alerts and natural-language strategy monitoring.
- Parses natural-language backtests for daily, 5-minute, and 1-minute bars, including fees, slippage, stop conditions, trades, performance metrics, and local HTML charts.
- Falls back across public data sources and a private local cache; optional model-assisted tasks can use a configured OpenClaw model chain.

All watchlists, holdings labels, alert state, caches, and backtest results stay in the user's configured local state directory and are excluded from Git.

## Install

Requirements: macOS or Linux, OpenClaw `>=2026.5.17`, Node.js `>=22.22.3`, Python `>=3.10`, and Git.

```bash
git clone https://github.com/cnan5336-dev/lobster-quant-agent.git
cd lobster-quant-agent
./scripts/install.sh
```

The installer is idempotent for the same checkout. It creates a repository-local Python virtual environment, installs dependencies, validates the plugin, links it into OpenClaw, creates an ignored local config copy, and writes only non-secret plugin paths/defaults to OpenClaw. It does **not** send a message, start monitoring, configure credentials, or make a trade.

Prefer an installation agent? After cloning, give [INSTALL_WITH_AGENT.md](INSTALL_WITH_AGENT.md) directly to Codex or Claude Code. They are setup/development assistants; **OpenClaw remains the v1 runtime** for channel operation.

## Configure

The safe template is [config/lobster-quant-agent.example.json](config/lobster-quant-agent.example.json). The installer creates the ignored `config/lobster-quant-agent.local.json`; it is a reference for your choices, not a secret store.

1. Configure Telegram, WeChat, or QQ in OpenClaw using your own credentials. Prefer OpenClaw SecretRefs or its interactive channel setup so tokens are not committed or left in shell history.
2. Set `defaultChannel` to `telegram`, `weixin`, or `qq`.
3. Optionally set OpenClaw model keys in `primaryModel` and `fallbackModels`.
4. Leave `notifyChannels` empty until proactive alerts are wanted. Then add only channels with an explicit local `notificationTargets` entry.
5. Validate without delivery:

```bash
openclaw config validate
openclaw plugins inspect lobster-quant-agent --runtime --json
./scripts/validate.sh
```

Never commit local OpenClaw configuration, notification targets, credentials, channel/account identifiers, state, caches, logs, screenshots, or generated results.

## Ask it

Once the plugin is enabled and the chosen OpenClaw channel works, examples include:

```text
查一下 600519 行情
查看美股 AAPL 行情
查看美股指数
查今天龙虎榜
给我一份盘前简报
来个完整版盘后复盘
观察池加入 600519 贵州茅台
查看全部池行情快照
设置策略盯盘：510050 一分钟 MACD 金叉时提醒
买入条件：收盘价站上20日均线；卖出条件：跌破买入价5%。回测510050最近60天，日线
生成回测图形
```

OpenClaw calls the `lobster_quant` tool. Deterministic CLI commands are also available to contributors through `python/lobster_quant_agent/cli.py`; that CLI is an internal plugin layer, not a supported standalone runtime.

## Architecture

```text
Telegram / WeChat / QQ
          │
       OpenClaw
          │  lobster_quant tool
  TypeScript plugin adapter
          │  explicit argv + private env config
  Python research core + CLI
      ├── A-share / US quote adapters
      ├── reports + channel renderers
      ├── watchlist / strategy monitor
      ├── natural-language backtest engine
      └── fallback data sources + local cache
```

The adapter/core boundary keeps the implementation maintainable, but v1 depends on OpenClaw and is not marketed or tested as a general cross-agent runtime.

## Commands for contributors

```bash
npm install --omit=peer
python3 -m venv .venv
.venv/bin/pip install -r python/requirements.txt
./scripts/validate.sh
```

The validation suite compiles Python, runs deterministic unit tests, checks a no-write strategy dry run in a temporary directory, builds and validates OpenClaw metadata, runs TypeScript tests, and performs a privacy audit.

## Data, privacy, and limitations

- Public market endpoints can be delayed, unavailable, rate-limited, or structurally changed. The US snapshot adapter uses Yahoo Finance's public chart endpoint and should be treated as delayed research data.
- 龙虎榜 and some breadth/news workflows depend on AkShare and its upstream sources.
- Minute history can be shorter than requested. Cache fallback can be stale and is labeled in output.
- Backtests are simplified simulations; they do not fully model liquidity, price limits, corporate actions, or real execution.
- Monitoring is local and opt-in. Delivery fails closed when a channel target is missing.
- The current monitor uses POSIX file locks, so Windows is not supported in v1.
- Screenshots are intentionally omitted because real chat captures can expose account and holdings data. All examples above are synthetic.

See [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), and [DISCLAIMER.md](DISCLAIMER.md). Licensed under the [MIT License](LICENSE).
