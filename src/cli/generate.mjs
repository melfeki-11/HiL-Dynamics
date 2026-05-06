#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";
import {
  DEFAULT_ASK_HUMAN_MODEL,
  DEFAULT_ASK_HUMAN_SEED,
  DEFAULT_CODEX_REASONING_EFFORT,
  DEFAULT_OPENCODE_MODEL,
  dataDir,
  defaultGenerateConcurrency,
  evalsDir,
} from "../shared/config.mjs";
import { CLARIFICATION_INSTRUCTION_PROFILES, DEFAULT_CLARIFICATION_INSTRUCTION_PROFILE, loadSamples } from "../shared/dataset.mjs";
import { appendJsonl, ensureDir, pathExists, writeJsonAtomic, writeText } from "../shared/io.mjs";
import { collectRunPredictions, comparePredictions } from "../shared/predictions.mjs";
import { redactString } from "../shared/redact.mjs";
import { runBounded } from "../shared/worker_pool.mjs";
import { resolveHarnesses } from "../harnesses/index.mjs";

function parseArgs(argv) {
  const args = {
    input: path.join(dataDir, "swebench_pro_samples.jsonl"),
    harness: "claude-code",
    runId: new Date().toISOString().replace(/[:.]/g, "-"),
    mode: process.env.HARNESS_MODE || "ask_human",
    k: 1,
    limit: undefined,
    model: undefined,
    claudeModel: undefined,
    claudeThinking: process.env.CLAUDE_CODE_THINKING || undefined,
    claudeEffort: process.env.CLAUDE_CODE_EFFORT || undefined,
    codexModel: undefined,
    opencodeModel: process.env.OPENCODE_MODEL || DEFAULT_OPENCODE_MODEL,
    modelReasoningEffort: process.env.CODEX_MODEL_REASONING_EFFORT || DEFAULT_CODEX_REASONING_EFFORT,
    maxTurns: 40,
    permissionMode: "acceptEdits",
    concurrency: undefined,
    attemptTimeoutMs: Number(process.env.HARNESS_ATTEMPT_TIMEOUT_MS || 0),
    resume: false,
    humanKb: process.env.HARNESS_HUMAN_KB || undefined,
    askHumanCache: process.env.HARNESS_ASK_HUMAN_CACHE || undefined,
    askHumanReplay: false,
    askHumanModel: process.env.ASK_HUMAN_MODEL || DEFAULT_ASK_HUMAN_MODEL,
    askHumanSeed: Number(process.env.ASK_HUMAN_SEED || DEFAULT_ASK_HUMAN_SEED),
    humanSimulatorMode: process.env.HARNESS_HUMAN_SIMULATOR_MODE || undefined,
    clarificationInstructionProfile: process.env.HARNESS_CLARIFICATION_INSTRUCTION_PROFILE || DEFAULT_CLARIFICATION_INSTRUCTION_PROFILE,
    approvalPolicyRouter: process.env.HARNESS_APPROVAL_POLICY_ROUTER || "safe-looking",
    codexTransport: process.env.CODEX_TRANSPORT || undefined,
    codexApprovalPolicy: process.env.CODEX_APPROVAL_POLICY || undefined,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--input") args.input = argv[++i];
    else if (arg === "--harness") args.harness = argv[++i];
    else if (arg === "--run-id") args.runId = argv[++i];
    else if (arg === "--mode") args.mode = argv[++i];
    else if (arg === "--k") args.k = Number(argv[++i]);
    else if (arg === "--limit") args.limit = Number(argv[++i]);
    else if (arg === "--model") args.model = argv[++i];
    else if (arg === "--claude-model") args.claudeModel = argv[++i];
    else if (arg === "--claude-thinking") args.claudeThinking = argv[++i];
    else if (arg === "--claude-effort") args.claudeEffort = argv[++i];
    else if (arg === "--codex-model") args.codexModel = argv[++i];
    else if (arg === "--opencode-model") args.opencodeModel = argv[++i];
    else if (arg === "--model-reasoning-effort") args.modelReasoningEffort = argv[++i];
    else if (arg === "--max-turns") args.maxTurns = Number(argv[++i]);
    else if (arg === "--permission-mode") args.permissionMode = argv[++i];
    else if (arg === "--concurrency") args.concurrency = Number(argv[++i]);
    else if (arg === "--attempt-timeout-ms") args.attemptTimeoutMs = Number(argv[++i]);
    else if (arg === "--resume") args.resume = true;
    else if (arg === "--human-kb") args.humanKb = argv[++i];
    else if (arg === "--human-cache") args.askHumanCache = argv[++i];
    else if (arg === "--ask-human-cache") args.askHumanCache = argv[++i];
    else if (arg === "--ask-human-replay") args.askHumanReplay = true;
    else if (arg === "--ask-human-model") args.askHumanModel = argv[++i];
    else if (arg === "--ask-human-seed") args.askHumanSeed = Number(argv[++i]);
    else if (arg === "--human-simulator-mode") args.humanSimulatorMode = argv[++i];
    else if (arg === "--clarification-instruction-profile") args.clarificationInstructionProfile = argv[++i];
    else if (arg === "--enable-human-input") args.humanSimulatorMode = argv[++i] === "false" ? "off" : "live";
    else if (arg === "--approval-policy-router") args.approvalPolicyRouter = argv[++i];
    else if (arg === "--codex-transport") args.codexTransport = argv[++i];
    else if (arg === "--codex-approval-policy") args.codexApprovalPolicy = argv[++i];
    else throw new Error(`Unknown argument: ${arg}`);
  }
  if (!Number.isInteger(args.k) || args.k < 1) throw new Error("--k must be a positive integer");
  if (args.concurrency !== undefined && (!Number.isInteger(args.concurrency) || args.concurrency < 1)) throw new Error("--concurrency must be a positive integer");
  if (!Number.isInteger(args.attemptTimeoutMs) || args.attemptTimeoutMs < 0) throw new Error("--attempt-timeout-ms must be a non-negative integer");
  if (!Number.isInteger(args.askHumanSeed)) throw new Error("--ask-human-seed must be an integer");
  if (!["ask_human", "full_info", "baseline"].includes(args.mode)) throw new Error("--mode must be ask_human, full_info, or baseline");
  if (args.claudeThinking !== undefined && !["adaptive", "disabled"].includes(args.claudeThinking)) {
    throw new Error("--claude-thinking must be adaptive or disabled");
  }
  if (args.claudeEffort !== undefined && !["low", "medium", "high", "xhigh", "max"].includes(args.claudeEffort)) {
    throw new Error("--claude-effort must be low, medium, high, xhigh, or max");
  }
  if (args.humanSimulatorMode !== undefined && !["off", "live", "replay"].includes(args.humanSimulatorMode)) throw new Error("--human-simulator-mode must be off, live, or replay");
  if (!CLARIFICATION_INSTRUCTION_PROFILES.has(args.clarificationInstructionProfile)) {
    throw new Error(`--clarification-instruction-profile must be one of: ${[...CLARIFICATION_INSTRUCTION_PROFILES].join(", ")}`);
  }
  if (args.codexTransport !== undefined && !["exec", "app-server"].includes(args.codexTransport)) throw new Error("--codex-transport must be exec or app-server");
  if (!["safe-looking", "deny", "fail"].includes(args.approvalPolicyRouter)) throw new Error("--approval-policy-router must be safe-looking, deny, or fail");
  return args;
}

