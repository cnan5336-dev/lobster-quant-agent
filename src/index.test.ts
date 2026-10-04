import { beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";

const mocks = vi.hoisted(() => ({ execFile: vi.fn() }));
vi.mock("node:child_process", () => ({ execFile: mocks.execFile }));
vi.mock("openclaw/plugin-sdk/tool-plugin", () => ({ defineToolPlugin: (definition: unknown) => definition }));

import plugin from "./index.js";

type Invocation = { command: string; arguments?: string[]; channel?: string };
type TestTool = {
  description: string;
  execute: (params: Invocation, config: Record<string, unknown>, context: { toolCallId: string; signal?: AbortSignal }) => Promise<string>;
};
const tool = (plugin as { tools: (factory: (definition: TestTool) => TestTool) => TestTool[] }).tools((definition) => definition)[0];

describe("OpenClaw command execution", () => {
  beforeEach(() => {
    mocks.execFile.mockReset();
    mocks.execFile.mockImplementation((_executable, _args, _options, callback) => callback(null, "draft result", ""));
  });

  it("passes the exact strategy wording in one argument without a shell", async () => {
    const original = "观察池价格高于3元且换手率超过3%，或者跌破2.8元；不要改成全部或。";
    await tool.execute({ command: "strategy", arguments: ["dry-run", original] }, {}, { toolCallId: "test" });
    const [executable, args, options] = mocks.execFile.mock.calls[0];
    expect(executable).toBe("python3");
    expect(args.slice(1)).toEqual(["strategy", "dry-run", original]);
    expect(options.shell).toBeUndefined();
    expect(options.timeout).toBe(120000);
    expect(options.env.LOBSTER_QUANT_MODEL_TIMEOUT_SECONDS).toBe("115");
    expect(options.env.LOBSTER_QUANT_PRIMARY_MODEL).toBe("");
    expect(tool.description).toContain("original wording unchanged");
  });

  it("propagates explicit configuration and cancellation without changing host environment", async () => {
    const before = process.env.LOBSTER_QUANT_PRIMARY_MODEL;
    const controller = new AbortController();
    await tool.execute({ command: "model", arguments: ["call", "synthetic"], channel: "weixin" }, {
      pythonExecutable: "custom-python", primaryModel: "provider/model", fallbackModels: ["provider/backup"],
      timeoutSeconds: 30, stateDirectory: "/tmp/synthetic-state",
    }, { toolCallId: "test", signal: controller.signal });
    const [executable, , options] = mocks.execFile.mock.calls[0];
    expect(executable).toBe("custom-python");
    expect(options.signal).toBe(controller.signal);
    expect(options.env.LOBSTER_QUANT_MODEL_TIMEOUT_SECONDS).toBe("25");
    expect(options.env.LOBSTER_QUANT_PRIMARY_MODEL).toBe("provider/model");
    expect(options.env.LOBSTER_QUANT_HOME).toBe("/tmp/synthetic-state");
    expect(process.env.LOBSTER_QUANT_PRIMARY_MODEL).toBe(before);
  });

  it("does not launch an already aborted request", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(tool.execute({ command: "quote" }, {}, { toolCallId: "test", signal: controller.signal })).rejects.toThrow();
    expect(mocks.execFile).not.toHaveBeenCalled();
  });

  it("reports deadlines and cancellations without suggesting an unsafe duplicate retry", async () => {
    mocks.execFile.mockImplementation((_executable, _args, _options, callback) => callback({ killed: true }, "", ""));
    await expect(tool.execute({ command: "strategy", arguments: ["set", "synthetic"] }, {}, { toolCallId: "test" }))
      .rejects.toThrow("120s command deadline. Check the command status");
    mocks.execFile.mockImplementation((_executable, _args, _options, callback) => callback({ name: "AbortError" }, "", ""));
    await expect(tool.execute({ command: "quote" }, {}, { toolCallId: "test" })).rejects.toThrow("cancelled");
  });
});

describe("lobster-quant-agent", () => {
  it("ships generated metadata for the OpenClaw research tool", () => {
    const manifest = JSON.parse(readFileSync("openclaw.plugin.json", "utf8"));
    const packageJson = JSON.parse(readFileSync("package.json", "utf8"));
    const packageLock = JSON.parse(readFileSync("package-lock.json", "utf8"));
    const pythonVersion = readFileSync("python/lobster_quant_agent/__init__.py", "utf8")
      .match(/^__version__ = ["']([^"']+)["']$/m)?.[1];
    expect(manifest.contracts.tools).toEqual(["lobster_quant"]);
    expect(manifest.description).toContain("No broker access");
    expect(manifest.version).toBe(packageJson.version);
    expect(packageJson.version).toBe("0.2.0");
    expect(packageLock.version).toBe(packageJson.version);
    expect(packageLock.packages[""].version).toBe(packageJson.version);
    expect(pythonVersion).toBe(packageJson.version);
    expect(packageJson.openclaw.compat.pluginApi).toBe(">=2026.5.17");
    expect(packageJson.openclaw.build.openclawVersion).toBe("2026.7.1");
    expect(packageJson.openclaw.install.clawhubSpec).toBe("@cnan5336-dev/lobster-quant-agent");
  });
});
