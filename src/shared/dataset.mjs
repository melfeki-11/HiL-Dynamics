import { readJsonl } from "./io.mjs";

export const DEFAULT_CLARIFICATION_INSTRUCTION_PROFILE = "current";
export const CLARIFICATION_INSTRUCTION_PROFILES = new Set([DEFAULT_CLARIFICATION_INSTRUCTION_PROFILE, "generic-v1", "balanced-v2"]);
export const GENERIC_CLARIFICATION_PROMPT =
  "A human collaborator is available through the harness clarification channel. " +
  "First inspect the repository, tests, and task context. " +
  "If you are unsure how to proceed because required context is missing, ambiguous, or contradictory and cannot be resolved locally, ask one concise clarifying question. " +
  "Do not ask for information you can determine with tools, and do not ask broad exploratory questions. " +
  "Incorporate any answer and continue; if no useful answer is available, proceed with the safest documented assumption.";

const MODEL_HIDDEN_FIELDS = new Set([
  "patch",
  "test_patch",
  "fail_to_pass",
  "pass_to_pass",
  "clone_repo",
  "hil_bench_attempt_id",
  "hil_bench_mode",
  "hil_bench_row_index",
  "hil_bench_source_zip",
  "hil_bench_split",
  "hil_bench_task_id",
]);

export async function loadSamples(file) {
  return readJsonl(file);
}

export function publicMetadata(row) {
  const out = {};
  for (const [key, value] of Object.entries(row)) {
    if (MODEL_HIDDEN_FIELDS.has(key)) continue;
    out[key] = value;
  }
  return out;
}

export function normalizeClarificationInstructionProfile(profile = DEFAULT_CLARIFICATION_INSTRUCTION_PROFILE) {
  const normalized = profile || DEFAULT_CLARIFICATION_INSTRUCTION_PROFILE;
  if (!CLARIFICATION_INSTRUCTION_PROFILES.has(normalized)) {
    throw new Error(`Unknown clarification instruction profile: ${normalized}`);
  }
  return normalized;
}

export function isHilBenchRow(row) {
  return Boolean(row.hil_bench_source_zip || row.hil_bench_split || row.hil_bench_task_id);
}

export function clarificationToolName(harnessName) {
  if (harnessName === "claude-code") return "human_input.ask_human";
  if (harnessName === "codex") return "request_user_input";
  if (harnessName === "opencode") return "human_input.ask_human";
  return "the harness clarification tool";
}

export function clarificationInstruction({ harnessName, profile = DEFAULT_CLARIFICATION_INSTRUCTION_PROFILE, system = false } = {}) {
  normalizeClarificationInstructionProfile(profile);
  void harnessName;
  return system ? GENERIC_CLARIFICATION_PROMPT : `\n${GENERIC_CLARIFICATION_PROMPT}\n`;
}

export function clarificationPostMetadataReminder({ harnessName, profile = DEFAULT_CLARIFICATION_INSTRUCTION_PROFILE } = {}) {
  normalizeClarificationInstructionProfile(profile);
  void harnessName;
  return "";
}

export function promptForInstance(row, options = {}) {
  const metadata = publicMetadata(row);
  const isHilBench = isHilBenchRow(row);
  const taskKind = isHilBench ? "realistic software engineering" : "SWE-bench Pro";
  const promptMode = options.mode || (row.hil_bench_mode === "full_info" ? "full_info" : "ask_human");
  const includeClarification = isHilBench && promptMode === "ask_human";
  const clarificationText = includeClarification
    ? clarificationInstruction({
        harnessName: options.harnessName,
        profile: options.clarificationInstructionProfile,
      })
    : "";
  const clarificationReminder = includeClarification
    ? clarificationPostMetadataReminder({
        harnessName: options.harnessName,
        profile: options.clarificationInstructionProfile,
      })
    : "";
  return `You are solving a ${taskKind} task.

Repository: ${row.repo}
Base commit: ${row.base_commit}
Instance ID: ${row.instance_id}

Work in the checked-out repository. Make the minimal code change needed to satisfy the issue. Do not modify tests unless the production fix genuinely requires it. At the end, leave the working tree containing only the intended patch.
${clarificationText}

Public task metadata:

\`\`\`json
${JSON.stringify(metadata, null, 2)}
\`\`\`
${clarificationReminder}
`;
}
