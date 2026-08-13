declare module "openclaw/plugin-sdk/tool-plugin" {
  import type { Static, TSchema } from "typebox";

  export type ToolPluginExecutionContext = {
    signal?: AbortSignal;
    toolCallId: string;
  };

  type ToolDefinition<TConfig, TParams extends TSchema> = {
    name: string;
    label?: string;
    description: string;
    parameters: TParams;
    optional?: boolean;
    execute: (
      params: Static<TParams>,
      config: TConfig,
      context: ToolPluginExecutionContext,
    ) => unknown;
  };

  type ToolFactory<TConfig> = <TParams extends TSchema>(
    definition: ToolDefinition<TConfig, TParams>,
  ) => unknown;

  export function defineToolPlugin<TConfigSchema extends TSchema>(definition: {
    id: string;
    name: string;
    description: string;
    configSchema: TConfigSchema;
    tools: (tool: ToolFactory<Static<TConfigSchema>>) => readonly unknown[];
  }): unknown;
}
