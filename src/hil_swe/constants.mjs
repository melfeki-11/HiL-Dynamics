/**
 * Shared constants for SWE harness entrypoints (run_claude.mjs, run_codex.mjs).
 * Everything here is used identically in both harnesses.
 */

import { spawn } from "node:child_process";

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

// ── ask_human judge ───────────────────────────────────────────────────────────

// Prefer a dedicated ASK_HUMAN_BASE_URL (vLLM server), fall back to LiteLLM/v1.
// Rewrite localhost → host.docker.internal so the container can reach the host process.
export const ASK_HUMAN_BASE_URL = (() => {
  const raw = (process.env.ASK_HUMAN_BASE_URL || "").trim().replace(/\blocalhost\b/g, "host.docker.internal");
  if (raw) return raw;
  const litellm = (process.env.LITELLM_BASE_URL || "").trim().replace(/\/+$/, "").replace(/\blocalhost\b/g, "host.docker.internal");
  return litellm ? `${litellm}/v1` : "";
})();

export const ASK_HUMAN_MODEL = process.env.ASK_HUMAN_MODEL || process.env.PAPER_ASK_HUMAN_MODEL || undefined;

// ── System-prompt guidance (ask_human mode only) ──────────────────────────────

// Claude:  systemPrompt.append in the query() call.        toolName = "AskUserQuestion"
// Codex:   developerInstructions in the thread/start RPC.  toolName = "requestUserInput"
// Not injected in full_info mode — all context is already in the task prompt
// and the question-asking tool is denied there.
//
// The toolName parameter MUST match the agent's actual native tool name so the
// model can connect the guidance to a concrete action in its tool list.
export function buildAskHumanGuidance(toolName) {
  return `A human expert is available via the ${toolName} tool to answer questions about the implementation requirements. You **must** do the following:
      - First understand the problem given to you
      - Then think of what are the missing pieces of information, ambiguities, or contradictions present in the problem, or what are the blockers you need to know before you can start implementing
      - Then, ask the human expert for clarifications on these topics. Do NOT make assumptions or guesses, you MUST ASK!
      
      **Do not spend more than 5-10 steps trying to find the answer to a blocker in the codebase. You have very limited steps. Instead, use the ${toolName} tool to get clarification FAST.**

      Rules for using the ${toolName} tool:
      - Submit only ONE, clear, specific question at a time, targeting one specific detail. Never ask multiple questions in one tool call.
      - Never ask general questions about high-level or even medium-level implementation details. E.g. "How should I implement function X?" is a bad question that will NOT be answered by the expert. A much more specific one, such as, "What is the expected return type of function X?" CAN be answered by the expert.
      - If the expert deems your question irrelevant, but you believe it's a necessary clarification, try asking again but word, structure, or format your question differently. An irrelevant classification doesn't just come from asking a useless question; it could also be because you did not ask a specific-enough question, or because you put more than one question in one tool call.
      - If the expert answers your question, **do not ask about the same detail again.** Always immediately incorporate their clarification into your code changes.
      - Always integrate previous expert answers into your problem solving process to unblock you in your implementation or so you can ask follow-up questions.`;
}

// ── Trajectory extraction helpers ─────────────────────────────────────────────

export const THOUGHT_CAP = 4000; // chars
export const ACT_CAP     = 4000; // chars

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
