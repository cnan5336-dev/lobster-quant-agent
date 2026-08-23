import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const readJson = (relativePath) => JSON.parse(readFileSync(join(root, relativePath), "utf8"));

const packageJson = readJson("package.json");
const manifest = readJson("openclaw.plugin.json");
const packageLock = readJson("package-lock.json");
const pythonInit = readFileSync(join(root, "python/lobster_quant_agent/__init__.py"), "utf8");
const pythonVersion = pythonInit.match(/^__version__\s*=\s*["']([^"']+)["']$/m)?.[1];
const changelog = readFileSync(join(root, "CHANGELOG.md"), "utf8");
const currentChangelogVersion = changelog.match(/^## \[([^\]]+)\]/m)?.[1];
const releaseNotesPath = join(root, "docs/releases", `${packageJson.version}.md`);
const releaseNotes = existsSync(releaseNotesPath)
  ? readFileSync(releaseNotesPath, "utf8")
  : "";
const readme = readFileSync(join(root, "README.md"), "utf8");
const clawhubGuide = readFileSync(join(root, "docs/CLAWHUB_RELEASE.md"), "utf8");

const surfaces = {
  "package.json": packageJson.version,
  "openclaw.plugin.json": manifest.version,
  "package-lock.json": packageLock.version,
  "package-lock.json packages root": packageLock.packages?.[""]?.version,
  "Python __version__": pythonVersion,
  "CHANGELOG current section": currentChangelogVersion,
};

const errors = [];
for (const [label, version] of Object.entries(surfaces)) {
  if (version !== packageJson.version) {
    errors.push(`${label}: expected ${packageJson.version}, got ${version ?? "missing"}`);
  }
}
if (!existsSync(releaseNotesPath)) {
  errors.push(`release notes missing: docs/releases/${packageJson.version}.md`);
}
if (!releaseNotes.startsWith(`# Lobster Quant Agent ${packageJson.version} `)) {
  errors.push(`release notes heading does not declare ${packageJson.version}`);
}
if (!readme.includes(`\`${packageJson.version}\``)) {
  errors.push(`README does not name current release ${packageJson.version}`);
}
if (!clawhubGuide.includes(`Current release: \`${packageJson.version}\``)) {
  errors.push(`ClawHub guide does not name current release ${packageJson.version}`);
}

if (errors.length > 0) {
  process.stderr.write(`Version check FAILED:\n- ${errors.join("\n- ")}\n`);
  process.exit(1);
}

console.log(
  `Version check passed: ${packageJson.version} across package, manifest, lockfile, Python, changelog, and release notes.`,
);
