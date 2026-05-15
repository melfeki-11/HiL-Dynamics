#!/usr/bin/env node
/**
 * SWE harness entrypoint for codex — runs INSIDE a hilbench-swe-harness-codex:<uid> container.
 *
 * Uses the codex app-server (JSON-RPC, stdio transport) so that native question-asking
 * (item/tool/requestUserInput) can be intercepted and routed through the same
 * LLM-backed human simulator used by run_claude.mjs.  This means:
 *
 *   ask_human mode : item/tool/requestUserInput → createHumanInputRouter → LLM judge
 *                    (identical to claude-code AskUserQuestion routing)
 *   full_info mode : item/tool/requestUserInput → UNKNOWN_RESOLUTION ("irrelevant question")
 *                    (identical to claude-code full_info behaviour)
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
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  createHumanInputRouter,
  UNKNOWN_RESOLUTION,
  UNKNOWN_BLOCKER_ID,
  ASK_HUMAN_REQUEST_TYPES,
  APPROVAL_REQUEST_TYPES,
} from "../shared/human_input.mjs";
import { ensureDir, writeJson, writeText } from "../shared/io.mjs";
import { redactString } from "../shared/redact.mjs";
import { buildSwePrompt } from "./prompt.mjs";
import { installAgentsSkill, SKILL_TOOL_REF } from "./skills.mjs";
import {
  WORKSPACE, TASK_DIR, OUTPUT_DIR,
  MODE, PASS_INDEX, RUN_ID, TIMEOUT_MS,
  LITELLM_CALL_TIMEOUT_MS, STEP_LITELLM_TRIES,
  ASK_HUMAN_BASE_URL, ASK_HUMAN_MODEL, buildAskHumanGuidance,
  THOUGHT_CAP, ACT_CAP, OBS_CAP, cap, gitDiff,
} from "./constants.mjs";

// Codex's native question-asking tool is requestUserInput
const ASK_HUMAN_GUIDANCE = buildAskHumanGuidance("requestUserInput");

// ── Configuration from env ──────────────────────────────────────────────────

const CODEX_MODEL = process.env.CODEX_MODEL || "gpt-5.5";
const CODEX_BIN   = process.env.CODEX_CODE_EXECUTABLE || "codex";
const WITH_CUSTOM_TOOL = /^(1|true|yes|on)$/i.test(String(process.env.WITH_CUSTOM_TOOL || ""));
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
// MAX_TURNS: max completed items (commands + file edits + tool calls) before we interrupt
// the turn via turn/interrupt.  0 or unset = no limit (wall-clock TIMEOUT_MS still applies).
// The codex app-server has no native turn limit param, so we implement it by counting
// ItemCompletedNotification events and calling turn/interrupt when the threshold is reached.
const MAX_TURNS   = Number(process.env.MAX_TURNS || "0");
const __dirname   = path.dirname(fileURLToPath(import.meta.url));
const SIDECAR_SCRIPT = path.join(__dirname, "ask_human_sidecar.mjs");
const BRIDGE_SCRIPT  = path.join(__dirname, "ask_human_mcp_bridge.mjs");

// ── Sidecar helpers (for optional custom MCP ask_human tool) ────────────────

function httpGetJson(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let data = "";
      res.on("data", (chunk) => { data += chunk; });
      res.on("end", () => {
        try { resolve(JSON.parse(data || "{}")); }
        catch (err) { reject(err); }
      });
    });
    req.on("error", reject);
    req.setTimeout(5000, () => req.destroy(new Error("GET timeout")));
  });
}

async function startSidecar(uid) {
  const env = {
    ...process.env,
    MODE,
    TASK_DIR,
    TASK_UID: uid,
    ASK_HUMAN_BASE_URL: ASK_HUMAN_BASE_URL || "",
    ASK_HUMAN_MODEL: ASK_HUMAN_MODEL || "",
  };
  const proc = spawn("node", [SIDECAR_SCRIPT], {
    env,
    cwd: WORKSPACE,
    stdio: ["ignore", "pipe", "pipe"],
  });

  proc.stderr.on("data", (chunk) => {
    process.stderr.write(`[ask_human_sidecar] ${String(chunk)}`);
  });

  let buf = "";
  const portLine = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      reject(new Error(`sidecar startup timeout after 20000ms. output=${buf}`));
    }, 20_000);

    const onData = (chunk) => {
      buf += String(chunk);
      const idx = buf.indexOf("\n");
      if (idx < 0) return;
      const line = buf.slice(0, idx).trim();
      clearTimeout(timeout);
      proc.stdout.off("data", onData);
      resolve(line);
    };

    proc.stdout.on("data", onData);
    proc.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`sidecar exited with code ${code} before announcing port. buf=${buf}`));
    });
    proc.once("error", (err) => {
      clearTimeout(timeout);
      reject(new Error(`sidecar spawn error: ${err.message}`));
    });
  });

  const m = /^SIDECAR_PORT=(\d+)$/.exec(String(portLine || ""));
  if (!m) throw new Error(`sidecar did not announce port. got=${portLine}`);
  const port = Number(m[1]);
  const url = `http://127.0.0.1:${port}`;

  for (let i = 0; i < 20; i++) {
    try {
      const health = await httpGetJson(`${url}/health`);
      if (health?.ok) return { proc, url };
    } catch {
      // keep polling
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("sidecar health check failed after 20 attempts");
}

function stopSidecar(proc) {
  if (!proc) return;
  try { proc.kill("SIGTERM"); } catch {}
}

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
    approval_policy:  "on-request",
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

  // Optional custom top-level ask_human tool for codex via MCP.
  // This is additive: native requestUserInput remains available.
  if (WITH_CUSTOM_TOOL) {
    if (!sidecarUrl) {
      throw new Error("WITH_CUSTOM_TOOL requires a started ask_human sidecar URL");
    }
    codexAppConfig.mcp_servers = {
      human_input: {
        command: process.execPath,
        args: [BRIDGE_SCRIPT],
        env: { SIDECAR_URL: sidecarUrl },
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
 * ask_human mode : route each question through the LLM judge (createHumanInputRouter).
 * full_info mode : deny with UNKNOWN_RESOLUTION ("irrelevant question") — all info
 *                  is already in the prompt so the agent should not need to ask.
 */
