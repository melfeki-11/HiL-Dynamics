#!/usr/bin/env node
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { dataDir, evalsDir, rootDir } from "../src/shared/config.mjs";
import { appendJsonl, writeJsonAtomic } from "../src/shared/io.mjs";

const execFileAsync = promisify(execFile);

function parseArgs(argv) {
  const args = { runId: process.env.RUN_ID || "clarification-smoke" };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--run-id") args.runId = argv[++i];
    else throw new Error(`Unknown argument: ${argv[i]}`);
  }
  return args;
}

async function readJson(filePath) {
  return JSON.parse(await fs.readFile(filePath, "utf8"));
}

async function readJsonl(filePath) {
  return (await fs.readFile(filePath, "utf8")).split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

function assert(condition, message, failures) {
  if (!condition) failures.push(message);
}

function hasNativeClarification(harnessName, events) {
  const nativeTypes = new Set(
    events
      .filter((event) => event.type === "human_input_raw_event" || event.type === "human_input_normalized_event")
      .map((event) => event.native_event_type || event.request?.native_event_type)
  );
  if (harnessName === "codex") return nativeTypes.has("codex.item/tool/requestUserInput") || nativeTypes.has("codex.item.tool.requestUserInput");
  if (harnessName === "claude-code") {
    return nativeTypes.has("claude.mcp.ask_human") || nativeTypes.has("claude.AskUserQuestion.canUseTool");
  }
  return false;
}

function hasAnsweredClarification(events) {
  return events.some(
    (event) =>
      event.type === "human_input_result" &&
      event.request_type === "clarification" &&
      event.result?.status === "answered" &&
      event.result?.blocker_id &&
      event.result.blocker_id !== "UNKNOWN"
  );
}

function hasRawAndNormalized(events) {
  return events.some((event) => event.type === "human_input_raw_event") && events.some((event) => event.type === "human_input_normalized_event");
}

async function runHiddenTests({ harnessName, patchPath, trajectoryFile }) {
  const row = JSON.parse((await fs.readFile(path.join(dataDir, "clarification_smoke.jsonl"), "utf8")).trim());
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), `clarification-hidden-${harnessName}-`));
  await fs.cp(path.join(rootDir, ".cache", "clarification-smoke", "repo"), tmp, { recursive: true, dereference: true });
  await execFileAsync("git", ["apply", patchPath], { cwd: tmp, maxBuffer: 10 * 1024 * 1024 });
  const testPatchPath = path.join(tmp, ".hidden-tests.patch");
  await fs.writeFile(testPatchPath, row.test_patch, "utf8");
  await execFileAsync("git", ["apply", testPatchPath], { cwd: tmp, maxBuffer: 10 * 1024 * 1024 });
  const startedAt = new Date().toISOString();
  const result = await new Promise((resolve) => {
    execFile(
      "python3",
      ["-m", "pytest", "-q", "test_labeler.py"],
      {
        cwd: tmp,
        env: { ...process.env, PYTEST_DISABLE_PLUGIN_AUTOLOAD: "1" },
        maxBuffer: 10 * 1024 * 1024,
        timeout: 30000,
      },
      (error, stdout, stderr) => {
        resolve({ code: error?.code ?? 0, signal: error?.signal || null, stdout, stderr });
      }
    );
  });
  const event = {
    type: "smoke_hidden_test_result",
    timestamp: new Date().toISOString(),
    started_at: startedAt,
    harness: harnessName,
    command: "python3 -m pytest -q",
    cwd: tmp,
    code: result.code,
    signal: result.signal,
    passed: result.code === 0,
    stdout: result.stdout,
    stderr: result.stderr,
  };
  const existingEvents = await readJsonl(trajectoryFile);
  if (!existingEvents.some((item) => item.type === "smoke_hidden_test_result" && item.harness === harnessName)) {
    await appendJsonl(trajectoryFile, event);
  }
  return event;
}

async function recomputeProcessMetrics(runId) {
  await execFileAsync("python3", ["scripts/process_metrics.py", "--run-id", runId, "--human-kb", "data/clarification_smoke_kb.json"], {
    cwd: rootDir,
    maxBuffer: 10 * 1024 * 1024,
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const runDir = path.join(evalsDir, args.runId);
  const predictions = await readJson(path.join(runDir, "predictions.json"));
  const failures = [];
  const verification = { run_id: args.runId, hidden_tests: [] };
  const requiredHarnesses = ["claude-code", "codex"];

  for (const harnessName of requiredHarnesses) {
    const prediction = predictions.find((item) => item.harness === harnessName && item.instance_id === "smoke_prefix_format");
    assert(prediction, `${harnessName}: missing prediction`, failures);
    if (!prediction) continue;
    assert(!prediction.sdk_error, `${harnessName}: sdk_error present`, failures);
    assert((prediction.patch || "").includes('return f"{prefix}: {name}"'), `${harnessName}: expected smoke patch not present`, failures);

    const trajectoryFile = path.join(runDir, "trajectories", harnessName, "smoke_prefix_format", "attempt-1", "trajectory.jsonl");
    const events = await readJsonl(trajectoryFile);
    assert(hasRawAndNormalized(events), `${harnessName}: missing raw or normalized human-input events`, failures);
    assert(hasNativeClarification(harnessName, events), `${harnessName}: missing expected native clarification event`, failures);
    assert(hasAnsweredClarification(events), `${harnessName}: missing answered clarification result`, failures);

    const patchPath = path.join(runDir, "trajectories", harnessName, "smoke_prefix_format", "attempt-1", "patch.diff");
    const hidden = await runHiddenTests({ harnessName, patchPath, trajectoryFile });
    verification.hidden_tests.push({ harness: harnessName, passed: hidden.passed, code: hidden.code, stdout: hidden.stdout, stderr: hidden.stderr });
    assert(hidden.passed, `${harnessName}: hidden smoke tests failed`, failures);
  }

  const cachePath = path.join(runDir, "ask-human-cache.json");
  const cache = await readJson(cachePath).catch(() => ({}));
  assert(Object.values(cache).some((entry) => entry.status === "answered" && entry.blocker_id !== "UNKNOWN"), "cache: missing answered oracle entry", failures);
  await writeJsonAtomic(path.join(runDir, "smoke_verification.json"), verification);
  await recomputeProcessMetrics(args.runId);

  if (failures.length > 0) {
    console.error(`Clarification smoke verification failed for ${runDir}:`);
    for (const failure of failures) console.error(`- ${failure}`);
    process.exitCode = 1;
    return;
  }
  console.log(`Clarification smoke verification passed for ${runDir}`);
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
