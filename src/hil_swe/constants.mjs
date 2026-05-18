/**
 * Shared constants for SWE harness entrypoints (run_claude.mjs, run_codex.mjs).
 * Everything here is used identically in both harnesses.
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DEFAULT_ASK_HUMAN_MODEL } from "../shared/config.mjs";

// ── Container layout ──────────────────────────────────────────────────────────

// /app is the canonical workspace in hilbench-swe images (/testbed is a symlink).
export const WORKSPACE  = "/app";
export const TASK_DIR   = process.env.TASK_DIR   || "/task";
export const OUTPUT_DIR = process.env.OUTPUT_DIR || "/output";

// ── Run identity ──────────────────────────────────────────────────────────────

export const MODE       = process.env.MODE       || "ask_human";
export const PASS_INDEX = Number(process.env.PASS_INDEX  || "1");
export const RUN_ID     = process.env.RUN_ID     || "swe-run";
export const TIMEOUT_MS = Number(process.env.ATTEMPT_TIMEOUT_MS || String(3 * 3600 * 1000));
export const LITELLM_CALL_TIMEOUT_MS = Number(process.env.LITELLM_CALL_TIMEOUT_MS || String(20 * 60 * 1000));
export const STEP_LITELLM_TRIES = Number(process.env.STEP_LITELLM_TRIES || "3");

// ── ask_human judge ───────────────────────────────────────────────────────────

// Prefer a dedicated ASK_HUMAN_BASE_URL (vLLM server), fall back to LiteLLM/v1.
// Local vLLM URLs (localhost / host.docker.internal) are ignored when unset or down
// so harness runs still use the LiteLLM judge proxy.
export const ASK_HUMAN_BASE_URL = (() => {
  const litellm = (process.env.LITELLM_BASE_URL || "")
    .trim()
    .replace(/\/+$/, "")
    .replace(/\blocalhost\b/g, "host.docker.internal");
  const litellmV1 = litellm ? `${litellm}/v1` : "";
  const raw = (process.env.ASK_HUMAN_BASE_URL || "").trim();
  const isLocalVllm = /^(https?:\/\/)?(localhost|127\.0\.0\.1|host\.docker\.internal)(:\d+)?/i.test(raw);
  if (raw && !isLocalVllm) {
    return raw.replace(/\blocalhost\b/g, "host.docker.internal");
  }
  return litellmV1;
})();

// vLLM-only slugs (e.g. casperhansen/...) fail on LiteLLM; use the bedrock default instead.
export const ASK_HUMAN_MODEL = (() => {
  const requested = process.env.ASK_HUMAN_MODEL || process.env.PAPER_ASK_HUMAN_MODEL || "";
  const usingLitellm = /litellm/i.test(ASK_HUMAN_BASE_URL);
  const looksVllmOnly = /casperhansen\/|\/.*-awq|-awq$/i.test(requested);
  if (usingLitellm && looksVllmOnly) return DEFAULT_ASK_HUMAN_MODEL;
  return requested || DEFAULT_ASK_HUMAN_MODEL;
})();

// ── System-prompt guidance (ask_human mode only) ──────────────────────────────

// Claude:  systemPrompt.append in the query() call.        toolName = "AskUserQuestion"
// Codex:   developerInstructions in the thread/start RPC.  toolName = "requestUserInput"
// Not injected in full_info mode — all context is already in the task prompt
// and native questions are short-circuited to "irrelevant question" without ask-human guidance.
//
// The toolName parameter MUST match the agent's actual native tool name so the
// model can connect the guidance to a concrete action in its tool list.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ASK_HUMAN_GUIDANCE_TEMPLATE_PATH = path.join(__dirname, "templates", "ask_human_guidance.txt");
const ASK_HUMAN_GUIDANCE_TEMPLATE = fs.readFileSync(ASK_HUMAN_GUIDANCE_TEMPLATE_PATH, "utf8");

// ── Recall-tweak feature flags ────────────────────────────────────────────────
//
// Each flag is independently togglable so we can ablate individual tweaks.
// All default OFF so the base guidance-only baseline is preserved.

export const SEED_BLOCKER_TODOS = /^(1|true|yes|on)$/i.test(
  String(process.env.SEED_BLOCKER_TODOS || ""),
);
export const CLAUDE_MD_HINT = /^(1|true|yes|on)$/i.test(
  String(process.env.CLAUDE_MD_HINT || ""),
);
export const RICH_ASK_TOOL_DESC = /^(1|true|yes|on)$/i.test(
  String(process.env.RICH_ASK_TOOL_DESC || ""),
);
// Tweak E — soften the “five categories mandated” wording (precision; env-gated).
export const SOFTEN_CATEGORY_MANDATE = /^(1|true|yes|on)$/i.test(
  String(process.env.SOFTEN_CATEGORY_MANDATE || ""),
);

// Tweak A — TodoWrite seed instruction (Claude only; Codex uses a softer parallel).
//
// We can't pre-seed Claude's TodoWriteTool externally via the SDK (the agent must
// invoke it itself), so we instruct it as turn-1 action. The seed list deliberately
// mirrors the 5 ground-truth blocker types so a recall ceiling of 5 is achievable.
const BLOCKER_TODOS_SEED_CLAUDE_STRICT = [
  "",
  "## Turn-1 procedure (blocker discovery — MANDATORY)",
  "",
  "BEFORE issuing any other tool call, your VERY FIRST action MUST be a single",
  "`TodoWrite` call seeded with the following five unchecked items, one per",
  "blocker category. Use these literal titles — do not paraphrase yet — and",
  "leave each item unchecked so you can return to it:",
  "",
  "1. Missing parameter values / defaults",
  "2. Unclear return type or output shape",
  "3. Ambiguous spec or contradicting tests",
  "4. Unclear scope / surface area",
  "5. Edge-case behavior (empty input, None, etc.)",
  "",
  "After seeding, walk through them one at a time. For each:",
  "- Read the problem statement and ~3-5 directly relevant code locations.",
  "- If the answer is unambiguous from the code/spec, mark the item complete",
  "  and rewrite it as a one-line note for your own use.",
  "- Otherwise, ask exactly one focused, identifier-anchored question via the",
  "  ask tool. Only after receiving an answer (or `irrelevant question`),",
  "  mark the item complete and proceed to the next category.",
  "",
  "Do not start writing code while any of the five items is unchecked.",
  "Do not consolidate categories into a single mega-question.",
].join("\n");

const BLOCKER_TODOS_SEED_CLAUDE_SOFT = [
  "",
  "## Turn-1 procedure (blocker discovery — MANDATORY)",
  "",
  "BEFORE issuing any other tool call, your VERY FIRST action MUST be a single",
  "`TodoWrite` call seeded with up to five unchecked **candidate** items — one",
  "per blocker category (use the literal titles below). Leave items unchecked until",
  "you resolve them from code or confirm them via the ask tool.",
  "",
  "1. Missing parameter values / defaults",
  "2. Unclear return type or output shape",
  "3. Ambiguous spec or contradicting tests",
  "4. Unclear scope / surface area",
  "5. Edge-case behavior (empty input, None, etc.)",
  "",
  "**Before asking anything**, read the README and 2–3 directly relevant files",
  "(`Read` / search). Resolve as many checklist items as you can from code/docs.",
  "**Only forward to the ask tool** items that remain genuinely ambiguous after",
  "that reading pass. Typical tasks expose **about 3–5 true blockers** — do NOT",
  "invent clarification questions merely to satisfy the checklist.",
  "",
  "Walk remaining items one at a time:",
  "- If unambiguous after reading, complete the checklist item locally (no ask needed).",
  "- Otherwise ONE focused identifier-anchored question per unresolved item;",
  "  after each answer (`irrelevant question` counts), complete the item and move on.",
  "",
  "Do not start writing implementation code while ambiguity remains on unresolved items.",
  "Do not consolidate multiple categories into a single mega-question.",
].join("\n");

const BLOCKER_TODOS_SEED_CODEX_STRICT = [
  "",
  "## Turn-1 procedure (blocker discovery — MANDATORY)",
  "",
  "BEFORE issuing any other action, write a markdown checklist in your reasoning",
  "with one item per blocker category and keep it pinned across turns. The five",
  "categories you MUST cover are:",
  "",
  "1. Missing parameter values / defaults",
  "2. Unclear return type or output shape",
  "3. Ambiguous spec or contradicting tests",
  "4. Unclear scope / surface area",
  "5. Edge-case behavior (empty input, None, etc.)",
  "",
  "Walk through them one at a time. For each:",
  "- Read the problem statement and ~3-5 directly relevant code locations.",
  "- If the answer is unambiguous from the code/spec, mark the item resolved",
  "  in your checklist and rewrite it as a one-line note.",
  "- Otherwise, ask exactly one focused, identifier-anchored question via the",
  "  ask tool. After the answer arrives, mark the item resolved before moving on.",
  "",
  "Do not start writing code while any of the five items is unresolved.",
  "Do not collapse categories into a single mega-question.",
].join("\n");

const BLOCKER_TODOS_SEED_CODEX_SOFT = [
  "",
  "## Turn-1 procedure (blocker discovery — MANDATORY)",
  "",
  "BEFORE other work, pin a markdown checklist with up to five **candidate** blocker",
  "items — one per category (titles below). These are hypotheses, not mandates to ask.",
  "",
  "1. Missing parameter values / defaults",
  "2. Unclear return type or output shape",
  "3. Ambiguous spec or contradicting tests",
  "4. Unclear scope / surface area",
  "5. Edge-case behavior (empty input, None, etc.)",
  "",
  "**Before any ask_human call**, read the README plus 2–3 relevant repository files;",
  "localize ambiguity to concrete identifiers. Prefer resolving checklist items via",
  "code/tests over asking. Typical tasks expose **about 3–5 true blockers** — avoid",
  "fill-in questions invented only to populate the checklist.",
  "",
  "For each unresolved item after reading:",
  "- If clarified from code/spec, resolve with a terse note.",
  "- Else ONE focused identifier-anchored question; then resolve the checklist line.",
  "",
  "Do not start implementation until remaining uncertainties are consciously chosen",
  "or answered. Never collapse categories into one umbrella question.",
].join("\n");

// Tweak D — rich description for the custom MCP ask_human tool. Carries the
// `read first, then ask one specific question` wording so the model sees
// it scoped to the tool itself rather than only in the global system prompt.
export const RICH_ASK_HUMAN_TOOL_DESCRIPTION_STRICT = [
  "Ask a human expert ONE focused, identifier-anchored question about task",
  "requirements when the answer cannot be resolved from the codebase.",
  "",
  "USE THIS TOOL WHEN, AND ONLY WHEN, you have:",
  "1. Read the problem statement carefully.",
  "2. Looked at the directly relevant code (the function under change, its",
  "   callers, and the failing/added tests).",
  "3. Identified one specific blocker — a missing parameter default, an",
  "   ambiguous return shape, a contradiction with tests, an unspecified",
  "   edge case, or an unclear surface — that is NOT answerable from the",
  "   code or spec.",
  "",
  "Question quality rules:",
  "- Submit exactly ONE question per call. Multi-question calls will be",
  "  rejected.",
  "- Anchor the question on a concrete identifier (function name, parameter",
  "  name, file path) from the codebase you just read.",
  "- Phrase the question so the answer is a fact or a single decision, not",
  "  an open-ended design discussion.",
  "- Good: `In foo.py:bar(), when 'mode' is omitted, should the default be",
  "  'strict' or 'lenient'?` Bad: `How should I implement bar()?`",
  "- If a previous question received `irrelevant question`, reword and re-ask",
  "  ONLY if the blocker is still unresolved — and only with a more specific",
  "  identifier anchor.",
  "",
  "Coverage rules:",
  "- Each task typically has 3-5 unresolved blockers spread across these",
  "  categories: missing param, unclear return, ambiguous spec, unclear",
  "  scope, edge case. Plan to ask at least one question per applicable",
  "  category — do not stop after the first answer.",
  "- Never consolidate multiple categories into one umbrella question.",
].join("\n");

const RICH_ASK_SOFT_SUFFIX = [
  "",
  "Precision rules:",
  "- An irrelevant question wastes your precision budget.",
  "- If answering your question only requires reading one more file/test, do that instead of asking.",
  "- Ask only where the codebase/spec truly leaves ambiguity after you have read neighboring context.",
].join("\n");

const RICH_ASK_COVERAGE_SOFT = [
  "Coverage rules:",
  "- Most tasks expose ~3–5 real blockers across: missing param, unclear return,",
  "  ambiguous spec, unclear scope, edge case.",
  "- Treat those categories as discovery lenses — resolve each from code/tests when possible.",
  "- Ask ONE question per blocker that survives your read pass; skip categories that are clearly resolved.",
  "- Never consolidate multiple categories into one umbrella question.",
].join("\n");

export const RICH_ASK_HUMAN_TOOL_DESCRIPTION_SOFT =
  RICH_ASK_HUMAN_TOOL_DESCRIPTION_STRICT.replace(
    /\nCoverage rules:[\s\S]*$/,
    "\n" + RICH_ASK_COVERAGE_SOFT,
  ) + RICH_ASK_SOFT_SUFFIX;

/** Back-compat wording when SOFTEN_CATEGORY_MANDATE is off */
export const RICH_ASK_HUMAN_TOOL_DESCRIPTION = RICH_ASK_HUMAN_TOOL_DESCRIPTION_STRICT;

