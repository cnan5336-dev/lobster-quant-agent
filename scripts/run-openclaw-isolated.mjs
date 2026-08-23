import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const args = process.argv.slice(2);
const allowed =
  args[0] === "plugins" &&
  ["build", "validate"].includes(args[1]);

if (!allowed) {
  process.stderr.write("Only isolated 'openclaw plugins build|validate' commands are allowed.\n");
  process.exit(2);
}

const isolationRoot = mkdtempSync(join(tmpdir(), "lobster-openclaw-validation-"));
const isolatedHome = join(isolationRoot, "home");
const isolatedState = join(isolationRoot, "state");
const isolatedConfig = join(isolationRoot, "xdg-config");
const isolatedCache = join(isolationRoot, "xdg-cache");
for (const directory of [isolatedHome, isolatedState, isolatedConfig, isolatedCache]) {
  mkdirSync(directory, { recursive: true });
}

try {
  const result = spawnSync("openclaw", args, {
    cwd: new URL("..", import.meta.url),
    env: {
      ...process.env,
      HOME: isolatedHome,
      OPENCLAW_STATE_DIR: isolatedState,
      XDG_CONFIG_HOME: isolatedConfig,
      XDG_CACHE_HOME: isolatedCache,
    },
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
} finally {
  rmSync(isolationRoot, { recursive: true, force: true });
}
