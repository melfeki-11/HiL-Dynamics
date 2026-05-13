#!/usr/bin/env node
/**
 * SWE harness entrypoint for claude-code — runs INSIDE a hilbench-swe-harness:<uid> container.
 *
 * Layout inside the container:
 *   /task/              (bind-mounted ro) task data: metadata.json, problem_statement.txt,
 *                                         blocker_registry.json, run_script.sh, parser.py
 *   /output/            (bind-mounted rw) trajectory.json, stats.json, patch.diff, result.json, attempt.json
 *   /app/               (built into image) repo at base commit — agent's workspace (/testbed is a symlink to /app)
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
 *   MAX_TURNS             max agent turns (default: 200)
 *   ATTEMPT_TIMEOUT_MS    hard timeout in ms (default: 10800000 = 3 h)
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
import { query } from "@anthropic-ai/claude-agent-sdk";
import { createHumanInputRouter, approvalPolicyRouter, UNKNOWN_RESOLUTION, CANT_ANSWER, UNKNOWN_BLOCKER_ID, ASK_HUMAN_REQUEST_TYPES, APPROVAL_REQUEST_TYPES } from "../shared/human_input.mjs";
import { ensureDir, writeJson, writeText } from "../shared/io.mjs";
import { redactString } from "../shared/redact.mjs";
import { buildSwePrompt } from "./prompt.mjs";
import {
  WORKSPACE, TASK_DIR, OUTPUT_DIR,
  MODE, PASS_INDEX, RUN_ID, TIMEOUT_MS,
  ASK_HUMAN_BASE_URL, ASK_HUMAN_MODEL, buildAskHumanGuidance,
  THOUGHT_CAP, ACT_CAP, OBS_CAP, cap, gitDiff,
} from "./constants.mjs";

// Claude's native question-asking tool is AskUserQuestion
const ASK_HUMAN_GUIDANCE = buildAskHumanGuidance("AskUserQuestion");

// ── Configuration from env ──────────────────────────────────────────────────

const CLAUDE_MODEL    = process.env.CLAUDE_MODEL  || "claude-sonnet-4-6";
const MAX_TURNS       = Number(process.env.MAX_TURNS || "200");
// "acceptEdits" auto-approves file edits while still letting canUseTool fire for
// shell/MCP/AskUserQuestion calls so we can intercept them.  bypassPermissions would
// skip the canUseTool callback for some tool types entirely.
const PERMISSION_MODE = process.env.PERMISSION_MODE || "acceptEdits";
const CLAUDE_BIN      = process.env.CLAUDE_CODE_EXECUTABLE || "claude";

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

function parseResolutionJson(resolution) {
  try { return JSON.parse(resolution); } catch { return { answer: resolution }; }
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

// ── Trajectory extraction ────────────────────────────────────────────────────

/**
 * Format a tool call as an "act" string comparable to SWE-agent's action format.
 * ask_human calls are prefixed with "ask_human " for metric counting compatibility
 * with run_hil_bench.py's trajectory analysis.
 */
function formatAct(toolName, toolInput) {
  const name = String(toolName || "");
  if (!name) return "";
  const isAskHuman = /ask_human|AskUserQuestion|askUserQuestion/i.test(name);
  if (isAskHuman) {
    const q = toolInput?.question || toolInput?.questions?.[0]?.question || JSON.stringify(toolInput || {});
    return `ask_human ${q}`;
  }
  try {
    const inputStr = typeof toolInput === "string" ? toolInput : JSON.stringify(toolInput || {});
    return `${name}: ${inputStr.slice(0, 4000)}`;
  } catch {
    return name;
  }
}

/**
 * Format a tool result as an "obs" string.
 */
function formatObs(content, isError) {
  const prefix = isError ? "[error] " : "";
  if (content == null) return `${prefix}`;
  if (typeof content === "string") return `${prefix}${content}`;
  if (Array.isArray(content)) {
    return prefix + content.map((c) => (typeof c === "string" ? c : c?.text || JSON.stringify(c))).join("\n");
  }
  try { return `${prefix}${JSON.stringify(content)}`; } catch { return `${prefix}[unserializable]`; }
}


