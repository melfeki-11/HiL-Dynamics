import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { opencodeConfig } from "../src/shared/config.mjs";
import { resolveHarnesses } from "../src/harnesses/index.mjs";
import { acquirePort, availableOpencodePorts, opencodePortRange, releasePort } from "../src/harnesses/opencode/server_pool.mjs";
import { appendJsonl, readJsonl } from "../src/shared/io.mjs";

async function tempDir() {
  return fs.mkdtemp(path.join(os.tmpdir(), "opencode-test-"));
}

const CANT_ANSWER = "can't answer (perhaps transient hiccup)";
const OBS_CAP = 8000;

function cap(s, limit) {
  const str = String(s || "");
  return str.length > limit ? `${str.slice(0, limit)}… [truncated]` : str;
}

function askResolutionQueueFromEvents(events) {
  const queue = [];
  for (const ev of Array.isArray(events) ? events : []) {
    if (ev?.type !== "human_input_result") continue;
    const resolution = String(ev?.result?.resolution ?? "");
    if (!resolution.trim()) continue;
    queue.push(resolution);
  }
  return queue;
}

function canonicalAskObservation(obs) {
  const text = String(obs ?? "").trim();
  if (!text) return CANT_ANSWER;
  if (text.startsWith("[no observation")) return CANT_ANSWER;
  if (text.startsWith("[error]")) return CANT_ANSWER;
  return text;
}

function isAskHumanToolName(toolName) {
  const normalized = String(toolName || "").trim().toLowerCase();
  if (!normalized) return false;
  if (normalized === "ask_human" || normalized.endsWith(".ask_human")) return true;
  return normalized.includes("ask_human");
}

function extractAskQuestion({ input, state, part }) {
  const candidates = [
    input?.question,
    input?.ask_human?.question,
    input?.arguments?.question,
    input?.input?.question,
    state?.input?.question,
    part?.input?.question,
    part?.args?.question,
    part?.tool?.input?.question,
    part?.tool?.arguments?.question,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "string") return candidate;
  }
  return "";
}

function extractTrajectory(serverEvents) {
  const steps = [];
  const emittedStepIndexByPartId = new Map();
  for (const ev of serverEvents) {
    if (ev?.type !== "message.part.updated") continue;
    const part = ev?.properties?.part || {};
    const partType = String(part.type || "").toLowerCase();
    if (partType !== "tool") continue;
    const partId = part.id || part.partID || part.partId || null;
    const toolName = String(part.tool?.name || part.tool || part.name || "");
    const state = part.state || {};
    const status = String(state.status || part.status || "").toLowerCase();
    const isRunningStatus = status === "running" || status === "in_progress" || status === "started";
    const input = state.input || part.input || part.args || {};
    const output = state.output || part.output || part.result || "";
    const hasInputObject = input && typeof input === "object" && Object.keys(input).length > 0;
    const hasOutputText = String(output ?? "").trim().length > 0;
    if (!toolName.trim() && !hasInputObject && !hasOutputText) continue;
    if (partId && isRunningStatus && !hasOutputText) continue;
    const obs = hasOutputText ? cap(String(output), OBS_CAP) : "[no observation returned by tool]";
    let step;
    if (isAskHumanToolName(toolName)) {
      const q = extractAskQuestion({ input, state, part });
      step = { thought: "", act: cap(`ask_human [custom_tool] ${q}`, 4000), obs };
    } else if (toolName === "shell" || toolName === "bash") {
      const cmd = String(input.command || input.cmd || "");
      step = { thought: "", act: cap(cmd || "shell: [missing command]", 4000), obs };
    } else {
      let inputStr;
      try { inputStr = JSON.stringify(input); } catch { inputStr = String(input); }
      if (!inputStr || inputStr === "{}") inputStr = "[no args]";
      const renderedName = toolName.trim() || "unknown_tool";
      step = { thought: "", act: `${renderedName}: ${inputStr}`, obs };
    }
    if (partId && emittedStepIndexByPartId.has(partId)) {
      steps[emittedStepIndexByPartId.get(partId)] = step;
    } else {
      if (partId) emittedStepIndexByPartId.set(partId, steps.length);
      steps.push(step);
    }
  }
  return steps;
}

