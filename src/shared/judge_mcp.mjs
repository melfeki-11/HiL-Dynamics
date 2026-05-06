#!/usr/bin/env node
import path from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { DEFAULT_ASK_HUMAN_MODEL, DEFAULT_ASK_HUMAN_SEED } from "./config.mjs";
import { createHumanInputRouter, UNKNOWN_RESOLUTION } from "./human_input.mjs";

const instanceId = process.env.INSTANCE_ID || process.env.HARNESS_INSTANCE_ID || "";
const kbPath = process.env.HARNESS_HUMAN_KB || process.env.HUMAN_KB || "";
const cachePath = process.env.HARNESS_ASK_HUMAN_CACHE || process.env.ASK_HUMAN_CACHE || "";
const trajectoryFile = process.env.HARNESS_TRAJECTORY_FILE || "";
const workspaceDir = process.env.HARNESS_WORKSPACE_DIR || process.cwd();

if (!instanceId) {
  console.error("judge_mcp requires INSTANCE_ID");
  process.exit(2);
}

const router = createHumanInputRouter({
  instanceId,
  kbPath,
  cachePath,
  replay: process.env.HARNESS_ASK_HUMAN_REPLAY === "true",
  modelId: process.env.ASK_HUMAN_MODEL || DEFAULT_ASK_HUMAN_MODEL,
  seed: Number(process.env.ASK_HUMAN_SEED || DEFAULT_ASK_HUMAN_SEED),
  trajectoryFile,
  workspaceDir: path.resolve(workspaceDir),
  approvalPolicy: process.env.HARNESS_APPROVAL_POLICY_ROUTER || "safe-looking",
});

const server = new McpServer({
  name: "human_input",
  version: "0.1.0",
});

server.registerTool(
  "ask_human",
  {
    title: "Ask Human",
    description:
      "Ask a concise targeted clarification question about project intent or requirements that cannot be determined from the repository, tests, or tools.",
    inputSchema: {
      question: z.string(),
      request_type: z.enum(["clarification", "elicitation"]).optional(),
      options: z.array(z.object({ label: z.string(), description: z.string().optional() })).optional(),
    },
  },
  async (input) => {
    const result = await router.route({
      requestType: input.request_type || "clarification",
      nativeEventType: "mcp.ask_human",
      rawEvent: input,
      question: input.question,
      options: input.options || [],
      context: { source: "stdio_mcp_tool" },
    });
    return {
      content: [{ type: "text", text: result.resolution || UNKNOWN_RESOLUTION }],
    };
  }
);

await server.connect(new StdioServerTransport());
