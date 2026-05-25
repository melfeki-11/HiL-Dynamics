#!/usr/bin/env node
/**
 * SWE harness entrypoint for claude-code — runs INSIDE a hilbench-swe-harness:<uid> container.
 *
 * Layout inside the container:
 *   /task/              (bind-mounted ro) task data: metadata.json, problem_statement.txt,
 *                                         blocker_registry.json, run_script.sh, parser.py
 *   /output/            (bind-mounted rw) trajectory.json, stats.json, patch.diff, result.json, attempt.json
 *   /app/               (built into image) repo at base commit — agent's workspace (/testbed is a symlink to /app)
 *   /opt/hil_dynamics/ (built into image) node_modules; src/ is bind-mounted ro
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
 *   CLAUDE_REASONING_EFFORT reasoning effort (low|medium|high|xhigh|max), optional
 *   MAX_STEPS             max agent steps (default: 0 = unbounded)
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
import { createSdkMcpServer, query, tool } from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";
import { approvalPolicyRouter, UNKNOWN_RESOLUTION, CANT_ANSWER, UNKNOWN_BLOCKER_ID, ASK_HUMAN_REQUEST_TYPES, APPROVAL_REQUEST_TYPES } from "../shared/human_input.mjs";
import { ensureDir, writeJson, writeText } from "../shared/io.mjs";
import { redactString } from "../shared/redact.mjs";
import { buildSwePrompt } from "./prompt.mjs";
import {
  installClaudeSkill,
  removeInstalledAskHumanSkills,
  SKILL_TOOL_REF,
} from "./skills.mjs";
import {
  WORKSPACE, TASK_DIR, OUTPUT_DIR,
  MODE, ASK_HUMAN_ENABLED, SKILL_ENABLED, FULL_INFO_ENABLED,
  SKILL_TEMPLATE_VERSION, ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION,
  ASK_HUMAN_GUIDANCE_ENABLED, PASS_INDEX, RUN_ID, TIMEOUT_MS,
  LITELLM_CALL_TIMEOUT_MS, STEP_LITELLM_TRIES,
  ASK_HUMAN_BASE_URL, ASK_HUMAN_MODEL, buildAskHumanGuidance,
  richAskHumanToolDescriptionForHarness,
  THOUGHT_CAP, ACT_CAP, OBS_CAP, cap, computeResourceStats, gitDiff,
} from "./constants.mjs";
import { sidecarAsk, startAskHumanSidecar, stopSidecar } from "./ask_human_sidecar_client.mjs";

// Claude's native question-asking tool is AskUserQuestion. Guidance is an
// explicit diagnostic flag, not part of ask_human by default.
const ASK_HUMAN_GUIDANCE = buildAskHumanGuidance("AskUserQuestion and/or ask_human");

// ── Configuration from env ──────────────────────────────────────────────────

const CLAUDE_MODEL    = process.env.CLAUDE_MODEL  || "claude-sonnet-4-6";
const CLAUDE_REASONING_EFFORT = (
  process.env.CLAUDE_REASONING_EFFORT ||
  process.env.CLAUDE_EFFORT || // backward-compat alias
  ""
).trim().toLowerCase();
const MAX_STEPS       = Number(process.env.MAX_STEPS || "0");
// "acceptEdits" auto-approves file edits while still letting canUseTool fire for
// shell/MCP/AskUserQuestion calls so we can intercept them.  bypassPermissions would
// skip the canUseTool callback for some tool types entirely.
const PERMISSION_MODE = process.env.PERMISSION_MODE || "acceptEdits";
const CLAUDE_BIN      = process.env.CLAUDE_CODE_EXECUTABLE || "claude";
const WITH_CUSTOM_TOOL = ASK_HUMAN_ENABLED && /^(1|true|yes|on)$/i.test(String(process.env.WITH_CUSTOM_TOOL ?? "0"));
const VALID_CLAUDE_EFFORTS = new Set(["low", "medium", "high", "xhigh", "max"]);
if (CLAUDE_REASONING_EFFORT && !VALID_CLAUDE_EFFORTS.has(CLAUDE_REASONING_EFFORT)) {
  throw new Error(
    `Invalid CLAUDE_REASONING_EFFORT='${CLAUDE_REASONING_EFFORT}'. ` +
    `Must be one of: ${[...VALID_CLAUDE_EFFORTS].join(", ")}.`,
  );
}

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
  // LiteLLM proxy / judge server on the host.  The orchestrator adds
  // --add-host=host.docker.internal:host-gateway to make this work.
  baseUrl = baseUrl.replace(/\blocalhost\b/g, "host.docker.internal");
  return {
    ...process.env,
    ANTHROPIC_AUTH_TOKEN: token,
    ANTHROPIC_BASE_URL: baseUrl,
    LITELLM_BASE_URL: baseUrl,
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
  if (depth > 6 || value == null) return false;
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
    s.includes("max_output_tokens") ||
    s.includes("max output token") ||
    s.includes("max_tokens") ||
    s.includes("token limit") ||
    s.includes("generation exceeded max tokens") ||
    s.includes("generation exceeded the maximum output token limit") ||
    s.includes("context window") ||
    s.includes("context length")
  );
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

function isCustomAskHumanTool(toolName) {
  return String(toolName || "") === "mcp__human_input__ask_human";
}

function appendSidecarEvents(result, pushEvent) {
  for (const ev of Array.isArray(result?.events) ? result.events : []) {
    pushEvent?.(ev);
  }
}

function createCustomAskHumanMcpServer({ sidecarUrl, pushEvent }) {
  const toolDesc =
    richAskHumanToolDescriptionForHarness() ??
    "Ask a focused clarification question about task requirements.";
  return createSdkMcpServer({
    name: "human_input",
    version: "0.1.0",
    alwaysLoad: true,
    tools: [
      tool(
        "ask_human",
        toolDesc,
        {
          question: z.string(),
          request_type: z.enum(["clarification", "elicitation"]).optional(),
          options: z.array(z.object({ label: z.string(), description: z.string().optional() })).optional(),
        },
        async (input) => {
          const question = String(input?.question || "");
          const requestType = input?.request_type || "clarification";
          try {
            const result = await sidecarAsk({
              sidecarUrl,
              question,
              requestType,
              nativeEventType: "claude.mcp.ask_human",
              rawEvent: input,
              options: input.options || [],
              context: { source: "claude_mcp_tool" },
              fallbackSource: "claude_mcp_tool_error_fallback",
            });
            appendSidecarEvents(result, pushEvent);
            const resolution = String(result.resolution ?? UNKNOWN_RESOLUTION);
            return { content: [{ type: "text", text: resolution }] };
          } catch (err) {
            // Best-effort tool failure handling: keep the agent loop alive and
            // return the canonical retryable answer text.
            const requestId = `claude_customtool_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
            // Emit synthetic raw/result events for auditability. computeTrajectoryStats
            // explicitly excludes status="error" requests from num_questions.
            pushEvent({
              type: "human_input_raw_event",
              timestamp: new Date().toISOString(),
              request_id: requestId,
              request_type: requestType,
              native_event_type: "claude.mcp.ask_human",
              question,
              options: input.options || [],
              context: { source: "claude_mcp_tool_error_fallback" },
              raw_event: input,
            });
            pushEvent({
              type: "human_input_result",
              timestamp: new Date().toISOString(),
              request_id: requestId,
              request_type: requestType,
              native_event_type: "claude.mcp.ask_human",
              result: {
                resolution: CANT_ANSWER,
                selected_labels: [CANT_ANSWER],
                blocker_id: UNKNOWN_BLOCKER_ID,
                status: "error",
              },
            });
            pushEvent({
              type: "custom_ask_human_tool_error",
              timestamp: new Date().toISOString(),
              error: String(err?.message || err),
            });
            return {
              content: [{ type: "text", text: CANT_ANSWER }],
              isError: true,
            };
          }
        },
        {
          alwaysLoad: true,
          annotations: { readOnlyHint: true },
        },
      ),
    ],
  });
}

async function answerClaudeAskUserQuestion({ sidecarUrl, input, permission, pushEvent }) {
  const questions = Array.isArray(input?.questions) ? input.questions : [input];
  const answerParts = [];
  // AskUserQuestionOutput.answers is keyed by question text (not header).
  // This is the structured map passed back via updatedInput so Claude Code
  // formats the tool result as "question=answer" pairs for the model.
  const answersMap = {};
  let hitJudgeAny = false;
  for (const question of questions) {
    const prompt = typeof question?.question === "string" ? question.question : "";
    let answerStr;

    const result = await sidecarAsk({
      sidecarUrl,
      question: prompt,
      requestType: "clarification",
      nativeEventType: "claude.AskUserQuestion.canUseTool",
      rawEvent: { input, permission: serializablePermission(permission) },
      options: question?.options || [],
      context: { source: "claude_builtin_AskUserQuestion" },
      fallbackSource: "claude_native_ask_user_question_error_fallback",
    });
    appendSidecarEvents(result, pushEvent);
    answerStr = String(result.resolution ?? UNKNOWN_RESOLUTION);
    hitJudgeAny = true;

    answerParts.push(`${prompt}\n${answerStr}`);
    answersMap[prompt] = answerStr;
  }
  return { answerText: answerParts.join("\n\n"), answers: answersMap, questions, hitJudgeAny };
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
  let q = "";
  if (typeof toolInput?.question === "string") q = toolInput.question;
  else if (typeof toolInput?.questions?.[0]?.question === "string") q = toolInput.questions[0].question;
  else if (typeof toolInput === "string") q = toolInput;
  else {
    try { q = JSON.stringify(toolInput || {}); } catch { q = ""; }
  }
  if (isCustomAskHumanTool(name)) return `ask_human [custom_tool] ${q}`;
  if (isAskUserQuestionTool(name)) return `ask_human [native] ${q}`;
  if (/ask_human/i.test(name)) return `ask_human [other] ${q}`;
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
 *   - At the end, flush any unmatched pending calls (e.g. interrupted before result arrived).
 */
