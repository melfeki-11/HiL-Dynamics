#!/usr/bin/env node
/**
 * SWE harness entrypoint for codex — runs INSIDE a hilbench-swe-harness-codex:<uid> container.
 *
 * Uses the codex app-server (JSON-RPC, stdio transport) so that native question-asking
 * (item/tool/requestUserInput) can be intercepted and routed through the same
 * LLM-backed human simulator used by run_claude.mjs.  This means:
 *
 *   ask_human mode : item/tool/requestUserInput → ask_human_sidecar → LLM judge
 *                    (identical to claude-code AskUserQuestion routing)
 *   full_info mode : item/tool/requestUserInput → answers filled with UNKNOWN_RESOLUTION
 *                    ("irrelevant question") — mirrors claude-code full_info behaviour
 *
 * Layout inside the container (identical to run_claude.mjs):
 *   /task/      ro  metadata.json, problem_statement.txt, blocker_registry.json, ...
 *   /output/    rw  trajectory.json, stats.json, patch.diff, result.json, attempt.json
 *   /app/           repo at base commit — agent workspace
 *   /opt/trust_horizon/  node_modules (npm ci); src/ bind-mounted ro
 *
 * Required env vars (passed by run_hil_swe.py via docker run -e):
 *   LITELLM_BASE_URL                    LiteLLM proxy base URL
 *   LITELLM_API_KEY | LITELLM_PROXY_API_KEY | ANTHROPIC_AUTH_TOKEN   (API key)
 *
 * Optional env vars:
 *   MODE               ask_human (default) | full_info
 *   PASS_INDEX         1-based pass number (default: 1)
 *   RUN_ID             run identifier string
 *   CODEX_MODEL        model slug forwarded to the app-server (default: gpt-5.5)
 *   ATTEMPT_TIMEOUT_MS hard timeout in ms (default: 10800000 = 3 h)
 *   TASK_DIR           path to mounted task dir (default: /task)
 *   OUTPUT_DIR         path to mounted output dir (default: /output)
 *   ASK_HUMAN_BASE_URL override base URL for ask_human judge
 *   ASK_HUMAN_MODEL    override ask_human judge model
 */

import path from "node:path";
import fs from "node:fs/promises";
import os from "node:os";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  UNKNOWN_RESOLUTION,
  CANT_ANSWER,
  UNKNOWN_BLOCKER_ID,
  ASK_HUMAN_REQUEST_TYPES,
  APPROVAL_REQUEST_TYPES,
} from "../shared/human_input.mjs";
import { ensureDir, writeJson, writeText } from "../shared/io.mjs";
import { redactString } from "../shared/redact.mjs";
import { buildSwePrompt } from "./prompt.mjs";
import { installAgentsSkill, removeInstalledAskHumanSkills, SKILL_TOOL_REF } from "./skills.mjs";
import {
  WORKSPACE, TASK_DIR, OUTPUT_DIR,
  MODE, ASK_HUMAN_ENABLED, SKILL_ENABLED, FULL_INFO_ENABLED,
  SKILL_TEMPLATE_VERSION, ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION,
  ASK_HUMAN_GUIDANCE_ENABLED, PASS_INDEX, RUN_ID, TIMEOUT_MS,
  LITELLM_CALL_TIMEOUT_MS, STEP_LITELLM_TRIES,
  ASK_HUMAN_BASE_URL, ASK_HUMAN_MODEL, buildAskHumanGuidance,
  THOUGHT_CAP, ACT_CAP, OBS_CAP, cap, computeResourceStats, gitDiff,
} from "./constants.mjs";
import { httpGetJson, sidecarAsk, startAskHumanSidecar, stopSidecar } from "./ask_human_sidecar_client.mjs";

// Codex's native question-asking tool is requestUserInput. Guidance is an
// explicit diagnostic flag, not part of ask_human by default.
const ASK_HUMAN_GUIDANCE = buildAskHumanGuidance("requestUserInput and/or ask_human");

// ── Configuration from env ──────────────────────────────────────────────────

const CODEX_MODEL = process.env.CODEX_MODEL || "gpt-5.5";
const CODEX_BIN   = process.env.CODEX_CODE_EXECUTABLE || "codex";
const WITH_CUSTOM_TOOL = ASK_HUMAN_ENABLED && /^(1|true|yes|on)$/i.test(String(process.env.WITH_CUSTOM_TOOL ?? "0"));
const CODEX_APPROVAL_POLICY = "on-request";
// Optional reasoning effort override (none | minimal | low | medium | high | xhigh).
// When set, plumbed three ways for belt-and-suspenders propagation:
//   1. injected into CODEX_APP_CONFIG so thread/start.config carries it
//   2. passed as `-c model_reasoning_effort=...` to codex app-server at spawn
//   3. logged to stderr so container.log records the effective value
const CODEX_REASONING_EFFORT = (process.env.CODEX_REASONING_EFFORT || "").trim();
const VALID_REASONING_EFFORTS = new Set([
  "none", "minimal", "low", "medium", "high", "xhigh",
]);
if (CODEX_REASONING_EFFORT && !VALID_REASONING_EFFORTS.has(CODEX_REASONING_EFFORT)) {
  throw new Error(
    `Invalid CODEX_REASONING_EFFORT='${CODEX_REASONING_EFFORT}'. ` +
    `Must be one of: ${[...VALID_REASONING_EFFORTS].join(", ")}.`,
  );
}
// MAX_STEPS: max completed items (commands + file edits + tool calls) before we interrupt
// the turn via turn/interrupt.  0 or unset = no limit (wall-clock TIMEOUT_MS still applies).
// The codex app-server has no native turn limit param, so we implement it by counting
// ItemCompletedNotification events and calling turn/interrupt when the threshold is reached.
const MAX_STEPS   = Number(process.env.MAX_STEPS || "0");
const __dirname   = path.dirname(fileURLToPath(import.meta.url));
const BRIDGE_SCRIPT  = path.join(__dirname, "ask_human_mcp_bridge.mjs");

// ── API env helpers ──────────────────────────────────────────────────────────

/**
 * Build the env block for the codex app-server subprocess, mirroring
 * codexClientOptions() from shared/config.mjs but adapted for the container
 * environment where credentials come from forwarded env vars, not .env files.
 *
 * CODEX_APP_CONFIG tells the app-server to use LiteLLM as its model provider
 * via the OpenAI Responses API (wire_api: "responses").
 */
