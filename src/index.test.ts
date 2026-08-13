import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

describe("lobster-quant-agent", () => {
  it("ships generated metadata for the OpenClaw research tool", () => {
    const manifest = JSON.parse(readFileSync("openclaw.plugin.json", "utf8"));
    expect(manifest.contracts.tools).toEqual(["lobster_quant"]);
    expect(manifest.description).toContain("No broker access");
  });
});
