# Compatibility

## Supported runtime

OpenClaw is the only supported runtime. Codex and Claude Code may assist with installation or development, and the Python CLI may be used for deterministic contributor checks, but neither is a supported standalone product runtime.

| Component | Minimum | Release validation | Notes |
| --- | --- | --- | --- |
| OpenClaw host | `>=2026.5.17` | `2026.7.1-2` locally | Retains the repository's previously declared minimum; the stable release was exercised on the version shown at left. |
| OpenClaw plugin API | `>=2026.5.17` | built against `2026.7.1` | Declared separately from the plugin's own version. |
| Node.js | `>=22.22.3` | `22.23.2` locally | Required by the OpenClaw tool-plugin contract. |
| Python | `>=3.10` | `3.10.0` locally | The managed package core uses only the standard library. |
| ClawHub CLI | n/a at runtime | `0.23.3` for release validation | Needed only by maintainers for validation/dry-run/publish. |
| macOS | supported | release tested on one Apple Silicon host | This does not substitute for external user testing. |
| Linux | supported target | repository CI on the tagged release commit | Separate manual Linux first-use testing remains uncovered. |
| Windows | unsupported | not tested | The monitor relies on POSIX file locks. |

## Install shapes

### Source checkout

`./scripts/install.sh` creates a repository-local virtual environment, installs the optional AkShare-enhanced A-share adapter, installs Node dependencies, validates the plugin, links it into OpenClaw, and writes safe local defaults. This is the full-featured source path.

### Managed package / ClawHub install

OpenClaw installs package dependencies with lifecycle scripts disabled. The packaged core therefore uses `python/lobster_quant_agent/http_client.py`, a dependency-free standard-library adapter, for ordinary public JSON/text endpoints. AkShare-only functions return a labeled `暂缺` result when that optional library is not present; they do not prompt for credentials or silently send data elsewhere.

The public stable install command is:

```bash
openclaw plugins install clawhub:@cnan5336-dev/lobster-quant-agent
```

Before installation, review the public ClawHub version, source link, compatibility metadata, and current scan state.

## Data and behavior boundaries

- A-share and US-market endpoints may be delayed, stale, rate-limited, unavailable, or changed upstream.
- Backtests are simplified research simulations, not execution-quality performance records.
- Real channel delivery requires user-owned OpenClaw channel configuration and an explicit private notification target.
- Missing notification targets fail closed.
- No supported configuration enables broker access or order execution.
