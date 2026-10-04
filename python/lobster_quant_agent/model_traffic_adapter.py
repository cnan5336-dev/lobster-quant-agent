"""Local OpenClaw validation and acknowledgement for the explicit traffic switch.

No inference, credential reads, session mutations, or service restarts occur here.
Only fixed error codes leave this boundary; subprocess output is never forwarded.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time


_EXPECTED_MODELS = {"on": "cliproxy/gpt-6-luna", "off": "deepseek-official/deepseek-v4-flash"}
_POLL_SECONDS = 0.2
_VERIFY_SECONDS = 10.0
_RPC_SECONDS = 4.5
_SCHEMA_SCRIPT = """
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
try {
  const {t: schema} = await import(pathToFileURL(process.argv[1]).href);
  const value = JSON.parse(fs.readFileSync(0, 'utf8'));
  const ok = !!schema && typeof schema.safeParse === 'function' && schema.safeParse(value).success;
  process.stdout.write(JSON.stringify({ok}));
  process.exitCode = ok ? 0 : 1;
} catch { process.stdout.write('{"ok":false}'); process.exitCode = 1; }
"""


def _failure(code):
    return {"ok": False, "error": code, "warnings": []}


def _same_path(value, expected):
    try:
        return Path(value).expanduser().absolute().resolve() == expected.resolve()
    except (OSError, ValueError, TypeError):
        return False


def controlled_subprocess_env(config_path, env=None):
    """Pin CLI children to the selected local profile; reject conflicting routing.

    The caller must not print this mapping: inherited unrelated credentials are
    retained for OpenClaw itself. The helper never opens a credential file.
    """
    result = dict(os.environ if env is None else env)
    config = Path(config_path).expanduser().absolute()
    state = config.parent
    conflicts = ("OPENCLAW_CONTAINER", "OPENCLAW_CONTAINER_HINT", "OPENCLAW_GATEWAY_URL", "OPENCLAW_GATEWAY_PORT")
    if any(str(result.get(key, "")).strip() for key in conflicts):
        raise ValueError("controlled_environment_conflict")
    if str(result.get("OPENCLAW_PROFILE", "")).strip() not in {"", "default"}:
        raise ValueError("controlled_environment_conflict")
    for name, expected in (("OPENCLAW_STATE_DIR", state), ("OPENCLAW_AGENT_DIR", state / "agents" / "main" / "agent"), ("PI_CODING_AGENT_DIR", state / "agents" / "main" / "agent")):
        value = result.get(name)
        if value and not _same_path(value, expected):
            raise ValueError("controlled_environment_conflict")
    result["OPENCLAW_CONFIG_PATH"] = str(config)
    result["OPENCLAW_STATE_DIR"] = str(state)
    result["OPENCLAW_AGENT_DIR"] = str(state / "agents" / "main" / "agent")
    result["PI_CODING_AGENT_DIR"] = str(state / "agents" / "main" / "agent")
    return result


def _installed_paths():
    executable = shutil.which("openclaw")
    node = shutil.which("node")
    if not executable or not node:
        raise ValueError("openclaw_runtime_unavailable")
    root = Path(executable).resolve().parent
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if package.get("name") != "openclaw":
        raise ValueError("openclaw_runtime_unsupported")
    # The adapter audits the leaf's named export rather than importing a CLI
    # entry point, whose startup can migrate state even for read-only commands.
    candidates = []
    for path in (root / "dist").glob("zod-schema-*.js"):
        source = path.read_text(encoding="utf-8")
        if "const OpenClawSchema =" in source and re.search(r"\bOpenClawSchema\s+as\s+t\b", source):
            candidates.append(path)
    if len(candidates) != 1:
        raise ValueError("openclaw_schema_unsupported")
    return str(executable), str(node), str(candidates[0])


def validate_config(config):
    """Validate in the installed pure leaf schema, without exposing config data."""
    if not isinstance(config, dict):
        return False
    try:
        _, node, schema = _installed_paths()
        completed = subprocess.run([node, "--input-type=module", "-e", _SCHEMA_SCRIPT, schema], input=json.dumps(config), capture_output=True, text=True, timeout=10, check=False)
        return completed.returncode == 0 and json.loads(completed.stdout) == {"ok": True}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


def _session_auth_overrides(config, state):
    expected = state / "agents" / "main" / "sessions" / "sessions.json"
    custom = config.get("session", {}).get("store")
    if custom and (not isinstance(custom, str) or not _same_path(custom.replace("{agentId}", "main"), expected)):
        raise ValueError("custom_session_store_unsupported")
    if not expected.exists():
        return []
    if expected.is_symlink() or expected.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("session_metadata_unavailable")
    entries = json.loads(expected.read_text(encoding="utf-8"))
    if not isinstance(entries, dict):
        raise ValueError("session_metadata_unavailable")
    auth_profiles = []
    for entry in entries.values():
        if not isinstance(entry, dict):
            raise ValueError("session_metadata_unavailable")
        if entry.get("modelOverride") or entry.get("providerOverride") or entry.get("liveModelSwitchPending"):
            raise ValueError("controlled_session_model_override")
        profile = entry.get("authProfileOverride")
        if profile:
            if not isinstance(profile, str):
                raise ValueError("controlled_session_auth_override")
            auth_profiles.append(profile)
    return auth_profiles


def _audit_state(config, state):
    """Read only provider identifiers and route metadata from authoritative state."""
    auth_profiles = _session_auth_overrides(config, state)
    database = state / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    if not database.is_file() or database.is_symlink():
        raise ValueError("authoritative_state_metadata_unavailable")
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1.0)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("SELECT count(*) FROM auth_profile_store WHERE store_key = 'primary' AND json_valid(store_json) AND json_type(store_json, '$.profiles') = 'object'").fetchone()[0] != 1:
            raise ValueError("authoritative_state_metadata_unavailable")
        # SQLite extracts provider metadata internally; no key/token columns or
        # whole credential documents are returned to this process.
        count = connection.execute("SELECT count(*) FROM auth_profile_store s, json_each(s.store_json, '$.profiles') p WHERE s.store_key = 'primary' AND lower(json_extract(p.value, '$.provider')) = 'cliproxy'").fetchone()[0]
        if count:
            raise ValueError("uncontrolled_proxy_auth_profile")
        for profile_id in auth_profiles:
            providers = {row[0] for row in connection.execute("SELECT DISTINCT lower(json_extract(p.value, '$.provider')) FROM auth_profile_store s, json_each(s.store_json, '$.profiles') p WHERE s.store_key = 'primary' AND p.key = ?", (profile_id,))}
            # Existing DeepSeek auth selections cannot route to cliproxy, and
            # OpenClaw clears incompatible provider selections at the next run.
            if not providers or any(provider != "deepseek-official" for provider in providers):
                raise ValueError("controlled_session_auth_override")
    database = state / "state" / "openclaw.sqlite"
    if not database.is_file() or database.is_symlink():
        raise ValueError("authoritative_state_metadata_unavailable")
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1.0)) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("SELECT agent_id, payload_model, payload_fallbacks_json FROM cron_jobs WHERE enabled = 1")
        for agent_id, model, fallbacks in rows:
            parsed_fallbacks = json.loads(fallbacks) if fallbacks else []
            if not isinstance(parsed_fallbacks, list):
                raise ValueError("cron_route_metadata_invalid")
            if agent_id in {None, "", "main"} and (model or parsed_fallbacks):
                raise ValueError("controlled_cron_model_override")
            if any(isinstance(value, str) and value.lower().startswith("cliproxy/") for value in [model] + parsed_fallbacks):
                raise ValueError("uncontrolled_proxy_cron_route")


def _configuration_preflight(config):
    gateway = config.get("gateway", {})
    reload_mode = gateway.get("reload", {}).get("mode", "hybrid")
    if reload_mode not in {"hot", "hybrid"}:
        raise ValueError("gateway_hot_reload_required")
    if gateway.get("mode", "local") != "local":
        raise ValueError("local_gateway_required")
    agents = config.get("agents", {})
    entries = [entry for entry in agents.get("list", []) if isinstance(entry, dict) and entry.get("id") == "main"]
    if len(entries) != 1:
        raise ValueError("main_agent_missing_or_ambiguous")
    main = entries[0]
    if main.get("agentDir"):
        raise ValueError("custom_agent_directory_unsupported")
    defaults = agents.get("defaults", {})
    for section in (defaults, main):
        for key in ("heartbeat", "subagents"):
            if section.get(key, {}).get("model"):
                raise ValueError("controlled_background_model_override")
    if config.get("channels", {}).get("modelByChannel"):
        raise ValueError("channel_model_override_unsupported")
    profiles = config.get("auth", {}).get("profiles", {})
    if any(isinstance(profile, dict) and str(profile.get("provider", "")).lower() == "cliproxy" for profile in profiles.values()):
        raise ValueError("uncontrolled_proxy_auth_profile")


_PREFLIGHT_ERRORS = frozenset({
    "controlled_environment_conflict", "custom_session_store_unsupported", "session_metadata_unavailable",
    "controlled_session_model_override", "controlled_session_auth_override", "authoritative_state_metadata_unavailable",
    "uncontrolled_proxy_auth_profile", "cron_route_metadata_invalid", "controlled_cron_model_override",
    "uncontrolled_proxy_cron_route", "gateway_hot_reload_required", "local_gateway_required",
    "main_agent_missing_or_ambiguous", "controlled_background_model_override", "channel_model_override_unsupported",
    "custom_agent_directory_unsupported",
})


def _metadata_preflight(mode, config, policy, config_path):
    if mode not in _EXPECTED_MODELS:
        return _failure("mode_must_be_on_or_off")
    if not isinstance(policy, dict) or policy.get("agent_id") != "main":
        return _failure("policy_invalid")
    try:
        controlled_subprocess_env(config_path)
        _configuration_preflight(config)
        _audit_state(config, Path(config_path).expanduser().absolute().parent)
        return {"ok": True, "warnings": []}
    except ValueError as error:
        code = str(error)
        return _failure(code if code in _PREFLIGHT_ERRORS else "runtime_metadata_unavailable")
    except (OSError, TypeError, AttributeError, sqlite3.Error):
        return _failure("runtime_metadata_unavailable")


def preflight(mode, config, policy, config_path):
    checked = _metadata_preflight(mode, config, policy, config_path)
    if not checked["ok"] or mode == "off":
        return checked
    # Never arm a new paid route against a stopped/unreachable gateway. Turning
    # OFF may still save the safe next-startup state; its ACK remains unverified.
    try:
        if _runtime_route(config_path, _RPC_SECONDS) is not None:
            return checked
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        pass
    return _failure("gateway_runtime_unavailable")


def _runtime_route(config_path, timeout):
    executable = shutil.which("openclaw")
    if not executable:
        return None
    environment = controlled_subprocess_env(config_path)
    completed = subprocess.run([executable, "gateway", "call", "agents.list", "--json", "--timeout", str(max(1, min(4000, int(timeout * 1000) - 250)))], capture_output=True, text=True, timeout=timeout, check=False, env=environment)
    if completed.returncode != 0:
        return None
    result = json.loads(completed.stdout)
    entries = [entry for entry in result.get("agents", []) if isinstance(entry, dict) and entry.get("id") == "main"]
    if len(entries) != 1 or not isinstance(entries[0].get("model"), dict):
        return None
    model = entries[0]["model"]
    return model.get("primary"), model.get("fallbacks", [])


def verify_activation(mode, selected_model, config_path):
    """Poll runtime configuration, never inference or a session's last-used model."""
    if _EXPECTED_MODELS.get(mode) != selected_model:
        return _failure("activation_target_invalid")
    def guard():
        try:
            with Path(config_path).expanduser().absolute().open(encoding="utf-8") as handle:
                config = json.load(handle)
            checked = _metadata_preflight(mode, config, {"agent_id": "main"}, config_path)
            if not checked["ok"]:
                return checked
            main = next(entry for entry in config["agents"]["list"] if entry.get("id") == "main")
            if main.get("model") != {"primary": selected_model, "fallbacks": []}:
                return _failure("controlled_agent_route_changed")
            return checked
        except (OSError, ValueError, TypeError):
            return _failure("runtime_metadata_unavailable")
    checked = guard()
    if not checked["ok"]:
        return checked
    deadline = time.monotonic() + _VERIFY_SECONDS
    observed = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            route = _runtime_route(config_path, min(_RPC_SECONDS, remaining))
            observed = observed or route is not None
            if route == (selected_model, []):
                return guard()
        except ValueError as error:
            if str(error) == "controlled_environment_conflict":
                return _failure("controlled_environment_conflict")
        except (OSError, TypeError, AttributeError, subprocess.SubprocessError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_POLL_SECONDS, remaining))
    return _failure("gateway_activation_not_confirmed" if observed else "gateway_runtime_unavailable")
