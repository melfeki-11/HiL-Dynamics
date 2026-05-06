#!/usr/bin/env node
import { query } from "@anthropic-ai/claude-agent-sdk";
import { DEFAULT_CLAUDE_MODEL, claudeEnv } from "../src/shared/config.mjs";

const env = await claudeEnv();
let text = "";
for await (const message of query({
  prompt: "Reply with exactly PONG.",
  options: { model: process.env.CLAUDE_CODE_MODEL || process.env.LITELLM_MODEL || DEFAULT_CLAUDE_MODEL, maxTurns: 2, env },
})) {
  if (message.type === "assistant") text += JSON.stringify(message);
}
console.log(text.includes("PONG") ? "Claude Code works: PONG" : `Claude Code completed: ${text.slice(0, 500)}`);
