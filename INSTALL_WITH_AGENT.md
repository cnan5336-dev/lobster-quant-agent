# Install Lobster Quant Agent into my OpenClaw

Give this entire file to Codex or Claude Code after cloning the repository.

## Objective

Deploy this checkout as the `lobster-quant-agent` plugin in **my existing OpenClaw installation**. OpenClaw is the required v1 runtime. Do not repackage or present the Python core as a standalone runtime or general cross-agent product.

## Safety boundaries

- Work only in this repository and the user's own OpenClaw configuration/plugin locations needed for installation.
- Do not copy, print, commit, or upload any existing OpenClaw credentials, bot tokens, emails, phone numbers, account/device/session/pairing data, chat history, holdings, watchlists, logs, caches, screenshots, or backtest results.
- Do not search the whole OpenClaw home for secrets. Use `openclaw` status/config commands and this repository's templates.
- Do not send any message, run a notification test, start monitoring, call a broker, or place an order during setup or validation.
- Do not insert credentials or personal destination identifiers into repository files. The user must enter their own secrets through OpenClaw's interactive setup or SecretRefs.
- Do not overwrite a different existing `lobster-quant-agent` installation. Stop and report its path for a user decision.

## Procedure

1. Confirm the current directory is the repository root and read `README.md`, `SECURITY.md`, and `config/lobster-quant-agent.example.json`.
2. Check prerequisites without changing user data:

   ```bash
   openclaw --version
   node --version
   python3 --version
   git status --short
   ```

   Require OpenClaw `>=2026.5.17`, Node `>=22.22.3`, Python `>=3.10`, npm, and Git on macOS or Linux.

3. Run the safe installer:

   ```bash
   ./scripts/install.sh
   ```

   It may create `.venv`, `node_modules`, `dist`, the ignored `config/lobster-quant-agent.local.json`, a private state directory, and a linked OpenClaw plugin entry. It must not send messages or start monitoring.

4. Verify the non-delivery installation:

   ```bash
   openclaw config validate
   openclaw plugins inspect lobster-quant-agent --runtime --json
   ./scripts/validate.sh
   ```

5. Run the dependency-free, no-network first-use path before asking for any channel choice:

   ```bash
   python3 python/lobster_quant_agent/cli.py demo
   python3 scripts/first_use_smoke.py
   ```

   Confirm that the demo says `synthetic_offline`, the smoke test reports zero network requests/messages/broker actions, and the missing-target check passes. These outputs are synthetic test evidence, not market performance.

6. Ask the user which OpenClaw channel they want: Telegram, WeChat (`openclaw-weixin`), or QQ (`qqbot`). If it is not configured, direct the user to complete OpenClaw's interactive channel setup themselves. Never invent or recover credentials and never paste them into this repository.
7. Ask whether the user wants model-assisted fallback. If yes, list available OpenClaw model keys with a read-only command and let the user choose a primary and up to two fallbacks. Model keys are not API keys. Do not alter global model/provider credentials.
8. Keep proactive notifications disabled by default. Only after explicit user approval, configure `notifyChannels` and a destination under `notificationTargets` in the user's local OpenClaw plugin config. Treat channel targets and account IDs as private. Do not send a test during installation unless the user separately asks for a real external send after reviewing the target.
9. Rerun `openclaw config validate`. Report exactly what was installed, which optional items remain unconfigured, and confirm that no message was sent and no monitor was started.

## Local configuration shape

The plugin config lives at `plugins.entries.lobster-quant-agent.config` in the user's OpenClaw config. Use `config/lobster-quant-agent.example.json` as the safe shape. The installer sets only:

- `pythonExecutable`: this checkout's `.venv` Python
- `stateDirectory`: a private directory outside the repository
- `defaultChannel`: initially `telegram`
- empty `notifyChannels` and `notificationTargets`

Optional user-selected values are `primaryModel`, `fallbackModels`, the default channel, and private notification targets. Credentials belong to OpenClaw's channel/provider configuration, never to this repository template.

## Safe validation definition

Validation may build code, run unit tests, parse a strategy in dry-run mode, inspect plugin metadata, and validate OpenClaw configuration. It may not execute `monitor on`, `monitor start`, `notify-test`, `simulate-alert`, `openclaw message send`, a model ping, or any broker/order action.

## Uninstall and cleanup

First inspect what OpenClaw would remove:

```bash
openclaw plugins uninstall lobster-quant-agent --dry-run
```

Only after the user confirms, uninstall the plugin with `openclaw plugins uninstall lobster-quant-agent`. Do not delete the checkout, local configuration, or configured private state directory unless the user separately identifies and approves each exact path. Uninstall does not authorize deleting watchlists, holdings labels, caches, or backtest results.

If OpenClaw rejects the write with `Config write rejected` or `size-drop`, stop. Do not bypass the guard or force-edit the configuration. Preserve the rejected artifact locally, run `openclaw config validate` and `openclaw doctor`, then follow the current OpenClaw troubleshooting guidance or ask the user how to proceed.

The ClawHub command documented in README resolves the public stable release. Before using it, inspect the package page, source link, requested version, compatibility metadata, and current scan state; do not silently substitute an rc or npm source.
