/**
 * Shared constants for SWE harness entrypoints (run_claude.mjs, run_codex.mjs).
 * Everything here is used identically in both harnesses.
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DEFAULT_ASK_HUMAN_MODEL, PAPER_ASK_HUMAN_MODEL_BEDROCK } from "../shared/config.mjs";

// ── Container layout ──────────────────────────────────────────────────────────

// /app is the canonical workspace in hilbench-swe images (/testbed is a symlink).
export const WORKSPACE  = "/app";
export const TASK_DIR   = process.env.TASK_DIR   || "/task";
export const OUTPUT_DIR = process.env.OUTPUT_DIR || "/output";

// ── Run identity ──────────────────────────────────────────────────────────────

export function normalizeMode(mode) {
  const raw = String(mode || "").trim();
  if (!raw) return "ask_human";
  if (["ask_human", "full_info"].includes(raw)) return raw;
  throw new Error(`Unknown MODE=${JSON.stringify(raw)}. Expected ask_human or full_info.`);
}

export const MODE       = normalizeMode(process.env.MODE || "ask_human");
export const ASK_HUMAN_ENABLED = MODE === "ask_human";
export const SKILL_TEMPLATE_VERSION = String(process.env.WITH_SKILL || "").trim();
export const SKILL_ENABLED = ASK_HUMAN_ENABLED && SKILL_TEMPLATE_VERSION.length > 0;
export const FULL_INFO_ENABLED = MODE === "full_info";
export const ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION = String(process.env.WITH_ASK_GUIDANCE || "").trim();
export const ASK_HUMAN_GUIDANCE_ENABLED = ASK_HUMAN_ENABLED && ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION.length > 0;
export const PASS_INDEX = Number(process.env.PASS_INDEX  || "1");
export const RUN_ID     = process.env.RUN_ID     || "swe-run";
export const TIMEOUT_MS = Number(process.env.ATTEMPT_TIMEOUT_MS || String(3 * 3600 * 1000));
export const LITELLM_CALL_TIMEOUT_MS = Number(process.env.LITELLM_CALL_TIMEOUT_MS || String(20 * 60 * 1000));
export const STEP_LITELLM_TRIES = Number(process.env.STEP_LITELLM_TRIES || "3");

// ── ask_human judge ───────────────────────────────────────────────────────────

// Prefer a dedicated ASK_HUMAN_BASE_URL, fall back to LiteLLM/v1.
// The selected URL is honored exactly (with localhost rewritten for containers).
export const ASK_HUMAN_BASE_URL = (() => {
  const litellm = (process.env.LITELLM_BASE_URL || "")
    .trim()
    .replace(/\/+$/, "")
    .replace(/\blocalhost\b/g, "host.docker.internal");
  const litellmV1 = litellm ? `${litellm}/v1` : "";
  const raw = (process.env.ASK_HUMAN_BASE_URL || "").trim();
  if (raw) return raw.replace(/\blocalhost\b/g, "host.docker.internal").replace(/\/+$/, "");
  return litellmV1;
})();

export const ASK_HUMAN_MODEL = process.env.ASK_HUMAN_MODEL || DEFAULT_ASK_HUMAN_MODEL;

// ── System-prompt guidance (ask_human mode only) ──────────────────────────────

// Claude:  systemPrompt.append in the query() call.
// Codex:   developerInstructions in the thread/start RPC.
// Not injected in full_info mode — all context is already in the task prompt
// and native questions are short-circuited to "irrelevant question" without ask-human guidance.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TEMPLATES_DIR = path.join(__dirname, "templates");
const TEMPLATE_VERSION_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

export const RICH_ASK_TOOL_DESC = /^(1|true|yes|on)$/i.test(
  String(process.env.RICH_ASK_TOOL_DESC || ""),
);

export const RICH_ASK_HUMAN_TOOL_DESCRIPTION = [
  "Ask a human collaborator one focused clarification question when a",
  "decision-critical requirement cannot be resolved from the task text,",
  "repository, tests, or local tools.",
  "",
  "Use one tool call per question. Phrase the question so the answer can be",
  "a concrete fact or single decision, and do not ask for information that",
  "can be found by reading nearby code or tests.",
].join("\n");

/**
 * MCP `ask_human` tool description — optional diagnostic only; default off.
 */
export function richAskHumanToolDescriptionForHarness() {
  if (!RICH_ASK_TOOL_DESC) return null;
  return RICH_ASK_HUMAN_TOOL_DESCRIPTION;
}

