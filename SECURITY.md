# Security

## Reporting a vulnerability

Please use this repository's **Security → Report a vulnerability** flow. Do not open a public issue containing credentials, private channel identifiers, exploit details, or user data.

## Secrets and local data

Lobster Quant Agent does not ship credentials. Channel credentials remain in the user's OpenClaw configuration and should use OpenClaw SecretRefs or environment-backed secret providers. Proactive notification targets are local configuration and must never be committed.

Runtime state is stored outside the repository under the configured private state directory. It can contain watchlists, holdings labels, alert history, cached market data, and backtest output; treat that directory as private.

## Trust boundary

This plugin executes a bundled Python research CLI and makes outbound requests to public market-data endpoints. It has no broker connector and no order-execution code. Install only reviewed releases or a pinned commit, and review changes before updating.