function askQuestionQueueFromEvents(events) {
  const queue = [];
  const rawByRequestId = new Map();
  for (const ev of Array.isArray(events) ? events : []) {
    if (ev?.type !== "human_input_raw_event") continue;
    const requestType = String(ev?.request_type || "");
    if (requestType !== "clarification" && requestType !== "elicitation") continue;
    const requestId = String(ev?.request_id ?? "").trim();
    if (!requestId) continue;
    rawByRequestId.set(requestId, String(ev?.question ?? ""));
  }
  for (const ev of Array.isArray(events) ? events : []) {
    if (ev?.type !== "human_input_result") continue;
    const requestId = String(ev?.request_id ?? "").trim();
    if (!requestId) continue;
    if (!rawByRequestId.has(requestId)) continue;
    queue.push(rawByRequestId.get(requestId) ?? "");
  }
  return queue;
}

function normalizeAskObservations(trajectorySteps, events) {
  const askResolutionQueue = askResolutionQueueFromEvents(events);
  const askQuestionQueue = askQuestionQueueFromEvents(events);
  return (Array.isArray(trajectorySteps) ? trajectorySteps : []).map((step) => {
    if (!step || typeof step !== "object") return step;
    const act = String(step.act || "");
    if (!act.trim().startsWith("ask_human")) return step;
    let normalizedAct = act;
    if (/^ask_human\s+\[custom_tool\]\s*$/.test(act.trim()) && askQuestionQueue.length > 0) {
      const matchedQuestion = String(askQuestionQueue.shift() ?? "");
      if (matchedQuestion.length > 0) {
        normalizedAct = `ask_human [custom_tool] ${matchedQuestion}`;
      }
    }
    let obs = step.obs;
    if (askResolutionQueue.length > 0) obs = askResolutionQueue.shift();
    return {
      ...step,
      act: cap(normalizedAct, 4000),
      obs: cap(canonicalAskObservation(obs), OBS_CAP),
    };
  });
}

test("resolveHarnesses includes OpenCode in all harness runs", () => {
  assert.equal(resolveHarnesses("opencode")[0].name, "opencode");
  assert.deepEqual(resolveHarnesses("all").map((harness) => harness.name), ["claude-code", "codex", "opencode"]);
});

test("OpenCode LiteLLM config uses the default provider/model shape without leaking secrets in model id", async () => {
  const oldKey = process.env.LITELLM_API_KEY;
  const oldBase = process.env.LITELLM_BASE_URL;
  process.env.LITELLM_API_KEY = "unit-test-litellm-key";
  process.env.LITELLM_BASE_URL = "http://127.0.0.1:4000";
  try {
    const defaultConfig = await opencodeConfig();
    assert.equal(defaultConfig.model, "litellm/fireworks_ai/glm-5p1");
    assert.equal(defaultConfig.provider.litellm.options.apiKey, "{env:LITELLM_API_KEY}");
    assert.equal(defaultConfig.provider.litellm.models["fireworks_ai/glm-5p1"].tool_call, true);

    const config = await opencodeConfig({ model: "gemini/gemini-3.1-pro-preview-customtools" });
    assert.equal(config.model, "litellm/gemini/gemini-3.1-pro-preview-customtools");
    assert.equal(config.provider.litellm.npm, "@ai-sdk/openai-compatible");
    assert.equal(config.provider.litellm.options.baseURL, "http://127.0.0.1:4000/v1");
    assert.equal(config.provider.litellm.options.apiKey, "{env:LITELLM_API_KEY}");
    assert.equal(JSON.stringify(config).includes("unit-test-litellm-key"), false);
    assert.equal(config.provider.litellm.models["gemini/gemini-3.1-pro-preview-customtools"].tool_call, true);
    assert.equal(config.provider.litellm.models["gemini/gemini-3.1-pro-preview-customtools"].reasoning, false);
  } finally {
    if (oldKey === undefined) delete process.env.LITELLM_API_KEY;
    else process.env.LITELLM_API_KEY = oldKey;
    if (oldBase === undefined) delete process.env.LITELLM_BASE_URL;
    else process.env.LITELLM_BASE_URL = oldBase;
  }
});

async function findFreeRange(size) {
  return 45000 + Math.floor(Math.random() * 500) * Math.max(10, size);
}