export function buildAskHumanGuidance(toolName) {
  if (!ASK_HUMAN_GUIDANCE_ENABLED) return null;
  if (!TEMPLATE_VERSION_RE.test(ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION)) {
    throw new Error(
      `Invalid WITH_ASK_GUIDANCE=${JSON.stringify(ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION)}. ` +
      "Use only letters, digits, dot, underscore, or hyphen.",
    );
  }
  const fileName = `${ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION}.txt`;
  const filePath = path.join(TEMPLATES_DIR, fileName);
  if (!fs.existsSync(filePath)) {
    throw new Error(
      `WITH_ASK_GUIDANCE=${JSON.stringify(ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION)} requires ${fileName} in ${TEMPLATES_DIR}.`,
    );
  }
  void toolName;
  return fs.readFileSync(filePath, "utf8");
}

// ── Trajectory extraction helpers ─────────────────────────────────────────────

export const THOUGHT_CAP = 4000; // chars
export const ACT_CAP     = 4000; // chars
export const OBS_CAP     = 8000; // chars

/**
 * Truncate a value to at most `limit` characters.
 * Always returns a string (coerces null/undefined to "").
 */
export function cap(s, limit) {
  const str = String(s || "");
  return str.length > limit ? `${str.slice(0, limit)}… [truncated]` : str;
}

export function computeResourceStats(events = [], trajectorySteps = [], startedAtMs = null, endedAtMs = Date.now()) {
  const tokenUsage = { input_tokens: 0, output_tokens: 0, total_tokens: 0, usage_records: 0 };
  for (const ev of events || []) collectTokenUsage(ev, tokenUsage);
  const explicitLlmCalls = (events || []).reduce((sum, ev) => {
    const n = numericField(ev, ["llm_call_count", "num_llm_calls", "llmCalls", "request_count"]);
    return sum + (n || 0);
  }, 0);
  const toolCalls = (trajectorySteps || []).filter((step) => String(step?.act || "").trim()).length;
  const turnsOrItems = (events || []).filter((ev) => {
    if (ev?.type === "sdk_message") return true;
    if (ev?.type === "sdk_event") {
      const method = String(ev?.event?.method || "");
      return /(^|\/)(completed|done)$/.test(method) || method === "turn/completed";
    }
    if (String(ev?.type || "").startsWith("session.")) return true;
    return false;
  }).length || (trajectorySteps || []).length;
  const wallClockMs = Number.isFinite(startedAtMs) ? Math.max(0, Number(endedAtMs) - Number(startedAtMs)) : null;
  return {
    wall_clock_ms: wallClockMs,
    num_llm_calls: explicitLlmCalls || tokenUsage.usage_records || null,
    num_tool_calls: toolCalls,
    num_turns_or_items: turnsOrItems,
    input_tokens: tokenUsage.usage_records ? tokenUsage.input_tokens : null,
    output_tokens: tokenUsage.usage_records ? tokenUsage.output_tokens : null,
    total_tokens: tokenUsage.usage_records ? tokenUsage.total_tokens : null,
  };
}

function collectTokenUsage(value, totals, seen = new Set()) {
  if (!value || typeof value !== "object") return;
  if (seen.has(value)) return;
  seen.add(value);
  const usageObj = value.usage && typeof value.usage === "object" ? value.usage : value;
  const input = numericField(usageObj, ["input_tokens", "prompt_tokens", "inputTokens", "promptTokens"]);
  const output = numericField(usageObj, ["output_tokens", "completion_tokens", "outputTokens", "completionTokens"]);
  const total = numericField(usageObj, ["total_tokens", "totalTokens"]);
  if (input !== null || output !== null || total !== null) {
    totals.usage_records += 1;
    totals.input_tokens += input || 0;
    totals.output_tokens += output || 0;
    totals.total_tokens += total || ((input || 0) + (output || 0));
  }
  if (Array.isArray(value)) {
    for (const item of value) collectTokenUsage(item, totals, seen);
    return;
  }
  for (const [key, item] of Object.entries(value)) {
    if (usageObj !== value && key === "usage") continue;
    collectTokenUsage(item, totals, seen);
  }
}

function numericField(obj, keys) {
  for (const key of keys) {
    const value = obj?.[key];
    if (Number.isFinite(value)) return Number(value);
  }
  return null;
}

// ── Git diff ──────────────────────────────────────────────────────────────────

/**
 * Capture all uncommitted changes (staged + unstaged) relative to HEAD.
 * Returns an empty string on any error.
 */
export async function gitDiff(cwd) {
  return new Promise((resolve) => {
    const child = spawn("git", ["diff", "--binary", "HEAD"], { cwd, stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (c) => { out += c; });
    child.on("close", () => resolve(out || ""));
    child.on("error", () => resolve(""));
  });
}
