#!/bin/sh
# Restore only an already-enabled monitor. monitor_start owns process identity/locking.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin=${PYTHON_BIN:-python3}
assistant_script=${MARKET_ASSISTANT_SCRIPT:-"$script_dir/../python/lobster_quant_agent/cli.py"}
status=$("$python_bin" "$assistant_script" monitor status 原始JSON)
enabled=$(printf '%s' "$status" | "$python_bin" -c 'import json,sys; s=json.load(sys.stdin); m=s.get("monitor", s); e=m.get("enabled"); assert isinstance(e,bool), "monitor status missing boolean enabled"; print("true" if e else "false")')
if [ "$enabled" = true ]; then
    "$python_bin" "$assistant_script" monitor_start
fi
