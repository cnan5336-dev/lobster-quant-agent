import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

const packageJson = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
const packed = spawnSync("npm", ["pack", "--dry-run", "--json", "--ignore-scripts", "--offline"], {
  cwd: new URL("..", import.meta.url),
  encoding: "utf8",
});

if (packed.status !== 0) {
  process.stderr.write(`npm pack --dry-run failed (exit ${packed.status ?? "unavailable"})\n`);
  process.exit(packed.status ?? 1);
}

let report;
try {
  [report] = JSON.parse(packed.stdout);
} catch {
  process.stderr.write("Could not parse npm pack report\n");
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
  /(?:^|\/)(?:reports|results|backtests|memory|logs|cache|\.openclaw|cliproxy-private|request-logs|runtime-state|session-state|auth-state)(?:\/|$)/i,
  /(?:^|\/)screenshots\/private\//i,
  /(?:^|\/)(?:\.env|credentials|secrets)(?:[./]|$)/i,
  /(?:^|\/)config\/.*(?:local|private|secret|token)/i,
  /(?:^|\/)(?:\.?client[-_]key|proxy[-_]client|auth[-_]profiles?|sessions?|request[-_]history|model[-_]traffic[-_]policy|cliproxy[-_]switch[-_]policy|\.?cliproxy[-_]switch[-_]installed)(?:[._-][^/]*)?(?:\/|$)/i,
  /(?:^|\/)(?:openclaw\.(?:json5?|ya?ml)|auth\.(?:json5?|ya?ml)|hosts\.yml)(?:[._-][^/]*)?$/i,
  /(?:^|\/)(?:market_watchlist|market_monitor_state|backtest_config|openclaw-workspace-state)\.json$/i,
  /(?:^|\/)(?:market_monitor_state\.json|[^/]+\.delivery\.json|[^/]+\.initialized|\.monitor-state-[^/]+\.tmp)(?:[._-][^/]*)?(?:\/|$)/i,
  /\.(?:log|jsonl|har|db|sqlite3?|pid|session|lock|pem|key|p12|pickle)(?:[._-][^/]*)?$/i,
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
