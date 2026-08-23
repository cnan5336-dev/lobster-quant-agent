# Privacy-preserving adoption metrics

This file defines measurement candidates. It contains no current performance claims. Set a target only after a baseline exists, and label every target with `TARGET`.

| Metric | Definition | Source | Limitation |
| --- | --- | --- | --- |
| Verified first-use completion | An opt-in tester confirms install, runtime inspect, synthetic demo, offline smoke test, and clean uninstall using the released version. | Trial-feedback issue/form | Small self-selected sample; not total users. |
| Time to first synthetic demo | Minutes from starting the published install guide to a successful `demo` output. | Opt-in tester report | Self-reported unless observed with consent. |
| Install failure rate | Failed first-use reports divided by all completed opt-in reports for the same version. | Trial-feedback issues | Unknown silent users are excluded. |
| ClawHub installs/downloads | Registry-provided count for the exact package/version, if ClawHub exposes a documented metric. | ClawHub | Record cutoff and definition; do not infer active users. |
| Repository unique cloners | GitHub traffic unique cloners for the available retention window. | GitHub repository traffic | Short retention; clones are not users or successful installs. |
| Release asset downloads | Downloads of an exact GitHub Release asset, if one is created. | GitHub Release API/UI | Downloads are not installs or retained users. |
| Actionable feedback rate | Feedback reports containing version, environment, steps, expected/actual behavior, and redacted output divided by all feedback reports. | GitHub Issues | Subjective classification; publish rubric. |
| Median maintainer response time | Median elapsed time from a valid issue to first substantive maintainer response. | GitHub Issues | Exclude spam and security issues handled privately. |
| 30-day opt-in retention | Testers who voluntarily confirm continued use of at least one workflow 30 days later divided by testers asked. | Manual opt-in follow-up | Never add in-product tracking; non-response is unknown, not churn. |
| Privacy/safety incidents | Confirmed credential/private-data exposure, unintended send, monitor startup, or broker/order action. | Security reports and issue triage | TARGET must always be zero. |

## Suggested target placeholders

- TARGET verified first-use completions, first 30 days: `[TBD after release capacity review]`
- TARGET median time to first synthetic demo: `[TBD after first 3 tests]`
- TARGET actionable feedback rate: `[TBD after baseline]`
- TARGET first substantive response: `[TBD] business days`
- TARGET privacy/safety incidents: `0`
- TARGET missing-target fail-closed checks: `100%`

## Reporting rules

- Include package version, date/timezone, and source cutoff for every snapshot.
- Write `not available` when a platform does not expose a trustworthy number.
- Never convert clones, page views, or downloads into “users.”
- Never publish a testimonial without the person's explicit permission and exact approved wording.
- Never report synthetic demo backtest values as performance.
- Do not add telemetry, device ids, account ids, or hidden network calls to improve measurement.