function extractTrajectorySteps(events, { stopReason = "", sdkErrorMsg = "" } = {}) {
  const steps = [];
  const pending    = new Map(); // toolUseId → { act, thought }
  // tool_use_ids that were already emitted via a claude_ask_question event;
  // any SDK-injected synthetic tool_result for these ids should be skipped to
  // prevent duplicate trajectory steps.
  const handledAskIds = new Set();

  for (const event of events) {
    // ── Ask/answer pairs from AskUserQuestion handling (ask_human mode) ──────
    // AskUserQuestion is handled via behavior:"allow" + pre-filled updatedInput.answers
    // so the model receives a proper tool-success result, not a deny error.
    // The claude_ask_question event is pushed by canUseTool after the LLM judge
    // returns the answer, giving us a clean question + answer pair.
    // We consume the pending entry here and add its id to handledAskIds so the
    // SDK's real tool_result for AskUserQuestion is skipped below (no duplicate).
    if (event.type === "claude_ask_question") {
      const p = pending.get(event.tool_use_id);
      const pairs = Array.isArray(event.qa_pairs) && event.qa_pairs.length
        ? event.qa_pairs
        : [{ question: event.question, answer: event.answer }];
      let first = true;
      for (const pair of pairs) {
        steps.push({
          thought: first ? (p?.thought ?? "") : "",
          act:     cap(`ask_human [native] ${pair?.question || ""}`, ACT_CAP),
          obs:     cap(String(pair?.answer ?? ""), OBS_CAP),
        });
        first = false;
      }
      pending.delete(event.tool_use_id);
      handledAskIds.add(event.tool_use_id);
      continue;
    }

    // ── full_info mode questions ──────────────────────────────────────────────
    // In full_info mode the agent may still call AskUserQuestion; the handler
    // injects irrelevant answers (behavior:"allow") and pushes this event so we capture the
    // attempt in the trajectory (act: "ask_human …", obs: "irrelevant question").
    // Adding the id to handledAskIds ensures the SDK's synthetic tool_result does
    // not create a duplicate trajectory step.
    if (event.type === "ask_question_full_info_mode") {
      const p = pending.get(event.tool_use_id);
      const pairs = Array.isArray(event.qa_pairs) && event.qa_pairs.length
        ? event.qa_pairs
        : [{ question: event.question, answer: UNKNOWN_RESOLUTION }];
      let first = true;
      for (const pair of pairs) {
        steps.push({
          thought: first ? (p?.thought ?? "") : "",
          act:     cap(`ask_human [native] ${pair?.question || ""}`, ACT_CAP),
          obs:     cap(String(pair?.answer ?? UNKNOWN_RESOLUTION), OBS_CAP),
        });
        first = false;
      }
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
          // Skip tool_results already captured via claude_ask_question.
          // AskUserQuestion allow+answers produces a real tool_result; without
          // this guard we would emit a duplicate trajectory step.
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

  // Flush tool calls that never got a result (e.g. interrupted mid-run).
  for (const [, p] of pending) {
    let interruptedObs = "[no observation — tool call was denied or interrupted]";
    if (String(stopReason || "").trim()) {
      interruptedObs += ` (stop_reason=${String(stopReason).trim()})`;
    }
    const firstLine = String(sdkErrorMsg || "").trim().split("\n")[0] || "";
    if (firstLine) {
      interruptedObs += ` (${cap(firstLine, 300)})`;
    }
    steps.push({ thought: p.thought, act: p.act, obs: interruptedObs });
  }
  pending.clear();

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
  let numAskHumanCapped    = 0;
  let numAskHumanCooldownDenied = 0;
  const resolvedBlockerIds = new Set();
  const seenRawEventIds    = new Set();
  const seenResultEventIds = new Set();
  const requestMetaById    = new Map();
  const requestStatusById  = new Map();

  // Build a request_id → terminal status map so failed ask_human invocations
  // (e.g. tool/bridge failures with status="error") are not counted as real
  // clarification/approval questions.
  for (const ev of events) {
    if (ev.type !== "human_input_result") continue;
    const rid = String(ev.request_id || "");
    if (!rid) continue;
    const status = String(ev.result?.status || "unknown").toLowerCase();
    requestStatusById.set(rid, status);
  }

  // One native AskUserQuestion use should count as one question regardless of
  // how many sub-questions were bundled — but ask-limit suppressions bypass the judge
  // and must not inflate num_questions.
  for (const ev of events) {
    if (ev.type === "claude_ask_question" && ev.hit_judge_for_native_ask !== false) numQuestions++;
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

      // Count custom MCP ask_human usage (one raw event = one tool use).
      if (ev.native_event_type === "claude.mcp.ask_human") {
        numQuestions++;
      } else if (APPROVAL_REQUEST_TYPES.has(ev.request_type)) {
        numQuestionsApproval++;
      }
    }

    if (ev.type === "human_input_result") {
      const rid = String(ev.request_id || "");
      if (rid && seenResultEventIds.has(rid)) continue;
      if (rid) seenResultEventIds.add(rid);

      // Ignore any non-clarification result types in core ask metrics.
      const meta = rid ? requestMetaById.get(rid) : null;
      if (meta?.request_type && !ASK_HUMAN_REQUEST_TYPES.has(meta.request_type)) continue;

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
    num_ask_human_capped:    numAskHumanCapped,
    num_ask_human_cooldown_denied: numAskHumanCooldownDenied,
    num_blockers_resolved:   resolvedBlockerIds.size,
    num_blockers_total:      numBlockersTotal,
    stats_schema_version:    2,
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
  const runStartedAtMs = Date.now();

  // 2. Build prompt
  let blockers = [];
  if (FULL_INFO_ENABLED) {
    const registryPath = path.join(TASK_DIR, "blocker_registry.json");
    const registry = JSON.parse(await fs.readFile(registryPath, "utf8"));
    blockers = (registry.entries || registry.blockers || []).map((e) => ({
      description: e.description,
      resolution: e.resolution,
    }));
  }
  const prompt = buildSwePrompt({ problemStatement, mode: MODE, blockers });
  await removeInstalledAskHumanSkills(WORKSPACE);
  if (SKILL_ENABLED) {
    await installClaudeSkill(WORKSPACE, SKILL_TOOL_REF.claude);
  }

  // 3. Write attempt metadata
  const attemptMeta = {
    run_id: RUN_ID,
    uid,
    mode: MODE,
    pass_index: PASS_INDEX,
    harness: "claude-code",
    model: CLAUDE_MODEL,
    max_steps: MAX_STEPS > 0 ? MAX_STEPS : null,
    timeout_ms: TIMEOUT_MS,
    workspace: WORKSPACE,
    task_dir: TASK_DIR,
    output_dir: OUTPUT_DIR,
    started_at: new Date().toISOString(),
    prompt,
    ask_human_tool_enabled: ASK_HUMAN_ENABLED,
    skill_enabled: SKILL_ENABLED,
    guidance_enabled: ASK_HUMAN_GUIDANCE_ENABLED,
    with_skill: SKILL_TEMPLATE_VERSION || "",
    with_ask_guidance: ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION || "",
    ask_human_model: ASK_HUMAN_MODEL,
    with_custom_tool: WITH_CUSTOM_TOOL,
    ask_human_backend: ASK_HUMAN_ENABLED ? "ask_human_sidecar" : null,
    native_question_routing: ASK_HUMAN_ENABLED ? "AskUserQuestion -> ask_human_sidecar" : "AskUserQuestion -> irrelevant_question",
  };
  await writeJson(path.join(OUTPUT_DIR, "attempt.json"), attemptMeta);
  pushEvent({ type: "attempt_start", timestamp: new Date().toISOString(), uid, mode: MODE, pass_index: PASS_INDEX, prompt });

  // 4. Run agent with up to 3 attempts.
  // Retries occur only on transient SDK errors; timeouts and clean completions
  // exit immediately. Each attempt starts its own ask_human sidecar so native
  // AskUserQuestion and the optional explicit MCP ask_human tool share the same
  // backend path as ADK/OpenCode and no sidecar events leak across retries.
  let sdkError = null;
  let stopReason = "complete";
  const MAX_RETRIES = STEP_LITELLM_TRIES;
  const _runStart = Date.now();

  const env = claudeApiEnv();

  let numBlockersTotal = 0;
  try {
    const regPath = path.join(TASK_DIR, "blocker_registry.json");
    const reg = JSON.parse(await fs.readFile(regPath, "utf8"));
    numBlockersTotal = (reg.entries || reg.blockers || []).length;
  } catch { /* stats will show 0 */ }

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
      () => abortController.abort(new Error(`SWE claude attempt timed out after ${PER_ATTEMPT_TIMEOUT_MS}ms`)),
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

      const customAskHumanMcp = WITH_CUSTOM_TOOL
        ? createCustomAskHumanMcpServer({ sidecarUrl, pushEvent })
        : null;

      for await (const message of query({
        prompt,
        options: {
          abortController,
          pathToClaudeCodeExecutable: CLAUDE_BIN,
          cwd: WORKSPACE,
          model: CLAUDE_MODEL,
          ...(CLAUDE_REASONING_EFFORT ? { effort: CLAUDE_REASONING_EFFORT } : {}),
          ...(MAX_STEPS > 0 ? { maxTurns: MAX_STEPS } : {}),
          permissionMode: PERMISSION_MODE,
          env,
          mcpServers: customAskHumanMcp ? { human_input: customAskHumanMcp } : [],
          canUseTool: async (_toolName, _input, permission) => {
            // Explicit MCP ask_human tool shares the same sidecar backend as the
            // native AskUserQuestion path. Set WITH_CUSTOM_TOOL=0 to hide it.
            // Let it run directly; the tool handler itself routes to the same
            // ask-human backend and records structured events.
            if (isCustomAskHumanTool(_toolName)) {
              return {
                behavior: "allow",
                updatedInput: _input || {},
                toolUseID: permission.toolUseID,
                decisionClassification: "user_temporary",
              };
            }

            // Native AskUserQuestion: intercept and route through the ask_human simulator
            // (ask_human mode) or short-circuit with irrelevant answers (full_info mode).
            if (isAskUserQuestionTool(_toolName)) {
              if (ASK_HUMAN_ENABLED) {
                // ask_human mode: route to the LLM-backed human simulator.
                const {
                  answerText, answers, questions: qs, hitJudgeAny,
                } = await answerClaudeAskUserQuestion({
                  sidecarUrl,
                  input: _input,
                  permission,
                  pushEvent,
                });
                // Emit a structured event so extractTrajectorySteps records the
                // ask/answer pair in trajectory.json with a clean obs.
                let q = "";
                if (typeof _input?.question === "string") q = _input.question;
                else if (typeof _input?.questions?.[0]?.question === "string") q = _input.questions[0].question;
                else {
                  try { q = JSON.stringify(_input || {}); } catch { q = ""; }
                }
                const qaPairs = (Array.isArray(qs) ? qs : []).map((qq) => {
                  const prompt = typeof qq?.question === "string" ? qq.question : "";
                  return { question: prompt, answer: String(answers[prompt] ?? UNKNOWN_RESOLUTION) };
                });
                pushEvent({
                  type:        "claude_ask_question",
                  timestamp:   new Date().toISOString(),
                  question:    String(q),
                  answer:      answerText,
                  qa_pairs:    qaPairs,
                  tool_use_id: permission.toolUseID,
                  source:      "native",
                  hit_judge_for_native_ask: hitJudgeAny,
                });
                // Return behavior:"allow" with pre-filled answers so the model receives
                // a proper AskUserQuestion tool-success result:
                //   "User has answered your questions: \"q?\"=\"answer\". You can now continue..."
                // This is the correct SDK pattern (allow + updatedInput.answers keyed by
                // question text).  It does NOT block on stdin — the answers are already
                // supplied, so Claude Code returns the tool result immediately.
                // The handledAskIds guard in extractTrajectorySteps suppresses the SDK's
                // real tool_result so we don't emit a duplicate trajectory step.
                return {
                  behavior:    "allow",
                  updatedInput: { questions: qs, answers },
                  toolUseID:   permission.toolUseID,
                  decisionClassification: "user_temporary",
                };
              }
              // full_info mode: same SDK pattern as ask_human (behavior:"allow" +
              // pre-filled answers) so the tool does not surface as a hard deny, but every
              // answer is UNKNOWN_RESOLUTION ("irrelevant question").  Native execution is
              // still avoided — answers are injected before any stdin prompt.
              const questions = Array.isArray(_input?.questions) ? _input.questions : [_input || {}];
              const answers = {};
              const qaPairs = [];
              for (const qq of questions) {
                const promptText = typeof qq?.question === "string" ? qq.question : "";
                answers[promptText] = UNKNOWN_RESOLUTION;
                qaPairs.push({ question: promptText, answer: UNKNOWN_RESOLUTION });
              }
              let q = "";
              if (typeof _input?.question === "string") q = _input.question;
              else if (typeof questions[0]?.question === "string") q = questions[0].question;
              else {
                try { q = JSON.stringify(_input || {}); } catch { q = ""; }
              }
              pushEvent({
                type:        "ask_question_full_info_mode",
                timestamp:   new Date().toISOString(),
                question:    String(q),
                qa_pairs:    qaPairs,
                tool_use_id: permission.toolUseID,
                source:      "native",
                toolName:    _toolName,
                input:       _input,
              });
              return {
                behavior:    "allow",
                updatedInput: { questions, answers },
                toolUseID:   permission.toolUseID,
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
          systemPrompt: ASK_HUMAN_ENABLED && ASK_HUMAN_GUIDANCE_ENABLED
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
      const maxTurnsReached = /Reached maximum number of turns/i.test(text);
      if (maxTurnsReached) {
        // Claude SDK surfaces maxTurns as an error result string. In this harness,
        // reaching MAX_STEPS is an expected stop condition, not an infra failure.
        sdkError = null;
        stopReason = "max_steps";
        pushEvent({ type: "max_steps_reached", timestamp: new Date().toISOString(), detail: text });
      } else if (isTokenLimitStructured(error) || isTokenLimitError(text)) {
        // Token-limit exhaustion is an agent-behavior stop condition, not infra.
        sdkError = null;
        stopReason = "token_limit";
        pushEvent({ type: "token_limit_reached", timestamp: new Date().toISOString(), detail: text });
      } else {
        sdkError = abortController.signal.aborted
          ? `Timed out after ${attemptTimeout}ms.\n\n${text}`
          : text;
        stopReason = abortController.signal.aborted ? "timeout" : "sdk_error";
        pushEvent({ type: "sdk_error", timestamp: new Date().toISOString(), error: sdkError });
      }
    } finally {
      stopSidecar(sidecarProc);
      clearTimeout(timeoutId);
    }

    // Retry transient failures, including timeout-aborted turns, up to STEP_LITELLM_TRIES.
    if (sdkError && _attempt < MAX_RETRIES) {
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
  pushEvent({
    type: "attempt_end",
    timestamp: new Date().toISOString(),
    uid,
    patch_bytes: Buffer.byteLength(patch),
    sdk_error: sdkError || null,
    stop_reason: stopReason,
  });
  const trajectorySteps = extractTrajectorySteps(allEvents, {
    stopReason,
    sdkErrorMsg: sdkError || "",
  });

  const stats = {
    ...computeTrajectoryStats(allEvents, trajectorySteps, numBlockersTotal),
    ...computeResourceStats(allEvents, trajectorySteps, runStartedAtMs),
  };
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
    stop_reason: stopReason,
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
