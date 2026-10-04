"""Optional controller contract exercised against temporary, synthetic config only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "lobster_quant_agent"))
import json
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import cli as stock

try:
    import model_traffic_control as controller
    import model_traffic_adapter as adapter
except ModuleNotFoundError as exc:
    if exc.name not in {"model_traffic_control", "model_traffic_adapter"}:
        raise
    controller = None
    adapter = None


@unittest.skipIf(controller is None, "Optional traffic controller is not installed")
class TrafficControllerCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "openclaw.json"
        self.policy = self.root / "policy.json"
        self.config.write_text(json.dumps({
            "agents": {"defaults": {"model": {"primary": controller.OFF_MODEL, "fallbacks": []}}, "list": [{"id": "main"}]},
            "models": {"providers": {"deepseek-official": {"models": [{"id": "deepseek-v4-flash"}]}}},
        }))
        key_dir = self.root / "cliproxy-private"
        key_dir.mkdir(mode=0o700)
        self.key = key_dir / "client-key"
        self.key.write_bytes(b"synthetic-never-used-test-value")
        self.key.chmod(0o600)
        original_read = Path.read_bytes
        def guarded_read(path):
            if path == self.key:
                raise AssertionError("Controller must not read credential contents")
            return original_read(path)
        fake_adapter = SimpleNamespace(
            controlled_subprocess_env=adapter.controlled_subprocess_env,
            validate_config=lambda config: True,
            preflight=lambda *args, **kwargs: {"ok": True},
            verify_activation=lambda *args, **kwargs: {"ok": True},
        )
        patches = [
            mock.patch.dict(sys.modules, {"model_traffic_adapter": fake_adapter}),
            mock.patch.dict(os.environ, {"LOBSTER_TRAFFIC_CONFIG_PATH": str(self.config), "LOBSTER_TRAFFIC_POLICY_PATH": str(self.policy),
                                        "OPENCLAW_STATE_DIR": str(self.root), "OPENCLAW_PROFILE": "", "OPENCLAW_AGENT_DIR": "",
                                        "PI_CODING_AGENT_DIR": "", "OPENCLAW_CONTAINER": "", "OPENCLAW_CONTAINER_HINT": ""}),
            mock.patch.object(stock, "_load_model_traffic_controller", return_value=controller),
            mock.patch.object(stock, "MODEL_PRIMARY", "legacy/override"),
            mock.patch.object(stock, "MODEL_FALLBACK_CANDIDATES", ["legacy/backup"]),
            mock.patch.object(stock, "_write_model_fallback_log"),
            mock.patch.object(Path, "read_bytes", guarded_read),
            mock.patch("socket.socket", side_effect=AssertionError("Unexpected network")),
            mock.patch.object(stock.subprocess, "run", side_effect=AssertionError("Unexpected subprocess")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def metadata(self):
        return {"error_type": None, "default": "legacy/default", "keys": {controller.ON_MODEL, controller.OFF_MODEL}}

    def test_shared_controller_install_off_on_and_snapshot_failure(self):
        self.assertFalse(stock.model_codex_control("status")["installed"])
        self.assertEqual(stock._model_candidates(self.metadata()), ["legacy/override", "legacy/backup"])
        switched = stock.model_codex_control("off")
        self.assertTrue(switched["ok"], switched)
        self.assertEqual(stock._model_candidates(self.metadata()), [controller.OFF_MODEL])
        switched = stock.model_codex_control("on")
        self.assertTrue(switched["ok"], switched)
        self.assertEqual(stock.model_codex_control("status")["selected_model"], controller.ON_MODEL)
        def discover(timeout, **kwargs):
            switched = stock.model_codex_control("off")
            self.assertTrue(switched["ok"], switched)
            return self.metadata()
        with mock.patch.object(stock, "_configured_model_metadata", side_effect=discover), \
             mock.patch.object(stock, "_run_model_once", return_value={"ok": False, "error_type": "forbidden"}) as run:
            result = stock.model_call_with_fallback("public synthetic prompt")
        self.assertFalse(result["ok"])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], controller.ON_MODEL)
        self.assertEqual(stock._model_candidates(self.metadata()), [controller.OFF_MODEL])

    def test_discovery_and_inference_share_pinned_config_and_state(self):
        self.assertTrue(stock.model_codex_control("off")["ok"])
        route = stock._model_route_snapshot()
        self.assertTrue(route["ok"], route)
        metadata = {"models": [{"key": controller.OFF_MODEL, "available": True, "tags": ["default"]}]}
        outputs = [SimpleNamespace(returncode=0, stdout=json.dumps(metadata), stderr=""),
                   SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "provider": "deepseek-official", "model": "deepseek-v4-flash", "outputs": [{"text": "ok"}]}), stderr="")]
        with mock.patch.dict(os.environ, {"OPENCLAW_CONFIG_PATH": "/synthetic/old-profile.json"}), \
             mock.patch.object(stock.subprocess, "run", side_effect=outputs) as run:
            result = stock.model_call_with_fallback("public synthetic prompt")
            self.assertEqual(os.environ["OPENCLAW_CONFIG_PATH"], "/synthetic/old-profile.json")
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["OPENCLAW_CONFIG_PATH"], str(self.config))
            self.assertEqual(call.kwargs["env"]["OPENCLAW_STATE_DIR"], str(self.root))
        for name in ("OPENCLAW_CONTAINER", "OPENCLAW_STATE_DIR", "OPENCLAW_PROFILE"):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "conflicting-target"}), \
                 mock.patch.object(stock, "_configured_model_metadata") as discover:
                self.assertEqual(stock.model_call_with_fallback("synthetic")["error_type"], "traffic_policy_blocked")
                discover.assert_not_called()

    def test_corrupt_installed_policy_blocks_cli_until_explicit_off_repair(self):
        self.assertTrue(stock.model_codex_control("off")["ok"])
        self.policy.write_text("{broken")
        with mock.patch.object(stock, "_configured_model_metadata") as discover, \
             mock.patch.object(stock, "_run_model_once") as run:
            self.assertEqual(stock.model_call_with_fallback("synthetic")["error_type"], "traffic_policy_blocked")
            self.assertEqual(stock.model_ping()["error_type"], "traffic_policy_blocked")
            self.assertFalse(stock.model_codex_control("status")["ok"])
            self.assertFalse(stock.model_codex_control("on")["ok"])
            discover.assert_not_called()
            run.assert_not_called()
        self.assertTrue(stock.model_codex_control("off")["ok"])
        self.assertEqual(stock._model_candidates(self.metadata()), [controller.OFF_MODEL])


if __name__ == "__main__":
    unittest.main()
