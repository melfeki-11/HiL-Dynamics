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
 *   OPENCODE_STARTUP_TIMEOUT_MS
 *                           startup watchdog in ms before first stdout line
 *                           (default: 300000 = 5 min)
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
 *                     ask_human_sidecar → human_input.mjs; ask-human SKILL.md installed.
 *   full_info mode  — NO MCP ask_human tool, NO ask-human guidance in system prompt,
 *                     and NO ask-human skill on disk.
 *
 * Tool approvals:
 *   In SDK/server mode we set agent.build.permission edit/bash to "allow" in
 *   opencodeConfig, so no interactive permission round-trip is required.
 */

import path     from "node:path";
import fs       from "node:fs/promises";
import http     from "node:http";
import net      from "node:net";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createOpencode } from "@opencode-ai/sdk";

import { buildSwePrompt } from "./prompt.mjs";
import { installOpenCodeSkill, removeInstalledAskHumanSkills, SKILL_TOOL_REF } from "./skills.mjs";
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
  UNKNOWN_RESOLUTION, UNKNOWN_BLOCKER_ID,
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
const OPENCODE_STARTUP_TIMEOUT_MS = Number(process.env.OPENCODE_STARTUP_TIMEOUT_MS || "300000");

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

function httpPostJson(url, body = {}) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const u = new URL(url);
    const req = http.request(
      {
        method: "POST",
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port,
        path: u.pathname + u.search,
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => { data += c; });
        res.on("end", () => {
          if (res.statusCode !== 200) {
            reject(new Error(`HTTP ${res.statusCode || "?"}`));
            return;
          }
          try { resolve(JSON.parse(data || "{}")); }
          catch (e) { reject(e); }
        });
      }
    );
    req.on("error", reject);
    req.setTimeout(10_000, () => { req.destroy(); reject(new Error("timeout")); });
    req.write(payload);
    req.end();
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
  const resolvedBlockerIds = new Set();
  const requestStatusById  = new Map();

  // Exclude failed ask_human invocations (status="error") from question counts.
  for (const ev of events) {
    if (ev.type !== "human_input_result") continue;
    const rid = String(ev.request_id || "");
    if (!rid) continue;
    const status = String(ev.result?.status || "unknown").toLowerCase();
    requestStatusById.set(rid, status);
  }

  for (const ev of events) {
    if (ev.type === "human_input_raw_event") {
      const rid = String(ev.request_id || "");
      const status = rid ? requestStatusById.get(rid) : null;
      if (status === "error") continue;
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
        resolvedBlockerIds.add(bid);
      }
    }
  }

  return {
    num_steps:               trajectorySteps.length,
    num_questions:           numQuestions,
    num_questions_approval:  numQuestionsApproval,
    num_total_questions:     numQuestions + numQuestionsApproval,
    num_questions_full_info: numQuestionsFullInfo,
    num_blockers_resolved:   resolvedBlockerIds.size,
    num_blockers_total:      numBlockersTotal,
    stats_schema_version:    2,
  };
}

// ── Trajectory extraction from OpenCode server events ─────────────────────────
//
// In SDK/server mode we collect SSE events from client.event.subscribe() and
// convert message.part.updated events into SWE-compatible trajectory steps.
//
// Relevant event shape:
//   { type: "message.part.updated", properties: { sessionID, part: {...} } }
//
// Part variants we care about:
//   • text/reasoning  → thought
//   • tool            → {thought, act, obs}
//   • patch / command → treated as actions so steps remain informative even if
//                       OpenCode emits non-tool action parts for edits/commands.
function extractTrajectory(serverEvents) {
  const steps = [];
  const seenPartIds = new Set();
  let pendingThought = "";

  for (const ev of serverEvents) {
    if (ev?.type !== "message.part.updated") continue;
    const part = ev?.properties?.part || {};
    const partType = String(part.type || "").toLowerCase();
    const partId = part.id || part.partID || part.partId || null;
    if (partId && seenPartIds.has(partId)) continue;
    if (partId) seenPartIds.add(partId);

    if (partType === "text" || partType === "reasoning") {
      const text = String(part.text || part.content || "");
      if (!pendingThought && text.trim()) pendingThought = cap(text, THOUGHT_CAP);
      continue;
    }

    if (partType === "tool") {
      const toolName = String(part.tool?.name || part.tool || part.name || "");
      const state    = part.state || {};
      const input    = state.input || part.input || part.args || {};
      const isError  = state.status === "error" || part.status === "error";
      const output   = isError
        ? (state.error || part.error || "")
        : (state.output || part.output || part.result || "");

      let act;
      if (toolName === "ask_human") {
        const q = String(input.question || "");
        act = cap(`ask_human ${q}`, ACT_CAP);
      } else if (toolName === "shell" || toolName === "bash") {
        const cmd = String(input.command || input.cmd || "");
        act = cap(cmd || JSON.stringify(input), ACT_CAP);
      } else {
        let inputStr;
        try { inputStr = JSON.stringify(input); } catch { inputStr = String(input); }
        act = cap(`${toolName}: ${inputStr}`, ACT_CAP);
      }

      steps.push({ thought: pendingThought, act, obs: cap(String(output), OBS_CAP) });
      pendingThought = "";
      continue;
    }

    if (partType === "patch") {
      const files = Array.isArray(part.files) ? part.files.filter(Boolean) : [];
      const act = files.length ? `Edit: ${files.join(", ")}` : "Edit";
      steps.push({ thought: pendingThought, act: cap(act, ACT_CAP), obs: "" });
      pendingThought = "";
      continue;
    }

    if (partType === "command") {
      const cmd = String(part.command || "");
      const output = String(part.output || part.result || "");
      steps.push({ thought: pendingThought, act: cap(cmd, ACT_CAP), obs: cap(output, OBS_CAP) });
      pendingThought = "";
      continue;
    }
  }

  if (pendingThought) steps.push({ thought: pendingThought, act: "", obs: "" });
  return steps;
}

