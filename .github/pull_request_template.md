## What changed

Describe the focused behavior or documentation change.

## Evidence

- [ ] `./scripts/validate.sh`
- [ ] `python3 scripts/privacy_audit.py .`
- [ ] Synthetic/redacted fixtures only
- [ ] Package or generated assets updated when applicable

## Safety boundaries

- [ ] No credentials, account/chat ids, notification targets, holdings, watchlists, private paths, logs, screenshots, caches, or real generated results
- [ ] No broker connection or order execution
- [ ] No message send or monitor startup in tests
- [ ] Data-source assumptions and delayed/stale behavior are documented
