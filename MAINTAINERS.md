# Maintainer guide

## Release authority

Preparing and validating files does not authorize a release. A maintainer must separately approve each external action: candidate commit, tag, GitHub Release, ClawHub publish, and community announcement.

Never place ClawHub tokens, GitHub tokens, OpenClaw credentials, channel/account identifiers, notification targets, watchlists, holdings, state, caches, logs, screenshots, or generated user results in this repository, command history, issue text, release notes, or validation artifacts.

## Candidate workflow

1. Review `git status`, the full diff, version alignment, and [CHANGELOG.md](CHANGELOG.md).
2. Run `./scripts/validate.sh` and follow [docs/CLAWHUB_RELEASE.md](docs/CLAWHUB_RELEASE.md).
3. Inspect the exact npm payload and ClawHub reports outside the repository.
4. Confirm the candidate commit is clean and immutable before claiming exact source provenance.
5. Ask for explicit approval before each external action.

The `plugin:build`, `plugin:check`, and `plugin:validate` npm scripts wrap OpenClaw with a temporary home/state directory. Keep that isolation in place so metadata generation does not inspect or migrate a maintainer's real OpenClaw state.

## Versioning

- `package.json` is the package-version authority.
- If `openclaw.plugin.json` contains `version`, it must match `package.json` exactly.
- `python/lobster_quant_agent/__init__.py::__version__` and the root package-lock versions must match `package.json` exactly.
- `openclaw.compat.pluginApi` is the minimum host API contract and is not the plugin release version.
- `openclaw.build.openclawVersion` and `pluginSdkVersion` record the release build/test baseline.

## Support triage

- Ask reporters to reproduce with synthetic or redacted inputs.
- Move vulnerability reports to GitHub's private Security reporting flow.
- Delete or redact accidental public credentials/identifiers as soon as platform permissions allow; never quote them into another issue.
- Broker integration and order execution remain out of scope.
- Treat upstream market-data failures as data-source gaps, not as a reason to fabricate a value.
