# Explicit retrieval control validation — 2026-10-18

Base: PR #1 head `efb84af45a550a0944fa9e983ac2b7edc4c6c685`.
Scope: additive, opt-in standard-library retrieval gate; no deployment changes.

Final `scripts/validate.sh` completed with an isolated npm cache:

- 525 Python regression tests passed, including 18 new synthetic retrieval tests.
- 5 TypeScript tests passed; plugin build and isolated validation passed.
- First-use smoke reported network_requests=0, messages_sent=0, broker_actions=0.
- Synthetic demo reproduced without changes.
- Package check passed with 47 files; retrieval module and documentation required.
- Dependency audit reported zero vulnerabilities; privacy audit passed.
- `git diff --check` and Python compilation passed.

New tests cover restart/cache sharing across tasks; parameter/provider separation;
URL fragments versus meaningful query parameters; TTL expiry; immutable evidence
readback; task/month limits and failure attempt ceilings; reservation before transport;
concurrent duplicate rejection; actual child-process exit during a fake transport;
unknown timeout persistence; malformed responses; empty and non-200 responses;
Beijing month rollover; missing/corrupt state; and evidence tampering.
Adapters return invented data. The new suite blocks socket construction in its process;
the child-process crash test has no network or credential code.

No real Search/Fetch adapter, production model request, credential lookup, notification,
monitor start or formal installation was performed. The tests do not establish provider
connectivity, provider-internal tool limits, bill reconciliation, or end-to-end ATI
integration. Existing unintegrated Agent tools are outside the gate's budget boundary.