function codexApiEnv({ sidecarUrl = "" } = {}) {
  const token =
    process.env.LITELLM_API_KEY ||
    process.env.LITELLM_PROXY_API_KEY ||
    process.env.ANTHROPIC_AUTH_TOKEN ||
    "";
  let baseUrl = (process.env.LITELLM_BASE_URL || "")
    .trim()
    .replace(/\/+$/, "")
    .replace(/\blocalhost\b/g, "host.docker.internal");

  if (!token)   throw new Error("Missing API key: set LITELLM_API_KEY or ANTHROPIC_AUTH_TOKEN");
  if (!baseUrl) throw new Error("Missing base URL: set LITELLM_BASE_URL");

  // The app-server expects the Responses-API endpoint at <base>/v1
  const responsesBaseUrl = baseUrl.endsWith("/v1") ? baseUrl : `${baseUrl}/v1`;

  // Mirrors the config shape from codexClientOptions():
  //   model_provider: "litellm"
  //   model_providers.litellm.base_url = responsesBaseUrl
  //   wire_api: "responses"   (OpenAI Responses API, not chat completions)
  const codexAppConfig = {
    approval_policy:  CODEX_APPROVAL_POLICY,
    model_provider:   "litellm",
    model_providers: {
      litellm: {
        name:                "LiteLLM",
        base_url:            responsesBaseUrl,
        env_key:             "CODEX_API_KEY",
        wire_api:            "responses",
        requires_openai_auth: true,
      },
    },
  };

  // When CODEX_REASONING_EFFORT is set, inject it into the config so thread/start
  // carries the override (path 1 of 3).  Paths 2 and 3 happen in runCodexAppServer.
  if (CODEX_REASONING_EFFORT) {
    codexAppConfig.model_reasoning_effort = CODEX_REASONING_EFFORT;
    process.stderr.write(
      `[run_codex] CODEX_REASONING_EFFORT='${CODEX_REASONING_EFFORT}' ` +
      `propagated via CODEX_APP_CONFIG.model_reasoning_effort\n`,
    );
  }

  // Custom top-level ask_human tool for Codex via MCP.
  // This is additive: native requestUserInput remains available and is routed
  // through the same sidecar backend. Set WITH_CUSTOM_TOOL=0 to hide the MCP tool
  // during diagnostic runs while keeping native requestUserInput interception.
  if (WITH_CUSTOM_TOOL) {
    if (!sidecarUrl) {
      throw new Error("WITH_CUSTOM_TOOL requires a started ask_human sidecar URL");
    }
    codexAppConfig.mcp_servers = {
      human_input: {
        command: process.execPath,
        args: [BRIDGE_SCRIPT],
        env: {
          SIDECAR_URL: sidecarUrl,
          RICH_ASK_TOOL_DESC: process.env.RICH_ASK_TOOL_DESC || "",
        },
      },
    };
  }

  return {
    ...process.env,
    CODEX_API_KEY:          token,
    OPENAI_API_KEY:         token,
    LITELLM_BASE_URL:       baseUrl,
    CODEX_LITELLM_BASE_URL: responsesBaseUrl,
    CODEX_APP_CONFIG:       JSON.stringify(codexAppConfig),
  };
}

const TOKEN_LIMIT_CODES = new Set([
  "contextwindowexceeded",
  "max_output_tokens",
  "max_tokens",
  "token_limit",
  "context_length_exceeded",
  "length",
]);

function _normalizeCode(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_");
}

function isTokenLimitStructured(value, depth = 0) {
  if (depth > 7 || value == null) return false;
  if (typeof value === "string") return TOKEN_LIMIT_CODES.has(_normalizeCode(value));
  if (Array.isArray(value)) return value.some((item) => isTokenLimitStructured(item, depth + 1));
  if (typeof value !== "object") return false;
  for (const [k, v] of Object.entries(value)) {
    const key = String(k || "").toLowerCase();
    if (
      key === "codexerrorinfo" ||
      key === "error" ||
      key === "details" ||
      key === "additionaldetails"
    ) {
      if (isTokenLimitStructured(v, depth + 1)) return true;
    }
    if (
      key === "code" ||
      key === "type" ||
      key === "subtype" ||
      key === "reason" ||
      key === "stop_reason" ||
      key === "stopreason" ||
      key === "finish_reason" ||
      key === "finishreason" ||
      key === "errorcode" ||
      key === "error_code"
    ) {
      if (TOKEN_LIMIT_CODES.has(_normalizeCode(v))) return true;
    }
  }
  return false;
}

function isTokenLimitError(text) {
  const s = String(text || "").toLowerCase();
  if (!s) return false;
  return (
    s.includes("contextwindowexceeded") ||
    s.includes("max_output_tokens") ||
    s.includes("max output token") ||
    s.includes("max_tokens") ||
    s.includes("token limit") ||
    s.includes("context window") ||
    s.includes("context length")
  );
}


// ── JsonRpcProcess ────────────────────────────────────────────────────────────
//
// Adapted from src/harnesses/codex/app_server.mjs.  The key difference: we do
// not log to a .jsonl file here — instead everything goes to pushEvent().

class JsonRpcProcess {
  constructor({ command, args, cwd, env, onRequest, onNotification }) {
    this.nextId      = 1;
    this.pending     = new Map();
    this.buffer      = "";
    this.onRequest      = onRequest;
    this.onNotification = onNotification;

    this.child = spawn(command, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
    this.child.stdout.on("data",   (chunk) => this._onStdout(chunk));
    this.child.stderr.on("data",   (chunk) => {
      process.stderr.write(`[codex-app-server] ${chunk}`);
    });
    this.child.on("exit", (code, signal) => {
      const err = new Error(`codex app-server exited code=${code} signal=${signal}`);
      for (const { reject } of this.pending.values()) reject(err);
      this.pending.clear();
    });
  }

  async _onStdout(chunk) {
    this.buffer += chunk.toString();
    while (true) {
      const idx = this.buffer.indexOf("\n");
      if (idx < 0) break;
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); }
      catch { process.stderr.write(`[codex-app-server non-json] ${line}\n`); continue; }
      await this._handle(msg);
    }
  }

  async _handle(msg) {
    // Incoming request from server (has both id and method)
    if (msg.id !== undefined && msg.method) {
      try {
        const result = await this.onRequest(msg);
        this._write({ jsonrpc: "2.0", id: msg.id, result });
      } catch (err) {
        this._write({ jsonrpc: "2.0", id: msg.id, error: { code: -32000, message: String(err?.message || err) } });
      }
      return;
    }
    // Response to one of our requests
    if (msg.id !== undefined) {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
      else p.resolve(msg.result);
      return;
    }
    // Notification (no id)
    if (msg.method) await this.onNotification(msg);
  }

  request(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this._write({ jsonrpc: "2.0", id, method, params });
    });
  }

  _write(msg) { this.child.stdin.write(`${JSON.stringify(msg)}\n`); }

  close() { try { this.child.kill("SIGTERM"); } catch {} }
}

