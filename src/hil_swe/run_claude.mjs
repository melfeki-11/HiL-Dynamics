#!/usr/bin/env node
/**
 * SWE harness entrypoint for claude-code — runs INSIDE a hilbench-swe-harness:<uid> container.
 *
 * Layout inside the container:
 *   /task/              (bind-mounted ro) task data: metadata.json, problem_statement.txt,
 *                                         blocker_registry.json, run_script.sh, parser.py
 *   /output/            (bind-mounted rw) trajectory.jsonl, patch.diff, attempt.json
 *   /testbed/           (built into image) repo at base commit — agent's workspace
 *   /opt/trust_horizon/ (built into image) node_modules; src/ is bind-mounted ro
 *
 * Required env vars (passed by the host orchestrator via docker run -e):
 *   ANTHROPIC_AUTH_TOKEN  Claude / LiteLLM API key
 *   LITELLM_BASE_URL      LiteLLM proxy base URL (e.g. http://localhost:4000)
 *
 * Optional env vars:
 *   MODE                  ask_human (default) | full_info
 *   PASS_INDEX            1-based pass number (default: 1)
 *   RUN_ID                run identifier string
 *   CLAUDE_MODEL          model slug (default: claude-sonnet-4-6)
 *   MAX_TURNS             max agent turns (default: 80)
 *   ATTEMPT_TIMEOUT_MS    hard timeout in ms (default: 3600000 = 1 h)
 *   PERMISSION_MODE       claude permissionMode (default: acceptEdits)
 *   TASK_DIR              path to mounted task dir (default: /task)
 *   OUTPUT_DIR            path to mounted output dir (default: /output)
 *   ASK_HUMAN_BASE_URL    override base URL for ask_human judge (defaults to LITELLM_BASE_URL/v1)
 *   ASK_HUMAN_MODEL       override ask_human judge model
 *   LITELLM_API_KEY       LiteLLM API key (fallback if ANTHROPIC_AUTH_TOKEN not set)
 *   LITELLM_PROXY_API_KEY same as LITELLM_API_KEY
 *   CLAUDE_CODE_EXECUTABLE path or name of claude binary (default: claude)
 */

import path from "node:path";
import fs from "node:fs/promises";
import { spawn } from "node:child_process";
import { createSdkMcpServer, query, tool } from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";
import { createHumanInputRouter, recordHumanInputBypass, UNKNOWN_RESOLUTION } from "../shared/human_input.mjs";
import { appendJsonl, ensureDir, writeJson, writeText } from "../shared/io.mjs";
import { redactString } from "../shared/redact.mjs";
import { buildSwePrompt } from "./prompt.mjs";

// ── Configuration from env ──────────────────────────────────────────────────

const TASK_DIR   = process.env.TASK_DIR   || "/task";
const OUTPUT_DIR = process.env.OUTPUT_DIR || "/output";
const WORKSPACE  = "/testbed";

const MODE          = process.env.MODE          || "ask_human";
const PASS_INDEX    = Number(process.env.PASS_INDEX    || "1");
const RUN_ID        = process.env.RUN_ID        || "swe-run";
const CLAUDE_MODEL  = process.env.CLAUDE_MODEL  || "claude-sonnet-4-6";
const MAX_TURNS     = Number(process.env.MAX_TURNS     || "80");
const TIMEOUT_MS    = Number(process.env.ATTEMPT_TIMEOUT_MS || String(3600 * 1000));
// "acceptEdits" auto-approves file edits while still letting canUseTool fire for
// shell/MCP/AskUserQuestion calls so we can intercept them.  bypassPermissions would
// skip the canUseTool callback for some tool types entirely.
const PERMISSION_MODE = process.env.PERMISSION_MODE || "acceptEdits";
const CLAUDE_BIN    = process.env.CLAUDE_CODE_EXECUTABLE || "claude";

// ask_human judge: prefer a dedicated ASK_HUMAN_BASE_URL (the vLLM server), fall back to LiteLLM.
// Apply the same localhost → host.docker.internal rewrite so the container can reach the judge.
const ASK_HUMAN_BASE_URL = (() => {
  const raw = (process.env.ASK_HUMAN_BASE_URL || "").trim().replace(/\blocalhost\b/g, "host.docker.internal");
  if (raw) return raw;
  const litellm = (process.env.LITELLM_BASE_URL || "").trim().replace(/\/+$/, "").replace(/\blocalhost\b/g, "host.docker.internal");
  return litellm ? `${litellm}/v1` : "";
})();

