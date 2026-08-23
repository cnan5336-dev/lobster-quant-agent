# Release checklist

Check items only with evidence from the exact candidate. This checklist does not authorize external actions.

## Scope and repository

- [ ] Work is in the intended `cnan5336-dev/lobster-quant-agent` repository.
- [ ] No unrelated or user-owned changes were overwritten.
- [ ] Full diff and untracked-file list were reviewed.
- [ ] No private OpenClaw workspace or old worktree supplied release content.
- [ ] Candidate commit is clean and immutable; SHA from `git rev-parse HEAD` matches the ClawHub folder dry-run: `[RECORD AFTER COMMIT]`.

## Version and compatibility

- [ ] `package.json` and `openclaw.plugin.json` versions match.
- [ ] Version is greater than the prior public version and semantically justified.
- [ ] `openclaw.compat.pluginApi`, build versions, install source, peer dependency, and minimum host agree.
- [ ] CHANGELOG and release notes match the actual diff.
- [ ] macOS/Linux/Windows support statements distinguish tested, CI-only, static-only, and unsupported.

## Build and tests

- [ ] `npm ci --omit=peer` succeeds from a fresh checkout/package context.
- [ ] Python compile and unit tests pass.
- [ ] TypeScript build and Vitest pass.
- [ ] `openclaw plugins build --check` passes without metadata drift.
- [ ] `openclaw plugins validate` passes.
- [ ] Offline first-use smoke test passes with zero network requests/messages/broker actions.
- [ ] Clean-environment package install, enable, runtime inspect, demo, and fail-closed check pass; uninstall dry-run passes.
- [ ] Linux CI passes on the exact candidate commit.

## Privacy and package payload

- [ ] Repository privacy audit passes.
- [ ] Package payload check passes and is manually reviewed.
- [ ] No credential, account/chat id, notification target, watchlist, holding, cache, log, screenshot, generated user result, or personal absolute path is present.
- [ ] Synthetic asset matches its generator and input.
- [ ] ClawHub-generated `reports/` artifacts remain ignored and outside the package.
- [ ] Dependency audit output is reviewed and any exception documented.

## ClawHub

- [ ] `clawhub package validate .` passes.
- [ ] `clawhub package publish . --family code-plugin --dry-run` passes.
- [ ] Package owner/scope, family, source repository, exact commit, version, compatibility, and changelog are correct.
- [ ] The prerelease uses the `rc` tag and does not replace the stable `latest` tag.
- [ ] Maintainer has explicitly approved real ClawHub publish.
- [ ] Real publish completed: `[NOT AUTHORIZED]`.
- [ ] Automated ClawHub scan is public and clear before install claims: `[NOT APPLICABLE YET]`.

## GitHub and announcements

- [ ] Maintainer has explicitly approved candidate commit.
- [ ] Maintainer has separately approved tag and push.
- [ ] GitHub Release draft matches the exact tag.
- [ ] Maintainer has separately approved GitHub Release publication.
- [ ] Community drafts contain no fabricated metrics or endorsements.
- [ ] Live platform rules and links were rechecked immediately before posting.
- [ ] Maintainer has separately approved each external post.

## Rollback and support

- [ ] Previous known-good version and source SHA are recorded.
- [ ] ClawHub rollback/yank/deprecation procedure is understood before publish.
- [ ] Uninstall dry-run was tested in an isolated state directory; any real-uninstall `size-drop` guard behavior is documented and never bypassed.
- [ ] Security reporting and privacy-safe issue guidance are visible.
- [ ] Maintainer availability for the first 48 hours is confirmed.
