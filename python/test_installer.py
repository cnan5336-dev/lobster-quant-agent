"""Installer regression checks with fake tools and invented configuration only."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PREFIX = "plugins.entries.lobster-quant-agent.config."
STUB = r'''
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["INSTALLER_TEST_ROOT"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "commands.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps([name, *args]) + "\n")

if name in {"python3", "python"}:
    if args[:2] == ["-m", "venv"]:
        target = root / "checkout" / ".venv" / "bin" / "python"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(Path(sys.argv[0]).read_text(), encoding="utf-8")
        target.chmod(0o755)
    elif args and args[0] in {"-", "-c"}:
        os.execv(os.environ["INSTALLER_TEST_PYTHON"],
                 [os.environ["INSTALLER_TEST_PYTHON"], *args])
    elif not (args[:2] == ["-m", "pip"] or args[:2] == ["-m", "unittest"]
              or args == ["scripts/privacy_audit.py", "."]):
        raise SystemExit("Unexpected Python invocation")
elif name == "openclaw":
    state_path = root / "synthetic-state.json"
    state = json.loads(state_path.read_text())
    if args[:2] == ["plugins", "inspect"]:
        if "installed_root" not in state:
            raise SystemExit(1)
        print(json.dumps({"plugin": {"rootDir": state["installed_root"]}}))
    elif args[:3] == ["plugins", "install", "-l"]:
        state["installed_root"] = args[3]
    elif args[:2] == ["config", "set"]:
        changes = json.loads(args[args.index("--batch-json") + 1])
        if "--dry-run" not in args:
            for change in changes:
                state.setdefault("config", {})[change["path"]] = change["value"]
    elif args[:2] not in (["plugins", "enable"], ["config", "validate"]):
        raise SystemExit("Unexpected OpenClaw invocation")
    state_path.write_text(json.dumps(state), encoding="utf-8")
elif name not in {"node", "npm"}:
    raise SystemExit("Unexpected tool")
'''


class InstallerPreservationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="lobster-installer-test-")
        self.root = Path(self.tempdir.name).resolve()
        self.checkout = self.root / "checkout"
        (self.checkout / "scripts").mkdir(parents=True)
        (self.checkout / "config").mkdir()
        shutil.copy2(REPO_ROOT / "scripts/install.sh", self.checkout / "scripts/install.sh")
        (self.checkout / "config/lobster-quant-agent.example.json").write_text("{}\n")
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        for name in ("python3", "node", "npm", "openclaw"):
            executable = self.bin_dir / name
            executable.write_text(f"#!{sys.executable}\n" + STUB, encoding="utf-8")
            executable.chmod(0o755)
        self.state_path = self.root / "synthetic-state.json"
        self.write_state({"config": {}})
        self.env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "TMPDIR": str(self.root),
            "INSTALLER_TEST_ROOT": str(self.root),
            "INSTALLER_TEST_PYTHON": sys.executable,
            "LOBSTER_QUANT_STATE_DIR": str(self.root / "synthetic-private-state"),
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def read_state(self):
        return json.loads(self.state_path.read_text())

    def install(self):
        return subprocess.run(
            ["/bin/bash", "scripts/install.sh"], cwd=self.checkout,
            env=self.env, capture_output=True, text=True, timeout=30,
        )

    def commands(self):
        return [json.loads(line) for line in (self.root / "commands.jsonl").read_text().splitlines()]

    def test_new_install_writes_safe_defaults(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.read_state()
        self.assertEqual(state["installed_root"], str(self.checkout))
        self.assertEqual(state["config"], {
            PREFIX + "pythonExecutable": str(self.checkout / ".venv/bin/python"),
            PREFIX + "stateDirectory": self.env["LOBSTER_QUANT_STATE_DIR"],
            PREFIX + "defaultChannel": "telegram",
            PREFIX + "notifyChannels": [],
            PREFIX + "notificationTargets": {},
        })

    def test_rerun_preserves_all_existing_user_configuration_without_reading_it(self):
        self.assertEqual(self.install().returncode, 0)
        custom = {
            PREFIX + "pythonExecutable": "synthetic-custom-python",
            PREFIX + "stateDirectory": "synthetic-custom-state",
            PREFIX + "defaultChannel": "weixin",
            PREFIX + "notifyChannels": ["weixin"],
            PREFIX + "notificationTargets": {"weixin": {"target": "SYNTHETIC_TEST_TARGET"}},
            PREFIX + "primaryModel": "synthetic-provider/model",
            "synthetic.unrelated.setting": "keep",
        }
        self.write_state({"installed_root": str(self.checkout), "config": custom})
        (self.root / "commands.jsonl").write_text("")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_state()["config"], custom)
        commands = self.commands()
        self.assertFalse(any(command[1:3] in (["config", "get"], ["config", "set"])
                             for command in commands))
        self.assertNotIn("SYNTHETIC_TEST_TARGET", result.stdout + result.stderr)
        self.assertNotIn("SYNTHETIC_TEST_TARGET", json.dumps(commands))

    def test_existing_install_missing_optional_fields_keeps_runtime_defaults(self):
        self.write_state({"installed_root": str(self.checkout), "config": {}})
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_state()["config"], {})
        self.assertFalse(any(command[1:3] == ["config", "set"] for command in self.commands()))

    def test_different_or_unknown_existing_root_is_not_modified(self):
        for installed_root in ("synthetic-other-checkout", ""):
            with self.subTest(installed_root=installed_root):
                original = {"installed_root": installed_root, "config": {"synthetic": "keep"}}
                self.write_state(original)
                (self.root / "commands.jsonl").write_text("")
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.read_state(), original)
                self.assertFalse(any(command[1:3] in (["config", "set"], ["plugins", "enable"])
                                     for command in self.commands()))


if __name__ == "__main__":
    unittest.main()