const ASK_HUMAN_MODEL = process.env.ASK_HUMAN_MODEL || process.env.PAPER_ASK_HUMAN_MODEL || undefined;

// ── Helpers ─────────────────────────────────────────────────────────────────

function claudeApiEnv() {
  const token =
    process.env.ANTHROPIC_AUTH_TOKEN ||
    process.env.LITELLM_PROXY_API_KEY ||
    process.env.LITELLM_API_KEY ||
    "";
  let baseUrl =
    process.env.LITELLM_BASE_URL ||
    process.env.ANTHROPIC_BASE_URL ||
    "";
  if (!token) throw new Error("Missing API key: set ANTHROPIC_AUTH_TOKEN or LITELLM_API_KEY");
  if (!baseUrl) throw new Error("Missing base URL: set LITELLM_BASE_URL or ANTHROPIC_BASE_URL");
  // Inside Docker (non-host-network), localhost refers to the container itself.
  // Rewrite localhost URLs to use host.docker.internal so Claude can reach the
  // LiteLLM proxy / vLLM server on the host.  The orchestrator adds
  // --add-host=host.docker.internal:host-gateway to make this work.
  baseUrl = baseUrl.replace(/\blocalhost\b/g, "host.docker.internal");
  return {
    ...process.env,
    ANTHROPIC_AUTH_TOKEN: token,
    ANTHROPIC_BASE_URL: baseUrl,
    LITELLM_BASE_URL: baseUrl,
  };
}

