# Changelog

All notable changes are recorded here. Versions follow Semantic Versioning.

## Unreleased

### Fixed

- Preserve every supported strategy condition, comparison, direction, timeframe and explicit symbol; reject ambiguous combinations instead of silently changing their meaning. Return a readable execution preview and keep the previous strategy when parsing fails.
- Fetch only the data required by each strategy, share a bounded worker pool, cache daily history briefly, and report per-source timing. Reject stale or invalid data before evaluating signals.
- Commit alert activation after verified delivery or explicit abandonment, preserve activation across unavailable data, and restore enabled monitoring through the verified process lifecycle.
- Keep explicit notification scope authoritative, including empty lists. Preserve existing channels on unqualified starts; reject invalid channels before any send and never append implicit fallback destinations.
- Persist per-channel send intent and acknowledgement across crashes. Hold uncertain outcomes instead of automatically resending, and expose `monitor delivery status` / `resolve ID CHANNEL delivered|not-delivered|abandon`; resolution commands never send messages.
- Preserve corrupt watchlist, monitor-state and delivery files for inspection instead of resetting history. Guard cooldown updates against stale concurrent snapshots; distinguish first initialization from missing previously initialized state.
- Apply the published 2026 exchange holiday calendar and exact session cutoffs to default monitoring. Label the closing auction correctly, pause on unverified calendar years, and keep status, diagnosis and next-open guidance consistent.
- Keep report dates, scope, source evidence and missing-data warnings consistent. Do not present live quotes as historical reports or count duplicate leaderboard rows as additional money flows.
- Validate backtest bars and conditions, normalize volume units, calculate requested moving-average windows, disclose conservative execution assumptions and retain the previous saved strategy when a run fails.
- Use a verified previous-session close for US daily price changes rather than a five-day chart reference.
- Connect documented K-line, news mapping, morning-news and US aliases to the CLI/plugin; distinguish leaderboard dates from stock codes and reject invalid arguments before fetching.
- Inherit the configured OpenClaw default model for explicit model helpers, use context-free inference, and share one timeout budget across attempts. Preserve plugin arguments and make cancellation/timeouts explicit.
- Preserve private channel, notification, interpreter and state settings when the source installer is run again.
- Update the development-only Vitest runner to patched 4.1.11 for GHSA-82fw-gwwq-j7x9; runtime dependencies are unchanged.
- Keep provider error text out of request identifiers, notification results and diagnostic logs. Use fixed error summaries and minimal delivery acknowledgements; reject unsafe log files without changing existing permissions.
- Detect private credential/state filenames and quoted credential assignments during publication checks, including tracked files inside excluded folders and readable secrets in binary or Unicode text. Reject symlinks and verify package exclusions with synthetic negative cases.

### Validation and boundaries

- Added synthetic regression tests for strategy semantics, monitoring, reports, historical data, models, installation and the plugin bridge. See [the reliability test matrix](docs/validation/2026-10-03-reliability.md).
- No version/tag/package release is included. Trading-session delivery and long-running service recovery still require a supervised operational check.

## [0.2.0] - 2026-08-23

This stable release promotes the validated `0.2.0-rc.2` code and safety boundaries without adding new runtime features.

### Changed

- Promoted package, plugin manifest, lockfile, and Python version metadata from `0.2.0-rc.2` to `0.2.0`.
- Updated installation and maintainer documentation so the default ClawHub entry follows stable `latest`, while the existing rc tags remain immutable.
- Kept the version-consistency checks that fail when package/plugin/lockfile/Python metadata drifts.

### Compatibility and security

- OpenClaw remains the only supported runtime; minimum OpenClaw/plugin API is `>=2026.5.17`.
- There is still no broker connector or order-execution capability, and notification delivery still fails closed without an explicit local target.
- The plugin package's runtime dependency audit is clean. Full development or host dependency trees can still inherit advisories from the peer-supplied OpenClaw runtime and upstream transitive dependencies, which this plugin does not bundle. Keep OpenClaw current and review upstream advisories.

### Release lineage

