# r/openclaw Showcase Weekend draft

Posting gate: Saturday–Sunday only for a standalone Showcase/Skills post; confirm the live sidebar and flair first.

Suggested title:

> I built an OpenClaw-only A-share/US-market research, alerts, and backtesting plugin (no broker access)

Draft body:

> I wanted one OpenClaw tool that could handle the repetitive parts of market research—quote snapshots, morning/post-market reports, conditional alerts, natural-language strategy monitoring, and simplified backtests—without ever getting trading permissions.
>
> Lobster Quant Agent is the result. It runs inside OpenClaw and supports:
>
> - A-share research plus delayed US stock/index snapshots
> - Telegram, the OpenClaw WeChat extension, and optional QQ via `qqbot`
> - local watchlist/holdings labels and alert state
> - daily, 5-minute, and 1-minute simplified backtests
> - fail-closed delivery when no explicit notification target exists
>
> The hard boundary is intentional: no broker connector, no order placement/modification/cancellation, and no promise that public market data is real-time or complete. Private state stays in a user-selected local directory outside the repo.
>
> The README preview is generated from synthetic JSON; it is not a real chat, account, portfolio, alert target, or performance record.
>
> Source: https://github.com/cnan5336-dev/lobster-quant-agent
>
> ClawHub: [ADD ONLY AFTER THE LISTING IS PUBLIC AND SCANNED]
>
> I would especially value feedback on first-run clarity, missing research workflows, Linux compatibility, and whether the no-trading boundary is obvious enough. Please use synthetic/redacted examples in public issues.

Do not add engagement bait, referral links, download/user counts, or performance claims. Do not ask for upvotes.
