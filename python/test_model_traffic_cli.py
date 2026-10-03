"""Only synthetic controllers and subprocess mocks; no secrets or model requests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import cli as stock


ON = "cliproxy/gpt-6-luna"
OFF = "deepseek-official/deepseek-v4-flash"


class ModelTrafficCliTests(unittest.TestCase):
    def setUp(self):
        self.controller = SimpleNamespace(
            route_for_request=mock.Mock(return_value=self.route("off")),
            get_status=mock.Mock(return_value=self.route("off")),
            set_mode=mock.Mock(side_effect=lambda mode: self.route(mode)),
        )
        for target, value in (
            ("_load_model_traffic_controller", mock.Mock(return_value=self.controller)),
            ("MODEL_PRIMARY", "legacy/primary"),
            ("MODEL_FALLBACK_CANDIDATES", ["legacy/fallback", ON]),
            ("_write_model_fallback_log", mock.Mock()),
            ("_model_subprocess_options", mock.Mock(return_value={})),
        ):
            patch = mock.patch.object(stock, target, value)
            patch.start()
            self.addCleanup(patch.stop)
        for target in ("socket.socket", "cli.subprocess.run", "cli.load_watchlist_config"):
            patch = mock.patch(target, side_effect=AssertionError("Unexpected external IO"))
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def route(mode):
        return {"installed": True, "ok": True, "mode": mode,
                "selected_model": ON if mode == "on" else OFF, "config_path": "/synthetic/openclaw.json"}

    @staticmethod
    def metadata():
        return {"error_type": None, "default": "legacy/default",
                "keys": {ON, OFF, "legacy/primary", "legacy/fallback"}}

    def invoke(self, argv):
        output = io.StringIO()
        with mock.patch.object(stock.sys, "argv", ["stock", *argv]), contextlib.redirect_stdout(output):
            stock.main()
        return json.loads(output.getvalue())

    def test_missing_optional_module_and_uninstalled_policy_keep_legacy(self):
        for controller in (None, SimpleNamespace(route_for_request=lambda: {
            "installed": False, "ok": True, "mode": "off", "selected_model": None,
        })):
            with self.subTest(controller=controller), mock.patch.object(stock, "_load_model_traffic_controller", return_value=controller):
                self.assertEqual(stock._model_candidates(self.metadata()), ["legacy/primary", "legacy/fallback", ON])
                with mock.patch.object(stock, "_configured_model_metadata", return_value=self.metadata()), \
                     mock.patch.object(stock, "_run_model_once", side_effect=[{"ok": False, "error_type": "rate_limit"}, {"ok": True, "text": "legacy"}]) as run:
                    result = stock.model_call_with_fallback("public synthetic prompt")
                self.assertEqual(result["model"], "legacy/fallback")
                self.assertEqual(run.call_count, 2)

    def test_installed_on_and_off_ignore_legacy_model_environment(self):
        for mode, target in (("on", ON), ("off", OFF)):
            with self.subTest(mode=mode):
                self.controller.route_for_request.return_value = self.route(mode)
                self.assertEqual(stock._model_candidates(self.metadata()), [target])
                with mock.patch.object(stock, "_configured_model_metadata", return_value=self.metadata()), \
                     mock.patch.object(stock, "_run_model_once", return_value={"ok": True, "text": "synthetic"}) as run:
                    result = stock.model_call_with_fallback("public synthetic prompt")
                self.assertEqual(result["model"], target)
                self.assertFalse(result["fallback_used"])
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], target)

    def test_request_snapshot_is_taken_once_and_next_request_sees_change(self):
        state = {"mode": "on"}
        self.controller.route_for_request.side_effect = lambda: self.route(state["mode"])
        def discover(timeout, **kwargs):
            state["mode"] = "off"  # Simulates a switch after this request has started.
            return self.metadata()
        with mock.patch.object(stock, "_configured_model_metadata", side_effect=discover), \
             mock.patch.object(stock, "_run_model_once", return_value={"ok": True, "text": "synthetic"}) as run:
            first = stock.model_call_with_fallback("request one")
            second = stock.model_call_with_fallback("request two")
        self.assertEqual([first["model"], second["model"]], [ON, OFF])
        self.assertEqual(self.controller.route_for_request.call_count, 2)
        self.assertEqual([call.args[0] for call in run.call_args_list], [ON, OFF])
        self.controller.set_mode.assert_not_called()

    def test_installed_model_failure_never_uses_another_model(self):
        for operation in (stock.model_call_with_fallback, lambda prompt: stock.model_ping()):
            with self.subTest(operation=operation), \
                 mock.patch.object(stock, "_configured_model_metadata", return_value=self.metadata()), \
                 mock.patch.object(stock, "_run_model_once", return_value={"ok": False, "error_type": "rate_limit"}) as run:
                result = operation("synthetic")
                self.assertFalse(result["ok"])
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], OFF)

    def test_missing_selected_model_does_not_try_available_fallback(self):
        metadata = self.metadata()
        metadata["keys"].remove(OFF)
        with mock.patch.object(stock, "_configured_model_metadata", return_value=metadata), \
             mock.patch.object(stock, "_run_model_once") as run:
            result = stock.model_call_with_fallback("synthetic")
        self.assertFalse(result["ok"])
        self.assertEqual([item["model"] for item in result["attempts"]], [OFF])
        run.assert_not_called()

    def test_broken_installed_controller_fails_closed_before_discovery(self):
        failures = (
            {"installed": True, "ok": False, "mode": "blocked", "selected_model": None, "error": "invalid_policy"},
            {"installed": True, "ok": True, "mode": "off", "selected_model": "invalid route"},
            {"ok": True, "selected_model": OFF}, None,
        )
        for result in failures:
            with self.subTest(result=result), mock.patch.object(stock, "_configured_model_metadata") as discovery, \
                 mock.patch.object(stock, "_run_model_once") as run:
                self.controller.route_for_request.return_value = result
                self.assertEqual(stock.model_call_with_fallback("synthetic")["error_type"], "traffic_policy_blocked")
                self.assertEqual(stock.model_ping()["error_type"], "traffic_policy_blocked")
                self.assertEqual(stock._model_candidates(self.metadata()), [])
                discovery.assert_not_called()
                run.assert_not_called()
        self.controller.route_for_request.side_effect = RuntimeError("private diagnostics must not escape")
        result = stock.model_call_with_fallback("synthetic")
        self.assertNotIn("private diagnostics", json.dumps(result))

    def test_conflicting_controlled_environment_blocks_before_discovery(self):
        with mock.patch.object(stock, "_model_subprocess_options", side_effect=ValueError("private env must not escape")), \
             mock.patch.object(stock, "_configured_model_metadata") as discover:
            result = stock.model_call_with_fallback("synthetic")
        self.assertEqual(result["error_type"], "traffic_policy_blocked")
        self.assertEqual(result["routing"]["error"], "controlled_environment_unavailable")
        self.assertNotIn("private env", json.dumps(result))
        discover.assert_not_called()

    def test_controlled_response_requires_matching_resolved_provider_and_model(self):
        for mode in ("on", "off"):
            route = self.route(mode)
            target = route["selected_model"]
            provider, model_id = target.split("/", 1)
            good = {"ok": True, "capability": "model.run", "transport": "local", "provider": provider,
                    "model": model_id, "outputs": [{"text": "synthetic response"}]}
            with self.subTest(mode=mode), mock.patch.object(stock.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(good), stderr="")):
                result = stock._run_model_once(target, "synthetic", route=route)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["routing_verified"])
            self.assertEqual(result["resolved_model"], target)
            for changes, code in (({"provider": "other"}, "model_routing_mismatch"),
                                  ({"model": "other"}, "model_routing_mismatch"),
                                  ({"model": target}, "model_routing_mismatch"),
                                  ({"provider": None}, "model_routing_unverified"),
                                  ({"model": None}, "model_routing_unverified"),
                                  ({"model": ""}, "model_routing_unverified")):
                with self.subTest(mode=mode, changes=changes), mock.patch.object(stock.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({**good, **changes}), stderr="")):
                    result = stock._run_model_once(target, "synthetic", route=route)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_type"], code)
                self.assertNotIn("text", result)
                self.assertNotIn("synthetic response", json.dumps(result))

    def test_routing_evidence_failure_is_terminal_without_fallback(self):
        for missing in (False, True):
            payload = {"ok": True, "provider": "other", "model": "other", "outputs": [{"text": "untrusted result"}]}
            if missing:
                payload.pop("model")
            with self.subTest(missing=missing), mock.patch.object(stock, "_configured_model_metadata", return_value=self.metadata()), \
                 mock.patch.object(stock.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")) as run:
                result = stock.model_call_with_fallback("synthetic")
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["attempts"]), 1)
            self.assertEqual(result["attempts"][0]["error_type"], "model_routing_unverified" if missing else "model_routing_mismatch")
            self.assertNotIn("untrusted result", json.dumps(result))
            run.assert_called_once()

    def test_legacy_response_evidence_behavior_is_unchanged(self):
        payload = {"ok": True, "provider": "legacy", "outputs": [{"text": "legacy response"}]}
        with mock.patch.object(stock.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")):
            result = stock._run_model_once("legacy/requested", "synthetic")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "legacy response")
        self.assertNotIn("routing_verified", result)

    def test_controlled_explicit_request_model_cannot_disagree_with_snapshot(self):
        with mock.patch.object(stock.subprocess, "run") as run:
            result = stock._run_model_once(ON, "synthetic", route=self.route("off"))
        self.assertEqual(result["error_type"], "traffic_policy_blocked")
        run.assert_not_called()

    def test_default_model_command_and_status_are_read_only(self):
        for args in (("model",), ("model", "status"), ("model", "codex", "status")):
            with self.subTest(args=args):
                result = self.invoke(args)
                self.assertTrue(result["ok"])
                self.assertFalse(result["model_request_sent"])
        self.assertEqual(self.controller.get_status.call_count, 3)
        self.controller.route_for_request.assert_not_called()
        self.controller.set_mode.assert_not_called()

    def test_uninstalled_status_does_not_claim_policy_is_effective(self):
        self.controller.get_status.return_value = {
            "installed": False, "ok": True, "mode": "off", "ready": False, "selected_model": None,
        }
        result = self.invoke(("model", "codex", "status"))
        self.assertEqual(result["routing_mode"], "legacy")
        self.assertFalse(result["installed"])
        self.assertFalse(result["model_request_sent"])
        self.controller.route_for_request.assert_not_called()
        self.controller.set_mode.assert_not_called()

    def test_explicit_on_and_off_delegate_to_single_controller(self):
        self.assertEqual(self.invoke(("model", "codex", "ON"))["selected_model"], ON)
        self.assertEqual(self.invoke(("model", "codex", "off"))["selected_model"], OFF)
        self.assertEqual(self.controller.set_mode.call_args_list, [mock.call("on"), mock.call("off")])
        self.controller.route_for_request.assert_not_called()

    def test_exact_natural_language_aliases_normalize_case_and_spaces(self):
        for text, mode in (("开启Codex流量", "on"), ("关闭 codex 流量", "off"), (" C O D E X 流量 状态 ", "status")):
            with self.subTest(text=text):
                result = stock.handle_natural_language_command(text)
                self.assertTrue(result["ok"])
                self.assertFalse(result["model_request_sent"])
        self.assertEqual(self.controller.set_mode.call_args_list, [mock.call("on"), mock.call("off")])
        self.controller.get_status.assert_called_once()

    def test_negated_quoted_or_question_sentences_never_toggle(self):
        for text in ("不要开启Codex流量", "请解释开启Codex流量", "“开启Codex流量”", "开启Codex流量吗", "如果开启Codex流量", "不开启Codex流量"):
            with self.subTest(text=text):
                result = stock.handle_natural_language_command(text)
                self.assertIn("error", result)
        self.controller.set_mode.assert_not_called()
        self.controller.get_status.assert_not_called()

    def test_invalid_control_arguments_are_rejected(self):
        for args in (("model", "codex"), ("model", "codex", "on", "extra"), ("model", "status", "extra"), ("model", "codex", "toggle")):
            with self.subTest(args=args):
                self.assertFalse(self.invoke(args)["ok"])
        self.controller.set_mode.assert_not_called()
        self.controller.get_status.assert_not_called()

    def test_module_absence_is_distinguished_from_broken_dependency(self):
        # Exercise the actual import wrapper while preserving the instance patch.
        original = stock._load_model_traffic_controller
        import importlib.util
        spec = importlib.util.spec_from_file_location("traffic_cli_under_test", stock.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module.importlib, "import_module", side_effect=ModuleNotFoundError(name="model_traffic_control")):
            self.assertIsNone(module._load_model_traffic_controller())
        with mock.patch.object(module.importlib, "import_module", side_effect=ModuleNotFoundError(name="controller_dependency")):
            self.assertFalse(module._model_route_snapshot()["ok"])
        self.assertIs(stock._load_model_traffic_controller, original)


if __name__ == "__main__":
    unittest.main()
