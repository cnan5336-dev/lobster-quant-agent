import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

const packageJson = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
const packed = spawnSync("npm", ["pack", "--dry-run", "--json"], {
  cwd: new URL("..", import.meta.url),
  encoding: "utf8",
});

if (packed.status !== 0) {
  process.stderr.write(packed.stderr || packed.stdout || "npm pack --dry-run failed\n");
  process.exit(packed.status ?? 1);
}

let report;
try {
  [report] = JSON.parse(packed.stdout);
} catch (error) {
  process.stderr.write(`Could not parse npm pack report: ${error.message}\n`);
  process.exit(1);
}

const files = new Set((report.files ?? []).map((item) => item.path));
const required = [
  "package.json",
  "openclaw.plugin.json",
  "dist/index.js",
  "python/lobster_quant_agent/cli.py",
  "python/lobster_quant_agent/model_traffic_control.py",
  "python/lobster_quant_agent/model_traffic_adapter.py",
  "scripts/prepare-cliproxy-key.py",
  "docs/model-traffic-switch.md",
  "scripts/install.sh",
  "scripts/check-versions.mjs",
  "scripts/run-openclaw-isolated.mjs",
  "scripts/test_clean_install.sh",
  "scripts/privacy_audit.py",
  "README.md",
  "CHANGELOG.md",
  "COMPATIBILITY.md",
  "MAINTAINERS.md",
  "docs/demo/README.md",
  "docs/demo/synthetic-market-data.json",
  "docs/demo/synthetic-session.svg",
  "LICENSE",
];
const forbidden = [
  /^\.git(?:\/|$)/,
  /^node_modules\//,
  /(?:^|\/)__pycache__\//,
  /\.py[co]$/,
  /^python\/test_.*\.py$/,
  /^reports\//,
  /^results\//,
  /^backtests\//,
  /^screenshots\/private\//,
  /(?:^|\/)(?:\.env|credentials|secrets)(?:[./]|$)/i,
  /^config\/.*(?:local|private|secret|token)/i,
];

const missing = required.filter((path) => !files.has(path));
const leaked = [...files].filter((path) => forbidden.some((pattern) => pattern.test(path)));
const errors = [];

if (report.name !== packageJson.name || report.version !== packageJson.version) {
  errors.push(
    `package identity mismatch: expected ${packageJson.name}@${packageJson.version}, got ${report.name}@${report.version}`,
  );
}
if (missing.length > 0) errors.push(`required files missing: ${missing.join(", ")}`);
if (leaked.length > 0) errors.push(`private/runtime paths included: ${leaked.join(", ")}`);
if ((report.unpackedSize ?? 0) > 5 * 1024 * 1024) {
  errors.push(`unpacked package exceeds 5 MiB: ${report.unpackedSize} bytes`);
}

if (errors.length > 0) {
  process.stderr.write(`Package check FAILED:\n- ${errors.join("\n- ")}\n`);
  process.exit(1);
}

console.log(
  `Package check passed: ${report.name}@${report.version}, ${files.size} files, ${report.size} packed bytes, ${report.unpackedSize} unpacked bytes.`,
);