function restoreEnv(snapshot) {
  for (const [key, value] of Object.entries(snapshot)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
}

test("OpenCode port leasing uses lock files and releases leases", async () => {
  const dir = await tempDir();
  const oldEnv = {
    OPENCODE_PORT_START: process.env.OPENCODE_PORT_START,
    OPENCODE_PORT_END: process.env.OPENCODE_PORT_END,
    OPENCODE_PORT_LOCK_DIR: process.env.OPENCODE_PORT_LOCK_DIR,
    OPENCODE_PORT_SKIP_BIND_CHECK: process.env.OPENCODE_PORT_SKIP_BIND_CHECK,
  };
  process.env.OPENCODE_PORT_SKIP_BIND_CHECK = "1";
  const start = await findFreeRange(2);
  process.env.OPENCODE_PORT_START = String(start);
  process.env.OPENCODE_PORT_END = String(start + 1);
  process.env.OPENCODE_PORT_LOCK_DIR = path.join(dir, "locks");
  const leased = [];
  try {
    assert.equal(opencodePortRange().size, 2);
    assert.equal(await availableOpencodePorts(), 2);
    const [first, second] = await Promise.all([acquirePort({ owner: "unit-a" }), acquirePort({ owner: "unit-b" })]);
    leased.push(first, second);
    assert.notEqual(first, second);
    assert.equal(await availableOpencodePorts(), 0);
    await fs.access(path.join(process.env.OPENCODE_PORT_LOCK_DIR, `${first}.lock`));
    await fs.access(path.join(process.env.OPENCODE_PORT_LOCK_DIR, `${second}.lock`));
  } finally {
    await Promise.all(leased.map((port) => releasePort(port)));
    restoreEnv(oldEnv);
  }
});

async function runChildPortLease(scriptPath, env, portsPath) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [scriptPath, portsPath], {
      env: { ...process.env, ...env },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`child port lease exited ${code}: ${stderr || stdout}`));
    });
  });
}

test("OpenCode port leases are collision-free across processes", async () => {
  const dir = await tempDir();
  const start = await findFreeRange(2);
  const scriptPath = path.join(dir, "child-port-lease.mjs");
  const portsPath = path.join(dir, "leased-ports.log");
  const moduleUrl = pathToFileURL(path.resolve("src/harnesses/opencode/server_pool.mjs")).href;
  await fs.writeFile(
    scriptPath,
    `
      import fs from "node:fs/promises";
      import { acquirePort, releasePort } from ${JSON.stringify(moduleUrl)};
      const [portsPath] = process.argv.slice(2);
      const port = await acquirePort({ owner: "child:" + process.pid });
      await fs.appendFile(portsPath, String(port) + "\\n", "utf8");
      await new Promise((resolve) => setTimeout(resolve, 150));
      await releasePort(port);
    `,
    "utf8"
  );
  const env = {
    OPENCODE_PORT_START: String(start),
    OPENCODE_PORT_END: String(start + 1),
    OPENCODE_PORT_LOCK_DIR: path.join(dir, "locks"),
    OPENCODE_PORT_SKIP_BIND_CHECK: "1",
  };
  await Promise.all([runChildPortLease(scriptPath, env, portsPath), runChildPortLease(scriptPath, env, portsPath)]);
  const [first, second] = (await fs.readFile(portsPath, "utf8"))
    .trim()
    .split(/\r?\n/)
    .map((value) => Number(value));
  assert.notEqual(first, second);
  assert.deepEqual([first, second].sort((a, b) => a - b), [start, start + 1]);
});

test("OpenCode native events normalize into shared trajectory fields", async () => {
  const dir = await tempDir();
  const trajectoryFile = path.join(dir, "run-1", "trajectories", "opencode", "smoke_prefix_format", "attempt-1", "trajectory.jsonl");
  await appendJsonl(trajectoryFile, {
    type: "opencode_event",
    timestamp: "2026-05-01T00:00:00.000Z",
    event: {
      type: "permission.updated",
      properties: {
        id: "perm-1",
        type: "bash",
        sessionID: "session-1",
        messageID: "message-1",
        title: "Run npm test?",
        pattern: "npm test",
        metadata: { command: "npm test" },
        time: { created: 1 },
      },
    },
  });
  await appendJsonl(trajectoryFile, {
    type: "opencode_event",
    timestamp: "2026-05-01T00:00:01.000Z",
    event: {
      type: "message.part.updated",
      properties: {
        part: {
          type: "patch",
          files: ["src/labeler.py"],
        },
      },
    },
  });
  const events = await readJsonl(trajectoryFile);
  assert.equal(events[0].event_type, "permission_request");
  assert.equal(events[0].normalized_request_type, "permission");
  assert.equal(events[0].question, "Run npm test?");
  assert.equal(events[0].tool_args.permission_id, "perm-1");
  assert.equal(events[1].event_type, "file_edit");
  assert.deepEqual(events[1].files_changed, ["src/labeler.py"]);
});

