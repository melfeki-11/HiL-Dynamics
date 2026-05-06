#!/usr/bin/env node
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import {
  DEFAULT_CLAUDE_MODEL,
  DEFAULT_CODEX_MODEL,
  DEFAULT_CODEX_REASONING_EFFORT,
  DEFAULT_OPENCODE_MODEL,
  dataDir,
  evalsDir,
} from "../src/shared/config.mjs";

function parseArgs(argv) {
  const args = {
    input: path.join(dataDir, "swebench_pro_samples.jsonl"),
    samples: path.join(dataDir, "swebench_pro_samples.csv"),
    harness: "all",
    mode: process.env.HARNESS_MODE || "ask_human",
    runId: `passk-${new Date().toISOString().replace(/[:.]/g, "-")}`,
    k: 3,
    limit: 1,
    claudeModel: process.env.CLAUDE_CODE_MODEL || DEFAULT_CLAUDE_MODEL,
    claudeThinking: process.env.CLAUDE_CODE_THINKING || undefined,
    claudeEffort: process.env.CLAUDE_CODE_EFFORT || undefined,
    codexModel: process.env.CODEX_MODEL || DEFAULT_CODEX_MODEL,
    opencodeModel: process.env.OPENCODE_MODEL || DEFAULT_OPENCODE_MODEL,
    codexReasoningEffort: process.env.CODEX_MODEL_REASONING_EFFORT || DEFAULT_CODEX_REASONING_EFFORT,
    maxTurns: Number(process.env.HARNESS_MAX_TURNS || 40),
    concurrency: undefined,
    evalWorkers: undefined,
    attemptTimeoutMs: Number(process.env.HARNESS_ATTEMPT_TIMEOUT_MS || 900000),
    reuseExistingEval: false,
    humanKb: undefined,
    humanCache: undefined,
    askHumanModel: undefined,
    askHumanReplay: false,
    clarificationInstructionProfile: process.env.HARNESS_CLARIFICATION_INSTRUCTION_PROFILE || undefined,
    approvalPolicyRouter: undefined,
    codexTransport: undefined,
    codexApprovalPolicy: undefined,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--input") args.input = argv[++i];
    else if (arg === "--samples") args.samples = argv[++i];
    else if (arg === "--harness") args.harness = argv[++i];
    else if (arg === "--mode") args.mode = argv[++i];
    else if (arg === "--run-id") args.runId = argv[++i];
    else if (arg === "--k") args.k = Number(argv[++i]);
    else if (arg === "--limit") args.limit = Number(argv[++i]);
    else if (arg === "--claude-model") args.claudeModel = argv[++i];
    else if (arg === "--claude-thinking") args.claudeThinking = argv[++i];
    else if (arg === "--claude-effort") args.claudeEffort = argv[++i];
    else if (arg === "--codex-model") args.codexModel = argv[++i];
    else if (arg === "--opencode-model") args.opencodeModel = argv[++i];
    else if (arg === "--codex-reasoning-effort") args.codexReasoningEffort = argv[++i];
    else if (arg === "--max-turns") args.maxTurns = Number(argv[++i]);
    else if (arg === "--concurrency") args.concurrency = Number(argv[++i]);
    else if (arg === "--eval-workers") args.evalWorkers = Number(argv[++i]);
    else if (arg === "--attempt-timeout-ms") args.attemptTimeoutMs = Number(argv[++i]);
    else if (arg === "--reuse-existing-eval") args.reuseExistingEval = true;
    else if (arg === "--human-kb") args.humanKb = argv[++i];
    else if (arg === "--human-cache" || arg === "--ask-human-cache") args.humanCache = argv[++i];
    else if (arg === "--ask-human-model") args.askHumanModel = argv[++i];
    else if (arg === "--ask-human-replay") args.askHumanReplay = true;
    else if (arg === "--clarification-instruction-profile") args.clarificationInstructionProfile = argv[++i];
    else if (arg === "--approval-policy-router") args.approvalPolicyRouter = argv[++i];
    else if (arg === "--codex-transport") args.codexTransport = argv[++i];
    else if (arg === "--codex-approval-policy") args.codexApprovalPolicy = argv[++i];
    else throw new Error(`Unknown argument: ${arg}`);
  }
  if (!Number.isInteger(args.k) || args.k < 1) throw new Error("--k must be a positive integer");
  if (!Number.isInteger(args.limit) || args.limit < 1) throw new Error("--limit must be a positive integer");
  if (args.concurrency !== undefined && (!Number.isInteger(args.concurrency) || args.concurrency < 1)) {
    throw new Error("--concurrency must be a positive integer");
  }
  if (args.evalWorkers !== undefined && (!Number.isInteger(args.evalWorkers) || args.evalWorkers < 1)) {
    throw new Error("--eval-workers must be a positive integer");
  }
  if (!Number.isInteger(args.maxTurns) || args.maxTurns < 1) {
    throw new Error("--max-turns must be a positive integer");
  }
  if (args.claudeThinking !== undefined && !["adaptive", "disabled"].includes(args.claudeThinking)) {
    throw new Error("--claude-thinking must be adaptive or disabled");
  }
  if (args.claudeEffort !== undefined && !["low", "medium", "high", "xhigh", "max"].includes(args.claudeEffort)) {
    throw new Error("--claude-effort must be low, medium, high, xhigh, or max");
  }
  if (!Number.isInteger(args.attemptTimeoutMs) || args.attemptTimeoutMs < 0) {
    throw new Error("--attempt-timeout-ms must be a non-negative integer");
  }
  return args;
}

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: "inherit" });
    child.on("error", reject);
    child.on("close", (code, signal) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} ${args.join(" ")} failed with ${signal || code}`));
    });
  });
}

function selectedHarnesses(harness) {
  if (harness === "all") return ["claude-code", "codex", "opencode"];
  return [harness];
}

async function countJsonlRows(filePath) {
  const text = await fs.readFile(filePath, "utf8");
  return text.split(/\r?\n/).filter(Boolean).length;
}

async function pathExists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

const args = parseArgs(process.argv.slice(2));
const harnesses = selectedHarnesses(args.harness);
const runDir = path.join(evalsDir, args.runId);
if (args.mode === "full_info") {
  const siblingFullInfo = path.join(path.dirname(args.input), "input_full_info.jsonl");
  if (path.basename(args.input) === "input.jsonl" && (await pathExists(siblingFullInfo))) {
    args.input = siblingFullInfo;
  }
  args.humanKb = undefined;
  args.humanCache = undefined;
  args.askHumanReplay = false;
} else if (args.mode === "ask_human" && !args.humanKb) {
  const siblingKb = path.join(path.dirname(args.input), "kb.json");
  if (await pathExists(siblingKb)) args.humanKb = siblingKb;
}
const availableRows = await countJsonlRows(args.input);
if (args.limit > availableRows) {
  throw new Error(`Requested --limit ${args.limit}, but ${args.input} contains only ${availableRows} samples. Run npm run download-samples -- --limit ${args.limit} first.`);
}

console.log("SWE-bench Pro pass@k run");
console.log(`  run_id: ${args.runId}`);
console.log(`  data: ${args.input}`);
console.log(`  available_data_size: ${availableRows}`);
console.log(`  selected_data_size: ${args.limit}`);
console.log(`  k: ${args.k}`);
console.log(`  harnesses: ${harnesses.join(", ")}`);
console.log(`  mode: ${args.mode}`);
if (harnesses.includes("claude-code")) {
  console.log(`  claude-code model: ${args.claudeModel}`);
  if (args.claudeThinking) console.log(`  claude-code thinking: ${args.claudeThinking}`);
  if (args.claudeEffort) console.log(`  claude-code effort: ${args.claudeEffort}`);
}
if (harnesses.includes("codex")) {
  console.log(`  codex model: ${args.codexModel}`);
  console.log(`  codex reasoning effort: ${args.codexReasoningEffort}`);
}
if (harnesses.includes("opencode")) {
  console.log(`  opencode model: ${args.opencodeModel}`);
}
console.log(`  attempt_timeout_ms: ${args.attemptTimeoutMs}`);
console.log(`  max_turns: ${args.maxTurns}`);
if (args.humanKb) console.log(`  human_kb: ${args.humanKb}`);
if (args.clarificationInstructionProfile) console.log(`  clarification_instruction_profile: ${args.clarificationInstructionProfile}`);

const generateArgs = [
  "src/cli/generate.mjs",
  "--input",
  args.input,
  "--harness",
  args.harness,
  "--k",
  String(args.k),
  "--limit",
  String(args.limit),
  "--run-id",
  args.runId,
  "--mode",
  args.mode,
  "--claude-model",
  args.claudeModel,
  "--codex-model",
  args.codexModel,
  "--opencode-model",
  args.opencodeModel,
  "--model-reasoning-effort",
  args.codexReasoningEffort,
  "--max-turns",
  String(args.maxTurns),
  "--attempt-timeout-ms",
  String(args.attemptTimeoutMs),
];
if (args.concurrency) generateArgs.push("--concurrency", String(args.concurrency));
if (args.claudeThinking) generateArgs.push("--claude-thinking", args.claudeThinking);
if (args.claudeEffort) generateArgs.push("--claude-effort", args.claudeEffort);
if (args.humanKb) generateArgs.push("--human-kb", args.humanKb);
if (args.humanCache) generateArgs.push("--human-cache", args.humanCache);
if (args.askHumanModel) generateArgs.push("--ask-human-model", args.askHumanModel);
if (args.askHumanReplay) generateArgs.push("--ask-human-replay");
if (args.clarificationInstructionProfile) generateArgs.push("--clarification-instruction-profile", args.clarificationInstructionProfile);
if (args.approvalPolicyRouter) generateArgs.push("--approval-policy-router", args.approvalPolicyRouter);
if (args.codexTransport) generateArgs.push("--codex-transport", args.codexTransport);
if (args.codexApprovalPolicy) generateArgs.push("--codex-approval-policy", args.codexApprovalPolicy);

const evaluateArgs = ["scripts/evaluate_official.py", "--run-id", args.runId, "--samples", args.samples];
if (args.evalWorkers) evaluateArgs.push("--num-workers", String(args.evalWorkers));
if (args.reuseExistingEval) evaluateArgs.push("--reuse-existing");

await run(process.execPath, generateArgs);
await run("python3", evaluateArgs);
const summarizeArgs = ["scripts/summarize_passk.py", "--run-id", args.runId, "--samples", args.samples, "--k", String(args.k)];
if (args.humanKb) summarizeArgs.push("--human-kb", args.humanKb);
await run("python3", summarizeArgs);

const summaryPath = path.join(runDir, "summary.md");
const summary = await fs.readFile(summaryPath, "utf8");
console.log("");
console.log(summary.trim());
