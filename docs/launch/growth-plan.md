# 30-day and 90-day adoption plan

Status: planning draft. Every number below is a **TARGET**, not a current result.

## Principles

- Optimize for successful, safe first use—not impressions.
- Keep telemetry out of the plugin. Measure only public repository/registry events and opt-in feedback.
- Publish one useful artifact per community, adapt it to that community, and do not cross-post spam.
- Close the loop: every repeated install failure should become a test, clearer prerequisite, or documented limitation.
- Do not pay for, coordinate, or ask for votes, stars, comments, or endorsements.

## First 30 days after an approved release

### Days 0–3: release integrity

- Complete every item in `release-checklist.md` on one immutable commit.
- Publish ClawHub only after folder/tarball validation and explicit approval.
- Wait for ClawHub security scan visibility before announcing its install command.
- Open one pinned feedback issue using the trial-feedback template.
- TARGET: `[TBD]` verified clean installs across maintainer-owned macOS and Linux test environments.
- TARGET: zero unresolved P0/P1 privacy, credential, unintended-send, or packaging findings.

### Days 4–10: small feedback cohort

- Invite 3–5 real OpenClaw users individually only where an existing relationship and community rules permit; do not represent them as customers or endorsers.
- Ask each tester to use a fresh environment and the public docs, then submit only synthetic/redacted evidence.
- Classify friction by prerequisite, install, runtime discovery, data-source behavior, channel setup, and expectations.
- TARGET: 3–5 completed opt-in test reports.
- TARGET: `[TBD after baseline]` median time to first synthetic demo.
- TARGET: 100% of missing-target tests produce a fail-closed result.

### Days 11–20: tutorials and fixes

- Publish at most one Chinese technical tutorial after reproducing every command.
- Land fixes for repeated first-run issues before broader promotion.
- Add a Linux result to the compatibility matrix once verified on the exact release.
- TARGET: at least `[TBD]` issue reports with enough reproduction detail to act on.
- TARGET: at least `[TBD]%` of feedback issues receive a maintainer response within `[TBD]` business days.

### Days 21–30: community showcases

- Choose only relevant venues: r/openclaw during Showcase Weekend, the current OpenClaw Discord showcase channel, and one Chinese maker community.
- Use the platform-specific drafts; answer questions directly and record recurring confusion.
- Consider Show HN only if independent users can install the public artifact without hand-holding and the maker can stay in the thread.
- Publish a transparent 30-day retrospective with measured values or `not available`; do not estimate missing metrics.

## Days 31–90

### Month 2: reliability and contributor path

- Turn the top three repeated failure modes into deterministic tests.
- Add a tested Linux install matrix and document unsupported environments.
- Label upstream data-source failures consistently and record data cutoffs in reports.
- Review dependency/security alerts and publish patches through the same release gates.
- TARGET: `[TBD after month-1 baseline]` first-use success rate among opt-in testers.
- TARGET: `[TBD]` external pull requests or actionable issue reproductions; zero is acceptable and must be reported honestly.

### Month 3: retention without surveillance

- Ask opt-in testers whether they still use at least one workflow at day 30/60; do not add phone-home telemetry.
- Prioritize workflows with repeated voluntary use, not feature-request volume alone.
- Publish one architecture/privacy note and one reproducible data-quality case study.
- Decide whether the project should remain a focused plugin, split optional data adapters, or pause expansion.
- TARGET: `[TBD]` opt-in 30-day retained testers.
- TARGET: `[TBD]` releases with complete provenance and zero accidental external actions.

## Stop conditions

Pause promotion and return to engineering if any of these occur:

- credential, account, target, holding, watchlist, cache, log, screenshot, or generated-result exposure;
- unintended message delivery or monitoring startup;
- package/source provenance mismatch;
- install instructions depend on an unpublished artifact;
- a data-source failure is presented as a confirmed market fact;
- a user interprets the project as a trading or order-execution system because the boundary is unclear.