test("OpenCode LiteLLM proxy strips Gemini-unsupported params and reports usage", async () => {
  const receivedBodies = [];
  const upstream = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      receivedBodies.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({
        id: "chatcmpl-test",
        choices: [],
        usage: { prompt_tokens: 3, completion_tokens: 5, total_tokens: 8 },
      }));
    });
  });
  await new Promise((resolve, reject) => {
    upstream.once("error", reject);
    upstream.listen(0, "127.0.0.1", resolve);
  });

  const upstreamPort = upstream.address().port;
  const proxy = spawn(process.execPath, ["src/hil_swe/litellm_drop_params_proxy.mjs"], {
    env: {
      ...process.env,
      REAL_LITELLM_URL: `http://127.0.0.1:${upstreamPort}`,
      LITELLM_API_KEY: "unit-test-key",
      LITELLM_PROXY_TARGET_MODEL: "gemini/gemini-3.1-pro",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stderr = "";
  proxy.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
  try {
    const portLine = await new Promise((resolve, reject) => {
      let stdout = "";
      proxy.stdout.on("data", (chunk) => {
        stdout += chunk.toString();
        const nl = stdout.indexOf("\n");
        if (nl !== -1) resolve(stdout.slice(0, nl).trim());
      });
      proxy.on("exit", (code) => reject(new Error(`proxy exited early ${code}: ${stderr}`)));
      proxy.on("error", reject);
    });
    assert.match(portLine, /^PROXY_PORT=\d+$/);
    const proxyPort = Number(portLine.split("=")[1]);

    const response = await fetch(`http://127.0.0.1:${proxyPort}/v1/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model: "gemini/gemini-3.1-pro",
        messages: [{ role: "user", content: "ping" }],
        tool_choice: "auto",
        reasoning_effort: "high",
        reasoning: { effort: "high" },
      }),
    });
    assert.equal(response.status, 200);
    await response.json();

    assert.equal(receivedBodies.length, 1);
    assert.equal(receivedBodies[0].tool_choice, undefined);
    assert.equal(receivedBodies[0].reasoning_effort, undefined);
    assert.equal(receivedBodies[0].reasoning, undefined);
    assert.equal(receivedBodies[0].drop_params, true);

    const stats = await (await fetch(`http://127.0.0.1:${proxyPort}/__stats`)).json();
    assert.equal(stats.llm_call_count, 1);
    assert.equal(stats.input_tokens, 3);
    assert.equal(stats.output_tokens, 5);
    assert.equal(stats.total_tokens, 8);
    assert.equal(stats.stripped_params.tool_choice, 1);
    assert.equal(stats.stripped_params.reasoning_effort, 1);
    assert.equal(stats.stripped_params.reasoning, 1);
  } finally {
    proxy.kill("SIGTERM");
    await new Promise((resolve) => upstream.close(resolve));
  }
});

test("normalizeAskObservations uses sidecar result when ask obs is empty", () => {
  const trajectory = [{ thought: "", act: "ask_human what is X?", obs: "" }];
  const events = [
    { type: "human_input_result", result: { resolution: "Use foo=bar." } },
  ];
  const out = normalizeAskObservations(trajectory, events);
  assert.equal(out[0].obs, "Use foo=bar.");
});

test("normalizeAskObservations preserves canonical irrelevant/cant-answer resolutions", () => {
  const trajectory = [
    { thought: "", act: "ask_human q1", obs: "" },
    { thought: "", act: "ask_human q2", obs: "" },
  ];
  const events = [
    { type: "human_input_result", result: { resolution: "irrelevant question" } },
    { type: "human_input_result", result: { resolution: "can't answer (perhaps transient hiccup)" } },
  ];
  const out = normalizeAskObservations(trajectory, events);
  assert.equal(out[0].obs, "irrelevant question");
  assert.equal(out[1].obs, "can't answer (perhaps transient hiccup)");
});

test("normalizeAskObservations falls back to can't-answer for malformed ask obs", () => {
  const trajectory = [
    { thought: "", act: "ask_human q1", obs: "[error] tool failed" },
    { thought: "", act: "ask_human q2", obs: "[no observation — tool call was interrupted]" },
    { thought: "", act: "ask_human q3", obs: "" },
  ];
  const out = normalizeAskObservations(trajectory, []);
  assert.equal(out[0].obs, CANT_ANSWER);
  assert.equal(out[1].obs, CANT_ANSWER);
  assert.equal(out[2].obs, CANT_ANSWER);
});

test("normalizeAskObservations does not touch non-ask steps", () => {
  const trajectory = [
    { thought: "", act: "bash: ls -la", obs: "ok" },
    { thought: "", act: "Edit: a.py", obs: "" },
  ];
  const out = normalizeAskObservations(trajectory, [
    { type: "human_input_result", result: { resolution: "Use foo=bar." } },
  ]);
  assert.equal(out[0].obs, "ok");
  assert.equal(out[1].obs, "");
});

test("extractTrajectory canonicalizes custom ask tool act format", () => {
  const events = [{
    type: "message.part.updated",
    properties: {
      part: {
        type: "tool",
        tool: "ask_human",
        state: {
          input: { question: "What should happen on timeout?" },
          output: "Return 504.",
        },
      },
    },
  }];
  const steps = extractTrajectory(events);
  assert.equal(steps[0].act, "ask_human [custom_tool] What should happen on timeout?");
});

test("extractTrajectory handles namespaced ask tool names and nested args", () => {
  const events = [{
    type: "message.part.updated",
    properties: {
      part: {
        type: "tool",
        tool: "ask_human_ask_human",
        args: { ask_human: { question: "Which module owns parsing?" } },
        result: "parser.mjs",
      },
    },
  }];
  const steps = extractTrajectory(events);
  assert.equal(steps[0].act, "ask_human [custom_tool] Which module owns parsing?");
  assert.equal(steps[0].obs, "parser.mjs");
});

test("normalizeAskObservations fills empty ask question text from sidecar raw events", () => {
  const trajectory = [{ thought: "", act: "ask_human [custom_tool] ", obs: "" }];
  const events = [
    { type: "human_input_raw_event", request_id: "r1", request_type: "clarification", question: "What timeout value should be used?" },
    { type: "human_input_result", request_id: "r1", result: { resolution: "Use 30 seconds." } },
  ];
  const out = normalizeAskObservations(trajectory, events);
  assert.equal(out[0].act, "ask_human [custom_tool] What timeout value should be used?");
  assert.equal(out[0].obs, "Use 30 seconds.");
});

test("normalizeAskObservations does not contaminate from different request text", () => {
  const trajectory = [{ thought: "", act: "ask_human [custom_tool] ", obs: "" }];
  const events = [
    { type: "human_input_raw_event", request_id: "r_old", request_type: "clarification", question: "Previous unrelated question" },
    { type: "human_input_raw_event", request_id: "r_now", request_type: "clarification", question: "" },
    { type: "human_input_result", request_id: "r_now", result: { resolution: "Resolved." } },
  ];
  const out = normalizeAskObservations(trajectory, events);
  assert.equal(out[0].act, "ask_human [custom_tool] ");
  assert.equal(out[0].obs, "Resolved.");
});

test("extractTrajectory drops malformed blank tool events", () => {
  const steps = extractTrajectory([{
    type: "message.part.updated",
    properties: { part: { type: "tool", tool: "", state: { input: {}, output: "" } } },
  }]);
  assert.deepEqual(steps, []);
});

test("extractTrajectory keeps latest update for repeated part id", () => {
  const steps = extractTrajectory([
    {
      type: "message.part.updated",
      properties: {
        part: {
          id: "part-1",
          type: "tool",
          tool: "shell",
          state: { status: "running", input: {}, output: "" },
        },
      },
    },
    {
      type: "message.part.updated",
      properties: {
        part: {
          id: "part-1",
          type: "tool",
          tool: "shell",
          state: { status: "completed", input: { command: "ls -la" }, output: "ok" },
        },
      },
    },
  ]);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].act, "ls -la");
  assert.equal(steps[0].obs, "ok");
});

test("extractTrajectory avoids empty generic input rendering", () => {
  const steps = extractTrajectory([{
    type: "message.part.updated",
    properties: {
      part: {
        id: "part-2",
        type: "tool",
        tool: "read",
        state: { status: "completed", input: {}, output: "" },
      },
    },
  }]);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].act, "read: [no args]");
  assert.equal(steps[0].obs, "[no observation returned by tool]");
});
