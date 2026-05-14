#!/usr/bin/env node
/**
 * SWE harness entrypoint for opencode — runs INSIDE a hilbench-swe-harness-opencode:<uid> container.
 *
 * Layout inside the container:
 *   /task/              (bind-mounted ro) task data: metadata.json, problem_statement.txt,
 *                                         blocker_registry.json
 *   /output/            (bind-mounted rw) trajectory.json, stats.json, patch.diff, result.json, attempt.json
 *   /app/               (built into image) repo at base commit — agent's workspace
 *   /opt/trust_horizon/ (built into image) node_modules + src
 *
 * Required env vars (forwarded by run_hil_swe.py via docker run -e):
 *   LITELLM_BASE_URL        LiteLLM proxy base URL (e.g. http://localhost:4000)
 *
 * Optional env vars:
 *   OPENCODE_MODEL          model slug (default: fireworks_ai/glm-5p1)
 *   OPENCODE_REASONING_EFFORT reasoning effort hint (low|medium|high|xhigh|max)
 *   MODE                    ask_human (default) | full_info
 *   PASS_INDEX              1-based pass number (default: 1)
 *   RUN_ID                  run identifier string
 *   MAX_TURNS               max agent steps (default: 200)
 *   ATTEMPT_TIMEOUT_MS      hard timeout in ms (default: 10800000 = 3 h)
 *   TASK_DIR                path to mounted task dir (default: /task)
 *   OUTPUT_DIR              path to mounted output dir (default: /output)
 *   ASK_HUMAN_BASE_URL      override base URL for ask_human judge
 *   ASK_HUMAN_MODEL         override ask_human judge model
 *   LITELLM_API_KEY         LiteLLM API key
 *   LITELLM_PROXY_API_KEY   same as LITELLM_API_KEY
 *   ANTHROPIC_AUTH_TOKEN    fallback API key
 *
 * ask_human tool behaviour:
 *   ask_human mode  — MCP ask_human tool registered + guided; questions routed through
 *                     ask_human_sidecar → human_input.mjs
 *   full_info mode  — MCP ask_human tool STILL registered (agent can call it), NO guidance
 *                     in system prompt; calls return "irrelevant question" and are counted
 *                     in num_questions_full_info (same rule as Claude + Codex + ADK)
 *
 * Tool approvals:
 *   --dangerously-skip-permissions auto-approves all permission.asked events inside run.ts
 *   before they are emitted to our stdout. We never see or mis-count them as questions.
 */

import path     from "node:path";
import fs       from "node:fs/promises";
import http     from "node:http";
import { spawn } from "node:child_process";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

import { buildSwePrompt } from "./prompt.mjs";
import { installAgentsSkill, SKILL_TOOL_REF } from "./skills.mjs";
import {
  WORKSPACE, TASK_DIR, OUTPUT_DIR,
  MODE, PASS_INDEX, RUN_ID, TIMEOUT_MS,
  LITELLM_CALL_TIMEOUT_MS, STEP_LITELLM_TRIES,
  ASK_HUMAN_BASE_URL, ASK_HUMAN_MODEL,
  buildAskHumanGuidance,
  THOUGHT_CAP, ACT_CAP, OBS_CAP, cap, gitDiff,
} from "./constants.mjs";

import { writeJson, writeText, ensureDir } from "../shared/io.mjs";
import {
  UNKNOWN_RESOLUTION, CANT_ANSWER, UNKNOWN_BLOCKER_ID,
  ASK_HUMAN_REQUEST_TYPES, APPROVAL_REQUEST_TYPES,
} from "../shared/human_input.mjs";

// ── Configuration from env ────────────────────────────────────────────────────

const MAX_TURNS       = Number(process.env.MAX_TURNS || "200");
const OPENCODE_MODEL  = process.env.OPENCODE_MODEL || "fireworks_ai/glm-5p1";
const OPENCODE_REASONING_EFFORT = (
  process.env.OPENCODE_REASONING_EFFORT ||
  process.env.OPENCODE_REASONING || // backward-compat alias
  ""
).trim().toLowerCase();
const OPENCODE_REASONING = !["", "0", "false", "no", "off", "none", "minimal", "low"].includes(OPENCODE_REASONING_EFFORT);

// Strip any leading "litellm/" the caller may have included, keep the bare model name.
// This is re-added as the config key: "model": "litellm/<modelId>".
const _modelId = OPENCODE_MODEL.startsWith("litellm/")
  ? OPENCODE_MODEL.slice("litellm/".length)
  : OPENCODE_MODEL;