function predictionKey(prediction) {
  return `${prediction.harness || ""}\0${prediction.instance_id || ""}\0${prediction.attempt_index || 0}`;
}

function predictionPrefix(args, harnessName, instanceId, attemptIndex) {
  return `${args.runId}__${harnessName}__${instanceId}__attempt-${attemptIndex}`;
}

async function writeFailurePrediction({ job, args, runDir, error }) {
  const instanceId = job.row.instance_id;
  const attemptDir = path.join(runDir, "trajectories", job.harness.name, instanceId, `attempt-${job.attemptIndex}`);
  const trajectoryFile = path.join(attemptDir, "trajectory.jsonl");
  const prefix = predictionPrefix(args, job.harness.name, instanceId, job.attemptIndex);
  const errorText = redactString(String(error?.stack || error));
  const prediction = {
    instance_id: instanceId,
    patch: "",
    prefix,
    harness: job.harness.name,
    mode: args.mode,
    model: args.model,
    attempt_index: job.attemptIndex,
    run_id: args.runId,
    sdk_error: errorText,
    generation_failed: true,
  };
  await ensureDir(attemptDir);
  await appendJsonl(trajectoryFile, { type: "attempt_error", timestamp: new Date().toISOString(), error: errorText });
  await writeText(path.join(attemptDir, "patch.diff"), "");
  await writeJsonAtomic(path.join(attemptDir, "prediction.json"), prediction);
  await appendJsonl(trajectoryFile, {
    type: "submission",
    timestamp: new Date().toISOString(),
    prediction_path: path.join(attemptDir, "prediction.json"),
    patch_path: path.join(attemptDir, "patch.diff"),
    prefix,
    patch_bytes: 0,
    sdk_error: errorText,
    generation_failed: true,
  });
  await appendJsonl(trajectoryFile, { type: "attempt_end", timestamp: new Date().toISOString(), patch_bytes: 0, sdk_error: errorText, generation_failed: true });
  return prediction;
}