// ── Question routing ──────────────────────────────────────────────────────────

/**
 * Handle item/tool/requestUserInput — Codex's native question-asking mechanism.
 * Mirrors handleRequestUserInput() in app_server.mjs.
 *
 * ask_human mode: route each question through the shared ask_human sidecar.
 * full_info mode : fill answers with UNKNOWN_RESOLUTION ("irrelevant question") — all info
 *                  is already in the prompt so the agent should not need to ask.
 */
function appendSidecarEvents(result, pushEvent) {
  for (const ev of Array.isArray(result?.events) ? result.events : []) {
    pushEvent?.(ev);
  }
}

async function handleRequestUserInput({ params, sidecarUrl, pushEvent }) {
  const answers = {};
  for (const question of params.questions || []) {
    const prompt = typeof question?.question === "string" ? question.question : "";

    if (sidecarUrl) {
      // ask_human mode: route to the LLM-backed human simulator
      const result = await sidecarAsk({
        sidecarUrl,
        question: prompt,
        requestType:      "clarification",
        nativeEventType:  "codex.item/tool/requestUserInput",
        rawEvent:         question,
        options:          question.options || [],
        context: {
          question_id: question.id,
          isOther:     question.isOther,
          isSecret:    question.isSecret,
        },
        fallbackSource: "codex_native_request_user_input_error_fallback",
      });
      appendSidecarEvents(result, pushEvent);
      const answerText = String(result.resolution ?? UNKNOWN_RESOLUTION);
      answers[question.id] = { answers: [answerText] };
      // Emit a structured event so extractCodexTrajectorySteps can include the
      // ask/answer pair in trajectory.json.  requestUserInput arrives as a JSON-RPC
      // *request* (not a notification), so it never appears as an sdk_event and would
      // otherwise be invisible to the trajectory extractor.
      pushEvent({
        type:        "codex_ask_question",
        timestamp:   new Date().toISOString(),
        question:    prompt,
        answer:      answerText,
        question_id: question.id,
        source:      "native_requestUserInput",
      });
    } else {
      // full_info mode: no human present — respond with canonical "irrelevant question"
      // We still push a structured event so the trajectory extractor can record the
      // attempt (as an ask_human step with obs "irrelevant question") and so the
      // stats counter num_questions_full_info is incremented.
      pushEvent({
        type:      "ask_question_full_info_mode",
        timestamp: new Date().toISOString(),
        question:  prompt,
      });
      answers[question.id] = { answers: [UNKNOWN_RESOLUTION] };
    }
  }
  return { answers };
}

function parseResolutionJson(resolution) {
  try {
    return JSON.parse(resolution);
  } catch {
    return { answer: resolution };
  }
}

async function handleElicitationRequest({ params, sidecarUrl, pushEvent }) {
  // In ask_human mode, route elicitation prompts through the same human router.
  // In full_info mode, still accept with canonical unknown resolution to avoid
  // hard MCP-call rejection loops.
  if (!sidecarUrl) {
    return { action: "accept", content: parseResolutionJson(UNKNOWN_RESOLUTION), _meta: null };
  }

  const result = await sidecarAsk({
    sidecarUrl,
    question: params?.message || params?.url || "MCP elicitation request",
    requestType: "elicitation",
    nativeEventType: "codex.mcpServer/elicitation/request",
    rawEvent: params,
    context: {
      serverName: params?.serverName,
      mode: params?.mode,
      requestedSchema: params?.requestedSchema,
    },
    fallbackSource: "codex_mcp_elicitation_error_fallback",
  });
  appendSidecarEvents(result, pushEvent);
  pushEvent({
    type:      "codex_mcp_elicitation",
    timestamp: new Date().toISOString(),
    prompt:    params?.message || params?.url || "MCP elicitation request",
    answer:    result?.resolution || UNKNOWN_RESOLUTION,
  });
  return {
    action: "accept",
    content: parseResolutionJson(result?.resolution || UNKNOWN_RESOLUTION),
    _meta: null,
  };
}

// ── Trajectory extraction ─────────────────────────────────────────────────────

/** camelCase/PascalCase item type → snake_case */
function snakeCase(t) {
  return String(t || "").replace(/([A-Z])/g, (m) => `_${m.toLowerCase()}`).replace(/^_/, "");
}

function safeJson(value) {
  try { return JSON.stringify(value); } catch { return "[unserializable]"; }
}

function mcpToolName(item) {
  return `${item?.server || ""}.${item?.tool || ""}`.replace(/^\./, "");
}

function readAskQuestionFromValue(value) {
  if (value == null) return "";
  if (typeof value === "string") {
    if (!value.length) return "";
    try {
      return readAskQuestionFromValue(JSON.parse(value));
    } catch {
      return value;
    }
  }
  if (typeof value !== "object") return "";
  if (typeof value.question === "string") return value.question;
  if (value.arguments && typeof value.arguments === "object") {
    const nested = readAskQuestionFromValue(value.arguments);
    if (nested !== "") return nested;
  }
  if (value.input && typeof value.input === "object") {
    const nested = readAskQuestionFromValue(value.input);
    if (nested !== "") return nested;
  }
  if (value.ask_human && typeof value.ask_human === "object") {
    const nested = readAskQuestionFromValue(value.ask_human);
    if (nested !== "") return nested;
  }
  return "";
}

function extractMcpResultText(result) {
  if (result == null) return "";
  if (typeof result === "string") return result;

  if (Array.isArray(result?.content)) {
    const parts = result.content
      .map((c) => {
        if (typeof c === "string") return c;
        if (typeof c?.text === "string") return c.text;
        return null;
      })
      .filter(Boolean);
    if (parts.length) return parts.join("\n");
  }

  if (typeof result?.structuredContent?.resolution === "string") {
    return result.structuredContent.resolution;
  }
  if (typeof result?.resolution === "string") {
    return result.resolution;
  }

  return safeJson(result);
}

