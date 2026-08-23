import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

describe("lobster-quant-agent", () => {
  it("ships generated metadata for the OpenClaw research tool", () => {
    const manifest = JSON.parse(readFileSync("openclaw.plugin.json", "utf8"));
    const packageJson = JSON.parse(readFileSync("package.json", "utf8"));
    expect(manifest.contracts.tools).toEqual(["lobster_quant"]);
    expect(manifest.description).toContain("No broker access");
    expect(manifest.version).toBe(packageJson.version);
    expect(packageJson.version).toBe("0.2.0-rc.1");
    expect(packageJson.openclaw.compat.pluginApi).toBe(">=2026.5.17");
    expect(packageJson.openclaw.build.openclawVersion).toBe("2026.7.1");
    expect(packageJson.openclaw.install.clawhubSpec).toBe("@cnan5336-dev/lobster-quant-agent");
  });
});
