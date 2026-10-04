"""Offline privacy and package-boundary regressions with invented inputs only."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("privacy_audit_under_test", REPO / "scripts/privacy_audit.py")
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)
RUNTIME_PATHS = (
    ".monitor-state-synthetic.tmp",
    "market_monitor_state.json.initialized", "market_monitor_state.json.tmp.synthetic",
    "market_monitor_state.json.delivery.json", "market_monitor_state.json.delivery.json.lock",
    "market_monitor_state.json.delivery.json.initialized", "market_monitor_state.json.delivery.json.tmp.synthetic",
    "custom_state.json.delivery.json", "custom_state.json.delivery.json.initialized",
    "custom_state.json.delivery.json.tmp.synthetic", "custom_state.json.initialized",
    "custom_state.json.initialized.tmp.synthetic",
    "copied_state.json.delivery.json/item.txt", "copied_state.json.initialized/item.txt",
)
PRIVATE_PATHS = (
    "client-key", "client-key.backup", ".client-key.tmp-fixture", "client_key",
    "cliproxy-private/value", "openclaw.json", "openclaw.json5",
    "auth.json", "auth-profiles.json", "auth_profiles.json", "hosts.yml",
    "sessions.json", "request-history.jsonl", "history.sqlite", "proxy-client.json",
    "model_traffic_policy.json", "cliproxy-switch-policy.json", ".cliproxy-switch-installed.json",
    "market_watchlist.json", "market_monitor_state.json", "backtest_config.json", "openclaw-workspace-state.json",
    "request-logs/item.txt", "runtime-state/item.txt", "session-state/item.txt", "auth-state/item.txt",
) + RUNTIME_PATHS


class PrivacyAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-privacy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        self.root.mkdir()
        self.value = "SYNTHETIC" + "-fixture-42"

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def assert_private(self, findings, path):
        self.assertTrue(any(path in finding for finding in findings), findings)
        self.assertNotIn(self.value, "\n".join(findings))

    def test_extensionless_credentials_and_runtime_aliases_are_forbidden(self):
        for name in PRIVATE_PATHS:
            self.write("nested/" + name, "invented content")
        findings = privacy.audit(self.root)
        for name in PRIVATE_PATHS:
            with self.subTest(path=name):
                self.assert_private(findings, "nested/" + name)

    def test_quoted_json_credentials_include_short_nonempty_values(self):
        for key in ("apiKey", "api_key", "api-key", "access_token", "accessToken", "refresh_token", "clientSecret", "password", "token"):
            for value in ("x", "abc", self.value):
                with self.subTest(key=key, length=len(value)):
                    self.write("settings.json", json.dumps({"nested": {key: value}}))
                    findings = privacy.audit(self.root)
                    self.assert_private(findings, "settings.json")
                    self.assertTrue(any("credential assignment" in row for row in findings))

    def test_private_directory_alias_used_as_plain_file_is_rejected(self):
        self.write("cliproxy-private", "invented content")
        self.assert_private(privacy.audit(self.root), "cliproxy-private")

    def test_delivery_artifacts_are_rejected_at_root_and_package_depths(self):
        paths = []
        for folder in ("", "python", "python/lobster_quant_agent", "scripts", "scripts/nested"):
            for name in RUNTIME_PATHS:
                path = str(Path(folder) / name)
                self.write(path, "invented content")
                paths.append(path)
        findings = privacy.audit(self.root)
        for path in paths:
            with self.subTest(path=path):
                self.assert_private(findings, path)

    def test_shell_credentials_quoted_and_unquoted_are_detected(self):
        for key in ("API_KEY", "DEEPSEEK_API_KEY", "ACCESS_TOKEN", "CLIENT_SECRET", "PASSWORD"):
            for value in ("abc", self.value, '"' + self.value + '"', "'" + self.value + "'"):
                with self.subTest(key=key, quoted=value.startswith(('"', "'"))):
                    self.write("setup.sh", "export " + key + "=" + value + "\n")
                    self.assert_private(privacy.audit(self.root), "setup.sh")

    def test_yaml_short_credentials_are_detected(self):
        for key in ("apiKey", "access_token", "password"):
            for value in ("x", "abc", self.value):
                with self.subTest(key=key, length=len(value)):
                    self.write("settings.yaml", "provider:\n  " + key + ": " + value + "\n")
                    self.assert_private(privacy.audit(self.root), "settings.yaml")

    def test_empty_and_secretref_examples_remain_allowed(self):
        self.write("settings.json", json.dumps({"apiKey": {"source": "env", "provider": "default", "id": "SYNTHETIC_ENV"}, "access_token": ""}))
        self.write("settings.yaml", "apiKey: null\n")
        self.write("module.py", "def parse(token: str):\n    token = token.strip()\n    return token\n")
        self.assertEqual(privacy.audit(self.root), [])

    def test_test_files_have_no_credential_exemption(self):
        self.write("python/test_example.py", json.dumps(dict(apiKey=self.value)))
        self.assert_private(privacy.audit(self.root), "python/test_example.py")

    def test_nul_does_not_disable_content_scanning(self):
        token_value = "gh" + "p_" + "Z" * 32
        (self.root / "payload.bin").write_bytes(b"header\x00" + token_value.encode())
        findings = privacy.audit(self.root)
        self.assertTrue(any("GitHub token" in row for row in findings))
        self.assertNotIn(token_value, "\n".join(findings))

    def test_bom_encoded_credentials_are_scanned(self):
        for encoding in ("utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                (self.root / "encoded.bin").write_bytes(json.dumps(dict(apiKey=self.value)).encode(encoding))
                self.assert_private(privacy.audit(self.root), "encoded.bin")

    def test_file_and_directory_symlinks_never_read_the_target(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        target = outside / "value.txt"
        target.write_text(self.value)
        (self.root / "file-link").symlink_to(target)
        (self.root / "directory-link").symlink_to(outside, target_is_directory=True)
        with mock.patch.object(privacy, "read_regular_file", side_effect=AssertionError("must not read linked content")):
            findings = privacy.audit(self.root)
        self.assert_private(findings, "file-link")
        self.assert_private(findings, "directory-link")
        self.assertNotIn(str(outside), "\n".join(findings))

    def test_link_replacement_between_inventory_and_open_is_rejected(self):
        path = self.write("ordinary.txt", "public")
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text(self.value)
        real_open = privacy.os.open
        replaced = False
        def swap_before_open(name, flags, *args, **kwargs):
            nonlocal replaced
            if name == path.name and not replaced:
                replaced = True
                path.unlink()
                path.symlink_to(outside)
            return real_open(name, flags, *args, **kwargs)
        with mock.patch.object(privacy.os, "open", side_effect=swap_before_open):
            findings = privacy.audit(self.root)
        self.assertTrue(replaced)
        self.assert_private(findings, "ordinary.txt")
        self.assertTrue(any("unsafe" in row for row in findings))

    def test_symlinked_audit_root_is_rejected(self):
        link = Path(self.temp.name) / "root-link"
        link.symlink_to(self.root, target_is_directory=True)
        with mock.patch.object(privacy, "read_regular_file", side_effect=AssertionError("must not open")):
            self.assertTrue(privacy.audit(link))

    def test_io_errors_do_not_echo_exception_or_value(self):
        self.write("ordinary.txt", "public")
        with mock.patch.object(privacy, "read_regular_file", side_effect=OSError(self.value)):
            findings = privacy.audit(self.root)
        self.assert_private(findings, "ordinary.txt")

    @unittest.skipUnless(shutil.which("git"), "Git is needed for tracked-file regression")
    def test_force_tracked_file_in_excluded_directory_is_still_scanned(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.write(".gitignore", "node_modules/\n")
        self.write("node_modules/tracked.txt", json.dumps(dict(apiKey=self.value)))
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "node_modules/tracked.txt"], check=True, capture_output=True)
        self.assert_private(privacy.audit(self.root), "node_modules/tracked.txt")

    @unittest.skipUnless(shutil.which("git"), "Git is needed for tracked-file regression")
    def test_force_tracked_delivery_artifact_is_not_hidden_by_ignore_rules(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        shutil.copyfile(REPO / ".gitignore", self.root / ".gitignore")
        name = "python/market_monitor_state.json.delivery.json"
        self.write(name, "invented content")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", name], check=True, capture_output=True)
        self.assert_private(privacy.audit(self.root), name)

    @unittest.skipUnless(shutil.which("git"), "Git is needed for ignore-rule regression")
    def test_git_ignore_covers_new_private_aliases(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        shutil.copyfile(REPO / ".gitignore", self.root / ".gitignore")
        for name in PRIVATE_PATHS:
            path = "nested/" + name
            with self.subTest(path=path):
                result = subprocess.run(["git", "-C", str(self.root), "check-ignore", "--no-index", path], capture_output=True)
                self.assertEqual(result.returncode, 0, path)

    def test_command_output_contains_locations_but_not_values(self):
        self.write("settings.json", json.dumps(dict(apiKey=self.value)))
        result = subprocess.run([sys.executable, str(REPO / "scripts/privacy_audit.py"), str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("settings.json", result.stdout)
        self.assertNotIn(self.value, result.stdout + result.stderr)


@unittest.skipUnless(shutil.which("npm") and shutil.which("node"), "Node and npm are needed for package-boundary regressions")
class PackagePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-package-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "python").mkdir()
        self.package = {"name": "synthetic-privacy-fixture", "version": "0.0.0", "files": ["python", "scripts"]}
        (self.root / "package.json").write_text(json.dumps(self.package))
        for name in (".npmignore", "python/.npmignore", "scripts/.npmignore"):
            shutil.copyfile(REPO / name, self.root / name)
        user_config = self.root / "user.npmrc"
        global_config = self.root / "global.npmrc"
        user_config.write_text("")
        global_config.write_text("")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NPM_CONFIG_USERCONFIG": str(user_config), "NPM_CONFIG_GLOBALCONFIG": str(global_config), "NPM_CONFIG_CACHE": str(self.root / "npm-cache"), "NPM_CONFIG_OFFLINE": "true", "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_UPDATE_NOTIFIER": "false"}

    def test_actual_npm_file_allowlist_cannot_reinclude_nested_private_files(self):
        for folder in ("python", "scripts"):
            (self.root / folder / "public.py").write_text("# public fixture\n")
            (self.root / folder / "cliproxy-private").write_text("invented content")
            for name in PRIVATE_PATHS:
                path = self.root / folder / "nested" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("invented content")
        result = subprocess.run([shutil.which("npm"), "pack", "--dry-run", "--json", "--ignore-scripts", "--offline"], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, "isolated npm pack failed")
        names = {row["path"] for row in json.loads(result.stdout)[0]["files"]}
        self.assertIn("python/public.py", names)
        self.assertIn("scripts/public.py", names)
        for folder in ("python", "scripts"):
            self.assertNotIn(folder + "/cliproxy-private", names)
            for name in PRIVATE_PATHS:
                self.assertNotIn(folder + "/nested/" + name, names)

    def test_actual_npm_excludes_delivery_artifacts_at_package_depths(self):
        paths = []
        for folder in ("", "python", "python/lobster_quant_agent", "scripts", "scripts/nested"):
            for name in RUNTIME_PATHS:
                path = self.root / folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("invented content")
                paths.append(path.relative_to(self.root).as_posix())
        (self.root / "python/public.py").write_text("# public fixture\n")
        result = subprocess.run([shutil.which("npm"), "pack", "--dry-run", "--json", "--ignore-scripts", "--offline"], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, "isolated npm pack failed")
        names = {row["path"] for row in json.loads(result.stdout)[0]["files"]}
        self.assertIn("python/public.py", names)
        self.assertFalse(set(paths) & names, sorted(set(paths) & names))

    def run_checker(self, paths=None, malformed=False):
        shutil.copyfile(REPO / "scripts/check-package.mjs", self.root / "scripts/check-package.mjs")
        binaries = self.root / "bin"
        binaries.mkdir(exist_ok=True)
        fake = binaries / "npm"
        payload = {"name": self.package["name"], "version": "0.0.0", "files": [{"path": path} for path in paths or []], "unpackedSize": 1}
        response = "SYNTHETIC" + "-private-output" if malformed else json.dumps([payload])
        fake.write_text(f"#!{sys.executable}\nimport sys\nif '--ignore-scripts' not in sys.argv or '--offline' not in sys.argv: raise SystemExit(91)\nprint({response!r})\n")
        fake.chmod(0o755)
        environment = dict(self.env, PATH=str(binaries) + os.pathsep + self.env["PATH"])
        return subprocess.run([shutil.which("node"), "scripts/check-package.mjs"], cwd=self.root, env=environment, capture_output=True, text=True, timeout=15)

    def test_package_checker_rejects_private_paths_even_if_packer_includes_them(self):
        paths = ["dist/nested/" + name for name in PRIVATE_PATHS]
        result = self.run_checker(paths)
        self.assertEqual(result.returncode, 1)
        self.assertIn("private/runtime paths included", result.stderr)
        for path in paths:
            self.assertIn(path, result.stderr)

    def test_package_checker_does_not_echo_malformed_packer_output(self):
        result = self.run_checker(malformed=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not parse npm pack report", result.stderr)
        self.assertNotIn("SYNTHETIC", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