function extractMcpResultEvents(result) {
  if (!result || typeof result !== "object") return [];
  const maybeEvents = result?.structuredContent?.events ?? result?.events;
  if (!Array.isArray(maybeEvents)) return [];
  return maybeEvents.filter((ev) => ev && typeof ev === "object" && typeof ev.type === "string");
}

/**
 * Convert raw app-server notifications from allEvents into [{thought, act, obs}, ...] steps.
 *
 * The codex app-server emits JSON-RPC notifications.  Relevant ones:
 *   • item notifications with params.item of type reasoning/agentMessage → thought
 *   • item notifications with params.item of type commandExecution (final, has aggregatedOutput
 *     or exitCode) → {thought, act: command, obs: output}
 *   • item notifications with params.item of type fileChange → {thought, act: "Edit: <paths>", obs: ""}
 *   • item notifications with params.item of type mcpToolCall (completed) → tool step
 *   • turn/completed → flush any remaining thought as standalone text step
 *
 * Streaming delta events (outputDelta, patchUpdated, diff/updated) are skipped —
 * they are intermediate fragments; only the final item state is used.
 *
 * Item IDs are tracked to avoid emitting duplicate steps from update notifications.
 */
function extractCodexTrajectorySteps(events, { stopReason = "", sdkErrorMsg = "" } = {}) {
  const steps = [];
  const emittedItemIds = new Set();
  const pendingByItemId = new Map();
  let currentThought = "";

  const interruptedObs = () => {
    let obs = "[no observation — tool call was interrupted]";
    const sr = String(stopReason || "").trim();
    if (sr) obs += ` (stop_reason=${sr})`;
    const firstLine = String(sdkErrorMsg || "").trim().split("\n")[0] || "";
    if (firstLine) obs += ` (${cap(firstLine, 300)})`;
    return obs;
  };

  for (const ev of events) {
    // ── Ask/answer pairs from requestUserInput handling ───────────────────────
    // requestUserInput arrives as a JSON-RPC *request* (not a notification), so it
    // is never wrapped in sdk_event.  The codex_ask_question event is pushed by
    // handleRequestUserInput after the LLM judge returns the answer.
    if (ev.type === "codex_ask_question") {
      steps.push({
        thought: currentThought,
        act:     cap(`ask_human [native] ${ev.question}`, ACT_CAP),
        obs:     cap(String(ev.answer ?? ""), OBS_CAP),
      });
      currentThought = "";
      continue;
    }

    if (ev.type === "codex_mcp_elicitation") {
      steps.push({
        thought: currentThought,
        act:     cap(`mcp_elicitation ${ev.prompt || ""}`, ACT_CAP),
        obs:     cap(String(ev.answer ?? ""), OBS_CAP),
      });
      currentThought = "";
      continue;
    }

    // ── full_info mode questions ──────────────────────────────────────────────
    // In full_info mode the agent may still call requestUserInput; the handler
    // denies it with UNKNOWN_RESOLUTION and pushes this event so we capture the
    // attempt in the trajectory (act: "ask_human …", obs: "irrelevant question").
    if (ev.type === "ask_question_full_info_mode") {
      steps.push({
        thought: currentThought,
        act:     cap(`ask_human [native] ${ev.question || ""}`, ACT_CAP),
        obs:     UNKNOWN_RESOLUTION,
      });
      currentThought = "";
      continue;
    }

    if (ev.type !== "sdk_event") continue;
    const notif = ev.event;
    if (!notif?.method) continue;

    const method = notif.method;
    const params = notif.params || {};

    // ── Skip intermediate streaming events ───────────────────────────────────
    if (
      method === "item/commandExecution/outputDelta"  ||
      method === "item/fileChange/outputDelta"        ||
      method === "item/fileChange/patchUpdated"       ||
      method === "turn/diff/updated"
    ) continue;

    // ── Turn completed — flush any trailing thought ──────────────────────────
    if (method === "turn/completed") {
      if (currentThought) {
        steps.push({ thought: currentThought, act: "", obs: "" });
        currentThought = "";
      }
      continue;
    }

    // ── Error notifications — skip (error is captured at SDK level) ──────────
    if (method === "error" || method === "turn/error") continue;

    // ── Item notifications ────────────────────────────────────────────────────
    const item = params.item;
    if (!item) continue;

    const itemType = snakeCase(item.type);
    const itemId   = item.id || null;

    // Reasoning / agent message — accumulate as thought
    if (itemType === "reasoning" || itemType === "agent_message") {
      const text = item.text || "";
      if (text) currentThought = cap(text, THOUGHT_CAP);
      continue;
    }

    // User-side messages — not agent actions
    if (itemType === "user_message") continue;

    // Command execution — only emit when the command has actually finished.
    //
    // The app-server emits two events per command:
    //   item/started  — exitCode: null, aggregatedOutput: null  (in-progress)
    //   item/completed — exitCode: <N>, aggregatedOutput: "<output>"  (done)
    //
    // We use `!= null` (not `!== undefined`) so that null exit codes are
    // treated the same as missing ones — both mean the command is still running.
    // Wire format uses camelCase (exitCode, aggregatedOutput) regardless of
    // what the SDK TypeScript types say.
    if (itemType === "command_execution") {
      // eslint-disable-next-line eqeqeq
      const done = item.exitCode != null || item.exit_code != null;
      if (!done) {
        if (itemId && !pendingByItemId.has(itemId)) {
          pendingByItemId.set(itemId, {
            thought: currentThought,
            act: cap(item.command || "", ACT_CAP),
          });
          currentThought = "";
        }
        continue;
      }
      if (itemId && emittedItemIds.has(itemId)) continue;
      if (itemId) emittedItemIds.add(itemId);
      if (itemId) pendingByItemId.delete(itemId);

      const output = item.aggregatedOutput ?? item.aggregated_output ?? "";
      steps.push({
        thought: currentThought,
        act:     cap(item.command || "", ACT_CAP),
        obs:     cap(String(output), OBS_CAP),
      });
      currentThought = "";
      continue;
    }

    // File change
    if (itemType === "file_change") {
      const paths = (Array.isArray(item.changes) ? item.changes : [])
        .map((c) => c.path)
        .filter(Boolean);
      if (!paths.length) continue;
      if (itemId && emittedItemIds.has(itemId)) continue;
      if (itemId) emittedItemIds.add(itemId);

      steps.push({
        thought: currentThought,
        act:     cap(`Edit: ${paths.join(", ")}`, ACT_CAP),
        obs:     "",
      });
      currentThought = "";
      continue;
    }

    // MCP tool call — emit when completed or failed
    if (itemType === "mcp_tool_call") {
      const done = item.status === "completed" || item.status === "failed";
      if (!done) {
        if (itemId && !pendingByItemId.has(itemId)) {
          const toolName = mcpToolName(item);
          let act = `${toolName}: ${safeJson(item.arguments || {})}`;
          if (toolName === "human_input.ask_human") {
            const q = readAskQuestionFromValue(item?.arguments);
            act = `ask_human [custom_tool] ${q}`;
          }
          pendingByItemId.set(itemId, { thought: currentThought, act: cap(act, ACT_CAP) });
          currentThought = "";
        }
        continue;
      }
      if (itemId && emittedItemIds.has(itemId)) continue;
      if (itemId) emittedItemIds.add(itemId);
      if (itemId) pendingByItemId.delete(itemId);

      const toolName = mcpToolName(item);
      let act = `${toolName}: ${safeJson(item.arguments || {})}`;
      if (toolName === "human_input.ask_human") {
        const q = readAskQuestionFromValue(item?.arguments);
        act = `ask_human [custom_tool] ${q}`;
      }
      steps.push({
        thought: currentThought,
        act:     cap(act, ACT_CAP),
        obs:     cap(extractMcpResultText(item.result ?? item.error), OBS_CAP),
      });
      currentThought = "";
      continue;
    }
  }

  for (const pending of pendingByItemId.values()) {
    steps.push({
      thought: pending.thought || "",
      act: pending.act || "",
      obs: interruptedObs(),
    });
  }

  return steps.map((step) => {
    if (!step || typeof step !== "object") return step;
    const act = String(step.act || "");
    if (!act.trim().startsWith("ask_human")) return step;
    const obs = String(step.obs ?? "").trim();
    if (!obs || obs.startsWith("[no observation") || obs.startsWith("[error]")) {
      return { ...step, obs: CANT_ANSWER };
    }
    return step;
  });
}

