import { rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outputDirectory = resolve(repositoryRoot, "dist");
if (outputDirectory !== resolve(repositoryRoot, "dist")) {
  throw new Error("Refusing to clean an unexpected directory.");
}
rmSync(outputDirectory, { recursive: true, force: true });
