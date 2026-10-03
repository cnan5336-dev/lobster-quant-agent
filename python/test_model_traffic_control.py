"""Synthetic filesystem-only switch tests; no real config, keys, gateway or model."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import copy
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import model_traffic_control as control


def toggle_worker(args):
    config, policy, mode = args
    return control.TrafficController(config, policy, validator=lambda cfg: True).set_mode(mode)


class TrafficControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "openclaw.json"
        self.policy = self.root / "policy.json"
        self.controller = control.TrafficController(self.config, self.policy, validator=lambda cfg: True)
        self.original = {
            "agents": {"defaults": {"model": {"primary": control.OFF_MODEL, "fallbacks": []}, "models": {control.OFF_MODEL: {"alias": "existing-deepseek"}}}, "list": [{"id": "main"}, {"id": "custom-ollama", "name": "Keep me"}]},
            "models": {"providers": {"deepseek-official": {"baseUrl": "https://api.synthetic.invalid", "api": "openai-completions", "apiKey": "SYNTHETIC_UNRELATED_VALUE", "models": [{"id": "deepseek-v4-flash", "name": "Synthetic DeepSeek"}]}}},
            "channels": {},
        }
        self.write(self.original)

    def write(self, doc):
        self.config.write_text(json.dumps(doc))
        os.chmod(self.config, 0o600)

    def read(self):
        return json.loads(self.config.read_text())

    def key(self, permission=0o600):
        path = self.controller.key_path
        path.parent.mkdir(mode=0o700)
        path.write_text("SYNTHETIC_TEST_CREDENTIAL_NEVER_READ")
        os.chmod(path, permission)
        return path

    def assert_preserved(self):
        doc = self.read()
        self.assertEqual(doc["agents"]["defaults"]["model"], self.original["agents"]["defaults"]["model"])
        self.assertEqual(doc["agents"]["list"][1], self.original["agents"]["list"][1])
        self.assertEqual(doc["models"]["providers"]["deepseek-official"], self.original["models"]["providers"]["deepseek-official"])
        self.assertEqual(doc["agents"]["defaults"]["models"][control.OFF_MODEL], {"alias": "existing-deepseek"})

    def test_uninstalled_status_defaults_off_without_reading_configuration(self):
        status = self.controller.get_status()
        self.assertFalse(status["installed"])
        self.assertEqual(status["mode"], "off")
        self.assertIsNone(status["selected_model"])
        self.assertFalse(self.policy.exists())

    def test_initial_off_works_without_key_and_preserves_other_routes(self):
        with mock.patch.object(self.controller, "_key_ready", side_effect=AssertionError("off must not touch key")):
            result = self.controller.set_mode("off")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["selected_model"], control.OFF_MODEL)
        self.assertFalse(result["activation_verified"])
        self.assertEqual(self.read()["agents"]["list"][0]["model"], {"primary": control.OFF_MODEL, "fallbacks": []})
        self.assert_preserved()
        marker = self.read()["models"]["providers"][control.PROVIDER]["apiKey"]
        self.assertTrue(marker.startswith(control.MARKER_PREFIX))
        self.assertNotIn("SYNTHETIC", marker)
        self.assertNotIn(marker, json.dumps(result))

    def test_missing_key_on_fails_without_any_write(self):
        previous = self.config.read_bytes()
        result = self.controller.set_mode("on")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "key_file_missing")
        self.assertEqual(self.config.read_bytes(), previous)
        self.assertFalse(self.policy.exists())

    def test_on_and_off_never_read_or_serialize_credential_content(self):
        key = self.key()
        original_open = Path.open
        def checked_open(path, *args, **kwargs):
            if path == key:
                raise AssertionError("credential content must not be opened")
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", checked_open):
            on = self.controller.set_mode("on")
            self.assertTrue(on["ok"], on)
            self.assertEqual(self.controller.route_for_request()["selected_model"], control.ON_MODEL)
            self.assertTrue(self.controller.set_mode("off")["ok"])
        self.assertNotIn("SYNTHETIC_TEST_CREDENTIAL", self.config.read_text() + self.policy.read_text() + json.dumps(on))
        self.assert_preserved()

    def test_on_uses_file_secretref_and_exclusive_route(self):
        self.key()
        self.assertTrue(self.controller.set_mode("on")["ok"])
        doc = self.read()
        self.assertEqual(doc["agents"]["list"][0]["model"], {"primary": control.ON_MODEL, "fallbacks": []})
        self.assertEqual(doc["models"]["providers"][control.PROVIDER]["apiKey"], {"source": "file", "provider": control.KEY_PROVIDER, "id": "value"})
        self.assertEqual(doc["secrets"]["providers"][control.KEY_PROVIDER]["mode"], "singleValue")
        self.assert_preserved()

    def test_off_removes_only_allowlist_entry_owned_by_switch(self):
        self.key()
        self.assertTrue(self.controller.set_mode("on")["ok"])
        self.assertIn(control.ON_MODEL, self.read()["agents"]["defaults"]["models"])
        self.assertTrue(self.controller.set_mode("off")["ok"])
        self.assertNotIn(control.ON_MODEL, self.read()["agents"]["defaults"]["models"])
        self.assertIn(control.OFF_MODEL, self.read()["agents"]["defaults"]["models"])

    def test_preexisting_allowlist_entry_is_preserved(self):
        doc = copy.deepcopy(self.original)
        doc["agents"]["defaults"]["models"][control.ON_MODEL] = {"alias": "already-existing"}
        self.write(doc)
        self.key()
        self.assertTrue(self.controller.set_mode("on")["ok"])
        self.assertTrue(self.controller.set_mode("off")["ok"])
        self.assertEqual(self.read()["agents"]["defaults"]["models"][control.ON_MODEL], {"alias": "already-existing"})

    def test_user_modified_allowlist_entry_is_not_deleted(self):
        self.key()
        self.controller.set_mode("on")
        doc = self.read()
        doc["agents"]["defaults"]["models"][control.ON_MODEL] = {"alias": "user-change"}
        self.write(doc)
        self.assertTrue(self.controller.set_mode("off")["ok"])
        self.assertIn(control.ON_MODEL, self.read()["agents"]["defaults"]["models"])

    def test_restart_reads_actual_config_not_cached_policy_mode(self):
        self.key()
        self.controller.set_mode("on")
        fresh = control.TrafficController(self.config, self.policy, validator=lambda cfg: True)
        self.assertEqual(fresh.get_status()["mode"], "on")
        doc = self.read()
        doc["agents"]["list"][0]["model"]["fallbacks"] = [control.OFF_MODEL]
        self.write(doc)
        status = fresh.route_for_request()
        self.assertFalse(status["ok"])
        self.assertIsNone(status["selected_model"])

    def test_corrupt_policy_is_fail_closed_and_explicit_off_repairs(self):
        self.controller.set_mode("off")
        self.policy.write_text("{broken")
        self.assertFalse(self.controller.route_for_request()["ok"])
        self.assertTrue(self.controller.route_for_request()["installed"])
        self.assertFalse(self.controller.set_mode("on")["ok"])
        self.assertTrue(self.controller.set_mode("off")["ok"])
        self.assertEqual(self.controller.get_status()["mode"], "off")

    def test_corrupt_config_never_claims_off_success(self):
        self.controller.set_mode("off")
        self.config.write_text("{broken")
        self.assertFalse(self.controller.set_mode("off")["ok"])
        self.assertFalse(self.controller.get_status()["ok"])
        self.assertEqual(self.config.read_text(), "{broken")

    def test_on_rejects_unsafe_key_permissions_empty_key_and_symlink(self):
        key = self.key(0o644)
        self.assertFalse(self.controller.set_mode("on")["ok"])
        key.chmod(0o600)
        key.write_text("")
        self.assertFalse(self.controller.set_mode("on")["ok"])
        target = self.root / "synthetic-other"
        target.write_text("SYNTHETIC")
        key.unlink()
        key.symlink_to(target)
        self.assertFalse(self.controller.set_mode("on")["ok"])

    def test_uncontrolled_routes_reject_on_but_off_cuts_provider(self):
        self.key()
        mutations = [
            lambda doc: doc["agents"]["list"][1].update(model={"primary": control.ON_MODEL, "fallbacks": []}),
            lambda doc: doc["agents"]["defaults"].update(heartbeat={"model": control.ON_MODEL}),
            lambda doc: doc["channels"].update(modelByChannel={"synthetic": control.ON_MODEL}),
            lambda doc: doc.update(auth={"profiles": {"synthetic": {"provider": control.PROVIDER, "mode": "api_key"}}}),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                doc = copy.deepcopy(self.original)
                mutate(doc)
                self.write(doc)
                self.assertFalse(self.controller.set_mode("on")["ok"])
                result = self.controller.set_mode("off")
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["uncontrolled_routes"])
                self.assertTrue(self.read()["models"]["providers"][control.PROVIDER]["apiKey"].startswith(control.MARKER_PREFIX))

    def test_existing_unowned_provider_is_not_modified(self):
        doc = copy.deepcopy(self.original)
        doc["models"]["providers"][control.PROVIDER] = {"headers": {"Authorization": "SYNTHETIC"}, "models": [{"id": "gpt-6-luna", "name": "old"}]}
        self.write(doc)
        self.key()
        before = self.config.read_bytes()
        for mode in ("on", "off"):
            result = self.controller.set_mode(mode)
            self.assertEqual(result["error"], "proxy_provider_already_owned_elsewhere")
            self.assertEqual(self.config.read_bytes(), before)

    def test_headers_cannot_bypass_an_already_owned_provider(self):
        self.controller.set_mode("off")
        doc = self.read()
        provider = doc["models"]["providers"][control.PROVIDER]
        provider["headers"] = {"Authorization": "SYNTHETIC"}
        provider["models"][0]["headers"] = {"Authorization": "SYNTHETIC"}
        self.write(doc)
        self.assertFalse(self.controller.route_for_request()["ok"])
        self.assertTrue(self.controller.set_mode("off")["ok"])
        provider = self.read()["models"]["providers"][control.PROVIDER]
        self.assertNotIn("headers", provider)
        self.assertNotIn("headers", provider["models"][0])

    def test_provider_request_auth_cannot_bypass_disabled_key(self):
        self.controller.set_mode("off")
        doc = self.read()
        doc["models"]["providers"][control.PROVIDER]["request"] = {"auth": {"mode": "authorization-bearer", "token": "SYNTHETIC"}, "headers": {"Authorization": "SYNTHETIC"}}
        self.write(doc)
        self.assertFalse(self.controller.route_for_request()["ok"])
        self.assertTrue(self.controller.set_mode("off")["ok"])
        self.assertNotIn("request", self.read()["models"]["providers"][control.PROVIDER])

    def test_missing_policy_or_changed_policy_path_stays_fail_closed(self):
        self.controller.set_mode("off")
        self.policy.unlink()
        self.assertTrue(self.controller.route_for_request()["installed"])
        self.assertFalse(self.controller.route_for_request()["ok"])
        redirected = control.TrafficController(self.config, self.root / "other.json", validator=lambda cfg: True)
        self.assertFalse(redirected.route_for_request()["ok"])
        self.assertTrue(redirected.route_for_request()["installed"])

    def test_policy_and_anchor_loss_still_detects_owned_provider_trace(self):
        self.controller.set_mode("off")
        self.policy.unlink()
        self.controller.marker_path.unlink()
        self.assertFalse(self.controller.route_for_request()["ok"])
        self.assertTrue(self.controller.route_for_request()["installed"])

    def test_empty_or_absent_allowlist_stays_unrestricted(self):
        self.key()
        for present in (False, True):
            with self.subTest(present=present):
                doc = copy.deepcopy(self.original)
                if present:
                    doc["agents"]["defaults"]["models"] = {}
                else:
                    doc["agents"]["defaults"].pop("models")
                self.write(doc)
                self.assertTrue(self.controller.set_mode("on")["ok"])
                self.assertEqual(self.read()["agents"]["defaults"].get("models"), {} if present else None)
                self.assertTrue(self.controller.set_mode("off")["ok"])
                self.assertEqual(self.read()["agents"]["defaults"].get("models"), {} if present else None)

    def test_nonproxy_subagent_or_heartbeat_override_blocks_on(self):
        self.key()
        for area, kind in (("defaults", "subagents"), ("defaults", "heartbeat"), ("main", "subagents"), ("main", "heartbeat")):
            doc = copy.deepcopy(self.original)
            target = doc["agents"]["defaults"] if area == "defaults" else doc["agents"]["list"][0]
            target[kind] = {"model": control.OFF_MODEL}
            self.write(doc)
            result = self.controller.set_mode("on")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "uncontrolled_proxy_route_or_auth")

    def test_off_model_must_be_configured_and_allowed(self):
        for missing in ("provider", "definition", "allowlist"):
            doc = copy.deepcopy(self.original)
            if missing == "provider":
                doc["models"]["providers"] = {}
            elif missing == "definition":
                doc["models"]["providers"]["deepseek-official"]["models"] = []
            else:
                doc["agents"]["defaults"]["models"] = {"other/model": {}}
            self.write(doc)
            self.assertFalse(self.controller.set_mode("off")["ok"])
            self.assertFalse(self.policy.exists())

    def test_removed_off_provider_blocks_later_route_snapshot(self):
        self.controller.set_mode("off")
        doc = self.read()
        doc["models"]["providers"].pop("deepseek-official")
        self.write(doc)
        result = self.controller.route_for_request()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "off_model_provider_unavailable")
        self.assertIsNone(result["selected_model"])

    def test_error_after_atomic_replace_reports_observed_config_write(self):
        write = control._atomic_write
        def post_commit_error(path, *args, **kwargs):
            write(path, *args, **kwargs)
            if path == self.config:
                raise OSError("synthetic post-replace directory-fsync failure")
        with mock.patch.object(control, "_atomic_write", side_effect=post_commit_error):
            result = self.controller.set_mode("off")
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(self.controller.route_for_request()["selected_model"], control.OFF_MODEL)

    def test_model_aliases_cannot_redirect_controlled_full_or_provider_names(self):
        cases = [("other/target", control.OFF_MODEL.upper()), ("other/target", control.ON_MODEL), ("cliproxy/another-model", "gpt-6-luna"), ("deepseek-official/another-model", "deepseek-v4-flash")]
        for target, alias in cases:
            with self.subTest(target=target, alias=alias):
                doc = copy.deepcopy(self.original)
                doc["agents"]["defaults"]["models"][target] = {"alias": alias}
                self.write(doc)
                self.assertEqual(self.controller.set_mode("off")["error"], "controlled_model_alias_redirection")
                self.assertFalse(self.policy.exists())
        self.write(self.original)
        self.controller.set_mode("off")
        doc = self.read()
        doc["agents"]["defaults"]["models"]["other/target"] = {"alias": control.OFF_MODEL}
        self.write(doc)
        self.assertFalse(self.controller.route_for_request()["ok"])

    def test_main_must_match_cli_default_agent(self):
        doc = copy.deepcopy(self.original)
        doc["agents"]["list"][1]["default"] = True
        self.write(doc)
        self.assertEqual(self.controller.set_mode("off")["error"], "controlled_main_is_not_default_agent")

    def test_activation_failure_does_not_claim_success(self):
        controller = control.TrafficController(self.config, self.policy, validator=lambda cfg: True, preflight=lambda *args: {"ok": True}, activation_verifier=lambda *args: {"ok": False, "error": "runtime_not_updated"})
        result = controller.set_mode("off")
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertFalse(result["activation_verified"])
        self.assertEqual(controller.route_for_request()["selected_model"], control.OFF_MODEL)
        self.assertEqual(controller.route_for_request()["config_path"], str(self.config.absolute()))
        self.assertFalse(controller.get_status()["ok"])

    def test_activation_exception_reports_config_already_written(self):
        controller = control.TrafficController(self.config, self.policy, validator=lambda cfg: True, activation_verifier=mock.Mock(side_effect=RuntimeError("synthetic")))
        result = controller.set_mode("off")
        self.assertFalse(result["ok"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["error"], "activation_check_failed")

    def test_validator_exception_is_safe_and_has_no_partial_config_write(self):
        controller = control.TrafficController(self.config, self.policy, validator=mock.Mock(side_effect=RuntimeError("SYNTHETIC_ERROR_NOT_EXPOSED")))
        result = controller.set_mode("off")
        self.assertFalse(result["ok"])
        self.assertFalse(result["configured"])
        self.assertNotIn("SYNTHETIC_ERROR", json.dumps(result))
        self.assertFalse(self.policy.exists())

    def test_status_rechecks_runtime_guards_and_success_removes_pending_warning(self):
        preflight = mock.Mock(return_value={"ok": True})
        verifier = mock.Mock(return_value={"ok": True})
        controller = control.TrafficController(self.config, self.policy, validator=lambda cfg: True, preflight=preflight, activation_verifier=verifier)
        result = controller.set_mode("off")
        self.assertTrue(result["activation_verified"])
        self.assertNotIn("gateway_activation_and_session_overrides_require_external_verification", result["warnings"])
        # The deployment verifier includes fresh session/auth/cron guards.
        verifier.return_value = {"ok": False, "error": "session_override_conflict"}
        self.assertFalse(controller.get_status()["ok"])
        # Explicit CLI routing uses its pinned config and does not contact Gateway.
        self.assertTrue(controller.route_for_request()["ok"])

    def test_preflight_failure_prevents_all_configuration_writes(self):
        controller = control.TrafficController(self.config, self.policy, validator=lambda cfg: True, preflight=lambda *args: {"ok": False, "error": "session_override_conflict"})
        before = self.config.read_bytes()
        self.assertEqual(controller.set_mode("off")["error"], "session_override_conflict")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.policy.exists())
        self.assertFalse(controller.marker_path.exists())

    def test_validator_rejection_does_not_write_config_or_policy(self):
        controller = control.TrafficController(self.config, self.policy, validator=lambda cfg: False)
        before = self.config.read_bytes()
        result = controller.set_mode("off")
        self.assertEqual(result["error"], "config_schema_validation_failed")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.policy.exists())

    def test_foreign_config_modification_is_detected_not_overwritten(self):
        write = control._atomic_write
        def interfere(path, *args, **kwargs):
            if path == self.config:
                doc = self.read()
                doc["foreign_editor_marker"] = "preserve-this"
                self.write(doc)
            return write(path, *args, **kwargs)
        with mock.patch.object(control, "_atomic_write", side_effect=interfere):
            result = self.controller.set_mode("off")
        self.assertEqual(result["error"], "concurrent_configuration_change")
        self.assertEqual(self.read()["foreign_editor_marker"], "preserve-this")
        self.assertNotIn(control.PROVIDER, self.read()["models"]["providers"])
        self.assertFalse(self.controller.route_for_request()["ok"])

    def test_concurrent_switches_serialize_and_restart_consistently(self):
        self.key()
        jobs = [(str(self.config), str(self.policy), "on" if index % 2 else "off") for index in range(12)]
        with multiprocessing.get_context("spawn").Pool(4) as pool:
            results = pool.map(toggle_worker, jobs)
        self.assertTrue(all(result["ok"] for result in results), results)
        status = control.TrafficController(self.config, self.policy, validator=lambda cfg: True).get_status()
        self.assertTrue(status["ok"], status)
        self.assertIn(status["mode"], {"on", "off"})
        self.assert_preserved()
        self.assertEqual(stat_mode(self.policy), 0o600)


def stat_mode(path):
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