/**
 * Compute per-run stats — identical to computeTrajectoryStats in run_claude.mjs.
 * Counts are derived from the same human_input_raw_event / human_input_result
 * events that the router emits into allEvents via the pushEvent callback.
 *
 * num_questions_full_info  — questions asked in full_info mode (ask_question_full_info_mode
 *                            events).  These are tracked even though the agent receives
 *                            "irrelevant question" because it is analytically useful to know
 *                            how often agents ask despite having all info in the prompt.
 */
function computeTrajectoryStats(events, trajectorySteps, numBlockersTotal) {
  let numQuestions            = 0;
  let numQuestionsApproval    = 0;
  let numQuestionsFullInfo    = 0;
  let numQuestionsElicitation = 0;
  const resolvedBlockerIds    = new Set();
  let numAskHumanCapped       = 0;
  let numAskHumanCooldownDenied = 0;
  const seenRawEventIds       = new Set();
  const seenResultEventIds    = new Set();
  const requestMetaById       = new Map();
  const requestStatusById     = new Map();

  // Build request_id → status map first so failed ask_human bridge/tool calls
  // (status="error") can be excluded from question counters.
  for (const ev of events) {
    if (ev.type !== "human_input_result") continue;
    const rid = String(ev.request_id || "");
    if (!rid) continue;
    const status = String(ev.result?.status || "unknown").toLowerCase();
    requestStatusById.set(rid, status);
  }

  // Count native tool usage directly from synthesized events: one event = one usage.
  for (const ev of events) {
    if (ev.type === "codex_ask_question") numQuestions++;
    if (ev.type === "ask_question_full_info_mode") numQuestionsFullInfo++;
    if (ev.type === "ask_human_suppressed") {
      if (ev.reason === "cooldown") numAskHumanCooldownDenied += 1;
      else if (ev.reason === "cap") numAskHumanCapped += 1;
    }
  }

  for (const ev of events) {
    if (ev.type === "human_input_raw_event") {
      const rid = String(ev.request_id || "");
      if (rid && seenRawEventIds.has(rid)) continue;
      if (rid) seenRawEventIds.add(rid);
      const status = rid ? requestStatusById.get(rid) : null;
      if (status === "error") continue;
      if (rid) {
        requestMetaById.set(rid, {
          request_type: ev.request_type,
          native_event_type: ev.native_event_type,
        });
      }

      // Count custom MCP ask_human usage separately from native ask_human.
      if (ev.native_event_type === "codex.mcp.ask_human") {
        numQuestions++;
      } else if (ev.native_event_type === "codex.mcpServer/elicitation/request") {
        // Protocol-level MCP gating prompt, tracked separately from clarification asks.
        numQuestionsElicitation++;
      } else if (APPROVAL_REQUEST_TYPES.has(ev.request_type)) {
        numQuestionsApproval++;
      }
    }

    if (ev.type === "human_input_result") {
      const rid = String(ev.request_id || "");
      if (rid && seenResultEventIds.has(rid)) continue;
      if (rid) seenResultEventIds.add(rid);

      const meta = rid ? requestMetaById.get(rid) : null;
      const isElicitation = meta?.native_event_type === "codex.mcpServer/elicitation/request";
      if (isElicitation) continue;

      const bid = ev.result?.blocker_id;
      if (bid && bid !== UNKNOWN_BLOCKER_ID && ev.result?.status === "answered") {
        resolvedBlockerIds.add(bid);
      }
    }
  }

  return {
    num_steps:                trajectorySteps.length,
    num_questions:            numQuestions,
    num_questions_approval:   numQuestionsApproval,
    num_total_questions:      numQuestions + numQuestionsApproval,
    num_questions_elicitation:numQuestionsElicitation,
    num_questions_full_info:  numQuestionsFullInfo,
    num_ask_human_capped:     numAskHumanCapped,
    num_ask_human_cooldown_denied: numAskHumanCooldownDenied,
    num_blockers_resolved:    resolvedBlockerIds.size,
    num_blockers_total:       numBlockersTotal,
    stats_schema_version:     2,
  };
}

// ── Codex app-server runner ───────────────────────────────────────────────────

/**
 * Start the codex app-server, run one turn with the given prompt, and resolve
 * when turn/completed fires (or reject on error / timeout abort).
 *
 * All JSON-RPC notifications are pushed to allEvents as { type: "sdk_event", event }.
 * Server requests (user input, approvals) are pushed as { type: "codex_server_request", event }
 * and responded to immediately.
 */