/**
 * MCP `ask_human` tool description — depends on RICH_ASK_TOOL_DESC + SOFTEN_CATEGORY_MANDATE.
 */
export function richAskHumanToolDescriptionForHarness() {
  if (!RICH_ASK_TOOL_DESC) return null;
  return SOFTEN_CATEGORY_MANDATE ? RICH_ASK_HUMAN_TOOL_DESCRIPTION_SOFT : RICH_ASK_HUMAN_TOOL_DESCRIPTION_STRICT;
}

export function buildAskHumanGuidance(toolName, sdk = "claude") {
  const base = ASK_HUMAN_GUIDANCE_TEMPLATE.replaceAll(
    "{{TOOL_NAME}}",
    String(toolName || ""),
  );
  if (!SEED_BLOCKER_TODOS) return base;
  const blockerSeed = (() => {
    if (sdk === "codex") return SOFTEN_CATEGORY_MANDATE ? BLOCKER_TODOS_SEED_CODEX_SOFT : BLOCKER_TODOS_SEED_CODEX_STRICT;
    return SOFTEN_CATEGORY_MANDATE ? BLOCKER_TODOS_SEED_CLAUDE_SOFT : BLOCKER_TODOS_SEED_CLAUDE_STRICT;
  })();
  return base + "\n" + blockerSeed;
}

