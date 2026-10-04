"""Synthetic metadata and mocked subprocess tests; never touch live OpenClaw."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import model_traffic_adapter as adapter


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.config_path = self.state / "openclaw.json"
        self.config = {"agents": {"defaults": {"model": {"primary": adapter._EXPECTED_MODELS["off"], "fallbacks": []}}, "list": [{"id": "main", "model": {"primary": adapter._EXPECTED_MODELS["off"], "fallbacks": []}}, {"id": "custom-ollama"}]}}
        self.config_path.write_text(json.dumps(self.config))
        self.policy = {"agent_id": "main"}
        self.auth_db = self.state / "agents/main/agent/openclaw-agent.sqlite"
        self.auth_db.parent.mkdir(parents=True)
        self.state_db = self.state / "state/openclaw.sqlite"
        self.state_db.parent.mkdir(parents=True)
        with sqlite3.connect(self.auth_db) as db:
            db.execute("CREATE TABLE auth_profile_store (store_key TEXT PRIMARY KEY, store_json TEXT)")
            db.execute("INSERT INTO auth_profile_store VALUES ('primary', ?)", (json.dumps({"profiles": {}}),))
        with sqlite3.connect(self.state_db) as db:
            db.execute("CREATE TABLE cron_jobs (agent_id TEXT, payload_model TEXT, payload_fallbacks_json TEXT, enabled INTEGER)")
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def preflight(self, mode="off"):
        return adapter.preflight(mode, self.config, self.policy, self.config_path)

    def profiles(self, values):
        with sqlite3.connect(self.auth_db) as db:
            db.execute("UPDATE auth_profile_store SET store_json = ?", (json.dumps({"profiles": values}),))

    def sessions(self, entry):
        path = self.state / "agents/main/sessions/sessions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"synthetic-session": entry}))

    def cron(self, agent, model=None, fallbacks=None, enabled=1):
        with sqlite3.connect(self.state_db) as db:
            db.execute("INSERT INTO cron_jobs VALUES (?, ?, ?, ?)", (agent, model, json.dumps(fallbacks or []), enabled))

    def test_environment_pins_config_state_and_main_without_changing_parent(self):
        original = {"PATH": "/synthetic", "OPENCLAW_CONFIG_PATH": "/old/config.json", "UNRELATED": "preserve"}
        result = adapter.controlled_subprocess_env(self.config_path, original)
        self.assertEqual(result["OPENCLAW_CONFIG_PATH"], str(self.config_path))
        self.assertEqual(result["OPENCLAW_STATE_DIR"], str(self.state))
        self.assertEqual(result["OPENCLAW_AGENT_DIR"], str(self.auth_db.parent))
        self.assertEqual(result["PI_CODING_AGENT_DIR"], str(self.auth_db.parent))
        self.assertEqual(result["UNRELATED"], "preserve")
        self.assertEqual(original["OPENCLAW_CONFIG_PATH"], "/old/config.json")

    def test_environment_rejects_profile_container_gateway_or_agent_conflict(self):
        conflicts = {"OPENCLAW_PROFILE": "other", "OPENCLAW_CONTAINER": "other", "OPENCLAW_CONTAINER_HINT": "other", "OPENCLAW_GATEWAY_URL": "ws://other", "OPENCLAW_GATEWAY_PORT": "9999", "OPENCLAW_STATE_DIR": "/other", "OPENCLAW_AGENT_DIR": "/other", "PI_CODING_AGENT_DIR": "/other"}
        for key, value in conflicts.items():
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "^controlled_environment_conflict$"):
                adapter.controlled_subprocess_env(self.config_path, {key: value})

    def test_environment_allows_matching_explicit_local_paths(self):
        result = adapter.controlled_subprocess_env(self.config_path, {"OPENCLAW_PROFILE": "default", "OPENCLAW_STATE_DIR": str(self.state), "OPENCLAW_AGENT_DIR": str(self.auth_db.parent)})
        self.assertEqual(result["OPENCLAW_STATE_DIR"], str(self.state))

    def test_off_preflight_needs_no_key_no_model_or_gateway_calls(self):
        with mock.patch.object(adapter, "_runtime_route", side_effect=AssertionError("off preflight must remain usable offline")):
            self.assertTrue(self.preflight()["ok"])
        self.assertFalse((self.state / "cliproxy-private").exists())

    def test_on_preflight_requires_runtime_reachable(self):
        with mock.patch.object(adapter, "_runtime_route", return_value=None):
            self.assertEqual(self.preflight("on")["error"], "gateway_runtime_unavailable")
        with mock.patch.object(adapter, "_runtime_route", return_value=(adapter._EXPECTED_MODELS["off"], [])) as rpc:
            self.assertTrue(self.preflight("on")["ok"])
            rpc.assert_called_once()

    def test_reload_off_restart_and_remote_fail_closed(self):
        for mode in ("off", "restart", "unexpected"):
            with self.subTest(mode=mode):
                self.config["gateway"] = {"reload": {"mode": mode}}
                self.assertEqual(self.preflight()["error"], "gateway_hot_reload_required")
        self.config["gateway"] = {"mode": "remote"}
        self.assertEqual(self.preflight()["error"], "local_gateway_required")

    def test_hot_and_hybrid_reload_are_supported(self):
        for mode in ("hot", "hybrid"):
            self.config["gateway"] = {"reload": {"mode": mode}}
            self.assertTrue(self.preflight()["ok"])

    def test_background_or_channel_model_cannot_bypass_selection(self):
        for node in (self.config["agents"]["defaults"], self.config["agents"]["list"][0]):
            for section in ("heartbeat", "subagents"):
                node[section] = {"model": "synthetic/other"}
                self.assertEqual(self.preflight()["error"], "controlled_background_model_override")
                node.pop(section)
        self.config["channels"] = {"modelByChannel": {"synthetic": {"any": "synthetic/other"}}}
        self.assertEqual(self.preflight()["error"], "channel_model_override_unsupported")

    def test_actual_overrides_block_but_previous_used_model_does_not(self):
        self.sessions({"model": "old", "modelProvider": "cliproxy"})
        self.assertTrue(self.preflight()["ok"])
        for field in ("modelOverride", "providerOverride", "liveModelSwitchPending"):
            self.sessions({field: "synthetic"})
            self.assertEqual(self.preflight()["error"], "controlled_session_model_override")

    def test_custom_session_and_agent_paths_fail_closed(self):
        self.config["session"] = {"store": "/unverified/store.json"}
        self.assertEqual(self.preflight()["error"], "custom_session_store_unsupported")
        self.config.pop("session")
        self.config["agents"]["list"][0]["agentDir"] = "/unverified/agent"
        self.assertEqual(self.preflight()["error"], "custom_agent_directory_unsupported")

    def test_sql_authoritative_auth_detects_clip_profiles_without_key_output(self):
        self.profiles({"synthetic-profile": {"provider": "ClIpRoXy", "key": "NEVER_RETURN_SYNTHETIC_KEY"}})
        result = self.preflight()
        self.assertEqual(result["error"], "uncontrolled_proxy_auth_profile")
        self.assertNotIn("NEVER_RETURN", json.dumps(result))

    def test_config_auth_profile_also_blocks(self):
        self.config["auth"] = {"profiles": {"synthetic": {"provider": "cliproxy"}}}
        self.assertEqual(self.preflight()["error"], "uncontrolled_proxy_auth_profile")

    def test_known_deepseek_profile_allowed_unknown_auth_selection_blocked(self):
        self.sessions({"authProfileOverride": "existing", "authProfileOverrideSource": "auto"})
        self.assertEqual(self.preflight()["error"], "controlled_session_auth_override")
        self.profiles({"existing": {"provider": "deepseek-official", "key": "SYNTHETIC"}})
        self.assertTrue(self.preflight()["ok"])

    def test_missing_or_invalid_auth_store_fail_closed_without_legacy_key_read(self):
        self.auth_db.unlink()
        self.assertEqual(self.preflight()["error"], "authoritative_state_metadata_unavailable")
        self.assertFalse((self.auth_db.parent / "auth-profiles.json").exists())

    def test_shared_legacy_auth_table_is_not_used_as_authority(self):
        self.profiles({"synthetic": {"provider": "cliproxy"}})
        with sqlite3.connect(self.state_db) as db:
            db.execute("CREATE TABLE auth_profile_stores (store_json TEXT)")
            db.execute("INSERT INTO auth_profile_stores VALUES ('{}')")
        self.assertEqual(self.preflight()["error"], "uncontrolled_proxy_auth_profile")

    def test_main_cron_explicit_route_blocks_and_other_agent_route_is_preserved(self):
        self.cron("custom-ollama", "ollama/synthetic")
        self.assertTrue(self.preflight()["ok"])
        self.cron(None, "deepseek-official/synthetic")
        self.assertEqual(self.preflight()["error"], "controlled_cron_model_override")

    def test_cron_clipproxy_fallback_and_main_fallback_block(self):
        self.cron("custom-ollama", fallbacks=["cliproxy/gpt-6-luna"])
        self.assertEqual(self.preflight()["error"], "uncontrolled_proxy_cron_route")

    def test_validate_uses_only_pure_leaf_and_stdin_never_config_in_argv(self):
        config = {"synthetic": "DO_NOT_LOG"}
        result = subprocess.CompletedProcess([], 0, '{"ok":true}', "")
        with mock.patch.object(adapter, "_installed_paths", return_value=("/fake/openclaw", "/fake/node", "/fake/leaf.js")), mock.patch.object(adapter.subprocess, "run", return_value=result) as run:
            self.assertTrue(adapter.validate_config(config))
        call = run.call_args
        self.assertNotIn("DO_NOT_LOG", repr(call.args))
        self.assertEqual(json.loads(call.kwargs["input"]), config)
        self.assertTrue(call.kwargs["capture_output"])
        self.assertNotIn("gateway", call.args[0])

    def test_validation_errors_never_escape(self):
        for value in (subprocess.TimeoutExpired("PRIVATE_ARG", 10), ValueError("PRIVATE_CONFIG_ERROR"), OSError("PRIVATE_PATH")):
            with mock.patch.object(adapter, "_installed_paths", side_effect=value):
                self.assertFalse(adapter.validate_config({}))

    def test_runtime_rpc_is_profile_bound_and_does_not_use_last_session_model(self):
        output = {"agents": [{"id": "main", "model": {"primary": adapter._EXPECTED_MODELS["off"]}}, {"id": "other", "model": {"primary": "other/model"}}]}
        completed = subprocess.CompletedProcess([], 0, json.dumps(output), "PRIVATE_UNFORWARDED")
        with mock.patch.object(adapter.shutil, "which", return_value="/fake/openclaw"), mock.patch.object(adapter.subprocess, "run", return_value=completed) as run:
            self.assertEqual(adapter._runtime_route(self.config_path, 4.5), (adapter._EXPECTED_MODELS["off"], []))
        self.assertEqual(run.call_args.args[0][1:4], ["gateway", "call", "agents.list"])
        self.assertEqual(run.call_args.kwargs["env"]["OPENCLAW_CONFIG_PATH"], str(self.config_path))

    def test_verify_accepts_runtime_ack_with_no_fallbacks(self):
        with mock.patch.object(adapter, "_runtime_route", return_value=(adapter._EXPECTED_MODELS["off"], [])):
            self.assertTrue(adapter.verify_activation("off", adapter._EXPECTED_MODELS["off"], self.config_path)["ok"])

    def test_verify_rechecks_metadata_after_runtime_ack(self):
        def changed(*args):
            self.sessions({"modelOverride": "synthetic"})
            return adapter._EXPECTED_MODELS["off"], []
        with mock.patch.object(adapter, "_runtime_route", side_effect=changed):
            result = adapter.verify_activation("off", adapter._EXPECTED_MODELS["off"], self.config_path)
        self.assertEqual(result["error"], "controlled_session_model_override")

    def test_verify_polls_until_new_runtime_model_without_forcing_live_session(self):
        routes = [(adapter._EXPECTED_MODELS["on"], []), (adapter._EXPECTED_MODELS["off"], [])]
        with mock.patch.object(adapter, "_runtime_route", side_effect=routes) as rpc, mock.patch.object(adapter.time, "sleep") as sleep:
            self.assertTrue(adapter.verify_activation("off", adapter._EXPECTED_MODELS["off"], self.config_path)["ok"])
        self.assertEqual(rpc.call_count, 2)
        sleep.assert_called_once_with(0.2)

    def test_verify_bounded_timeout_does_not_accept_nonempty_fallbacks(self):
        ticks = iter([0.0, 0.0, 9.95, 10.0])
        with mock.patch.object(adapter.time, "monotonic", side_effect=lambda: next(ticks)), mock.patch.object(adapter.time, "sleep") as sleep, mock.patch.object(adapter, "_runtime_route", return_value=(adapter._EXPECTED_MODELS["off"], ["cliproxy/gpt-6-luna"])):
            result = adapter.verify_activation("off", adapter._EXPECTED_MODELS["off"], self.config_path)
        self.assertEqual(result["error"], "gateway_activation_not_confirmed")
        self.assertLessEqual(sleep.call_args.args[0], 0.2)

    def test_verify_unavailable_gateway_does_not_leak_captured_output(self):
        ticks = iter([0.0, 0.0, 10.0])
        with mock.patch.object(adapter.time, "monotonic", side_effect=lambda: next(ticks)), mock.patch.object(adapter, "_runtime_route", side_effect=subprocess.CalledProcessError(1, "synthetic", output="PRIVATE_BODY", stderr="PRIVATE_KEY")):
            result = adapter.verify_activation("off", adapter._EXPECTED_MODELS["off"], self.config_path)
        self.assertEqual(result["error"], "gateway_runtime_unavailable")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_verify_rejects_stale_config_or_invalid_target(self):
        self.assertEqual(adapter.verify_activation("off", "wrong/model", self.config_path)["error"], "activation_target_invalid")
        self.assertEqual(adapter.verify_activation("on", adapter._EXPECTED_MODELS["on"], self.config_path)["error"], "controlled_agent_route_changed")


if __name__ == "__main__":
    unittest.main()
