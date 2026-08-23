# Contributing

Contributions are welcome for public-data adapters, parsers, reports, deterministic tests, OpenClaw channel presentation, accessibility, and documentation.

## Before opening an issue

- Use the structured bug or first-use feedback form.
- Reproduce with the offline synthetic demo or invented/redacted inputs when possible.
- Use GitHub's private Security reporting flow for vulnerabilities or accidental sensitive-data exposure.
- Do not request broker integration, order placement, order modification, or order cancellation; those are intentionally out of scope.

## Pull requests

1. Fork the repository and create a focused branch.
2. Keep credentials, account/chat ids, notification targets, watchlists, holdings, state, logs, caches, screenshots, private paths, and real backtest output out of commits.
3. Add deterministic tests for behavior changes. Tests must not send messages, start monitoring, ping a model, or perform any broker/order action.
4. Run:

   ```bash
   ./scripts/validate.sh
   python3 scripts/privacy_audit.py .
   ```

5. Explain behavior changes, data-source assumptions, privacy impact, and validation evidence in the pull request.

## Market-data changes

Public endpoints can be delayed, unavailable, rate-limited, or structurally changed. A fallback must preserve source labels and timestamps. If evidence is missing, return a clear unavailable/`暂缺` result; never invent a value.

Fixtures must be synthetic or derived from a redistribution-compatible public source with provenance documented. Do not copy a user's holdings, watchlist, alert history, cache, chat, screenshot, or generated report into a test.

## Release-related changes

Keep `package.json`, `openclaw.plugin.json`, `CHANGELOG.md`, compatibility notes, and generated assets aligned. A pull request does not authorize publishing, tagging, pushing on behalf of a maintainer, sending announcements, or contacting users.