// Tweak B — per-task memory hint. Written as CLAUDE.md (Claude auto-injects)
// or AGENTS.md (Codex auto-injects) into WORKSPACE at attempt start.
//
// Composed dynamically using metadata so the hint references the actual number
// of blockers the registry expects, anchoring the model's intent register.
export function buildPerTaskMemoryHint({ uid, numBlockers, sdk }) {
  const blockerCountSentence = numBlockers > 0
    ? `This task has approximately ${numBlockers} unresolved blocker(s) drawn from the categories below.`
    : "Tasks in this benchmark typically have 3-5 unresolved blockers drawn from the categories below.";
  const toolReminder = sdk === "codex"
    ? "Use `requestUserInput` (Codex's native ask tool) or the custom `human_input.ask_human` MCP tool when blocked."
    : "Use the `AskUserQuestion` tool or the custom `human_input.ask_human` MCP tool when blocked.";
  return [
    `# HiL-SWE per-task hint (uid=${uid})`,
    "",
    blockerCountSentence,
    "",
    "Blocker categories you should expect to encounter:",
    "1. Missing parameter values or defaults",
    "2. Unclear return type / output shape",
    "3. Ambiguous spec or contradicting tests",
    "4. Unclear scope / surface area",
    "5. Edge-case behavior (empty input, None, malformed input)",
    "",
    `${toolReminder} Ask ONE focused, identifier-anchored question per blocker.`,
    "Do NOT assume answers from your prior knowledge — assumed values WILL fail the hidden tests.",
    "Do NOT consolidate multiple blocker categories into a single umbrella question.",
  ].join("\n");
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
