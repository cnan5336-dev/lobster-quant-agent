# Changelog

All notable changes are recorded here. Versions follow Semantic Versioning.

## [0.2.0-rc.2] - Unreleased

This corrective candidate is committed locally for review. It has not been tagged, pushed, published to ClawHub, released on GitHub, or announced.

### Fixed

- Aligned `package.json`, `openclaw.plugin.json`, both root lockfile versions, and the Python package `__version__` at `0.2.0-rc.2`.
- Added a reusable version-consistency check plus independent Python and Vitest assertions so package/plugin/Python version drift fails validation.
- Added corrective release notes and validation guidance without moving or overwriting the public `v0.2.0-rc.1` tag.

### Release status

- `v0.2.0-rc.1` was pushed as a source tag, but no GitHub Release, ClawHub package, npm package, or announcement was created for it.
- Release publication stopped when its Python `__version__` was found to remain at `0.1.0`; `0.2.0-rc.2` is the forward-only correction.

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
