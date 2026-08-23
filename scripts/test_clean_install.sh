#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/lobster-clean-install.XXXXXX")"

cleanup() {
  if [[ -n "${test_root:-}" && -d "$test_root" && "$test_root" == *lobster-clean-install.* ]]; then
    find "$test_root" -depth -delete
  fi
}
trap cleanup EXIT

for command_name in git node npm python3 openclaw tar; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing prerequisite: $command_name" >&2
    exit 1
  }
done

artifact_dir="$test_root/artifact"
source_dir="$test_root/source-candidate"
source_home="$test_root/source-home"
source_state="$test_root/source-openclaw-state"
managed_home="$test_root/managed-home"
managed_state="$test_root/managed-openclaw-state"
mkdir -p "$artifact_dir" "$source_dir" \
  "$source_home" "$source_state" "$test_root/source-xdg" "$test_root/source-cache" \
  "$managed_home" "$managed_state" "$test_root/managed-xdg" "$test_root/managed-cache"

run_source() {
  env \
    HOME="$source_home" \
    OPENCLAW_STATE_DIR="$source_state" \
    XDG_CONFIG_HOME="$test_root/source-xdg" \
    XDG_CACHE_HOME="$test_root/source-cache" \
    NPM_CONFIG_CACHE="$test_root/source-cache/npm" \
    "$@"
}

run_managed() {
  env \
    HOME="$managed_home" \
    OPENCLAW_STATE_DIR="$managed_state" \
    XDG_CONFIG_HOME="$test_root/managed-xdg" \
    XDG_CACHE_HOME="$test_root/managed-cache" \
    NPM_CONFIG_CACHE="$test_root/managed-cache/npm" \
    "$@"
}

cd "$repo_root"
echo "[1/4] Packing the candidate payload"
npm pack --pack-destination "$artifact_dir" >/dev/null
tarball="$(find "$artifact_dir" -maxdepth 1 -type f -name '*.tgz' -print -quit)"
if [[ -z "$tarball" || "$tarball" != "$artifact_dir"/* ]]; then
  echo "Candidate tarball was not created inside the isolated artifact directory." >&2
  exit 1
fi

# Build a clean source snapshot from exactly the tracked + reviewable untracked files.
# Ignored dependencies, local config, runtime state, caches, and validation reports stay out.
candidate_list="$test_root/candidate-files.zlist"
candidate_archive="$test_root/source-candidate.tar"
git ls-files --cached --others --exclude-standard -z >"$candidate_list"
tar -cf "$candidate_archive" --null -T "$candidate_list"
tar -xf "$candidate_archive" -C "$source_dir"

# Path 1: README/INSTALL_WITH_AGENT source-style install from the candidate payload.
echo "[2/4] Testing source-style install in an isolated home"
cd "$source_dir"
source_install_log="$test_root/source-install.log"
if ! run_source ./scripts/install.sh >"$source_install_log" 2>&1; then
  echo "Source-style installer failed; redacted tail follows:" >&2
  tail -n 80 "$source_install_log" >&2
  exit 1
fi
run_source openclaw config validate >/dev/null
run_source openclaw plugins inspect lobster-quant-agent --runtime --json >/dev/null
run_source .venv/bin/python python/lobster_quant_agent/cli.py demo >/dev/null
run_source .venv/bin/python scripts/first_use_smoke.py >/dev/null
run_source openclaw plugins uninstall lobster-quant-agent --dry-run >/dev/null

# Path 2: managed npm-pack install, equivalent to the dependency shape ClawHub uses.
echo "[3/4] Testing managed npm-pack install in a second isolated home"
cd "$test_root"
run_managed openclaw plugins install "npm-pack:$tarball" >/dev/null
run_managed openclaw plugins enable lobster-quant-agent >/dev/null
managed_inspect="$test_root/managed-inspect.json"
run_managed openclaw plugins inspect lobster-quant-agent --runtime --json >"$managed_inspect"
managed_root="$(python3 - "$managed_inspect" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print((payload.get("plugin") or {}).get("rootDir") or "")
PY
)"
if [[ -z "$managed_root" || ! -f "$managed_root/python/lobster_quant_agent/cli.py" ]]; then
  echo "Managed install did not expose the packaged Python core." >&2
  exit 1
fi
run_managed python3 "$managed_root/python/lobster_quant_agent/cli.py" demo >/dev/null
run_managed python3 "$managed_root/scripts/first_use_smoke.py" >/dev/null
run_managed openclaw plugins uninstall lobster-quant-agent --dry-run >/dev/null

if ! run_managed openclaw plugins inspect lobster-quant-agent --json >/dev/null 2>&1; then
  echo "Managed plugin disappeared before the isolated profile cleanup step." >&2
  exit 1
fi

echo "[4/4] Confirmed uninstall dry-run; removing the complete isolated profiles"
echo "Clean install passed: source snapshot + managed npm-pack, runtime inspect, synthetic demo, offline report/backtest/fail-closed smoke, uninstall dry-run, and isolated-profile cleanup."
echo "No real OpenClaw config, channel, credential, watchlist, cache, or notification target was read or reused."