/**
 * Convert raw SDK events into [{thought?, act, obs}, ...] trajectory steps.
 *
 * Every meaningful assistant output unit becomes a step:
 *   - Tool call (with optional preceding thought) → {thought, act, obs} once result arrives.
 *   - Text-only turn (no tool call) → {thought: <text>, act: "", obs: ""}.
 *
 * Message structure (from claude-agent-sdk types):
 *   SDKAssistantMessage:  { type:"assistant", message: BetaMessage }
 *     BetaMessage.content: Array of { type:"thinking"|"text"|"tool_use", ... }
 *   SDKUserMessage:       { type:"user", message: MessageParam }
 *     MessageParam.content: string | Array of { type:"tool_result", tool_use_id, content, is_error? }
 *
 * Strategy:
 *   - For each assistant turn:
 *       • Extract thought (thinking block > first text block > "").
 *       • If the turn has tool_use blocks, register each in a pending map with the shared thought.
 *       • If the turn has NO tool_use blocks but has text/thinking content, emit a text-only step.
 *   - For each user turn with tool_result blocks, match by tool_use_id → emit step.
 *   - At the end, flush any unmatched pending calls (e.g. AskUserQuestion denied with no result).
 */
function extractTrajectorySteps(events) {
  const steps = [];
  const pending    = new Map(); // toolUseId → { act, thought }
  // tool_use_ids that were already emitted via a claude_ask_question event;
  // any SDK-injected synthetic tool_result for these ids should be skipped to
  // prevent duplicate trajectory steps.
  const handledAskIds = new Set();

  for (const event of events) {
    // ── Ask/answer pairs from AskUserQuestion handling (ask_human mode) ──────
    // AskUserQuestion is denied (behavior:"deny") so the SDK never executes it
    // natively.  The claude_ask_question event is pushed by canUseTool after the
    // LLM judge returns the answer, giving us a clean question + answer pair.
    // We consume the pending entry here so the deny's synthetic tool_result (if
    // the SDK emits one) is suppressed by the handledAskIds guard below.
    if (event.type === "claude_ask_question") {
      const p = pending.get(event.tool_use_id);
      steps.push({
        thought: p?.thought ?? "",
        act:     cap(`ask_human ${event.question}`, ACT_CAP),
        obs:     cap(String(event.answer ?? ""), OBS_CAP),
      });
      pending.delete(event.tool_use_id);
      handledAskIds.add(event.tool_use_id);
      continue;
    }

    // ── full_info mode questions ──────────────────────────────────────────────
    // In full_info mode the agent may still call AskUserQuestion; the handler
    // denies it with UNKNOWN_RESOLUTION and pushes this event so we capture the
    // attempt in the trajectory (act: "ask_human …", obs: "irrelevant question").
    // Adding the id to handledAskIds ensures the SDK's synthetic tool_result does
    // not create a duplicate trajectory step.
    if (event.type === "ask_question_full_info_mode") {
      const p = pending.get(event.tool_use_id);
      steps.push({
        thought: p?.thought ?? "",
        act:     cap(`ask_human ${event.question || ""}`, ACT_CAP),
        obs:     UNKNOWN_RESOLUTION,
      });
      if (event.tool_use_id) {
        pending.delete(event.tool_use_id);
        handledAskIds.add(event.tool_use_id);
      }
      continue;
    }

    if (event.type !== "sdk_message") continue;
    const msg = event.message;
    if (!msg) continue;

    if (msg.type === "assistant") {
      const content = Array.isArray(msg.message?.content) ? msg.message.content : [];

      // Priority: thinking block > first text block > "".
      let turnThought = "";
      for (const block of content) {
        if (block.type === "thinking" && block.thinking) { turnThought = block.thinking; break; }
      }
      if (!turnThought) {
        for (const block of content) {
          if (block.type === "text" && block.text) { turnThought = block.text; break; }
        }
      }

      const toolUseBlocks = content.filter((b) => b.type === "tool_use" && b.id);

      if (toolUseBlocks.length > 0) {
        // Register each tool call; thought is shared across all calls in this turn.
        for (const block of toolUseBlocks) {
          pending.set(block.id, { act: cap(formatAct(block.name, block.input), ACT_CAP), thought: cap(turnThought, THOUGHT_CAP) });
        }
      } else if (turnThought) {
        // Text-only turn — emit immediately as a standalone step with no act/obs.
        steps.push({ thought: cap(turnThought, THOUGHT_CAP), act: "", obs: "" });
      }

    } else if (msg.type === "user") {
      const content = Array.isArray(msg.message?.content) ? msg.message.content : [];
      for (const block of content) {
        if (block.type === "tool_result") {
          // Skip tool_results that were already captured via claude_ask_question.
          // The SDK may inject a synthetic tool_result when canUseTool denies a
          // tool; without this guard we would emit a second, duplicate step.
          if (handledAskIds.has(block.tool_use_id)) continue;
          const obs = formatObs(block.content, block.is_error === true);
          const p = pending.get(block.tool_use_id);
          if (p) {
            pending.delete(block.tool_use_id);
            steps.push({ thought: p.thought, act: p.act, obs });
          } else {
            steps.push({ thought: "", act: "", obs }); // orphaned result
          }
        }
      }
    }
  }

  // Flush tool calls that never got a result (e.g. AskUserQuestion denied by canUseTool).
  for (const [, p] of pending) {
    steps.push({ thought: p.thought, act: p.act, obs: "[no observation — tool call was denied or interrupted]" });
  }
  pending.clear();

  return steps;
}