- The public `v0.2.0-rc.1` and `v0.2.0-rc.2` tags remain unchanged.
- `0.2.0` is the stable promotion of the corrected rc.2 line; it does not rewrite either candidate.

## [0.2.0-rc.2] - 2026-08-23

This corrective candidate was tagged, published as a GitHub prerelease, and published to the ClawHub `rc` tag. It was not published to npm or announced through community posts.

### Fixed

- Aligned `package.json`, `openclaw.plugin.json`, both root lockfile versions, and the Python package `__version__` at `0.2.0-rc.2`.
- Added a reusable version-consistency check plus independent Python and Vitest assertions so package/plugin/Python version drift fails validation.
- Added corrective release notes and validation guidance without moving or overwriting the public `v0.2.0-rc.1` tag.

### Release status

- `v0.2.0-rc.1` was pushed as a source tag, but no GitHub Release, ClawHub package, npm package, or announcement was created for it.
- Release publication stopped when its Python `__version__` was found to remain at `0.1.0`; `0.2.0-rc.2` is the forward-only correction.
- The rc.2 GitHub prerelease and ClawHub package point to the corrected immutable rc.2 commit; neither candidate tag was moved or overwritten.

## [0.2.0-rc.1] - 2026-08-23

This source candidate was committed, pushed, and tagged, but it was not published as a GitHub Release or package after the internal Python version mismatch was found.

### Added

- ClawHub code-plugin metadata, scoped package identity, compatibility/build declarations, and local package/dry-run commands.
- A bilingual, value-first README with explicit OpenClaw-only, privacy, delayed-data, and no-trading boundaries.
- A deterministic SVG product preview generated from clearly labeled synthetic JSON.
- Release/maintenance instructions, compatibility matrix, community launch drafts, 30/90-day plan, adoption metrics, and release checklist.
- Privacy-first bug and trial-feedback issue forms.
- An offline first-use smoke test covering the synthetic demo, fixed report rendering, synthetic backtesting, and fail-closed notification behavior.

### Changed

- Advanced the package and plugin manifest from `0.1.0` to `0.2.0-rc.1`; this is a forward prerelease, not a downgrade.
- Renamed the distributable package to `@cnan5336-dev/lobster-quant-agent` for ClawHub owner/package alignment.
- Replaced the packaged core's mandatory `requests` import with a small standard-library HTTP adapter so a managed OpenClaw package can run basic workflows even though managed installs ignore lifecycle scripts.
- Pinned candidate CI validation to OpenClaw `2026.7.1` and ClawHub CLI `0.23.3` for reproducibility.

### Compatibility

- Only OpenClaw is supported as a runtime.
- Minimum OpenClaw/plugin API: `>=2026.5.17`.
- Minimum Node.js: `>=22.22.3`.
- Minimum Python: `>=3.10`.
- Full source installs add the optional AkShare-enhanced A-share data paths. Managed package installs keep dependency-free core paths and report AkShare-only data as unavailable rather than failing unsafely.

### Security and privacy

- No broker connector or order-execution capability was added.
- Notification delivery still fails closed without an explicit local target.
- Runtime state remains outside the repository in the configured private state directory.
- The public package must not contain credentials, identifiers, watchlists, holdings, caches, logs, screenshots, or generated user results.

### Known candidate limitations

- The current candidate has only been exercised locally on one macOS host; Linux candidate CI has not run until the changes are pushed, and Windows remains unsupported.
- Real Telegram/WeChat/QQ delivery, real user credentials, and live notification targets are intentionally untested in this preparation pass.
- In a nearly empty isolated OpenClaw profile, the real uninstall write triggered OpenClaw's `size-drop` safety guard. Uninstall dry-run is covered; the test harness removes the entire disposable profile instead of bypassing the guard.
- Exact immutable ClawHub source provenance must be established by rerunning the folder dry-run from the clean candidate commit and matching its reported commit to `git rev-parse HEAD`; the candidate remains unpublished until that check passes.

## [0.1.0] - 2026-08-13

- Initial public source release of the OpenClaw-only research and alert plugin.