// ── Utility ───────────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function nowIso() {
  return new Date().toISOString();
}

function unwrapSdkResponse(value) {
  return value && typeof value === "object" && "data" in value ? value.data : value;
}

async function allocateLoopbackPort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      srv.close((err) => {
        if (err) reject(err);
        else resolve(port);
      });
    });
  });
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
  const askHumanToolEnabled = MODE === "ask_human";
  if (!askHumanToolEnabled) {
    await removeInstalledAskHumanSkills(WORKSPACE);
  } else {
    await installOpenCodeSkill(WORKSPACE, SKILL_TOOL_REF.opencode);
  }

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
    ask_human_tool_enabled: askHumanToolEnabled,
  });

  // 5a. Start ask_human sidecar only when ask_human tool is enabled.
  let sidecarProc = null;
  let sidecarUrl  = "";
  if (askHumanToolEnabled) {
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
  //    • "mcp.ask_human" is present ONLY in ask_human mode. In full_info mode
  //      no ask_human tool is exposed to the agent.
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
          // OpenCode defaults provider request timeout to 300000ms (5 min).
          // Long tool-heavy turns can exceed this and surface as generic "fetch failed".
          // Harness-level timeout/retry already governs total runtime, so disable provider timeout.
          timeout: false,
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
          // SDK/server mode does not use --dangerously-skip-permissions, so allow
          // edit/bash directly inside the container sandbox (same trust boundary
          // rationale used by Claude/Codex harnesses).
          edit:               "allow",
          bash:               "allow",
          webfetch:           "deny",
          external_directory: "deny",
        },
      },
    },
    ...(askHumanToolEnabled
      ? {
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
        }
      : {}),
  };
  // 7. Configure OpenCode runtime behavior for SDK/server mode.
  //
  // We no longer wrap `opencode run` CLI output.  Instead we drive OpenCode via
  // its SDK/server APIs. These env flags still matter because the SDK starts the
  // same OpenCode runtime under the hood.
  process.env.OPENCODE_NO_UPDATE = "1";
  process.env.HOME = "/tmp";
  process.env.OPENAI_API_KEY = LITELLM_API_KEY;
  process.env.OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER = "1";
  process.env.OPENCODE_DISABLE_MODELS_FETCH = "1";
  process.env.OPENCODE_DISABLE_LSP_DOWNLOAD = "1";
  process.env.OPENCODE_FAST_BOOT = "1";

  // 8. Run OpenCode via SDK/server with up to 3 attempts.
  const opencodeEvents = [];  // normalized server events for trajectory extraction
  let sdkError   = null;
  let timedOut   = false;
  let stopReason = "complete";
  let allEvents  = [];

  const MAX_RETRIES = STEP_LITELLM_TRIES;
  const _runStart = Date.now();

  for (let _attempt = 1; _attempt <= MAX_RETRIES; _attempt++) {
    // Reset per-attempt state
    opencodeEvents.length = 0;
    sdkError   = null;
    timedOut   = false;
    stopReason = "complete";
    let attemptEventStartIndex = 0;

    if (askHumanToolEnabled) {
      // Best-effort reset so sidecar /events contains only this retry attempt.
      // If reset fails, fall back to snapshot/slice logic for isolation.
      try {
        await httpPostJson(`${sidecarUrl}/events/reset`);
        attemptEventStartIndex = 0;
      } catch (err) {
        process.stderr.write(`[run_opencode] WARNING: failed to reset sidecar events before retry attempt: ${err}\n`);
        try {
          const snap = await httpGetJson(`${sidecarUrl}/events`);
          const existing = Array.isArray(snap?.events) ? snap.events : [];
          attemptEventStartIndex = existing.length;
        } catch (snapErr) {
          process.stderr.write(`[run_opencode] WARNING: failed pre-attempt sidecar event snapshot: ${snapErr}\n`);
          attemptEventStartIndex = 0;
        }
      }
    }

    const PER_ATTEMPT_TIMEOUT_MS = LITELLM_CALL_TIMEOUT_MS;
    const remainingMs = TIMEOUT_MS - (Date.now() - _runStart);
    const attemptTimeout = Math.min(remainingMs, PER_ATTEMPT_TIMEOUT_MS);
    if (attemptTimeout <= 0) {
      timedOut   = true;
      stopReason = "timeout";
      break;
    }

    let opencodeInstance = null;
    let sdkPort = 0;
    let sessionId = null;
    let stopEventReader = false;
    let eventReaderPromise = null;
    let eventAbort = null;
    let runDoneResolve = null;
    let runDoneReject = null;
    const runDonePromise = new Promise((resolve, reject) => {
      runDoneResolve = resolve;
      runDoneReject = reject;
    });
    let runDoneSettled = false;
    const resolveRunDone = (value) => {
      if (runDoneSettled) return;
      runDoneSettled = true;
      runDoneResolve(value);
    };
    const rejectRunDone = (reason) => {
      if (runDoneSettled) return;
      runDoneSettled = true;
      runDoneReject(reason instanceof Error ? reason : new Error(String(reason || "unknown run error")));
    };

    try {
      sdkPort = await allocateLoopbackPort();
      opencodeInstance = await createOpencode({
        port: sdkPort,
        timeout: OPENCODE_STARTUP_TIMEOUT_MS,
        config: opencodeConfig,
      });
      const client = opencodeInstance.client;

      // Create a dedicated session for this attempt.
      const createdSessionResp = await client.session.create({
        body: { title: `hil_swe_${uid.slice(0, 12)}_${MODE}_p${PASS_INDEX}_a${_attempt}` },
      });
      const createdSession = unwrapSdkResponse(createdSessionResp);
      sessionId = String(createdSession?.id || createdSession?.session?.id || "");
      if (!sessionId) throw new Error("OpenCode SDK did not return a valid session id");

      // Collect server events for this session in the background while prompt runs.
      eventAbort = new AbortController();
      const subscription = await client.event.subscribe({
        signal: eventAbort.signal,
        // Prevent infinite reconnect loops after shutdown; we own retry at harness level.
        sseMaxRetryAttempts: 0,
      });
      const stream = subscription?.stream || subscription;
      eventReaderPromise = (async () => {
        for await (const raw of stream) {
          if (stopEventReader) break;
          const ev = unwrapSdkResponse(raw);
          const type = String(ev?.type || "");
          const properties = ev?.properties || {};
          const eventSessionId = String(
            properties?.sessionID ||
            properties?.sessionId ||
            properties?.session?.id ||
            ""
          );
          if (!eventSessionId || eventSessionId !== sessionId) continue;
          opencodeEvents.push({ type, properties });

          // Capture explicit session error events early.
          if (type === "session.error") {
            const message = String(
              properties?.error?.message ||
              properties?.message ||
              "opencode session error"
            );
            if (!sdkError) sdkError = message;
            rejectRunDone(new Error(message));
            continue;
          }

          if (type === "session.idle") {
            resolveRunDone("idle");
            continue;
          }

          if (type === "session.status") {
            const status = String(properties?.status || "").toLowerCase();
            if (!status) continue;
            if (status === "idle" || status === "completed" || status === "done") {
              resolveRunDone(status);
              continue;
            }
            if (status === "error" || status === "failed" || status === "aborted") {
              const message = String(properties?.error?.message || properties?.message || `session status ${status}`);
              if (!sdkError) sdkError = message;
              rejectRunDone(new Error(message));
              continue;
            }
          }
        }
      })();

      const promptText = `${instruction}\n\n${prompt}`;
      // Use promptAsync + event-driven completion so long turns do not fail on
      // long-held HTTP request timeouts from the sync prompt endpoint.
      const runPromise = client.session.promptAsync ? client.session.promptAsync({
        path: { id: sessionId },
        body: {
          agent: "build",
          parts: [{ type: "text", text: promptText }],
        },
      }) : client.session.prompt({
        path: { id: sessionId },
        body: {
          agent: "build",
          parts: [{ type: "text", text: promptText }],
        },
      });
      const timeoutPromise = new Promise((_, reject) => {
        setTimeout(() => reject(new Error(`OpenCode attempt timed out after ${attemptTimeout}ms`)), attemptTimeout);
      });

      await runPromise;
      await Promise.race([runDonePromise, timeoutPromise]);

      // Treat session.error (captured via stream) as sdk_error even if prompt resolved.
      if (sdkError) stopReason = "sdk_error";
      else stopReason = "complete";
    } catch (err) {
      const cause = err && typeof err === "object" ? err.cause : null;
      const causeText = cause
        ? String(cause?.code || cause?.message || cause)
        : "";
      const msg = causeText
        ? `${String(err?.message || err)} (cause: ${causeText})`
        : String(err?.message || err);
      sdkError = msg;
      if (/timed out/i.test(msg)) {
        timedOut = true;
        stopReason = "timeout";
      } else {
        stopReason = "sdk_error";
      }
      // Abort current session on failure/timeout to prevent leaked tasks.
      if (sessionId && opencodeInstance?.client?.session?.abort) {
        try { await opencodeInstance.client.session.abort({ path: { id: sessionId } }); } catch { /* ignore */ }
      }
    } finally {
      stopEventReader = true;
      if (eventAbort) {
        try { eventAbort.abort(); } catch { /* ignore */ }
      }
      if (eventReaderPromise) {
        try { await Promise.race([eventReaderPromise, sleep(2000)]); } catch { /* ignore */ }
      }
      if (sessionId && opencodeInstance?.client?.instance?.dispose) {
        try { await opencodeInstance.client.instance.dispose(); } catch { /* ignore */ }
      }
      if (opencodeInstance?.server?.close) {
        try { opencodeInstance.server.close(); } catch { /* ignore */ }
      }
    }

    // Capture ONLY this attempt's sidecar events (delta since attempt start).
    // Retries should not leak ask_human events into the final pass stats.
    let attemptEvents = [];
    if (askHumanToolEnabled) {
      try {
        const snap = await httpGetJson(`${sidecarUrl}/events`);
        const fullEvents = Array.isArray(snap?.events) ? snap.events : [];
        const start = Math.max(0, Math.min(attemptEventStartIndex, fullEvents.length));
        attemptEvents = fullEvents.slice(start);
      } catch (err) {
        process.stderr.write(`[run_opencode] WARNING: failed to fetch attempt sidecar events: ${err}\n`);
        attemptEvents = [];
      }
    }

    // Retry transient failures and timeout-aborted turns up to STEP_LITELLM_TRIES.
    if ((stopReason === "sdk_error" || stopReason === "timeout") && _attempt < MAX_RETRIES) {
      process.stderr.write(
        `[run_opencode] ${stopReason} on attempt ${_attempt}/${MAX_RETRIES}, retrying: ${String(sdkError || "").slice(0, 300)}\n`,
      );
      continue;
    }
    allEvents = attemptEvents;
    break;
  }

  // 9. Collect patch and build trajectory + stats
  const patch = await gitDiff(WORKSPACE);
  const trajectorySteps = extractTrajectory(opencodeEvents);
  const stats = computeTrajectoryStats(allEvents, trajectorySteps, numBlockersTotal);

  // 10. Write output files (same order / format as run_claude.mjs + run_adk.py)
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

  // 11. Cleanup
  stopSidecar(sidecarProc);
  stopLiteLLMProxy(llmProxyProc);

  const label = `[${uid.slice(0, 12)}|${MODE}|p${PASS_INDEX}]`;
  if (sdkError && !timedOut) {
    process.stderr.write(`${label} opencode error: ${sdkError}\n`);
  } else if (timedOut) {
    process.stderr.write(`${label} timed out (max ${TIMEOUT_MS / 1000}s)\n`);
  } else {
    process.stdout.write(
      `${label} done  steps=${stats.num_steps}  ` +
      `questions=${stats.num_questions}  ` +
      `questions_full_info=${stats.num_questions_full_info}  ` +
      `patch_bytes=${Buffer.byteLength(patch, "utf8")}\n`,
    );
  }
}

main()
  .then(() => {
    // Ensure container exits even if OpenCode SDK/server leaves open handles.
    process.exit(0);
  })
  .catch((err) => {
    process.stderr.write(`[run_opencode] FATAL: ${err.stack || err}\n`);
    process.exit(1);
  });