// Rewrite localhost → host.docker.internal so OpenCode (running inside the container)
// can reach the LiteLLM proxy on the host.  The orchestrator adds
// --add-host=host.docker.internal:host-gateway to docker run.
const LITELLM_BASE_URL = (process.env.LITELLM_BASE_URL || "")
  .trim()
  .replace(/\blocalhost\b/g, "host.docker.internal")
  .replace(/\/+$/, "");

const LITELLM_API_KEY = (
  process.env.LITELLM_PROXY_API_KEY ||
  process.env.LITELLM_API_KEY ||
  process.env.ANTHROPIC_AUTH_TOKEN ||
  "dummy"
);

// ── Sidecar + bridge paths ────────────────────────────────────────────────────

const __dirname        = path.dirname(fileURLToPath(import.meta.url));
const SIDECAR_SCRIPT   = path.join(__dirname, "ask_human_sidecar.mjs");
const BRIDGE_SCRIPT    = path.join(__dirname, "ask_human_mcp_bridge.mjs");
const LLM_PROXY_SCRIPT = path.join(__dirname, "litellm_drop_params_proxy.mjs");

// ── System prompt constants ───────────────────────────────────────────────────

const _BASE_SYSTEM = "You are a helpful assistant that can interact with a computer to solve tasks.";

/** Mirrors _build_instruction(mode) in run_adk.py exactly. */
function buildInstruction(mode) {
  if (mode === "ask_human") {
    return `${_BASE_SYSTEM}\n\n${buildAskHumanGuidance("ask_human")}`;
  }
  return _BASE_SYSTEM;
}

// ── Sidecar management ────────────────────────────────────────────────────────

/**
 * Spawn ask_human_sidecar.mjs, read SIDECAR_PORT=N from its stdout, health-check.
 * Returns { proc, url }.
 * Mirrors _start_sidecar() in run_adk.py.
 */
async function startSidecar(uid) {
  const env = {
    ...process.env,
    TASK_UID: uid,
    // Forward ASK_HUMAN_* env vars in case they were not already in process.env
    ...(ASK_HUMAN_BASE_URL ? { ASK_HUMAN_BASE_URL } : {}),
    ...(ASK_HUMAN_MODEL    ? { ASK_HUMAN_MODEL }    : {}),
  };

  const proc = spawn("node", [SIDECAR_SCRIPT], {
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });

  // Drain stderr to avoid buffer deadlock (same pattern as ADK Python).
  proc.stderr.on("data", () => {});

  // Read first line from stdout: "SIDECAR_PORT=<n>"
  const portLine = await new Promise((resolve, reject) => {
    let buf = "";
    proc.stdout.on("data", (chunk) => {
      buf += chunk.toString();
      const nl = buf.indexOf("\n");
      if (nl !== -1) {
        proc.stdout.removeAllListeners("data");
        resolve(buf.slice(0, nl).trim());
      }
    });
    proc.on("exit", (code) => {
      reject(new Error(`sidecar exited with code ${code} before announcing port. buf=${buf}`));
    });
    proc.on("error", reject);
  });

  if (!portLine.startsWith("SIDECAR_PORT=")) {
    proc.kill();
    throw new Error(`sidecar did not announce port. Got: ${portLine}`);
  }
  const port = parseInt(portLine.split("=")[1], 10);
  const url  = `http://127.0.0.1:${port}`;

  // Health-check with retries (up to 5 s)
  for (let i = 0; i < 20; i++) {
    try {
      const ok = await httpGet(`${url}/health`);
      if (ok) return { proc, url };
    } catch {
      // ignore
    }
    await sleep(250);
  }
  proc.kill();
  throw new Error("sidecar health check failed after 20 attempts");
}

function stopSidecar(proc) {
  if (!proc) return;
  try { proc.kill("SIGTERM"); } catch { /* ignore */ }
}

/**
 * Spawn litellm_drop_params_proxy.mjs, read PROXY_PORT=N from its stdout.
 * Returns { proc, url }.
 *
 * The proxy strips "tool_choice" from outgoing LLM requests and injects
 * "drop_params: true" so the LiteLLM upstream silently removes any params
 * that the model (e.g. fireworks_ai/glm-5p1) does not support.
 */
