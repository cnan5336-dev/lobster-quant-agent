import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lobster_quant_agent'))
"""Deterministic tests: every CLI/model invocation is mocked."""
import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

import cli as stock


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.logs = mock.patch.object(stock, "_write_model_fallback_log").start()
        mock.patch.object(stock, "MODEL_PRIMARY", "").start()
        mock.patch.object(stock, "MODEL_FALLBACK_CANDIDATES", []).start()
        mock.patch.dict(os.environ, {"LOBSTER_QUANT_MODEL_TIMEOUT_SECONDS": "90"}).start()
        self.addCleanup(mock.patch.stopall)

    def metadata(self, default="provider/current", keys=None):
        return {"keys": set(keys or [default]), "default": default, "error_type": None}

    def test_metadata_uses_available_default_not_list_order(self):
        payload = {"models": [
            {"key": "provider/old", "available": True, "tags": []},
            {"key": "provider/broken", "available": False, "tags": ["default"]},
            {"key": "provider/current", "available": True, "tags": ["configured", "default"]},
        ]}
        with mock.patch.object(stock.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))):
            meta = stock._configured_model_metadata()
        self.assertEqual(meta["default"], "provider/current")
        self.assertNotIn("provider/broken", meta["keys"])

    def test_default_is_inherited_and_no_unconfigured_fallback_is_invented(self):
        with mock.patch.object(stock, "_configured_model_metadata", return_value=self.metadata()), \
             mock.patch.object(stock, "_run_model_once", return_value={"ok": True, "text": "response"}) as run:
            result = stock.model_call_with_fallback("synthetic")
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "provider/current")
        self.assertEqual(run.call_count, 1)

    def test_explicit_model_takes_precedence_and_candidates_are_unique(self):
        with mock.patch.object(stock, "MODEL_PRIMARY", "provider/explicit"), \
             mock.patch.object(stock, "MODEL_FALLBACK_CANDIDATES", ["provider/explicit", "provider/backup"]):
            self.assertEqual(stock._model_candidates(self.metadata()), ["provider/explicit", "provider/backup"])

    def test_outer_plugin_budget_caps_model_budget(self):
        with mock.patch.dict(os.environ, {"LOBSTER_QUANT_MODEL_TIMEOUT_SECONDS": "25"}):
            self.assertEqual(stock._model_budget(90), 25)
        with mock.patch.dict(os.environ, {"LOBSTER_QUANT_MODEL_TIMEOUT_SECONDS": "bad"}):
            self.assertEqual(stock._model_budget(90), 90)

    def test_discovery_failure_is_not_reported_as_missing_configuration(self):
        with mock.patch.object(stock, "_configured_model_metadata", return_value={"keys": set(), "error_type": "discovery_failed"}), \
             mock.patch.object(stock, "_run_model_once") as run:
            result = stock.model_call_with_fallback("synthetic")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "discovery_failed")
        run.assert_not_called()

    def test_budget_covers_discovery_and_all_fallbacks(self):
        clock = [0.0]
        budgets = []
        def discover(timeout):
            clock[0] += 10
            return self.metadata(keys=["provider/current", "provider/backup"])
        def run(model, prompt, timeout):
            budgets.append(timeout)
            clock[0] += 30
            return {"ok": model.endswith("backup"), "text": "ok", "error_type": "rate_limit"}
        with mock.patch.object(stock.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(stock, "MODEL_FALLBACK_CANDIDATES", ["provider/backup"]), \
             mock.patch.object(stock, "_configured_model_metadata", side_effect=discover), \
             mock.patch.object(stock, "_run_model_once", side_effect=run):
            result = stock.model_call_with_fallback("synthetic", timeout_seconds=80)
        self.assertEqual(budgets, [70, 40])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["latency_seconds"], 70)

    def test_exhausted_budget_does_not_start_another_model(self):
        clock = [0.0]
        def run(model, prompt, timeout):
            clock[0] += timeout
            return {"ok": False, "error_type": "timeout"}
        with mock.patch.object(stock.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(stock, "MODEL_FALLBACK_CANDIDATES", ["provider/backup"]), \
             mock.patch.object(stock, "_configured_model_metadata", return_value=self.metadata(keys=["provider/current", "provider/backup"])), \
             mock.patch.object(stock, "_run_model_once", side_effect=run) as called:
            result = stock.model_call_with_fallback("synthetic", timeout_seconds=10)
        self.assertFalse(result["ok"])
        self.assertEqual(called.call_count, 1)
        self.assertEqual(result["latency_seconds"], 10)

    def test_one_shot_has_no_agent_session_tools_or_delivery(self):
        payload = {"ok": True, "provider": "provider", "outputs": [{"text": "response"}]}
        with mock.patch.object(stock.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")) as run:
            result = stock._run_model_once("provider/current", "synthetic", 7)
        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["openclaw", "infer", "model", "run", "--local"])
        self.assertNotIn("--session-id", command)
        self.assertNotIn("--deliver", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_old_cli_returns_actionable_error_without_full_agent_fallback(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="error: unknown command 'infer'")
        with mock.patch.object(stock.subprocess, "run", return_value=result):
            attempt = stock._run_model_once("provider/current", "synthetic")
        self.assertEqual(attempt["error_type"], "unsupported_cli")
        with mock.patch.object(stock, "_configured_model_metadata", return_value=self.metadata()), \
             mock.patch.object(stock, "_run_model_once", return_value=attempt) as run:
            result = stock.model_call_with_fallback("synthetic")
        self.assertIn("infer model run", result["message"])
        self.assertEqual(run.call_count, 1)

    def test_timeout_missing_cli_and_malformed_outputs_fail_cleanly(self):
        cases = [(subprocess.TimeoutExpired("openclaw", 1), "timeout"),
                 (FileNotFoundError(), "cli_unavailable")]
        for exc, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(stock.subprocess, "run", side_effect=exc):
                self.assertEqual(stock._run_model_once("provider/current")["error_type"], expected)
        with mock.patch.object(stock.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{"ok":true,"outputs":null}', stderr="")):
            self.assertEqual(stock._run_model_once("provider/current")["error_type"], "json_parse_error")


if __name__ == "__main__":
    unittest.main()
