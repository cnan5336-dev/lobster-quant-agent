# OpenClaw Discord draft

Posting gate: confirm the current showcase/plugin-development channel and its rules. Send once; do not repeat across channels.

Draft:

> 🦞 **Lobster Quant Agent — OpenClaw market-research plugin (stable 0.2.0)**
>
> I built an OpenClaw-only tool for A-share research, delayed US-market snapshots, morning/post-market reports, local alerts, natural-language strategy monitoring, and simplified backtests.
>
> Safety boundary: research and alerts only—no broker connection or order execution. Local watchlists/holdings labels, notification targets, caches, and results stay outside the repo. Missing notification targets fail closed.
>
> The README includes a reproducible synthetic demo (no real account/chat/portfolio data) and source-install instructions.
>
> Repo: https://github.com/cnan5336-dev/lobster-quant-agent
> ClawHub: [ADD ONLY AFTER PUBLICATION AND SCAN CLEARANCE]
>
> Looking for focused feedback on install friction, OpenClaw plugin packaging, Linux behavior, and research/report usability. Please keep issue examples synthetic or redacted.

Optional follow-up only when asked: explain the TypeScript-to-Python boundary and why the packaged core avoids pip-dependent HTTP calls.