const args = parseArgs(process.argv.slice(2));
const selectedHarnesses = resolveHarnesses(args.harness);
const allSamples = await loadSamples(args.input);
if (args.limit !== undefined && args.limit > allSamples.length) {
  throw new Error(`Requested --limit ${args.limit}, but ${args.input} contains only ${allSamples.length} samples. Run npm run download-samples -- --limit ${args.limit} first.`);
}
const samples = args.limit ? allSamples.slice(0, args.limit) : allSamples;
const runDir = path.join(evalsDir, args.runId);
if (args.humanSimulatorMode === "off") {
  args.humanKb = undefined;
  args.askHumanReplay = false;
} else if (args.humanSimulatorMode === "replay") {
  args.askHumanReplay = true;
}
if (args.mode === "ask_human" && !args.humanKb) {
  const inferredKb = path.join(path.dirname(args.input), "kb.json");
  if (await pathExists(inferredKb)) args.humanKb = inferredKb;
}
if (args.mode !== "ask_human") {
  args.humanKb = undefined;
  args.humanKbSource = undefined;
  args.askHumanCache = undefined;
  args.askHumanReplay = false;
}
await ensureDir(runDir);
if (args.humanKb) {
  args.humanKbSource = path.resolve(args.humanKb);
  const snapshotPath = path.join(runDir, "human-kb.json");
  if (args.humanKbSource !== path.resolve(snapshotPath) && (!args.resume || !(await pathExists(snapshotPath)))) {
    await fs.copyFile(args.humanKbSource, snapshotPath);
  }
  args.humanKb = snapshotPath;
}
if (!args.askHumanCache && args.humanKb) args.askHumanCache = path.join(runDir, "ask-human-cache.json");
if (!args.codexTransport) args.codexTransport = args.humanKb ? "app-server" : "exec";
if (!args.codexApprovalPolicy) args.codexApprovalPolicy = args.humanKb ? "on-request" : "never";

const jobs = [];
for (const harness of selectedHarnesses) {
  for (const [sampleIndex, row] of samples.entries()) {
    for (let attemptIndex = 1; attemptIndex <= args.k; attemptIndex += 1) {
      jobs.push({ harness, row, sampleIndex, attemptIndex });
    }
  }
}

const concurrency = args.concurrency || defaultGenerateConcurrency(jobs.length);
const failures = [];
const completedByKey = new Map();
let progressWrite = Promise.resolve();
console.log(`Starting ${jobs.length} attempts across ${selectedHarnesses.map((h) => h.name).join(", ")} with concurrency=${concurrency}`);

function completedPredictions() {
  return [...completedByKey.values()].sort(comparePredictions);
}

function queueProgressWrite() {
  progressWrite = progressWrite.then(async () => {
    const predictions = completedPredictions();
    await writeJsonAtomic(path.join(runDir, "predictions.json"), predictions);
    await writeJsonAtomic(path.join(runDir, "generation-progress.json"), {
      run_id: args.runId,
      harness: args.harness,
      mode: args.mode,
      total_jobs: jobs.length,
      completed_predictions: predictions.length,
      failed_jobs: failures.length,
      concurrency,
      attempt_timeout_ms: args.attemptTimeoutMs,
      claude_model: args.claudeModel,
      claude_thinking: args.claudeThinking,
      claude_effort: args.claudeEffort,
      codex_model: args.codexModel,
      opencode_model: args.opencodeModel,
      model_reasoning_effort: args.modelReasoningEffort,
      human_kb: args.humanKb,
      human_kb_source: args.humanKbSource,
      ask_human_cache: args.askHumanCache,
      ask_human_replay: args.askHumanReplay,
      ask_human_model: args.askHumanModel,
      clarification_instruction_profile: args.clarificationInstructionProfile,
      human_simulator_mode: args.humanSimulatorMode,
      approval_policy_router: args.approvalPolicyRouter,
      codex_transport: args.codexTransport,
      codex_approval_policy: args.codexApprovalPolicy,
      failures,
    });
  });
  return progressWrite;
}