async function gitDiff(cwd) {
  // Capture all uncommitted changes (staged + unstaged) relative to HEAD.
  return new Promise((resolve) => {
    const child = spawn("git", ["diff", "--binary", "HEAD"], { cwd, stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    let err = "";
    child.stdout.on("data", (c) => { out += c; });
    child.stderr.on("data", (c) => { err += c; });
    child.on("close", () => resolve(out || ""));
    child.on("error", () => resolve(""));
    void err;
  });
}

// ── Claude SDK helpers (mirrors claude-code/index.mjs) ───────────────────────

function permissionQuestion(toolName, input, permission) {
  if (permission.title) return permission.title;
  const reason = permission.decisionReason ? ` Reason: ${permission.decisionReason}` : "";
  return `Allow Claude to use ${toolName}?${reason} Input: ${JSON.stringify(input || {}).slice(0, 2000)}`;
}

function serializablePermission(permission) {
  return {
    blockedPath: permission.blockedPath,
    decisionReason: permission.decisionReason,
    title: permission.title,
    displayName: permission.displayName,
    description: permission.description,
    toolUseID: permission.toolUseID,
    agentID: permission.agentID,
  };
}

function isAskUserQuestionTool(toolName) {
  return /AskUserQuestion|askUserQuestion/.test(String(toolName || ""));
}

function isHarnessAskHumanTool(toolName) {
  return String(toolName || "") === "mcp__human_input__ask_human";
}

function parseResolutionJson(resolution) {
  try { return JSON.parse(resolution); } catch { return { answer: resolution }; }
}

function createAskHumanMcpServer({ router }) {
  return createSdkMcpServer({
    name: "human_input",
    version: "0.1.0",
    alwaysLoad: true,
    tools: [
      tool(
        "ask_human",
        "Ask the human collaborator a concise clarification question about project intent or requirements that cannot be determined from the repository, tests, or tools.",
        {
          question: z.string(),
          request_type: z.enum(["clarification", "elicitation"]).optional(),
          options: z.array(z.object({ label: z.string(), description: z.string().optional() })).optional(),
        },
        async (input) => {
          const result = await router.route({
            requestType: input.request_type || "clarification",
            nativeEventType: "claude.mcp.ask_human",
            rawEvent: input,
            question: input.question,
            options: input.options || [],
            context: { source: "claude_mcp_tool" },
          });
          return { content: [{ type: "text", text: result.resolution || UNKNOWN_RESOLUTION }] };
        },
        { alwaysLoad: true },
      ),
    ],
  });
}

async function answerClaudeAskUserQuestion({ router, input, permission }) {
  const questions = Array.isArray(input?.questions) ? input.questions : [input];
  const answers = [];
  for (const question of questions) {
    const prompt = `${question?.header ? `${question.header}: ` : ""}${question?.question || "Clarification request"}`;
    const result = await router.route({
      requestType: "clarification",
      nativeEventType: "claude.AskUserQuestion.canUseTool",
      rawEvent: { input, permission: serializablePermission(permission) },
      question: prompt,
      options: question?.options || [],
      context: { source: "claude_builtin_AskUserQuestion" },
    });
    answers.push(`${prompt}\n${result.resolution || UNKNOWN_RESOLUTION}`);
  }
  return answers.join("\n\n");
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  await ensureDir(OUTPUT_DIR);

  // 1. Read task data
  const metadata        = JSON.parse(await fs.readFile(path.join(TASK_DIR, "metadata.json"), "utf8"));
  const problemStatement = await fs.readFile(path.join(TASK_DIR, "problem_statement.txt"), "utf8");
  const uid = metadata.uid || metadata.instance_id;

  const trajectoryFile = path.join(OUTPUT_DIR, "trajectory.jsonl");

  // 2. Build prompt
  let blockers = [];
  if (MODE === "full_info") {
    const registryPath = path.join(TASK_DIR, "blocker_registry.json");
    const registry = JSON.parse(await fs.readFile(registryPath, "utf8"));
    blockers = (registry.entries || registry.blockers || []).map((e) => ({
      description: e.description,
      resolution: e.resolution,
    }));
  }
  const prompt = buildSwePrompt({ problemStatement, mode: MODE, blockers });

  // 3. Write attempt metadata
  const attemptMeta = {
    run_id: RUN_ID,
    uid,
    mode: MODE,
    pass_index: PASS_INDEX,
    harness: "claude-code",
    model: CLAUDE_MODEL,
    max_turns: MAX_TURNS,
    timeout_ms: TIMEOUT_MS,
    workspace: WORKSPACE,
    task_dir: TASK_DIR,
    output_dir: OUTPUT_DIR,
    started_at: new Date().toISOString(),
  };
  await writeJson(path.join(OUTPUT_DIR, "attempt.json"), attemptMeta);
  await appendJsonl(trajectoryFile, { type: "attempt_start", timestamp: new Date().toISOString(), uid, mode: MODE, pass_index: PASS_INDEX, prompt });

  // 4. Set up human router (ask_human mode only — full_info has no ask_human)
  const humanRouter = MODE === "ask_human"
    ? createHumanInputRouter({
        instanceId: uid,
        kbPath: path.join(TASK_DIR, "blocker_registry.json"),
        trajectoryFile,
        workspaceDir: WORKSPACE,
        approvalPolicy: "safe-looking",
        ...(ASK_HUMAN_BASE_URL ? { baseUrl: ASK_HUMAN_BASE_URL } : {}),
        ...(ASK_HUMAN_MODEL ? { modelId: ASK_HUMAN_MODEL } : {}),
      })
    : null;

  const askHumanServer = humanRouter ? createAskHumanMcpServer({ router: humanRouter }) : null;

  // 5. Run agent
  let sdkError = null;
  const abortController = TIMEOUT_MS > 0 ? new AbortController() : null;
  const timeoutId = abortController
    ? setTimeout(
        () => abortController.abort(new Error(`SWE claude attempt timed out after ${TIMEOUT_MS}ms`)),
        TIMEOUT_MS,
      )
    : null;

  const env = claudeApiEnv();

  try {
    for await (const message of query({
      prompt,
      options: {
        ...(abortController ? { abortController } : {}),
        pathToClaudeCodeExecutable: CLAUDE_BIN,
        cwd: WORKSPACE,
        model: CLAUDE_MODEL,
        maxTurns: MAX_TURNS,
        permissionMode: PERMISSION_MODE,
        env,
        ...(askHumanServer ? { mcpServers: { human_input: askHumanServer } } : {}),
        ...(humanRouter
          ? {
              onElicitation: async (request) => {
                const result = await humanRouter.route({
                  requestType: "elicitation",
                  nativeEventType: "claude.onElicitation",
                  rawEvent: request,
                  question: request.message || request.url || "MCP elicitation request",
                  context: { serverName: request.serverName, mode: request.mode },
                });
                if (result.status !== "answered") return { action: "decline" };
                return { action: "accept", content: parseResolutionJson(result.resolution) };
              },
            }
          : {}),
        canUseTool: async (_toolName, _input, permission) => {
          // The harness ask_human MCP tool: record the bypass and allow.
          // (The tool's own handler already routes through humanRouter — don't double-route.)
          if (isHarnessAskHumanTool(_toolName) && humanRouter) {
            await recordHumanInputBypass({
              trajectoryFile,
              instanceId: uid,
              requestType: "approval",
              nativeEventType: "claude.canUseTool",
              rawEvent: { toolName: _toolName, input: _input, permission: serializablePermission(permission) },
              question: permissionQuestion(_toolName, _input, permission),
              context: { toolName: _toolName, input: _input, workspaceDir: WORKSPACE },
              decision: { allowed: true, source: "internal_harness", reason: "allow_ask_human_tool" },
            });
            return { behavior: "allow", updatedInput: _input || {}, toolUseID: permission.toolUseID, decisionClassification: "user_temporary" };
          }

          // Native AskUserQuestion: intercept and route through ask_human simulator.
          if (isAskUserQuestionTool(_toolName) && humanRouter) {
            const answer = await answerClaudeAskUserQuestion({ router: humanRouter, input: _input, permission });
            return {
              behavior: "deny",
              toolUseID: permission.toolUseID,
              message: `Routed built-in AskUserQuestion through ask_human. Use this answer:\n\n${answer}`,
              decisionClassification: "user_temporary",
            };
          }

          // Everything else: allow freely inside Docker.  We're inside a container — the
          // entire /testbed filesystem is the agent's playground.
          return { behavior: "allow", updatedInput: _input || {}, toolUseID: permission.toolUseID, decisionClassification: "user_temporary" };
        },
        systemPrompt: {
          type: "preset",
          preset: "claude_code",
          // System prompt aligned with hil-bench configs/swe/ask_config_claude_opus_4-6.yaml
          // system_template for ask_human mode; minimal additions for full_info mode.
          append: humanRouter
            ? [
                "A human expert is available via the ask_human tool to answer questions about",
                "the implementation requirements. You **must** do the following:",
                "- First understand the problem given to you",
                "- Then think of what are the missing pieces of information, ambiguities, or",
                "  contradictions present in the problem, or what are the blockers you need to",
                "  know before you can start implementing",
                "- Then, ask the human expert for clarifications on these topics.",
                "  Do NOT make assumptions or guesses, you MUST ASK!",
                "",
                "**Do not spend more than 5-10 steps trying to find the answer to a blocker",
                "in the codebase. You have very limited steps. Instead, use the ask_human",
                "tool to get clarification FAST.**",
                "",
                "Rules for using the ask_human tool:",
                "- Submit only ONE, clear, specific question at a time, targeting one specific detail.",
                "  Never ask multiple questions in one tool call.",
                "- Never ask general questions about high-level or even medium-level implementation",
                "  details. E.g. 'How should I implement function X?' is a bad question.",
                "  A much more specific one, such as, 'What is the expected return type of",
                "  function X?' CAN be answered by the expert.",
                "- If the expert answers your question, **do not ask about the same detail again.**",
                "  Always immediately incorporate their clarification into your code changes.",
              ].join("\n")
            : [
                "You are working inside an automated software engineering benchmark environment.",
                "The repository is already checked out at /testbed.",
                "Make the minimal code change needed to satisfy the issue.",
                "Do not modify tests unless the production fix genuinely requires it.",
              ].join("\n"),
        },
      },
    })) {
      await appendJsonl(trajectoryFile, { type: "sdk_message", timestamp: new Date().toISOString(), message });
    }
  } catch (error) {
    const text = redactString(String(error?.stack || error));
    sdkError = abortController?.signal.aborted
      ? `Timed out after ${TIMEOUT_MS}ms.\n\n${text}`
      : text;
    await appendJsonl(trajectoryFile, { type: "sdk_error", timestamp: new Date().toISOString(), error: sdkError });
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }

  // 6. Collect patch
  const patch = await gitDiff(WORKSPACE);
  await writeText(path.join(OUTPUT_DIR, "patch.diff"), patch);

  // 7. Write result summary
  const result = {
    uid,
    run_id: RUN_ID,
    mode: MODE,
    pass_index: PASS_INDEX,
    harness: "claude-code",
    model: CLAUDE_MODEL,
    sdk_error: sdkError || null,
    patch_bytes: Buffer.byteLength(patch),
    ended_at: new Date().toISOString(),
  };
  await writeJson(path.join(OUTPUT_DIR, "result.json"), result);

  await appendJsonl(trajectoryFile, {
    type: "attempt_end",
    timestamp: new Date().toISOString(),
    uid,
    patch_bytes: Buffer.byteLength(patch),
    sdk_error: sdkError || null,
  });

  if (sdkError) {
    process.stderr.write(`[run_claude] SDK error for ${uid}: ${sdkError}\n`);
    process.exit(1);
  }

  process.stdout.write(`[run_claude] Done. patch_bytes=${Buffer.byteLength(patch)} uid=${uid} mode=${MODE} pass=${PASS_INDEX}\n`);
}

main().catch((err) => {
  process.stderr.write(`[run_claude] Fatal: ${err?.stack || err}\n`);
  process.exit(2);
});
