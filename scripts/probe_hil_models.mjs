#!/usr/bin/env node
import path from "node:path";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { Codex } from "@openai/codex-sdk";
import {
  DEFAULT_ASK_HUMAN_MODEL,
  DEFAULT_CLAUDE_MODEL,
  DEFAULT_CODEX_MODEL,
  DEFAULT_CODEX_REASONING_EFFORT,
  DEFAULT_OPENCODE_MODEL,
  claudeEnv,
  codexClientOptions,
  getLiteLLMKey,
  getResponsesBaseUrl,
} from "../src/shared/config.mjs";
import { ensureDir, writeJsonAtomic } from "../src/shared/io.mjs";
import { redactString } from "../src/shared/redact.mjs";

function parseArgs(argv) {
  const args = {
    claudeModelCandidates: [DEFAULT_CLAUDE_MODEL],
    claudeThinking: "disabled",
    claudeEffort: "low",
    codexModel: DEFAULT_CODEX_MODEL,
    codexReasoningEffort: DEFAULT_CODEX_REASONING_EFFORT,
    opencodeModel: DEFAULT_OPENCODE_MODEL,
    askHumanModel: DEFAULT_ASK_HUMAN_MODEL,
    out: undefined,
    timeoutMs: Number(process.env.HIL_MODEL_PROBE_TIMEOUT_MS || 120000),
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--claude-model-candidates") args.claudeModelCandidates = argv[++i].split(",").map((item) => item.trim()).filter(Boolean);
    else if (arg === "--claude-thinking") args.claudeThinking = argv[++i];
    else if (arg === "--claude-effort") args.claudeEffort = argv[++i];
    else if (arg === "--codex-model") args.codexModel = argv[++i];
    else if (arg === "--codex-reasoning-effort") args.codexReasoningEffort = argv[++i];
    else if (arg === "--opencode-model") args.opencodeModel = argv[++i];
    else if (arg === "--ask-human-model") args.askHumanModel = argv[++i];
    else if (arg === "--out") args.out = argv[++i];
    else if (arg === "--timeout-ms") args.timeoutMs = Number(argv[++i]);
    else throw new Error(`Unknown argument: ${arg}`);
  }
  if (!args.claudeModelCandidates.length) throw new Error("--claude-model-candidates must include at least one model");
  return args;
}

function errorText(error) {
  return redactString(String(error?.stack || error?.message || error)).slice(0, 2000);
}

async function withTimeout(label, timeoutMs, fn) {
  const abortController = new AbortController();
  const timeout = setTimeout(() => abortController.abort(new Error(`${label} probe timed out after ${timeoutMs}ms`)), timeoutMs);
  try {
    return await fn(abortController);
  } finally {
    clearTimeout(timeout);
  }
}

async function probeClaude(args) {
  const attempts = [];
  const env = await claudeEnv();
  for (const model of args.claudeModelCandidates) {
    try {
      let text = "";
      await withTimeout(`Claude ${model}`, args.timeoutMs, async (abortController) => {
        const options = {
          abortController,
          model,
          maxTurns: 2,
          env,
          ...(args.claudeThinking === "adaptive" ? { thinking: { type: "adaptive" } } : {}),
          ...(args.claudeThinking === "disabled" ? { thinking: { type: "disabled" } } : {}),
          ...(args.claudeEffort ? { effort: args.claudeEffort } : {}),
        };
        for await (const message of query({ prompt: "Reply with exactly PONG.", options })) {
          if (message.type === "assistant") text += JSON.stringify(message);
        }
      });
      const ok = /PONG/i.test(text);
      attempts.push({ model, ok, output_excerpt: text.slice(0, 500) });
      if (ok) return { accepted_model: model, thinking: args.claudeThinking, effort: args.claudeEffort, attempts };
    } catch (error) {
      attempts.push({ model, ok: false, error: errorText(error) });
    }
  }
  throw new Error(`Claude probe failed for all candidates: ${JSON.stringify(attempts, null, 2)}`);
}

async function probeCodex(args) {
  const options = await codexClientOptions();
  return await withTimeout(`Codex ${args.codexModel}`, args.timeoutMs, async (abortController) => {
    const codex = new Codex(options);
    const thread = codex.startThread({
      workingDirectory: process.cwd(),
      skipGitRepoCheck: false,
      model: args.codexModel,
      modelReasoningEffort: args.codexReasoningEffort,
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
    if (!/PONG/i.test(final)) throw new Error(`Codex probe did not include PONG: ${final.slice(0, 500)}`);
    return { accepted_model: args.codexModel, reasoning_effort: args.codexReasoningEffort, output_excerpt: final.slice(0, 500) };
  });
}

async function probeAskHumanJudge(args) {
  return probeLiteLLMModel(args.askHumanModel, "ask_human judge");
}

async function probeOpenCodeModel(args) {
  return probeLiteLLMModel(args.opencodeModel, "OpenCode model");
}

async function probeLiteLLMModel(model, label) {
  const token = await getLiteLLMKey();
  const url = `${getResponsesBaseUrl().replace(/\/+$/, "")}/chat/completions`;
  const response = await fetch(url, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({
      model,
      messages: [{ role: "user", content: "Reply with exactly PONG." }],
      temperature: 0,
      max_tokens: 16,
    }),
  });
  const bodyText = await response.text();
  if (!response.ok) throw new Error(`${label} probe failed ${response.status}: ${bodyText.slice(0, 500)}`);
  const parsed = JSON.parse(bodyText);
  const content = parsed?.choices?.[0]?.message?.content || "";
  if (!/PONG/i.test(content)) throw new Error(`${label} probe did not include PONG: ${content.slice(0, 500)}`);
  return { accepted_model: model, output_excerpt: content.slice(0, 500) };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const startedAt = new Date().toISOString();
  const result = {
    started_at: startedAt,
    cwd: process.cwd(),
    claude: await probeClaude(args),
    codex: await probeCodex(args),
    opencode: await probeOpenCodeModel(args),
    ask_human_judge: await probeAskHumanJudge(args),
    completed_at: new Date().toISOString(),
  };
  if (args.out) {
    await ensureDir(path.dirname(path.resolve(args.out)));
    await writeJsonAtomic(args.out, result);
  }
  console.log(`Claude accepted model: ${result.claude.accepted_model}`);
  console.log(`Codex accepted model: ${result.codex.accepted_model}`);
  console.log(`OpenCode accepted model: ${result.opencode.accepted_model}`);
  console.log(`ask_human judge accepted model: ${result.ask_human_judge.accepted_model}`);
  if (args.out) console.log(args.out);
}

main().catch((error) => {
  console.error(errorText(error));
  process.exitCode = 1;
});