async function startLiteLLMProxy() {
  const env = {
    ...process.env,
    REAL_LITELLM_URL: LITELLM_BASE_URL,
    LITELLM_API_KEY,
  };

  const proc = spawn("node", [LLM_PROXY_SCRIPT], {
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });

  proc.stderr.on("data", () => {});

  const portLine = await new Promise((resolve, reject) => {
    let buf = "";
    proc.stdout.on("data", (chunk) => {
      buf += chunk.toString();
      const nl = buf.indexOf("\n");
      if (nl !== -1) {
        proc.stdout.removeAllListeners("data");
        resolve(buf.slice(0, nl).trim());
      }
    });
    proc.on("exit",  (code) => reject(new Error(`llm-proxy exited with code ${code}`)));
    proc.on("error", reject);
  });

  if (!portLine.startsWith("PROXY_PORT=")) {
    proc.kill();
    throw new Error(`llm-proxy did not announce port. Got: ${portLine}`);
  }
  const port = parseInt(portLine.split("=")[1], 10);
  const url  = `http://127.0.0.1:${port}`;
  return { proc, url };
}

function stopLiteLLMProxy(proc) {
  if (!proc) return;
  try { proc.kill("SIGTERM"); } catch { /* ignore */ }
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────

function httpGet(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let data = "";
      res.on("data", (c) => { data += c; });
      res.on("end",  () => resolve(res.statusCode === 200));
    });
    req.on("error", reject);
    req.setTimeout(3000, () => { req.destroy(); reject(new Error("timeout")); });
  });
}

function httpGetJson(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let data = "";
      res.on("data", (c) => { data += c; });
      res.on("end",  () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { reject(e); }
      });
    });
    req.on("error", reject);
    req.setTimeout(10_000, () => { req.destroy(); reject(new Error("timeout")); });
  });
}

// ── Stats computation ─────────────────────────────────────────────────────────

/**
 * Mirrors computeTrajectoryStats in run_claude.mjs exactly.
 * Input `events` are plain objects: human_input_* events from sidecar
 * (retrieved via GET /events after the run) and ask_question_full_info_mode
 * events (pushed directly by the sidecar when MODE !== "ask_human").
 */
function computeTrajectoryStats(events, trajectorySteps, numBlockersTotal) {
  let numQuestions         = 0;
  let numQuestionsApproval = 0;
  let numQuestionsFullInfo = 0;
  let numBlockersResolved  = 0;

  for (const ev of events) {
    if (ev.type === "human_input_raw_event") {
      if (ASK_HUMAN_REQUEST_TYPES.has(ev.request_type)) {
        numQuestions++;
      } else if (APPROVAL_REQUEST_TYPES.has(ev.request_type)) {
        numQuestionsApproval++;
      }
    }
    if (ev.type === "ask_question_full_info_mode") numQuestionsFullInfo++;
    if (ev.type === "human_input_result") {
      const bid = ev.result?.blocker_id;
      if (bid && bid !== UNKNOWN_BLOCKER_ID && ev.result?.status === "answered") {
        numBlockersResolved++;
      }
    }
  }

  return {
    num_steps:               trajectorySteps.length,
    num_questions:           numQuestions,
    num_questions_approval:  numQuestionsApproval,
    num_total_questions:     numQuestions + numQuestionsApproval,
    num_questions_full_info: numQuestionsFullInfo,
    num_blockers_resolved:   numBlockersResolved,
    num_blockers_total:      numBlockersTotal,
  };
}

// ── Trajectory extraction from OpenCode --format json events ──────────────────
//
// OpenCode's `opencode run --format json` emits JSON lines to stdout.
// Each line is: { type, timestamp, sessionID, ...data }
//
// Event types (from packages/opencode/src/cli/cmd/run.ts):
//   "step_start"  — step boundary start; part: { type: "step-start", ... }
//   "step_finish" — step boundary end;   part: { type: "step-finish", tokens, cost, ... }
//   "text"        — completed assistant text block; part: { type: "text", text, time: { start, end } }
//   "reasoning"   — thinking block (only when --thinking is set; we don't set it → safe to ignore)
//   "tool_use"    — completed (or error) tool call; part: { type: "tool", tool, state, ... }
//   "error"       — session.error event; error: { name, data?: { message } }
//
// The tool_use event for our ask_human MCP tool has:
//   part.tool  = "ask_human"  (the MCP tool name as registered)
//   part.state.input = { question: "..." }
//   part.state.output = resolution string (e.g. "irrelevant question" or an actual answer)
//
// We do NOT see permission.asked events — run.ts handles them internally before emitting
// to stdout. With --dangerously-skip-permissions they are auto-approved as "once".
// They are never miscounted as questions.
//
// Trajectory algorithm:
//   • Track current step using step_start / step_finish boundaries.
//   • Within a step, the first "text" event becomes the "thought" for any tool calls
//     in that step (subsequent tools get thought="").
//   • Each "tool_use" event → one trajectory step {thought, act, obs}.
//   • A step that has text but no tool_use → standalone {thought, act:"", obs:""} step.
//   • An "error" event sets sdkError.