/**
 * Compute per-run stats from the full event list.
 *
 * num_questions         — clarification + elicitation requests (both go to LLM judge)
 * num_questions_approval — approval + permission requests (tool-use authorizations, pattern-matched)
 * num_total_questions   — all four types combined
 * num_blockers_resolved — human_input_result events where a real blocker was matched
 * num_blockers_total    — total blockers present in the registry for this task
 */
/**
 * num_questions_full_info  — questions asked in full_info mode (ask_question_full_info_mode
 *                            events).  Tracked even though the agent receives "irrelevant
 *                            question", because it is analytically useful to know how often
 *                            agents ask despite having all info in the prompt.
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

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  await ensureDir(OUTPUT_DIR);

  // 1. Read task data
  const metadata        = JSON.parse(await fs.readFile(path.join(TASK_DIR, "metadata.json"), "utf8"));
  const problemStatement = await fs.readFile(path.join(TASK_DIR, "problem_statement.txt"), "utf8");
  const uid = metadata.uid || metadata.instance_id;

  // In-memory event log — replaces trajectory.jsonl on disk.
  // After the run, used to build trajectory.json and stats.json.
  const allEvents = [];
  const pushEvent = (ev) => allEvents.push(ev);

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
    prompt,
  };
  await writeJson(path.join(OUTPUT_DIR, "attempt.json"), attemptMeta);
  pushEvent({ type: "attempt_start", timestamp: new Date().toISOString(), uid, mode: MODE, pass_index: PASS_INDEX, prompt });

  // 4. Set up human router (ask_human mode only — full_info has no clarification routing)
  // approvalPolicy "allow": inside a per-task Docker container the container IS the security
  // boundary.  The registry hard-deny checks (paths outside workspaceDir) still apply, but
  // the safe-looking allowlist is intentionally bypassed so complex SWE commands (pip install,
  // npm install, custom test runners, …) are not blocked.
  const humanRouter = MODE === "ask_human"
    ? createHumanInputRouter({
        instanceId: uid,
        kbPath: path.join(TASK_DIR, "blocker_registry.json"),
        trajectoryFile: pushEvent,
        workspaceDir: WORKSPACE,
        approvalPolicy: "allow",
        ...(ASK_HUMAN_BASE_URL ? { baseUrl: ASK_HUMAN_BASE_URL } : {}),
        ...(ASK_HUMAN_MODEL ? { modelId: ASK_HUMAN_MODEL } : {}),
      })
    : null;

  // 5. Run agent with up to 3 attempts
  // Retries occur only on transient SDK errors; timeouts and clean completions
  // exit immediately.  Each attempt re-uses the same humanRouter and pushEvent
  // callback but clears allEvents so only the successful attempt's events are
  // preserved in the final trajectory.
  let sdkError = null;
  const MAX_RETRIES = 3;
  const _runStart = Date.now();

  const env = claudeApiEnv();

  for (let _attempt = 1; _attempt <= MAX_RETRIES; _attempt++) {
    sdkError = null;
    allEvents.length = 0;   // clear in-place so pushEvent closure remains valid

    const PER_ATTEMPT_TIMEOUT_MS = 1_200_000; // 1200 s = 20 min per attempt
    const remainingMs    = TIMEOUT_MS - (Date.now() - _runStart);
    const attemptTimeout = Math.min(remainingMs, PER_ATTEMPT_TIMEOUT_MS);
    if (attemptTimeout <= 0) {
      sdkError = `Timed out after ${TIMEOUT_MS}ms`;
      pushEvent({ type: "sdk_error", timestamp: new Date().toISOString(), error: sdkError });
      break;
    }

    const abortController = new AbortController();
    const timeoutId = setTimeout(
      () => abortController.abort(new Error(`SWE claude attempt timed out after ${PER_ATTEMPT_TIMEOUT_MS}ms`)),
      attemptTimeout,
    );

    try {
      for await (const message of query({
        prompt,
        options: {
          abortController,
          pathToClaudeCodeExecutable: CLAUDE_BIN,
          cwd: WORKSPACE,
          model: CLAUDE_MODEL,
          maxTurns: MAX_TURNS,
          permissionMode: PERMISSION_MODE,
          env,
          mcpServers: [],
          canUseTool: async (_toolName, _input, permission) => {
            // Native AskUserQuestion: intercept and route through the ask_human simulator
            // (ask_human mode) or deny cleanly (full_info mode, where all information is
            // provided upfront and no human is present to answer questions).
            if (isAskUserQuestionTool(_toolName)) {
              if (humanRouter) {
                // ask_human mode: route to the LLM-backed human simulator.
                const answer = await answerClaudeAskUserQuestion({ router: humanRouter, input: _input, permission });
                // Emit a structured event so extractTrajectorySteps can record the
                // ask/answer pair in trajectory.json with a clean obs.  We deny the
                // tool (so it never blocks on stdin) and pass the answer as the deny
                // message.  If the SDK also injects a synthetic tool_result for the
                // deny, extractTrajectorySteps will skip it via the handledAskIds set.
                const q = _input?.question || _input?.questions?.[0]?.question || JSON.stringify(_input || {});
                pushEvent({
                  type:        "claude_ask_question",
                  timestamp:   new Date().toISOString(),
                  question:    String(q),
                  answer,
                  tool_use_id: permission.toolUseID,
                });
                return {
                  behavior: "deny",
                  toolUseID: permission.toolUseID,
                  message:   answer,
                  decisionClassification: "user_temporary",
                };
              }
              // full_info mode: no human is present (all blockers are provided in the prompt).
              // We cannot let AskUserQuestion execute natively — it would block on stdin in a
              // non-TTY container.  Return the same UNKNOWN_RESOLUTION ("irrelevant question")
              // that ask_human_server.py returns for unmatched questions.  This keeps the
              // trajectory observation clean and consistent across modes: the model sees
              // "irrelevant question" and understands it should proceed without asking.
              // We still push a structured event (with question + tool_use_id) so:
              //   1. extractTrajectorySteps can record the attempt as an ask_human step.
              //   2. computeTrajectoryStats can increment num_questions_full_info.
              //   3. handledAskIds suppresses the SDK's synthetic tool_result duplicate.
              {
                const q = _input?.question || _input?.questions?.[0]?.question || JSON.stringify(_input || {});
                pushEvent({
                  type:        "ask_question_full_info_mode",
                  timestamp:   new Date().toISOString(),
                  question:    String(q),
                  tool_use_id: permission.toolUseID,
                  toolName:    _toolName,
                  input:       _input,
                });
              }
              return {
                behavior: "deny",
                toolUseID: permission.toolUseID,
                message: UNKNOWN_RESOLUTION,   // "irrelevant question" — canonical ask_human_server.py response
                decisionClassification: "user_temporary",
              };
            }

            // All other tools (Bash, Read, Edit, Glob, …): silently enforce the workspace
            // boundary and allow everything inside /app.  No approval event is logged — the
            // container IS the security boundary and tool usage is not an "approval question."
            const decision = approvalPolicyRouter({
              registryDecision: { status: "unknown", decision: "unknown" },
              nativeEventType: "claude.canUseTool",
              context: { toolName: _toolName, input: _input, blockedPath: permission.blockedPath },
              workspaceDir: WORKSPACE,
              policy: "allow",
            });
            if (!decision.allowed) {
              // Log workspace hard-denies for debugging — these are real enforcement events.
              pushEvent({
                type: "workspace_hard_deny",
                timestamp: new Date().toISOString(),
                toolName: _toolName,
                reason: decision.reason,
                blockedPath: permission.blockedPath || null,
              });
              return { behavior: "deny", toolUseID: permission.toolUseID, message: `Denied: ${decision.reason}`, decisionClassification: "user_temporary" };
            }
            return { behavior: "allow", updatedInput: _input || {}, toolUseID: permission.toolUseID, decisionClassification: "user_temporary" };
          },
          systemPrompt: MODE === "ask_human"
            ? {
                type: "preset",
                preset: "claude_code",
                append: ASK_HUMAN_GUIDANCE,
              }
            : { type: "preset", preset: "claude_code" },
        },
      })) {
        pushEvent({ type: "sdk_message", timestamp: new Date().toISOString(), message });
      }
    } catch (error) {
      const text = redactString(String(error?.stack || error));
      sdkError = abortController.signal.aborted
        ? `Timed out after ${TIMEOUT_MS}ms.\n\n${text}`
        : text;
      pushEvent({ type: "sdk_error", timestamp: new Date().toISOString(), error: sdkError });
    } finally {
      clearTimeout(timeoutId);
    }

    // Retry only on transient errors (not on timeout — the overall wall-clock budget is exhausted).
    if (sdkError && !abortController.signal.aborted && _attempt < MAX_RETRIES) {
      process.stderr.write(
        `[run_claude] sdk_error on attempt ${_attempt}/${MAX_RETRIES} for ${uid}, retrying: ${sdkError.slice(0, 200)}\n`,
      );
      continue;
    }
    break;
  }

  // 6. Collect patch
  const patch = await gitDiff(WORKSPACE);
  await writeText(path.join(OUTPUT_DIR, "patch.diff"), patch);

  // 6b. Post-process trajectory: extract {act, obs, thought} steps and compute stats.
  pushEvent({ type: "attempt_end", timestamp: new Date().toISOString(), uid, patch_bytes: Buffer.byteLength(patch), sdk_error: sdkError || null });
  const trajectorySteps = extractTrajectorySteps(allEvents);

  // Load actual blocker count from blocker_registry.json for ask metrics
  let numBlockersTotal = 0;
  try {
    const regPath = path.join(TASK_DIR, "blocker_registry.json");
    const reg = JSON.parse(await fs.readFile(regPath, "utf8"));
    numBlockersTotal = (reg.entries || reg.blockers || []).length;
  } catch { /* ignore — stats will show 0 */ }

  const stats = computeTrajectoryStats(allEvents, trajectorySteps, numBlockersTotal);
  await writeJson(path.join(OUTPUT_DIR, "trajectory.json"), trajectorySteps);
  await writeJson(path.join(OUTPUT_DIR, "stats.json"), stats);

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
