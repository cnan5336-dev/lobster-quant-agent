# Reliability validation — 2026-10-03

This update was developed against public main `84bdb9ac815680de3d87c6ffd2ad94757f636c84` and compared with the original deployed Python implementation. All committed fixtures are invented. Private configuration, account identifiers, watchlists, holdings, logs and credentials are excluded.

## Reproduced causes

The old local parser could drop a second price threshold, reuse one indicator's crossing direction for another, retain only the first symbol, or turn mixed AND/OR into OR. The monitor fetched minute and daily data even for a price-only rule. It marked a strategy active before delivery, so a failed notification could suppress the next attempt. Historical report labels could describe current quotes, missing breadth values could become zero, and duplicate leaderboard reasons could inflate aggregate flows. These are deterministic program defects; changing the model does not repair them.

## Functional coverage

| Area | Deterministic validation | Operational limit |
| --- | --- | --- |
| Strategy setup and lifecycle | Parse, dry-run, set/reload/hash, list, explain, test, delete/clear; multiple symbols, strict boundaries, unsupported/ambiguous input retains prior config | Complex temporal strategies and parenthesized mixed logic are not silently approximated |
| Ordinary and strategy monitoring | Source dependency selection, explicit symbols outside pools, missing/stale/future quotes, daily volume units, duplicate symbols, cooldown, failed delivery, activation recovery, loop exceptions, PID identity and locks | No persistent daemon or real alert was started during testing |
| Recovery helper | Enabled/disabled/malformed status with mocked executable; verified CLI lifecycle entry | No LaunchAgent or recurring job is installed by tests |
| A-share quotes, pools and news | Quote parsing/finite values, symbol normalization, pool CRUD/NL routes, K-line/news-map/morning-news CLI wiring, leaderboard date/code routing, snapshots and synthetic keyword mapping | Public endpoints can be delayed or unavailable; news fetch time is not article publication time |
| US snapshots | Previous-session close precedence, chart-bar fallback, timestamp and unavailable reference, invalid symbols | No guarantee of exchange-real-time data |
| Pre-market and post-close reports | Full/simple and channel renderers, scope/date routing, historical date isolation, missing values, stale caches, leaderboard deduplication, malformed analysis JSON and collection exceptions | Historical breadth and unverified news timing remain labeled gaps; current pools are not historical positions |
| Backtesting | Complete condition parsing, numeric/schema/OHLC validation, requested MA periods, finite costs, fees/slippage, conservative T+1, zero-volume exclusion, fallback/cache provenance, corrupt-cache recovery, failed-run configuration preservation, summary and chart output | Signals fill at a later eligible bar open; simplified research simulator, not exchange execution modelling |
| Model helper and plugin | Default inheritance, explicit overrides, unavailable models, shared deadline, malformed replies, old CLI support failure; exact argument/env forwarding, abort and timeout | Manual model probe is separate from automated tests |
| Installation and package | First install and safe rerun with mocked tools; version/manifest/build/package/privacy checks | No actual reinstall or release publication was performed by tests |

## Timing evidence

The deterministic parser's measured p95 was about 0.033 ms over 1,000 synthetic parses. With identical injected source delays (20 ms for quote/supplement/minute and 40 ms for daily data), three-run median price-only scans changed as follows:

| Synthetic case | Before | After | Source requests |
| --- | ---: | ---: | ---: |
| One symbol | 114.47 ms | 25.09 ms | 4 → 1 |
| Four symbols | 469.48 ms | 30.00 ms | 16 → 4 |

These numbers demonstrate avoided work and concurrency, not a live-market latency promise.

Three separately authorized, context-free manual calls to the existing DeepSeek model used only synthetic strategy prompts. They completed in 16.308, 20.721 and 11.073 seconds. All preserved the supplied logical/temporal distinctions or requested clarification. A fourth bounded request through the integrated helper completed in 6.149 seconds, inherited the same default model, made one attempt, and preserved both strict price thresholds. This small sample does not establish general model quality or chat-to-notification latency.

## Live-source boundaries

A bounded read-only off-session probe of the deployed HTTP adapter returned a dated pre-holiday quote from Sina and unavailable supplementary ratio/turnover fields from Eastmoney. The standard-library adapter on the tested Python installation encountered a local certificate trust-store error. TLS verification was not disabled and global certificate/network settings were not changed. This is recorded as an environment verification gap, not a provider success or a trading-session pass.

## Reproduction

The final local regression run passed all 210 public-package Python tests and all 194 standalone-deployment Python tests. The standard-library-only run passed 202 public-package tests with 8 optional DataFrame-dependent cases skipped. The plugin bridge passed all 5 Vitest tests. These are separate runs with overlapping coverage and should not be added together as unique tests.

The first complete validation attempt found the pre-existing development runner affected by [GHSA-82fw-gwwq-j7x9](https://github.com/advisories/GHSA-82fw-gwwq-j7x9). Vitest was updated to patched 4.1.11; this changes development tooling only.

The subsequent complete `scripts/validate.sh` run passed Python compilation/tests, synthetic first-use checks, version consistency, isolated plugin validation/build, Vitest 4.1.11, dependency audit (zero reported vulnerabilities), reproducible demo, package inspection and privacy audit. `npm ci --dry-run --omit=peer --ignore-scripts` also accepted the updated lockfile. No global software was updated.

```bash
python3 -m unittest discover -s python -p 'test_*.py'
python3 -S -m unittest discover -s python -p 'test_*.py'
./scripts/validate.sh
python3 scripts/privacy_audit.py .
```

The second command checks the standard-library install shape. DataFrame-specific cases explicitly skip when optional pandas is absent; core parsing, monitoring and report tests still run. Automated tests never call a real model, send a message, place an order or start monitoring. `validate.sh` also contacts the npm advisory service and can install development dependencies if none are present; it is not an offline-only command.

After deployment, an operator can inspect `strategy dry-run`, `strategy list`, `strategy explain`, `monitor status` and `monitor diagnose`. Actual channel delivery and a trading-session run remain separate supervised acceptance checks. Local OpenClaw health/repair/maintenance scripts outside this repository were only inspected; they are not part of this plugin's tested or published changes.

Delivery uses the existing any-channel-success policy: failed channels are disclosed but not independently retried after another channel succeeds. There is no durable outbox. If the process crashes after a message is delivered but before its acknowledgement can be persisted, recovery may produce a duplicate. The running process retries the acknowledgement without resending while it remains alive.
