# 🦞 Lobster Quant Agent

**把 OpenClaw 变成面向 A 股与美股的本地研究、提醒和回测助手，同时不接券商、不下单。**

**Turn OpenClaw into a local A-share and US-market research, alert, and backtesting assistant—without broker access or order execution.**

[简体中文](#中文简介) · [English](#english-overview) · [安装](#安装) · [Install](#install) · [安全与隐私](#安全与隐私--safety--privacy)

> OpenClaw is the only supported runtime. This project provides research and alerts, not investment advice. It cannot place, modify, or cancel orders.

![Lobster Quant Agent synthetic OpenClaw session](docs/demo/synthetic-session.svg)

The preview above is generated from [purpose-built synthetic data](docs/demo/README.md). It contains no real account, holding, watchlist, chat identity, notification target, or live result.

## 中文简介

Lobster Quant Agent 将行情查询、报告、条件提醒、自然语言策略盯盘和历史回测收进一个 OpenClaw 工具 `lobster_quant`。它适合已经使用 OpenClaw、希望通过聊天渠道完成研究工作流，但不希望工具触碰券商账户或交易权限的用户。

| 能力 | 当前范围 |
| --- | --- |
| A 股研究 | 个股/指数行情、K 线、龙虎榜、市场宽度、盘前简报、盘后复盘 |
| 美股研究 | 延迟的美股个股与指数快照；只作为研究参考 |
| 提醒与盯盘 | 普通阈值提醒、自然语言策略监控；未配置明确目标时拒绝发送 |
| 回测 | 日线、5 分钟、1 分钟；费用、滑点、止损、交易明细、指标与本地 HTML 图表 |
| 对话渠道 | Telegram、OpenClaw 微信扩展、可选 `qqbot` QQ 扩展 |
| 隐私 | 观察池、持仓标签、提醒状态、缓存和回测结果保存在用户指定的本地私有目录 |
| 交易边界 | 无券商连接、无交易接口、无下单/改单/撤单能力 |

### 它刻意不做什么

- 不把 Codex、Claude Code 或 Python CLI 宣传为独立运行时；它们只可协助安装和开发。
- 不接收、恢复或托管用户的频道凭据、账户标识或通知目标。
- 不在安装或验证时发送消息、启动监控、调用模型 ping 或执行任何交易动作。
- 不把回测、延迟行情或缓存数据包装成实时信号、收益承诺或个性化投资建议。

## 安装

稳定版 `0.2.0` 要求 macOS 或 Linux、OpenClaw `>=2026.5.17`、Node.js `>=22.22.3`、Python `>=3.10`。源码安装还需要 npm 和 Git。详见 [兼容性说明](COMPATIBILITY.md)。

推荐从 ClawHub 安装稳定版：

```bash
openclaw plugins install clawhub:@cnan5336-dev/lobster-quant-agent
```

也可以从公开源码安装：

```bash
git clone https://github.com/cnan5336-dev/lobster-quant-agent.git
cd lobster-quant-agent
./scripts/install.sh
```

需要复现候选版时应显式选择对应的 rc 版本；默认安装入口跟随稳定 `latest`。

安装脚本对同一 checkout 可重复执行。它会创建仓库内的 Python 虚拟环境、安装依赖、验证并链接插件、生成被 Git 忽略的本地配置副本，并只向 OpenClaw 写入非敏感的插件路径与安全默认值。它不会发送消息、启动监控、配置凭据或执行交易。

希望由安装助手带着完成？克隆后把 [INSTALL_WITH_AGENT.md](INSTALL_WITH_AGENT.md) 交给 Codex 或 Claude Code。涉及凭据、通知目标或真实外部发送时，助手必须停下并交还给用户确认。

### 配置

安全模板位于 [config/lobster-quant-agent.example.json](config/lobster-quant-agent.example.json)。安装器会生成被忽略的 `config/lobster-quant-agent.local.json`，它只用于记录本地选择，不应成为密钥存储。

1. 使用你自己的凭据，在 OpenClaw 内配置 Telegram、微信或 QQ。优先使用 OpenClaw SecretRefs 或交互式频道配置。
2. 将 `defaultChannel` 设为 `telegram`、`weixin` 或 `qq`。
3. 可选：填写 OpenClaw 模型键 `primaryModel` 与最多两个 `fallbackModels`。
4. 主动提醒默认关闭。只有在明确配置本地 `notificationTargets` 后，才把对应频道加入 `notifyChannels`。
5. 先做无发送验证：

```bash
openclaw config validate
openclaw plugins inspect lobster-quant-agent --runtime --json
./scripts/validate.sh
```

还可以先运行完全离线、无写入的合成演示：

```bash
python3 python/lobster_quant_agent/cli.py demo
python3 scripts/first_use_smoke.py
```

第二条命令只在临时状态目录中验证固定报告、合成回测和通知目标缺失时的 fail-closed 行为；它不请求网络、不发送消息，也不执行交易动作。

### 示例问题

```text
查一下 600519 行情
查看美股 AAPL 行情
查看美股指数
查今天龙虎榜
给我一份盘前简报
来个完整版盘后复盘
观察池加入 600519 贵州茅台
设置策略盯盘：510050 一分钟 MACD 金叉时提醒
买入条件：收盘价站上20日均线；卖出条件：跌破买入价5%。回测510050最近60天，日线
生成回测图形
```

这些文字只是功能示例，不代表推荐、实际持仓、既有提醒或历史成绩。

开发中的可靠性修复及验证范围见[功能测试矩阵](docs/validation/2026-10-03-reliability.md)。工具也支持 `kline 600519 30`、`news_map 新能源`、`morning_news 10`、`us_quote AAPL`、`us_index` 和 `lhb 20260520`；龙虎榜八位参数表示日期，六位参数表示股票代码。新闻发布时间暂不可核验时会明确提示。

已配置本机 DeepSeek 与 CLIProxyAPI 的用户可显式初始化[Codex 流量开关](docs/model-traffic-switch.md)，使用 `model codex on|off|status` 控制后续模型请求。安装项目本身不会启用它；未初始化时继续使用原有配置。`model` 不带参数只查询状态，不调用模型。

默认盯盘按上海时区及已收录的交易所日历运行，当前覆盖 **2026 年**（[上交所休市公告](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)、[深交所休市公告](https://www.szse.cn/disclosure/notice/t20251222_618087.html)）；补班周末仍休市。盯盘窗口为 09:30:00–11:30:00、13:00:00–15:00:00，包含截止时点，下一微秒即暂停；14:57 起显示为收盘集合竞价，继续监控。开盘集合竞价不在盯盘窗口内。

未收录年份会返回 `calendar_status: "unverified_year"`、`is_trading_day: null`，默认暂停扫描；不能仅按星期推算恢复时间。跨出日历覆盖范围时 `next_open` 也为 `null`，需依据新年度交易所公告更新 `_A_SHARE_CALENDARS` 并验证休市、恢复及年末边界。日历仅涵盖已公告常规安排，临时停市、个股停牌和实时数据新鲜度须另行核实；将 `market_hours_only` 设为 `false` 会显式绕过时段门禁，不应用于宣称盘中验收通过。

### 通知渠道与送达恢复

`notify_channels` 是明确的发送范围：`["telegram"]` 只发 Telegram，`[]` 表示不发送，不会补上微信、QQ、旧 `notify_channel` 或环境默认值。只有复数键缺失时才读取旧单渠道键；两个键都缺失时，才允许使用显式配置的 `LOBSTER_QUANT_NOTIFY_CHANNELS`。未知渠道或无效列表会在发送前整组拒绝。

不带 `--channel` 的启动命令保留现有范围；`monitor on --channel telegram` 只选择 Telegram。新安装默认关闭盯盘且没有通知渠道，范围为空时启动会明确失败。普通 `monitor notify-test` / `monitor simulate-alert` 使用现有配置；显式指定渠道才测试该渠道。这两条命令会发送测试消息，必须由用户主动请求。

提醒发送前会写入本地送达记录，再记录各渠道的确认结果。超时、确认不明或发送期间崩溃可能留下 `unknown` / `inflight`；此时通知进入 `hold`，重启也不会自动重发，测试通知同样暂停。查看当前事务后再作人工判断：

```text
monitor delivery status
monitor delivery resolve ID CHANNEL delivered
monitor delivery resolve ID CHANNEL not-delivered
monitor delivery resolve ID CHANNEL abandon
```

`ID` 和 `CHANNEL` 必须来自当前状态，后三条是互斥的处理选择：

| 选择 | 含义与后续行为 |
| --- | --- |
| `delivered` | 用户确认该渠道已收到；记录确认并提交本批冷却。 |
| `not-delivered` | 用户确认未收到；全部渠道都未送达或跳过时，允许后续新鲜行情重新评估。已有其他渠道成功时，不重发成功渠道，也不补发失败渠道。 |
| `abandon` | 放弃并消费本次候选，提交冷却及相应策略激活状态；不把它记成已送达。 |

三个恢复动作本身都不发送消息，也不重放旧正文；剩余未知渠道仍会保持 `hold`。不得仅因日志出现 `hold` 就自动选择恢复动作。网络确认与本地写盘无法原子完成，因此不能保证 exactly-once：误判为未送达可能使后续新信号重复提醒，放弃则可能漏掉本次提醒。

状态、送达记录或盯盘配置损坏时保留原文件并明确失败，不自动重建、清空冷却或删除记录。先保存现场并核实状态；不要用删除文件或重启来解除未知送达状态。更多验证范围见[可靠性记录](docs/validation/2026-10-03-reliability.md)。

## English overview

Lobster Quant Agent puts quote lookup, research reports, conditional alerts, natural-language strategy monitoring, and historical backtesting behind one OpenClaw tool: `lobster_quant`.

- **A-shares:** stock and index snapshots, K-lines, 龙虎榜 data, breadth, morning reports, and post-market reviews.
- **US markets:** delayed US stock and index snapshots for research context.
- **Alerts:** threshold and natural-language strategy monitoring. Delivery fails closed when an explicit target is missing.
- **Backtests:** daily, 5-minute, and 1-minute bars with simplified fees, slippage, stop conditions, trades, metrics, and local HTML charts.
- **Channels:** Telegram, the OpenClaw WeChat extension, and optional QQ through `qqbot`.
- **Privacy:** watchlists, holdings labels, alert state, caches, and generated backtests remain in a user-selected local state directory outside the repository.
- **Hard boundary:** no broker integration and no order placement, modification, or cancellation.

## Install

Stable release `0.2.0` requires macOS or Linux, OpenClaw `>=2026.5.17`, Node.js `>=22.22.3`, and Python `>=3.10`. Source installation also requires npm and Git. See [COMPATIBILITY.md](COMPATIBILITY.md).

Install the stable release from ClawHub:

```bash
openclaw plugins install clawhub:@cnan5336-dev/lobster-quant-agent
```

You can also install from the public source repository:

```bash
git clone https://github.com/cnan5336-dev/lobster-quant-agent.git
cd lobster-quant-agent
./scripts/install.sh
```

To reproduce a release candidate, select that rc version explicitly; the default installation entry follows stable `latest`.

The installer is idempotent for the same checkout. It creates a repository-local Python virtual environment, installs dependencies, validates and links the plugin, creates an ignored local configuration copy, and writes only non-secret plugin paths and safe defaults to OpenClaw. It does not send a message, start monitoring, configure credentials, or perform a trade.

Before adding any channel or credential, run the no-network synthetic path:

```bash
python3 python/lobster_quant_agent/cli.py demo
python3 scripts/first_use_smoke.py
```

The smoke test uses a temporary state directory and makes zero network requests, message sends, or broker actions.

For agent-guided setup, give [INSTALL_WITH_AGENT.md](INSTALL_WITH_AGENT.md) to Codex or Claude Code after cloning. OpenClaw remains the runtime; setup assistants must stop for any credential, private destination, or real external-send decision.

## Architecture

```text
Telegram / WeChat / QQ
          │
       OpenClaw
          │  lobster_quant tool
  TypeScript plugin adapter
          │  explicit argv + private env config
  Python research core + CLI
      ├── A-share / delayed US-market adapters
      ├── reports + channel renderers
      ├── local watchlist / strategy monitor
      ├── natural-language backtest engine
      └── fallback data sources + private local cache
```

The Python CLI is an internal plugin layer for deterministic development and testing. It is not a supported standalone product or cross-agent runtime.

## Contributor and release checks

```bash
npm install --omit=peer
python3 -m venv .venv
.venv/bin/pip install -r python/requirements.txt
./scripts/validate.sh
```

The suite compiles Python, runs deterministic unit tests, exercises strategy parsing in a temporary no-write state directory, validates generated OpenClaw metadata, runs TypeScript tests, checks dependency vulnerabilities, verifies the synthetic demo and npm package payload, and performs a repository privacy audit.

Maintainers should also follow [MAINTAINERS.md](MAINTAINERS.md) and the [local ClawHub release procedure](docs/CLAWHUB_RELEASE.md). See the [0.2.0 release notes](docs/releases/0.2.0.md) and [CHANGELOG.md](CHANGELOG.md).

## 安全与隐私 / Safety & privacy

- Public market endpoints may be delayed, unavailable, rate-limited, stale, or structurally changed. The US adapter uses a public Yahoo Finance chart endpoint and must be treated as delayed research data.
- 龙虎榜 and some breadth/news workflows depend on AkShare and its upstream sources.
- Minute history can be shorter than requested. Cache fallback is labeled and may be stale.
- Backtests are simplified simulations; they do not fully model liquidity, price limits, corporate actions, or real execution.
- Monitoring is local and opt-in. Explicit channel lists are never expanded; an empty list or missing target prevents delivery. A durable journal holds uncertain outcomes for manual resolution; it does not guarantee exactly-once delivery.
- The monitor uses POSIX file locks, so Windows is not supported in this release.
- Never commit local OpenClaw configuration, credentials, channel/account identifiers, notification targets, watchlists, holdings, state, caches, logs, screenshots, or generated results.

See [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), and [DISCLAIMER.md](DISCLAIMER.md). Licensed under the [MIT License](LICENSE).
