# Contributing

Contributions are welcome for data adapters, parsers, reports, tests, and OpenClaw channel presentation.

1. Fork the repository and create a focused branch.
2. Keep credentials, personal identifiers, watchlists, holdings, logs, caches, screenshots, and backtest output out of commits.
3. Run `./scripts/validate.sh` and `python3 scripts/privacy_audit.py .`.
4. Explain behavior changes, data-source assumptions, and validation in the pull request.

New broker integration or order execution is intentionally out of scope. Tests must be deterministic and must not send messages, start monitoring, or place network orders.