async function handleRequestUserInput({ params, router, pushEvent }) {
  const answers = {};
  for (const question of params.questions || []) {
    const prompt = question.question || "Clarification request";

    if (router) {
      // ask_human mode: route to the LLM-backed human simulator
      const result = await router.route({
        requestType:      "clarification",
        nativeEventType:  "codex.item/tool/requestUserInput",
        rawEvent:         question,
        question:         prompt,
        options:          question.options || [],
        context: {
          question_id: question.id,
          isOther:     question.isOther,
          isSecret:    question.isSecret,
        },
      });
      const selected = result.selected_labels?.length
        ? result.selected_labels
        : [result.resolution || UNKNOWN_RESOLUTION];
      answers[question.id] = { answers: selected };
      // Emit a structured event so extractCodexTrajectorySteps can include the
      // ask/answer pair in trajectory.json.  requestUserInput arrives as a JSON-RPC
      // *request* (not a notification), so it never appears as an sdk_event and would
      // otherwise be invisible to the trajectory extractor.
      pushEvent({
        type:        "codex_ask_question",
        timestamp:   new Date().toISOString(),
        question:    prompt,
        answer:      selected.join("; "),
        question_id: question.id,
      });
    } else {
      // full_info mode: no human present — deny with canonical "irrelevant question".
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

// ── Trajectory extraction ─────────────────────────────────────────────────────

/** camelCase/PascalCase item type → snake_case */
function snakeCase(t) {
  return String(t || "").replace(/([A-Z])/g, (m) => `_${m.toLowerCase()}`).replace(/^_/, "");
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
function extractCodexTrajectorySteps(events) {
  const steps = [];
  const emittedItemIds = new Set();
  let currentThought = "";

  for (const ev of events) {
    // ── Ask/answer pairs from requestUserInput handling ───────────────────────
    // requestUserInput arrives as a JSON-RPC *request* (not a notification), so it
    // is never wrapped in sdk_event.  The codex_ask_question event is pushed by
    // handleRequestUserInput after the LLM judge returns the answer.
    if (ev.type === "codex_ask_question") {
      steps.push({
        thought: currentThought,
        act:     cap(`ask_human ${ev.question}`, ACT_CAP),
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
        act:     cap(`ask_human ${ev.question || ""}`, ACT_CAP),
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
      if (!done) continue;
      if (itemId && emittedItemIds.has(itemId)) continue;
      if (itemId) emittedItemIds.add(itemId);

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
      if (!done) continue;
      if (itemId && emittedItemIds.has(itemId)) continue;
      if (itemId) emittedItemIds.add(itemId);

      const toolName = `${item.server || ""}.${item.tool || ""}`.replace(/^\./, "");
      steps.push({
        thought: currentThought,
        act:     cap(`${toolName}: ${JSON.stringify(item.arguments || {})}`, ACT_CAP),
        obs:     cap(String(item.result || item.error || ""), OBS_CAP),
      });
      currentThought = "";
      continue;
    }
  }

  return steps;
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
  let numQuestions          = 0;
  let numQuestionsApproval  = 0;
  let numQuestionsFullInfo  = 0;
  let numBlockersResolved   = 0;

  for (const ev of events) {
    if (ev.type === "human_input_raw_event") {
      if (ASK_HUMAN_REQUEST_TYPES.has(ev.request_type))  numQuestions++;
      else if (APPROVAL_REQUEST_TYPES.has(ev.request_type)) numQuestionsApproval++;
    }
    if (ev.type === "ask_question_full_info_mode") numQuestionsFullInfo++;
    if (ev.type === "human_input_result") {
      const bid = ev.result?.blocker_id;
      if (bid && bid !== UNKNOWN_BLOCKER_ID && ev.result?.status === "answered")
        numBlockersResolved++;
    }
  }

  return {
    num_steps:                trajectorySteps.length,
    num_questions:            numQuestions,
    num_questions_approval:   numQuestionsApproval,
    num_total_questions:      numQuestions + numQuestionsApproval,
    num_questions_full_info:  numQuestionsFullInfo,
    num_blockers_resolved:    numBlockersResolved,
    num_blockers_total:       numBlockersTotal,
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
async function runCodexAppServer({ prompt, env, uid, humanRouter, pushEvent, abortController }) {
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
          return handleRequestUserInput({ params, router: humanRouter, pushEvent });
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

        // Legacy approval methods
        if (method === "execCommandApproval" || method === "applyPatchApproval") {
          return { decision: "approved" };
        }

        // MCP elicitation prompts are unsupported in this harness path.
        if (method === "mcpServer/elicitation/request") {
          return { action: "decline", content: null, _meta: null };
        }

        return {};
      },

      onNotification: async (msg) => {
        // All notifications go into allEvents for trajectory extraction and stats
        pushEvent({ type: "sdk_event", timestamp: new Date().toISOString(), event: msg });

        // Track the active turn ID so we can interrupt it if needed
        if (msg.method === "turn/started" &&
            (!threadId || msg.params?.threadId === threadId)) {
          currentTurnId = msg.params?.turn?.id ?? null;
          itemsDone = 0;
        }

        // Count completed items (commands + file edits + MCP tool calls).
        // When MAX_TURNS is set and the threshold is reached, interrupt the turn.
        // This is the codex equivalent of Claude SDK's maxTurns — the app-server
        // protocol has no native turn/step limit parameter.
        if (msg.method === "item/completed" &&
            MAX_TURNS > 0 &&
            !settled &&
            currentTurnId &&
            threadId) {
          itemsDone++;
          if (itemsDone >= MAX_TURNS) {
            pushEvent({
              type: "max_turns_reached",
              timestamp: new Date().toISOString(),
              items_done: itemsDone,
              max_turns: MAX_TURNS,
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
          settle(new Error(`Codex app-server error: ${JSON.stringify(msg.params)}`));
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
          approvalPolicy:       "on-request",
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
          ...(MODE === "ask_human" ? { developerInstructions: ASK_HUMAN_GUIDANCE } : {}),
        });

        threadId = threadStart?.thread?.id;
        if (!threadId)
          throw new Error(`Codex app-server did not return a thread id: ${JSON.stringify(threadStart)}`);

        await rpc.request("turn/start", {
          threadId,
          input: [{ type: "text", text: prompt, text_elements: [] }],
          cwd:              WORKSPACE,
          approvalPolicy:   "on-request",
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

  // 2. Build prompt
  let blockers = [];
  if (MODE === "full_info") {
    const registry = JSON.parse(await fs.readFile(path.join(TASK_DIR, "blocker_registry.json"), "utf8"));
    blockers = (registry.entries || registry.blockers || []).map((e) => ({
      description: e.description,
      resolution:  e.resolution,
    }));
  }
  const prompt = buildSwePrompt({ problemStatement, mode: MODE, blockers });
  await installAgentsSkill(WORKSPACE, SKILL_TOOL_REF.codex);

  // 3. Write attempt metadata
  await writeJson(path.join(OUTPUT_DIR, "attempt.json"), {
    run_id:     RUN_ID,
    uid,
    mode:       MODE,
    pass_index: PASS_INDEX,
    harness:    "codex",
    model:      CODEX_MODEL,
    max_turns:  MAX_TURNS > 0 ? MAX_TURNS : null,  // null = no limit (timeout only)
    timeout_ms: TIMEOUT_MS,
    workspace:  WORKSPACE,
    task_dir:   TASK_DIR,
    output_dir: OUTPUT_DIR,
    started_at: new Date().toISOString(),
    prompt,
    with_custom_tool: WITH_CUSTOM_TOOL,
  });
  pushEvent({ type: "attempt_start", timestamp: new Date().toISOString(), uid, mode: MODE, pass_index: PASS_INDEX, prompt });

  // 4. Set up human router (ask_human mode only — full_info has no routing)
  const humanRouter = MODE === "ask_human"
    ? createHumanInputRouter({
        instanceId:    uid,
        kbPath:        path.join(TASK_DIR, "blocker_registry.json"),
        trajectoryFile: pushEvent,  // router pushes human_input_* events here
        workspaceDir:  WORKSPACE,
        approvalPolicy: "allow",    // container is security boundary
        ...(ASK_HUMAN_BASE_URL ? { baseUrl:  ASK_HUMAN_BASE_URL } : {}),
        ...(ASK_HUMAN_MODEL    ? { modelId: ASK_HUMAN_MODEL    } : {}),
      })
    : null;

  // 5. Run agent with up to 3 attempts
  // Retries occur only on transient SDK errors; timeouts and clean completions
  // exit immediately.  allEvents is cleared in-place between attempts so only
  // the successful attempt's events are preserved in the final trajectory.
  let sdkError = null;
  const MAX_RETRIES = STEP_LITELLM_TRIES;
  const _runStart = Date.now();

  for (let _attempt = 1; _attempt <= MAX_RETRIES; _attempt++) {
    sdkError = null;
    allEvents.length = 0;   // clear in-place so pushEvent closure remains valid

    const PER_ATTEMPT_TIMEOUT_MS = LITELLM_CALL_TIMEOUT_MS;
    const remainingMs    = TIMEOUT_MS - (Date.now() - _runStart);
    const attemptTimeout = Math.min(remainingMs, PER_ATTEMPT_TIMEOUT_MS);
    if (attemptTimeout <= 0) {
      sdkError = `Timed out after ${TIMEOUT_MS}ms`;
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
      if (WITH_CUSTOM_TOOL) {
        ({ proc: sidecarProc, url: sidecarUrl } = await startSidecar(uid));
      }
      const env = codexApiEnv({ sidecarUrl });
      await runCodexAppServer({ prompt, env, uid, humanRouter, pushEvent, abortController });
    } catch (err) {
      const text = redactString(String(err?.stack || err));
      sdkError = abortController.signal.aborted
        ? `Timed out after ${PER_ATTEMPT_TIMEOUT_MS}ms.\n\n${text}`
        : text;
      pushEvent({ type: "sdk_error", timestamp: new Date().toISOString(), error: sdkError });
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
  const trajectorySteps = extractCodexTrajectorySteps(allEvents);

  let numBlockersTotal = 0;
  try {
    const reg = JSON.parse(await fs.readFile(path.join(TASK_DIR, "blocker_registry.json"), "utf8"));
    numBlockersTotal = (reg.entries || reg.blockers || []).length;
  } catch { /* non-fatal */ }

  const stats = computeTrajectoryStats(allEvents, trajectorySteps, numBlockersTotal);
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
