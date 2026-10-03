"""Explicit, config-scoped Codex traffic switch. No network or credential reads.

Installing this module alone changes nothing. A valid policy file opts in. CLI
requests take one route snapshot; Gateway hot-reload and session overrides must
be verified separately by the deployment adapter before claiming activation.
"""
import contextlib
import copy
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

OFF_MODEL = "deepseek-official/deepseek-v4-flash"
ON_MODEL = "cliproxy/gpt-6-luna"
PROVIDER = "cliproxy"
KEY_PROVIDER = "cliproxy_key"
BASE_URL = "http://127.0.0.1:8317/v1"
MARKER_PREFIX = "lobster-codex-disabled-v1-"
SCHEMA_VERSION = 1


class ControlError(Exception):
    """Only fixed public error codes cross the API boundary."""


def _failure(code, installed=True):
    return {"installed": installed, "ok": False, "ready": False, "mode": "blocked", "selected_model": None, "error": code}


def _read_document(path):
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except FileNotFoundError:
        raise ControlError("configuration_missing") from None
    except (OSError, ValueError):
        raise ControlError("configuration_unreadable_or_invalid") from None
    if not isinstance(document, dict):
        raise ControlError("configuration_must_be_object")
    return document, hashlib.sha256(raw).hexdigest()


def _atomic_write(path, document, expected_digest=None, must_be_absent=False):
    """Atomic replacement plus optimistic conflict detection, under our flock.

    An unrelated editor that ignores the lock can still race after the final
    digest check. Deployments needing a strict cross-writer CAS should inject a
    Gateway config.patch/baseHash adapter instead of writing concurrently.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if must_be_absent and path.exists():
            raise ControlError("concurrent_configuration_change")
        if expected_digest is not None:
            _, current_digest = _read_document(path)
            if current_digest != expected_digest:
                raise ControlError("concurrent_configuration_change")
        os.replace(temporary, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _model_pair(value):
    if isinstance(value, str):
        return value, None  # implicit fallbacks are not an exclusive route
    if isinstance(value, dict):
        return value.get("primary"), value.get("fallbacks")
    return None, None


def _references_proxy(value):
    if isinstance(value, str):
        return value.startswith(PROVIDER + "/")
    if isinstance(value, dict):
        return any(_references_proxy(item) for item in value.values())
    if isinstance(value, list):
        return any(_references_proxy(item) for item in value)
    return False


def _uncontrolled_routes(config, selected_model=None):
    conflicts = []
    agents = config.get("agents", {})
    defaults = agents.get("defaults", {})
    if _references_proxy(defaults.get("model")):
        conflicts.append("agents.defaults.model")
    def incompatible(value):
        if value is None:
            return False
        if selected_model is None:
            return _references_proxy(value)
        # Fixed per-session/channel/background overrides cannot follow both switch
        # positions without editing unrelated policy; require them absent for on.
        if selected_model == ON_MODEL:
            return True
        if isinstance(value, str):
            return value != selected_model
        primary, fallbacks = _model_pair(value)
        return primary != selected_model or bool(fallbacks)
    for key in ("subagents", "heartbeat"):
        if incompatible((defaults.get(key) or {}).get("model")):
            conflicts.append("agents.defaults." + key + ".model")
    for entry in agents.get("list", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("id") != "main" and _references_proxy(entry):
            conflicts.append("agents.other_agent_route")
        elif entry.get("id") == "main":
            for key in ("subagents", "heartbeat"):
                if incompatible((entry.get(key) or {}).get("model")):
                    conflicts.append("agents.main." + key + ".model")
    channels = config.get("channels", {}).get("modelByChannel") or {}
    def channel_values(value):
        if isinstance(value, dict):
            return [leaf for child in value.values() for leaf in channel_values(child)]
        return [value]
    if any(incompatible(value) for value in channel_values(channels)):
        conflicts.append("channels.modelByChannel")
    for profile in config.get("auth", {}).get("profiles", {}).values():
        if isinstance(profile, dict) and profile.get("provider") == PROVIDER:
            conflicts.append("auth.proxy_profile")
    return sorted(set(conflicts))


def _assert_no_alias_redirection(config):
    # OpenClaw resolves configured aliases before parsing even a full model ref.
    allowlist = config.get("agents", {}).get("defaults", {}).get("models", {})
    for key, entry in allowlist.items():
        alias = entry.get("alias") if isinstance(entry, dict) else None
        if not isinstance(alias, str):
            continue
        alias = alias.strip().lower()
        candidate = str(key).strip().lower()
        for controlled in (OFF_MODEL, ON_MODEL):
            expected = controlled.lower()
            provider, short_name = expected.split("/", 1)
            redirects_full_ref = alias == expected
            redirects_provider_alias = alias == short_name and candidate.split("/", 1)[0] == provider
            if candidate != expected and (redirects_full_ref or redirects_provider_alias):
                raise ControlError("controlled_model_alias_redirection")


def _off_available(config):
    _assert_no_alias_redirection(config)
    off_provider, off_id = OFF_MODEL.split("/", 1)
    provider = config.get("models", {}).get("providers", {}).get(off_provider)
    if not isinstance(provider, dict):
        raise ControlError("off_model_provider_unavailable")
    if not any(isinstance(item, dict) and item.get("id") == off_id for item in provider.get("models", [])):
        raise ControlError("off_model_definition_unavailable")
    allowlist = config.get("agents", {}).get("defaults", {}).get("models", {})
    if allowlist and OFF_MODEL not in allowlist:
        raise ControlError("off_model_not_allowlisted")


class TrafficController:
    def __init__(self, config_path=None, policy_path=None, validator=None, preflight=None, activation_verifier=None):
        self.config_path = Path(config_path or os.environ.get("LOBSTER_TRAFFIC_CONFIG_PATH") or Path.home() / ".openclaw" / "openclaw.json")
        self.policy_path = Path(policy_path or os.environ.get("LOBSTER_TRAFFIC_POLICY_PATH") or self.config_path.parent / "cliproxy-switch-policy.json")
        self.key_path = self.config_path.parent / "cliproxy-private" / "client-key"
        self.lock_path = self.policy_path.with_name(self.policy_path.name + ".lock")
        self.marker_path = self.config_path.parent / ".cliproxy-switch-installed.json"
        self.validator = validator
        self.preflight = preflight
        self.activation_verifier = activation_verifier
        self.injected = validator is not None or preflight is not None or activation_verifier is not None

    def _hooks(self):
        if self.injected:
            return self.validator, self.preflight, self.activation_verifier
        try:
            adapter = importlib.import_module("model_traffic_adapter")
            return adapter.validate_config, adapter.preflight, adapter.verify_activation
        except (ImportError, AttributeError):
            raise ControlError("control_adapter_unavailable") from None

    def _anchor(self):
        expected = {"schema_version": 1, "config_path": str(self.config_path.absolute()), "policy_path": str(self.policy_path.absolute()), "provider_id": PROVIDER}
        if self.marker_path.is_symlink():
            raise ControlError("installation_marker_invalid")
        if self.marker_path.exists():
            value, _ = _read_document(self.marker_path)
            if value != expected:
                raise ControlError("installation_marker_mismatch")
        return expected

    def _has_installation_trace(self):
        if self.policy_path.exists() or self.policy_path.is_symlink() or self.marker_path.exists() or self.marker_path.is_symlink():
            return True
        # A deleted policy/anchor must not silently restore legacy proxy fallback.
        try:
            config, _ = _read_document(self.config_path)
        except ControlError:
            return False
        key = config.get("models", {}).get("providers", {}).get(PROVIDER, {}).get("apiKey")
        return key == self._secret_ref() or (isinstance(key, str) and key.startswith(MARKER_PREFIX))

    def _activation(self, result, verifier):
        if verifier is None:
            return result
        try:
            checked = verifier(result["mode"], result["selected_model"], str(self.config_path.absolute()))
        except Exception:
            return dict(result, ok=False, ready=False, activation_verified=False, error="activation_check_failed")
        result["activation_verified"] = bool(checked.get("ok"))
        if checked.get("ok"):
            result["warnings"] = [item for item in result["warnings"] if item != "gateway_activation_and_session_overrides_require_external_verification"]
        result["warnings"].extend(checked.get("warnings", []))
        if not checked.get("ok"):
            result.update(ok=False, ready=False, error=checked.get("error", "activation_not_verified"))
        return result

    @contextlib.contextmanager
    def _lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # A persistent lock inode serializes controller instances and processes.
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _policy(self):
        self._anchor()
        if self.policy_path.is_symlink():
            raise ControlError("policy_symlink_not_allowed")
        policy, digest = _read_document(self.policy_path)
        expected = {"schema_version": SCHEMA_VERSION, "agent_id": "main", "off_model": OFF_MODEL, "on_model": ON_MODEL, "provider_id": PROVIDER, "key_provider_id": KEY_PROVIDER, "base_url": BASE_URL, "key_file": str(self.key_path.absolute())}
        allowed = set(expected) | {"disabled_marker", "owns_allowlist_entry"}
        if set(policy) != allowed or any(policy.get(key) != value for key, value in expected.items()):
            raise ControlError("policy_invalid")
        marker = policy.get("disabled_marker")
        if not isinstance(marker, str) or not marker.startswith(MARKER_PREFIX) or len(marker) != len(MARKER_PREFIX) + 32:
            raise ControlError("policy_invalid")
        if not isinstance(policy.get("owns_allowlist_entry"), bool):
            raise ControlError("policy_invalid")
        return policy, digest

    def _new_policy(self, config):
        models = config.get("agents", {}).get("defaults", {}).get("models", {})
        return {"schema_version": SCHEMA_VERSION, "agent_id": "main", "off_model": OFF_MODEL, "on_model": ON_MODEL, "provider_id": PROVIDER, "key_provider_id": KEY_PROVIDER, "base_url": BASE_URL, "key_file": str(self.key_path.absolute()), "disabled_marker": MARKER_PREFIX + uuid.uuid4().hex, "owns_allowlist_entry": bool(models) and ON_MODEL not in models}

    def _secret_ref(self):
        return {"source": "file", "provider": KEY_PROVIDER, "id": "value"}

    def _secret_provider(self):
        return {"source": "file", "path": str(self.key_path.absolute()), "mode": "singleValue"}

    def _key_ready(self):
        # Metadata only: never open/read/copy/log the key, including on activation.
        try:
            info = self.key_path.lstat()
            directory = self.key_path.parent.lstat()
        except FileNotFoundError:
            raise ControlError("key_file_missing") from None
        if not stat.S_ISREG(info.st_mode) or not stat.S_ISDIR(directory.st_mode):
            raise ControlError("key_file_or_directory_not_regular")
        if stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(directory.st_mode) & 0o077:
            raise ControlError("key_file_permissions_require_0600_private_directory")
        if info.st_uid != os.geteuid() or directory.st_uid != os.geteuid():
            raise ControlError("key_file_wrong_owner")
        if not 0 < info.st_size <= 65536:
            raise ControlError("key_file_empty_or_oversized")

    def _infer(self, config, policy):
        _off_available(config)
        agents = config.get("agents", {})
        entries = [entry for entry in agents.get("list", []) if isinstance(entry, dict) and entry.get("id") == "main"]
        if len(entries) != 1:
            raise ControlError("main_agent_missing_or_ambiguous")
        configured_agents = [entry for entry in agents.get("list", []) if isinstance(entry, dict)]
        defaults = [entry for entry in configured_agents if entry.get("default") is True]
        selected_agent = defaults[0] if len(defaults) == 1 else configured_agents[0] if not defaults else {}
        if selected_agent.get("id") != "main":
            raise ControlError("controlled_main_is_not_default_agent")
        selected, fallbacks = _model_pair(entries[0].get("model", agents.get("defaults", {}).get("model")))
        provider = config.get("models", {}).get("providers", {}).get(PROVIDER, {})
        mode = "on" if selected == ON_MODEL else "off" if selected == OFF_MODEL else None
        if mode is None or fallbacks != []:
            raise ControlError("controlled_agent_route_not_exclusive")
        model_overrides = any(isinstance(item, dict) and (item.get("headers") or item.get("baseUrl") not in {None, BASE_URL} or item.get("api") not in {None, "openai-completions"}) for item in provider.get("models", []))
        if provider.get("baseUrl") != BASE_URL or provider.get("api") != "openai-completions" or provider.get("headers") or provider.get("request") or model_overrides:
            raise ControlError("controlled_provider_transport_mismatch")
        if provider.get("auth") != "api-key" or provider.get("authHeader") is not True:
            raise ControlError("controlled_provider_auth_mismatch")
        expected_key = self._secret_ref() if mode == "on" else policy["disabled_marker"]
        if provider.get("apiKey") != expected_key:
            raise ControlError("controlled_provider_auth_mismatch")
        conflicts = _uncontrolled_routes(config, selected)
        if mode == "on":
            if conflicts:
                raise ControlError("uncontrolled_proxy_route_or_auth")
            if config.get("secrets", {}).get("providers", {}).get(KEY_PROVIDER) != self._secret_provider():
                raise ControlError("secret_provider_mismatch")
            allowlist = agents.get("defaults", {}).get("models", {})
            if allowlist and ON_MODEL not in allowlist:
                raise ControlError("controlled_model_not_allowlisted")
            self._key_ready()
        return {"installed": True, "ok": True, "ready": True, "mode": mode, "selected_model": selected, "fallbacks": [], "activation_verified": False, "scope": "main_agent_config_and_stock_cli", "warnings": ["gateway_activation_and_session_overrides_require_external_verification"] + (["uncontrolled_routes_remain_disabled_provider_applied"] if conflicts else []), "uncontrolled_routes": conflicts}

    def _pure_status(self):
        try:
            if not self._has_installation_trace():
                return {"installed": False, "ok": True, "ready": False, "mode": "off", "selected_model": None, "warnings": ["switch_not_initialized"]}
            with self._lock():
                self._anchor()
                policy, _ = self._policy()
                config, digest = _read_document(self.config_path)
                return dict(self._infer(config, policy), config_revision=digest)
        except ControlError as exc:
            return _failure(str(exc))
        except (OSError, TypeError, AttributeError):
            return _failure("control_io_or_configuration_error")

    def get_status(self):
        result = self._pure_status()
        if result.get("installed") and result.get("ok"):
            try:
                _, _, verifier = self._hooks()
                return self._activation(result, verifier)
            except ControlError as exc:
                return dict(result, ok=False, ready=False, error=str(exc))
            except (OSError, ValueError, TypeError, AttributeError):
                return dict(result, ok=False, ready=False, error="activation_check_failed")
        return result

    def route_for_request(self):
        # Stock CLI pins this path into the child process environment. It uses an
        # explicit exclusive model, independent of Gateway availability.
        result = self._pure_status()
        if result.get("installed") and result.get("ok"):
            result["config_path"] = str(self.config_path.absolute())
        return result

    def set_mode(self, mode):
        configured = False
        if mode not in {"on", "off"}:
            return _failure("mode_must_be_on_or_off", self.policy_path.exists())
        try:
            with self._lock():
                validator, preflight, verifier = self._hooks()
                if validator is None:
                    raise ControlError("config_schema_validator_unavailable")
                anchor = self._anchor()
                config, config_digest = _read_document(self.config_path)
                if not isinstance(config.get("agents", {}).get("list"), list):
                    raise ControlError("main_agent_missing_or_ambiguous")
                entries = [entry for entry in config["agents"]["list"] if isinstance(entry, dict) and entry.get("id") == "main"]
                if len(entries) != 1:
                    raise ControlError("main_agent_missing_or_ambiguous")
                existing_policy = self.policy_path.exists() or self.policy_path.is_symlink()
                known_installation = self.marker_path.exists()
                existing_provider = config.get("models", {}).get("providers", {}).get(PROVIDER)
                if existing_provider is not None and not known_installation:
                    raise ControlError("proxy_provider_already_owned_elsewhere")
                _off_available(config)
                repaired_policy = False
                if existing_policy:
                    try:
                        policy, policy_digest = self._policy()
                    except ControlError:
                        if mode == "on":
                            raise ControlError("policy_invalid_use_off_to_repair") from None
                        policy, policy_digest = self._new_policy(config), None
                        # Without valid ownership evidence, preserve existing entries.
                        policy["owns_allowlist_entry"] = False
                        repaired_policy = True
                else:
                    policy, policy_digest = self._new_policy(config), None
                if mode == "on":
                    self._key_ready()
                    if _uncontrolled_routes(config, ON_MODEL):
                        raise ControlError("uncontrolled_proxy_route_or_auth")
                    previous_secret = config.get("secrets", {}).get("providers", {}).get(KEY_PROVIDER)
                    if previous_secret is not None and previous_secret != self._secret_provider():
                        raise ControlError("secret_provider_already_owned_elsewhere")
                if preflight is not None:
                    checked = preflight(mode, config, policy, str(self.config_path.absolute()))
                    if not checked.get("ok"):
                        raise ControlError(checked.get("error", "switch_preflight_failed"))
                updated = copy.deepcopy(config)
                main = next(entry for entry in updated["agents"]["list"] if entry.get("id") == "main")
                main["model"] = {"primary": ON_MODEL if mode == "on" else OFF_MODEL, "fallbacks": []}
                provider = updated.setdefault("models", {}).setdefault("providers", {}).setdefault(PROVIDER, {})
                provider.update(baseUrl=BASE_URL, api="openai-completions", auth="api-key", authHeader=True, apiKey=self._secret_ref() if mode == "on" else policy["disabled_marker"])
                # Header credentials must not bypass the controlled apiKey surface.
                provider.pop("headers", None)
                provider.pop("request", None)
                models = provider.setdefault("models", [])
                # Model-level headers/URLs must not bypass the provider switch.
                for item in models:
                    if isinstance(item, dict):
                        for transport_key in ("headers", "baseUrl", "api"):
                            item.pop(transport_key, None)
                if not any(isinstance(item, dict) and item.get("id") == "gpt-6-luna" for item in models):
                    models.append({"id": "gpt-6-luna", "name": "GPT-6 Luna"})
                allowlist = updated["agents"].setdefault("defaults", {}).get("models", {})
                if mode == "on":
                    if allowlist:
                        if ON_MODEL not in allowlist:
                            policy["owns_allowlist_entry"] = True
                        allowlist.setdefault(ON_MODEL, {})
                    updated.setdefault("secrets", {}).setdefault("providers", {})[KEY_PROVIDER] = self._secret_provider()
                elif policy["owns_allowlist_entry"] and allowlist.get(ON_MODEL) == {}:
                    allowlist.pop(ON_MODEL, None)
                if not validator(updated):
                    raise ControlError("config_schema_validation_failed")
                # Infer before writing: malformed/unmanaged inputs cannot yield a
                # successful route. off can still disable provider despite conflicts.
                self._infer(updated, policy)
                if not self.marker_path.exists():
                    _atomic_write(self.marker_path, anchor, must_be_absent=True)
                if not existing_policy or repaired_policy:
                    # Opt in before config mutation. A crash between files is blocked
                    # by inference, never interpreted as an active proxy route.
                    _atomic_write(self.policy_path, policy, must_be_absent=not existing_policy)
                else:
                    _atomic_write(self.policy_path, policy, expected_digest=policy_digest)
                try:
                    _atomic_write(self.config_path, updated, expected_digest=config_digest)
                    configured = True
                except Exception:
                    # A directory-fsync error may occur after atomic replacement.
                    # Report the observed configuration, not a false no-write claim.
                    try:
                        current, _ = _read_document(self.config_path)
                        configured = current == updated
                    except ControlError:
                        pass
                    raise
                result = self._infer(updated, policy)
                result["configured"] = True
                result["activation_verified"] = False
                if repaired_policy:
                    result["warnings"].append("invalid_policy_repaired_by_explicit_off")
                return self._activation(result, verifier)
        except ControlError as exc:
            return dict(_failure(str(exc), self._has_installation_trace()), configured=configured)
        except Exception:
            return dict(_failure("control_io_or_configuration_error", self.policy_path.exists() or self.marker_path.exists()), configured=configured)


def get_status(config_path=None, policy_path=None):
    return TrafficController(config_path, policy_path).get_status()


def route_for_request(config_path=None, policy_path=None):
    return TrafficController(config_path, policy_path).route_for_request()


def set_mode(mode, config_path=None, policy_path=None):
    return TrafficController(config_path, policy_path).set_mode(mode)
