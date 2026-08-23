# Clean-environment first-use test report

Date: 2026-08-23 (Asia/Shanghai)
Candidate: committed local `0.2.0-rc.2`; the final commit SHA is recorded by post-commit verification output
Status: local review evidence; not a release or real-user testimonial

## Environment actually tested

| Item | Tested value |
| --- | --- |
| Host | macOS 26.4 (25E246), arm64 |
| OpenClaw | 2026.7.1-2 (0790d9f) |
| Node.js | 22.23.2 |
| npm | 10.9.8 |
| Python | 3.10.0 |
| ClawHub CLI | 0.23.3 |

No conclusion in this report should be generalized to Linux, Windows, real channels, or independent users.

## Isolation method

`scripts/test_clean_install.sh` created two independent temporary homes, OpenClaw state directories, XDG directories, and npm caches:

1. a clean source snapshot made from tracked and reviewable untracked candidate files while excluding ignored dependencies/config/state/output;
2. a managed `npm-pack` install using the same dependency shape as OpenClaw's ClawHub/npm plugin installer.

Both profiles were removed at the end. The test did not reuse a real OpenClaw config, channel, credential, account/chat id, notification target, watchlist, holding, cache, or log.

## Steps and results

### Source-style first use — tested

- Built a clean candidate source snapshot.
- Ran `./scripts/install.sh` from that snapshot.
- Created a fresh Python virtual environment and installed the optional AkShare dependency set.
- Installed Node dependencies and built/validated the plugin.
- Passed Python tests, Vitest, dependency audit, package check, and privacy audit within the isolated snapshot.
- Applied only safe plugin defaults to the isolated OpenClaw config.
- Passed `openclaw config validate`.
- Passed `openclaw plugins inspect lobster-quant-agent --runtime --json`.
- Ran the offline `demo` command.
- Ran `scripts/first_use_smoke.py`.
- Passed `openclaw plugins uninstall lobster-quant-agent --dry-run`.

### Managed candidate package — tested

- Built the candidate npm tarball.
- Installed it with `openclaw plugins install npm-pack:<isolated-tarball>` in a second fresh profile.
- Enabled the plugin and passed runtime inspect.
- Located the packaged Python core through inspect output, then ran the dependency-free offline `demo` command.
- Ran the packaged offline first-use smoke test without pip or a source checkout.
- Passed uninstall dry-run.
- Removed the complete disposable profile as the cleanup mechanism.

### Offline behavioral smoke — tested

The smoke test verified:

- committed synthetic demo loads with `mode=synthetic_offline`;
- the fixed Telegram report renderer produces its conclusion and research-only disclaimer;
- a 60-session synthetic daily backtest executes (10 deterministic trades in this fixture);
- a Telegram alert with no explicit target returns `skipped` and does not call the OpenClaw send command;
- zero network requests, zero messages, and zero broker actions occur;
- temporary state is removed on exit.

### Package and ClawHub — tested locally

- ClawHub Plugin Inspector: PASS, 0 breakages, 0 warnings, no findings.
- ClawHub Plugin Inspector and folder dry-run: PASS for package version `0.2.0-rc.2`, with 0 breakages, 0 warnings, and no findings.
- The final post-commit dry-run must report the current rc.2 commit, and the verifier must require that value to equal `git rev-parse HEAD`; the exact SHA belongs in immutable verification output rather than this self-referential file.
- Final package check after excluding Python caches/tests: 40 files, 101,649 bytes packed and 380,714 bytes unpacked.
- The prerelease dry-run must use the `rc` tag; it must not replace `latest`.

## First-use blockers found and addressed

1. **Managed package could install but initially depended on pip-provided `requests`.** OpenClaw intentionally ignores lifecycle scripts, so this could fail on first tool use. The core now uses a standard-library HTTP adapter; AkShare-exclusive paths return a labeled gap when the optional dependency is absent.
2. **The first package import form could not resolve the new HTTP adapter.** Imports now support both direct CLI execution and package import; both tests pass.
3. **Python `__pycache__` and test bytecode entered the initial dry-run payload.** Nested npm ignores plus package assertions now exclude them; package size fell from roughly 212 KB packed/622 KB unpacked to roughly 100 KB/377 KB.
4. **An npm tarball is not a source checkout.** It intentionally omits TypeScript source/config, so the source installer must be tested from a clean source snapshot, while the tarball must be tested through OpenClaw's managed install path. The harness now tests both correct shapes separately.
5. **Real uninstall in a nearly empty isolated profile hit OpenClaw's `size-drop` config-write guard.** The guard was not bypassed. Uninstall dry-run is verified; the harness removes the entire disposable profile. User-facing instructions now say to stop, validate, and follow OpenClaw troubleshooting if a real uninstall is rejected.
6. **The `v0.2.0-rc.1` source tag retained a stale Python `__version__ = "0.1.0"`.** No GitHub Release or package was created. `0.2.0-rc.2` aligns package, plugin, lockfile, and Python versions and adds three validation layers to prevent recurrence; the public rc.1 tag remains unchanged.

## Isolation incident and correction

Before the isolation wrapper was added, one metadata-build command started OpenClaw with the normal home and emitted a global plugin-index doctor notice. It printed no credential, account, notification target, holding, watchlist, message, cache, or log content, and reported that the index was left in place. No such value was copied into the candidate. All subsequent metadata build/validate commands use `scripts/run-openclaw-isolated.mjs`, which forces a temporary home and state directory.

## Only statically checked or not covered

| Area | Status | Reason / next evidence |
| --- | --- | --- |
| Live A-share/US endpoints | Not covered in clean smoke | Avoided network-dependent evidence; run separate labeled data-source probes later. |
| Real Telegram/WeChat/QQ delivery | Intentionally not covered | Requires private credentials and targets plus explicit send approval. |
| Long-running monitor | Intentionally not started | Installation/validation safety boundary. |
| Model fallback | Not covered | Would require configured provider credentials and a separately approved ping. |
| Linux | Static target only for this candidate | Run Ubuntu CI and a fresh Linux install on the exact committed candidate. |
| Windows | Unsupported | POSIX file locks are required. |
| ClawHub registry install | Not available | The package has not been published; only folder and npm-pack paths can be tested. |
| Exact tarball-to-commit provenance | Post-commit verification required | Run the folder dry-run from the clean rc.2 candidate commit and require its reported commit to equal `git rev-parse HEAD`; this remains local evidence until that commit is pushed. |
| Human comprehension/UX | Not independently validated | Requires 3–5 real users following the public docs without maintainer guidance. |

## What automation can and cannot replace

The automated and isolated tests can replace repeated technical checks for build reproducibility, manifest/version alignment, package contents, runtime discovery, dependency shape, deterministic demo/report/backtest behavior, fail-closed missing targets, and the absence of message/broker actions in those paths.

They cannot establish that new users understand the prerequisites, privacy language, channel setup, error recovery, or investment-research limitations. They also cannot cover different Linux distributions, real upstream data behavior, private channel credentials, notification delivery, long-running use, or perceived usefulness. Before describing the release as broadly easy to use, obtain 3–5 opt-in real-user tests across at least macOS and Linux using the structured first-use form and only synthetic/redacted evidence.
