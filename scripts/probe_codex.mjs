#!/usr/bin/env node
import { Codex } from "@openai/codex-sdk";
import { DEFAULT_CODEX_MODEL, DEFAULT_CODEX_REASONING_EFFORT, codexClientOptions } from "../src/shared/config.mjs";

const timeoutMs = Number(process.env.CODEX_PROBE_TIMEOUT_MS || 60000);
const abortController = new AbortController();
const timeoutId = setTimeout(() => abortController.abort(new Error(`Codex probe timed out after ${timeoutMs}ms`)), timeoutMs);

try {
  const options = await codexClientOptions();
  const codex = new Codex(options);
  const thread = codex.startThread({
    workingDirectory: process.cwd(),
    skipGitRepoCheck: false,
    model: process.env.CODEX_MODEL || process.env.LITELLM_MODEL || DEFAULT_CODEX_MODEL,
    modelReasoningEffort: process.env.CODEX_MODEL_REASONING_EFFORT || DEFAULT_CODEX_REASONING_EFFORT,
    sandboxMode: "workspace-write",
    networkAccessEnabled: true,
    approvalPolicy: "never",
  });
  const { events } = await thread.runStreamed("Reply with exactly PONG.", { signal: abortController.signal });
  let final = "";
  for await (const event of events) {
    if (event.type === "item.completed" && event.item?.type === "agent_message") final = event.item.text || final;
    if (event.type === "turn.failed") throw new Error(event.error?.message || JSON.stringify(event));
    if (event.type === "error") throw new Error(event.message || JSON.stringify(event));
  }
  console.log(final.includes("PONG") ? "Codex works: PONG" : `Codex completed: ${final}`);
} catch (error) {
  const message = String(error?.stack || error);
  const prefix = abortController.signal.aborted ? `Codex probe timed out after ${timeoutMs}ms.\n\n` : "";
  throw new Error(`${prefix}${message}\n\nCodex is configured to use the LiteLLM proxy. If this is a Responses API/protocol compatibility failure, fix the proxy/model route rather than falling back to local Codex login.`);
} finally {
  clearTimeout(timeoutId);
}
