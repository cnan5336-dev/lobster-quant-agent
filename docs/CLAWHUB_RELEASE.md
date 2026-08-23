# ClawHub release procedure

This procedure is for maintainers. The candidate is a **code plugin**, not a skill, so use `clawhub package ... --family code-plugin`; do not use `clawhub skill publish`.

## Current package contract

- Package: `@cnan5336-dev/lobster-quant-agent`
- Plugin id: `lobster-quant-agent`
- Candidate: `0.2.0-rc.1`
- Minimum OpenClaw/plugin API: `>=2026.5.17`
- Release build baseline: OpenClaw/plugin SDK `2026.7.1`

Official references:

- [ClawHub quickstart](https://docs.openclaw.ai/clawhub/quickstart)
- [ClawHub publishing](https://docs.openclaw.ai/clawhub/publishing)
- [Plugin validation fixes](https://docs.openclaw.ai/clawhub/plugin-validation-fixes)
- [Tool plugin packaging](https://docs.openclaw.ai/plugins/tool-plugins)

## Local candidate checks

Run from a reviewed checkout with no credentials or private runtime files:

```bash
npm ci --omit=peer
./scripts/validate.sh
./scripts/test_clean_install.sh
npx --yes clawhub@0.23.3 package validate .
npx --yes clawhub@0.23.3 package publish . --family code-plugin --tags rc \
  --changelog "ClawHub packaging, bilingual docs, synthetic demo, and privacy-safe first-use checks." \
  --dry-run
```

ClawHub validation may create an ignored `reports/` directory containing local paths. Treat it as a transient validation artifact: inspect it locally, keep it out of the package and Git, then remove only that generated directory.

`npm run pack:check` asserts required files, identity/version, a 5 MiB unpacked-size ceiling, and forbidden runtime/private paths. Also inspect `npm pack --dry-run --json` manually.

## Immutable provenance gate

Folder dry-run is useful during review, but a dirty working tree cannot prove that a tarball corresponds exactly to a public commit. Before a real publish:

1. Obtain explicit permission to commit the reviewed candidate.
2. Confirm `git status --short` is empty and record `git rev-parse HEAD`.
3. Build and validate from that exact commit in a fresh checkout.
4. Confirm the package report's repository/ref metadata identifies that commit.
5. Only then consider a matching Git tag and GitHub Release, each with separate approval.

## External actions — separate approvals required

The following are intentionally not part of local preparation:

```bash
# Real ClawHub publish — DO NOT RUN without explicit approval.
clawhub package publish . --family code-plugin --tags rc \
  --changelog "ClawHub packaging, bilingual docs, synthetic demo, and privacy-safe first-use checks."

# Git tag, push, GitHub Release, npm publish, and community posts are also gated.
```

After a real ClawHub publish, wait for its automated security checks to clear before documenting the listing as installable. Do not claim download counts, users, ratings, or endorsements without traceable measurements.