await runBounded(jobs, concurrency, async (job, _jobIndex, workerId) => {
  const harnessModel =
    job.harness.name === "claude-code"
      ? args.claudeModel
      : job.harness.name === "codex"
        ? args.codexModel
        : job.harness.name === "opencode"
          ? args.opencodeModel
          : undefined;
  const model = args.model || harnessModel || job.harness.defaultModel;
  const jobArgs = { ...args, model };
  console.log(`[worker ${workerId}] ${job.harness.name} ${job.row.instance_id} attempt ${job.attemptIndex}/${args.k}`);
  try {
    const prediction = await job.harness.runAttempt({ row: job.row, attemptIndex: job.attemptIndex, args: jobArgs, runDir });
    if (prediction) completedByKey.set(predictionKey(prediction), prediction);
  } catch (error) {
    const failure = { harness: job.harness.name, instance_id: job.row.instance_id, attempt_index: job.attemptIndex, error: redactString(String(error?.stack || error)) };
    failures.push(failure);
    console.error(`[worker ${workerId}] failed ${failure.harness} ${failure.instance_id} attempt ${failure.attempt_index}: ${failure.error}`);
    const prediction = await writeFailurePrediction({ job, args: jobArgs, runDir, error });
    completedByKey.set(predictionKey(prediction), prediction);
  }
  await queueProgressWrite();
});

await progressWrite;
const predictions = await collectRunPredictions(runDir);
await writeJsonAtomic(path.join(runDir, "predictions.json"), predictions);
await writeJsonAtomic(path.join(runDir, "generation-progress.json"), {
  run_id: args.runId,
  harness: args.harness,
  mode: args.mode,
  total_jobs: jobs.length,
  completed_predictions: predictions.length,
  failed_jobs: failures.length,
  concurrency,
  attempt_timeout_ms: args.attemptTimeoutMs,
  claude_model: args.claudeModel,
  claude_thinking: args.claudeThinking,
  claude_effort: args.claudeEffort,
  codex_model: args.codexModel,
  opencode_model: args.opencodeModel,
  model_reasoning_effort: args.modelReasoningEffort,
  human_kb: args.humanKb,
  human_kb_source: args.humanKbSource,
  ask_human_cache: args.askHumanCache,
  ask_human_replay: args.askHumanReplay,
  ask_human_model: args.askHumanModel,
  clarification_instruction_profile: args.clarificationInstructionProfile,
  human_simulator_mode: args.humanSimulatorMode,
  approval_policy_router: args.approvalPolicyRouter,
  codex_transport: args.codexTransport,
  codex_approval_policy: args.codexApprovalPolicy,
  failures,
});
await writeJsonAtomic(path.join(runDir, "attempts-index.json"), {
  run_id: args.runId,
  harness: args.harness,
  mode: args.mode,
  generated_at: new Date().toISOString(),
  concurrency,
  attempt_timeout_ms: args.attemptTimeoutMs,
  claude_model: args.claudeModel,
  claude_thinking: args.claudeThinking,
  claude_effort: args.claudeEffort,
  codex_model: args.codexModel,
  opencode_model: args.opencodeModel,
  model_reasoning_effort: args.modelReasoningEffort,
  human_kb: args.humanKb,
  human_kb_source: args.humanKbSource,
  ask_human_cache: args.askHumanCache,
  ask_human_replay: args.askHumanReplay,
  ask_human_model: args.askHumanModel,
  clarification_instruction_profile: args.clarificationInstructionProfile,
  human_simulator_mode: args.humanSimulatorMode,
  approval_policy_router: args.approvalPolicyRouter,
  codex_transport: args.codexTransport,
  codex_approval_policy: args.codexApprovalPolicy,
  failed_jobs: failures,
  predictions: predictions.map((prediction) => ({
    harness: prediction.harness,
    mode: prediction.mode,
    model: prediction.model,
    instance_id: prediction.instance_id,
    prefix: prediction.prefix,
    attempt_index: prediction.attempt_index,
    patch_bytes: Buffer.byteLength(prediction.patch || ""),
    sdk_error: prediction.sdk_error,
  })),
});
console.log(`Wrote ${predictions.length} predictions to ${path.join(runDir, "predictions.json")}`);
