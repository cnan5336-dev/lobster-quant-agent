import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

const deliveryTarget = Type.Object({
  target: Type.String({ description: "Destination understood by the installed OpenClaw channel." }),
  account: Type.Optional(Type.String({ description: "Optional OpenClaw channel account id." })),
}, { additionalProperties: false });

const configSchema = Type.Object({
  pythonExecutable: Type.Optional(Type.String({ description: "Python executable from the repository virtual environment." })),
  stateDirectory: Type.Optional(Type.String({ description: "Private local state directory. Never point this at the repository." })),
  defaultChannel: Type.Optional(Type.Union([
    Type.Literal("weixin"),
    Type.Literal("telegram"),
    Type.Literal("qq"),
  ])),
  primaryModel: Type.Optional(Type.String({ description: "OpenClaw model key used first for model-assisted tasks." })),
  fallbackModels: Type.Optional(Type.Array(Type.String(), { maxItems: 2 })),
  notifyChannels: Type.Optional(Type.Array(Type.Union([
    Type.Literal("weixin"),
    Type.Literal("telegram"),
    Type.Literal("qq"),
  ]), { uniqueItems: true })),
  notificationTargets: Type.Optional(Type.Object({
    weixin: Type.Optional(deliveryTarget),
    telegram: Type.Optional(deliveryTarget),
    qq: Type.Optional(deliveryTarget),
  })),
  timeoutSeconds: Type.Optional(Type.Integer({ minimum: 5, maximum: 600 })),
}, { additionalProperties: false });

const cliPath = fileURLToPath(new URL("../python/lobster_quant_agent/cli.py", import.meta.url));

function executeCli(
  pythonExecutable: string,
  args: string[],
  env: NodeJS.ProcessEnv,
  timeoutSeconds: number,
  signal?: AbortSignal,
): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      pythonExecutable,
      [cliPath, ...args],
      {
        env,
        timeout: timeoutSeconds * 1000,
        maxBuffer: 10 * 1024 * 1024,
        signal,
      },
      (error, stdout, stderr) => {
        if (error) {
          reject(new Error((stderr || error.message || "Lobster Quant Agent failed").trim().slice(0, 2000)));
          return;
        }
        resolve((stdout || stderr || "").trim());
      },
    );
  });
}

export default defineToolPlugin({
  id: "lobster-quant-agent",
  name: "Lobster Quant Agent",
  description: "OpenClaw research and alert tools for A-share and US-market workflows. No broker access or order execution.",
  configSchema,
  tools: (tool) => [
    tool({
      name: "lobster_quant",
      label: "Lobster Quant Research",
      description: "Run a Lobster Quant Agent research command inside OpenClaw. Use nl for natural-language requests. This tool provides research and alerts only and cannot place orders.",
      parameters: Type.Object({
        command: Type.Union([
          Type.Literal("nl"),
          Type.Literal("quote"),
          Type.Literal("index"),
          Type.Literal("lhb"),
          Type.Literal("morning_report"),
          Type.Literal("after_close_report"),
          Type.Literal("watch"),
          Type.Literal("hold"),
          Type.Literal("pool"),
          Type.Literal("monitor"),
          Type.Literal("strategy"),
          Type.Literal("backtest"),
          Type.Literal("model"),
          Type.Literal("demo"),
        ]),
        arguments: Type.Optional(Type.Array(Type.String(), { maxItems: 40 })),
        channel: Type.Optional(Type.Union([
          Type.Literal("weixin"),
          Type.Literal("telegram"),
          Type.Literal("qq"),
        ])),
      }, { additionalProperties: false }),
      async execute({ command, arguments: commandArguments, channel }, config, context) {
        context.signal?.throwIfAborted();
        const selectedChannel = channel ?? config.defaultChannel ?? "telegram";
        const env: NodeJS.ProcessEnv = {
          ...process.env,
          LOBSTER_QUANT_CHANNEL: selectedChannel,
          LOBSTER_QUANT_PRIMARY_MODEL: config.primaryModel ?? "",
          LOBSTER_QUANT_FALLBACK_MODELS: (config.fallbackModels ?? []).join(","),
          LOBSTER_QUANT_NOTIFY_CHANNELS: (config.notifyChannels ?? []).join(","),
          LOBSTER_QUANT_NOTIFY_TARGETS: JSON.stringify(config.notificationTargets ?? {}),
        };
        if (config.stateDirectory) {
          env.LOBSTER_QUANT_HOME = config.stateDirectory;
        }
        const output = await executeCli(
          config.pythonExecutable ?? process.env.LOBSTER_QUANT_PYTHON ?? "python3",
          [command, ...(commandArguments ?? [])],
          env,
          config.timeoutSeconds ?? 120,
          context.signal,
        );
        return output || "Lobster Quant Agent completed without output.";
      },
    }),
  ],
});