function extractTrajectory(opencodeEvents) {
  const steps = [];
  let pendingThought = "";
  let stepHadTool    = false;

  for (const ev of opencodeEvents) {
    const { type } = ev;

    if (type === "step_start") {
      pendingThought = "";
      stepHadTool    = false;
      continue;
    }

    if (type === "text") {
      const text = ev.part?.text || "";
      // Only capture the first text in a step as the thought
      if (!pendingThought && text.trim()) {
        pendingThought = cap(text, THOUGHT_CAP);
      }
      continue;
    }

    if (type === "tool_use") {
      const part     = ev.part || {};
      const toolName = String(part.tool || "");
      const state    = part.state || {};
      const input    = state.input  || {};
      const isError  = state.status === "error";
      const output   = isError ? (state.error || "") : (state.output || "");

      // Build act string
      let act;
      if (toolName === "ask_human") {
        // MCP ask_human tool — same format as ADK / Claude AskUserQuestion / Codex requestUserInput
        const q = String(input.question || "");
        act = cap(`ask_human ${q}`, ACT_CAP);
      } else if (toolName === "shell" || toolName === "bash") {
        const cmd = String(input.command || input.cmd || "");
        act = cap(cmd || JSON.stringify(input), ACT_CAP);
      } else {
        // glob, read, write, edit, webfetch, websearch, task, todowrite, or any MCP tool
        let inputStr;
        try { inputStr = JSON.stringify(input); } catch { inputStr = String(input); }
        act = cap(`${toolName}: ${inputStr}`, ACT_CAP);
      }

      const obs = cap(String(output), OBS_CAP);

      steps.push({ thought: pendingThought, act, obs });
      // Subsequent tools in the same step get an empty thought
      pendingThought = "";
      stepHadTool    = true;
      continue;
    }

    if (type === "step_finish") {
      // If the step had only text (no tool call), emit as a thought-only step
      if (pendingThought && !stepHadTool) {
        steps.push({ thought: pendingThought, act: "", obs: "" });
      }
      pendingThought = "";
      stepHadTool    = false;
      continue;
    }

    // "reasoning" and "error" are handled at the caller level
  }

  // Edge-case: run ended mid-step (e.g. timeout interrupted between step_start and step_finish)
  if (pendingThought) {
    steps.push({ thought: pendingThought, act: "", obs: "" });
  }

  return steps;
}

