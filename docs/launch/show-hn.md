# Show HN draft

Hard gate: do not submit until the source/package install is publicly runnable, the exact candidate commit is validated, and the maker can remain available to answer questions. Follow the current [Show HN guidelines](https://news.ycombinator.com/showhn.html). Never ask anyone to upvote or comment.

Suggested title:

> Show HN: Lobster Quant Agent – local market research and backtesting inside OpenClaw

Draft first comment:

> I built Lobster Quant Agent because I wanted chat-based market research without giving the software a path to a brokerage account.
>
> It is an OpenClaw-only tool plugin with a TypeScript adapter and Python research core. It supports A-share research, delayed US-market snapshots, morning/post-market reports, conditional alerts, natural-language strategy monitoring, and simplified daily/minute backtests. Telegram, an OpenClaw WeChat extension, and optional QQ are presentation channels.
>
> The main design constraint is negative capability: there is no broker connector and no order execution. Notification delivery is opt-in and fails closed if an explicit local destination is absent. Watchlists, holdings labels, caches, targets, and generated results remain in a user-selected local directory.
>
> OpenClaw's managed plugin installer disables lifecycle scripts. To keep the first package call usable without silently running pip, I replaced the core Python HTTP dependency with a small standard-library adapter. AkShare remains an optional enhancement for source installs; its exclusive workflows report an explicit gap when unavailable.
>
> The repository also contains a deterministic SVG demo generated from synthetic JSON, an offline first-use smoke test, package-payload assertions, and a privacy audit.
>
> Repo: https://github.com/cnan5336-dev/lobster-quant-agent
> Install: [ADD VERIFIED PUBLIC INSTALL COMMAND AT POSTING TIME]
>
> I would appreciate technical feedback on the mixed TypeScript/Python packaging boundary, fail-closed configuration, and reproducible first-run testing. This is research software, not investment advice or a trading system.

Do not submit a documentation-only page or unpublished candidate. The linked project must be directly usable without a signup gate.
