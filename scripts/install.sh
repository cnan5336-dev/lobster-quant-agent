#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

for command_name in node npm python3 openclaw; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing prerequisite: $command_name" >&2
    exit 1
  }
done

if ! node -e 'const a=process.versions.node.split(".").map(Number), b=[22,22,3]; process.exit(a[0]>b[0] || (a[0]===b[0] && (a[1]>b[1] || (a[1]===b[1] && a[2]>=b[2]))) ? 0 : 1)'; then
  echo "Node.js 22.22.3 or newer is required." >&2
  exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required." >&2
  exit 1
fi

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r python/requirements.txt
npm install --omit=peer
npm run plugin:validate
npm test
npm audit --omit=peer
.venv/bin/python -m unittest discover -s python -p 'test_*.py'
python3 scripts/privacy_audit.py .

if [[ ! -f config/lobster-quant-agent.local.json ]]; then
  cp config/lobster-quant-agent.example.json config/lobster-quant-agent.local.json
fi

inspect_file="$(mktemp)"
cleanup() {
  if [[ -n "${inspect_file:-}" && -f "$inspect_file" ]]; then
    rm -f -- "$inspect_file"
  fi
}
trap cleanup EXIT

if openclaw plugins inspect lobster-quant-agent --json >"$inspect_file" 2>/dev/null; then
  installed_root="$(python3 - "$inspect_file" <<'PY'
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text())
print((data.get("plugin") or {}).get("rootDir") or "")
PY
)"
  if [[ -n "$installed_root" && "$installed_root" != "$repo_root" ]]; then
    echo "A different lobster-quant-agent installation already exists at: $installed_root" >&2
    echo "Review it manually; this installer will not overwrite it." >&2
    exit 1
  fi
else
  openclaw plugins install -l "$repo_root"
fi

openclaw plugins enable lobster-quant-agent

state_dir="${LOBSTER_QUANT_STATE_DIR:-${HOME}/.openclaw/lobster-quant-agent}"
batch_json="$(python3 - "$repo_root/.venv/bin/python" "$state_dir" <<'PY'
import json, sys
print(json.dumps([
    {"path": "plugins.entries.lobster-quant-agent.config.pythonExecutable", "value": sys.argv[1]},
    {"path": "plugins.entries.lobster-quant-agent.config.stateDirectory", "value": sys.argv[2]},
    {"path": "plugins.entries.lobster-quant-agent.config.defaultChannel", "value": "telegram"},
    {"path": "plugins.entries.lobster-quant-agent.config.notifyChannels", "value": []},
    {"path": "plugins.entries.lobster-quant-agent.config.notificationTargets", "value": {}},
]))
PY
)"
openclaw config set --batch-json "$batch_json" --dry-run
openclaw config set --batch-json "$batch_json"
openclaw config validate
openclaw plugins inspect lobster-quant-agent --runtime --json >/dev/null

echo "Lobster Quant Agent is installed for OpenClaw."
echo "No messages were sent, no monitor was started, and no credentials were created."
echo "Next: edit only local OpenClaw configuration with your own channel credentials and targets."