// ── Utility ───────────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function nowIso() {
  return new Date().toISOString();
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  await ensureDir(OUTPUT_DIR);

  // 1. Read task data
  const metadata         = JSON.parse(await fs.readFile(path.join(TASK_DIR, "metadata.json"), "utf8"));
  const problemStatement = await fs.readFile(path.join(TASK_DIR, "problem_statement.txt"), "utf8");
  const uid = String(metadata.uid || metadata.instance_id || "unknown");

  // 2. Load blockers (full_info only) and count total (both modes)
  let blockers       = [];
  let numBlockersTotal = 0;
  const registryPath = path.join(TASK_DIR, "blocker_registry.json");
  try {
    const registry = JSON.parse(await fs.readFile(registryPath, "utf8"));
    const entries  = registry.entries || registry.blockers || (Array.isArray(registry) ? registry : []);
    numBlockersTotal = entries.length;
    if (MODE === "full_info") {
      blockers = entries.map((e) => ({
        description: e.description || "",
        resolution:  e.resolution  || "",
      }));
    }
  } catch {
    // Registry absent → no blockers (still valid for ask_human mode)
  }

  // 3. Build prompt and system instruction
  const prompt      = buildSwePrompt({ problemStatement, mode: MODE, blockers });
  const instruction = buildInstruction(MODE);
  await installAgentsSkill(WORKSPACE, SKILL_TOOL_REF.opencode);

  // 4. Write attempt metadata
  await writeJson(path.join(OUTPUT_DIR, "attempt.json"), {
    run_id:     RUN_ID,
    uid,
    mode:       MODE,
    pass_index: PASS_INDEX,
    harness:    "opencode",
    model:      OPENCODE_MODEL,
    max_turns:  MAX_TURNS,
    timeout_ms: TIMEOUT_MS,
    workspace:  WORKSPACE,
    task_dir:   TASK_DIR,
    output_dir: OUTPUT_DIR,
    started_at: nowIso(),
    prompt,
  });

  // 5a. Start ask_human sidecar (unconditional — handles both modes gracefully)
  let sidecarProc = null;
  let sidecarUrl  = "";
  try {
    ({ proc: sidecarProc, url: sidecarUrl } = await startSidecar(uid));
  } catch (err) {
    const sdkErr = `Failed to start ask_human sidecar: ${err}`;
    process.stderr.write(`[run_opencode] ERROR: ${sdkErr}\n`);
    await writeJson(path.join(OUTPUT_DIR, "result.json"), {
      patch_bytes:  0,
      num_steps:    0,
      completed_at: nowIso(),
      sdk_error:    sdkErr,
      timeout:      false,
      stop_reason:  "sidecar_start_failed",
    });
    return;
  }

  // 5b. Start the LiteLLM drop-params proxy.
  //
  //     @ai-sdk/openai-compatible (used by OpenCode) unconditionally adds
  //     "tool_choice" to every LLM request.  Several LiteLLM backends
  //     (including fireworks_ai) reject "tool_choice" with a 400 error.
  //     Our local proxy strips "tool_choice" and injects "drop_params: true"
  //     so the upstream LiteLLM proxy silently drops any other unsupported
  //     parameters for the target model.
  let llmProxyProc = null;
  let llmProxyUrl  = "";
  try {
    ({ proc: llmProxyProc, url: llmProxyUrl } = await startLiteLLMProxy());
  } catch (err) {
    const sdkErr = `Failed to start LiteLLM drop-params proxy: ${err}`;
    process.stderr.write(`[run_opencode] ERROR: ${sdkErr}\n`);
    stopSidecar(sidecarProc);
    await writeJson(path.join(OUTPUT_DIR, "result.json"), {
      patch_bytes:  0,
      num_steps:    0,
      completed_at: nowIso(),
      sdk_error:    sdkErr,
      timeout:      false,
      stop_reason:  "proxy_start_failed",
    });
    return;
  }

  // 6. Build OPENCODE_CONFIG_CONTENT
  //
  //    Provider strategy — IMPORTANT:
  //    We use "@ai-sdk/openai-compatible" as a CUSTOM npm provider.  Despite the
  //    npm field name, this package IS bundled inside the OpenCode Bun binary —
  //    it is NOT downloaded from npm at runtime.  All built-in OpenCode providers
  //    (302ai, ollama, llama.cpp, …) use npm:"@ai-sdk/openai-compatible" and
  //    resolve to the same bundled copy.
  //
  //    Crucially, @ai-sdk/openai-compatible sends requests to /v1/chat/completions
  //    (NOT /v1/responses).  The built-in "openai" provider uses /v1/responses,
  //    which is the OpenAI Responses API.  LiteLLM's implementation of that API
  //    for fireworks_ai omits "response.output_item.added" events for message
  //    items, causing OpenCode to crash with "text part <id> not found".  We
  //    therefore MUST use @ai-sdk/openai-compatible to stay on chat/completions.
  //
  //    • "model": "litellm/<modelId>" — "litellm/" prefix selects the custom
  //      provider defined below; the bare modelId is sent to the server.
  //    • "agent.build.prompt" is intentionally OMITTED.
  //      Setting agent.build.prompt triggers OpenCode's codebase-indexing/model-discovery
  //      initialisation when --dir points to a large git repo (like /app), causing the
  //      process to hang indefinitely after sqlite migration without making any LLM calls.
  //      Tested: prompt=set + --dir /tmp → works; prompt=set + --dir /app → hangs forever;
  //              prompt=unset + --dir /app → works.  The system instruction is instead
  //              prepended to the user message (stdin), which the model sees first and
  //              treats as authoritative instructions before the task description.
  //    • "agent.build.steps" = MAX_TURNS — caps the number of agentic steps.
  //    • "mcp.ask_human" is present UNCONDITIONALLY (both ask_human and full_info modes).
  //      In full_info mode, the sidecar returns "irrelevant question" immediately and
  //      records an ask_question_full_info_mode event, mirroring Claude/Codex/ADK behaviour.
  //    • "autoupdate: false" prevents the agent from downloading updates mid-run.
  //    • "environment" (not "env") is the correct key in OpenCode's MCP local config schema.
  const opencodeConfig = {
    provider: {
      litellm: {
        npm:     "@ai-sdk/openai-compatible",
        name:    "LiteLLM",
        options: {
          apiKey:  LITELLM_API_KEY,
          baseURL: `${llmProxyUrl}/v1`,
        },
        models: {
          [_modelId]: {
            name:      _modelId,
            tool_call: true,
            reasoning: OPENCODE_REASONING,
          },
        },
      },
    },
    model:             `litellm/${_modelId}`,
    small_model:       `litellm/${_modelId}`,
    enabled_providers: ["litellm"],
    autoupdate:        false,
    agent: {
      build: {
        model:  `litellm/${_modelId}`,
        mode:   "primary",
        steps:  MAX_TURNS,
        permission: {
          edit:               "ask",
          bash:               "ask",
          webfetch:           "deny",
          external_directory: "deny",
        },
      },
    },
    mcp: {
      ask_human: {
        // type "local" + command as array.
        // "environment" is the correct key in OpenCode's McpLocalConfig schema
        // (not "env" — that field is unknown and silently ignored).
        type:        "local",
        enabled:     true,
        command:     ["node", BRIDGE_SCRIPT],
        environment: { SIDECAR_URL: sidecarUrl },
      },
    },
  };
  const configJson = JSON.stringify(opencodeConfig);

  // 7. Spawn `opencode run --format json --dangerously-skip-permissions`
  //
  //    opencode is on PATH via the container's ENV PATH=/opt/trust_horizon/node_modules/.bin:...
  //    The environment inherits all of process.env so TASK_UID, MODE, LITELLM_* etc. are all
  //    available to the sidecar if OpenCode forks any child process.
  //
  //    OPENCODE_NO_UPDATE=1 suppresses any auto-update banner (belt-and-suspenders with autoupdate:false).
  //    HOME=/tmp prevents OpenCode from reading/writing ~/.opencode config which could interfere.
  //
  //    PROMPT DELIVERY via stdin (not positional CLI arg):
  //    run.ts wraps any positional arg containing spaces in double-quotes:
  //      .map((arg) => (arg.includes(" ") ? `"${arg.replace(/"/g, '\\"')}"` : arg))
  //    This would cause the agent to receive `"<prompt>"` with literal surrounding quotes.
  //    Instead, we pass no positional args and pipe the prompt via stdin.  run.ts reads stdin
  //    when process.stdin.isTTY is falsy (it is, for a pipe) via Bun.stdin.text(), then
  //    appends it to message.  The agent receives "\n<prompt>" (the leading \n is harmless).
  const ocEnv = {
    ...process.env,
    OPENCODE_CONFIG_CONTENT:     configJson,
    OPENCODE_NO_UPDATE:          "1",
    HOME:                        "/tmp",
    // Ensure the LiteLLM API key is available under all names OpenCode might look for
    OPENAI_API_KEY:              LITELLM_API_KEY,
    // Prevent hang when --dir points to a large git repo.
    //
    // Root cause (discovered via binary analysis + --print-logs tracing):
    //
    // OpenCode initialises an inotify file watcher for the --dir workspace.  On a
    // large git repository like /app (thousands of files), the inotify init blocks
    // the Effect async scheduler indefinitely — the log stalls at:
    //   service=file.watcher directory=/app backend=inotify
    // and never reaches "server backend selected" or any LLM call.
    //
    // OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER=1 skips the watcher entirely.
    // We don't need live file-change notifications: the agent issues explicit
    // read/write/bash commands; it doesn't rely on reactive file watching.
    //
    // DISABLE_MODELS_FETCH prevents OpenCode from fetching the models catalog from
    // models.dev on startup (60-minute refresh timer + initial fetch).  This avoids
    // network I/O that times out in isolated harness containers.
    //
    // DISABLE_LSP_DOWNLOAD prevents downloading language-server binaries (clangd,
    // jdtls, kotlin-ls, etc.) over the network.
    //
    // FAST_BOOT skips a "loading" readiness gate (makes app.ready return true
    // immediately) to speed up session initialisation.
    OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER: "1",
    OPENCODE_DISABLE_MODELS_FETCH:             "1",
    OPENCODE_DISABLE_LSP_DOWNLOAD:             "1",
    OPENCODE_FAST_BOOT:                        "1",
    // ENABLE_QUESTION_TOOL activates OpenCode's native ask_human/question tool (PR #5958).
    // NOTE: Intentionally NOT set.  Our MCP-based ask_human bridge (registered via
    // config.mcp.ask_human) is the question-asking mechanism.
    // OPENCODE_ENABLE_QUESTION_TOOL: "1",
  };

  // 8. Spawn opencode and run with up to 3 attempts.
  //
  // Retries re-spawn the opencode process from scratch; they occur only on
  // sdk_error (non-zero exit, session error event) and NOT on timeout — the
  // overall TIMEOUT_MS wall-clock budget is shared across all attempts.
  const opencodeEvents = [];  // raw JSON events from opencode stdout
  let sdkError   = null;
  let timedOut   = false;
  let stopReason = "complete";
  let stderrBuf  = "";

  const MAX_RETRIES = STEP_LITELLM_TRIES;
  const _runStart = Date.now();

  for (let _attempt = 1; _attempt <= MAX_RETRIES; _attempt++) {
    // Reset per-attempt state (opencodeEvents cleared in-place so no outer ref breaks)
    opencodeEvents.length = 0;
    sdkError   = null;
    timedOut   = false;
    stopReason = "complete";
    stderrBuf  = "";

    const PER_ATTEMPT_TIMEOUT_MS = LITELLM_CALL_TIMEOUT_MS;
    const remainingMs    = TIMEOUT_MS - (Date.now() - _runStart);
    const attemptTimeout = Math.min(remainingMs, PER_ATTEMPT_TIMEOUT_MS);
    if (attemptTimeout <= 0) {
      timedOut   = true;
      stopReason = "timeout";
      break;
    }

    const ocProc = spawn(
      "opencode",
      ["run", "--format", "json", "--dangerously-skip-permissions",
       "--dir", WORKSPACE, "--agent", "build"],
      {
        env:   ocEnv,
        stdio: ["pipe", "pipe", "pipe"],
      },
    );

    // Write system instruction + task prompt to stdin and close immediately.
    //
    // We prepend the system instruction here (not via agent.build.prompt) because
    // agent.build.prompt triggers a hang with --dir /app (large repo) — see config comment.
    // The model sees the instruction first in the user turn, which is standard practice
    // for models that support chat/completions without a dedicated system-prompt field.
    //
    // Format: "<instruction>\n\n<task prompt>"
    // Bun.stdin.text() in run.ts reads until EOF, gets the full string, and sends it
    // as the initial user message.  The leading newline that run.ts prepends is harmless.
    //
    // Suppress EPIPE in the unlikely event the process crashes before reading stdin.
    ocProc.stdin.on("error", () => {});
    ocProc.stdin.end(`${instruction}\n\n${prompt}`, "utf8");

    // Accumulate stderr for debugging — print on error/timeout only
    ocProc.stderr.on("data", (chunk) => {
      stderrBuf += chunk.toString();
      // Bound the stderr buffer to avoid memory blowup on very chatty runs
      if (stderrBuf.length > 64_000) {
        stderrBuf = stderrBuf.slice(-32_000);
      }
    });

    // Parse stdout line-by-line as JSON events
    const ocRl = readline.createInterface({ input: ocProc.stdout, terminal: false });
    ocRl.on("line", (line) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      try {
        const ev = JSON.parse(trimmed);
        opencodeEvents.push(ev);
        // Capture session errors immediately
        if (ev.type === "error") {
          const errData = ev.error || {};
          let errMsg = String(errData.name || "opencode error");
          if (errData.data?.message) errMsg = String(errData.data.message);
          if (!sdkError) sdkError = errMsg;
        }
      } catch {
        // Non-JSON lines on stdout (e.g. banner, warning) — ignore
      }
    });

    await new Promise((resolve) => {
      let settled = false;
      const settle = (reason) => {
        if (settled) return;
        settled = true;
        stopReason = reason;
        resolve();
      };

      // Hard timeout: each attempt is individually capped at attemptTimeout.
      const timer = setTimeout(() => {
        timedOut   = true;
        stopReason = "timeout";
        process.stderr.write(`[run_opencode] timeout after ${PER_ATTEMPT_TIMEOUT_MS}ms — killing opencode\n`);
        try { ocProc.kill("SIGTERM"); } catch { /* ignore */ }
        // Give it 5 s to exit cleanly before SIGKILL
        setTimeout(() => {
          try { ocProc.kill("SIGKILL"); } catch { /* ignore */ }
          settle("timeout");
        }, 5_000);
      }, attemptTimeout);

      ocProc.on("close", (code) => {
        clearTimeout(timer);
        if (timedOut) { settle("timeout"); return; }
        if (code !== 0 && !sdkError) sdkError = `opencode exited with code ${code}`;
        settle(sdkError ? "sdk_error" : "complete");
      });

      ocProc.on("error", (err) => {
        clearTimeout(timer);
        if (!sdkError) sdkError = `spawn error: ${err.message}`;
        settle("sdk_error");
      });
    });

    // Flush any remaining readline buffer for this attempt
    ocRl.close();

    // Retry transient failures and timeout-aborted turns up to STEP_LITELLM_TRIES.
    if ((stopReason === "sdk_error" || stopReason === "timeout") && _attempt < MAX_RETRIES) {
      process.stderr.write(
        `[run_opencode] ${stopReason} on attempt ${_attempt}/${MAX_RETRIES}, retrying: ${String(sdkError || "").slice(0, 200)}\n`,
      );
      if (stderrBuf.trim()) {
        process.stderr.write(`[run_opencode] stderr tail:\n${stderrBuf.slice(-2048)}\n`);
      }
      continue;
    }
    break;
  }

  // 9. Retrieve all human_input events accumulated by the sidecar.
  //    These are the ground-truth events for stats (same approach as
  //    ADK's all_events list, just retrieved post-run instead of inline).
  let allEvents = [];
  try {
    const resp = await httpGetJson(`${sidecarUrl}/events`);
    allEvents = Array.isArray(resp.events) ? resp.events : [];
  } catch (err) {
    process.stderr.write(`[run_opencode] WARNING: failed to fetch sidecar events: ${err}\n`);
  }

  // 10. Collect patch and build trajectory + stats
  const patch = await gitDiff(WORKSPACE);
  const trajectorySteps = extractTrajectory(opencodeEvents);
  const stats = computeTrajectoryStats(allEvents, trajectorySteps, numBlockersTotal);

  // 11. Write output files (same order / format as run_claude.mjs + run_adk.py)
  await writeText(path.join(OUTPUT_DIR, "patch.diff"),       patch);
  await writeJson(path.join(OUTPUT_DIR, "trajectory.json"),  trajectorySteps);
  await writeJson(path.join(OUTPUT_DIR, "stats.json"),       stats);
  await writeJson(path.join(OUTPUT_DIR, "result.json"), {
    patch_bytes:  Buffer.byteLength(patch, "utf8"),
    num_steps:    stats.num_steps,
    completed_at: nowIso(),
    sdk_error:    sdkError,
    timeout:      timedOut,
    stop_reason:  stopReason,
  });

  // 12. Cleanup
  stopSidecar(sidecarProc);
  stopLiteLLMProxy(llmProxyProc);

  const label = `[${uid.slice(0, 12)}|${MODE}|p${PASS_INDEX}]`;
  if (sdkError && !timedOut) {
    process.stderr.write(`${label} opencode error: ${sdkError}\n`);
    if (stderrBuf.trim()) {
      process.stderr.write(`${label} stderr tail:\n${stderrBuf.slice(-4096)}\n`);
    }
  } else if (timedOut) {
    process.stderr.write(`${label} timed out (max ${TIMEOUT_MS / 1000}s)\n`);
    if (stderrBuf.trim()) {
      process.stderr.write(`${label} stderr tail:\n${stderrBuf.slice(-4096)}\n`);
    }
  } else {
    process.stdout.write(
      `${label} done  steps=${stats.num_steps}  ` +
      `questions=${stats.num_questions}  ` +
      `questions_full_info=${stats.num_questions_full_info}  ` +
      `patch_bytes=${Buffer.byteLength(patch, "utf8")}\n`,
    );
  }
}

main().catch((err) => {
  process.stderr.write(`[run_opencode] FATAL: ${err.stack || err}\n`);
  process.exit(1);
});
