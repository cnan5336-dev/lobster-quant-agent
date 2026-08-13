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

if [[ ! -d node_modules ]]; then
  npm install --omit=peer
fi

python_command="python3"
if [[ -x .venv/bin/python ]]; then
  python_command=".venv/bin/python"
fi

"$python_command" -m compileall -q python/lobster_quant_agent python/test_stock_assistant.py
"$python_command" -m unittest discover -s python -p 'test_*.py'

validation_state="$(mktemp -d)"
cleanup() {
  if [[ -n "${validation_state:-}" && -d "$validation_state" ]]; then
    rm -rf -- "$validation_state"
  fi
}
trap cleanup EXIT

LOBSTER_QUANT_HOME="$validation_state" \
  "$python_command" python/lobster_quant_agent/cli.py strategy dry-run \
  "510050价格高于3元时提醒买入" >/dev/null

npm run plugin:validate
npm test
npm audit --omit=peer
python3 scripts/privacy_audit.py .

echo "Validation passed. No messages were sent and no monitor was started."