async function runCodexAppServer({ prompt, env, uid, sidecarUrl, pushEvent, abortController }) {
  // Isolate codex state to avoid polluting /root in the container
  // Keep CODEX_HOME out of /tmp. Codex warns when helper binaries would be
  // created under temporary directories ("/tmp"), which is noisy and can
  // disable PATH helper setup. Per-attempt /opt paths remain isolated while
  // avoiding the temporary-dir restriction.
  const runBase   = path.join("/opt", `codex-${uid.slice(0, 12)}-p${PASS_INDEX}`);
  const codexHome = path.join(runBase, "codex-home");
  const homeDir   = path.join(runBase, "home");
  await ensureDir(codexHome);
  await ensureDir(homeDir);

  const serverEnv = { ...env, CODEX_HOME: codexHome, HOME: homeDir };

  return new Promise((resolve, reject) => {
    let threadId    = null;
    let currentTurnId = null;
    let itemsDone   = 0;   // completed items (commands + edits + tool calls) in this turn
    let settled     = false;
    const injectedMcpEventItemIds = new Set();

    const settle = (err) => {
      if (settled) return;
      settled = true;
      abortController?.signal.removeEventListener("abort", abortHandler);
      rpc.close();
      if (err) reject(err);
      else resolve();
    };

    const abortHandler = () => settle(new Error("Codex SWE attempt aborted by timeout"));
    abortController?.signal.addEventListener("abort", abortHandler, { once: true });

    // Belt-and-suspenders: also pass model_reasoning_effort as a `-c` flag at
    // app-server spawn time (path 2 of 3).  This overrides ~/.codex/config.toml
    // and any built-in defaults baked into the codex CLI.
    const appServerArgs = [
      "app-server",
      "--enable", "default_mode_request_user_input",
    ];
    if (CODEX_REASONING_EFFORT) {
      appServerArgs.push("-c", `model_reasoning_effort="${CODEX_REASONING_EFFORT}"`);
      process.stderr.write(
        `[run_codex] CODEX_REASONING_EFFORT='${CODEX_REASONING_EFFORT}' ` +
        `propagated via app-server -c flag\n`,
      );
    }
    appServerArgs.push("--listen", "stdio://");

    const rpc = new JsonRpcProcess({
      command: CODEX_BIN,
      args:    appServerArgs,
      cwd:     WORKSPACE,
      env:     serverEnv,

      onRequest: async (msg) => {
        const { method, params = {} } = msg;
        // Log server-originated requests separately from notifications
        pushEvent({ type: "codex_server_request", timestamp: new Date().toISOString(), event: msg });

        // ── Native question-asking ─────────────────────────────────────────
        if (method === "item/tool/requestUserInput") {
          return handleRequestUserInput({ params, sidecarUrl, pushEvent });
        }

        // ── Approval requests — auto-approve (container is security boundary) ──
        if (method === "item/commandExecution/requestApproval" ||
            method === "item/fileChange/requestApproval") {
          return { decision: "accept" };
        }

        if (method === "item/permissions/requestApproval") {
          // Grant all permissions that were requested
          const perms = {};
          if (params.permissions?.network)    perms.network    = params.permissions.network;
          if (params.permissions?.fileSystem) perms.fileSystem = params.permissions.fileSystem;
          return { permissions: perms, scope: "turn", strictAutoReview: false };
        }

        // MCP and other approval-style requests can appear under additional
        // method names (for example, tool-specific requestApproval paths).
        // Inside this Docker harness, the container is the security boundary,
        // so all such approvals should be accepted.
        if (String(method).endsWith("/requestApproval")) {
          return { decision: "accept" };
        }

        // Legacy approval methods
        if (method === "execCommandApproval" || method === "applyPatchApproval") {
          return { decision: "approved" };
        }

        // MCP elicitation prompts are unsupported in this harness path.
        if (method === "mcpServer/elicitation/request") {
          return handleElicitationRequest({ params, sidecarUrl, pushEvent });
        }

        // Defensive MCP fallback: if Codex introduces additional mcpServer/*
        // request methods, do not silently reject by returning {}. Log the
        // method and return a permissive, schema-compatible acceptance payload.
        if (String(method).startsWith("mcpServer/")) {
          process.stderr.write(
            `[run_codex] WARN: unhandled MCP server request method '${method}', ` +
            `using permissive fallback response\n`,
          );
          return {
            action: "accept",
            content: parseResolutionJson(UNKNOWN_RESOLUTION),
            _meta: null,
          };
        }

        return {};
      },

      onNotification: async (msg) => {
        // All notifications go into allEvents for trajectory extraction and stats
        pushEvent({ type: "sdk_event", timestamp: new Date().toISOString(), event: msg });

        // For custom MCP ask_human calls, inject sidecar-emitted human_input_* events
        // inline from the tool result payload so stats don't depend on out-of-band fetches.
        if (msg.method === "item/completed") {
          const item = msg.params?.item;
          if (snakeCase(item?.type) === "mcp_tool_call") {
            const itemId = String(item?.id || "");
            if (!itemId || !injectedMcpEventItemIds.has(itemId)) {
              const toolName = mcpToolName(item);
              if (toolName === "human_input.ask_human") {
                const embeddedEvents = extractMcpResultEvents(item?.result);
                for (const ev of embeddedEvents) pushEvent(ev);
              }
              if (itemId) injectedMcpEventItemIds.add(itemId);
            }
          }
        }

        // Track the active turn ID so we can interrupt it if needed
        if (msg.method === "turn/started" &&
            (!threadId || msg.params?.threadId === threadId)) {
          currentTurnId = msg.params?.turn?.id ?? null;
          itemsDone = 0;
        }

        // Count completed items (commands + file edits + MCP tool calls).
        // When MAX_STEPS is set and the threshold is reached, interrupt the turn.
        // This is the codex equivalent of Claude SDK's maxTurns — the app-server
        // protocol has no native turn/step limit parameter.
        if (msg.method === "item/completed" &&
            MAX_STEPS > 0 &&
            !settled &&
            currentTurnId &&
            threadId) {
          itemsDone++;
          if (itemsDone >= MAX_STEPS) {
            pushEvent({
              type: "max_steps_reached",
              timestamp: new Date().toISOString(),
              items_done: itemsDone,
              max_steps: MAX_STEPS,
            });
            // Fire-and-forget: don't await so we don't block the notification handler
            rpc.request("turn/interrupt", { threadId, turnId: currentTurnId }).catch(() => {});
          }
        }

        if (msg.method === "turn/completed" &&
            (!threadId || msg.params?.threadId === threadId)) {
          settle(null);
        }

        if ((msg.method === "error" || msg.method === "turn/error") &&
            !msg.params?.willRetry) {
          const err = new Error(`Codex app-server error: ${JSON.stringify(msg.params)}`);
          if (isTokenLimitStructured(msg.params)) err.__tokenLimit = true;
          settle(err);
        }
      },
    });

    // ── Protocol handshake + turn start (async, errors bubble to settle) ──
    (async () => {
      try {
        await rpc.request("initialize", {
          clientInfo:   { name: "trust-horizon-hil-swe", title: "Trust Horizon HiL-SWE", version: "0.1.0" },
          capabilities: { experimentalApi: true },
        });

        // Path 3 of 3: verify the app-server resolved our reasoning effort
        // override by reading back the effective config.  This is a runtime
        // assertion so a silent drop of the setting fails the attempt instead
        // of silently running with the wrong effort.
        if (CODEX_REASONING_EFFORT) {
          try {
            const cfg = await rpc.request("config/read", {
              includeLayers: false,
              cwd: WORKSPACE,
            });
            const resolved = cfg?.config?.model_reasoning_effort;
            const evt = {
              type:      "codex_reasoning_effort_verification",
              timestamp: new Date().toISOString(),
              expected:  CODEX_REASONING_EFFORT,
              resolved,
              ok:        resolved === CODEX_REASONING_EFFORT,
            };
            pushEvent(evt);
            process.stderr.write(
              `[run_codex] config/read: model_reasoning_effort=${resolved} ` +
              `(expected=${CODEX_REASONING_EFFORT}, ok=${evt.ok})\n`,
            );
            if (resolved !== CODEX_REASONING_EFFORT) {
              throw new Error(
                `CODEX_REASONING_EFFORT propagation failed: ` +
                `expected '${CODEX_REASONING_EFFORT}', resolved '${resolved}'`,
              );
            }
          } catch (cfgErr) {
            // If config/read isn't supported on this codex version, fall back
            // to logging and continue — the -c flag + thread/start.config still
            // apply.  Only fail-hard on an explicit mismatch above.
            if (!String(cfgErr.message || cfgErr).includes("propagation failed")) {
              process.stderr.write(
                `[run_codex] WARN: config/read failed (${cfgErr.message || cfgErr}); ` +
                `continuing with -c flag + thread/start.config propagation only\n`,
              );
            } else {
              throw cfgErr;
            }
          }
        }

        const threadStart = await rpc.request("thread/start", {
          cwd:                  WORKSPACE,
          model:                CODEX_MODEL,
          modelProvider:        "litellm",
          approvalPolicy:       CODEX_APPROVAL_POLICY,
          approvalsReviewer:    "user",
          sandbox:              "workspace-write",
          sandboxPolicy: {
            type:                "workspaceWrite",
            writableRoots:       [WORKSPACE],
            networkAccess:       true,
            excludeTmpdirEnvVar: false,
            excludeSlashTmp:     false,
          },
          config:               env.CODEX_APP_CONFIG ? JSON.parse(env.CODEX_APP_CONFIG) : undefined,
          ephemeral:            true,
          ...(ASK_HUMAN_ENABLED && ASK_HUMAN_GUIDANCE_ENABLED ? { developerInstructions: ASK_HUMAN_GUIDANCE } : {}),
        });

        threadId = threadStart?.thread?.id;
        if (!threadId)
          throw new Error(`Codex app-server did not return a thread id: ${JSON.stringify(threadStart)}`);

        await rpc.request("turn/start", {
          threadId,
          input: [{ type: "text", text: prompt, text_elements: [] }],
          cwd:              WORKSPACE,
          approvalPolicy:   CODEX_APPROVAL_POLICY,
          approvalsReviewer:"user",
          sandboxPolicy: {
            type:                "workspaceWrite",
            writableRoots:       [WORKSPACE],
            networkAccess:       true,
            excludeTmpdirEnvVar: false,
            excludeSlashTmp:     false,
          },
          model: CODEX_MODEL,
        });
      } catch (err) {
        settle(err);
      }
    })();
  });
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  await ensureDir(OUTPUT_DIR);

  // 1. Read task data
  const metadata         = JSON.parse(await fs.readFile(path.join(TASK_DIR, "metadata.json"), "utf8"));
  const problemStatement = await fs.readFile(path.join(TASK_DIR, "problem_statement.txt"), "utf8");
  const uid = metadata.uid || metadata.instance_id;

  // In-memory event log — same pattern as run_claude.mjs
  const allEvents = [];
  const pushEvent = (ev) => allEvents.push(ev);
  const runStartedAtMs = Date.now();

  // 2. Build prompt
  let blockers = [];
  if (FULL_INFO_ENABLED) {
    const registry = JSON.parse(await fs.readFile(path.join(TASK_DIR, "blocker_registry.json"), "utf8"));
    blockers = (registry.entries || registry.blockers || []).map((e) => ({
      description: e.description,
      resolution:  e.resolution,
    }));
  }
  const prompt = buildSwePrompt({ problemStatement, mode: MODE, blockers });
  await removeInstalledAskHumanSkills(WORKSPACE);
  if (SKILL_ENABLED) {
    await installAgentsSkill(WORKSPACE, SKILL_TOOL_REF.codex);
  }

  // 3. Write attempt metadata
  await writeJson(path.join(OUTPUT_DIR, "attempt.json"), {
    run_id:     RUN_ID,
    uid,
    mode:       MODE,
    pass_index: PASS_INDEX,
    harness:    "codex",
    model:      CODEX_MODEL,
    max_steps:  MAX_STEPS > 0 ? MAX_STEPS : null,  // null = no limit (timeout only)
    timeout_ms: TIMEOUT_MS,
    workspace:  WORKSPACE,
    task_dir:   TASK_DIR,
    output_dir: OUTPUT_DIR,
    started_at: new Date().toISOString(),
    prompt,
    with_custom_tool: WITH_CUSTOM_TOOL,
    ask_human_tool_enabled: ASK_HUMAN_ENABLED,
    skill_enabled: SKILL_ENABLED,
    guidance_enabled: ASK_HUMAN_GUIDANCE_ENABLED,
    with_skill: SKILL_TEMPLATE_VERSION || "",
    with_ask_guidance: ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION || "",
    ask_human_model: ASK_HUMAN_MODEL,
    ask_human_backend: ASK_HUMAN_ENABLED ? "ask_human_sidecar" : null,
    native_question_routing: ASK_HUMAN_ENABLED ? "requestUserInput -> ask_human_sidecar" : "requestUserInput -> irrelevant_question",
  });
  pushEvent({ type: "attempt_start", timestamp: new Date().toISOString(), uid, mode: MODE, pass_index: PASS_INDEX, prompt });

  // 5. Run agent with up to 3 attempts
  // Retries occur only on transient SDK errors; timeouts and clean completions
  // exit immediately. allEvents is cleared in-place between attempts so only
  // the successful attempt's events are preserved in the final trajectory. Each
  // attempt gets its own sidecar so native requestUserInput and explicit MCP
  // ask_human calls share the same backend path without stale event leakage.
  let sdkError = null;
  let stopReason = "complete";
  const MAX_RETRIES = STEP_LITELLM_TRIES;
  const _runStart = Date.now();

  for (let _attempt = 1; _attempt <= MAX_RETRIES; _attempt++) {
    sdkError = null;
    stopReason = "complete";
    allEvents.length = 0;   // clear in-place so pushEvent closure remains valid

    const PER_ATTEMPT_TIMEOUT_MS = LITELLM_CALL_TIMEOUT_MS;
    const remainingMs    = TIMEOUT_MS - (Date.now() - _runStart);
    const attemptTimeout = Math.min(remainingMs, PER_ATTEMPT_TIMEOUT_MS);
    if (attemptTimeout <= 0) {
      sdkError = `Timed out after ${TIMEOUT_MS}ms`;
      stopReason = "timeout";
      pushEvent({ type: "sdk_error", timestamp: new Date().toISOString(), error: sdkError });
      break;
    }

    const abortController = new AbortController();
    const timeoutId = setTimeout(
      () => abortController.abort(new Error(`Codex SWE attempt timed out after ${PER_ATTEMPT_TIMEOUT_MS}ms`)),
      attemptTimeout,
    );
    let sidecarProc = null;
    let sidecarUrl = "";

    try {
      if (ASK_HUMAN_ENABLED) {
        ({ proc: sidecarProc, url: sidecarUrl } = await startAskHumanSidecar({
          uid,
          mode: MODE,
          taskDir: TASK_DIR,
          workspace: WORKSPACE,
          askHumanBaseUrl: ASK_HUMAN_BASE_URL,
          askHumanModel: ASK_HUMAN_MODEL,
        }));
      }
      const env = codexApiEnv({ sidecarUrl });
      await runCodexAppServer({ prompt, env, uid, sidecarUrl, pushEvent, abortController });
    } catch (err) {
      const text = redactString(String(err?.stack || err));
      if (abortController.signal.aborted) {
        sdkError = `Timed out after ${PER_ATTEMPT_TIMEOUT_MS}ms.\n\n${text}`;
        stopReason = "timeout";
        pushEvent({ type: "sdk_error", timestamp: new Date().toISOString(), error: sdkError });
      } else if (err?.__tokenLimit || isTokenLimitStructured(err) || isTokenLimitError(text)) {
        sdkError = null;
        stopReason = "token_limit";
        pushEvent({ type: "token_limit_reached", timestamp: new Date().toISOString(), detail: text });
      } else {
        sdkError = text;
        stopReason = "sdk_error";
        pushEvent({ type: "sdk_error", timestamp: new Date().toISOString(), error: sdkError });
      }
    } finally {
      // Collect custom-tool ask_human events from the sidecar so stats/trajectory
      // use the same human_input_* event stream as other harnesses.
      if (sidecarUrl) {
        try {
          const resp = await httpGetJson(`${sidecarUrl}/events`);
          if (Array.isArray(resp?.events)) {
            for (const ev of resp.events) pushEvent(ev);
          }
        } catch (err) {
          process.stderr.write(`[run_codex] WARNING: failed to fetch sidecar events: ${err}\n`);
        }
      }
      stopSidecar(sidecarProc);
      clearTimeout(timeoutId);
    }

    // Retry transient failures, including timeout-aborted turns, up to STEP_LITELLM_TRIES.
    if (sdkError && _attempt < MAX_RETRIES) {
      process.stderr.write(
        `[run_codex] sdk_error on attempt ${_attempt}/${MAX_RETRIES} for ${uid}, retrying: ${sdkError.slice(0, 200)}\n`,
      );
      continue;
    }
    break;
  }

  // 6. Collect patch
  const patch = await gitDiff(WORKSPACE);
  await writeText(path.join(OUTPUT_DIR, "patch.diff"), patch);

  // 7. Post-process: extract trajectory + compute stats
  pushEvent({ type: "attempt_end", timestamp: new Date().toISOString(), uid, patch_bytes: Buffer.byteLength(patch), sdk_error: sdkError || null });
  const trajectorySteps = extractCodexTrajectorySteps(allEvents, {
    stopReason,
    sdkErrorMsg: sdkError || "",
  });

  let numBlockersTotal = 0;
  try {
    const reg = JSON.parse(await fs.readFile(path.join(TASK_DIR, "blocker_registry.json"), "utf8"));
    numBlockersTotal = (reg.entries || reg.blockers || []).length;
  } catch { /* non-fatal */ }

  const stats = {
    ...computeTrajectoryStats(allEvents, trajectorySteps, numBlockersTotal),
    ...computeResourceStats(allEvents, trajectorySteps, runStartedAtMs),
  };
  await writeJson(path.join(OUTPUT_DIR, "trajectory.json"), trajectorySteps);
  await writeJson(path.join(OUTPUT_DIR, "stats.json"), stats);

  // 8. Write result
  await writeJson(path.join(OUTPUT_DIR, "result.json"), {
    uid,
    run_id:      RUN_ID,
    mode:        MODE,
    pass_index:  PASS_INDEX,
    harness:     "codex",
    model:       CODEX_MODEL,
    stop_reason: stopReason,
    sdk_error:   sdkError || null,
    patch_bytes: Buffer.byteLength(patch),
    ended_at:    new Date().toISOString(),
  });

  if (sdkError) {
    process.stderr.write(`[run_codex] SDK error for ${uid}: ${sdkError}\n`);
    process.exit(1);
  }

  process.stdout.write(`[run_codex] Done. patch_bytes=${Buffer.byteLength(patch)} uid=${uid} mode=${MODE} pass=${PASS_INDEX}\n`);
}

main().catch((err) => {
  process.stderr.write(`[run_codex] Fatal: ${err?.stack || err}\n`);
  process.exit(2);
});
